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
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

try:
    from .auth import AuthError, revalidate_websocket_user
    from . import orgs, deployment_drain
    from .auth import get_current_user
except ImportError:
    from auth import AuthError, revalidate_websocket_user  # type: ignore
    import orgs, deployment_drain
    from auth import get_current_user

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
    """Live portal chat presence, partitioned by team or personal account."""

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _users: dict[tuple[str, str], str] = field(default_factory=dict)
    _refs: dict[tuple[str, str], int] = field(default_factory=dict)

    async def join(self, scope_key: str, email: str, display: str) -> bool:
        """Register a connection. Returns True if this is a new presence entry."""
        email = (email or "").strip().lower()
        display = (display or email or "user").strip() or "user"
        if not email:
            return False
        key = (scope_key, email)
        async with self._lock:
            first = key not in self._users
            self._users[key] = display
            self._refs[key] = self._refs.get(key, 0) + 1
            return first

    async def leave(self, scope_key: str, email: str) -> bool:
        """Drop one connection. Returns True if the user fully left presence."""
        email = (email or "").strip().lower()
        if not email:
            return False
        key = (scope_key, email)
        async with self._lock:
            n = self._refs.get(key, 0) - 1
            if n <= 0:
                self._refs.pop(key, None)
                self._users.pop(key, None)
                return True
            self._refs[key] = n
            return False

    async def list(self, scope_key: str | None = None) -> list[dict[str, str]]:
        async with self._lock:
            users = list(self._users.items())
        visible: list[dict[str, str]] = []
        for (scope, email), display in users:
            if scope_key is not None and scope != scope_key:
                continue
            if scope_key is not None and scope.startswith("team:"):
                try:
                    await asyncio.to_thread(
                        orgs.resolve_team_bucket, email, scope[5:], write=False
                    )
                except HTTPException:
                    continue
            visible.append({"email": email, "display": display})
        return sorted(visible, key=lambda user: user["display"].lower())


presence = Presence()


# ---------------------------------------------------------------------------
# History (PostgreSQL — migrations/0001_init.sql and 0020_chat_scope.sql)
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


def _insert_message(ts: float, scope_key: str, email: str, display: str, text: str) -> int:
    with tx() as cur:
        cur.execute(
            "INSERT INTO messages (ts, scope_key, email, display, text)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (ts, scope_key, email, display, text),
        )
        return int(cur.fetchone()["id"])


