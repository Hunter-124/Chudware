"""Portal chat: history persistence + WS fan-out / presence."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))


class _User:
    def __init__(self, email: str, display: str, role: str = "user", heavy: bool = False):
        self.email = email
        self.display = display
        self.role = role
        self.heavy = heavy


@pytest.fixture()
def chat_env(pg, tmp_path, monkeypatch, reload_settings):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CHUDWARE_WEB_DATA_ROOT", str(data))
    monkeypatch.setenv("CHUDWARE_WEB_DEV", "1")

    # In-place, so the `settings` object `auth` bound at import time is the one
    # that now carries dev_mode=True. Swapping `config.settings` for a new object
    # instead left `auth` on the old one, `AuthMiddleware.__init__` hit its
    # no-strong-auth fail-fast inside the WS message loop, no frame was ever
    # broadcast, and the `receive_json()` below blocked forever — but only when
    # some earlier test file had already imported `auth`.
    reload_settings()

    import auth  # type: ignore
    import chat  # type: ignore
    import config  # type: ignore

    # Per-test fan-out state; the module singletons outlive one test otherwise.
    chat.presence = chat.Presence()
    chat.hub = chat.Hub()

    import accounts  # type: ignore
    accounts.ensure_account("alice@example.com", display="Alice")
    accounts.set_status("alice@example.com", "active", decided_by="test")
    accounts.ensure_account("bob@example.com", display="Bob")
    accounts.set_status("bob@example.com", "active", decided_by="test")
    monkeypatch.setattr(
        chat,
        "revalidate_websocket_user",
        lambda scope: auth.AuthMiddleware(lambda *_: None)._resolve_user(
            scope["state"]["user"].email
        ),
    )
    return chat, config, data




def _make_app(chat, user: _User | None = None):
    from fastapi import FastAPI
    from starlette.types import ASGIApp, Receive, Scope, Send

    app = FastAPI()
    app.include_router(chat.router)

    if user is not None:

        class AttachUser:
            def __init__(self, app_: ASGIApp, u: _User):
                self.app = app_
                self.u = u

            async def __call__(self, scope: Scope, receive: Receive, send: Send):
                if scope["type"] in {"http", "websocket"}:
                    st = scope.setdefault("state", {})
                    if isinstance(st, dict):
                        st["user"] = self.u
                        st["user_email"] = self.u.email
                        st["user_display"] = self.u.display
                await self.app(scope, receive, send)

        app.add_middleware(AttachUser, u=user)
    return app


def test_history_roundtrip(chat_env):
    chat, config, data = chat_env
    from fastapi.testclient import TestClient

    app = _make_app(chat, _User("alice@example.com", "Alice"))
    with TestClient(app) as client:
        r = client.get("/api/chat/history")
        assert r.status_code == 200
        assert r.json()["messages"] == []

        async def seed():
            await chat.save_message("alice@example.com", "Alice", "hello history")
            await chat.save_message("bob@example.com", "Bob", "second")

        asyncio.run(seed())

        r = client.get("/api/chat/history?limit=10")
        assert r.status_code == 200
        msgs = r.json()["messages"]
        assert len(msgs) == 2
        assert msgs[0]["text"] == "hello history"
        assert msgs[0]["user"] == "alice@example.com"
        assert msgs[0]["display"] == "Alice"
        assert msgs[1]["text"] == "second"
        assert msgs[1]["user"] == "bob@example.com"
        # Persisted, not merely fanned out in memory.
        import db  # type: ignore

        with db.tx() as cur:
            cur.execute("SELECT text FROM messages ORDER BY id")
            assert [r["text"] for r in cur.fetchall()] == ["hello history", "second"]


def test_ws_broadcast_and_presence(chat_env):
    chat, config, data = chat_env
    from fastapi.testclient import TestClient

    # Identity comes from middleware-attached state.user only — chat.py has
    # no unauthenticated query-param fallback (impersonation primitive).
    app_a = _make_app(chat, user=_User("alice@example.com", "Alice"))
    app_b = _make_app(chat, user=_User("bob@example.com", "Bob"))
    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        with client_a.websocket_connect("/ws/chat") as ws_a:
            snap = ws_a.receive_json()
            assert snap["type"] == "presence"
            assert any(u["email"] == "alice@example.com" for u in snap["users"])

            with client_b.websocket_connect("/ws/chat") as ws_b:
                snap_b = ws_b.receive_json()
                assert snap_b["type"] == "presence"
                emails = {u["email"] for u in snap_b["users"]}
                assert "alice@example.com" in emails
                assert "bob@example.com" in emails

                join_a = ws_a.receive_json()
                assert join_a["type"] == "join"
                assert join_a["user"] == "bob@example.com"
                assert join_a["display"] == "Bob"

                ws_a.send_text(json.dumps({"text": "hello lab"}))

                def drain_message(ws, first):
                    cur = first
                    for _ in range(8):
                        if cur.get("type") == "message":
                            return cur
                        cur = ws.receive_json()
                    return cur

                msg_a = drain_message(ws_a, ws_a.receive_json())
                msg_b = drain_message(ws_b, ws_b.receive_json())
                assert msg_a["type"] == "message"
                assert msg_a["text"] == "hello lab"
                assert msg_a["user"] == "alice@example.com"
                assert msg_b["type"] == "message"
                assert msg_b["text"] == "hello lab"
                assert msg_b["display"] == "Alice"

                online = asyncio.run(chat.presence.list())
                assert {u["email"] for u in online} == {
                    "alice@example.com",
                    "bob@example.com",
                }

            leave = ws_a.receive_json()
            assert leave["type"] == "leave"
            assert leave["user"] == "bob@example.com"

        r = client_a.get("/api/chat/history")
        assert r.status_code == 200
        texts = [m["text"] for m in r.json()["messages"]]
        assert "hello lab" in texts


def test_ws_suspension_closes_before_next_message_is_saved(chat_env):
    chat, _config, _data = chat_env
    import accounts  # type: ignore
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    app = _make_app(chat, _User("alice@example.com", "Alice"))
    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws/chat", headers={"X-Dev-User": "alice@example.com"}
        ) as ws:
            assert ws.receive_json()["type"] == "presence"
            accounts.set_status("alice@example.com", "suspended", decided_by="test")
            ws.send_json({"text": "must not persist"})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4403
    assert asyncio.run(chat.load_history()) == []


def test_presence_refcount(chat_env):
    chat, _, _ = chat_env

    async def run():
        p = chat.Presence()
        assert await p.join("a@x.com", "A") is True
        assert await p.join("a@x.com", "A") is False
        assert await p.list() == [{"email": "a@x.com", "display": "A"}]
        assert await p.leave("a@x.com") is False
        assert await p.list() == [{"email": "a@x.com", "display": "A"}]
        assert await p.leave("a@x.com") is True
        assert await p.list() == []

    asyncio.run(run())
