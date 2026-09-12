"""daemon_bridge tests — the MCP side of the shared engine-host daemon.

Native integration tests use a scratch CHUDWARE_INSTANCE_KEY and skip without a
built hostd. Protocol regression tests use an in-memory peer and need no engine.
"""
from __future__ import annotations

import json
import os
import signal
import time
import uuid

import pytest

from services.mcp import daemon_bridge
from services.mcp.daemon_bridge import DaemonClient, DaemonError, DaemonSessionLost

HOSTD = daemon_bridge.resolve_hostd_bin()



@pytest.fixture()
def scratch_daemon(monkeypatch):
    """Fresh instance key + fast linger; kills the daemon on teardown."""
    if HOSTD is None:
        pytest.skip("chudware-hostd not built")
    key = "pytest-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("CHUDWARE_INSTANCE_KEY", key)
    monkeypatch.setenv("CHUDWARE_HOSTD_LINGER_SECS", "2")
    monkeypatch.delenv("CHUDWARE_HOSTD_SOCKET", raising=False)
    pids = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _pid(c: DaemonClient) -> int:
    return c.call("daemon_status")["pid"]


def test_connect_spawns_and_serves(scratch_daemon):
    c = DaemonClient()
    c.connect()
    assert c.server_info.get("proto") == 1
    scratch_daemon.append(_pid(c))
    r = c.call("doc_new", {"name": "bridge-test"})
    assert "doc_id" in r
    c.close()


def test_two_clients_share_one_engine(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    assert _pid(a) == _pid(b), "both clients must land on the same daemon"
    doc = a.call("doc_new", {"name": "shared"})["doc_id"]
    docs = b.call("doc_list", {})
    listed = [d["doc_id"] for d in (docs.get("docs") or [])]
    assert doc in listed, "doc created by client A must be visible to client B"
    a.close()
    b.close()


def test_reconnect_after_daemon_sigkill(scratch_daemon):
    c = DaemonClient()
    c.connect()
    pid = _pid(c)
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.3)
    # The failed call is never replayed. A later explicit request can reconnect.
    with pytest.raises(DaemonSessionLost) as lost:
        c.call("daemon_status")
    assert lost.value.detail["outcome"] == "unknown"
    st = c.call("daemon_status")
    assert st["pid"] != pid
    scratch_daemon.append(st["pid"])
    c.close()


def test_rpc_error_maps_to_is_error(scratch_daemon):
    c = DaemonClient()
    c.connect()
    scratch_daemon.append(_pid(c))
    r = c.call("definitely_not_a_method", {})
    assert r.get("_is_error") is True
    assert "unknown method" in (r.get("message") or "")
    c.close()


def test_core_bridge_daemon_mode_raises_coreerror(scratch_daemon, monkeypatch):
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    # Reset the daemon_bridge singleton so it picks up the scratch key.
    monkeypatch.setattr(daemon_bridge, "_client", None)
    from services.mcp import core_bridge

    r = core_bridge.call("doc_new", {"name": "via-core-bridge"})
    assert "doc_id" in r
    scratch_daemon.append(daemon_bridge.client().call("daemon_status")["pid"])
    with pytest.raises(core_bridge.CoreError):
        core_bridge.call("definitely_not_a_method", {})


def test_attach_only_fails_without_daemon(scratch_daemon):
    c = DaemonClient()
    with pytest.raises(DaemonError):
        c.connect(spawn=False, budget_s=1.0)


class _MutationPeer:
    """A protocol peer that commits once, then loses that mutation's reply."""

    def __init__(self, loss):
        self.loss = loss
        self.mutations = []
        self.connections = 0

    def connect(self, client, spawn=True, budget_s=15.0):
        self.connections += 1
        client._sock = _MutationConnection(self)
        client._session_established = True


class _MutationConnection:
    def __init__(self, peer):
        self.peer = peer
        self.reply = b""
        self.lost = None

    def sendall(self, data):
        request = json.loads(data)
        if request["method"] == "doc_apply_ops":
            self.peer.mutations.append(request["params"])
            loss, self.peer.loss = self.peer.loss, None
            if loss == "send":
                # sendall cannot say how many bytes the server consumed.
                raise BrokenPipeError("reply lost after accepting request")
            if loss:
                self.lost = loss
                return
        result = {"mutations": self.peer.mutations}
        self.reply = json.dumps({"id": request["id"], "result": result}).encode() + b"\n"

    def recv(self, size):
        if self.lost == "reset":
            raise ConnectionResetError("peer reset")
        reply, self.reply = self.reply, b""
        return reply

    def close(self):
        self.reply = b""


