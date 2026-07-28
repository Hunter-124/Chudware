"""Give the MCP test suites their own engine daemon, so they cannot touch the user's app.

`core_bridge` defaults to daemon mode and connects to whatever `chudware-hostd` is already
listening — which on a developer box is the daemon behind the user's open Chudware window.
Three things then went wrong, all observed live on 2026-07-28:

  * Every part a test minted fanned out `doc.opened`, and the GUI opened a mirror tab for
    it. One `pytest services/mcp` run buried the user's tab strip in "AI: SwingArm",
    "AI: SwingArm2" ... "AI: SwingArm7", plus plate7/9/10, atom/atom2, sk/sk2 and ~30 more.
  * Those documents outlive the run, so the NEXT run's `new_part("SwingArm")` collided with
    a stale one and got a uniquified "SwingArm8" — the duplication compounding itself.
  * A run started right after `./build.sh daemon` saw a daemon older than the binary on
    disk and, per the bridge's version drain-replace, told it to DRAIN — killing the daemon
    out from under the window the user was working in.

The invariant that prevents all three is simply: a test process must never attach to a
daemon it did not start. Enforcing it here rather than in the daemon keeps hostd's wire
semantics untouched — `services/mcp/test_hostd_registry.py` and friends assert real
`origin_kind`, fanout and drain-replace behaviour, and must keep seeing exactly that.

`CHUDWARE_HOSTD_SOCKET` alone does NOT isolate: `core/hostd/main.cpp` derives both the lock
and the socket path from `instanceKey(selfPath())`, so a second daemon launched from the
same binary path loses the SingleInstance race and exits 0 without ever listening. The
binary is therefore COPIED to a temp dir — a different path is a different instance key.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BIN = _REPO / "core" / "build-occt-linux" / "bin"


def _sockets() -> set:
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return set(glob.glob(f"{run}/chudware/*.sock"))


def _spawn_private_daemon(tmp: Path):
    """Start a private hostd from a COPY of the binary. Returns (proc, socket) or None."""
    hostd = _BIN / "chudware-hostd"
    if not hostd.is_file():
        return None
    for f in os.listdir(_BIN):
        if f == "chudware-hostd" or f.endswith(".so") or ".so." in f:
            shutil.copy2(_BIN / f, tmp)
    exe = tmp / "chudware-hostd"
    exe.chmod(0o755)
    before = _sockets()
    proc = subprocess.Popen(
        [str(exe), "--foreground"], cwd=str(tmp),
        env=dict(os.environ, LD_LIBRARY_PATH=str(tmp)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        new = _sockets() - before
        if new:
            return proc, new.pop()
        time.sleep(0.1)
    proc.terminate()
    return None


@pytest.fixture(scope="session", autouse=True)
def _private_engine_daemon():
    # An explicit socket (a probe, CI, or a test that manages its own daemon) wins:
    # the caller has already chosen what to talk to.
    if os.environ.get("CHUDWARE_HOSTD_SOCKET") or os.environ.get("CHUDWARE_ENGINE_DAEMON") == "0":
        yield
        return
    tmp = Path(tempfile.mkdtemp(prefix="chudware_test_hostd_"))
    spawned = _spawn_private_daemon(tmp)
    if spawned is None:
        # No daemon binary built (or it failed to come up). Fall through to whatever the
        # bridge would have done — a plain source checkout still runs the pure-Python
        # normalizer tests, which is most of this directory.
        shutil.rmtree(tmp, ignore_errors=True)
        yield
        return
    proc, sock = spawned
    os.environ["CHUDWARE_HOSTD_SOCKET"] = sock
    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
