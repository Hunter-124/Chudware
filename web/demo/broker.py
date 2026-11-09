"""AI broker (free models and capped Claude), one unix socket per session.

The owner's keys live only in this (host) process. A session container sees nothing but
``/run/chudware-demo/provider.sock``; the socket identity *is* the session, so there is
no credential to steal inside the container. Wire contract:

    POST /v1/chat/completions        OpenAI-compatible; free models, streaming allowed. With an
                                     NVIDIA key the models NVIDIA serves go there under NVIDIA's
                                     id; the rest go to OpenRouter.
    POST /v1/messages                Claude Messages API; allowlisted Claude 5.5 models only
    POST /v1/messages/count_tokens   Claude token counting (free upstream, not charged)
    GET  /v1/models                  Claude models first, then the free models (``served_by``
                                     names their upstream)

Free models carry no demo quota; Claude carries USD caps. Refusals are JSON
``{"error":{"code":...,"message":...}}`` on the OpenAI routes and
``{"type":"error","error":{"type":...,"message":...}}`` on the Claude routes:
    403 model_not_allowed        model outside the allowlist of that route
    429 demo_quota_exhausted     Claude: this session or this client today used its allowance
    429 demo_ai_daily_limit      Claude's daily or monthly USD cap is spent, or OpenRouter's own
                                 daily free-model allowance for the owner's account is
    429 demo_ai_rate_limited     the provider is rate limiting right now (retry shortly)
    503 ai_unavailable           no key configured, or the provider refused the key
Prompts, completions and the keys are never logged; only per-session counters are.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
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

from .config import CLAUDE_MODELS, NVIDIA_MODEL_NAMES, NVIDIA_MODELS, DemoConfig

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
    "model_not_allowed": "This demo only serves its listed AI models.",
    "demo_ai_daily_limit": ("OpenRouter's free allowance for today is used up; pick another model "
                            "or try again tomorrow. Modeling still works."),
    "demo_ai_rate_limited": "The free AI model is busy right now; try again in a minute.",
    "ai_unavailable": "AI is not available in this demo right now; modeling still works.",
    "upstream_unavailable": "The AI provider could not be reached; try again shortly.",
    "invalid_request": "The request is not a valid chat completion request.",
    "request_too_large": "The request body is too large.",
    "not_found": "Not found.",
    "method_not_allowed": "Method not allowed.",
}
CLAUDE_MESSAGES = {
    "session": "This demo session's Claude allowance is used up; pick a free model to keep going.",
    "client": "Your Claude allowance for today is used up; pick a free model to keep going.",
    "day": "The demo's Claude budget for today is used up; pick a free model to keep going.",
    "month": "The demo's Claude budget for this month is used up; pick a free model to keep going.",
    "rate": "Claude is busy right now; try again in a minute.",
    "invalid_request": "The request is not a valid Claude Messages request.",
}

# The only Claude request fields forwarded: everything OMP's Anthropic transport sends for
# a plain turn. Anything else (server compaction, fallbacks, MCP servers, containers,
# priority tier, fast mode, inference geo, ...) can add cost or reach a model outside the
# allowlist, so it is dropped. Server tools (a ``type`` other than ``custom``) are dropped too.
CLAUDE_FIELDS = frozenset({"model", "messages", "system", "max_tokens", "stop_sequences", "stream",
                           "temperature", "top_p", "top_k", "tools", "tool_choice", "thinking", "metadata",
                           "output_config", "cache_control", "context_management"})
# Beta families OMP sends that do not enable paid features; matched without their date.
CLAUDE_BETAS = frozenset({
    "interleaved-thinking", "fine-grained-tool-streaming", "effort", "context-management",
    "structured-outputs", "extended-cache-ttl", "mid-conversation-system", "mid-conversation-output-config",
    "mid-conversation-tool-changes", "mid-conversation-system-clear-at", "prompt-caching-scope",
    "thinking-binding-controls", "thinking-token-count", "task-budgets"})
_BETA_RE = re.compile(r"([a-z][a-z-]{0,62}[a-z])-\d{4}-\d{2}-\d{2}\Z")
COUNT_FIELDS = ("model", "messages", "system", "tools", "tool_choice", "thinking")
_VERSION_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")

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


def claude_error(status: int, code: str, message: str | None = None,
                 headers: dict[str, str] | None = None) -> JSONResponse:
    """A refusal in the Claude Messages API's own error shape."""
    out = dict(headers or {})
    if code in NO_RETRY:
        out["x-should-retry"] = "false"
    return JSONResponse({"type": "error", "error": {"type": code, "message": message or MESSAGES.get(code, code)}},
                        status_code=status, headers=out or None)