@pytest.mark.parametrize("loss", ["eof", "reset", "send"])
def test_lost_mutation_reply_is_not_replayed(monkeypatch, loss):
    client = DaemonClient()
    peer = _MutationPeer(loss)
    monkeypatch.setattr(client, "_connect", lambda *args: peer.connect(client, *args))
    params = {"doc_id": "doc-cpp00000001", "ops": [{"op": "rename", "name": "changed"}]}
    with pytest.raises(DaemonSessionLost) as lost:
        client.call("doc_apply_ops", params)
    assert lost.value.code == "engine_connection_lost"
    assert lost.value.detail == {
        "method": "doc_apply_ops", "outcome": "unknown", "retryable": False,
    }
    assert peer.connections == 1
    assert peer.mutations == [params]
    # The same daemon can still be alive: a lost connection is not proof of restart.
    assert client.call("daemon_status") == {"mutations": [params]}
    assert peer.connections == 2
    client.close()


def test_ambiguous_mutation_never_falls_back_to_another_engine(monkeypatch):
    from services.mcp import core_bridge

    client = DaemonClient()
    peer = _MutationPeer("eof")
    monkeypatch.setattr(client, "_connect", lambda *args: peer.connect(client, *args))
    monkeypatch.setattr(daemon_bridge, "_client", client)
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    monkeypatch.setattr(core_bridge, "_DAEMON_FALLBACK_WARNED", False)

    def unrelated_engine():
        pytest.fail("ambiguous mutation reached the unrelated in-process engine")

    monkeypatch.setattr(core_bridge, "_load", unrelated_engine)
    params = {"doc_id": "doc-cpp00000001", "ops": [{"op": "rename", "name": "changed"}]}
    with pytest.raises(core_bridge.CoreError) as lost:
        core_bridge.call("doc_apply_ops", params)
    assert lost.value.code == "engine_connection_lost"
    assert lost.value.detail["outcome"] == "unknown"
    assert core_bridge.call("daemon_status") == {"mutations": [params]}

    # Nor may a later failed reconnect demote an already-established session.
    client.close()

    def unavailable(*args):
        raise DaemonError("connection refused")

    monkeypatch.setattr(client, "_connect", unavailable)
    with pytest.raises(core_bridge.CoreError) as reconnect:
        core_bridge.call("daemon_status")
    assert reconnect.value.code == "engine_connection_lost"
    assert reconnect.value.detail["outcome"] == "not_dispatched"
    assert peer.mutations == [params]


def test_startup_failure_precedes_request_dispatch(monkeypatch):
    client = DaemonClient()

    def unavailable(*args):
        raise DaemonError("connection refused")

    monkeypatch.setattr(client, "_connect", unavailable)
    with pytest.raises(DaemonError) as failure:
        client.call("doc_apply_ops", {"doc_id": "doc-cpp00000001", "ops": []})
    assert not isinstance(failure.value, DaemonSessionLost)
    assert not client.connected()


@pytest.mark.parametrize("state", ["ready", "failed", "lost", "timeout"])
def test_loading_refusal_is_distinct_from_lost_mutation_reply(monkeypatch, state):
    client = DaemonClient()
    applied = []
    refused = False

    class LoadingConnection:
        reply = b""

        def sendall(self, data):
            nonlocal refused
            request = json.loads(data)
            if request["method"] == "doc_apply_ops" and not refused:
                refused = True
                payload = {"error": {"code": "loading"}}
            elif request["method"] == "daemon_status":
                if state == "lost":
                    self.reply = b""
                    return
                payload = {"result": {"state": state}}
            else:
                applied.append(request["params"])
                payload = {"result": {"revision": 2}}
            self.reply = json.dumps({"id": request["id"], **payload}).encode() + b"\n"

        def recv(self, size):
            reply, self.reply = self.reply, b""
            return reply

        def close(self):
            pass

    client._sock = LoadingConnection()
    if state == "timeout":
        monkeypatch.setattr(client, "ENGINE_READY_BUDGET_S", 0)
    params = {"doc_id": "doc-cpp00000001", "ops": [{"op": "rename", "name": "new"}]}
    if state == "lost":
        with pytest.raises(DaemonSessionLost) as lost:
            client.call("doc_apply_ops", params)
        assert lost.value.detail["outcome"] == "not_dispatched"
    else:
        result = client.call("doc_apply_ops", params)
        if state == "ready":
            assert result == {"revision": 2}
        else:
            assert result["_is_error"] is True
            assert result["code"] == (
                "engine_failed" if state == "failed" else "engine_load_timeout"
            )
    assert applied == ([params] if state == "ready" else [])
    client.close()
