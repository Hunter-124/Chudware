import threading
import json
import os
import socket
import stat
import time

import pytest

from services.agent.omp_bridge import BridgeError, ExtensionBridge, _CONTROL_LIMIT, _MAX_PENDING, _Control
from services.mcp.core_bridge import CoreError


def definition(name="cad_tool"):
    return {"name": name, "description": "CAD", "input_schema": {"type": "object"}, "load_mode": "discoverable"}


def test_child_identity_progress_and_cancellation(tmp_path):
    seen = []
    entered = threading.Event()

    def dispatch(name, args, ctx):
        seen.append((name, args, ctx.agent_id, ctx.parent_agent_id, ctx.session_id, ctx.tool_call_id))
        ctx.send_update({"content": [{"type": "text", "text": "progress"}]})
        entered.set()
        while not ctx.cancelled:
            time.sleep(0.001)
        return {"content": [{"type": "text", "text": "cancelled"}], "details": {}}

    bridge = ExtensionBridge(tmp_path, dispatch)
    bridge.publish_tools([definition()])
    started = bridge._handle("tool_start", {"name": "cad_tool", "arguments": {"v": 1},
        "context": {"agent_id": "child", "parent_agent_id": "root", "session_id": "session", "tool_call_id": "call"}})
    assert entered.wait(1)
    progress = bridge._handle("tool_poll", {"id": started["id"]})
    assert progress["updates"][0]["content"][0]["text"] == "progress"
    bridge._handle("tool_cancel", {"id": started["id"]})
    deadline = time.monotonic() + 1
    while True:
        state = bridge._handle("tool_poll", {"id": started["id"]})
        if state.get("done"):
            break
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert seen == [("cad_tool", {"v": 1}, "child", "root", "session", "call")]
    assert state["result"]["content"][0]["text"] == "cancelled"


def test_cancel_closes_only_own_hostd_lease():
    from services.agent.omp_bridge import _ToolContext
    from services.mcp.daemon_bridge import TurnConnections, turn_connection_scope, client

    pool = TurnConnections()
    contexts = [_ToolContext({}), _ToolContext({})]
    pairs = [socket.socketpair(), socket.socketpair()]
    done = [threading.Event(), threading.Event()]
    results = [None, None]

    def run(index):
        try:
            with turn_connection_scope(pool, contexts[index]):
                conn = client()
                conn._sock = pairs[index][0]
                results[index] = conn.call("doc_tree", {})
        except Exception as exc:
            results[index] = exc
        finally:
            done[index].set()

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    try:
        for thread in threads:
            thread.start()
        for _, peer in pairs:
            peer.settimeout(2)
            assert b"doc_tree" in peer.recv(4096)
        contexts[0].cancel()
        assert pairs[0][1].recv(1) == b""
        assert done[0].wait(2)
        assert not done[1].is_set()
        pairs[1][1].sendall(b'{"id":1,"result":{"ok":true}}\n')
        assert done[1].wait(2)
        assert results[1] == {"ok": True}
    finally:
        for _, peer in pairs:
            peer.close()
        for thread in threads:
            thread.join(2)
        pool.close()


def test_cancel_before_connection_acquire_rejects_dispatch():
    from services.agent.omp_bridge import _ToolContext
    from services.mcp.daemon_bridge import TurnConnections, turn_connection_scope, client, DaemonError

    pool = TurnConnections()
    ctx = _ToolContext({})
    ctx.cancel()
    try:
        with turn_connection_scope(pool, ctx):
            with pytest.raises(DaemonError):
                client().call("doc_tree", {})
    finally:
        pool.close()


