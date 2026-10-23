"""WS /engine against a fake cell hostd: principal hello, admission, bounds, filtering."""
from __future__ import annotations

import asyncio
import json
import os
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect

from gateway import cells, engine_bridge
from gateway.config import settings


class FakeDaemon:
    """A cell hostd stand-in listening on the gateway socket path."""

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
            self.ready.set()
            return server

        server = self.loop.run_until_complete(boot())
        try:
            self.loop.run_forever()
        finally:
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
                if self.mute and request["id"] != engine_bridge._INTERNAL_HELLO:
                    continue
                result = ({"doc_id": "doc-1", "aicm_path": "/state/mesh/" + "a" * 32 + ".aicm"}
                          if request["method"] == "doc_open" else {"ok": True, "proto": 1})
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
def environment(client, gateway_home, tmp_path, monkeypatch):
    policy = tmp_path / "methods.json"
    policy.write_text(json.dumps({name: {"cell": "allow", "right": right, "scope": "workspace"}
                                 for name, right in (("hello", "read"), ("ping", "read"),
                                                      ("doc_open", "read"), ("doc_save", "write"))}))
    monkeypatch.setattr(cells, "POLICY_PATH", policy)
    fakes = {}
    activate = cells.start_cell

    async def start(cell_id):
        if cell_id not in fakes:
            fake = FakeDaemon(settings.cells_root / cell_id / "gw.sock")
            fake.start()
            fakes[cell_id] = fake

    monkeypatch.setattr(cells, "start_cell", start)
    yield SimpleNamespace(client=client, fakes=fakes, root=tmp_path, policy=policy, activate=activate)
    for fake in fakes.values():
        fake.stop()
    engine_bridge._mesh_grants.clear()


def request(ws, method, ident=1, params=None):
    ws.send_json({"id": ident, "method": method, "params": params or {}})
    return ws.receive_json()


def only_fake(environment):
    (fake,) = environment.fakes.values()
    return fake


def test_operator_principal_hello_and_cell_identity(environment, workspace):
    with environment.client.websocket_connect("/engine") as ws:
        result = request(ws, "hello")["result"]
    principal = cells.resolve_workspace()
    assert result["web_rpc_version"] == 2
    assert result["workspace_key"] == "user:" + settings.operator
    assert result["web_rpc_methods"] == ["doc_open", "doc_save", "hello", "ping"]
    fake = environment.fakes[principal.cell_id]
    hello = fake.calls[0]
    assert hello["id"] == engine_bridge._INTERNAL_HELLO
    assert {k: v for k, v in hello["params"]["principal"].items() if k != "connection_nonce"} == {
        "workspace_key": principal.workspace_key, "actor_key": settings.operator,
        "right": "owner", "epoch": 0}
    # The browser's own hello is rewritten to the trusted principal.
    assert fake.calls[1]["params"]["principal"]["actor_key"] == settings.operator
    cell_json = json.loads((settings.cells_root / principal.cell_id / "cell.json").read_text())
    assert cell_json == {"schema": "chudware-cell-v1", "cell_id": principal.cell_id,
                         "workspace_key": principal.workspace_key, "root": str(workspace)}


def test_cell_configuration_mismatch_is_refused(environment):
    principal = cells.resolve_workspace()
    directory = cells.prepare_cell(principal)
    config = directory / "cell.json"
    config.write_text(json.dumps({**json.loads(config.read_text()), "root": "/elsewhere"}))
    with pytest.raises(HTTPException) as error:
        cells.prepare_cell(principal)
    assert error.value.status_code == 403


def test_unknown_and_forged_identity_requests_never_reach_cell(environment):
    with environment.client.websocket_connect("/engine") as ws:
        assert request(ws, "future_unknown")["error"]["code"] == "cell_refused"
        assert request(ws, "ping", params={"principal": {"right": "owner"}})["error"]["code"] == -32700
        assert request(ws, "ping", ident=2)["result"]["ok"] is True
    assert not any(c["method"] == "future_unknown" for c in only_fake(environment).calls)


def test_viewer_right_cannot_write():
    policy = cells.method_policy()
    assert policy["doc_save"]["right"] == "write"
    with pytest.raises(engine_bridge.InvalidRequest) as error:
        engine_bridge.parse_browser_request('{"id":1,"method":"doc_save"}', right="viewer")
    assert error.value.code == "forbidden_right"


def test_refused_policy_entry_is_not_forwarded(environment):
    policy = json.loads(environment.policy.read_text())
    policy["agent_spawn"] = {"cell": "refuse", "right": "run", "scope": "actor"}
    environment.policy.write_text(json.dumps(policy))
    with environment.client.websocket_connect("/engine") as ws:
        assert request(ws, "agent_spawn")["error"]["code"] == "cell_refused"
    assert all(c["method"] != "agent_spawn" for c in only_fake(environment).calls)


def test_document_events_and_mesh_paths_are_scoped_to_opened_documents(environment):
    with environment.client.websocket_connect("/engine") as ws:
        opened = request(ws, "doc_open", params={"path": "part.step+"})["result"]
        principal = cells.resolve_workspace()
        reference = json.loads(opened["aicm_path"].removeprefix("aicm-cell:"))
        assert reference == {"workspace_key": principal.workspace_key, "doc_id": "doc-1",
                             "path": "a" * 32 + ".aicm"}
        fake = only_fake(environment)
        fake.inject(b'{"event":"doc.changed","data":{"doc_id":"foreign"}}\n'
                    b'{"event":"doc.changed","data":{"doc_id":"doc-1","aicm_path":"/etc/passwd"}}\n')
        event = ws.receive_json()
        assert event["data"]["doc_id"] == "doc-1"
        assert event["data"]["aicm_path"] == ""


