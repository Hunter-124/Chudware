"""daemon_bridge tests — the MCP side of the shared engine-host daemon.

Each test uses a scratch CHUDWARE_INSTANCE_KEY so a live dev/installed daemon is
never touched. Skips when no chudware-hostd binary is built (mirrors the gate's
hostd_smoke SKIP semantics).
"""
from __future__ import annotations

import os
import signal
import time
import uuid

import pytest

from mcp_server import daemon_bridge
from mcp_server.daemon_bridge import DaemonClient, DaemonError

HOSTD = daemon_bridge.resolve_hostd_bin()

pytestmark = pytest.mark.skipif(HOSTD is None, reason="chudware-hostd not built")


@pytest.fixture()
def scratch_daemon(monkeypatch):
    """Fresh instance key + fast linger; kills the daemon on teardown."""
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
    # Next call sees EOF, reconnects, and respawns a fresh daemon transparently.
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
    from mcp_server import core_bridge

    r = core_bridge.call("doc_new", {"name": "via-core-bridge"})
    assert "doc_id" in r
    scratch_daemon.append(daemon_bridge.client().call("daemon_status")["pid"])
    with pytest.raises(core_bridge.CoreError):
        core_bridge.call("definitely_not_a_method", {})


def test_attach_only_fails_without_daemon(scratch_daemon):
    c = DaemonClient()
    with pytest.raises(DaemonError):
        c.connect(spawn=False, budget_s=1.0)