def _select_history(scope_key: str, limit: int) -> list[dict[str, Any]]:
    with tx() as cur:
        cur.execute(
            """
            SELECT id, ts, email, display, text
            FROM messages
            WHERE scope_key = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (scope_key, limit),
        )
        rows = cur.fetchall()
    return [_message_row(r) for r in reversed(rows)]


async def save_message(
    scope_key: str, email: str, display: str, text: str, ts: float | None = None
) -> dict[str, Any]:
    ts = float(ts if ts is not None else time.time())
    email = (email or "").strip().lower()
    display = (display or email or "user").strip() or "user"
    text = (text or "").strip()
    row_id = await deployment_drain.run_blocking(_insert_message, ts, scope_key, email, display, text)
    return {
        "type": "message",
        "id": row_id,
        "user": email,
        "display": display,
        "text": text,
        "ts": ts,
    }


async def load_history(scope_key: str, limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 500))
    return await asyncio.to_thread(_select_history, scope_key, limit)


# ---------------------------------------------------------------------------
# WebSocket hub
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Connection:
    scope: str
    email: str
    display: str
    loop: asyncio.AbstractEventLoop
    revoked: bool = False


class Hub:
    def __init__(self) -> None:
        # Org/account mutations can run in a different TestClient loop or a
        # sync worker thread. Never touch a WebSocket from that other thread.
        self._lock = threading.Lock()
        self._clients: dict[WebSocket, _Connection] = {}

    async def add(self, ws: WebSocket, scope_key: str, email: str, display: str) -> None:
        with self._lock:
            self._clients[ws] = _Connection(scope_key, email, display, asyncio.get_running_loop())

    def is_active(self, ws: WebSocket) -> bool:
        with self._lock:
            entry = self._clients.get(ws)
            return entry is not None and not entry.revoked

    async def remove(self, ws: WebSocket) -> tuple[str, str, str] | None:
        with self._lock:
            entry = self._clients.pop(ws, None)
        return (entry.scope, entry.email, entry.display) if entry else None

    def revoke(self, email: str, team_id: str | None = None) -> None:
        """Immediately exclude matching sockets; close them on their own loops."""
        email = email.strip().lower()
        scope = "team:" + team_id if team_id is not None else None
        with self._lock:
            targets = []
            for ws, entry in self._clients.items():
                if entry.email != email or (scope is not None and entry.scope != scope) or entry.revoked:
                    continue
                entry.revoked = True
                targets.append((ws, entry.loop))
        for ws, loop in targets:
            loop.call_soon_threadsafe(lambda socket=ws: asyncio.create_task(self._close_revoked(socket)))

    def revoke_team(self, team_id: str) -> None:
        """Close every live socket scoped to a deleted team."""
        scope = "team:" + team_id
        with self._lock:
            users = {entry.email for entry in self._clients.values()
                     if entry.scope == scope and not entry.revoked}
        for email in users:
            self.revoke(email, team_id)

    async def _close_revoked(self, ws: WebSocket) -> None:
        try:
            await ws.close(code=4403, reason="chat access revoked")
        except Exception:
            pass  # The peer may already have disconnected.
        finally:
            await _leave(ws)

    async def broadcast(
        self, scope_key: str, payload: dict[str, Any], *, skip: WebSocket | None = None
    ) -> None:
        data = json.dumps(payload, separators=(",", ":"))
        with self._lock:
            targets = [ws for ws, entry in self._clients.items()
                       if entry.scope == scope_key and not entry.revoked]
        dead: list[WebSocket] = []
        for ws in targets:
            if skip is not None and ws is skip:
                continue
            with self._lock:
                entry = self._clients.get(ws)
                if entry is None or entry.revoked:
                    continue
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await _leave(ws)


async def _leave(ws: WebSocket) -> None:
    entry = await hub.remove(ws)
    if entry is None:
        return
    scope_key, email, display = entry
    if await presence.leave(scope_key, email):
        await hub.broadcast(scope_key, _event("leave", email, display))

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

def _scope_key(request: Request | WebSocket, email: str) -> str:
    team = orgs.selected_workspace_team(request, email)
    if team:
        return "team:" + orgs.resolve_team_bucket(email, team, write=False).team_id
    return "personal:" + email


_MAX_TEXT = 2000


def _clean_text(text: str) -> str:
    """Strip control characters and cap length BEFORE persisting.

    The Postgres store is the durable artifact — today's frontend renders with
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
    email, display = _user_from_ws(websocket)
    if not email:
        await websocket.close(code=4401)
        return
    try:
        scope_key = _scope_key(websocket, email)
    except Exception:
        await websocket.close(code=4403)
        return

    await websocket.accept()

    await hub.add(websocket, scope_key, email, display)
    first = await presence.join(scope_key, email, display)
    try:
        # Snapshot presence to the joining client.
        await websocket.send_text(
            json.dumps(
                _event("presence", email, display, users=await presence.list(scope_key)),
                separators=(",", ":"),
            )
        )
        if first:
            await hub.broadcast(scope_key, _event("join", email, display), skip=websocket)

        while True:
            raw = await websocket.receive_text()
            if not hub.is_active(websocket):
                await websocket.close(code=4403, reason="chat access revoked")
                return
            try:
                revalidate_websocket_user(websocket.scope)
            except AuthError as exc:
                await websocket.close(code=4401 if exc.status == 401 else 4403)
                return
            try:
                if _scope_key(websocket, email) != scope_key:
                    await websocket.close(code=4403)
                    return
            except Exception:
                await websocket.close(code=4403)
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
            msg = await save_message(scope_key, email, display, text)
            await hub.broadcast(scope_key, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("chat ws error for %s", email)
    finally:
        await _leave(websocket)


@router.get("/api/chat/history")
async def chat_history(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
) -> JSONResponse:
    email = get_current_user(request).email.strip().lower()
    scope_key = _scope_key(request, email)
    items = await load_history(scope_key, limit=limit)
    return JSONResponse({"messages": items, "limit": limit})