def test_foreign_actor_events_are_filtered(environment):
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        foreign = {"event": "agent.event", "data": {"kind": "assistant_delta", "actor_key": "someone-else",
                                                    "text": "Must not reach this browser."}}
        own = {"event": "agent.event", "data": {"kind": "assistant_delta", "actor_key": settings.operator,
                                                "text": "Visible."}}
        only_fake(environment).inject(b"".join(json.dumps(row).encode() + b"\n" for row in (foreign, own)))
        assert ws.receive_json() == own


def test_pending_table_with_mute_daemon_is_bounded_and_releases_bridge(environment, monkeypatch):
    monkeypatch.setattr(engine_bridge, "_MAX_PENDING_REQUESTS", 3)
    monkeypatch.setattr(settings, "max_engine_bridges", 1)
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = only_fake(environment)
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


def test_duplicate_pending_id_never_reaches_mute_daemon_twice(environment):
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = only_fake(environment)
        fake.mute = True
        baseline = len(fake.calls)
        ws.send_json({"id": 7, "method": "ping"})
        fake.wait_calls(baseline + 1)
        ws.send_json({"id": 7, "method": "ping"})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4429
        assert len(fake.calls) == baseline + 1


def test_correlated_replies_reclaim_pending_slots_and_allow_id_reuse(environment, monkeypatch):
    monkeypatch.setattr(engine_bridge, "_MAX_PENDING_REQUESTS", 2)
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = only_fake(environment)
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
def test_non_int32_ids_do_not_reserve_or_forward_pending_slots(environment, ident):
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        fake = only_fake(environment)
        baseline = len(fake.calls)
        ws.send_json({"id": ident, "method": "ping"})
        assert ws.receive_json()["error"]["code"] == -32600
        assert len(fake.calls) == baseline
        assert request(ws, "ping", ident=2**31 - 1)["id"] == 2**31 - 1
        assert request(ws, "ping", ident=-(2**31) + 1)["id"] == -(2**31) + 1


def test_garbage_unsolicited_and_disallowed_events_are_dropped(environment):
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        only_fake(environment).inject(b'garbage\n{"id":999,"result":{"secret":true}}\n'
                                      b'{"event":"daemon.draining","data":{}}\n'
                                      b'{"event":"doc.changed","data":{"doc_id":"foreign"}}\n')
        assert request(ws, "ping", ident=3)["id"] == 3


def test_oversized_cell_reply_disconnects_without_forwarding(environment, monkeypatch):
    monkeypatch.setattr(settings, "ws_max_message_bytes", 1024)
    with environment.client.websocket_connect("/engine") as ws:
        request(ws, "hello")
        only_fake(environment).inject(b'{"id":99,"result":"' + b"x" * 2048 + b'"}\n')
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1009


def test_symlinked_gateway_socket_refused(environment, monkeypatch):
    principal = cells.resolve_workspace()
    directory = cells.prepare_cell(principal)
    foreign = settings.cells_root / "foreign.sock"
    fake = FakeDaemon(foreign)
    fake.start()
    (directory / "gw.sock").symlink_to(foreign)

    async def no_start(cell_id):
        pass

    monkeypatch.setattr(cells, "start_cell", no_start)
    try:
        with environment.client.websocket_connect("/engine") as ws:
            with pytest.raises(WebSocketDisconnect) as error:
                ws.receive_json()
            assert error.value.code == 1013
        assert fake.calls == []
    finally:
        fake.stop()


def test_cell_policy_missing_refuses_instead_of_guessing(environment, monkeypatch):
    monkeypatch.setattr(cells, "POLICY_PATH", environment.root / "absent.json")
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
    '{"id":1,"method":"ping","params":{"nested":{"_chudware_actor":{}}}}',
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
    b'{"event":"agent.event","data":{"type":"text_delta"}}\n',
    b'{"event":"agent.event","data":{"kind":""}}\n',
    b'{"event":"agent.event","data":{"kind":"' + b"x" * 129 + b'"}}\n',
    b'{"id":1,"result":{}}',
])
def test_untrusted_cell_shape_refused(raw):
    assert engine_bridge.parse_cell_frame(raw) is None


def test_cell_activation_accepts_only_hex(environment, monkeypatch):
    calls = []

    async def execute(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(wait=lambda: None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", execute)
    with pytest.raises(HTTPException):
        asyncio.run(environment.activate("bad/../other.socket"))
    assert calls == []


def test_workspace_root_symlink_refused(gateway_home, monkeypatch):
    foreign = gateway_home / "foreign"
    foreign.mkdir()
    link = gateway_home / "linked-workspace"
    link.symlink_to(foreign, target_is_directory=True)
    monkeypatch.setattr(settings, "workspace_root", link)
    with pytest.raises(HTTPException):
        cells.resolve_workspace()


def test_fifo_cell_artifact_refused_without_waiting_for_a_writer(tmp_path):
    os.mkfifo(tmp_path / "not-a-mesh.aicm")
    with pytest.raises(HTTPException) as error:
        cells.open_beneath(tmp_path, "not-a-mesh.aicm", max_bytes=1024)
    assert error.value.status_code == 403


def test_repeated_disconnects_release_the_only_bridge_slot(environment, monkeypatch):
    monkeypatch.setattr(settings, "max_engine_bridges", 1)
    for ident in range(12):
        with environment.client.websocket_connect("/engine") as ws:
            assert request(ws, "ping", ident=ident)["result"]["ok"] is True
    assert engine_bridge.active_bridge_count() == 0
