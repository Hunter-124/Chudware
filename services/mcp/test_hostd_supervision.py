"""Phase-5 hostd child-supervision tests: agent_spawn/agent_stop/agent_list,
PDEATHSIG containment, mission.exited events, lifetime policy (no idle-exit
with children; drain refusal / --stop --force), per-child logs, spawn edges.

Each test uses a scratch CHUDWARE_INSTANCE_KEY (its own daemon, never the real
one) — the pattern from test_hostd_registry.py. Skips when chudware-hostd is not
built.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid

import pytest

from services.mcp import daemon_bridge
from services.mcp.daemon_bridge import DaemonClient

HOSTD = daemon_bridge.resolve_hostd_bin()

pytestmark = pytest.mark.skipif(HOSTD is None, reason="chudware-hostd not built")

PY = "python3"
SLEEPER = [PY, "-c", "import time; time.sleep(30)"]
# Both print "ready" once their SIGTERM disposition is installed — tests must
# wait for it before stopping, or the signal races interpreter startup.
TERM_CLEAN = [PY, "-c",
              "import signal,sys,time;"
              "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0));"
              "print('ready', flush=True);"
              "time.sleep(30)"]
TERM_IGNORE = [PY, "-c",
               "import signal,time;"
               "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
               "print('ready', flush=True);"
               "time.sleep(30)"]


def _wait_ready(spawn_result: dict, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with open(spawn_result["log"]) as fh:
                if "ready" in fh.read():
                    return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise AssertionError("child never signalled ready")


@pytest.fixture()
def scratch_daemon(monkeypatch, tmp_path):
    """Fresh instance key + fast linger; kills any daemon we started on teardown."""
    key = "pytest-sup-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("CHUDWARE_INSTANCE_KEY", key)
    monkeypatch.setenv("CHUDWARE_HOSTD_LINGER_SECS", "2")
    monkeypatch.setenv("CHUDWARE_AUTOSAVE_INTERVAL_S", "0")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CHUDWARE_HOSTD_SOCKET", raising=False)
    pids: list = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _pid(c: DaemonClient) -> int:
    return c.call("daemon_status")["pid"]


def _alive(pid: int) -> bool:
    """True while `pid` exists and is not a zombie. Daemons are Popen children
    of this process, so an exited daemon lingers as a zombie until reaped —
    reap opportunistically and count Z as gone (hostd_smoke.py wait_gone)."""
    try:
        os.waitpid(pid, os.WNOHANG)  # reap if it's our child (no-op otherwise)
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except (ProcessLookupError, FileNotFoundError):
        return False


def _wait_gone(pid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def _spawn(c: DaemonClient, argv, **kw) -> dict:
    r = c.call("agent_spawn", {"argv": argv, "env": {}, **kw})
    assert not r.get("_is_error"), r
    return r


def _events(c: DaemonClient, name: str) -> list:
    c.call("ping")  # drains the socket
    return [e for e in c.poll_events() if e.get("event") == name]


# ---------------------------------------------------------- spawn + containment


def test_spawn_list_and_pdeathsig(scratch_daemon):
    c = DaemonClient()
    c.connect()
    daemon_pid = _pid(c)
    scratch_daemon.append(daemon_pid)

    r = _spawn(c, SLEEPER, mission_id="m-pdeathsig")
    child_pid = r["pid"]
    assert _alive(child_pid)

    listed = c.call("agent_list")["children"]
    assert [x for x in listed if x["child_id"] == r["child_id"]
            and x["pid"] == child_pid and x["mission_id"] == "m-pdeathsig"
            and x["status"] == "running"]

    # Daemon SIGKILL (no chance to clean up) -> PDEATHSIG kills the child.
    os.kill(daemon_pid, signal.SIGKILL)
    assert _wait_gone(child_pid, 2.0), "PDEATHSIG did not kill the child within 2s"


def test_agent_stop_term_honored(scratch_daemon):
    c = DaemonClient()
    c.connect()
    scratch_daemon.append(_pid(c))
    r = _spawn(c, TERM_CLEAN, mission_id="m-term")
    _wait_ready(r)
    assert c.call("agent_stop", {"child_id": r["child_id"]}).get("ok") is True
    assert _wait_gone(r["pid"], 3.0)
    # Clean exit (the child's SIGTERM handler sys.exit(0)'d).
    deadline = time.monotonic() + 3.0
    exits = []
    while time.monotonic() < deadline and not exits:
        exits = [e for e in _events(c, "mission.exited")
                 if e["data"]["child_id"] == r["child_id"]]
        time.sleep(0.1)
    assert exits and exits[0]["data"]["exit_code"] == 0


def test_agent_stop_grace_kills_term_ignorer(scratch_daemon):
    c = DaemonClient()
    c.connect()
    scratch_daemon.append(_pid(c))
    r = _spawn(c, TERM_IGNORE)
    _wait_ready(r)
    c.call("agent_stop", {"child_id": r["child_id"]})  # TERM: ignored
    time.sleep(1.0)
    assert _alive(r["pid"]), "TERM-ignoring child died too early?"
    # The reaper enforces SIGKILL after the 5 s grace.
    assert _wait_gone(r["pid"], 7.0), "grace SIGKILL never landed"


def test_agent_stop_force_immediate(scratch_daemon):
    c = DaemonClient()
    c.connect()
    scratch_daemon.append(_pid(c))
    r = _spawn(c, TERM_IGNORE)
    _wait_ready(r)
    c.call("agent_stop", {"child_id": r["child_id"], "force": True})
    assert _wait_gone(r["pid"], 2.0)
    # Idempotent double stop.
    again = c.call("agent_stop", {"child_id": r["child_id"], "force": True})
    assert again.get("ok") is True


# ----------------------------------------------------------- events + lifetime


def test_mission_exited_event(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    b.call("subscribe", {"doc_ids": "*"})
    r = _spawn(a, [PY, "-c", "import sys; sys.exit(7)"], mission_id="m-exit7")
    deadline = time.monotonic() + 5.0
    got = []
    while time.monotonic() < deadline and not got:
        got = [e for e in _events(b, "mission.exited")
               if e["data"]["child_id"] == r["child_id"]]
        time.sleep(0.1)
    assert got, "subscribed client never saw mission.exited"
    d = got[0]["data"]
    assert d["exit_code"] == 7 and d["mission_id"] == "m-exit7" and d["pid"] == r["pid"]


def test_child_blocks_idle_exit(scratch_daemon):
    c = DaemonClient()
    c.connect()
    daemon_pid = _pid(c)
    scratch_daemon.append(daemon_pid)
    r = _spawn(c, [PY, "-c", "import time; time.sleep(5)"])
    st = c.call("daemon_status")
    assert st["children"] == 1
    c.close()
    # linger=2s: well past it the daemon must still be up (child running)...
    time.sleep(3.5)
    assert _alive(daemon_pid), "daemon idle-exited under a running child"
    assert _alive(r["pid"])
    # ...and once the child exits, the normal idle-exit resumes.
    assert _wait_gone(daemon_pid, 15.0), "daemon never idle-exited after child done"


def test_drain_refused_then_stop_force(scratch_daemon):
    c = DaemonClient()
    c.connect()
    daemon_pid = _pid(c)
    scratch_daemon.append(daemon_pid)
    r = _spawn(c, SLEEPER, mission_id="m-drain")
    st = c.call("daemon_status")
    assert st["children"] == 1 and st["missions"] == ["m-drain"]

    refused = c.call("daemon_drain")
    assert refused.get("_is_error") is True and refused.get("code") == "mission_active"
    assert _alive(r["pid"])

    c.close()
    env = dict(os.environ)
    out = subprocess.run([str(HOSTD), "--stop", "--force"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert _wait_gone(daemon_pid, 15.0)
    assert _wait_gone(r["pid"], 2.0)  # stopped + reaped before the drain


def test_stop_escalates_when_socket_wedged(scratch_daemon):
    """H1: --stop with a dead/unresponsive socket but live pidfile process must
    SIGTERM (then SIGKILL after 3s) instead of printing 'no running instance'
    and giving up. Simulate a wedged daemon with SIGSTOP (socket may still
    accept but the process never answers)."""
    c = DaemonClient()
    c.connect()
    daemon_pid = _pid(c)
    scratch_daemon.append(daemon_pid)
    c.close()

    # Freeze the daemon so control RPC cannot complete.
    os.kill(daemon_pid, signal.SIGSTOP)
    try:
        env = dict(os.environ)
        out = subprocess.run(
            [str(HOSTD), "--stop"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        # If --stop failed to kill, unfreeze so teardown SIGKILL works.
        try:
            os.kill(daemon_pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            pass

    assert out.returncode == 0, (out.stdout, out.stderr)
    combined = (out.stdout or "") + (out.stderr or "")
    assert "pidfile" in combined or "SIGTERM" in combined or "SIGKILL" in combined, combined
    assert _wait_gone(daemon_pid, 10.0), f"wedged daemon pid={daemon_pid} still alive"



def test_spawner_disconnect_keeps_child(scratch_daemon):
    """GUI-close continuity: the spawner dying does not touch the mission."""
    a = DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    r = _spawn(a, SLEEPER, mission_id="m-cont")
    a.close()
    time.sleep(0.5)
    assert _alive(r["pid"]), "child died when its spawner disconnected"
    b = DaemonClient()
    b.connect()
    listed = b.call("agent_list")["children"]
    assert [x for x in listed if x["child_id"] == r["child_id"]]
    b.call("agent_stop", {"child_id": r["child_id"], "force": True})


# ------------------------------------------------------------------- edges


def test_child_stdio_lands_in_log(scratch_daemon):
    c = DaemonClient()
    c.connect()
    scratch_daemon.append(_pid(c))
    marker = "hostd-log-" + uuid.uuid4().hex[:8]
    r = _spawn(c, [PY, "-c",
                   f"import sys; print('{marker}'); print('{marker}-err', file=sys.stderr)"])
    assert r["log"]
    deadline = time.monotonic() + 5.0
    text = ""
    while time.monotonic() < deadline:
        try:
            with open(r["log"]) as fh:
                text = fh.read()
        except FileNotFoundError:
            pass
        if marker in text and marker + "-err" in text:
            break
        time.sleep(0.1)
    assert marker in text and marker + "-err" in text


def test_bad_argv_clean_error_no_zombie(scratch_daemon):
    c = DaemonClient()
    c.connect()
    daemon_pid = _pid(c)
    scratch_daemon.append(daemon_pid)
    r = c.call("agent_spawn", {"argv": ["/nonexistent/agent-worker-xyz"], "env": {}})
    assert r.get("_is_error") is True and r.get("code") == "spawn_failed"
    assert c.call("agent_list")["children"] == []
    # No zombie left under the daemon.
    zombies = [t for t in os.listdir("/proc")
               if t.isdigit() and _ppid_stat(int(t)) == (daemon_pid, "Z")]
    assert zombies == []
    # Empty / missing argv is a clean error too.
    r2 = c.call("agent_spawn", {"argv": []})
    assert r2.get("_is_error") is True and r2.get("code") == "invalid_params"


@pytest.mark.parametrize("metadata", [
    {"mission_id": 0}, {"mission_id": []}, {"mission_id": {}}, {"mission_id": None},
    {"doc_id": 0}, {"doc_id": []}, {"doc_id": {}}, {"doc_id": None},
    {"env": []}, {"env": {"KEY": 4}}, {"env": {"A=B": "value"}},
    {"env": {"KEY": "bad\0value"}}, {"mission_id": "bad\0value"},
])
def test_bad_spawn_metadata_never_launches_child(scratch_daemon, tmp_path, metadata):
    c = DaemonClient()
    c.connect()
    daemon_pid = _pid(c)
    scratch_daemon.append(daemon_pid)
    marker = tmp_path / "must-not-launch"
    before = c.call("agent_list")["children"]
    r = c.call("agent_spawn", {
        "argv": [PY, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)],
        **metadata,
    })
    assert r.get("_is_error") is True and r.get("code") == "invalid_params", r
    assert c.call("ping").get("ok") is True
    assert _pid(c) == daemon_pid
    assert c.call("agent_list")["children"] == before
    assert not list((tmp_path / "state").rglob("agent-*.log"))
    time.sleep(0.15)
    assert not marker.exists()
    c.close()


@pytest.mark.parametrize("argv", [[""], [0], [["nested"]], ["python3", "bad\0arg"]])
def test_invalid_argv_returns_parameter_error(scratch_daemon, argv):
    c = DaemonClient()
    c.connect()
    scratch_daemon.append(_pid(c))
    r = c.call("agent_spawn", {"argv": argv})
    assert r.get("_is_error") is True and r.get("code") == "invalid_params", r
    assert c.call("agent_list")["children"] == []
    assert c.call("ping").get("ok") is True
    c.close()


def _ppid_stat(pid: int):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            rest = fh.read().split(")")[-1].split()
            return int(rest[1]), rest[0]  # (ppid, state)
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return (None, None)
