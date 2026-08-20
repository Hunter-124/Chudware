"""Spawn a PRIVATE ``chudware-hostd`` so a tool run cannot touch the user's app.

``core_bridge`` defaults to daemon mode and attaches to whatever ``chudware-hostd`` is
already listening — on a developer box that is the daemon behind the user's open
Chudware window. Anything that mints documents (the MCP test suites, the ledger
replay) then fans out ``doc.opened``, the GUI opens a mirror tab per document, the
documents outlive the run, and a stale-version attach can DRAIN the daemon out from
under the window the user is working in. All three were observed live on 2026-07-28.

The invariant: **a batch process must never attach to a daemon it did not start.**

``CHUDWARE_HOSTD_SOCKET`` alone does NOT isolate — ``core/hostd/main.cpp`` derives both
the lock and the socket path from ``instanceKey(selfPath())``, so a second daemon
launched from the same binary path loses the SingleInstance race and exits 0 without
ever listening. The binary is therefore COPIED to a temp dir: a different path is a
different instance key.
"""
from __future__ import annotations

import contextlib
import glob
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_BIN = _REPO / "core" / "build-occt-linux" / "bin"


def _sockets() -> set[str]:
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return set(glob.glob(f"{run}/chudware/*.sock"))


def spawn_private_daemon(tmp: Path) -> tuple[subprocess.Popen, str] | None:
    """Start a private hostd from a COPY of the binary in *tmp*.

    Returns ``(proc, socket_path)``, or None when no daemon binary is built (a plain
    source checkout still runs the pure-Python paths, which is most of what callers
    want) or it failed to come up.
    """
    hostd = _BIN / "chudware-hostd"
    if not hostd.is_file():
        return None
    for f in os.listdir(_BIN):
        if f == "chudware-hostd" or f.endswith(".so") or ".so." in f:
            shutil.copy2(_BIN / f, tmp)
    exe = tmp / "chudware-hostd"
    exe.chmod(0o755)
    before = _sockets()
    # The manufacturing journal takes an exclusive flock in its state dir
    # (core/hostd/mfg_lane.cpp defaultStateDirectory); without a private override the
    # spawned daemon collides with any already-running hostd (the web daemon, a gate
    # leftover) and aborts in the Journal constructor before it ever listens.
    # start_new_session: the daemon must be outside the test runner's process group.
    # The engine forks isolation children during heavy solves; without a private
    # session any group-directed signal from that subtree (or a terminal Ctrl+C
    # intended for the runner) lands on pytest as a phantom KeyboardInterrupt mid-RPC.
    proc = subprocess.Popen(
        [str(exe), "--foreground"], cwd=str(tmp),
        env=dict(os.environ, LD_LIBRARY_PATH=str(tmp),
                 CHUDWARE_MFG_STATE_DIR=str(tmp / "mfg-state")),
        stdout=subprocess.DEVNULL,
        stderr=open(Path(tempfile.gettempdir()) / f"private-hostd-{os.getpid()}.log", "wb"),
        start_new_session=True)
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


def _stop(proc: subprocess.Popen, tmp: Path) -> None:
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def private_daemon():
    """Point ``CHUDWARE_HOSTD_SOCKET`` at a private daemon for the duration.

    An explicit socket (a probe, CI, or a caller managing its own daemon) wins — it has
    already chosen what to talk to. Yields True when isolation is actually in force, so
    a caller can say so rather than assume it.
    """
    if os.environ.get("CHUDWARE_HOSTD_SOCKET") or os.environ.get("CHUDWARE_ENGINE_DAEMON") == "0":
        yield False
        return
    tmp = Path(tempfile.mkdtemp(prefix="chudware_private_hostd_"))
    spawned = spawn_private_daemon(tmp)
    if spawned is None:
        shutil.rmtree(tmp, ignore_errors=True)
        yield False
        return
    proc, sock = spawned
    os.environ["CHUDWARE_HOSTD_SOCKET"] = sock
    try:
        yield True
    finally:
        os.environ.pop("CHUDWARE_HOSTD_SOCKET", None)
        _stop(proc, tmp)