@pytest.mark.parametrize("error_type", [ValueError, CoreError, RuntimeError])
def test_tool_failure_diagnostics_are_actionable_bounded_and_private(tmp_path, error_type):
    private = {}

    def dispatch(*_):
        raise error_type(
            "target must name a face; use query_geometry first. "
            f"{private['token']} {private['endpoint']} api_key=sk-secret123456 "
            + "x" * 3000)

    bridge = ExtensionBridge(tmp_path, dispatch).start()
    private.update(bridge.info())
    try:
        bridge.publish_tools([definition()])
        started = bridge._handle("tool_start", {"name": "cad_tool", "arguments": {},
            "context": {"agent_id": "root", "session_id": "s", "tool_call_id": "c"}})
        deadline = time.monotonic() + 2
        while True:
            state = bridge._handle("tool_poll", {"id": started["id"]})
            if state.get("done"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert state["error"] == "failed"
        assert state["result"] is None
        if error_type is RuntimeError:
            assert state["error_detail"] is None
        else:
            detail = state["error_detail"]
            assert "use query_geometry first" in detail
            assert len(detail) <= 2000
            assert private["token"] not in detail
            assert private["endpoint"] not in detail
            assert "sk-secret123456" not in detail
    finally:
        bridge.stop()


def test_control_correlation_and_timeout_are_scoped(tmp_path):
    bridge = ExtensionBridge(tmp_path, lambda *_: None)
    output = []
    worker = threading.Thread(target=lambda: output.append(bridge.call("auth_state", {"provider": "demo"}, timeout=2)))
    worker.start()
    deadline = time.monotonic() + 1
    while True:
        command = bridge._handle("poll", {})["command"]
        if command:
            break
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert command["operation"] == "auth_state"
    assert bridge._handle("is_pending", {"id": command["id"]})["pending"]
    assert not bridge._handle("result", {"id": "wrong", "result": {}})["accepted"]
    bridge._handle("result", {"id": command["id"], "result": {"configured": False}})
    worker.join(1)
    assert not bridge._handle("is_pending", {"id": command["id"]})["pending"]
    assert output == [{"configured": False}]
    with pytest.raises(BridgeError) as exc:
        bridge.call("memory_status", {}, timeout=0.001)
    assert exc.value.code == "timeout"
    assert bridge._handle("poll", {})["command"] is None


def test_tool_state_control_preserves_runtime_rows(tmp_path):
    bridge = ExtensionBridge(tmp_path, lambda *_: None)
    output = []
    worker = threading.Thread(target=lambda: output.append(bridge.call("tool_state", {}, timeout=2)))
    worker.start()
    deadline = time.monotonic() + 1
    while True:
        command = bridge._handle("poll", {})["command"]
        if command:
            break
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert command["operation"] == "tool_state"
    rows = [{"name": "read", "builtin": True, "available": True},
            {"name": "cad", "builtin": False, "available": False}]
    bridge._handle("result", {"id": command["id"], "result": {"tools": rows}})
    worker.join(1)
    assert output == [{"tools": rows}]


def test_bounds_admission_and_no_arbitrary_controls(tmp_path):
    bridge = ExtensionBridge(tmp_path, lambda *_: None)
    bridge.publish_tools([definition()])
    with pytest.raises(ValueError):
        bridge.publish_tools([definition(), definition()])
    with pytest.raises(BridgeError) as exc:
        bridge.call("eval", {}, timeout=1)
    assert exc.value.code == "unsupported"
    with pytest.raises(BridgeError) as exc:
        bridge.call("memory_save", {"content": "x" * _CONTROL_LIMIT}, timeout=1)
    assert exc.value.code == "invalid_request"
    assert bridge._handle("tool_start", {"name": "unknown", "arguments": {},
        "context": {"agent_id": "root", "session_id": "s", "tool_call_id": "c"}})["error"] == "unsupported"
    bridge._commands.update({str(index): _Control("memory_status", {}) for index in range(_MAX_PENDING)})
    with pytest.raises(BridgeError) as exc:
        bridge.call("memory_status", {}, timeout=1)
    assert exc.value.code == "busy"


def test_child_keyless_policy_updates_immediately(tmp_path):
    bridge = ExtensionBridge(tmp_path, lambda *_: None)
    assert bridge._handle("model_policy", {"provider": "local"}) == {"allowed": False}
    bridge.publish_keyless_providers(["local"])
    assert bridge._handle("model_policy", {"provider": "local"}) == {"allowed": True}
    assert bridge._handle("model_policy", {"provider": "other"}) == {"allowed": False}
    bridge.publish_keyless_providers([])
    assert bridge._handle("model_policy", {"provider": "local"}) == {"allowed": False}
    with pytest.raises(ValueError):
        bridge.publish_keyless_providers([""])


def test_registry_ack_tracks_current_generation(tmp_path):
    bridge = ExtensionBridge(tmp_path, lambda *_: None)
    bridge.publish_tools([definition()])
    generation = bridge._handle("bootstrap", {})["generation"]
    assert not bridge._handle("tool_ack", {"generation": generation - 1})["accepted"]
    with pytest.raises(BridgeError) as exc:
        bridge.wait_tools(timeout=0.001)
    assert exc.value.code == "timeout"
    assert bridge._handle("tool_ack", {"generation": generation})["accepted"]
    bridge.wait_tools(timeout=1)
    bridge.publish_tools([definition("other")])
    with pytest.raises(BridgeError):
        bridge.wait_tools(timeout=0.001)


def test_registry_activation_is_explicit_and_versioned(tmp_path):
    bridge = ExtensionBridge(tmp_path, lambda *_: None)
    bridge.publish_tools([definition(), {**definition("dormant"), "active": False}])
    initial = bridge._handle("bootstrap", {})
    assert [tool["active"] for tool in initial["tools"]] == [True, False]
    bridge.publish_tools([definition(), {**definition("dormant"), "active": True}])
    updated = bridge._handle("bootstrap", {})
    assert updated["generation"] > initial["generation"]
    assert [tool["active"] for tool in updated["tools"]] == [True, True]
    with pytest.raises(ValueError):
        bridge.publish_tools([{**definition(), "active": "yes"}])


def test_first_published_description_is_pinned_for_the_runtime(tmp_path):
    """Settings notes/timeouts or the skill catalog changing mid-conversation must not
    rewrite a tool description (provider-visible prefix) or bump the registry."""
    bridge = ExtensionBridge(tmp_path, lambda *_: None)
    bridge.publish_tools([{**definition(), "description": "as first read"}])
    first = bridge._handle("bootstrap", {})
    bridge.publish_tools([{**definition(), "description": "after a Settings edit"}])
    after = bridge._handle("bootstrap", {})
    assert after["generation"] == first["generation"]
    assert after["tools"][0]["description"] == "as first read"
    bridge.publish_tools([{**definition(), "description": "after a Settings edit"},
                          {**definition("late"), "description": "new tool, own text"}])
    assert [tool["description"] for tool in bridge._handle("bootstrap", {})["tools"]] == [
        "as first read", "new tool, own text"]


def test_stop_wakes_pending_control(tmp_path):
    bridge = ExtensionBridge(tmp_path, lambda *_: None)
    started = threading.Event()
    errors = []
    def call():
        started.set()
        try:
            bridge.call("memory_status", {}, timeout=5)
        except BridgeError as exc:
            errors.append(exc.code)
    worker = threading.Thread(target=call)
    worker.start()
    assert started.wait(1)
    deadline = time.monotonic() + 1
    while not bridge._commands:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    bridge.stop()
    worker.join(1)
    assert errors == ["stopped"]


@pytest.mark.skipif(os.name == "nt", reason="Unix transport")
def test_transport_authentication_and_private_socket(tmp_path):
    bridge = ExtensionBridge(tmp_path, lambda *_: None).start()
    try:
        info = bridge.info()
        assert stat.S_IMODE(os.stat(info["endpoint"]).st_mode) == 0o600
        def exchange(token):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(info["endpoint"])
                client.sendall((json.dumps({"token": token, "action": "bootstrap", "params": {}}) + "\n").encode())
                data = bytearray()
                while b"\n" not in data:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data.extend(chunk)
                return json.loads(data)
            finally:
                client.close()
        assert not exchange("wrong")["ok"]
        assert exchange(info["token"])["result"] == {"tools": [], "generation": 0}
    finally:
        bridge.stop()


@pytest.mark.skipif(os.name == "nt", reason="Unix transport")
def test_extension_round_trips_over_its_socket_inside_a_cell(tmp_path, monkeypatch):
    # The extension speaks to the bridge over its socket with no hostd identity. In a cell the
    # registry bootstrap and a tool call must still reach the bridge, not a cell refusal.
    monkeypatch.setenv("CHUDWARE_CELL", "1")
    bridge = ExtensionBridge(tmp_path, lambda name, args, ctx: {"echo": args}).start()
    bridge.publish_tools([definition()])
    info = bridge.info()

    def exchange(action, params=None):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        try:
            client.connect(info["endpoint"])
            client.sendall((json.dumps({"token": info["token"], "action": action, "params": params or {}}) + "\n").encode())
            data = bytearray()
            while b"\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
            return json.loads(data)
        finally:
            client.close()

    try:
        snapshot = exchange("bootstrap")
        assert snapshot["ok"] is True
        assert [tool["name"] for tool in snapshot["result"]["tools"]] == ["cad_tool"]
        assert isinstance(snapshot["result"]["generation"], int)
        started = exchange("tool_start", {"name": "cad_tool", "arguments": {"v": 1}, "context": {
            "agent_id": "root", "session_id": "session", "tool_call_id": "call"}})["result"]
        deadline = time.monotonic() + 2
        while True:
            state = exchange("tool_poll", {"id": started["id"]})["result"]
            if state.get("done"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert state["error"] is None and state["result"] == {"echo": {"v": 1}}
    finally:
        bridge.stop()
