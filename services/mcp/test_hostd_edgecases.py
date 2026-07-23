"""Phase-8 edge-case hammering for aicad-hostd (the release-cut sweep).

Covers the failure modes the default flip exposes to every session:
  (a) draining daemon refuses new clients; respawn works after it exits
  (b) stale-binary self-drain backstop (AICAD_HOSTD_STALECHECK_S)
  (c) engine load FAILURE: clients get engine_failed, daemon still drains
  (d) multi-MB payloads cross the socket intact (both directions)
  (e) rapid connect/disconnect churn: no fd leak, clients table drains

Each test uses a scratch AICAD_INSTANCE_KEY (its own daemon) so a live dev/
installed daemon is never touched. Skips when no aicad-hostd is built
(mirrors test_daemon_bridge.py / test_hostd_registry.py).
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path

import pytest

from mcp_server import daemon_bridge
from mcp_server.daemon_bridge import DaemonClient, DaemonError

HOSTD = daemon_bridge.resolve_hostd_bin()

pytestmark = pytest.mark.skipif(HOSTD is None, reason="aicad-hostd not built")

BOX_OPS = [{"op": "add_feature",
            "feature": {"type": "box", "name": "b1",
                        "params": {"length": 10, "width": 10, "height": 10},
                        "refs": {}}}]


@pytest.fixture()
def scratch_daemon(monkeypatch):
    """Fresh instance key + fast linger/stale-check; kills the daemon on teardown."""
    key = "pytest-edge-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("AICAD_INSTANCE_KEY", key)
    monkeypatch.setenv("AICAD_HOSTD_LINGER_SECS", "3")
    monkeypatch.setenv("AICAD_HOSTD_STALECHECK_S", "1")
    monkeypatch.delenv("AICAD_HOSTD_SOCKET", raising=False)
    pids: list = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _pid(c: DaemonClient) -> int:
    return c.call("daemon_status")["pid"]


def _wait_pid_gone(pid: int, timeout: float = 15.0) -> bool:
    # _pid_alive treats a zombie as gone (the daemon is our unreaped child).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not daemon_bridge._pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _wait_state(c: DaemonClient, state: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    st: dict = {}
    while time.monotonic() < deadline:
        st = c.call("daemon_status")
        if st.get("state") == state:
            return st
        time.sleep(0.2)
    return st


# ------------------------------------------------------- (a) draining refusal


def test_draining_refuses_new_clients_then_respawn(scratch_daemon):
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    assert a.call("daemon_drain").get("ok") is True

    # A NEW client during the drain window: the daemon pushes daemon.draining
    # and closes — the connect must fail cleanly (no hang, no half-session).
    b = DaemonClient()
    with pytest.raises(DaemonError):
        b.connect(spawn=False, budget_s=3.0)

    # The existing client finishes and disconnects; the daemon exits...
    a.close()
    assert _wait_pid_gone(pid), "drained daemon must exit once its clients leave"

    # ...and respawn-after-exit works: the next connect starts a fresh daemon.
    c = DaemonClient()
    c.connect()
    pid2 = _pid(c)
    scratch_daemon.append(pid2)
    assert pid2 != pid
    assert c.call("ping").get("ok") is True
    c.close()


# ------------------------------------------------ (b) stale-binary self-drain


def test_stale_binary_self_drain(scratch_daemon, monkeypatch, tmp_path):
    # A private COPY of the hostd binary so touching it can never drain a real
    # dev daemon (which watches the build-tree binary's mtime).
    hostd_copy = tmp_path / ("aicad-hostd.exe" if sys.platform == "win32" else "aicad-hostd")
    shutil.copy2(HOSTD, hostd_copy)
    monkeypatch.setenv("AICAD_HOSTD_BIN", str(hostd_copy))

    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    assert a.call("daemon_status").get("draining") is False

    time.sleep(1.2)     # let the first stale-check tick record the mtime baseline
    os.utime(hostd_copy)  # "a new build landed at this path"

    deadline = time.monotonic() + 10.0  # AICAD_HOSTD_STALECHECK_S=1 -> well within
    st: dict = {}
    while time.monotonic() < deadline:
        st = a.call("daemon_status")
        if st.get("draining"):
            break
        time.sleep(0.3)
    assert st.get("draining") is True, f"self-drain never fired: {st}"

    # Existing client keeps working during the drain; exit happens on close.
    assert a.call("ping").get("ok") is True
    a.close()
    assert _wait_pid_gone(pid), "self-drained daemon must exit once idle"


# --------------------------------------------------- (c) engine load failure


def test_engine_load_failure_clients_get_engine_failed(scratch_daemon, monkeypatch, tmp_path):
    # Copy hostd away from its build dir (no exe-adjacent libaicad.so fallback)
    # and point the engine path at a non-library: the async load must fail,
    # clients must see engine_failed (not a hang), and drain must still work.
    hostd_copy = tmp_path / ("aicad-hostd.exe" if sys.platform == "win32" else "aicad-hostd")
    shutil.copy2(HOSTD, hostd_copy)
    monkeypatch.setenv("AICAD_HOSTD_BIN", str(hostd_copy))
    monkeypatch.setenv("AI_CAD_LIBAICAD", os.devnull)
    monkeypatch.delenv("AI_CAD_DIST", raising=False)
    # core_bridge's in-process load prepends the real lib dir to LD_LIBRARY_PATH
    # (process-global); the spawned daemon would inherit it and its last-resort
    # bare "libaicad.so" dlopen would find the build-tree lib — scrub it.
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("DYLD_LIBRARY_PATH", raising=False)

    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)

    st = _wait_state(a, "failed", timeout=60.0)
    assert st.get("state") == "failed", f"engine load must fail fast, got {st}"

    # Engine calls answer engine_failed immediately — no hang. (Bypass
    # DaemonClient.call's loading-absorption by checking the raw reply.)
    r = a.call("doc_new", {"name": "doomed"})
    assert r.get("_is_error") is True
    assert r.get("code") == "engine_failed"

    # Daemon-level methods still work and the daemon drains/stops cleanly.
    assert a.call("ping").get("ok") is True
    assert a.call("daemon_drain").get("ok") is True
    a.close()
    assert _wait_pid_gone(pid), "engine-failed daemon must still exit cleanly"


# ------------------------------------------- client-side version drain-replace


@pytest.mark.skipif(sys.platform == "win32", reason="wrapper-script trick is POSIX-only")
def test_client_drain_replace_on_stale_build(scratch_daemon, monkeypatch, tmp_path):
    """A client whose on-disk hostd reports a DIFFERENT build id than the
    running daemon must drain it, wait for exit, and respawn/reconnect."""
    # Daemon A runs the real binary.
    monkeypatch.setenv("AICAD_HOSTD_BIN", str(HOSTD))
    a = DaemonClient()
    a.connect()
    pid_a = _pid(a)
    scratch_daemon.append(pid_a)
    assert a._stale_retry_done is False, "matched build must never trigger a drain"
    a.close()

    # Client B resolves a wrapper whose --version lies ("a newer build landed");
    # everything else execs the real hostd, so the respawn serves for real.
    wrapper = tmp_path / "aicad-hostd"
    wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "aicad-hostd fake-newer-build"; exit 0; fi\n'
        f'exec "{HOSTD}" "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("AICAD_HOSTD_BIN", str(wrapper))

    b = DaemonClient()
    b.connect()  # -> stale detected -> drain A -> wait exit -> respawn -> reconnect
    assert b._stale_retry_done is True
    pid_b = _pid(b)
    scratch_daemon.append(pid_b)
    assert pid_b != pid_a, "client must land on a freshly spawned daemon"
    assert not daemon_bridge._pid_alive(pid_a), "the stale daemon must be gone"
    assert b.call("ping").get("ok") is True
    b.close()


# ------------------------------------------------------ (d) huge payloads


def test_multi_mb_payload_roundtrip(scratch_daemon):
    a = DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))

    # Request direction: a 3 MB params line must cross the socket intact and
    # come back as a clean unknown-method error (not a framing corruption/hang).
    blob = "x" * (3 * 1024 * 1024)
    r = a.call("definitely_not_a_method", {"blob": blob})
    assert r.get("_is_error") is True
    # The connection survives the oversized exchange.
    assert a.call("ping").get("ok") is True

    # Response direction: a doc tree with a few hundred sketch entities round-
    # trips (doc_apply_ops in, doc_tree out) without truncation.
    doc = a.call("doc_new", {"name": "bulk"})["doc_id"]
    entities = []
    for i in range(400):
        x = float(5 * i)
        entities.append({"id": f"e{i + 1}", "type": "line", "construction": False,
                         "p1": [x, 0.0], "p2": [x + 2.0, 3.0]})
    ops = [{"op": "add_feature",
            "feature": {"type": "sketch", "name": "s1",
                        "params": {"entities": entities, "constraints": []},
                        "refs": {"plane": "plane.top"}}}]
    r2 = a.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
    assert r2.get("_is_error") is None, r2
    tree = a.call("doc_tree", {"doc_id": doc})
    assert tree.get("_is_error") is None
    dumped = str(tree)
    assert dumped.count("line") >= 400, "doc tree lost sketch entities in transit"
    a.close()


# ---------------------------------------------------- (e) connect churn / fds


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="/proc fd accounting is Linux-only")
def test_rapid_connect_churn_no_fd_leak(scratch_daemon):
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    fd_dir = Path(f"/proc/{pid}/fd")

    def fds() -> int:
        return len(list(fd_dir.iterdir()))

    # Warm-up: the first RPC lazily spins up the engine/worker (one-time fd cost —
    # worker pipes, libaicad threads); take the baseline AFTER that settles so the
    # assertion measures the churn, not the init.
    warm = DaemonClient()
    warm.connect()
    assert warm.call("ping").get("ok") is True
    warm.close()
    time.sleep(1.0)

    baseline = fds()
    for _ in range(50):
        c = DaemonClient()
        c.connect()
        assert c.call("ping").get("ok") is True
        c.close()

    # Disconnect handling is asynchronous (per-client thread teardown).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if a.call("daemon_status").get("clients") == 1 and fds() <= baseline + 3:
            break
        time.sleep(0.2)
    assert a.call("daemon_status").get("clients") == 1, "clients table must drain"
    assert fds() <= baseline + 3, f"fd leak: baseline={baseline} now={fds()}"
    a.close()
