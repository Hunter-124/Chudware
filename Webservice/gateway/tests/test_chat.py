"""Portal chat: history persistence + WS fan-out / presence."""

from __future__ import annotations

import asyncio
import json
import uuid
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
    accounts.ensure_account("charlie@example.com", display="Charlie")
    accounts.set_status("charlie@example.com", "active", decided_by="test")
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
    from orgs import router as orgs_router  # type: ignore
    app.include_router(orgs_router)

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


def _team(owner: str, member: str | None = None) -> str:
    import db  # type: ignore

    org_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO organizations (id, name, created_by) VALUES (%s, %s, %s)",
            (org_id, "Test team", owner),
        )
        cur.execute(
            "INSERT INTO org_members (org_id, email, role) VALUES (%s, %s, 'owner')",
            (org_id, owner),
        )
        cur.execute(
            "INSERT INTO teams (id, org_id, name, is_general) VALUES (%s, %s, 'General', TRUE)",
            (team_id, org_id),
        )
        if member:
            cur.execute(
                "INSERT INTO org_members (org_id, email, role) VALUES (%s, %s, 'member')",
                (org_id, member),
            )
            cur.execute(
                "INSERT INTO team_members (team_id, email, role) VALUES (%s, %s, 'member')",
                (team_id, member),
            )
    return team_id


def test_history_roundtrip(chat_env):
    chat, config, data = chat_env
    from fastapi.testclient import TestClient

    app = _make_app(chat, _User("alice@example.com", "Alice"))
    with TestClient(app) as client:
        r = client.get("/api/chat/history")
        assert r.status_code == 200
        assert r.json()["messages"] == []

        async def seed():
            await chat.save_message("personal:alice@example.com", "alice@example.com", "Alice", "hello history")
            await chat.save_message("personal:bob@example.com", "bob@example.com", "Bob", "second")

        asyncio.run(seed())

        r = client.get("/api/chat/history?limit=10")
        assert r.status_code == 200
        msgs = r.json()["messages"]
        assert len(msgs) == 1
        assert msgs[0]["text"] == "hello history"
        assert msgs[0]["user"] == "alice@example.com"
        assert msgs[0]["display"] == "Alice"
        with TestClient(_make_app(chat, _User("bob@example.com", "Bob"))) as bob:
            assert [m["text"] for m in bob.get("/api/chat/history").json()["messages"]] == ["second"]
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
    team_id = _team("alice@example.com", "bob@example.com")
    app_a = _make_app(chat, user=_User("alice@example.com", "Alice"))
    app_b = _make_app(chat, user=_User("bob@example.com", "Bob"))
    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        with client_a.websocket_connect(f"/ws/chat?team={team_id}") as ws_a:
            snap = ws_a.receive_json()
            assert snap["type"] == "presence"
            assert any(u["email"] == "alice@example.com" for u in snap["users"])

            with client_b.websocket_connect(f"/ws/chat?team={team_id}") as ws_b:
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

        r = client_a.get("/api/chat/history", params={"team": team_id})
        assert r.status_code == 200
        texts = [m["text"] for m in r.json()["messages"]]
        assert "hello lab" in texts


def test_chat_teams_never_expose_messages_or_presence_to_unrelated_accounts(chat_env):
    chat, _, _ = chat_env
    from fastapi.testclient import TestClient

    team_id = _team("alice@example.com", "bob@example.com")
    other_team = _team("charlie@example.com")
    app_a = _make_app(chat, _User("alice@example.com", "Alice"))
    app_b = _make_app(chat, _User("bob@example.com", "Bob"))
    app_c = _make_app(chat, _User("charlie@example.com", "Charlie"))
    with TestClient(app_a) as alice, TestClient(app_b) as bob, TestClient(app_c) as charlie:
        with alice.websocket_connect(f"/ws/chat?team={team_id}") as a:
            assert [u["email"] for u in a.receive_json()["users"]] == ["alice@example.com"]
            with bob.websocket_connect(f"/ws/chat?team={team_id}") as b:
                assert {u["email"] for u in b.receive_json()["users"]} == {
                    "alice@example.com", "bob@example.com"
                }
                assert a.receive_json()["type"] == "join"
                with charlie.websocket_connect(f"/ws/chat?team={other_team}") as c:
                    assert [u["email"] for u in c.receive_json()["users"]] == ["charlie@example.com"]
                    a.send_json({"text": "private team design"})
                    assert a.receive_json()["text"] == "private team design"
                    assert b.receive_json()["text"] == "private team design"
                    c.send_json({"text": "other team note"})
                    assert c.receive_json()["text"] == "other team note"
                    assert [
                        m["text"] for m in charlie.get(
                            "/api/chat/history", params={"team": other_team}
                        ).json()["messages"]
                    ] == ["other team note"]
                    assert [
                        m["text"] for m in bob.get(
                            "/api/chat/history", params={"team": team_id}
                        ).json()["messages"]
                    ] == ["private team design"]
                    assert charlie.get(
                        "/api/chat/history", params={"team": team_id}
                    ).status_code == 404


