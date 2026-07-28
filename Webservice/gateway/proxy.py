"""HTTP + WebSocket reverse proxy for /stream/<sid>/…"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Mapping
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

try:
    from .sessions import manager
except ImportError:
    from sessions import manager

log = logging.getLogger("chudware-web.proxy")

router = APIRouter(tags=["stream"])

HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
})

# Identity / forwarding headers that must never reach the per-session
# container: they carry the caller's SSO credential (Cloudflare Access JWT,
# Basic auth) and would leak to any host an SSRF attempt points at.
IDENTITY_HEADERS = frozenset({
    "authorization",
    "cookie",
    "x-real-ip",
    "x-chudware-user",  # set by us below; never trust the inbound one
})
IDENTITY_PREFIXES = ("x-forwarded-", "cf-access-")

# CR, LF, NUL and friends: rejected in path/query because they are a
# request-smuggling vector if the HTTP client ever stops rejecting them.
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | {chr(0x7F)}


def _upstream_base(host_port: int) -> str:
    return f"http://127.0.0.1:{host_port}"


def build_upstream_url(base: str, path: str, query: str = "") -> str:
    """Build an upstream URL that can only ever live under ``base``.

    Rejects absolute/scheme-relative URLs (SSRF via urljoin semantics) and
    control characters. Raises ValueError on rejection.
    """
    if any(c in _CONTROL_CHARS for c in path) or "\\" in path:
        raise ValueError("illegal characters in upstream path")
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        raise ValueError("absolute upstream URLs are not allowed")
    if query and any(c in _CONTROL_CHARS for c in query):
        raise ValueError("illegal characters in upstream query")
    # urlsplit accepts e.g. 'http:x' as scheme='http' above; a leading '//'
    # parses as netloc. What remains is a relative reference — join by
    # concatenation, never urljoin (which honours absolute replacements).
    rel = path.lstrip("/")
    url = f"{base}/{rel}" if rel else f"{base}/"
    if query:
        url = f"{url}?{query}"
    return url


def scrub_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Drop hop-by-hop and identity/forwarding headers before proxying."""
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP or lk in IDENTITY_HEADERS:
            continue
        if lk.startswith(IDENTITY_PREFIXES):
            continue
        out[k] = v
    return out


@router.api_route(
    "/stream/{sid}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
@router.api_route(
    "/stream/{sid}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy_http(sid: str, request: Request, path: str = "") -> Response:
    sess = manager.get(sid)
    if sess is None or sess.status not in {"starting", "ready"}:
        return Response("session not found", status_code=404)
    user = getattr(request.state, "user", None)
    if user is None:
        return Response("unauthorized", status_code=401)
    # Owner or admin (spectate/assist from /admin thumbnails).
    if sess.email != getattr(user, "email", None) \
            and getattr(user, "role", "") != "admin":
        return Response("forbidden", status_code=403)

    manager.touch(sid)
    base = _upstream_base(sess.host_port)
    try:
        url = build_upstream_url(base, path, request.url.query)
    except ValueError:
        return Response("invalid upstream path", status_code=400)

    headers = scrub_request_headers(request.headers)
    headers["X-Chudware-User"] = user.email

    body = await request.body()
    client = httpx.AsyncClient(timeout=None)
    try:
        upstream = await client.send(
            client.build_request(
                request.method,
                url,
                headers=headers,
                content=body,
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        log.warning("proxy HTTP error sid=%s: %s", sid, exc)
        return Response(f"upstream unavailable: {exc}", status_code=502)

    manager.touch(sid)
    resp_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
    }

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                if chunk:
                    manager.touch(sid)
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream(),
        status_code=upstream.status_code,
        headers=resp_headers,
        background=BackgroundTask(lambda: None),
    )


@router.websocket("/stream/{sid}/ws")
@router.websocket("/stream/{sid}/{path:path}")
async def proxy_ws(websocket: WebSocket, sid: str, path: str = "ws") -> None:
    sess = manager.get(sid)
    if sess is None or sess.status not in {"starting", "ready"}:
        await websocket.close(code=4404)
        return

    user = getattr(websocket.state, "user", None)
    if user is None:
        await websocket.close(code=4401)
        return
    if sess.email != getattr(user, "email", None) \
            and getattr(user, "role", "") != "admin":
        await websocket.close(code=4403)
        return

    qs = websocket.url.query
    subpath = path if path else "ws"
    try:
        upstream = build_upstream_url(
            f"ws://127.0.0.1:{sess.host_port}", subpath, qs
        )
    except ValueError:
        await websocket.close(code=4400)
        return

    await websocket.accept()
    manager.touch(sid)

    try:
        import websockets
        from websockets.exceptions import ConnectionClosed
    except ImportError:
        await websocket.close(code=1011)
        return

    try:
        async with websockets.connect(
            upstream,
            open_timeout=10,
            max_size=16 * 1024 * 1024,
            subprotocols=list(websocket.scope.get("subprotocols") or []),
        ) as upstream_ws:

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        manager.touch(sid)
                        if msg["type"] == "websocket.disconnect":
                            break
                        if "text" in msg and msg["text"] is not None:
                            await upstream_ws.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream_ws.send(msg["bytes"])
                except WebSocketDisconnect:
                    pass
                except Exception:  # noqa: BLE001
                    log.debug("client→upstream closed", exc_info=True)

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream_ws:
                        manager.touch(sid)
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except ConnectionClosed:
                    pass
                except Exception:  # noqa: BLE001
                    log.debug("upstream→client closed", exc_info=True)

            _done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception as exc:  # noqa: BLE001
        log.warning("WS proxy failed sid=%s: %s", sid, exc)
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
