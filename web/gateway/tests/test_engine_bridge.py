"""Hermetic cell sockets; real PostgreSQL principals and revocation epochs."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import tempfile
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway import accounts, cells, db, engine_bridge, orgs, provider_broker, provider_keys
from gateway.config import settings


class FakeDaemon:
    def __init__(self, path):
        self.path, self.calls = path, []
        self.mute = False
        self.called = threading.Condition()
        self.ready = threading.Event()
        self.writers = []
        self.loop = None
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def start(self):
        self.thread.start()
        assert self.ready.wait(5)

    def serve(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        async def boot():
            server = await asyncio.start_unix_server(self.client, path=str(self.path))
            self.path.chmod(0o600)
            operator_path = self.path.parent / "state/run/operator.sock"
            operator_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            operator = await asyncio.start_unix_server(self.client, path=str(operator_path))
            operator_path.chmod(0o600)
            self.ready.set()
            return server, operator
        servers = self.loop.run_until_complete(boot())
        try:
            self.loop.run_forever()
        finally:
            for server in servers:
                server.close()
                self.loop.run_until_complete(server.wait_closed())
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()

    async def client(self, reader, writer):
        self.writers.append(writer)
        try:
            while raw := await reader.readline():
                request = json.loads(raw)
                with self.called:
                    self.calls.append(request)
                    self.called.notify_all()
                method = request["method"]
                if self.mute and request["id"] != engine_bridge._INTERNAL_HELLO:
                    continue
                result = ({"doc_id": "doc-1", "aicm_path": "/state/mesh/" + "a" * 32 + ".aicm"}
                          if method == "doc_open" else {"ok": True, "proto": 1})
                writer.write(json.dumps({"id": request["id"], "result": result}).encode() + b"\n")
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            self.writers.remove(writer)
            writer.close()

    def wait_calls(self, count):
        with self.called:
            assert self.called.wait_for(lambda: len(self.calls) >= count, timeout=5)

    def inject(self, data):
        async def send():
            for writer in list(self.writers):
                writer.write(data)
                await writer.drain()
        asyncio.run_coroutine_threadsafe(send(), self.loop).result(5)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def environment(pg, tmp_path, monkeypatch, request):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspace")
    monkeypatch.setattr(settings, "bucket_root", tmp_path / "bucket")
    cell_directory = tempfile.TemporaryDirectory(prefix="cw-cells-", dir="/tmp")
    request.addfinalizer(cell_directory.cleanup)
    monkeypatch.setattr(settings, "cells_root", Path(cell_directory.name))
    monkeypatch.setattr(settings, "university_team_id", "")
    monkeypatch.setattr(settings, "provider_key_encryption_key",
                        base64.urlsafe_b64encode(b"B" * 32).decode().rstrip("="))
    monkeypatch.setattr(provider_broker, "_broker", None)
    monkeypatch.setattr(engine_bridge, "revalidate_websocket_user", lambda *a, **k: None)
    monkeypatch.setattr(orgs, "selected_workspace_team", lambda ws, email: ws.query_params.get("team", ""))
    policy = tmp_path / "methods.json"
    policy.write_text(json.dumps({name: {"cell": "allow", "right": right, "scope": "workspace"}
                                 for name, right in (("hello", "read"), ("ping", "read"),
                                                      ("doc_open", "read"), ("doc_save", "write"))}))
    monkeypatch.setattr(cells, "POLICY_PATH", policy)
    for email in ("a@example.com", "b@example.com"):
        accounts.ensure_account(email)
        accounts.set_status(email, "active")
        accounts.set_plan(email, "studio")
    @asynccontextmanager
    async def lifespan(app):
        await provider_broker.startup()
        try:
            yield
        finally:
            await provider_broker.shutdown()
    app = FastAPI(lifespan=lifespan)
    app.include_router(engine_bridge.router)
    class Identity:
        def __init__(self, app): self.app = app
        async def __call__(self, scope, receive, send):
            if scope["type"] == "websocket":
                headers = dict(scope["headers"])
                scope.setdefault("state", {})["user"] = SimpleNamespace(
                    email=headers.get(b"x-user", b"a@example.com").decode(), display="Test")
            await self.app(scope, receive, send)
    app.add_middleware(Identity)
    fakes = {}
    activate = cells.start_cell
    async def start(cell_id):
        if cell_id not in fakes:
            fake = FakeDaemon(settings.cells_root / cell_id / "gw.sock")
            fake.start()
            fakes[cell_id] = fake
    monkeypatch.setattr(cells, "start_cell", start)
    async def service_state(cell_id):
        return {"cell_id": cell_id, "active": "active" if cell_id in fakes else "inactive",
                "sub": "running" if cell_id in fakes else "dead", "pid": 123 if cell_id in fakes else 0,
                "invocation": "d" * 32 if cell_id in fakes else ""}
    monkeypatch.setattr(cells, "service_state", service_state)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, fakes=fakes, root=tmp_path, activate=activate)
    for fake in fakes.values():
        fake.stop()
    engine_bridge._mesh_grants.clear()


def request(ws, method, ident=1, params=None):
    ws.send_json({"id": ident, "method": method, "params": params or {}})
    return ws.receive_json()


def test_distinct_accounts_resolve_distinct_cells_and_principal_hello(environment):
    env = environment
    with env.client.websocket_connect("/engine", headers={"x-user": "a@example.com"}) as a:
        with env.client.websocket_connect("/engine", headers={"x-user": "b@example.com"}) as b:
            ar = request(a, "hello")["result"]
            br = request(b, "hello")["result"]
            assert ar["workspace_key"] != br["workspace_key"]
            assert ar["web_rpc_version"] == 2
            assert len(env.fakes) == 2
            assert {fake.calls[0]["params"]["principal"]["actor_key"] for fake in env.fakes.values()} == {
                accounts.get_account("a@example.com").uid, accounts.get_account("b@example.com").uid}


def team_fixture():
    org_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.tx() as cur:
        cur.execute("INSERT INTO organizations(id,name) VALUES(%s,'test')", (org_id,))
        cur.execute("INSERT INTO org_members(org_id,email,role) VALUES(%s,'a@example.com','owner'),"
                    "(%s,'b@example.com','member')", (org_id, org_id))
        cur.execute("INSERT INTO teams(id,org_id,name) VALUES(%s,%s,'team')", (team_id, org_id))
        cur.execute("INSERT INTO team_members(team_id,email,role) VALUES(%s,'b@example.com','viewer')", (team_id,))
    return org_id, team_id


def test_viewer_cannot_write_and_membership_removal_closes_silent_observer(environment):
    env = environment
    org_id, team_id = team_fixture()
    with env.client.websocket_connect(f"/engine?team={team_id}", headers={"x-user": "b@example.com"}) as ws:
        assert request(ws, "doc_save")["error"]["code"] == "forbidden_right"
        assert not any(c["method"] == "doc_save" for f in env.fakes.values() for c in f.calls)
        principal = cells.resolve_workspace("b@example.com", team_id)
        engine_bridge.grant_mesh_document(principal, "doc-1", "/work/mesh.aicm")
        assert engine_bridge.mesh_document_authorized(principal.account_uid, principal.workspace_key, "doc-1")
        with db.tx() as cur:
            cur.execute("DELETE FROM team_members WHERE team_id=%s AND email='b@example.com'", (team_id,))
        cells.revoke("b@example.com", team_id)
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4403
        assert not engine_bridge.mesh_document_authorized(principal.account_uid, principal.workspace_key, "doc-1")


def test_unknown_and_forged_identity_requests_never_reach_cell(environment):
    with environment.client.websocket_connect("/engine") as ws:
        assert request(ws, "future_unknown")["error"]["code"] == "cell_refused"
        assert request(ws, "ping", params={"principal": {"right": "owner"}})["error"]["code"] == -32700
        assert request(ws, "ping", ident=2)["result"]["ok"] is True
        assert not any(c["method"] == "future_unknown" for f in environment.fakes.values() for c in f.calls)


@pytest.mark.parametrize("method", ["agent_admin_turn", "agent_cancel", "agent_steer",
                                  "agent_answer", "chudcode_close_session"])
def test_turns_receive_grants_but_controls_work_without_provider_keys(environment, method):
    policy = json.loads(cells.POLICY_PATH.read_text())
    policy[method] = {"cell": "allow", "right": "run", "scope": "actor"}
    cells.POLICY_PATH.write_text(json.dumps(policy))
    with environment.client.websocket_connect("/engine") as ws:
        params = {"provider": "openai", "session_id": "session"}
        if method == "agent_admin_turn":
            params["model"] = "test-model"
            assert request(ws, method, params=params)["error"]["code"] == "credential_isolation_unsupported"
            assert all(call["method"] != method for fake in environment.fakes.values() for call in fake.calls)
            provider_keys._persist_validated_key("a@example.com", "openai", "upstream-only-sentinel")
        assert request(ws, method, ident=2, params=params)["result"]["ok"] is True
        forwarded = next(call for fake in environment.fakes.values()
                         for call in fake.calls if call["method"] == method)["params"]
        assert "api_key" not in forwarded
        assert forwarded["_chudware_actor"]["key_source"] == "byok"
        assert "_byok_key_sha256" not in forwarded["_chudware_actor"]
        assert "upstream-only-sentinel" not in json.dumps(forwarded)
        if method == "agent_admin_turn":
            assert forwarded["provider_grant"].startswith("cpg1.")
        else:
            assert "provider_grant" not in forwarded


def test_mapped_turn_stream_reaches_browser_before_terminal_and_filters_foreign_actor(environment):
    from services.agent.chudcode_events import RpcFrameState, map_rpc_frame

    policy = json.loads(cells.POLICY_PATH.read_text())
    policy["agent_admin_turn"] = {"cell": "allow", "right": "run", "scope": "actor"}
    cells.POLICY_PATH.write_text(json.dumps(policy))
    provider_keys._persist_validated_key("a@example.com", "openai", "upstream-only-sentinel")
    context = {"run_id": "current-run", "session_id": "current-session"}
    state = RpcFrameState()

    def mapped(frame):
        return [{"event": "agent.event", "data": {**payload, "kind": kind}}
                for kind, payload in map_rpc_frame(frame, context, state)]

    opening = mapped({"type": "agent_start"})
    opening += mapped({"type": "message_update", "message": {"id": "current-message"},
                       "assistantMessageEvent": {"type": "thinking_delta", "delta": "Reasoning. "}})
    opening += mapped({"type": "message_update", "message": {"id": "current-message"},
                       "assistantMessageEvent": {"type": "text_delta", "delta": "Visible while held."}})
    assert [row["data"]["kind"] for row in opening] == [
        "run_started", "assistant_delta", "assistant_delta"]
    assert all("type" not in row["data"] for row in opening)
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = next(iter(environment.fakes.values()))
        fake.mute = True
        baseline = len(fake.calls)
        ws.send_json({"id": 41, "method": "agent_admin_turn",
                      "params": {**context, "request": "Hold the turn open",
                                 "provider": "openai", "model": "test-model"}})
        ws.send_json({"id": 42, "method": "ping"})
        fake.wait_calls(baseline + 2)
        foreign = {"event": "agent.event",
                   "data": {"kind": "assistant_delta", "run_id": "foreign-run",
                            "actor_key": "foreign-actor", "channel": "text",
                            "text": "Must not reach this browser."}}
        # The ping is a read barrier, not a turn terminal: a dropped stream fails
        # on its reply rather than hanging while waiting for an absent event.
        fake.inject(b"".join(json.dumps(row).encode() + b"\n"
                             for row in [foreign, *opening, {"id": 42, "result": {"pong": True}}]))
        received = [ws.receive_json() for _ in opening]
        assert received == opening
        assert [row["data"].get("channel") for row in received[1:]] == ["thinking", "text"]
        assert received[-1]["data"]["text"] == "Visible while held."
        assert all(row["data"]["run_id"] == context["run_id"] for row in received)
        assert ws.receive_json() == {"id": 42, "result": {"pong": True}}
        # Only after the browser saw this run's delta may the held turn finish.
        terminal = mapped({"type": "agent_end"})
        assert [row["data"]["kind"] for row in terminal] == ["run_finished"]
        fake.inject(b"".join(json.dumps(row).encode() + b"\n"
                             for row in [*terminal, {"id": 41, "result": {"ok": True}}]))
        assert ws.receive_json() == terminal[0]
        assert ws.receive_json() == {"id": 41, "result": {"ok": True}}


def test_gwb09_pending_table_mute_daemon_is_bounded_and_releases_bridge(environment, monkeypatch):
    monkeypatch.setattr(engine_bridge, "_MAX_PENDING_REQUESTS", 3)
    monkeypatch.setattr(settings, "max_engine_bridges_per_user", 1)
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = next(iter(environment.fakes.values()))
        fake.mute = True
        baseline = len(fake.calls)
        for ident in (11, 12, 13):
            ws.send_json({"id": ident, "method": "ping"})
        fake.wait_calls(baseline + 3)
        ws.send_json({"id": 14, "method": "ping"})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4429
        assert [row["id"] for row in fake.calls[baseline:]] == [11, 12, 13]
    fake.mute = False
    with environment.client.websocket_connect("/engine") as ws:
        assert request(ws, "ping", ident=14)["id"] == 14


def test_gwb09_duplicate_pending_id_never_reaches_mute_daemon_twice(environment):
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = next(iter(environment.fakes.values()))
        fake.mute = True
        baseline = len(fake.calls)
        ws.send_json({"id": 7, "method": "ping"})
        fake.wait_calls(baseline + 1)
        ws.send_json({"id": 7, "method": "ping"})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4429
        assert len(fake.calls) == baseline + 1


def test_gwb09_correlated_replies_reclaim_pending_slots_and_allow_id_reuse(environment, monkeypatch):
    monkeypatch.setattr(engine_bridge, "_MAX_PENDING_REQUESTS", 2)
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = next(iter(environment.fakes.values()))
        fake.mute = True
        baseline = len(fake.calls)
        for ident in (7, 8):
            ws.send_json({"id": ident, "method": "ping"})
        fake.wait_calls(baseline + 2)
        fake.inject(b'{"id":8,"result":{"ok":true}}\n')
        assert ws.receive_json()["id"] == 8
        ws.send_json({"id": 8, "method": "ping"})
        fake.wait_calls(baseline + 3)
        fake.inject(b'{"id":7,"result":{"ok":true}}\n{"id":8,"result":{"ok":true}}\n')
        assert [ws.receive_json()["id"], ws.receive_json()["id"]] == [7, 8]
        assert [row["id"] for row in fake.calls[baseline:]] == [7, 8, 8]


@pytest.mark.parametrize("ident", [True, 1.0, "1", -(2**31), 2**31, None])
def test_gwb09_non_int32_ids_do_not_reserve_or_forward_pending_slots(environment, ident):
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = next(iter(environment.fakes.values()))
        baseline = len(fake.calls)
        ws.send_json({"id": ident, "method": "ping"})
        assert ws.receive_json()["error"]["code"] == -32600
        assert len(fake.calls) == baseline
        assert request(ws, "ping", ident=2**31 - 1)["id"] == 2**31 - 1
        assert request(ws, "ping", ident=-(2**31) + 1)["id"] == -(2**31) + 1


def test_garbage_unsolicited_and_disallowed_events_are_dropped(environment):
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = next(iter(environment.fakes.values()))
        fake.inject(b'garbage\n{"id":999,"result":{"secret":true}}\n'
                    b'{"event":"daemon.draining","data":{}}\n'
                    b'{"event":"doc.changed","data":{"doc_id":"foreign"}}\n')
        reply = request(ws, "ping", ident=3)
        assert reply["id"] == 3


def test_oversized_cell_reply_disconnects_without_forwarding(environment, monkeypatch):
    monkeypatch.setattr(settings, "ws_max_message_bytes", 1024)
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        next(iter(environment.fakes.values())).inject(b'{"id":99,"result":"' + b'x' * 2048 + b'"}\n')
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1009


def test_symlinked_gateway_socket_refused(environment, monkeypatch):
    principal = cells.resolve_workspace("a@example.com")
    directory = cells.prepare_cell(principal)
    foreign = settings.cells_root / "foreign.sock"
    fake = FakeDaemon(foreign)
    fake.start()
    (directory / "gw.sock").symlink_to(foreign)
    async def no_start(cell_id): pass
    monkeypatch.setattr(cells, "start_cell", no_start)
    try:
        with environment.client.websocket_connect("/engine") as ws:
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1013
        assert fake.calls == []
    finally:
        fake.stop()


def test_cell_policy_missing_refuses_instead_of_shared_daemon(environment, monkeypatch):
    monkeypatch.setattr(cells, "POLICY_PATH", environment.root / "absent.json")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        engine_bridge.parse_browser_request('{"id":1,"method":"ping"}')
    assert error.value.status_code == 503




@pytest.mark.parametrize("frame", [
    '{"id":1,"id":2,"method":"ping"}',
    '{"id":1,"method":"ping"} {}',
    '{"id":true,"method":"ping"}',
    '{"id":1.0,"method":"ping"}',
    '{"id":1,"method":"ping","params":[]}',
    '{"id":1,"method":"ping","params":{"nested":{"ctx":{}}}}',
    '{"id":1,"method":"ping","params":{"provider_grant":"forged"}}',
    '{"id":1,"method":"ping","params":{"nested":{"web_provider_grant":"forged"}}}',
    '{"id":1,"method":"ping","params":{"value":NaN}}',
    '{"id":1,"method":"ping","params":{"value":1e999}}',
    '{"id":1,"method":"ping","params":{"value":"\\ud800"}}',
])
def test_strict_browser_frames_refused(environment, frame):
    with pytest.raises(engine_bridge.InvalidRequest):
        engine_bridge.parse_browser_request(frame, right="owner")


@pytest.mark.parametrize("raw", [
    b'{"id":1,"result":{},"error":{"code":1,"message":"bad"}}\n',
    b'{"id":true,"result":{}}\n',
    b'{"id":1,"result":{"n":Infinity}}\n',
    b'{"id":1,"result":{"n":1e999}}\n',
    b'{"id":1,"jsonrpc":"wrong","result":{}}\n',
    b'{"id":1,"error":{"code":true,"message":"bad"}}\n',
    b'{"event":[],"data":{}}\n',
    b'{"event":{},"data":{}}\n',
    b'{"id":1,"id":2,"result":{}}\n',
    b'{"event":"child","data":{}}\n',
    b'{"event":"doc.changed","data":{"doc_id":42}}\n',
    b'{"event":"agent.event","data":{"type":[]}}\n',
    b'{"event":"agent.event","data":{"type":"text_delta"}}\n',
    b'{"event":"agent.event","data":{"kind":[]}}\n',
    b'{"event":"agent.event","data":{"kind":""}}\n',
    b'{"event":"agent.event","data":{"kind":"' + b'x' * 129 + b'"}}\n',
    b'{"id":1,"result":{}}',
])
def test_untrusted_cell_shape_refused(raw):
    assert engine_bridge.parse_cell_frame(raw) is None


def test_cell_activation_accepts_only_hex(environment, monkeypatch):
    from fastapi import HTTPException
    calls = []
    async def execute(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(wait=lambda: None)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", execute)
    with pytest.raises(HTTPException):
        asyncio.run(environment.activate("bad/../other.socket"))
    assert calls == []


def test_workspace_root_symlink_refused(environment):
    from fastapi import HTTPException
    root = cells.personal_root("a@example.com")
    root.parent.mkdir(parents=True, exist_ok=True)
    foreign = environment.root / "foreign"
    foreign.mkdir()
    root.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(HTTPException):
        cells.resolve_workspace("a@example.com")


def test_fifo_cell_artifact_refused_without_waiting_for_a_writer(tmp_path):
    from fastapi import HTTPException
    os.mkfifo(tmp_path / "not-a-mesh.aicm")
    with pytest.raises(HTTPException) as error:
        cells.open_beneath(tmp_path, "not-a-mesh.aicm", max_bytes=1024)
    assert error.value.status_code == 403


def test_mail_context_key_required_even_in_dev(monkeypatch):
    from gateway.config import Settings
    monkeypatch.delenv("CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="MAIL_CONTEXT_ENCRYPTION_KEY"):
        Settings(dev_mode=True, session_secret="not-a-mail-key")
    monkeypatch.setenv("CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY", "!" * 43)
    with pytest.raises(ValueError, match="MAIL_CONTEXT_ENCRYPTION_KEY"):
        Settings(dev_mode=True)


def test_repeated_disconnects_release_the_only_bridge_slot(environment, monkeypatch):
    monkeypatch.setattr(settings, "max_engine_bridges_per_user", 1)
    for ident in range(12):
        with environment.client.websocket_connect("/engine") as ws:
            assert request(ws, "ping", ident=ident)["result"]["ok"] is True
    uid = accounts.get_account("a@example.com").uid
    assert uid not in engine_bridge._bridge_owners
