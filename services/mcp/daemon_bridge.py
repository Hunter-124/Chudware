"""Client for the shared engine-host daemon (chudware-hostd).

The Python mirror of services/ipc/ipc_client.cpp: connect-or-spawn against the
instance-key socket, newline-delimited JSON-RPC, one outstanding request per
connection (Lock-serialized). Calling threads own separate lazy connections, all
sharing one process writer identity. core_bridge
routes through here BY DEFAULT since the Phase-8 flip (unset or
CHUDWARE_ENGINE_DAEMON=1; only an explicit =0 selects the legacy in-process
path), keeping its call() signature so the ~200 tool call sites never change.

Semantics preserved vs the legacy in-process ctypes path:
  * call() BLOCKS with no client timeout (solves run minutes; ctypes had none).
  * The engine loads asynchronously in the daemon; a {"code":"loading"} reply is
    absorbed here by polling daemon_status until ready (bounded), so callers
    still see the old "load blocks until done" behavior.
  * A lost reply is outcome-unknown: never replay that RPC. Only a subsequent
    explicit call reconnects; callers must recover document identity explicitly.
  * Daemon push events (doc.changed etc., Phase 3) buffer in-process; MCP tool
    layers drain them via poll_events() to surface _foreign_edits notes.
  * A draining hostd (engine rebuilt on disk, or a stop) refuses new clients with a
    daemon_draining hello error -> DaemonDraining. One daemon serves each binary
    path, so no successor can start beside it; core_bridge never falls back to an
    in-process engine on this refusal.

Discovery mirrors ipc_discovery.cpp / ipc_discovery_win.cpp — keep them in sync:
  key    = $CHUDWARE_INSTANCE_KEY or fnv1a64(realpath(hostd binary))
           (Windows: fnv1a64 of the resolved path LOWERCASED — NTFS is
           case-insensitive and the C++ side lowercases before hashing)
  POSIX  = $CHUDWARE_HOSTD_SOCKET or $XDG_RUNTIME_DIR/chudware/hostd-<key>.sock
           (fallback /tmp/chudware-<uid>/hostd-<key>.sock)
  win32  = $CHUDWARE_HOSTD_PIPE/$CHUDWARE_HOSTD_SOCKET or
           \\\\.\\pipe\\chudware-hostd-<SID>-<key>

Windows transport choice (documented per the Phase 6 plan): the pipe is opened
with the plain builtin ``open(path, 'r+b', buffering=0)``. CPython never
shipped AF_UNIX on Windows, but a duplex byte-mode named pipe behaves as a
regular file handle: unbuffered ``write()`` maps to WriteFile, ``read(n)`` maps
to ReadFile and blocks until >=1 byte then returns what is available — exactly
the recv()-shaped semantics ``_read_msg`` needs. A ctypes CreateFileW path
(for overlapped/timeout control) is deliberately NOT used: call() has no client
timeout by design (solves run minutes), so blocking reads are the contract, and
the simple route has no handle-lifetime or GIL-during-Overlapped pitfalls.
Escalate to ctypes only if a real Windows box shows edge semantics (e.g.
ERROR_PIPE_BUSY storms) that open() cannot express.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from services.agent.request_context import current_context, in_cell, owner_key
import time
import uuid
from pathlib import Path
from typing import Iterator, Optional


class DaemonError(RuntimeError):
    """Transport-level daemon failure (connect/spawn/protocol)."""


class DaemonSessionLost(DaemonError):
    """The established engine connection is unusable; never switch engines."""

    code = "engine_connection_lost"

    def __init__(self, message: str, method: str, outcome: str = "unknown"):
        super().__init__(
            f"{method}: engine connection lost; outcome {outcome}. {message}"
        )
        self.detail = {"method": method, "outcome": outcome, "retryable": False}


class DaemonDraining(DaemonError):
    """hostd refused this new client because it drains (see Server::admitWhileDraining).

    One daemon serves each hostd binary path, so no successor can start until the
    draining daemon exits. This is never a reason to start a private in-process
    engine: core_bridge raises it as CoreError ``daemon_draining``.
    """

    code = "daemon_draining"

    def __init__(self, refusal: dict):
        detail = {k: v for k, v in refusal.items() if k not in ("_is_error", "event")}
        detail["code"] = self.code
        reason = str(detail.get("reason") or "")
        pid = detail.get("daemon_pid")
        who = f"chudware-hostd (pid {pid})" if pid else "chudware-hostd"
        if reason == "stale_binary":
            changed = Path(str(detail.get("changed_path") or "")).name or "an engine binary"
            active = detail.get("active_clients")
            sessions = f"its {active} active session(s)" if active is not None else "its open sessions"
            text = (
                f"{who} is draining after an engine rebuild ({changed} changed on disk). "
                f"It refuses new clients until {sessions} end. "
                "Restart Chudware to load the new engine. "
                "Do not set CHUDWARE_ENGINE_DAEMON=0: an in-process engine does not share documents."
            )
        elif reason:
            text = " ".join(str(part) for part in (
                f"{who} is draining ({reason}) and refuses new clients.",
                detail.get("message") or "",
                detail.get("hint") or "",
            ) if part)
        else:
            # The daemon.draining event alone names no reason (a daemon that closed the
            # connection before it answered hello).
            text = (
                f"{who} is draining after an engine rebuild or a stop request, and refuses "
                "new clients. Wait until it exits, or restart Chudware to load the new engine."
            )
        super().__init__(text)
        self.detail = detail


def _fnv1a64_hex(s: str) -> str:
    # Offset basis MUST byte-match chudware_ipc::fnv1aHex (services/ipc/ipc_discovery.cpp),
    # which — like every other FNV site in the C++ tree — uses 1469598103934665603
    # (the textbook 14695981039346656037 with its last digit dropped). It's a
    # rendezvous protocol constant: agreement with the deployed binaries is what
    # matters, not textbook correctness. Using the standard basis here made the
    # MCP compute a different hostd socket than the GUI/daemon, splitting the
    # world into two engines and silently killing live doc mirroring.
    h = 1469598103934665603
    for b in s.encode():
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


class _PipeIO:
    """socket-shaped adapter over a Windows named-pipe file handle so
    DaemonClient's wire I/O is transport-agnostic (see module docstring for why
    plain open() is the chosen transport)."""

    def __init__(self, path: str) -> None:
        self._f = open(path, "r+b", buffering=0)

    def sendall(self, data: bytes) -> None:
        self._f.write(data)

    def recv(self, n: int) -> bytes:
        # Raw ReadFile: blocks until at least one byte, returns up to n. A
        # server-side close raises BrokenPipeError (ERROR_BROKEN_PIPE) — map to
        # the b"" EOF convention _read_msg expects from sockets.
        try:
            return self._f.read(n) or b""
        except (BrokenPipeError, OSError):
            return b""

    def close(self) -> None:
        try:
            self._f.close()
        except OSError:
            pass


def _repo_root() -> Path:
    # .../services/mcp/daemon_bridge.py -> repo root (also correct for the
    # installed services/mcp/ package layout, where core/ sits beside it).
    return Path(__file__).resolve().parent.parent.parent


def resolve_hostd_bin() -> Optional[Path]:
    """Locate the chudware-hostd binary. Mirrors the libchudware candidate order in
    core_bridge (env -> dist -> full OCCT build -> slim build)."""
    if p := os.environ.get("CHUDWARE_HOSTD_BIN"):
        return Path(p)
    exe = "chudware-hostd.exe" if sys.platform == "win32" else "chudware-hostd"
    root = _repo_root()
    if sys.platform == "win32":
        cands = (
            Path(os.environ.get("CHUDWARE_DIST") or "nonexistent") / exe,
            root / "core" / "build" / "bin" / exe,  # build.bat / MSVC preset dir
        )
    else:
        cands = (
            Path(os.environ.get("CHUDWARE_DIST") or "nonexistent") / exe,
            root / "core" / "build-occt-linux" / "bin" / exe,
            root / "core" / "build-linux" / "bin" / exe,
        )
    for cand in cands:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def socket_path(hostd_bin: Optional[Path]) -> Optional[str]:
    """The daemon endpoint: unix-socket path on POSIX, \\\\.\\pipe\\... name on
    win32 (transport-neutral name, matching ipc_discovery's socketPath())."""
    if sys.platform == "win32":
        if p := os.environ.get("CHUDWARE_HOSTD_PIPE"):
            return p
    if p := os.environ.get("CHUDWARE_HOSTD_SOCKET"):
        return p
    key = os.environ.get("CHUDWARE_INSTANCE_KEY")
    if not key:
        if not hostd_bin:
            return None
        resolved = str(Path(hostd_bin).resolve())
        if sys.platform == "win32":
            resolved = resolved.lower()  # NTFS case-insensitive; C++ side lowercases too
        key = _fnv1a64_hex(resolved)
    if sys.platform == "win32":
        from services.ipc.windows_pipe import current_user_sid
        try:
            sid = current_user_sid()
        except OSError as exc:
            raise DaemonError(f"Windows user identity unavailable: {exc}") from exc
        return f"\\\\.\\pipe\\chudware-hostd-{sid}-{key}"
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        return f"{xdg}/chudware/hostd-{key}.sock"
    # No XDG_RUNTIME_DIR (common for agent-spawned processes): fall back to the
    # canonical /run/user/<uid> BEFORE /tmp so every client resolves the SAME
    # directory as desktop-launched ones — otherwise two "singleton" daemons
    # coexist on different sockets. Mirrors ipc_discovery.cpp runtimeDir().
    run_user = f"/run/user/{os.getuid()}"
    try:
        st = os.lstat(run_user)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
            return f"{run_user}/chudware/hostd-{key}.sock"
    except OSError:
        pass
    return f"/tmp/chudware-{os.getuid()}/hostd-{key}.sock"


def _pid_alive(pid: int) -> bool:
    """Liveness for a daemon pid, treating a ZOMBIE as dead. The daemon is
    spawned as OUR direct child (single fork + setsid), so after it exits it
    lingers as a zombie until reaped — os.kill(pid, 0) alone would report it
    alive forever. POSIX only (the Windows path polls the pipe name instead)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[-1].split()[0] == "Z":
                try:
                    os.waitpid(pid, os.WNOHANG)  # reap if it's our child
                except ChildProcessError:
                    pass
                return False
    except OSError:
        pass  # no /proc (macOS): fall through to "alive"; bounded by the deadline
    return True


_DISK_BUILD_ID_CACHE: dict = {}  # str(path) -> (mtime_ns, build_id)


def _disk_build_id(hostd: Optional[Path]) -> str:
    """Build id of the hostd binary ON DISK (`chudware-hostd --version` prints
    "chudware-hostd <git-describe id>"). Cached per (path, mtime) so the subprocess
    runs once per binary change per process — steady-state connects are free.
    Returns "" on any failure (treated as not-stale). Client half of the version
    drain-replace: compared against hello.git_rev, the RUNNING daemon's id."""
    if not hostd:
        return ""
    try:
        key = str(hostd)
        mtime = os.stat(key).st_mtime_ns
        hit = _DISK_BUILD_ID_CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        out = subprocess.run([key, "--version"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        bid = out.split()[-1] if out else ""
        _DISK_BUILD_ID_CACHE[key] = (mtime, bid)
        return bid
    except Exception:
        return ""


def daemon_socket_alive(hostd_bin: Optional[Path] = None) -> bool:
    """Cheap liveness probe — used by the legacy path's mode-divergence warning."""
    sp = socket_path(hostd_bin or resolve_hostd_bin())
    if not sp:
        return False
    if sys.platform == "win32":
        # A named pipe exists exactly while a server instance is listening.
        return os.path.exists(sp)
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(0.25)
        s.connect(sp)
        s.close()
        return True
    except OSError:
        return False


# Every hostd we ever spawn, so exited children (e.g. flock-singleton race
# losers) get reaped instead of lingering as zombies in this process — 12 such
# zombies were observed parented to MCP servers (docs/review/PERF_AUDIT_2026-07-11.md §1).
_SPAWNED: list = []


def _reap_spawned() -> None:
    _SPAWNED[:] = [p for p in _SPAWNED if p.poll() is None]


def _watch_spawned(p: "subprocess.Popen") -> "subprocess.Popen":
    """Track a spawned hostd AND reap it the moment it exits. The polling
    _reap_spawned() only runs on the next connect, so a flock-race loser or a
    drained daemon used to sit <defunct> in this process for its whole life;
    a dedicated wait() thread collects it immediately."""
    _SPAWNED.append(p)
    threading.Thread(target=p.wait, name="chudware-hostd-reaper", daemon=True).start()
    return p


def _spawn_hostd_detached(hostd: Path) -> "subprocess.Popen":
    """Launch the daemon so it outlives this client.

    POSIX: start_new_session detaches enough for a daemon that self-manages via
    its own flock singleton; an orphaned child reparents to init.

    Windows: DETACHED_PROCESS (no console) + CREATE_NEW_PROCESS_GROUP, and try
    CREATE_BREAKAWAY_FROM_JOB first — MCP under Claude Code often runs inside a
    KILL_ON_JOB_CLOSE Job Object, and a daemon left inside it dies with the
    agent session (plan Top-risk 1). If the job denies breakaway
    (PermissionError / ERROR_ACCESS_DENIED), retry contained with a loud
    warning: autosave bounds the loss and the next client respawns."""
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        try:
            p = subprocess.Popen([str(hostd)], creationflags=flags | CREATE_BREAKAWAY_FROM_JOB,
                                 close_fds=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return _watch_spawned(p)
        except OSError:
            print(
                "[daemon_bridge] WARNING: job breakaway denied — chudware-hostd will die "
                "with this process's Job Object (e.g. when the agent session ends). "
                "Start the Chudware GUI first for a durable daemon.",
                file=sys.stderr,
            )
            p = subprocess.Popen([str(hostd)], creationflags=flags, close_fds=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            return _watch_spawned(p)
    p = subprocess.Popen(
        [str(hostd)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return _watch_spawned(p)


class DaemonClient:
    """One connection to chudware-hostd. Thread-safe; never replays a lost reply."""

    ENGINE_READY_BUDGET_S = 300.0  # cold OCCT load headroom
    # Spawn gate (see connect()): once a spawn+connect cycle has failed, refuse to
    # fork another hostd until this window closes, doubling per consecutive
    # failure. 5 s is long enough that a tool-call loop can't out-run it, 60 s
    # short enough that a user who fixes the cause isn't locked out for long.
    SPAWN_COOLDOWN_S = 5.0
    SPAWN_COOLDOWN_MAX_S = 60.0

    def __init__(self, *, writer_group: str = "") -> None:
        self.writer_group = writer_group
        self.owner = owner_key()
        self._lock = threading.Lock()
        # AF_UNIX socket on POSIX, _PipeIO on win32 (same sendall/recv/close shape).
        self._sock: Optional[object] = None
        self._buf = bytearray()
        self._buf_start = 0  # first unconsumed byte
        self._buf_scan = 0  # first byte not yet searched for a newline
        self._next_id = 1
        self._events: list[dict] = []
        self.server_info: dict = {}
        self._sock_path: Optional[str] = None
        self._stale_retry_done = False  # one drain-replace attempt per client
        self._spawn_block_until = 0.0  # monotonic deadline; 0 = spawning allowed
        self._spawn_fail_streak = 0
        self._session_established = False

    # ---------------------------------------------------------------- connect

    def connect(self, spawn: bool = True, budget_s: float = 15.0) -> None:
        try:
            self._connect(spawn, budget_s)
        except DaemonDraining:
            # Nothing was dispatched and the refusal names its cause: never relabel it as
            # a lost session or a plain transport failure (core_bridge must not fall back).
            self._disconnect()
            raise
        except (DaemonError, OSError) as e:
            self._disconnect()
            if self._session_established or (
                self.writer_group == _WRITER_GROUP and _group_session_established.is_set()
            ):
                raise DaemonSessionLost(str(e), "connect", "not_dispatched") from e
            raise DaemonError(str(e)) from e

    def _connect(self, spawn: bool, budget_s: float) -> None:
        if in_cell():
            owner_key()
            spawn = False  # an actor may connect only to its admitted hostd
        _reap_spawned()
        hostd = resolve_hostd_bin()
        self._sock_path = socket_path(hostd)
        if not self._sock_path:
            raise DaemonError("cannot resolve hostd socket (no binary / CHUDWARE_HOSTD_BIN)")
        try:
            budget_s = float(os.environ.get("CHUDWARE_HOSTD_CONNECT_BUDGET_S", "") or budget_s)
        except ValueError:
            pass
        deadline = time.monotonic() + budget_s
        backoff = 0.05
        child = None
        child_gone_at = 0.0
        spawn_tried = False
        try:
            while True:
                try:
                    if sys.platform == "win32":
                        # FileNotFoundError = no server instance yet (spawn/retry);
                        # ERROR_PIPE_BUSY surfaces as OSError and is retried the same
                        # way — the daemon creates the next instance right after each
                        # accept, so busy is a sub-millisecond window.
                        self._sock = _PipeIO(self._sock_path)
                    else:
                        s = socket.socket(socket.AF_UNIX)
                        s.connect(self._sock_path)
                        self._sock = s
                    self._buf.clear()
                    self._buf_start = self._buf_scan = 0
                    break
                except OSError as e:
                    if not spawn:
                        raise DaemonError(f"hostd not running ({e})") from e
                    if child is None:
                        # Spawn gate. call() re-enters connect(spawn=True) for EVERY
                        # RPC whose socket is None, and the budget/backoff in this
                        # loop is per-call — so an agent loop hammering MCP tools at
                        # a hostd that dies on startup used to fork a fresh daemon
                        # per tool call. Once a spawn has failed, fail the call fast
                        # until the cooldown expires instead of forking again.
                        # Attaching to a healthy daemon is never gated: this branch
                        # is only reached when the plain connect already failed.
                        left = self._spawn_block_until - time.monotonic()
                        if left > 0:
                            raise DaemonError(
                                f"chudware-hostd failed to start "
                                f"{self._spawn_fail_streak}x; not retrying for another "
                                f"{left:.0f}s ({e}). See "
                                "~/.local/share/Chudware/logs/hostd.log"
                            ) from e
                        if not hostd:
                            raise DaemonError(
                                "chudware-hostd binary not found (set CHUDWARE_HOSTD_BIN or build core)"
                            ) from e
                        spawn_tried = True
                        try:
                            child = _spawn_hostd_detached(hostd)
                        except OSError as se:
                            raise DaemonError(f"cannot spawn {hostd}: {se}") from se
                        child_spawned_at = time.monotonic()
                    else:
                        # Broken-rendezvous fast-fail (observed in the wild: a daemon
                        # holds the singleton flock but serves no socket). Two shapes:
                        # our spawn EXITED (lost the flock race) and the incumbent's
                        # socket still refuses after a 2 s grace, or our spawn is
                        # ALIVE but stuck waiting on the flock — a healthy cold spawn
                        # binds its socket well under 5 s (bind precedes OCCT load).
                        now = time.monotonic()
                        exited = child.poll() is not None
                        if exited and not child_gone_at:
                            child_gone_at = now
                        if (exited and now - child_gone_at > 2.0) or (
                                not exited and now - child_spawned_at > 5.0):
                            raise DaemonError(
                                "hostd rendezvous broken: a daemon holds the singleton "
                                f"lock but its socket is unreachable ({e}). "
                                "`pkill chudware-hostd` and retry."
                            ) from e
                    if time.monotonic() > deadline:
                        raise DaemonError(f"hostd spawn timed out ({e})") from e
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 0.5)
        except DaemonError:
            # Only a cycle that actually forked a daemon arms the cooldown. An
            # attach-only failure (spawn=False, no binary, or the gate itself)
            # must never extend it, or the gate could never reopen.
            if spawn_tried:
                self._note_spawn_failed()
            raise
        # Any accepted socket — spawned or attached — proves the daemon is up, so
        # disarm the gate: a much later death should get the full 5 s window back,
        # not the remainder of a stale one. Safe by construction (the gate only
        # ever guards a path reached after a connect FAILED).
        self._spawn_fail_streak = 0
        self._spawn_block_until = 0.0
        self.server_info = self._hello()
        self._session_established = True
        if self.writer_group == _WRITER_GROUP:
            _group_session_established.set()
        self._drain_replace_if_stale(hostd, spawn)

    def _hello(self) -> dict:
        """Send hello; a draining daemon answers with a ``daemon_draining`` refusal.

        hostd also pushes a ``daemon.draining`` event before it closes the connection,
        so a transport failure after that event is the same refusal, not a lost daemon.
        """
        try:
            params = {"client": "mcp", "pid": os.getpid(), "proto": 1}
            ctx = current_context()
            if ctx.get("cell"):
                params["user"] = {"id": ctx["actor_key"]}
                params["actor_key"] = ctx["actor_key"]
                params["writer_group"] = repr(owner_key())
            if self.writer_group and not ctx.get("cell"):
                params["writer_group"] = self.writer_group
            info = self._call_raw("hello", params)
        except DaemonError:
            event = self._draining_event()
            if event is not None:
                raise DaemonDraining(event) from None
            raise
        if info.get("_is_error"):
            if info.get("code") == "daemon_draining":
                raise DaemonDraining(info)
            raise DaemonError(
                f"hostd refused hello ({info.get('code') or 'error'}): "
                f"{info.get('message') or info}"
            )
        return info

    def _draining_event(self) -> Optional[dict]:
        """The ``daemon.draining`` event a refusing hostd pushed before it closed, if any.

        The send of hello can fail on the closed peer before the event was read, so
        read what is still queued on the socket first, without blocking.
        """
        sock = self._sock
        if isinstance(sock, socket.socket):
            try:
                while True:
                    chunk = sock.recv(65536, socket.MSG_DONTWAIT)
                    if not chunk:
                        break
                    self._buf.extend(chunk)
            except OSError:
                pass  # nothing queued, or the reset that follows the queued bytes
        for msg in self._events:
            if msg.get("event") == "daemon.draining":
                return msg
        return next(
            (msg for msg in self._buffered_messages()
             if msg.get("event") == "daemon.draining"),
            None,
        )

    def _note_spawn_failed(self) -> None:
        """Arm/extend the spawn cooldown after a spawn+connect cycle failed."""
        self._spawn_fail_streak += 1
        wait = min(self.SPAWN_COOLDOWN_S * 2 ** (self._spawn_fail_streak - 1),
                   self.SPAWN_COOLDOWN_MAX_S)
        self._spawn_block_until = time.monotonic() + wait
        print(
            f"[daemon_bridge] chudware-hostd spawn failed ({self._spawn_fail_streak}x) "
            f"— refusing further spawn attempts for {wait:.0f}s",
            file=sys.stderr,
        )

    def _drain_replace_if_stale(self, hostd: Optional[Path], spawn: bool) -> None:
        """Version drain-replace (Phase 8): the running daemon predates the hostd
        binary on disk (dev rebuild / installer upgrade) -> ask it to drain
        (autosaves + exits), wait for the old pid to die, then respawn/reconnect
        so everyone lands on the new build. A mission_active refusal is
        warn-and-continue: never kill running agent work from a connect."""
        if not spawn or self._stale_retry_done:
            return
        running = str(self.server_info.get("git_rev") or "")
        disk = _disk_build_id(hostd)
        if not running or not disk or "unknown" in (running, disk) or running == disk:
            return
        self._stale_retry_done = True  # one attempt per connection, never a loop
        r = self._call_raw("daemon_drain", {})
        if r.get("_is_error"):
            print(
                f"[daemon_bridge] WARNING: hostd on disk is {disk} but the running "
                f"daemon is {running}; drain refused ({r.get('code')}) — continuing "
                "on the old daemon.",
                file=sys.stderr,
            )
            return
        print(
            f"[daemon_bridge] hostd stale (running {running}, on disk {disk}) — "
            "drained; respawning the new build.",
            file=sys.stderr,
        )
        old_pid = int(self.server_info.get("daemon_pid") or 0)
        if self._sock:
            self._sock.close()
            self._sock = None
        deadline = time.monotonic() + 30.0
        while old_pid > 0 and time.monotonic() < deadline:
            if sys.platform == "win32":
                # os.kill(pid, 0) is NOT a liveness probe on Windows; the pipe
                # name existing exactly while a server listens is.
                if not (self._sock_path and os.path.exists(self._sock_path)):
                    break
            else:
                if not _pid_alive(old_pid):
                    break
            time.sleep(0.1)
        self.connect()

    def connected(self) -> bool:
        return self._sock is not None

    def close(self) -> None:
        with self._lock:
            self._disconnect()

    def _disconnect(self) -> None:
        """Drop a failed connection while retaining its established-session history."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._buf.clear()
        self._buf_start = self._buf_scan = 0
        self._events.clear()

    # ---------------------------------------------------------------- wire I/O

    def _buffered_messages(self) -> Iterator[dict]:
        """Inspect unread lines without consuming them, including an EOF tail."""
        start = self._buf_start
        while start < len(self._buf):
            nl = self._buf.find(b"\n", start)
            end = nl if nl >= 0 else len(self._buf)
            try:
                msg = json.loads(self._buf[start:end])
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            else:
                if isinstance(msg, dict):
                    yield msg
            start = end + 1

    def _read_msg(self) -> dict:
        assert self._sock is not None
        while True:
            nl = self._buf.find(b"\n", self._buf_scan)
            if nl >= 0:
                if self._buf_start == 0 and nl == len(self._buf) - 1:
                    # JSON accepts the framing newline as trailing whitespace:
                    # hand off a whole reply without copying its byte array.
                    line, self._buf = self._buf, bytearray()
                    self._buf_start = self._buf_scan = 0
                else:
                    line = self._buf[self._buf_start:nl]
                    self._buf_start = self._buf_scan = nl + 1
                    if self._buf_start == len(self._buf):
                        self._buf.clear()
                        self._buf_start = self._buf_scan = 0
                    elif self._buf_start >= 65536 and self._buf_start >= len(self._buf) // 2:
                        # Compact geometrically, not once per line. Replacing the
                        # array releases the capacity of a large consumed reply.
                        self._buf = self._buf[self._buf_start:]
                        self._buf_start = self._buf_scan = 0
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate stray lines
            self._buf_scan = len(self._buf)
            try:
                chunk = self._sock.recv(65536)
            except OSError as e:
                # e.g. ECONNRESET from a draining daemon that closed on accept.
                raise DaemonError(f"daemon connection lost ({e})") from e
            if not chunk:
                raise DaemonError("daemon connection closed")
            self._buf.extend(chunk)

    def _call_raw(self, method: str, params: dict) -> dict:
        """One round trip, lock held by caller-or-self. Raises DaemonError on
        transport failure; returns the response's result OR error payload with
        an '_is_error' marker (core_bridge maps that to CoreError)."""
        assert self._sock is not None
        rid = self._next_id
        self._next_id += 1
        req = json.dumps({"id": rid, "method": method, "params": params})
        try:
            self._sock.sendall(req.encode() + b"\n")
        except OSError as e:
            raise DaemonError(f"daemon connection lost ({e})") from e
        while True:
            msg = self._read_msg()
            if "event" in msg:
                # Bounded: nothing drains _events unless poll_events() is wired
                # up, so a chatty daemon must not grow this list forever.
                if len(self._events) >= 1000:
                    del self._events[:100]
                self._events.append(msg)
                continue
            if msg.get("id") == rid:
                if "result" in msg:
                    return msg["result"]
                err = msg.get("error") or {}
                if isinstance(err, dict):
                    err["_is_error"] = True
                    return err
                return {"_is_error": True, "message": str(err)}

    def _peer_closed_idle(self) -> bool:
        """True only when hostd announced a clean exit (idle / soft drain) and then closed this socket.

        Non-blocking: whatever the daemon pushed meanwhile (events) is appended to the
        read buffer, so _read_msg still sees it in order. An idle exit happens only when
        this client had nothing attached and nothing in flight, so reconnecting loses no
        session state. A close WITHOUT that announcement (crash, kill, hard drain) keeps
        the loud path: the next call reports the lost session and is never replayed.
        Windows named pipes have no cheap probe and keep the loud path too.
        """
        sock = self._sock
        if not isinstance(sock, socket.socket):
            return False
        closed = False
        try:
            while True:
                chunk = sock.recv(65536, socket.MSG_DONTWAIT)
                if not chunk:
                    closed = True
                    break
                self._buf.extend(chunk)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            closed = True
        if not closed:
            return False
        for msg in self._buffered_messages():
            if (msg.get("event") == "daemon.exiting"
                    and msg.get("reason") in ("idle", "drained")):
                return True
        return False

    # ---------------------------------------------------------------- public

    def refresh_server_info(self) -> dict:
        """Refresh hello metadata without changing this connection's admitted identity."""
        if self.owner != owner_key():
            raise DaemonError("not_found")
        with self._lock:
            if self._sock is not None and self._peer_closed_idle():
                self._disconnect()
            if self._sock is None:
                self.connect()
                return self.server_info
            try:
                self.server_info = self._hello()
                return self.server_info
            except DaemonDraining:
                self._disconnect()
                raise
            except (DaemonError, OSError) as e:
                self._disconnect()
                raise DaemonSessionLost(str(e), "hello", "unknown") from e

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        """Blocking RPC; only an explicit loading refusal permits redispatch."""
        if self.owner != owner_key():
            raise DaemonError("not_found")
        with self._lock:
            if self._sock is not None and self._peer_closed_idle():
                # hostd idle-exited (or drained) while this connection sat between
                # calls. Nothing of this call was sent, so reconnecting is not a
                # replay; without this the next request dies on a dead socket as
                # "outcome unknown" (live: a headless turn read files for 2 min).
                self._disconnect()
            if self._sock is None:
                self.connect()
            outcome = "unknown"
            try:
                result = self._call_raw(method, params or {})
                if result.get("_is_error") and result.get("code") == "loading":
                    # hostd explicitly refused dispatch: readiness polling cannot
                    # have applied this operation, unlike a lost operation reply.
                    outcome = "not_dispatched"
                    readiness_error = self._wait_engine_ready()
                    if readiness_error is not None:
                        return readiness_error
                    outcome = "unknown"
                    result = self._call_raw(method, params or {})
                return result
            except (DaemonError, OSError) as e:
                # Even sendall failure can follow a partial/complete delivery.
                # Do not infer a restart, reissue ordinal doc_ids, or fall back
                # into an unrelated native engine. Next explicit call reconnects.
                self._disconnect()
                raise DaemonSessionLost(str(e), method, outcome) from e

    def _wait_engine_ready(self) -> Optional[dict]:
        deadline = time.monotonic() + self.ENGINE_READY_BUDGET_S
        while time.monotonic() < deadline:
            st = self._call_raw("daemon_status", {})
            state = st.get("state")
            if state == "ready":
                return None
            if state == "failed":
                return {"_is_error": True, "code": "engine_failed",
                        "message": f"engine failed to load in hostd: {st}"}
            time.sleep(0.25)
        return {"_is_error": True, "code": "engine_load_timeout",
                "message": "engine load timed out in hostd"}

    def poll_events(self) -> list[dict]:
        """Drain daemon push events buffered while reading responses."""
        with self._lock:
            out, self._events = self._events, []
            return out


# --------------------------------------------------------------- agent spawn
#
# Mission DAG execution remains in the calling MCP/GUI worker. Online model turns
# use the Chudcode session host; services.worker_main is a separate GUI child.
# spawn_agent_worker() is the explicit daemon-supervision seam: its child survives
# client exit, dies with the daemon, and pins daemon idle-exit while running.


def agent_worker_env(extra: Optional[dict] = None) -> dict:
    """Environment for a daemon-supervised agent worker: daemon mode ON plus
    instance-key/binary/socket passthrough, so the child's own engine calls
    land on the SAME daemon that supervises it (never a second instance)."""
    env = {"CHUDWARE_ENGINE_DAEMON": "1"}
    for k in ("CHUDWARE_INSTANCE_KEY", "CHUDWARE_HOSTD_BIN", "CHUDWARE_HOSTD_SOCKET"):
        v = os.environ.get(k)
        if v:
            env[k] = v
    if extra:
        env.update(extra)
    return env


def spawn_agent_worker(argv: list, mission_id: str = "", doc_id: str = "",
                       env: Optional[dict] = None) -> dict:
    """Spawn an agent worker as a supervised child of chudware-hostd.

    Returns the daemon's {child_id, pid, pgid, log} (or the error payload with
    _is_error). Stop via client().call("agent_stop", {"child_id": ..}); list
    via "agent_list"; exits arrive as {"event": "mission.exited"} pushes."""
    return client().call("agent_spawn", {
        "argv": [str(a) for a in argv],
        "env": agent_worker_env(env),
        "mission_id": mission_id,
        "doc_id": doc_id,
    })


_WRITER_GROUP = f"py-{os.getpid()}-{uuid.uuid4().hex}"
_group_session_established = threading.Event()
_clients = threading.local()
_call_connections = threading.local()


class TurnConnections:
    """Lease independent sockets to concurrent tools for one agent turn.

    RPC host-call threads are short-lived. Their exit must not detach documents
    between every pair of tools; the turn, not the thread, owns these sockets.
    Idle sockets are reused, so storage is bounded by peak concurrent CAD calls.
    """

    def __init__(self) -> None:
        self.owner = owner_key()
        self._lock = threading.Lock()
        self._idle: list[DaemonClient] = []
        self._closed = False

    def acquire(self) -> DaemonClient:
        if self.owner != owner_key():
            raise DaemonError("not_found")
        with self._lock:
            if self._closed:
                raise DaemonSessionLost("agent turn has ended", "connect", "not_dispatched")
            if self._idle:
                return self._idle.pop()
        return DaemonClient(writer_group=_WRITER_GROUP)

    def release(self, connection: DaemonClient) -> None:
        with self._lock:
            if not self._closed:
                self._idle.append(connection)
                return
        connection.close()

    def close(self) -> None:
        # Never interrupt an in-flight mutation. Active leases close when their
        # calls unwind; new calls on this finished turn refuse before dispatch.
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for connection in idle:
            connection.close()


@contextlib.contextmanager
def turn_connection_scope(pool: TurnConnections | None):
    """Install a lazy call lease, preserving nested dispatch on this thread."""
    previous = getattr(_call_connections, "lease", None)
    if pool is None or (previous is not None and previous[0] is pool):
        yield
        return
    lease = [pool, None]
    _call_connections.lease = lease
    try:
        yield
    finally:
        _call_connections.lease = previous
        if lease[1] is not None:
            pool.release(lease[1])


class _ThreadClient:
    """Thread-local owner: release its socket when the calling thread exits."""

    def __init__(self) -> None:
        self.owner = owner_key()
        self.value = DaemonClient(writer_group=_WRITER_GROUP)

    def __del__(self) -> None:
        self.value.close()


def client() -> DaemonClient:
    """Current tool's leased connection, or a thread-owned standalone connection."""
    lease = getattr(_call_connections, "lease", None)
    if lease is not None:
        if lease[1] is None:
            lease[1] = lease[0].acquire()
        return lease[1]
    holder = getattr(_clients, "holder", None)
    if holder is None or holder.owner != owner_key():
        if holder is not None:
            holder.value.close()
        holder = _clients.holder = _ThreadClient()
    return holder.value
