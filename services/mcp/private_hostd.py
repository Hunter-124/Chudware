"""Spawn a PRIVATE ``chudware-hostd`` so a tool run cannot touch the user's app.

``core_bridge`` defaults to daemon mode and attaches to whatever ``chudware-hostd`` is
already listening — on a developer box that is the daemon behind the user's open
Chudware window. Anything that mints documents (the MCP test suites, the ledger
replay) then fans out ``doc.opened``, the GUI opens a mirror tab per document, the
documents outlive the run, and a stale-version attach can DRAIN the daemon out from
under the window the user is working in. All three were observed live on 2026-07-28.

The invariant: **a batch process must never attach to a daemon it did not start.**

Ownership is assigned, never discovered:

* The binary is the one ``daemon_bridge.resolve_hostd_bin()`` selects for every other
  client (``CHUDWARE_HOSTD_BIN`` -> dist -> build trees). A private daemon built from a
  different tree reports a different build id, and the client's version drain-replace
  then drains it and respawns an unowned daemon in its place.
* ``core/hostd/main.cpp`` derives the singleton lock and the socket from
  ``instanceKey()``, and ``services/ipc/ipc_discovery.cpp`` returns an explicit
  ``CHUDWARE_INSTANCE_KEY`` verbatim. An inherited key (a gate run exports one) therefore
  gave the private daemon the same lock and socket as every other daemon started under
  that key: it lost the SingleInstance race and exited 0, or shared a socket path with a
  stale one. The child gets a fresh key and a socket inside its own temp dir instead.
* Readiness is that exact socket accepting a connection from the spawned pid. Scanning
  the shared runtime dir for "a new socket" adopted whichever daemon bound first.

Both overrides are developer-build switches; a binary that ignores them never answers on
the private socket, and the spawn reports failure instead of trusting it. The binary is
still COPIED into the temp dir so a rebuild landing mid-run cannot trip the daemon's
stale-binary self-drain.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from services.mcp.daemon_bridge import resolve_hostd_bin

# AF_UNIX sun_path is 108 bytes on Linux, including the terminating NUL.
_SUN_PATH_MAX = 107
_READY_TIMEOUT_S = 60.0

# Why the most recent spawn_private_daemon() call returned None ("" after a success).
last_failure = ""


def _listener_pid(path: str) -> int:
    """Pid of the process accepting on *path*; 0 while nothing accepts there."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(1.0)
        s.connect(path)
        peercred = getattr(socket, "SO_PEERCRED", None)
        if peercred is None:
            return -1  # accepting, owner unverifiable on this platform
        pid, _uid, _gid = struct.unpack(
            "3i", s.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i")))
        return pid
    except OSError:
        return 0
    finally:
        s.close()


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def spawn_private_daemon(tmp: Path) -> tuple[subprocess.Popen, str, str] | None:
    """Start a private hostd from a COPY of the selected binary in *tmp*.

    Returns ``(proc, socket_path, instance_key)``, or None when no daemon binary is
    built (a plain source checkout still runs the pure-Python paths, which is most of
    what callers want) or it failed to come up; ``last_failure`` then says why.
    """
    global last_failure
    hostd = resolve_hostd_bin()
    if hostd is None or not hostd.is_file():
        last_failure = ("no chudware-hostd binary (set CHUDWARE_HOSTD_BIN or build the "
                        f"daemon); resolved {hostd}")
        return None
    sock = str(tmp / "hostd.sock")
    if len(os.fsencode(sock)) > _SUN_PATH_MAX:
        last_failure = f"private socket path exceeds AF_UNIX limit: {sock}"
        return None
    for f in os.listdir(hostd.parent):
        if f.endswith(".so") or ".so." in f:
            shutil.copy2(hostd.parent / f, tmp)
    exe = tmp / "chudware-hostd"
    shutil.copy2(hostd, exe)
    exe.chmod(0o755)
    key = f"private-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    log = Path(tempfile.gettempdir()) / f"private-hostd-{key}.log"
    roots = {
        "HOME": tmp / "home",
        "USERPROFILE": tmp / "home",
        "XDG_DATA_HOME": tmp / "data",
        "XDG_CONFIG_HOME": tmp / "config",
        "XDG_CACHE_HOME": tmp / "cache",
        "XDG_RUNTIME_DIR": tmp / "run",
        "CHUDWARE_CACHE_ROOT": tmp / "agent-cache",
        "CHUDWARE_MFG_STATE_DIR": tmp / "mfg-state",
    }
    for directory in roots.values():
        directory.mkdir(mode=0o700, exist_ok=True)
    env = dict(os.environ, LD_LIBRARY_PATH=str(tmp),
               CHUDWARE_INSTANCE_KEY=key, CHUDWARE_HOSTD_SOCKET=sock,
               **{name: str(directory) for name, directory in roots.items()})
    env.pop("APPDATA", None)
    env.pop("CHUDWARE_HOSTD_PIPE", None)
    # The manufacturing journal takes an exclusive flock in its state dir
    # (core/hostd/mfg_lane.cpp defaultStateDirectory); without a private override the
    # spawned daemon collides with any already-running hostd (the web daemon, a gate
    # leftover) and aborts in the Journal constructor before it ever listens.
    # start_new_session: the daemon must be outside the test runner's process group.
    # The engine forks isolation children during heavy solves; without a private
    # session any group-directed signal from that subtree (or a terminal Ctrl+C
    # intended for the runner) lands on pytest as a phantom KeyboardInterrupt mid-RPC.
    with open(log, "wb") as err:
        proc = subprocess.Popen(
            [str(exe), "--foreground"], cwd=str(tmp), env=env,
            stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            last_failure = (f"private hostd from {hostd} exited {proc.returncode} "
                            f"before listening on {sock} (log {log})")
            return None
        pid = _listener_pid(sock)
        if pid in (proc.pid, -1):
            last_failure = ""
            return proc, sock, key
        if pid:
            _terminate(proc)
            last_failure = f"pid {pid}, not the spawned hostd {proc.pid}, answered on {sock}"
            return None
        time.sleep(0.1)
    _terminate(proc)
    last_failure = (f"private hostd from {hostd} did not accept on {sock} within "
                    f"{_READY_TIMEOUT_S:.0f} s (log {log})")
    return None


def _stop(proc: subprocess.Popen, tmp: Path) -> None:
    _terminate(proc)
    shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def private_daemon():
    """Point this process at a private daemon for the duration.

    ``CHUDWARE_HOSTD_SOCKET`` and ``CHUDWARE_INSTANCE_KEY`` both name the private
    daemon, so a reconnect-spawn or a supervised worker started from here stays on it
    instead of falling back to an inherited key or the binary-path key the user's app
    uses. Prior values are restored on exit.

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
    proc, sock, key = spawned
    owned = {"CHUDWARE_HOSTD_SOCKET": sock, "CHUDWARE_INSTANCE_KEY": key}
    saved = {name: os.environ.get(name) for name in owned}
    os.environ.update(owned)
    try:
        yield True
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _stop(proc, tmp)
