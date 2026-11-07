"""Free-tier OpenRouter broker, served on one unix socket per session.

The owner's key lives only in this (host) process. A session container sees nothing but
``/run/chudware-demo/provider.sock``; the socket identity *is* the session, so there is
no credential to steal inside the container. Wire contract (OpenAI-compatible):

    POST /v1/chat/completions   streaming allowed
    GET  /v1/models             only allowlisted models that OpenRouter still lists

Refusals are JSON ``{"error":{"code":...,"message":...}}``:
    403 model_not_allowed        model outside the ``:free`` allowlist
    429 demo_quota_exhausted     this session used its request/token allowance
    429 demo_ai_daily_limit      the owner's shared daily free allowance is spent
    429 demo_ai_rate_limited     OpenRouter is rate limiting right now (retry shortly)
    503 ai_unavailable           no key configured, or OpenRouter refused the key
Prompts, completions and the key are never logged; only per-session counters are.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .config import DemoConfig

log = logging.getLogger("chudware-demo.broker")

# OpenRouter-specific request fields that can route to a paid model, add paid plugins
# (web search) or fall back to another model. They are dropped before forwarding.
STRIPPED_FIELDS = frozenset({
    "models", "route", "provider", "plugins", "transforms", "preset", "web_search_options",
})
MAX_UPSTREAM_BODY = 16 * 1024 * 1024
MAX_ERROR_BODY = 64 * 1024
_DAILY_RE = re.compile(r"per[- _]?day|daily", re.IGNORECASE)
MESSAGES = {
    "model_not_allowed": "This demo only serves its list of free AI models.",
    "demo_quota_exhausted": "This demo session's AI allowance is used up; modeling still works.",
    "demo_ai_daily_limit": "The demo's free AI allowance for today is used up; modeling still works",
    "demo_ai_rate_limited": "The free AI model is busy right now; try again in a minute.",
    "ai_unavailable": "AI is not available in this demo right now; modeling still works.",
    "upstream_unavailable": "The AI provider could not be reached; try again shortly.",
    "invalid_request": "The request is not a valid chat completion request.",
    "request_too_large": "The request body is too large.",
    "not_found": "Not found.",
    "method_not_allowed": "Method not allowed.",
}

# Deterministic refusals a client must not retry: the `x-should-retry: false` header is
# honoured by OMP's OpenAI-compatible transport; without it a 429 is re-POSTed 5 times.
NO_RETRY = frozenset({"model_not_allowed", "demo_quota_exhausted", "demo_ai_daily_limit",
                      "ai_unavailable", "invalid_request", "request_too_large"})


def error_response(status: int, code: str, headers: dict[str, str] | None = None) -> JSONResponse:
    out = dict(headers or {})
    if code in NO_RETRY:
        out["x-should-retry"] = "false"
    return JSONResponse({"error": {"code": code, "message": MESSAGES.get(code, code)}},
                        status_code=status, headers=out or None)


@dataclass
class Usage:
    """One session's AI spend (counts forwarded requests and reported total tokens)."""

    requests: int = 0
    tokens: int = 0

    @classmethod
    def load(cls, path: Path) -> "Usage":
        try:
            raw = json.loads(path.read_text("utf-8"))
            return cls(max(0, int(raw["requests"])), max(0, int(raw["tokens"])))
        except (OSError, ValueError, KeyError, TypeError):
            return cls()

    def save(self, path: Path | None) -> None:
        if path is not None:
            _atomic_json(path, {"requests": self.requests, "tokens": self.tokens})


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        log.warning("could not persist %s", path.name, exc_info=True)


