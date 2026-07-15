"""HTTP + WebSocket reverse proxy for /stream/<sid>/…"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

try:
    from .sessions import manager
except ImportError:
    from sessions import manager

log = logging.getLogger("billet-web.proxy")

router = APIRouter(tags=["stream"])

HOP_BY_HOP = {
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
}


def _upstream_base(host_port: int) -> str:
    return f"http://127.0.0.1:{host_port}"


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
    if user is not None and sess.email != getattr(user, "email", None):
        return Response("forbidden", status_code=403)

    manager.touch(sid)
    base = _upstream_base(sess.host_port)
    if not path:
        url = base + "/"
    else:
        url = urljoin(base + "/", path)
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() != "cookie"
    }
    if user is not None:
        headers["X-Billet-User"] = user.email

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
    if user is not None and sess.email != getattr(user, "email", None):
        await websocket.close(code=4403)
        return

    await websocket.accept()
    manager.touch(sid)

    qs = websocket.url.query
    subpath = path if path else "ws"
    upstream = f"ws://127.0.0.1:{sess.host_port}/{subpath}"
    if qs:
        upstream = f"{upstream}?{qs}"

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
