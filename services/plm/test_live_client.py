"""Focused contract tests for the aicad.plm-live/1 Python client seams."""
from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path

import pytest

from services.plm import live_client, manifest, remote, worker_rpc
from services.plm.live_client import LiveClient, LiveClientError, LiveClientManager, state_hash
from services.plm.vault import Vault

PARTICIPANT = {"id": "u-sarah", "display": "Sarah", "account_id": None}
PROJECT_ID, ITEM_ID, SESSION_ID = "p-project", "i-item", "s-session"


class FakeSocket:
    def __init__(self, incoming=()):
        self.incoming = deque(json.dumps(value) for value in incoming)
        self.sent: list[dict] = []
        self._wake = asyncio.Event()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if self.incoming:
            return self.incoming.popleft()
        await self._wake.wait()
        raise RuntimeError("closed")

    async def close(self) -> None:
        self._wake.set()


def _document(value: int = 0) -> dict:
    return {"schema": "aicad.part/1", "name": "Part", "features": [], "value": value}


def _joined(document: dict | None = None, head_seq: int = 0) -> dict:
    result = {"schema": live_client.SCHEMA, "type": "joined", "session_id": SESSION_ID,
              "project_id": PROJECT_ID, "item_id": ITEM_ID, "head_seq": head_seq,
              "engine_revision": head_seq, "participants": [PARTICIPANT],
              "checkpoint_base": "checkpoint-1"}
    if document is not None:
        result["snapshot"] = {"encoding": "canonical-json", "document": document}
    return result


def _commit(seq: int, document: dict, client_op_id: str | None = None) -> dict:
    return {"schema": live_client.SCHEMA, "type": "commit", "session_id": SESSION_ID,
            "seq": seq, "client_op_id": client_op_id or f"op-{seq}",
            "author": PARTICIPANT, "engine_revision": seq,
            "ops": [{"op": "set_value", "value": document["value"]}],
            "snapshot": {"encoding": "canonical-json", "document": document},
            "state_hash": state_hash(document)}


def _primed(*, apply_ops=None, install_snapshot=None) -> LiveClient:
    client = LiveClient("ws://127.0.0.1:8765/live", PROJECT_ID, ITEM_ID,
                        PARTICIPANT, "token", apply_ops=apply_ops,
                        install_snapshot=install_snapshot)
    client._session_id = SESSION_ID
    client._ws = FakeSocket()
    client._ready.set()
    return client


def test_profile_mode_selection_is_kind_and_capability_driven() -> None:
    assert manifest.remote_mode({"kind": "plm-server", "capabilities": ["live_coedit"]}) == "live"
    assert manifest.remote_mode({"kind": "plm-server", "capabilities": ["snapshot_sync"]}) == "snapshot"
    assert manifest.remote_mode({"kind": "folder", "url": "wss://live", "capabilities": ["live_coedit"]}) == "snapshot"