class DailyBudget:
    """Requests per UTC day across every session; 429 from upstream also zeroes it."""

    def __init__(self, limit: int, path: Path | None = None, clock: Callable[[], float] = time.time):
        self.limit, self.path, self.clock = limit, path, clock
        self.day, self.used, self.exhausted = self._today(), 0, False
        if path is not None:
            try:
                raw = json.loads(path.read_text("utf-8"))
                if int(raw["day"]) == self.day:
                    self.used, self.exhausted = max(0, int(raw["used"])), bool(raw["exhausted"])
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def _today(self) -> int:
        return int(self.clock() // 86400)

    def _roll(self) -> None:
        today = self._today()
        if today != self.day:
            self.day, self.used, self.exhausted = today, 0, False

    def _save(self) -> None:
        if self.path is not None:
            _atomic_json(self.path, {"day": self.day, "used": self.used, "exhausted": self.exhausted})

    def remaining(self) -> int:
        self._roll()
        return 0 if self.exhausted else max(0, self.limit - self.used)

    def try_consume(self) -> bool:
        if self.remaining() <= 0:
            return False
        self.used += 1
        self._save()
        return True

    def mark_exhausted(self) -> None:
        self._roll()
        self.exhausted = True
        self._save()


class ModelCatalog:
    """The allowlist restricted to models OpenRouter still lists (cached)."""

    def __init__(self, cfg: DemoConfig, client: httpx.AsyncClient, clock: Callable[[], float] = time.time):
        self.cfg, self.client, self.clock = cfg, client, clock
        self._items: list[dict[str, Any]] | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _entry(model: str, live: dict[str, Any] | None) -> dict[str, Any]:
        """An OpenAI-style model entry, carrying OpenRouter's own metadata when known."""
        live = live or {}
        item: dict[str, Any] = {"id": model, "object": "model",
                                "created": int(live.get("created") or 0),
                                "owned_by": model.split("/", 1)[0]}

        def number(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool)

        def strings(value: Any) -> bool:
            return isinstance(value, list) and all(isinstance(v, str) for v in value)

        if isinstance(live.get("name"), str):
            item["name"] = live["name"]
        if number(live.get("context_length")):
            item["context_length"] = live["context_length"]
        top = live.get("top_provider")
        if isinstance(top, dict):
            item["top_provider"] = {k: top[k] for k in ("context_length", "max_completion_tokens")
                                    if number(top.get(k))}
        arch = live.get("architecture")
        if isinstance(arch, dict):
            picked = {k: arch[k] for k in ("input_modalities", "output_modalities") if strings(arch.get(k))}
            picked.update({k: arch[k] for k in ("modality", "tokenizer") if isinstance(arch.get(k), str)})
            item["architecture"] = picked
        if strings(live.get("supported_parameters")):
            item["supported_parameters"] = live["supported_parameters"]
        return item

    async def listing(self) -> list[dict[str, Any]]:
        now = self.clock()
        if self._items is not None and now < self._expires:
            return self._items
        async with self._lock:
            now = self.clock()
            if self._items is not None and now < self._expires:
                return self._items
            try:
                resp = await self.client.get(
                    f"{self.cfg.openrouter_base}/models", timeout=15.0,
                    headers={"Accept": "application/json", "User-Agent": "chudware-demo-broker/1"})
                resp.raise_for_status()
                live = {m["id"]: m for m in resp.json()["data"]
                        if isinstance(m, dict) and isinstance(m.get("id"), str)}
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                log.warning("OpenRouter model catalogue unavailable; keeping the last known list")
                self._expires = now + 60
                if self._items is None:
                    self._items = [self._entry(m, None) for m in self.cfg.ai_models]
                return self._items
            self._items = [self._entry(m, live[m]) for m in self.cfg.ai_models if m in live]
            self._expires = now + self.cfg.ai_models_cache_seconds
            retired = [m for m in self.cfg.ai_models if m not in live]
            if retired:
                log.warning("allowlisted models no longer on OpenRouter: %s", ", ".join(retired))
            return self._items


@dataclass
class Lane:
    """One session's broker endpoint."""

    sid: str
    usage: Usage
    usage_path: Path | None
    app: Starlette
    sock: Path | None = None
    server: "_OwnedServer | None" = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)


