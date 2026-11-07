"""Reverse proxy from an admitted visitor's browser to their session container.

Only a visitor whose signed cookie names an ``active`` session is ever proxied, and only
to *that* session's published loopback port, so there is no URL a visitor can craft that
reaches another session, the host, or an arbitrary address. Host and Origin are
rewritten to the container's own loopback origin, which is exactly what the inner
gateway's TrustedHost / same-origin checks require; the session cookie, Cloudflare and
forwarding headers are removed so nothing about the visitor leaks into the container.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import unquote, urlsplit

import httpx
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketState
from websockets.asyncio.client import connect as ws_connect

from .config import DemoConfig
from .manager import DemoManager

log = logging.getLogger("chudware-demo.proxy")

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "trailers", "transfer-encoding", "upgrade",
})
# Request headers that describe the visitor or the edge, never forwarded.
REQUEST_DROP = frozenset({"host", "cookie", "origin", "forwarded", "x-real-ip", "true-client-ip",
                          "cdn-loop", "authorization"})
REQUEST_DROP_PREFIXES = ("x-forwarded-", "cf-", "sec-websocket-")
RESPONSE_DROP = frozenset({"server", "date", "set-cookie"})
EXACT_PATHS = frozenset({"/app", "/healthz", "/engine"})
PREFIXES = ("/app/", "/workspace/", "/files/", "/reveng/")
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")
_NO_BODY_METHODS = frozenset({"GET", "HEAD"})


class _TooLarge(Exception):
    pass


def same_origin(origin: str, host: str) -> bool:
    """True when ``origin`` names the host the request was sent to (gateway semantics)."""
    parsed = urlsplit(origin)
    return (parsed.scheme in {"http", "https"} and bool(parsed.netloc)
            and not parsed.path and parsed.netloc.lower() == host.lower())


def safe_target(raw_path: bytes, raw_query: bytes) -> str | None:
    """Origin-form request target if it is one the proxy is willing to forward."""
    try:
        path = raw_path.decode("ascii")
        query = raw_query.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not path.startswith("/") or path.startswith("//"):
        return None
    if path not in EXACT_PATHS and not path.startswith(PREFIXES):
        return None
    decoded = unquote(path)
    if _BAD_CHARS.search(path) or _BAD_CHARS.search(decoded) or _BAD_CHARS.search(query):
        return None
    if any(segment in (".", "..") for segment in decoded.split("/")):
        return None
    return path + ("?" + query if query else "")


class Proxy:
    def __init__(self, cfg: DemoConfig, manager: DemoManager,
                 session_of: Callable[[HTTPConnection], str | None],
                 client: httpx.AsyncClient | None = None):
        self.cfg, self.manager, self.session_of = cfg, manager, session_of
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=10.0),
            follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=16))
        self._own_client = client is None
        self._sockets: dict[str, set[Callable[[], Awaitable[None]]]] = {}

    async def aclose(self) -> None:
        if self._own_client:
            await self.client.aclose()

    # -- HTTP ------------------------------------------------------------------------
    def _request_headers(self, request: Request, port: int) -> list[tuple[str, str]]:
        out = []
        for name, value in request.headers.items():
            if name in HOP_BY_HOP or name in REQUEST_DROP or name.startswith(REQUEST_DROP_PREFIXES):
                continue
            out.append((name, value))
        if "origin" in request.headers:
            out.append(("origin", f"http://127.0.0.1:{port}"))
        return out

    async def http(self, request: Request) -> Response:
        sid = self.session_of(request)
        entry = self.manager.admitted(sid)
        if entry is None:
            if request.method in _NO_BODY_METHODS and request.url.path in ("/app", "/app/"):
                return RedirectResponse("/", status_code=307)
            return JSONResponse({"error": {"code": "no_session"}}, status_code=401,
                                headers={"Cache-Control": "no-store"})
        port = entry.port
        target = safe_target(request.scope.get("raw_path") or request.url.path.encode("utf-8"),
                             request.scope.get("query_string", b""))
        if target is None:
            return JSONResponse({"error": {"code": "bad_path"}}, status_code=400)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self.cfg.max_request_bytes:
            return Response("request body too large", status_code=413)
        if not target.startswith("/healthz"):
            self.manager.touch(sid)

        has_body = (request.method not in _NO_BODY_METHODS
                    and (declared not in ("", "0") or "transfer-encoding" in request.headers))
        content = self._body(request) if has_body else None
        upstream_request = self.client.build_request(
            request.method, f"http://127.0.0.1:{port}{target}",
            headers=self._request_headers(request, port), content=content)
        try:
            upstream = await self.client.send(upstream_request, stream=True)
        except _TooLarge:
            return Response("request body too large", status_code=413)
        except httpx.HTTPError as exc:
            log.warning("upstream error sid=%s: %s", sid[:8], type(exc).__name__)
            return JSONResponse({"error": {"code": "session_unreachable"}}, status_code=502)

        prefix = f"http://127.0.0.1:{port}"
        headers: list[tuple[bytes, bytes]] = []
        for name, value in upstream.headers.multi_items():
            lowered = name.lower()
            if lowered in HOP_BY_HOP or lowered in RESPONSE_DROP:
                continue
            if lowered == "location" and value.startswith(prefix):
                value = value[len(prefix):] or "/"
            headers.append((lowered.encode("latin-1"), value.encode("latin-1")))

        async def chunks() -> AsyncIterator[bytes]:
            last = time.monotonic()
            try:
                async for chunk in upstream.aiter_raw():
                    now = time.monotonic()
                    if now - last > 1.0:
                        self.manager.touch(sid)
                        last = now
                    yield chunk
            except httpx.HTTPError as exc:
                log.debug("upstream stream ended early sid=%s: %s", sid[:8], type(exc).__name__)
            finally:
                with contextlib.suppress(Exception):
                    await upstream.aclose()

        response = StreamingResponse(chunks(), status_code=upstream.status_code)
        response.raw_headers = headers
        return response

    async def _body(self, request: Request) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > self.cfg.max_request_bytes:
                raise _TooLarge
            yield chunk

    # -- WebSocket ---------------------------------------------------------------------
    async def websocket(self, ws: WebSocket) -> None:
        sid = self.session_of(ws)
        entry = self.manager.admitted(sid)
        if entry is None:
            await ws.close(code=4401)
            return
        origin = ws.headers.get("origin")
        if origin is not None and not same_origin(origin, ws.headers.get("host", "")):
            await ws.close(code=4403)
            return
        port = entry.port
        try:
            upstream = await ws_connect(
                f"ws://127.0.0.1:{port}/engine", origin=f"http://127.0.0.1:{port}",
                subprotocols=list(ws.scope.get("subprotocols") or []) or None,
                max_size=self.cfg.ws_max_message_bytes, compression=None, open_timeout=10,
                proxy=None)
        except Exception as exc:  # noqa: BLE001 - refuse the handshake, whatever failed
            log.warning("engine connect failed sid=%s: %s", sid[:8], type(exc).__name__)
            await ws.close(code=1011)
            return
        await ws.accept(subprotocol=upstream.subprotocol)
        self.manager.touch(sid)

        async def kill() -> None:
            with contextlib.suppress(Exception):
                await ws.close(code=4001)
            with contextlib.suppress(Exception):
                await upstream.close()

        self._sockets.setdefault(sid, set()).add(kill)

        async def to_upstream() -> None:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                self.manager.touch(sid)
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])

        async def to_client() -> None:
            async for message in upstream:
                if isinstance(message, str):
                    await ws.send_text(message)
                else:
                    await ws.send_bytes(message)

        tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._sockets.get(sid, set()).discard(kill)
            code = upstream.close_code
            with contextlib.suppress(Exception):
                await upstream.close()
            if ws.application_state != WebSocketState.DISCONNECTED:
                with contextlib.suppress(Exception):
                    await ws.close(code=code if code and 1000 <= code <= 4999 and code not in (1005, 1006, 1015)
                                   else 1000)

    async def close_session(self, sid: str) -> None:
        """End hook: drop every proxied WebSocket of a session that just ended."""
        for kill in list(self._sockets.pop(sid, ())):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(kill(), 3)
