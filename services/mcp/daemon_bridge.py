"""Client for the shared engine-host daemon (aicad-hostd).

The Python mirror of services/ipc/ipc_client.cpp: connect-or-spawn against the
instance-key socket, newline-delimited JSON-RPC, one outstanding request per
connection (Lock-serialized — FastMCP runs sync tools off-thread). core_bridge
routes through here BY DEFAULT since the Phase-8 flip (unset or
AI_CAD_ENGINE_DAEMON=1; only an explicit =0 selects the legacy in-process
path), keeping its call() signature so the ~200 tool call sites never change.

Semantics preserved vs the legacy in-process ctypes path:
  * call() BLOCKS with no client timeout (solves run minutes; ctypes had none).
  * The engine loads asynchronously in the daemon; a {"code":"loading"} reply is
    absorbed here by polling daemon_status until ready (bounded), so callers
    still see the old "load blocks until done" behavior.
  * Daemon push events (doc.changed etc., Phase 3) buffer in-process; MCP tool
    layers drain them via poll_events() to surface _foreign_edits notes.

Discovery mirrors ipc_discovery.cpp / ipc_discovery_win.cpp — keep them in sync:
  key    = $AICAD_INSTANCE_KEY or fnv1a64(realpath(hostd binary))
           (Windows: fnv1a64 of the resolved path LOWERCASED — NTFS is
           case-insensitive and the C++ side lowercases before hashing)
  POSIX  = $AICAD_HOSTD_SOCKET or $XDG_RUNTIME_DIR/aicad/hostd-<key>.sock
           (fallback /tmp/aicad-<uid>/hostd-<key>.sock)
  win32  = $AICAD_HOSTD_PIPE/$AICAD_HOSTD_SOCKET or
           \\\\.\\pipe\\aicad-hostd-<SID>-<key>

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

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional


class DaemonError(RuntimeError):
    """Transport-level daemon failure (connect/spawn/protocol)."""


def _fnv1a64_hex(s: str) -> str:
    h = 0xCBF29CE484222325
    for b in s.encode():
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def _windows_user_sid() -> str:
    """Current user's SID string ("S-1-5-21-..."), matching the SID the C++ side
    (ipc_discovery_win.cpp currentUserSid) embeds in the pipe name. ctypes only —
    no pywin32 dependency."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TOKEN_QUERY, TOKEN_USER_CLASS = 0x0008, 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY,
                                     ctypes.byref(token)):
        raise DaemonError("OpenProcessToken failed")
    try:
        need = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0,
                                     ctypes.byref(need))
        buf = ctypes.create_string_buffer(need.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, buf, need,
                                            ctypes.byref(need)):
            raise DaemonError("GetTokenInformation failed")
        # TOKEN_USER starts with SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents
        str_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_sid)):
            raise DaemonError("ConvertSidToStringSidW failed")
        try:
            return str_sid.value or "S-unknown"
        finally:
            kernel32.LocalFree(str_sid)
    finally:
        kernel32.CloseHandle(token)


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
    # installed mcp_server/ package layout, where core/ sits beside it).
    return Path(__file__).resolve().parent.parent.parent


