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
def chat_env(pg, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CHUDWARE_WEB_DATA_ROOT", str(data))
    monkeypatch.setenv("CHUDWARE_WEB_DEV", "1")

    for mod in ("gateway.config", "gateway.chat", "config", "chat"):
        sys.modules.pop(mod, None)

    import config  # type: ignore
    import chat  # type: ignore

    config.settings = config.load_settings()
    chat.presence = chat.Presence()
    chat.hub = chat.Hub()
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