class _OwnedServer(uvicorn.Server):
    """An embedded listener must not steal the host process's signal handlers."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def sanitize_chat_payload(payload: dict[str, Any], max_output: int) -> dict[str, Any] | None:
    """Forwardable copy of ``payload``, or None when it is malformed."""
    if not isinstance(payload.get("messages"), list):
        return None
    if "stream" in payload and not isinstance(payload["stream"], bool):
        return None
    clean = {k: v for k, v in payload.items() if k not in STRIPPED_FIELDS}
    present = False
    for key in ("max_tokens", "max_completion_tokens"):
        if key in clean:
            value = clean[key]
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            clean[key] = min(value, max_output) if value >= 1 else max_output
            present = True
    if not present:
        clean["max_tokens"] = max_output
    if clean.get("stream") is True:
        options = clean.get("stream_options")
        options = dict(options) if isinstance(options, dict) else {}
        options["include_usage"] = True
        clean["stream_options"] = options
    return clean


def usage_total(obj: Any) -> int | None:
    usage = obj.get("usage") if isinstance(obj, dict) else None
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    parts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
    if all(isinstance(p, int) and not isinstance(p, bool) and p >= 0 for p in parts):
        return parts[0] + parts[1]
    return None


class ProviderBroker:
    def __init__(self, cfg: DemoConfig, *, client: httpx.AsyncClient | None = None,
                 budget_path: Path | None = None, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8))
        self._own_client = client is None
        self.budget = DailyBudget(cfg.ai_daily_requests, budget_path, clock)
        self.catalog = ModelCatalog(cfg, self.client, clock)
        self.lanes: dict[str, Lane] = {}

    # -- public --------------------------------------------------------------
    def ai_remaining_today(self) -> int | None:
        return self.budget.remaining() if self.cfg.ai_enabled else None

    def make_lane(self, sid: str, usage: Usage, usage_path: Path | None = None) -> Lane:
        lane = Lane(sid, usage, usage_path, app=None)  # type: ignore[arg-type]
        lane.app = Starlette(routes=[Route(
            "/{path:path}", self._endpoint(lane),
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])])
        return lane

    async def open_session(self, sid: str, run_dir: Path, usage: Usage,
                           usage_path: Path | None = None) -> Lane:
        """Create ``run_dir/provider.sock`` (mode 0666 in a 0711 directory)."""
        await self.close_session(sid)
        run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(run_dir, 0o711)
        lane = self.make_lane(sid, usage, usage_path)
        lane.sock = run_dir / "provider.sock"
        with contextlib.suppress(FileNotFoundError):
            lane.sock.unlink()
        config = uvicorn.Config(
            lane.app, uds=str(lane.sock), lifespan="off", access_log=False, log_config=None,
            log_level=None, server_header=False, date_header=False, ws="none",
            timeout_keep_alive=60, limit_concurrency=16)
        lane.server = _OwnedServer(config)
        lane.task = asyncio.create_task(lane.server.serve(), name=f"provider-{sid[:8]}")
        deadline = time.monotonic() + 5
        while not lane.server.started:
            if lane.task.done() or time.monotonic() > deadline:
                await self._stop(lane)
                raise RuntimeError("provider socket did not start")
            await asyncio.sleep(0.01)
        os.chmod(lane.sock, 0o666)
        self.lanes[sid] = lane
        return lane

    async def close_session(self, sid: str) -> None:
        lane = self.lanes.pop(sid, None)
        if lane is not None:
            await self._stop(lane)

    async def aclose(self) -> None:
        for sid in list(self.lanes):
            await self.close_session(sid)
        if self._own_client:
            await self.client.aclose()

    @staticmethod
    async def _stop(lane: Lane) -> None:
        if lane.server is not None and lane.task is not None:
            lane.server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(lane.task), 5)
            except (asyncio.TimeoutError, Exception):
                lane.server.force_exit = True
                lane.task.cancel()
                with contextlib.suppress(BaseException):
                    await lane.task
        if lane.sock is not None:
            with contextlib.suppress(FileNotFoundError):
                lane.sock.unlink()

    # -- request handling ------------------------------------------------------
    def _endpoint(self, lane: Lane):
        async def endpoint(request: Request) -> Response:
            path = request.url.path
            if path == "/v1/models":
                if request.method not in ("GET", "HEAD"):
                    return error_response(405, "method_not_allowed", {"Allow": "GET, HEAD"})
                return await self._models(request)
            if path == "/v1/chat/completions":
                if request.method != "POST":
                    return error_response(405, "method_not_allowed", {"Allow": "POST"})
                return await self._chat(lane, request)
            return error_response(404, "not_found")
        return endpoint

    async def _models(self, request: Request) -> Response:
        if not self.cfg.ai_enabled:
            return error_response(503, "ai_unavailable")
        if "output_modalities" in request.query_params:
            # OMP's discovery asks for rerank/decision models; this demo serves chat only.
            return JSONResponse({"object": "list", "data": []})
        return JSONResponse({"object": "list", "data": await self.catalog.listing()})

    async def _read_body(self, request: Request) -> bytes | None:
        limit = self.cfg.ai_max_body_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            return None
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                return None
        return bytes(body)

    def _tag(self, lane: Lane) -> str:
        return (f"sid={lane.sid[:8]} req={lane.usage.requests}/{self.cfg.ai_max_requests} "
                f"tokens={lane.usage.tokens}/{self.cfg.ai_max_tokens}")

    def _charge(self, lane: Lane, reported: int | None, sent: int, received: int) -> None:
        # No usage block (stream cut off, odd upstream): charge a byte-length estimate
        # so a session can never consume tokens for free.
        tokens = reported if reported is not None else (sent + received + 3) // 4
        lane.usage.tokens += max(0, min(tokens, 50_000_000))
        lane.usage.save(lane.usage_path)

    async def _chat(self, lane: Lane, request: Request) -> Response:
        if not self.cfg.ai_enabled:
            return error_response(503, "ai_unavailable")
        raw = await self._read_body(request)
        if raw is None:
            return error_response(413, "request_too_large")
        try:
            payload = json.loads(raw)
        except ValueError:
            return error_response(400, "invalid_request")
        if not isinstance(payload, dict):
            return error_response(400, "invalid_request")
        model = payload.get("model")
        if not isinstance(model, str) or model not in self.cfg.ai_models:
            return error_response(403, "model_not_allowed")
        clean = sanitize_chat_payload(payload, self.cfg.ai_max_output_tokens)
        if clean is None:
            return error_response(400, "invalid_request")
        if lane.usage.requests >= self.cfg.ai_max_requests or lane.usage.tokens >= self.cfg.ai_max_tokens:
            log.info("ai quota refused %s", self._tag(lane))
            return error_response(429, "demo_quota_exhausted")
        if not self.budget.try_consume():
            log.info("ai daily budget spent, refused sid=%s", lane.sid[:8])
            return error_response(429, "demo_ai_daily_limit")
        lane.usage.requests += 1
        lane.usage.save(lane.usage_path)

        body = json.dumps(clean, separators=(",", ":")).encode("utf-8")
        streaming = clean.get("stream") is True
        # The client's own headers (Authorization included) are never forwarded: the
        # upstream request is built from scratch around the owner's key.
        headers = {
            "Authorization": f"Bearer {self.cfg.openrouter_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if streaming else "application/json",
            "Accept-Encoding": "identity",
            "HTTP-Referer": f"https://{self.cfg.public_hosts[0]}",
            "X-Title": "Chudware demo",
            "User-Agent": "chudware-demo-broker/1",
        }
        try:
            upstream = await self.client.send(self.client.build_request(
                "POST", f"{self.cfg.openrouter_base}/chat/completions", content=body,
                headers=headers), stream=True)
        except httpx.HTTPError as exc:
            log.warning("openrouter unreachable (%s) %s", type(exc).__name__, self._tag(lane))
            return error_response(502, "upstream_unavailable")

        status = upstream.status_code
        if status >= 400:
            try:
                detail = await _read_capped(upstream, MAX_ERROR_BODY)
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
            return self._upstream_error(lane, status, detail or b"", upstream.headers)
        if streaming:
            return self._stream_response(lane, upstream, len(body))
        try:
            data = await _read_capped(upstream, MAX_UPSTREAM_BODY)
        finally:
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
        if data is None:
            self._charge(lane, None, len(body), MAX_UPSTREAM_BODY)
            return error_response(502, "upstream_unavailable")
        try:
            reported = usage_total(json.loads(data))
        except ValueError:
            reported = None
        self._charge(lane, reported, len(body), len(data))
        log.info("ai ok model=%s %s", model, self._tag(lane))
        return Response(data, status_code=status,
                        media_type=upstream.headers.get("content-type", "application/json"))

    def _upstream_error(self, lane: Lane, status: int, detail: bytes, headers: httpx.Headers) -> Response:
        if status == 429:
            if _DAILY_RE.search(detail.decode("utf-8", "replace")):
                self.budget.mark_exhausted()
                log.warning("openrouter daily free allowance exhausted")
                return error_response(429, "demo_ai_daily_limit")
            return error_response(429, "demo_ai_rate_limited", {"Retry-After": "30"})
        if status in (401, 402, 403):
            log.error("openrouter refused the demo key (HTTP %s); check the key and credits", status)
            return error_response(503, "ai_unavailable")
        log.info("openrouter HTTP %s %s", status, self._tag(lane))
        return Response(detail, status_code=status,
                        media_type=headers.get("content-type", "application/json"))

    def _stream_response(self, lane: Lane, upstream: httpx.Response, sent: int) -> Response:
        async def body():
            reported: int | None = None
            received = 0
            pending = b""
            try:
                async for chunk in upstream.aiter_bytes():
                    received += len(chunk)
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    if len(pending) > 1_048_576:
                        pending = b""
                    for line in lines:
                        if line.startswith(b"data:") and b'"usage"' in line:
                            try:
                                found = usage_total(json.loads(line[5:]))
                            except ValueError:
                                found = None
                            if found is not None:
                                reported = found
                    yield chunk
            finally:
                # Charge before any await: a disconnect cancels this generator.
                self._charge(lane, reported, sent, received)
                log.info("ai stream done %s", self._tag(lane))
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()

        return StreamingResponse(body(), status_code=upstream.status_code,
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _read_capped(resp: httpx.Response, cap: int) -> bytes | None:
    data = bytearray()
    async for chunk in resp.aiter_bytes():
        data += chunk
        if len(data) > cap:
            return None
    return bytes(data)