@dataclass
class Usage:
    """One session's AI spend: forwarded free-model requests, their tokens, and Claude USD."""

    requests: int = 0
    tokens: int = 0
    claude_micros: int = 0

    @classmethod
    def load(cls, path: Path) -> "Usage":
        try:
            raw = json.loads(path.read_text("utf-8"))
            return cls(max(0, int(raw["requests"])), max(0, int(raw["tokens"])),
                       max(0, int(raw.get("claude_micros", 0))))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path | None) -> None:
        if path is not None:
            _atomic_json(path, {"requests": self.requests, "tokens": self.tokens,
                                "claude_micros": self.claude_micros})


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        log.warning("could not persist %s", path.name, exc_info=True)


class UpstreamAllowance:
    """OpenRouter's own daily free-model allowance for the owner's account: no count is kept
    here, only whether OpenRouter answered that it is spent (until the next UTC day), so the
    rest of the day is refused locally instead of being re-sent upstream."""

    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time):
        self.path, self.clock = path, clock
        self.day, self.exhausted = self._today(), False
        if path is not None:
            try:
                raw = json.loads(path.read_text("utf-8"))
                if int(raw["day"]) == self.day:
                    self.exhausted = bool(raw["exhausted"])
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def _today(self) -> int:
        return int(self.clock() // 86400)

    def available(self) -> bool:
        today = self._today()
        if today != self.day:
            self.day, self.exhausted = today, False
        return not self.exhausted

    def mark_exhausted(self) -> None:
        self.available()
        self.exhausted = True
        if self.path is not None:
            _atomic_json(self.path, {"day": self.day, "exhausted": True})


@dataclass(frozen=True)
class FreeRoute:
    """Where one listed free model id is sent, and under which upstream id."""

    upstream: str        # "openrouter" | "nvidia"
    model: str           # the id the upstream knows
    source: str          # the configured (OpenRouter) id, for its catalogue metadata


def free_routes(cfg: DemoConfig) -> dict[str, FreeRoute]:
    """Listed id -> route, in listing order. A model NVIDIA serves goes there (under NVIDIA's id)
    when an NVIDIA key is set; the rest go to OpenRouter when its key is set; then come the
    NVIDIA-only models. Anything else is not served."""
    routes: dict[str, FreeRoute] = {}
    for model in cfg.ai_models:
        nvidia = NVIDIA_MODELS.get(model)
        if cfg.nvidia_key and nvidia:
            routes[nvidia] = FreeRoute("nvidia", nvidia, model)
        elif cfg.openrouter_key:
            routes[model] = FreeRoute("openrouter", model, model)
    if cfg.nvidia_key:
        for model in cfg.nvidia_models:
            routes.setdefault(model, FreeRoute("nvidia", model, model))
    return routes


MICROS = 1_000_000  # micro-USD per USD; tokens x (USD per million tokens) is micro-USD exactly


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def merge_claude_usage(into: dict[str, Any], usage: Any) -> None:
    """Fold one ``usage`` object (message_start / message_delta / a full response) into ``into``.

    A response with ``iterations`` (server-side work such as compaction) bills their sum, which
    its top-level counts leave out; the larger of the two is kept for every counter.
    """
    if not isinstance(usage, dict):
        return
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    for key in keys:
        if key in usage:
            into[key] = max(into.get(key, 0), _count(usage[key]))
    detail = usage.get("cache_creation")
    if isinstance(detail, dict) and "ephemeral_1h_input_tokens" in detail:
        into["cache_1h"] = max(into.get("cache_1h", 0), _count(detail["ephemeral_1h_input_tokens"]))
    iterations = usage.get("iterations")
    if isinstance(iterations, list):
        steps = [step for step in iterations if isinstance(step, dict)]
        for key in keys:
            into[key] = max(into.get(key, 0), sum(_count(step.get(key)) for step in steps))
        written_1h = sum(_count(step["cache_creation"].get("ephemeral_1h_input_tokens"))
                         for step in steps if isinstance(step.get("cache_creation"), dict))
        into["cache_1h"] = max(into.get("cache_1h", 0), written_1h)


_INLINE_SOURCES = frozenset({"base64", "text", "content"})


def _has_remote_media(blocks: Any, depth: int = 0) -> bool:
    """True when an image or document block (at any nesting) is fetched by the upstream from a
    URL or file id: it could be small when counted and huge when billed, so only inline
    sources are accepted."""
    if depth > 8:
        return True
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("image", "document"):
            source = block.get("source")
            if not isinstance(source, dict) or source.get("type") not in _INLINE_SOURCES:
                return True
            if _has_remote_media(source.get("content"), depth + 1):
                return True
        if _has_remote_media(block.get("content"), depth + 1):
            return True
    return False


def claude_cost_micros(model: str, usage: dict[str, Any]) -> int:
    """What one response cost, in micro-USD, from its merged usage."""
    spec = CLAUDE_MODELS[model]
    fresh = usage.get("input_tokens", 0)
    read = usage.get("cache_read_input_tokens", 0)
    written = usage.get("cache_creation_input_tokens", 0)
    written_1h = min(usage.get("cache_1h", 0), written)
    price = spec.price
    if spec.tier_tokens is not None and spec.tier_price is not None and fresh + read + written <= spec.tier_tokens:
        price = spec.tier_price
    return math.ceil(fresh * price.input + (written - written_1h) * price.cache_write_5m
                     + written_1h * price.cache_write_1h + read * price.cache_read
                     + usage.get("output_tokens", 0) * price.output)


def claude_estimate_micros(model: str, input_tokens: int, max_tokens: int) -> int:
    """An upper bound for one request: every counted input token at the dearest input rate
    (a 1 h cache write), the full ``max_tokens`` as output, at the model's base card."""
    price = CLAUDE_MODELS[model].price
    dearest = max(price.input, price.cache_write_5m, price.cache_write_1h)
    return math.ceil(input_tokens * dearest + max_tokens * price.output)


class ClaudeLedger:
    """Claude spend in micro-USD shared by all sessions: per client and in total per UTC day,
    in total per UTC calendar month. Persisted, so a restart cannot reset a cap."""

    def __init__(self, cfg: DemoConfig, path: Path | None = None, clock: Callable[[], float] = time.time):
        self.cfg, self.path, self.clock = cfg, path, clock
        self.day, self.month = self._today(), self._this_month()
        self.day_micros, self.month_micros = 0, 0
        self.clients: dict[str, int] = {}
        if path is not None:
            try:
                raw = json.loads(path.read_text("utf-8"))
                if raw["month"] == self.month:
                    self.month_micros = max(0, int(raw["month_micros"]))
                if int(raw["day"]) == self.day:
                    self.day_micros = max(0, int(raw["day_micros"]))
                    self.clients = {str(k): max(0, int(v)) for k, v in dict(raw["clients"]).items()}
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def _today(self) -> int:
        return int(self.clock() // 86400)

    def _this_month(self) -> str:
        return time.strftime("%Y-%m", time.gmtime(self.clock()))

    def _roll(self) -> None:
        month, today = self._this_month(), self._today()
        if month != self.month:
            self.month, self.month_micros = month, 0
        if today != self.day:
            self.day, self.day_micros, self.clients = today, 0, {}

    def _save(self) -> None:
        if self.path is not None:
            _atomic_json(self.path, {"day": self.day, "month": self.month, "day_micros": self.day_micros,
                                     "month_micros": self.month_micros, "clients": self.clients})

    def refusal(self, client: str, session_micros: int, cost: int) -> str | None:
        """Which cap a request costing up to ``cost`` would cross (``session``/``client``/``day``/
        ``month``), if any."""
        self._roll()
        cfg = self.cfg
        if self.month_micros + cost > cfg.claude_monthly_usd * MICROS:
            return "month"
        if self.day_micros + cost > cfg.claude_daily_usd * MICROS:
            return "day"
        if self.clients.get(client, 0) + cost > cfg.claude_client_daily_usd * MICROS:
            return "client"
        if session_micros + cost > cfg.claude_session_usd * MICROS:
            return "session"
        return None

    def available(self) -> bool:
        self._roll()
        return (self.month_micros < self.cfg.claude_monthly_usd * MICROS
                and self.day_micros < self.cfg.claude_daily_usd * MICROS)

    def period(self) -> tuple[int, str]:
        self._roll()
        return self.day, self.month

    def charge(self, client: str, micros: int, period: tuple[int, str] | None = None) -> None:
        """Add ``micros`` to every running total. A negative correction (an over-estimate given
        back) applies only to the day / month it was reserved in, never to a newer one."""
        self._roll()
        same_day = period is None or period[0] == self.day
        same_month = period is None or period[1] == self.month
        if micros >= 0 or same_day:
            self.day_micros = max(0, self.day_micros + micros)
            self.clients[client] = max(0, self.clients.get(client, 0) + micros)
        if micros >= 0 or same_month:
            self.month_micros = max(0, self.month_micros + micros)
        self._save()


def sanitize_messages_payload(payload: dict[str, Any], max_output: int) -> dict[str, Any] | None:
    """Forwardable copy of a Claude Messages ``payload``, or None when it is malformed."""
    if not isinstance(payload.get("messages"), list):
        return None
    if "stream" in payload and not isinstance(payload["stream"], bool):
        return None
    if any(isinstance(m, dict) and _has_remote_media(m.get("content")) for m in payload["messages"]) \
            or _has_remote_media(payload.get("system")):
        return None
    clean = {k: v for k, v in payload.items() if k in CLAUDE_FIELDS}
    management = clean.get("context_management")
    if management is not None:
        # Client-side clearing only; ``compact_*`` edits run billed server-side compaction.
        edits = management.get("edits") if isinstance(management, dict) else None
        kept = [e for e in edits if isinstance(e, dict) and str(e.get("type", "")).startswith("clear_")] \
            if isinstance(edits, list) else []
        if kept:
            clean["context_management"] = {**management, "edits": kept}
        else:
            del clean["context_management"]
    if "tools" in clean:
        if not isinstance(clean["tools"], list):
            return None
        # Client tools only: server tools (web search, code execution, ...) bill per use.
        tools = [t for t in clean["tools"] if isinstance(t, dict) and t.get("type", "custom") == "custom"]
        if tools:
            clean["tools"] = tools
        else:
            del clean["tools"]
    value = clean.get("max_tokens")
    if value is None:
        clean["max_tokens"] = max_output
    elif isinstance(value, bool) or not isinstance(value, int):
        return None
    else:
        clean["max_tokens"] = min(value, max_output) if value >= 1 else max_output
    thinking = clean.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        # The 5.5 models accept only adaptive thinking (effort rides in output_config); a
        # client that knows them only as generic reasoning models still sends a token budget.
        clean["thinking"] = {"type": "adaptive"}
    return clean


def claude_betas(raw: str | None) -> str | None:
    """The client's ``anthropic-beta`` flags that are on the free-feature allowlist."""
    if not raw:
        return None
    kept = []
    for part in raw.split(","):
        match = _BETA_RE.fullmatch(part.strip())
        if match and match.group(1) in CLAUDE_BETAS:
            kept.append(part.strip())
    return ",".join(kept[:16]) or None


class ModelCatalog:
    """The free routes, with OpenRouter's catalogue metadata (cached). An OpenRouter-served
    model OpenRouter no longer lists is dropped; an NVIDIA-served one is kept."""

    def __init__(self, cfg: DemoConfig, client: httpx.AsyncClient, clock: Callable[[], float] = time.time):
        self.cfg, self.client, self.clock = cfg, client, clock
        self.routes = free_routes(cfg)
        self._items: list[dict[str, Any]] | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _entry(model: str, route: FreeRoute, live: dict[str, Any] | None) -> dict[str, Any]:
        """An OpenAI-style model entry, carrying OpenRouter's own metadata when known."""
        live = live or {}
        item: dict[str, Any] = {"id": model, "object": "model",
                                "created": int(live.get("created") or 0),
                                "owned_by": model.split("/", 1)[0], "served_by": route.upstream}

        def number(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool)

        def strings(value: Any) -> bool:
            return isinstance(value, list) and all(isinstance(v, str) for v in value)

        if isinstance(live.get("name"), str):
            # NVIDIA serves the full model, not OpenRouter's ":free" listing of it.
            item["name"] = live["name"].removesuffix(" (free)") if route.upstream == "nvidia" else live["name"]
        elif model in NVIDIA_MODEL_NAMES:
            item["name"] = NVIDIA_MODEL_NAMES[model]
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
                    self._items = [self._entry(m, r, None) for m, r in self.routes.items()]
                return self._items
            self._items = [self._entry(m, r, live.get(r.source)) for m, r in self.routes.items()
                           if r.upstream == "nvidia" or r.source in live]
            self._expires = now + self.cfg.ai_models_cache_seconds
            retired = [r.source for r in self.routes.values()
                       if r.upstream == "openrouter" and r.source not in live]
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
    # The manager's anonymised client-network tag; Claude's per-client daily cap keys on it.
    client: str = ""
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
                 budget_path: Path | None = None, claude_path: Path | None = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8))
        self._own_client = client is None
        self.allowance = UpstreamAllowance(budget_path, clock)
        self.catalog = ModelCatalog(cfg, self.client, clock)
        self.claude = ClaudeLedger(cfg, claude_path, clock)
        self.lanes: dict[str, Lane] = {}

    # -- public --------------------------------------------------------------
    def free_ai_available(self) -> bool:
        """Some free model is routable right now (OpenRouter's daily allowance aside, NVIDIA)."""
        routes = self.catalog.routes.values()
        return any(r.upstream == "nvidia" for r in routes) or (
            any(r.upstream == "openrouter" for r in routes) and self.allowance.available())

    def claude_available(self) -> bool:
        """A Claude key is configured and neither the daily nor the monthly budget is spent."""
        return self.cfg.claude_enabled and self.claude.available()

    def make_lane(self, sid: str, usage: Usage, usage_path: Path | None = None, client: str = "") -> Lane:
        lane = Lane(sid, usage, usage_path, app=None, client=client)  # type: ignore[arg-type]
        lane.app = Starlette(routes=[Route(
            "/{path:path}", self._endpoint(lane),
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])])
        return lane

    async def open_session(self, sid: str, run_dir: Path, usage: Usage,
                           usage_path: Path | None = None, client: str = "") -> Lane:
        """Create ``run_dir/provider.sock`` (mode 0666 in a 0711 directory)."""
        await self.close_session(sid)
        run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(run_dir, 0o711)
        lane = self.make_lane(sid, usage, usage_path, client)
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
            if path in ("/v1/messages", "/v1/messages/count_tokens"):
                if request.method != "POST":
                    return claude_error(405, "method_not_allowed", headers={"Allow": "POST"})
                return await self._messages(lane, request, count_only=path.endswith("count_tokens"))
            return error_response(404, "not_found")
        return endpoint

    async def _models(self, request: Request) -> Response:
        if not (self.cfg.ai_enabled or self.cfg.claude_enabled):
            return error_response(503, "ai_unavailable")
        if "output_modalities" in request.query_params:
            # OMP's discovery asks for rerank/decision models; this demo serves chat only.
            return JSONResponse({"object": "list", "data": []})
        data = [self._claude_entry(m) for m in self.cfg.claude_models] if self.cfg.claude_enabled else []
        if self.cfg.ai_enabled:
            data += await self.catalog.listing()
        return JSONResponse({"object": "list", "data": data})

    def _claude_entry(self, model: str) -> dict[str, Any]:
        spec = CLAUDE_MODELS[model]
        price = spec.tier_price or spec.price   # the card most requests are billed at

        def per_token(usd_per_million: float) -> str:
            return f"{usd_per_million / MICROS:.10f}".rstrip("0")

        return {"id": model, "object": "model", "created": 0, "owned_by": "anthropic", "name": spec.name,
                "context_length": spec.context,
                "top_provider": {"context_length": spec.context,
                                 "max_completion_tokens": self.cfg.claude_max_output_tokens},
                "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                "supported_parameters": ["tools", "tool_choice", "max_tokens", "reasoning"],
                "pricing": {"prompt": per_token(price.input), "completion": per_token(price.output),
                            "input_cache_read": per_token(price.cache_read),
                            "input_cache_write": per_token(price.cache_write_5m)}}

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
        return f"sid={lane.sid[:8]} req={lane.usage.requests} tokens={lane.usage.tokens}"

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
        route = self.catalog.routes.get(model) if isinstance(model, str) else None
        if route is None:
            return error_response(403, "model_not_allowed")
        clean = sanitize_chat_payload(payload, self.cfg.ai_max_output_tokens)
        if clean is None:
            return error_response(400, "invalid_request")
        nvidia = route.upstream == "nvidia"
        if not nvidia and not self.allowance.available():
            log.info("openrouter daily allowance spent, refused sid=%s", lane.sid[:8])
            return error_response(429, "demo_ai_daily_limit")
        clean["model"] = route.model
        lane.usage.requests += 1
        lane.usage.save(lane.usage_path)

        body = json.dumps(clean, separators=(",", ":")).encode("utf-8")
        streaming = clean.get("stream") is True
        # The client's own headers (Authorization included) are never forwarded: the
        # upstream request is built from scratch around the owner's key.
        headers = {
            "Authorization": f"Bearer {self.cfg.nvidia_key if nvidia else self.cfg.openrouter_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if streaming else "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "chudware-demo-broker/1",
        }
        if not nvidia:
            headers["HTTP-Referer"] = f"https://{self.cfg.public_hosts[0]}"
            headers["X-Title"] = "Chudware demo"
        base = self.cfg.nvidia_base if nvidia else self.cfg.openrouter_base
        try:
            upstream = await self.client.send(self.client.build_request(
                "POST", f"{base}/chat/completions", content=body,
                headers=headers), stream=True)
        except httpx.HTTPError as exc:
            log.warning("%s unreachable (%s) %s", route.upstream, type(exc).__name__, self._tag(lane))
            return error_response(502, "upstream_unavailable")

        status = upstream.status_code
        if status >= 400:
            try:
                detail = await _read_capped(upstream, MAX_ERROR_BODY)
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
            return self._upstream_error(lane, route, status, detail or b"", upstream.headers)
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
        log.info("ai ok %s model=%s %s", route.upstream, route.model, self._tag(lane))
        return Response(data, status_code=status,
                        media_type=upstream.headers.get("content-type", "application/json"))

    def _upstream_error(self, lane: Lane, route: FreeRoute, status: int, detail: bytes,
                        headers: httpx.Headers) -> Response:
        if status == 429:
            # Only OpenRouter has a daily allowance; NVIDIA's free API limits per minute.
            if route.upstream == "openrouter" and _DAILY_RE.search(detail.decode("utf-8", "replace")):
                self.allowance.mark_exhausted()
                log.warning("openrouter daily free allowance exhausted")
                return error_response(429, "demo_ai_daily_limit")
            log.info("%s rate limited %s", route.upstream, self._tag(lane))
            return error_response(429, "demo_ai_rate_limited", {"Retry-After": "30"})
        if status in (401, 402, 403):
            log.error("%s refused the demo key (HTTP %s); check the key", route.upstream, status)
            return error_response(503, "ai_unavailable")
        log.info("%s HTTP %s model=%s %s", route.upstream, status, route.model, self._tag(lane))
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

    # -- Claude ------------------------------------------------------------------
    def _claude_tag(self, lane: Lane) -> str:
        cfg, ledger = self.cfg, self.claude
        return (f"sid={lane.sid[:8]} session=${lane.usage.claude_micros / MICROS:.4f}/{cfg.claude_session_usd:g} "
                f"day=${ledger.day_micros / MICROS:.4f}/{cfg.claude_daily_usd:g} "
                f"month=${ledger.month_micros / MICROS:.2f}/{cfg.claude_monthly_usd:g}")

    def _claude_charge(self, lane: Lane, micros: int, period: tuple[int, str] | None = None) -> None:
        lane.usage.claude_micros = max(0, lane.usage.claude_micros + micros)
        lane.usage.save(lane.usage_path)
        self.claude.charge(lane.client, micros, period)

    def _claude_settle(self, lane: Lane, model: str, usage: dict[str, Any] | None,
                       hold: tuple[int, tuple[int, str]]) -> None:
        """Replace the advance charge with the reported cost; without a final usage keep it."""
        if usage is not None:
            self._claude_charge(lane, claude_cost_micros(model, usage) - hold[0], hold[1])

    def _claude_headers(self, request: Request, streaming: bool) -> dict[str, str]:
        # Built from scratch around the owner's key: no client header is forwarded
        # except the API version and the allowlisted beta flags.
        version = request.headers.get("anthropic-version", "")
        headers = {
            "x-api-key": self.cfg.anthropic_key,
            "anthropic-version": version if _VERSION_RE.fullmatch(version) else "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if streaming else "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "chudware-demo-broker/1",
        }
        betas = claude_betas(request.headers.get("anthropic-beta"))
        if betas:
            headers["anthropic-beta"] = betas
        return headers

    async def _claude_post(self, path: str, body: bytes, headers: dict[str, str]) -> httpx.Response:
        return await self.client.send(self.client.build_request(
            "POST", f"{self.cfg.anthropic_base}{path}", content=body, headers=headers), stream=True)

    async def _messages(self, lane: Lane, request: Request, *, count_only: bool) -> Response:
        if not self.cfg.claude_enabled:
            return claude_error(503, "ai_unavailable")
        raw = await self._read_body(request)
        if raw is None:
            return claude_error(413, "request_too_large")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return claude_error(400, "invalid_request", CLAUDE_MESSAGES["invalid_request"])
        model = payload.get("model")
        if not isinstance(model, str) or model not in self.cfg.claude_models:
            return claude_error(403, "model_not_allowed")
        clean = sanitize_messages_payload(payload, self.cfg.claude_max_output_tokens)
        if clean is None:
            return claude_error(400, "invalid_request", CLAUDE_MESSAGES["invalid_request"])
        # Token counting is free upstream and only takes the prompt-shaping fields.
        counted = json.dumps({k: clean[k] for k in COUNT_FIELDS if k in clean},
                             separators=(",", ":")).encode("utf-8")
        try:
            probe = await self._claude_post("/v1/messages/count_tokens", counted,
                                            self._claude_headers(request, False))
            try:
                detail = await _read_capped(probe, MAX_ERROR_BODY)
            finally:
                with anyio.CancelScope(shield=True):
                    await probe.aclose()
        except httpx.HTTPError as exc:
            log.warning("claude unreachable (%s) %s", type(exc).__name__, self._claude_tag(lane))
            return claude_error(502, "upstream_unavailable")
        if probe.status_code >= 400:
            return self._claude_upstream_error(lane, probe.status_code, detail or b"", probe.headers)
        if count_only:
            return Response(detail or b"", status_code=probe.status_code,
                            media_type=probe.headers.get("content-type", "application/json"))
        try:
            input_tokens = _count(json.loads(detail or b"").get("input_tokens"))
        except (ValueError, AttributeError):
            return claude_error(502, "upstream_unavailable")

        # An upper bound of this request's cost is checked against every cap and charged up
        # front, so concurrent requests of one visitor see each other.
        reservation = claude_estimate_micros(model, input_tokens, clean["max_tokens"])
        cap = self.claude.refusal(lane.client, lane.usage.claude_micros, reservation)
        if cap is not None:
            log.info("claude %s cap refused %s", cap, self._claude_tag(lane))
            code = "demo_quota_exhausted" if cap in ("session", "client") else "demo_ai_daily_limit"
            return claude_error(429, code, CLAUDE_MESSAGES[cap])
        hold = (reservation, self.claude.period())
        self._claude_charge(lane, reservation, hold[1])

        body = json.dumps(clean, separators=(",", ":")).encode("utf-8")
        streaming = clean.get("stream") is True
        try:
            upstream = await self._claude_post("/v1/messages", body, self._claude_headers(request, streaming))
        except httpx.HTTPError as exc:
            # Only a request that provably never left keeps nothing; after a read timeout the
            # upstream may still be generating (and billing), so the advance charge stands.
            if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
                self._claude_charge(lane, -reservation, hold[1])
            log.warning("claude unreachable (%s) %s", type(exc).__name__, self._claude_tag(lane))
            return claude_error(502, "upstream_unavailable")
        status = upstream.status_code
        if status >= 400:
            try:
                detail = await _read_capped(upstream, MAX_ERROR_BODY)
            finally:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
            self._claude_charge(lane, -reservation, hold[1])          # a refused request is not billed
            return self._claude_upstream_error(lane, status, detail or b"", upstream.headers)
        if streaming:
            return self._claude_stream(lane, model, upstream, hold)
        try:
            data = await _read_capped(upstream, MAX_UPSTREAM_BODY)
        finally:
            with anyio.CancelScope(shield=True):
                await upstream.aclose()
        if data is None:
            return claude_error(502, "upstream_unavailable")   # the advance charge stands
        usage: dict[str, Any] = {}
        try:
            parsed = json.loads(data)
            merge_claude_usage(usage, parsed.get("usage") if isinstance(parsed, dict) else None)
        except ValueError:
            pass
        self._claude_settle(lane, model, usage or None, hold)
        log.info("claude ok model=%s %s", model, self._claude_tag(lane))
        return Response(data, status_code=status,
                        media_type=upstream.headers.get("content-type", "application/json"))

    def _claude_upstream_error(self, lane: Lane, status: int, detail: bytes, headers: httpx.Headers) -> Response:
        if status == 429:
            retry = headers.get("retry-after", "")
            return claude_error(429, "demo_ai_rate_limited", CLAUDE_MESSAGES["rate"],
                                {"Retry-After": retry if retry.isdigit() else "30"})
        if status in (401, 403) or (status == 400 and b"credit balance" in detail.lower()):
            log.error("claude refused the demo key (HTTP %s); check the key, workspace and credits", status)
            return claude_error(503, "ai_unavailable")
        log.info("claude HTTP %s %s", status, self._claude_tag(lane))
        return Response(detail, status_code=status, media_type=headers.get("content-type", "application/json"))

    def _claude_stream(self, lane: Lane, model: str, upstream: httpx.Response,
                       hold: tuple[int, tuple[int, str]]) -> Response:
        async def body():
            usage: dict[str, Any] = {}
            finished = False   # message_delta carries the final output count
            pending = b""
            try:
                async for chunk in upstream.aiter_bytes():
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    if len(pending) > 1_048_576:
                        pending = b""
                    for line in lines:
                        if not (line.startswith(b"data:") and b'"usage"' in line):
                            continue
                        try:
                            event = json.loads(line[5:])
                        except ValueError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        merge_claude_usage(usage, event.get("usage"))
                        message = event.get("message")
                        if isinstance(message, dict):
                            merge_claude_usage(usage, message.get("usage"))
                        finished = finished or event.get("type") == "message_delta"
                    yield chunk
            finally:
                # Settle before any await: a disconnect cancels this generator. A stream cut
                # before its final usage keeps the pessimistic advance charge.
                self._claude_settle(lane, model, usage if finished else None, hold)
                log.info("claude stream done model=%s %s", model, self._claude_tag(lane))
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