@pytest.mark.parametrize("remove_from_org", [False, True])
def test_membership_removal_revokes_idle_socket_without_affecting_others(
    chat_env, remove_from_org
):
    chat, _, _ = chat_env
    import db  # type: ignore
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    team_id = _team("alice@example.com", "bob@example.com")
    with db.tx() as cur:
        cur.execute("SELECT org_id FROM teams WHERE id = %s", (team_id,))
        org_id = str(cur.fetchone()["org_id"])

    with TestClient(_make_app(chat, _User("alice@example.com", "Alice"))) as admin, \
         TestClient(_make_app(chat, _User("bob@example.com", "Bob"))) as member:
        with admin.websocket_connect(f"/ws/chat?team={team_id}") as ws_a:
            assert ws_a.receive_json()["type"] == "presence"
            with member.websocket_connect(f"/ws/chat?team={team_id}") as ws_b:
                assert {u["email"] for u in ws_b.receive_json()["users"]} == {
                    "alice@example.com", "bob@example.com"
                }
                assert ws_a.receive_json()["type"] == "join"
                endpoint = (
                    f"/api/orgs/{org_id}/members/bob@example.com"
                    if remove_from_org
                    else f"/api/orgs/{org_id}/teams/{team_id}/members/bob@example.com"
                )
                response = admin.delete(endpoint)
                assert response.status_code == 200
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws_b.receive_json()
                assert exc.value.code == 4403
                assert ws_a.receive_json()["type"] == "leave"
                assert asyncio.run(chat.presence.list("team:" + team_id)) == [
                    {"email": "alice@example.com", "display": "Alice"}
                ]
                ws_a.send_json({"text": "remaining team message"})
                assert ws_a.receive_json()["text"] == "remaining team message"


def test_account_suspension_revokes_idle_team_and_personal_sockets(chat_env):
    chat, _, _ = chat_env
    import accounts  # type: ignore
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    team_id = _team("alice@example.com", "bob@example.com")
    with TestClient(_make_app(chat, _User("bob@example.com", "Bob"))) as client:
        with client.websocket_connect(f"/ws/chat?team={team_id}") as team_ws, \
             client.websocket_connect("/ws/chat") as personal_ws:
            assert team_ws.receive_json()["type"] == "presence"
            assert personal_ws.receive_json()["type"] == "presence"
            accounts.set_status("bob@example.com", "suspended", decided_by="test")
            for ws in (team_ws, personal_ws):
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 4403
            assert asyncio.run(chat.presence.list()) == []


@pytest.mark.parametrize("delete_org", [False, True])
def test_deleting_team_or_organization_revokes_all_team_sockets(chat_env, delete_org):
    chat, _, _ = chat_env
    import db  # type: ignore
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    general_id = _team("alice@example.com", "bob@example.com")
    with db.tx() as cur:
        cur.execute("SELECT org_id FROM teams WHERE id = %s", (general_id,))
        org_id = str(cur.fetchone()["org_id"])
    with TestClient(_make_app(chat, _User("alice@example.com", "Alice"))) as alice, \
         TestClient(_make_app(chat, _User("bob@example.com", "Bob"))) as bob:
        team_id = general_id
        if not delete_org:
            response = alice.post(f"/api/orgs/{org_id}/teams", json={"name": "Design"})
            assert response.status_code == 201
            team_id = response.json()["team"]["id"]
            response = alice.post(
                f"/api/orgs/{org_id}/teams/{team_id}/members",
                json={"email": "bob@example.com"},
            )
            assert response.status_code == 201
        with alice.websocket_connect(f"/ws/chat?team={team_id}") as ws_a:
            assert ws_a.receive_json()["type"] == "presence"
            with bob.websocket_connect(f"/ws/chat?team={team_id}") as ws_b:
                assert ws_b.receive_json()["type"] == "presence"
                assert ws_a.receive_json()["type"] == "join"
                endpoint = f"/api/orgs/{org_id}" if delete_org else f"/api/orgs/{org_id}/teams/{team_id}"
                assert alice.delete(endpoint).status_code == 200
                for ws in (ws_a, ws_b):
                    with pytest.raises(WebSocketDisconnect) as exc:
                        ws.receive_json()
                    assert exc.value.code == 4403
                assert asyncio.run(chat.presence.list("team:" + team_id)) == []


def test_org_admin_demotion_revokes_implicit_team_access(chat_env):
    chat, _, _ = chat_env
    import db  # type: ignore
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    team_id = _team("alice@example.com")
    with db.tx() as cur:
        cur.execute("SELECT org_id FROM teams WHERE id = %s", (team_id,))
        org_id = str(cur.fetchone()["org_id"])
        cur.execute(
            "INSERT INTO org_members (org_id, email, role) VALUES (%s, %s, 'admin')",
            (org_id, "bob@example.com"),
        )
    with TestClient(_make_app(chat, _User("alice@example.com", "Alice"))) as owner, \
         TestClient(_make_app(chat, _User("bob@example.com", "Bob"))) as admin:
        with admin.websocket_connect(f"/ws/chat?team={team_id}") as ws:
            assert ws.receive_json()["type"] == "presence"
            response = owner.patch(
                f"/api/orgs/{org_id}/members/bob@example.com", json={"role": "member"}
            )
            assert response.status_code == 200
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 4403
            assert asyncio.run(chat.presence.list("team:" + team_id)) == []


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
    assert asyncio.run(chat.load_history("personal:alice@example.com")) == []


def test_presence_refcount(chat_env):
    chat, _, _ = chat_env

    async def run():
        p = chat.Presence()
        assert await p.join("personal:a@x.com", "a@x.com", "A") is True
        assert await p.join("personal:b@x.com", "b@x.com", "B") is True
        assert await p.join("personal:a@x.com", "a@x.com", "A") is False
        assert await p.list("personal:a@x.com") == [{"email": "a@x.com", "display": "A"}]
        assert await p.leave("personal:a@x.com", "a@x.com") is False
        assert await p.list("personal:a@x.com") == [{"email": "a@x.com", "display": "A"}]
        assert await p.leave("personal:a@x.com", "a@x.com") is True
        assert await p.list("personal:a@x.com") == []

    asyncio.run(run())
