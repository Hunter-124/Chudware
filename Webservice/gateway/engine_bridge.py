"""WS /engine — bridge browser clients to the shared billet-webd unix socket.

One WebSocket connection == one daemon IPC connection (newline-delimited
JSON-RPC both ways; one WS text message == one JSON line). The bridge is a
dumb pipe except for ONE rewrite: the client's `hello` gets server-asserted
identity (`params.user` from the verified gateway User) and a forced
`params.client = "gui"` — a browser can never claim to be an agent or another
person. Backpressure is natural: each side awaits the other's write.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

try:
    from .config import settings
except ImportError:  # uvicorn app:app from package dir
    from config import settings

log = logging.getLogger("billet-web.engine")

router = APIRouter()

# Engine responses can carry whole doc-JSON snapshots; the default 64 KiB
# StreamReader limit would kill the bridge mid-session.
_LINE_LIMIT = 256 * 1024 * 1024


def _inject_identity(line: str, email: str, display: str) -> str:
    """Rewrite a hello line with gateway-verified identity; pass others through."""
    try:
        msg = json.loads(line)
    except ValueError:
        return line
    if not isinstance(msg, dict) or msg.get("method") != "hello":
        return line
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    params["client"] = "gui"  # web users are humans, never agent/mcp
    params["user"] = {"id": email, "display": display}
    msg["params"] = params
    return json.dumps(msg, separators=(",", ":"))


@router.websocket("/engine")
async def engine_ws(ws: WebSocket) -> None:
    user = ws.state.user  # AuthMiddleware closed 4401/4403 already if unauthenticated
    await ws.accept()
    sock = str(settings.webd_socket)
    try:
        reader, writer = await asyncio.open_unix_connection(sock, limit=_LINE_LIMIT)
    except OSError as exc:
        log.warning("engine bridge: webd connect failed for %s: %s", user.email, exc)
        await ws.close(code=1013, reason="engine unavailable")
        return
    log.info("engine bridge open: %s -> %s", user.email, sock)

    async def ws_to_daemon() -> None:
        while True:
            line = await ws.receive_text()
            # Cheap pre-filter; _inject_identity re-checks method properly.
            if '"hello"' in line[:64]:
                line = _inject_identity(line, user.email, user.display)
            writer.write(line.encode("utf-8") + b"\n")
            await writer.drain()

    async def daemon_to_ws() -> None:
        while True:
            raw = await reader.readline()
            if not raw:  # daemon EOF (drain/restart)
                break
            await ws.send_text(raw.decode("utf-8", errors="replace").rstrip("\n"))

    tasks = (
        asyncio.create_task(ws_to_daemon(), name="engine-ws2d"),
        asyncio.create_task(daemon_to_ws(), name="engine-d2ws"),
    )
    client_gone = False
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:  # surface unexpected errors (disconnects are normal)
            exc = t.exception()
            if isinstance(exc, WebSocketDisconnect):
                client_gone = True
            elif exc and not isinstance(exc, ConnectionError):
                log.warning("engine bridge %s: %r", user.email, exc)
    except asyncio.CancelledError:
        client_gone = True  # session torn down under us — cleanup is all we owe
    finally:
        for t in tasks:
            t.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        writer.close()  # sync; skip wait_closed — nothing left to flush
        # Only close the WS when the daemon side ended it; racing a close
        # against a client-initiated disconnect trips ASGI double-close.
        if not client_gone:
            try:
                await ws.close(code=1012)  # daemon gone: client should reconnect
            except Exception:
                pass  # peer vanished mid-close
        log.info("engine bridge closed: %s", user.email)
