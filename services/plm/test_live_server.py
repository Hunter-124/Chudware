"""Focused protocol proof for the authoritative ``aicad.plm-live/1`` relay."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from services.plm.server import MAX_MESSAGE_BYTES, EngineRejected, LivePlmServer, SCHEMA
from services.plm.vault import Vault


def _container(document: dict[str, Any]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("document.json", json.dumps(document, indent=2, sort_keys=True))
        archive.writestr("thumbnail.png", b"\x89PNG\r\n_live")
    return out.getvalue()


def _project(tmp_path: Path) -> tuple[str, str, Path]:
    path = tmp_path / "Live.step+"
    path.write_bytes(_container({
        "schema": "aicad.part/1", "id": "live-doc", "name": "Live",
        "features": [], "values": [], "settings": {},
    }))
    vault = Vault.open(tmp_path)
    try:
        current = vault.manifest()
        return current["project"]["id"], current["items"][0]["item_id"], path
    finally:
        vault.close()


def _read_document(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read("document.json"))


class _FakeEngine:
    """Atomic whole-batch engine used only to isolate relay behavior."""

    def __init__(self, item_path: Path) -> None:
        self.item_path = item_path
        self.document: dict[str, Any] = {}
        self.revision = 0
        self.apply_count = 0
        self.save_count = 0

    async def open(self) -> dict[str, Any]:
        self.document = _read_document(self.item_path)
        return self._state()

    def _state(self) -> dict[str, Any]:
        return {"document": json.loads(json.dumps(self.document)), "engine_revision": self.revision}

    async def snapshot(self) -> dict[str, Any]:
        return self._state()

    async def apply_ops(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        for op in ops:
            if op.get("op") not in {"append", "set"}:
                raise EngineRejected("invalid_op", "The operation batch is invalid.")
            if op["op"] == "set" and not isinstance(op.get("key"), str):
                raise EngineRejected("invalid_op", "The operation batch is invalid.")
        candidate = json.loads(json.dumps(self.document))
        for op in ops:
            if op["op"] == "append":
                candidate.setdefault("values", []).append(op.get("value"))
            else:
                candidate.setdefault("settings", {})[op["key"]] = op.get("value")
        self.document = candidate
        self.revision += 1
        self.apply_count += 1
        await self.save()
        return self._state()

    async def save(self) -> None:
        self.save_count += 1
        extras: dict[str, bytes] = {}
        if self.item_path.exists():
            with zipfile.ZipFile(self.item_path) as archive:
                for name in archive.namelist():
                    if name != "document.json":
                        extras[name] = archive.read(name)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("document.json", json.dumps(self.document, indent=2, sort_keys=True))
            for name, value in extras.items():
                archive.writestr(name, value)
        self.item_path.write_bytes(out.getvalue())

    async def close(self) -> None:
        return None


class _Factory:
    def __init__(self) -> None:
        self.engines: list[_FakeEngine] = []

    def __call__(self, path: Path) -> _FakeEngine:
        engine = _FakeEngine(path)
        self.engines.append(engine)
        return engine


def _participant(participant_id: str) -> dict[str, Any]:
    return {"id": participant_id, "display": participant_id.upper(), "account_id": None}


def _wire(**fields: Any) -> str:
    if fields.get("type") == "join":
        fields.setdefault("token", "test-session-token")
    return json.dumps({"schema": SCHEMA, **fields}, separators=(",", ":"))


async def _connect(url: str):
    from websockets.asyncio.client import connect
    return await connect(url, compression=None, max_size=MAX_MESSAGE_BYTES)


async def _join(url: str, project_id: str, item_id: str, participant_id: str,
                *, token: str | None = "test-session-token", last_seq: int | None = None):
    websocket = await _connect(url)
    message: dict[str, Any] = {
        "schema": SCHEMA, "type": "join", "project_id": project_id, "item_id": item_id,
        "participant": _participant(participant_id),
    }
    if token is not None:
        message["token"] = token
    if last_seq is not None:
        message["last_seq"] = last_seq
    await websocket.send(json.dumps(message))
    joined = json.loads(await websocket.recv())
    assert joined["type"] == "joined", joined
    return websocket, joined


async def _recv_type(websocket: Any, expected: str) -> dict[str, Any]:
    while True:
        message = json.loads(await asyncio.wait_for(websocket.recv(), 2.0))
        if message.get("type") == expected:
            return message


async def _leave(websocket: Any, session_id: str, *, note: str | None = None) -> None:
    message: dict[str, Any] = {"schema": SCHEMA, "type": "leave", "session_id": session_id}
    if note is not None:
        message["session_note"] = note
    await websocket.send(json.dumps(message))
    await websocket.wait_closed()


async def _wait_sessions_end(server: LivePlmServer) -> None:
    for _ in range(200):
        if not server._sessions:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("live session did not finalize")


def test_non_loopback_and_authentication_fail_closed(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)
    with pytest.raises(ValueError, match="token"):
        LivePlmServer(tmp_path, None, engine_factory=_Factory(), host="0.0.0.0")
    for token in (None, "", "   "):
        with pytest.raises(ValueError, match="token"):
            LivePlmServer(tmp_path, token, engine_factory=_Factory())

    async def scenario() -> None:
        server = LivePlmServer(tmp_path, "correct-token", engine_factory=_Factory())
        await server.start()
        try:
            from websockets.exceptions import ConnectionClosed
            websocket = await _connect(server.url)
            await websocket.send(_wire(type="join", project_id=project_id, item_id=item_id,
                                       participant=_participant("intruder"), token="wrong-token"))
            with pytest.raises(ConnectionClosed) as closed:
                await websocket.recv()
            assert closed.value.code == 4401
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_schema_identity_scope_and_message_limits_are_plain_refusals(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)

    async def scenario() -> None:
        server = LivePlmServer(tmp_path, "test-session-token", engine_factory=_Factory())
        await server.start()
        try:
            websocket = await _connect(server.url)
            await websocket.send(json.dumps({"schema": "wrong/1", "type": "join"}))
            error = json.loads(await websocket.recv())
            assert error["reason"] == "invalid_schema"
            assert error["hint"] == f"Messages must use schema {SCHEMA}."
            await websocket.wait_closed()

            websocket = await _connect(server.url)
            await websocket.send(_wire(type="join", project_id=project_id, item_id=item_id,
                                       participant={"id": "u1", "display": "", "account_id": None}))
            error = json.loads(await websocket.recv())
            assert error["reason"] == "invalid_identity"
            assert "Traceback" not in error["hint"]
            await websocket.wait_closed()

            websocket = await _connect(server.url)
            await websocket.send(_wire(type="join", project_id="p-not-hosted", item_id=item_id,
                                       participant=_participant("u1"), path=str(tmp_path / "Live.step+")))
            error = json.loads(await websocket.recv())
            assert error["reason"] == "invalid_message"
            assert str(tmp_path) not in error["hint"]
            await websocket.wait_closed()

            websocket = await _connect(server.url)
            await websocket.send(_wire(type="join", project_id="p-not-hosted", item_id=item_id,
                                       participant=_participant("u1")))
            error = json.loads(await websocket.recv())
            assert error["reason"] == "unknown_project"
            await websocket.wait_closed()

            websocket = await _connect(server.url)
            oversized = '{"schema":"' + SCHEMA + '","type":"join","pad":"' + (
                "x" * MAX_MESSAGE_BYTES) + '"}'
            await websocket.send(oversized)
            from websockets.exceptions import ConnectionClosed
            with pytest.raises(ConnectionClosed) as closed:
                await websocket.recv()
            assert closed.value.code == 1009
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_operation_limits_reject_before_engine_and_are_idempotent(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)
    factory = _Factory()

    async def scenario() -> None:
        server = LivePlmServer(tmp_path, "test-session-token", engine_factory=factory)
        await server.start()
        try:
            websocket, joined = await _join(server.url, project_id, item_id, "u1")
            proposal = {"schema": SCHEMA, "type": "propose", "session_id": joined["session_id"],
                        "client_op_id": "too-many", "base_seq": 0,
                        "ops": [{"op": "append", "value": i} for i in range(1001)]}
            await websocket.send(json.dumps(proposal))
            first = json.loads(await websocket.recv())
            assert first["reason"] == "batch_too_large" and first["head_seq"] == 0
            await websocket.send(json.dumps(proposal))
            assert json.loads(await websocket.recv()) == first
            assert factory.engines[0].apply_count == 0
            await _leave(websocket, joined["session_id"])
            await _wait_sessions_end(server)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_interleaved_clients_receive_identical_total_order(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)

    async def scenario() -> None:
        server = LivePlmServer(tmp_path, "test-session-token", engine_factory=_Factory())
        await server.start()
        try:
            first, joined_a = await _join(server.url, project_id, item_id, "alice")
            second, joined_b = await _join(server.url, project_id, item_id, "bob")
            assert joined_a["session_id"] == joined_b["session_id"]
            sid = joined_a["session_id"]
            await asyncio.gather(
                first.send(_wire(type="propose", session_id=sid, client_op_id="alice-1",
                                 base_seq=0, ops=[{"op": "append", "value": "A"}])),
                second.send(_wire(type="propose", session_id=sid, client_op_id="bob-1",
                                  base_seq=0, ops=[{"op": "append", "value": "B"}])),
            )
            seen_a = [await _recv_type(first, "commit"), await _recv_type(first, "commit")]
            seen_b = [await _recv_type(second, "commit"), await _recv_type(second, "commit")]
            assert [item["seq"] for item in seen_a] == [1, 2]
            assert [item["seq"] for item in seen_b] == [1, 2]
            assert [(x["seq"], x["state_hash"], x["snapshot"]) for x in seen_a] == [
                (x["seq"], x["state_hash"], x["snapshot"]) for x in seen_b]
            assert len(seen_a[0]["snapshot"]["document"]["values"]) == 1
            assert len(seen_a[1]["snapshot"]["document"]["values"]) == 2
            await asyncio.gather(_leave(first, sid), _leave(second, sid))
            await _wait_sessions_end(server)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_duplicate_proposal_replays_original_without_second_apply(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)
    factory = _Factory()

    async def scenario() -> None:
        server = LivePlmServer(tmp_path, "test-session-token", engine_factory=factory)
        await server.start()
        try:
            websocket, joined = await _join(server.url, project_id, item_id, "alice")
            proposal = _wire(type="propose", session_id=joined["session_id"],
                             client_op_id="same-id", base_seq=0,
                             ops=[{"op": "append", "value": 1}])
            await websocket.send(proposal)
            committed = await _recv_type(websocket, "commit")
            await websocket.send(proposal)
            assert await _recv_type(websocket, "commit") == committed
            assert committed["seq"] == 1 and factory.engines[0].apply_count == 1
            await _leave(websocket, joined["session_id"])
            await _wait_sessions_end(server)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_late_join_and_atomic_reconnect_catch_up_contiguously(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)

    async def scenario() -> None:
        server = LivePlmServer(tmp_path, "test-session-token", engine_factory=_Factory())
        await server.start()
        try:
            alice_old, joined = await _join(server.url, project_id, item_id, "alice")
            sid = joined["session_id"]
            await alice_old.send(_wire(type="propose", session_id=sid, client_op_id="a1",
                                       base_seq=0, ops=[{"op": "append", "value": "one"}]))
            commit_one = await _recv_type(alice_old, "commit")
            bob, late = await _join(server.url, project_id, item_id, "bob")
            assert late["head_seq"] == 1
            assert late["snapshot"]["document"] == commit_one["snapshot"]["document"]
            await bob.send(_wire(type="propose", session_id=sid, client_op_id="b1",
                                 base_seq=1, ops=[{"op": "append", "value": "two"}]))
            assert await _recv_type(alice_old, "commit") == await _recv_type(bob, "commit")

            alice_new, resumed = await _join(server.url, project_id, item_id, "alice", last_seq=1)
            assert resumed["head_seq"] == 2 and "snapshot" not in resumed
            catchup = await _recv_type(alice_new, "commit")
            assert catchup["seq"] == 2 and "prior_state_hash" not in catchup
            await alice_old.wait_closed()
            assert alice_old.close_code == 4001
            await alice_new.send(_wire(type="catchup", session_id=sid, from_seq=2,
                                       state_hash="sha256:" + "0" * 64))
            snapshot = await _recv_type(alice_new, "snapshot")
            assert snapshot["head_seq"] == 2 and snapshot["state_hash"] == catchup["state_hash"]
            await asyncio.gather(_leave(alice_new, sid), _leave(bob, sid))
            await _wait_sessions_end(server)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_presence_leave_and_no_partial_snapshot(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)

    async def scenario() -> None:
        server = LivePlmServer(tmp_path, "test-session-token", engine_factory=_Factory())
        await server.start()
        try:
            alice, joined = await _join(server.url, project_id, item_id, "alice")
            bob, _ = await _join(server.url, project_id, item_id, "bob")
            sid = joined["session_id"]
            await alice.send(_wire(type="presence", session_id=sid,
                                   cursor={"space": "viewport", "x": 1.0},
                                   future_field={"safe": True}))
            presence = await _recv_type(bob, "presence")
            assert presence["participant"] == _participant("alice")
            assert presence["future_field"] == {"safe": True}

            await alice.send(_wire(type="propose", session_id=sid, client_op_id="atomic-fail",
                                   base_seq=0, ops=[{"op": "append", "value": "bad"}, {"op": "fail"}]))
            rejected = await _recv_type(alice, "rejected")
            assert rejected["head_seq"] == 0
            await alice.send(_wire(type="propose", session_id=sid, client_op_id="after",
                                   base_seq=0, ops=[{"op": "append", "value": "only-value"}]))
            committed_alice = await _recv_type(alice, "commit")
            committed_bob = await _recv_type(bob, "commit")
            assert committed_alice == committed_bob
            assert committed_alice["snapshot"]["document"]["values"] == ["only-value"]

            await _leave(alice, sid)
            left = await _recv_type(bob, "presence_left")
            assert left["participant_id"] == "alice"
            await _leave(bob, sid)
            await _wait_sessions_end(server)
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_concurrent_last_leave_writes_one_attributed_checkpoint(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)

    async def scenario() -> None:
        server = LivePlmServer(tmp_path, "test-session-token", engine_factory=_Factory())
        await server.start()
        try:
            alice, joined = await _join(server.url, project_id, item_id, "alice")
            bob, _ = await _join(server.url, project_id, item_id, "bob")
            sid = joined["session_id"]
            for websocket, op_id, value, base in ((alice, "a", "a", 0), (bob, "b", "b", 1)):
                await websocket.send(_wire(type="propose", session_id=sid, client_op_id=op_id,
                                           base_seq=base, ops=[{"op": "append", "value": value}]))
                await _recv_type(alice, "commit")
                await _recv_type(bob, "commit")
            await asyncio.gather(_leave(alice, sid, note="Reviewed together"),
                                 _leave(bob, sid, note="Reviewed together"))
            await _wait_sessions_end(server)
        finally:
            await server.stop()

        vault = Vault.open(tmp_path)
        try:
            checkpoints = [x for x in vault.history(item_id=item_id) if x["kind"] == "checkpoint"]
            assert len(checkpoints) == 1
            assert checkpoints[0]["message"] == "Reviewed together"
            assert {x["id"] for x in checkpoints[0]["participants"]} == {"alice", "bob"}
        finally:
            vault.close()

    asyncio.run(scenario())


def test_empty_session_writes_no_checkpoint(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)

    async def scenario() -> None:
        server = LivePlmServer(tmp_path, "test-session-token", engine_factory=_Factory())
        await server.start()
        websocket, joined = await _join(server.url, project_id, item_id, "observer")
        await _leave(websocket, joined["session_id"])
        await _wait_sessions_end(server)
        await server.stop()
        vault = Vault.open(tmp_path)
        try:
            assert not [x for x in vault.history(item_id=item_id) if x["kind"] == "checkpoint"]
        finally:
            vault.close()

    asyncio.run(scenario())


def test_real_engine_op_log_rebuilds_to_identical_geometry(tmp_path: Path) -> None:
    from services.mcp import core_bridge as cb
    if not cb.available():
        pytest.skip("libchudware core is unavailable")

    async def scenario() -> None:
        stem = "plm_live_" + uuid.uuid4().hex[:10]
        source = cb.call("doc_new", {"name": stem})["doc_id"]
        item_path = tmp_path / f"{stem}.step+"
        cb.call("doc_save", {"doc_id": source, "path": str(item_path)})
        with contextlib.suppress(Exception):
            cb.call("doc_close", {"doc_id": source})
        vault = Vault.open(tmp_path)
        try:
            current = vault.manifest()
            project_id, item_id = current["project"]["id"], current["items"][0]["item_id"]
        finally:
            vault.close()
        ops = [{"op": "add_feature", "feature": {"type": "box", "name": "Block",
                "params": {"length": {"value": 30.0, "expr": None},
                           "width": {"value": 20.0, "expr": None},
                           "height": {"value": 10.0, "expr": None}}, "refs": {}}}]
        server = LivePlmServer(tmp_path, "test-session-token")
        await server.start()
        try:
            websocket, joined = await _join(server.url, project_id, item_id, "engine-user")
            await websocket.send(_wire(type="propose", session_id=joined["session_id"],
                                       client_op_id="real-1", base_seq=0, ops=ops))
            assert (await _recv_type(websocket, "commit"))["seq"] == 1
            await _leave(websocket, joined["session_id"])
            await _wait_sessions_end(server)
        finally:
            await server.stop()

        rebuilt: list[str] = []
        try:
            for suffix in ("a", "b"):
                doc_id = cb.call("doc_new", {"name": f"{stem}_{suffix}"})["doc_id"]
                rebuilt.append(doc_id)
                result = cb.call("doc_apply_ops", {"doc_id": doc_id, "base_revision": 0,
                                                    "with_tess": False, "ops": ops})
                assert result["revision"] >= 1
            compared = cb.call("eval_compare", {"doc_a": rebuilt[0], "doc_b": rebuilt[1]})
            assert compared.get("ok") is True, compared
            assert compared.get("identical") is True or compared.get("match_pct", 0.0) >= 99.999
        finally:
            for doc_id in rebuilt:
                with contextlib.suppress(Exception):
                    cb.call("doc_close", {"doc_id": doc_id})

    asyncio.run(scenario())


def test_loopback_origin_policy_applies_before_join(tmp_path: Path) -> None:
    project_id, item_id, _path = _project(tmp_path)

    async def scenario() -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus, ConnectionClosed
        server = LivePlmServer(tmp_path, "test-session-token", engine_factory=_Factory(),
                               allowed_origins=("https://cad.example.invalid",))
        await server.start()
        try:
            for origin in ("https://attacker.invalid", "null", "http://localhost:9999"):
                with pytest.raises(InvalidStatus) as refused:
                    async with connect(server.url, origin=origin):
                        pass
                assert refused.value.response.status_code == 403
            async with connect(server.url, origin="https://cad.example.invalid") as websocket:
                await websocket.send(_wire(type="join", project_id=project_id, item_id=item_id,
                                           participant=_participant("intruder"), token=""))
                with pytest.raises(ConnectionClosed) as refused:
                    await websocket.recv()
                assert refused.value.code == 4401
            async with connect(server.url, origin="https://cad.example.invalid") as websocket:
                await websocket.send(_wire(type="join", project_id=project_id, item_id=item_id,
                                           participant=_participant("approved")))
                assert json.loads(await websocket.recv())["type"] == "joined"
        finally:
            await server.stop()

    asyncio.run(scenario())
