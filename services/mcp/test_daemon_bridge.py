"""daemon_bridge tests — the MCP side of the shared engine-host daemon.

Native integration tests use a scratch CHUDWARE_INSTANCE_KEY and skip without a
built hostd. Protocol regression tests use an in-memory peer and need no engine.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid

import pytest

from services.mcp import daemon_bridge
from services.mcp.daemon_bridge import (
    DaemonClient,
    DaemonDraining,
    DaemonError,
    DaemonSessionLost,
)

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


def test_idle_exit_under_a_parked_client_reconnects_cleanly(scratch_daemon):
    """hostd idle-exits under a connected client that holds nothing (a headless agent
    reading files for minutes). That clean exit is announced, so the client's next
    call reconnects instead of failing as a lost session — and the daemon's teardown
    with that socket still open must not abort."""
    c = DaemonClient()
    c.connect()
    pid = _pid(c)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        pytest.fail("hostd did not idle-exit under a parked client")
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
    # Use a fresh connection so it picks up the scratch key.
    monkeypatch.setattr(daemon_bridge, "client", lambda: client)
    client = DaemonClient()
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



class _ChunkConnection:
    """The recv/sendall/close shape shared by sockets and Windows pipe adapters."""

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.requests = []

    def recv(self, size):
        chunk = next(self.chunks, b"")
        assert len(chunk) <= size
        return chunk

    def sendall(self, data):
        self.requests.append(json.loads(data))

    def close(self):
        pass


def test_read_msg_every_chunk_boundary():
    message = {"id": 1, "result": {"name": "café", "text": "one\ntwo"}}
    wire = json.dumps(message, ensure_ascii=False).encode() + b"\r\n"
    # Includes splits inside a UTF-8 codepoint, a JSON escape and the CRLF.
    for split in range(1, len(wire)):
        client = DaemonClient()
        client._sock = _ChunkConnection([wire[:split], wire[split:]])
        try:
            assert client._read_msg() == message
            with pytest.raises(DaemonError, match="daemon connection closed"):
                client._read_msg()
        finally:
            client.close()


@pytest.mark.parametrize("line_bytes", [65535, 65536, 65537, 3 * 65536 + 1])
def test_read_msg_large_line_and_buffered_tail(line_bytes):
    prefix, suffix = b'{"id":1,"result":{"payload":"', b'"}}\n'
    payload = "x" * (line_bytes - len(prefix) - len(suffix))
    first = prefix + payload.encode() + suffix
    # The next line crosses a receive boundary after the large consumed prefix.
    second = {"id": 2, "result": {"payload": "y" * 70000}}
    third = {"event": "doc.changed", "data": {"revision": 3}}
    wire = first + json.dumps(second).encode() + b"\n" + json.dumps(third).encode() + b"\n"
    client = DaemonClient()
    client._sock = _ChunkConnection(
        [wire[start:start + 65536] for start in range(0, len(wire), 65536)]
    )
    try:
        assert client._read_msg() == {"id": 1, "result": {"payload": payload}}
        assert client._read_msg() == second
        assert client._read_msg() == third
        with pytest.raises(DaemonError, match="daemon connection closed"):
            client._read_msg()
    finally:
        client.close()


def test_call_raw_preserves_multiple_events_replies_and_stray_lines():
    first = {"event": "doc.changed", "data": {"revision": 1}}
    second = {"event": "doc.changed", "data": {"revision": 2}}
    messages = [
        first,
        {"id": 0, "result": {"ignored": True}},
        {"id": 1, "result": {"revision": 1}},
        second,
        {"id": 2, "error": {"code": "not_found"}},
    ]
    wire = b"\nstartup log\r\n{broken json}\n" + b"".join(
        json.dumps(message).encode() + b"\n" for message in messages
    )
    connection = _ChunkConnection([wire])
    client = DaemonClient()
    client._sock = connection
    try:
        assert client._call_raw("doc_tree", {}) == {"revision": 1}
        assert client.poll_events() == [first]
        assert client._call_raw("doc_tree", {}) == {"code": "not_found", "_is_error": True}
        assert client.poll_events() == [second]
        assert [request["id"] for request in connection.requests] == [1, 2]
    finally:
        client.close()


@pytest.mark.parametrize("tail", [b"", b'{"id":2', b'{"id":2,"result":{}}', b"stray\n{"])
def test_read_msg_eof_never_accepts_unframed_tail(tail):
    client = DaemonClient()
    client._sock = _ChunkConnection([b'{"id":1,"result":{}}\n' + tail])
    try:
        assert client._read_msg() == {"id": 1, "result": {}}
        with pytest.raises(DaemonError, match="daemon connection closed"):
            client._read_msg()
    finally:
        client.close()
    # Reusing a client must discard both a partial line and its old scan offset.
    client._sock = _ChunkConnection([b'{"id":2,"result":{"fresh":true}}\n'])
    try:
        assert client._read_msg() == {"id": 2, "result": {"fresh": True}}
    finally:
        client.close()


@pytest.mark.skipif(not hasattr(socket, "MSG_DONTWAIT"), reason="POSIX idle socket probe")
@pytest.mark.parametrize(
    "inspect,event",
    [
        ("_draining_event", {"event": "daemon.draining", "reason": "stale_binary"}),
        ("_peer_closed_idle", {"event": "daemon.exiting", "reason": "idle"}),
        ("_peer_closed_idle", {"event": "daemon.exiting", "reason": "drained"}),
    ],
)
def test_socket_inspection_ignores_consumed_prefix_and_preserves_unread(inspect, event):
    client = DaemonClient()
    client._sock, peer = socket.socketpair()
    # Seed a single recv containing several lines, with the event already consumed.
    wire = json.dumps(event).encode() + b"\n" + b'{"id":1,"result":{}}\n'
    try:
        peer.sendall(wire)
        assert client._read_msg() == event
        peer.close()
        assert not getattr(client, inspect)()
        assert client._read_msg() == {"id": 1, "result": {}}
    finally:
        peer.close()
        client.close()


@pytest.mark.skipif(not hasattr(socket, "MSG_DONTWAIT"), reason="POSIX idle socket probe")
@pytest.mark.parametrize(
    "inspect,event",
    [
        ("_draining_event", {"event": "daemon.draining", "reason": "stale_binary"}),
        ("_peer_closed_idle", {"event": "daemon.exiting", "reason": "idle"}),
    ],
)
@pytest.mark.parametrize("newline", [b"", b"\n"])
def test_socket_inspection_finds_fragmented_event_without_consuming(inspect, event, newline):
    client = DaemonClient()
    client._sock, peer = socket.socketpair()
    wire = json.dumps(event).encode()
    split = len(wire) // 2
    try:
        peer.sendall(wire[:split])
        assert not getattr(client, inspect)()
        peer.sendall(wire[split:] + newline)
        peer.shutdown(socket.SHUT_WR)
        found = getattr(client, inspect)()
        assert found == (event if inspect == "_draining_event" else True)
        if newline:
            assert client._read_msg() == event
        # Inspection historically recognizes an unframed EOF event, but the RPC
        # reader still requires a newline and must report EOF, never a reply.
        with pytest.raises(DaemonError, match="daemon connection closed"):
            client._read_msg()
    finally:
        peer.close()
        client.close()


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
    monkeypatch.setattr(daemon_bridge, "client", lambda: client)
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
    # core_bridge sends an implicit batch with base_revision -1 (no CAS between writers).
    sent = {**params, "base_revision": -1}
    assert core_bridge.call("daemon_status") == {"mutations": [sent]}

    # Nor may a later failed reconnect demote an already-established session.
    client.close()

    def unavailable(*args):
        raise DaemonError("connection refused")

    monkeypatch.setattr(client, "_connect", unavailable)
    with pytest.raises(core_bridge.CoreError) as reconnect:
        core_bridge.call("daemon_status")
    assert reconnect.value.code == "engine_connection_lost"
    assert reconnect.value.detail["outcome"] == "not_dispatched"
    assert peer.mutations == [sent]


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


def _draining_peer(path: str, answer_hello: bool) -> threading.Thread:
    """One-connection hostd stand-in that refuses the client because it drains.

    answer_hello=True is the current daemon: it reads hello, pushes daemon.draining,
    answers with the daemon_draining error, then closes. False is a daemon that pushes
    the event and closes at once with the hello unread (c250: ECONNRESET on hello).
    """
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            if answer_hello:
                buf = b""
                while b"\n" not in buf:
                    buf += conn.recv(4096)
                hello = json.loads(buf.split(b"\n", 1)[0])
                refusal = {"code": "daemon_draining", "reason": "stale_binary",
                           "daemon_pid": 4242, "clients": 2, "active_clients": 1,
                           "changed_path": "/opt/chudware/lib/libchudware.so"}
                conn.sendall(b'{"event":"daemon.draining","reason":"stale_binary"}\n')
                conn.sendall(json.dumps({"id": hello["id"], "error": refusal}).encode() + b"\n")
            else:
                conn.sendall(b'{"event":"daemon.draining"}\n')
        srv.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX stand-in peer")
@pytest.mark.parametrize("answer_hello", [True, False], ids=["refusal", "event_then_close"])
def test_draining_daemon_refusal_is_named_and_never_falls_back(monkeypatch, answer_hello):
    """A draining hostd refuses a new client. core_bridge must raise daemon_draining and
    stay in daemon mode: a private in-process engine would not share documents."""
    from services.mcp import core_bridge

    sock_dir = tempfile.mkdtemp(prefix="cw-drain-")
    path = os.path.join(sock_dir, "h.sock")
    peer = _draining_peer(path, answer_hello)
    monkeypatch.setenv("CHUDWARE_HOSTD_SOCKET", path)
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    monkeypatch.setattr(core_bridge, "_DAEMON_FALLBACK_WARNED", False)
    client = DaemonClient()
    monkeypatch.setattr(daemon_bridge, "client", lambda: client)

    def private_engine():
        pytest.fail("a draining daemon refusal reached the in-process engine")

    monkeypatch.setattr(core_bridge, "_load", private_engine)
    monkeypatch.setattr(daemon_bridge, "_spawn_hostd_detached",
                        lambda *a: pytest.fail("a refusal must not spawn another daemon"))
    try:
        with pytest.raises(core_bridge.CoreError) as refused:
            core_bridge.call("doc_list", {})
    finally:
        peer.join(5)
        shutil.rmtree(sock_dir, ignore_errors=True)
    assert refused.value.code == "daemon_draining"
    assert isinstance(refused.value.__cause__, DaemonDraining)
    if answer_hello:
        assert refused.value.detail["reason"] == "stale_binary"
        assert refused.value.detail["daemon_pid"] == 4242
    assert core_bridge.active_daemon_mode()
    assert not client.connected()


def test_new_thread_connection_cannot_demote_established_worker_to_private_engine(monkeypatch):
    from services.mcp import core_bridge

    established = threading.Event()
    established.set()
    monkeypatch.setattr(daemon_bridge, "_group_session_established", established)
    connection = DaemonClient(writer_group=daemon_bridge._WRITER_GROUP)

    def unavailable(*_args):
        raise DaemonError("new socket could not connect")

    monkeypatch.setattr(connection, "_connect", unavailable)
    monkeypatch.setattr(daemon_bridge, "client", lambda: connection)
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    monkeypatch.setattr(core_bridge, "_DAEMON_FALLBACK_WARNED", False)
    monkeypatch.setattr(
        core_bridge, "_load",
        lambda: pytest.fail("another thread's established engine session was demoted"),
    )
    with pytest.raises(core_bridge.CoreError) as error:
        core_bridge.call("doc_tree", {"doc_id": "part"})
    assert error.value.code == "engine_connection_lost"
    assert error.value.detail["outcome"] == "not_dispatched"
    assert core_bridge.active_daemon_mode()


def test_turn_connections_keep_documents_attached_between_fresh_tool_threads(scratch_daemon):
    """Finishing one host-call thread must not announce a still-running agent as gone."""
    pool = daemon_bridge.TurnConnections()
    viewer = DaemonClient()
    viewer.connect()
    scratch_daemon.append(_pid(viewer))
    viewer.call("subscribe", {"doc_ids": "*"})
    docs = [None, None]
    failures = []
    try:
        for round_index in range(3):
            together = threading.Barrier(2)

            def tool(index):
                try:
                    with daemon_bridge.turn_connection_scope(pool):
                        connection = daemon_bridge.client()
                        if round_index == 0:
                            docs[index] = connection.call(
                                "doc_new", {"name": f"parallel-turn-{index}"}
                            )["doc_id"]
                        else:
                            tree = connection.call("doc_tree", {"doc_id": docs[index]})
                            assert not tree.get("_is_error"), tree
                        # Both independent sockets remain leased until both calls arrive.
                        together.wait(timeout=10)
                except BaseException as error:
                    failures.append(error)
                    together.abort()

            threads = [threading.Thread(target=tool, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
                assert not thread.is_alive()
            assert not failures, failures
            listed = viewer.call("doc_list")["docs"]
            assert {row["doc_id"] for row in listed if row.get("other_writer")} >= set(docs)
            assert not [
                event for event in viewer.poll_events()
                if event.get("event") == "doc.lease_change"
                and event.get("data", {}).get("doc_id") in docs
                and event["data"].get("state") == "disconnected"
            ]
        pool.close()
        released = set()
        deadline = time.monotonic() + 5
        while released != set(docs) and time.monotonic() < deadline:
            viewer.call("ping")
            released.update(
                event["data"]["doc_id"] for event in viewer.poll_events()
                if event.get("event") == "doc.lease_change"
                and event.get("data", {}).get("doc_id") in docs
                and event["data"].get("state") == "disconnected"
            )
            if released != set(docs):
                time.sleep(0.01)
        assert released == set(docs), "turn completion must release both document writers"
        with daemon_bridge.turn_connection_scope(pool), pytest.raises(DaemonSessionLost):
            daemon_bridge.client().call("doc_new", {"name": "must-not-dispatch"})
    finally:
        pool.close()
        viewer.close()


def test_turn_connections_close_does_not_interrupt_active_tool(scratch_daemon):
    pool = daemon_bridge.TurnConnections()
    try:
        with daemon_bridge.turn_connection_scope(pool):
            connection = daemon_bridge.client()
            doc = connection.call("doc_new", {"name": "active-at-turn-close"})["doc_id"]
            scratch_daemon.append(_pid(connection))
            with daemon_bridge.turn_connection_scope(pool):
                pool.close()
                # Closing a turn retires idle leases, not this in-flight tool's socket.
                tree = daemon_bridge.client().call("doc_tree", {"doc_id": doc})
                assert not tree.get("_is_error"), tree
            assert connection.connected()
        assert not connection.connected()
    finally:
        pool.close()