def test_live_remote_normalizes_preserves_identity_and_stores_no_secret(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CHUDWARE_PLM_USER_ID", "u-owner")
    monkeypatch.setenv("CHUDWARE_PLM_LIVE_TOKEN", "must-not-persist")
    first = remote.add_remote(tmp_path, "workshop", kind="plm-server",
                              url="https://PLM.Example.Test/live", display_name="Sarah")
    assert first["remote"]["url"] == "wss://plm.example.test/live"
    assert first["remote"]["capabilities"] == ["snapshot_sync", "live_coedit", "presence"]
    identity = dict(first["remote"]["identity"])
    updated = remote.add_remote(tmp_path, "workshop", kind="plm-server",
                                url="plm.example.test/live")
    assert updated["remote"]["identity"] == identity
    assert "token" not in updated["remote"]
    v = Vault.open(tmp_path)
    try:
        assert "must-not-persist" not in json.dumps(v.manifest())
    finally:
        v.close()


def test_live_remote_sync_and_lock_return_plain_join_hint(tmp_path: Path) -> None:
    assert remote.add_remote(tmp_path, "live", kind="plm-server",
                             url="ws://127.0.0.1:8765/live")["ok"]
    expected = {"ok": False, "reason": "live_join_required",
                "hint": "This remote uses live editing; join the item with plm_live_join instead."}
    assert remote.sync(tmp_path, "live") == expected
    assert remote.list_locks(tmp_path, "live") == expected
    assert remote.plm_lock(tmp_path, "item", "live") == expected


def test_dumb_folder_profile_shape_is_unchanged(tmp_path: Path) -> None:
    profile = remote.add_remote(tmp_path, "folder", path=tmp_path / "shared")["remote"]
    assert profile["kind"] == "folder"
    assert profile["capabilities"] == ["snapshot_sync", "advisory_locks"]
    assert profile["url"] == str(tmp_path / "shared")


def test_explicit_and_environment_token_handling(monkeypatch) -> None:
    async def scenario(token):
        socket = FakeSocket([_joined(_document())])
        client = LiveClient("ws://127.0.0.1:8765/live", PROJECT_ID, ITEM_ID,
                            PARTICIPANT, token, connect_factory=lambda *_a, **_k: socket)
        await client.join()
        sent = socket.sent[0]["token"]
        await client.leave()
        return sent
    monkeypatch.setenv("CHUDWARE_PLM_LIVE_TOKEN", "environment-secret")
    assert asyncio.run(scenario("explicit-secret")) == "explicit-secret"
    assert asyncio.run(scenario(None)) == "environment-secret"


def test_ordered_poll_contains_only_whole_applied_commits() -> None:
    applied = []
    async def scenario():
        client = _primed(apply_ops=lambda ops: applied.append(ops) or {"revision": len(applied)})
        await client._handle(_commit(1, _document(1)))
        await client._handle(_commit(2, _document(2)))
        return await client.poll()
    result = asyncio.run(scenario())
    assert [event["seq"] for event in result["events"]] == [1, 2]
    assert [event["ops_response"]["revision"] for event in result["events"]] == [1, 2]
    assert [batch[0]["value"] for batch in applied] == [1, 2]


def test_gap_stops_application_until_contiguous_catchup() -> None:
    applied = []
    async def scenario():
        client = _primed(apply_ops=lambda ops: applied.append(ops[0]["value"]) or {})
        gap = _commit(2, _document(2))
        await client._handle(gap)
        assert applied == []
        assert client._ws.sent[-1]["type"] == "catchup"
        await client._handle(_commit(1, _document(1)))
        await client._handle(gap)
        return await client.poll()
    result = asyncio.run(scenario())
    assert applied == [1, 2]
    assert [event["seq"] for event in result["events"]] == [1, 2]


def test_reconnect_duplicate_commit_is_not_applied_twice() -> None:
    applied = []
    async def scenario():
        client = _primed(apply_ops=lambda ops: applied.append(ops[0]["value"]) or {})
        message = _commit(1, _document(1))
        await client._handle(message)
        await client._handle(message)
        return await client.poll()
    result = asyncio.run(scenario())
    assert applied == [1]
    assert [event["seq"] for event in result["events"]] == [1]


def test_identical_reconnect_snapshot_is_not_installed_twice() -> None:
    installed = []
    async def scenario():
        client = _primed(install_snapshot=lambda _doc, fingerprint: installed.append(fingerprint))
        await client._handle_joined(_joined(_document(3), 3))
        first = await client.poll()
        await client._handle_joined(_joined(_document(3), 3))
        return first, await client.poll()
    first, second = asyncio.run(scenario())
    assert len(installed) == 1
    assert first["events"][0]["type"] == "snapshot"
    assert second["events"] == []


def test_propose_is_idempotent_and_returns_ops_response() -> None:
    async def scenario():
        client = _primed(apply_ops=lambda _ops: {"revision": 11, "tree": {}})
        first = asyncio.create_task(client.propose([{"op": "set_value", "value": 1}], "same-id"))
        await asyncio.sleep(0)
        retry = asyncio.create_task(client.propose([{"op": "set_value", "value": 1}], "same-id"))
        await asyncio.sleep(0)
        await client._handle(_commit(1, _document(1), "same-id"))
        return await first, await retry
    first, retry = asyncio.run(scenario())
    assert first == retry
    assert first["ops_response"]["revision"] == 11


def test_participant_presence_shapes_and_bound() -> None:
    async def scenario():
        client = _primed()
        await client._handle({"schema": live_client.SCHEMA, "type": "presence",
                              "session_id": SESSION_ID, "participant": PARTICIPANT,
                              "selection": {"kind": "face", "selector": "face:top"}})
        result = await client.presence(cursor={"space": "viewport", "x": 0.0,
                                               "y": 0.0, "z": 0.0})
        return client, result
    client, result = asyncio.run(scenario())
    assert client._ws.sent[-1]["cursor"]["space"] == "viewport"
    assert result["participants"][0]["participant"] == PARTICIPANT
    with pytest.raises(LiveClientError, match="64 KiB"):
        asyncio.run(client.presence(selection={"selector": "x" * (65 * 1024)}))


def test_invalid_schema_session_and_hash_fail_closed() -> None:
    async def run(message):
        await _primed()._handle(message)
    bad = _commit(1, _document(1)); bad["schema"] = "aicad.plm-live/2"
    with pytest.raises(LiveClientError) as error:
        asyncio.run(run(bad))
    assert error.value.reason == "invalid_schema"
    bad = _commit(1, _document(1)); bad["session_id"] = "other"
    with pytest.raises(LiveClientError) as error:
        asyncio.run(run(bad))
    assert error.value.reason == "wrong_session"
    bad = _commit(1, _document(1)); bad["state_hash"] = "sha256:" + "0" * 64
    with pytest.raises(LiveClientError) as error:
        asyncio.run(run(bad))
    assert error.value.reason == "hash_mismatch"


def test_manager_lifecycle_and_local_doc_application(monkeypatch) -> None:
    calls = []
    document = _document()
    project_manifest = {"project": {"id": PROJECT_ID},
                        "items": [{"item_id": ITEM_ID, "path": "Part.step+"}]}
    profile = {"name": "live", "kind": "plm-server", "url": "ws://127.0.0.1/live",
               "capabilities": ["live_coedit"],
               "identity": {"user_id": PARTICIPANT["id"],
                            "display": PARTICIPANT["display"], "account_id": None}}
    class FakeClient:
        def __init__(self, _url, _project, _item, participant, _token, **kwargs):
            self.participant, self.session_id, self.last_seq = participant, SESSION_ID, 0
            self.install, self.apply = kwargs["install_snapshot"], kwargs["apply_ops"]
        async def join(self):
            installed = await self.install(document, state_hash(document))
            response = await self.apply([{"op": "set_value", "value": 1}])
            return {"ok": True, "session_id": SESSION_ID, **installed,
                    "ops_response": response}
        async def leave(self):
            return {"ok": True, "left": True}
    def core_call(method, params):
        calls.append((method, params))
        if method == "attach_doc": return {"ok": True}
        if method == "doc_snapshot": return {"document": document}
        if method == "doc_apply_ops": return {"revision": 1, "tree": {}}
        raise AssertionError(method)
    monkeypatch.setattr(LiveClientManager, "_profile", staticmethod(
        lambda *_args: (project_manifest, profile, profile["url"])))
    manager = LiveClientManager(client_factory=FakeClient, core_call=core_call)
    try:
        result = manager.join("/project", "live", ITEM_ID, PARTICIPANT,
                              local_doc_id="doc-local")
        assert result["local_doc_id"] == "doc-local"
        assert calls[0] == ("attach_doc", {"doc_id": "doc-local", "mode": "rw"})
        assert calls[1] == ("doc_snapshot", {"doc_id": "doc-local"})
        assert calls[2][0] == "doc_apply_ops"
        assert manager.running
    finally:
        manager.stop()
    assert not manager.running


def test_worker_and_mcp_parameter_plumbing_is_exact(monkeypatch) -> None:
    recorded = []
    class FakeManager:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                recorded.append((name, args, kwargs))
                return {"ok": True, "method": name}
            return call
    monkeypatch.setattr(live_client, "get_live_manager", lambda: FakeManager())
    params = {"path": "/project", "remote_id": "live", "item_id": ITEM_ID,
              "participant": PARTICIPANT, "token": "secret", "local_doc_id": "doc-1"}
    assert worker_rpc.WORKER_METHODS["plm_live_join"](params, lambda *_: None)["ok"]
    assert recorded.pop(0) == ("join", ("/project", "live", ITEM_ID, PARTICIPANT),
                               {"token": "secret", "local_doc_id": "doc-1"})
    from services.mcp.plm_tools import register_plm_tools
    captured = {}
    class Capture:
        def tool(self, *args, **kwargs):
            def decorate(fn): captured[fn.__name__] = fn; return fn
            return decorate
    register_plm_tools(Capture())
    result = json.loads(captured["plm_live_propose"](
        path="/project", remote_id="live", item_id=ITEM_ID,
        participant=PARTICIPANT, ops=[{"op": "x"}], client_op_id="op-1"))
    assert result == {"ok": True, "method": "propose"}
    assert recorded.pop(0) == ("propose", ("/project", "live", ITEM_ID,
                                           PARTICIPANT, [{"op": "x"}], "op-1"), {})


def test_manager_stop_without_start_is_idempotent() -> None:
    manager = LiveClientManager()
    manager.stop(); manager.stop()
    assert not manager.running
