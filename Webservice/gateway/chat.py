"""Portal shoutbox: WebSocket fan-out + Postgres history + presence.

Exports:
  - router: APIRouter with WS /ws/chat and GET /api/chat/history
  - Presence: async join/leave/list used by chat + /healthz
  - presence: module singleton
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

try:
    from .auth import AuthError, revalidate_websocket_user
except ImportError:
    from auth import AuthError, revalidate_websocket_user  # type: ignore

try:
    from .db import tx
    from .limits import chat_messages
except ImportError:  # allow `python -c 'import chat'` from package dir
    from db import tx  # type: ignore
    from limits import chat_messages  # type: ignore

log = logging.getLogger("chudware.gateway.chat")

router = APIRouter()


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


@dataclass
class Presence:
    """Live portal chat presence (email → display + refcount of WS connections)."""

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _users: dict[str, str] = field(default_factory=dict)  # email -> display
    _refs: dict[str, int] = field(default_factory=dict)  # email -> open WS count

    async def join(self, email: str, display: str) -> bool:
        """Register a connection. Returns True if this is a new presence entry."""
        email = (email or "").strip().lower()
        display = (display or email or "user").strip() or "user"
        if not email:
            return False
        async with self._lock:
            first = email not in self._users
            self._users[email] = display
            self._refs[email] = self._refs.get(email, 0) + 1
            return first

    async def leave(self, email: str) -> bool:
        """Drop one connection. Returns True if the user fully left presence."""
        email = (email or "").strip().lower()
        if not email:
            return False
        async with self._lock:
            n = self._refs.get(email, 0) - 1
            if n <= 0:
                self._refs.pop(email, None)
                self._users.pop(email, None)
                return True
            self._refs[email] = n
            return False

    async def list(self) -> list[dict[str, str]]:
        async with self._lock:
            return [
                {"email": e, "display": d}
                for e, d in sorted(self._users.items(), key=lambda kv: kv[1].lower())
            ]


presence = Presence()


# ---------------------------------------------------------------------------
# History (PostgreSQL — schema lives in migrations/0001_init.sql)
# ---------------------------------------------------------------------------

# These two stay `async def`: they are awaited from the WebSocket hot path and
# from a request handler, and psycopg's pool is synchronous. Running the query
# in a worker thread keeps a slow store from stalling every other socket in the
# process — the same property aiosqlite gave us before the port.


def _message_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "id": row["id"],
        "user": row["email"],
        "display": row["display"],
        "text": row["text"],
        "ts": row["ts"],
    }


def _insert_message(ts: float, email: str, display: str, text: str) -> int:
    with tx() as cur:
        cur.execute(
            "INSERT INTO messages (ts, email, display, text)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (ts, email, display, text),
        )
        return int(cur.fetchone()["id"])


def _select_history(limit: int) -> list[dict[str, Any]]:
    with tx() as cur:
        cur.execute(
            """
            SELECT id, ts, email, display, text
            FROM messages
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [_message_row(r) for r in reversed(rows)]


async def save_message(email: str, display: str, text: str, ts: float | None = None) -> dict[str, Any]:
    ts = float(ts if ts is not None else time.time())
    email = (email or "").strip().lower()
    display = (display or email or "user").strip() or "user"
    text = (text or "").strip()
    row_id = await asyncio.to_thread(_insert_message, ts, email, display, text)
    return {
        "type": "message",
        "id": row_id,
        "user": email,
        "display": display,
        "text": text,
        "ts": ts,
    }


async def load_history(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    return await asyncio.to_thread(_select_history, limit)


# ---------------------------------------------------------------------------
# WebSocket hub
# ---------------------------------------------------------------------------


class Hub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._clients: set[WebSocket] = set()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any], *, skip: WebSocket | None = None) -> None:
        data = json.dumps(payload, separators=(",", ":"))
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocket] = []
        for ws in targets:
            if skip is not None and ws is skip:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove(ws)


hub = Hub()


def _user_from_ws(ws: WebSocket) -> tuple[str, str]:
    """Pull email/display from middleware-populated scope state."""
    user = getattr(ws.state, "user", None)
    if user is not None:
        email = getattr(user, "email", None) or getattr(ws.state, "user_email", "") or ""
        display = getattr(user, "display", None) or email or "user"
        return str(email).strip().lower(), str(display).strip() or "user"
    email = getattr(ws.state, "user_email", "") or ""
    display = getattr(ws.state, "user_display", "") or email or "user"
    return str(email).strip().lower(), str(display).strip() or "user"


_MAX_TEXT = 2000


def _clean_text(text: str) -> str:
    """Strip control characters and cap length BEFORE persisting.

    The SQLite store is the durable artifact — today's frontend renders with
    textContent, but history must never carry terminal escapes / bidi
    overrides into a future renderer. Newlines and tabs survive; DEL and
    C0/C1 controls do not. The 2000-char cap is enforced server-side.
    """
    cleaned = "".join(
        ch for ch in text if ch in "\n\t" or (ord(ch) >= 0x20 and ord(ch) != 0x7F)
    )
    return cleaned[:_MAX_TEXT]


def _event(kind: str, email: str, display: str, text: str = "", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": kind,
        "user": email,
        "display": display,
        "text": text,
        "ts": time.time(),
    }
    payload.update(extra)
    return payload


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    # Identity comes ONLY from AuthMiddleware-populated scope state. A query
    # param fallback here is a full impersonation primitive the moment the
    # middleware is bypassed — never trust client-supplied identity.
    if getattr(websocket.state, "user", None) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    email, display = _user_from_ws(websocket)
    if not email:
        await websocket.close(code=4401)
        return

    await hub.add(websocket)
    first = await presence.join(email, display)
    try:
        # Snapshot presence to the joining client.
        await websocket.send_text(
            json.dumps(
                _event("presence", email, display, users=await presence.list()),
                separators=(",", ":"),
            )
        )
        if first:
            await hub.broadcast(_event("join", email, display), skip=websocket)

        while True:
            raw = await websocket.receive_text()
            try:
                revalidate_websocket_user(websocket.scope)
            except AuthError as exc:
                await websocket.close(code=4401 if exc.status == 401 else 4403)
                return
            text = ""
            try:
                body = json.loads(raw)
                if isinstance(body, dict):
                    text = str(body.get("text") or body.get("message") or "")
                else:
                    text = str(body)
            except (json.JSONDecodeError, TypeError):
                text = raw
            text = text.strip()
            if not text:
                continue
            text = _clean_text(text)
            if not text:
                continue
            if not chat_messages.check(email):
                # Burst control: drop + notify in-band; do NOT tear down the
                # socket for a chatty user (HTTPException has no meaning here).
                await websocket.send_text(
                    json.dumps(
                        _event(
                            "rate_limited",
                            email,
                            display,
                            "too many messages — slow down",
                        ),
                        separators=(",", ":"),
                    )
                )
                continue
            msg = await save_message(email, display, text)
            await hub.broadcast(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("chat ws error for %s", email)
    finally:
        await hub.remove(websocket)
        left = await presence.leave(email)
        if left:
            try:
                await hub.broadcast(_event("leave", email, display))
            except Exception:
                pass


@router.get("/api/chat/history")
async def chat_history(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
) -> JSONResponse:
    # Auth is enforced by GatewayCore middleware; we only need the store.
    _ = request
    items = await load_history(limit=limit)
    return JSONResponse({"messages": items, "limit": limit})