def resolve_hostd_bin() -> Optional[Path]:
    """Locate the aicad-hostd binary. Mirrors the libaicad candidate order in
    core_bridge (env -> dist -> full OCCT build -> slim build)."""
    if p := os.environ.get("AICAD_HOSTD_BIN"):
        return Path(p)
    exe = "aicad-hostd.exe" if sys.platform == "win32" else "aicad-hostd"
    root = _repo_root()
    if sys.platform == "win32":
        cands = (
            Path(os.environ.get("AI_CAD_DIST") or "nonexistent") / exe,
            root / "core" / "build" / "bin" / exe,  # build.bat / MSVC preset dir
        )
    else:
        cands = (
            Path(os.environ.get("AI_CAD_DIST") or "nonexistent") / exe,
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
        if p := os.environ.get("AICAD_HOSTD_PIPE"):
            return p
    if p := os.environ.get("AICAD_HOSTD_SOCKET"):
        return p
    key = os.environ.get("AICAD_INSTANCE_KEY")
    if not key:
        if not hostd_bin:
            return None
        resolved = str(Path(hostd_bin).resolve())
        if sys.platform == "win32":
            resolved = resolved.lower()  # NTFS case-insensitive; C++ side lowercases too
        key = _fnv1a64_hex(resolved)
    if sys.platform == "win32":
        return f"\\\\.\\pipe\\aicad-hostd-{_windows_user_sid()}-{key}"
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        return f"{xdg}/aicad/hostd-{key}.sock"
    # No XDG_RUNTIME_DIR (common for agent-spawned processes): fall back to the
    # canonical /run/user/<uid> BEFORE /tmp so every client resolves the SAME
    # directory as desktop-launched ones — otherwise two "singleton" daemons
    # coexist on different sockets. Mirrors ipc_discovery.cpp runtimeDir().
    run_user = f"/run/user/{os.getuid()}"
    try:
        st = os.lstat(run_user)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
            return f"{run_user}/aicad/hostd-{key}.sock"
    except OSError:
        pass
    return f"/tmp/aicad-{os.getuid()}/hostd-{key}.sock"


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
    """Build id of the hostd binary ON DISK (`aicad-hostd --version` prints
    "aicad-hostd <git-describe id>"). Cached per (path, mtime) so the subprocess
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
# zombies were observed parented to MCP servers (docs/PERF_AUDIT_2026-07-10.md §1).
_SPAWNED: list = []


def _reap_spawned() -> None:
    _SPAWNED[:] = [p for p in _SPAWNED if p.poll() is None]


def _watch_spawned(p: "subprocess.Popen") -> "subprocess.Popen":
    """Track a spawned hostd AND reap it the moment it exits. The polling
    _reap_spawned() only runs on the next connect, so a flock-race loser or a
    drained daemon used to sit <defunct> in this process for its whole life;
    a dedicated wait() thread collects it immediately."""
    _SPAWNED.append(p)
    threading.Thread(target=p.wait, name="aicad-hostd-reaper", daemon=True).start()
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
                "[daemon_bridge] WARNING: job breakaway denied — aicad-hostd will die "
                "with this process's Job Object (e.g. when the agent session ends). "
                "Start the AI-CAD GUI first for a durable daemon.",
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
    """One connection to aicad-hostd. Thread-safe; reconnects once on EOF."""

    ENGINE_READY_BUDGET_S = 300.0  # cold OCCT load headroom

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # AF_UNIX socket on POSIX, _PipeIO on win32 (same sendall/recv/close shape).
        self._sock: Optional[object] = None
        self._buf = b""
        self._next_id = 1
        self._events: list[dict] = []
        self.server_info: dict = {}
        self._sock_path: Optional[str] = None
        self._stale_retry_done = False  # one drain-replace attempt per client

    # ---------------------------------------------------------------- connect

    def connect(self, spawn: bool = True, budget_s: float = 15.0) -> None:
        _reap_spawned()
        hostd = resolve_hostd_bin()
        self._sock_path = socket_path(hostd)
        if not self._sock_path:
            raise DaemonError("cannot resolve hostd socket (no binary / AICAD_HOSTD_BIN)")
        try:
            budget_s = float(os.environ.get("AICAD_HOSTD_CONNECT_BUDGET_S", "") or budget_s)
        except ValueError:
            pass
        deadline = time.monotonic() + budget_s
        backoff = 0.05
        child = None
        child_gone_at = 0.0
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
                self._buf = b""
                break
            except OSError as e:
                if not spawn:
                    raise DaemonError(f"hostd not running ({e})") from e
                if child is None:
                    if not hostd:
                        raise DaemonError(
                            "aicad-hostd binary not found (set AICAD_HOSTD_BIN or build core)"
                        ) from e
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
                            "`pkill aicad-hostd` and retry."
                        ) from e
                if time.monotonic() > deadline:
                    raise DaemonError(f"hostd spawn timed out ({e})") from e
                time.sleep(backoff)
                backoff = min(backoff * 2, 0.5)
        self.server_info = self._call_raw(
            "hello", {"client": "mcp", "pid": os.getpid(), "proto": 1}
        )
        self._drain_replace_if_stale(hostd, spawn)

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
            if self._sock:
                self._sock.close()
                self._sock = None

    # ---------------------------------------------------------------- wire I/O

    def _read_msg(self) -> dict:
        assert self._sock is not None
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line, self._buf = self._buf[:nl], self._buf[nl + 1:]
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate stray lines
            try:
                chunk = self._sock.recv(65536)
            except OSError as e:
                # e.g. ECONNRESET from a draining daemon that closed on accept.
                raise DaemonError(f"daemon connection lost ({e})") from e
            if not chunk:
                raise DaemonError("daemon connection closed")
            self._buf += chunk

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

    # ---------------------------------------------------------------- public

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        """Blocking RPC with loading-absorption and one reconnect on EOF."""
        with self._lock:
            for attempt in (1, 2):
                if self._sock is None:
                    # Reconnect inline (also re-spawns if the daemon died).
                    self._lock.release()
                    try:
                        self.connect()
                    finally:
                        self._lock.acquire()
                try:
                    result = self._call_raw(method, params or {})
                    if result.get("_is_error") and result.get("code") == "loading":
                        self._wait_engine_ready()
                        result = self._call_raw(method, params or {})
                    return result
                except (DaemonError, OSError) as e:
                    # OSError covers a dead peer noticed on write (BrokenPipe)
                    # rather than on read — same reconnect-once handling.
                    self._sock = None
                    if attempt == 2:
                        raise DaemonError(str(e)) from e
        raise DaemonError("unreachable")  # pragma: no cover

    def _wait_engine_ready(self) -> None:
        deadline = time.monotonic() + self.ENGINE_READY_BUDGET_S
        while time.monotonic() < deadline:
            st = self._call_raw("daemon_status", {})
            state = st.get("state")
            if state == "ready":
                return
            if state == "failed":
                raise DaemonError(f"engine failed to load in hostd: {st}")
            time.sleep(0.25)
        raise DaemonError("engine load timed out in hostd")

    def poll_events(self) -> list[dict]:
        """Drain buffered daemon push events (arrive interleaved with responses).
        Phase 3 wires these into _foreign_edits tool notes."""
        with self._lock:
            out, self._events = self._events, []
            return out


# --------------------------------------------------------------- agent spawn
#
# Phase-5 routing note (child supervision): the MCP mission tools
# (services/mcp/mission_tools.py mission_start -> dag graph -> dag_run) and the
# in-app agent (services.agent.loop, driven by the GUI's Python worker) all run
# IN-PROCESS today — there is no subprocess-based mission worker path in
# services/{mcp,agent,dag} to reroute (verified: the only Popen in the tree is
# this module's own daemon spawn). When a subprocess agent-worker path lands,
# route it through spawn_agent_worker() below so the worker is a child of the
# DAEMON: it survives GUI/MCP-client exit, dies deterministically with the
# daemon (PDEATHSIG), and pins the daemon against idle-exit while it runs.
# The GUI's engine.worker Python services process stays a GUI child by design.


def agent_worker_env(extra: Optional[dict] = None) -> dict:
    """Environment for a daemon-supervised agent worker: daemon mode ON plus
    instance-key/binary/socket passthrough, so the child's own engine calls
    land on the SAME daemon that supervises it (never a second instance)."""
    env = {"AI_CAD_ENGINE_DAEMON": "1"}
    for k in ("AICAD_INSTANCE_KEY", "AICAD_HOSTD_BIN", "AICAD_HOSTD_SOCKET"):
        v = os.environ.get(k)
        if v:
            env[k] = v
    if extra:
        env.update(extra)
    return env


def spawn_agent_worker(argv: list, mission_id: str = "", doc_id: str = "",
                       env: Optional[dict] = None) -> dict:
    """Spawn an agent worker as a supervised child of aicad-hostd.

    Returns the daemon's {child_id, pid, pgid, log} (or the error payload with
    _is_error). Stop via client().call("agent_stop", {"child_id": ..}); list
    via "agent_list"; exits arrive as {"event": "mission.exited"} pushes."""
    return client().call("agent_spawn", {
        "argv": [str(a) for a in argv],
        "env": agent_worker_env(env),
        "mission_id": mission_id,
        "doc_id": doc_id,
    })


_client: Optional[DaemonClient] = None
_client_lock = threading.Lock()


def client() -> DaemonClient:
    """Process singleton, connected on first use."""
    global _client
    if _client is None or not _client.connected():
        with _client_lock:
            if _client is None:
                _client = DaemonClient()
            if not _client.connected():
                _client.connect()
    return _client
