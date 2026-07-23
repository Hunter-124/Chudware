"""Portal shoutbox: WebSocket fan-out + SQLite history + presence.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

try:
    from .config import settings
except ImportError:  # allow `python -c 'import chat'` from package dir
    from config import settings  # type: ignore

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
# SQLite history
# ---------------------------------------------------------------------------

_DB_READY = False
_DB_LOCK = asyncio.Lock()


def _db_path() -> Path:
    path = settings.chat_db
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def _ensure_db() -> Path:
    global _DB_READY
    path = _db_path()
    if _DB_READY:
        return path
    async with _DB_LOCK:
        if _DB_READY:
            return path
        async with aiosqlite.connect(path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    email TEXT NOT NULL,
                    display TEXT NOT NULL,
                    text TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts)"
            )
            await db.commit()
        _DB_READY = True
        return path


async def save_message(email: str, display: str, text: str, ts: float | None = None) -> dict[str, Any]:
    path = await _ensure_db()
    ts = float(ts if ts is not None else time.time())
    email = (email or "").strip().lower()
    display = (display or email or "user").strip() or "user"
    text = (text or "").strip()
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "INSERT INTO messages (ts, email, display, text) VALUES (?, ?, ?, ?)",
            (ts, email, display, text),
        )
        await db.commit()
        row_id = cur.lastrowid
    return {
        "type": "message",
        "id": row_id,
        "user": email,
        "display": display,
        "text": text,
        "ts": ts,
    }


async def load_history(limit: int = 100) -> list[dict[str, Any]]:
    path = await _ensure_db()
    limit = max(1, min(int(limit or 100), 500))
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, ts, email, display, text
            FROM messages
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in reversed(rows):
        out.append(
            {
                "type": "message",
                "id": r["id"],
                "user": r["email"],
                "display": r["display"],
                "text": r["text"],
                "ts": r["ts"],
            }
        )
    return out


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
    await websocket.accept()
    email, display = _user_from_ws(websocket)
    if not email:
        # Dev / unauthenticated fallback: allow query params so tests work.
        email = (websocket.query_params.get("email") or "").strip().lower()
        display = (websocket.query_params.get("display") or email or "user").strip() or "user"
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
            if len(text) > 2000:
                text = text[:2000]
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
