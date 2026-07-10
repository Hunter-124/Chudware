"""Client for the shared engine-host daemon (aicad-hostd).

The Python mirror of services/ipc/ipc_client.cpp: connect-or-spawn against the
instance-key socket, newline-delimited JSON-RPC, one outstanding request per
connection (Lock-serialized — FastMCP runs sync tools off-thread). core_bridge
routes through here when AI_CAD_ENGINE_DAEMON=1, keeping its call() signature
so the ~200 tool call sites never change.

Semantics preserved vs the legacy in-process ctypes path:
  * call() BLOCKS with no client timeout (solves run minutes; ctypes had none).
  * The engine loads asynchronously in the daemon; a {"code":"loading"} reply is
    absorbed here by polling daemon_status until ready (bounded), so callers
    still see the old "load blocks until done" behavior.
  * Daemon push events (doc.changed etc., Phase 3) buffer in-process; MCP tool
    layers drain them via poll_events() to surface _foreign_edits notes.

Discovery mirrors ipc_discovery.cpp — keep the two in sync:
  key    = $AICAD_INSTANCE_KEY or fnv1a64(realpath(hostd binary))
  socket = $AICAD_HOSTD_SOCKET or $XDG_RUNTIME_DIR/aicad/hostd-<key>.sock
           (fallback /tmp/aicad-<uid>/hostd-<key>.sock)
"""
from __future__ import annotations

import json
import os
import socket
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
    for cand in (
        Path(os.environ.get("AI_CAD_DIST") or "nonexistent") / exe,
        root / "core" / "build-occt-linux" / "bin" / exe,
        root / "core" / "build-linux" / "bin" / exe,
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def socket_path(hostd_bin: Optional[Path]) -> Optional[str]:
    if p := os.environ.get("AICAD_HOSTD_SOCKET"):
        return p
    key = os.environ.get("AICAD_INSTANCE_KEY")
    if not key:
        if not hostd_bin:
            return None
        key = _fnv1a64_hex(str(Path(hostd_bin).resolve()))
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        return f"{xdg}/aicad/hostd-{key}.sock"
    return f"/tmp/aicad-{os.getuid()}/hostd-{key}.sock"


def daemon_socket_alive(hostd_bin: Optional[Path] = None) -> bool:
    """Cheap liveness probe — used by the legacy path's mode-divergence warning."""
    sp = socket_path(hostd_bin or resolve_hostd_bin())
    if not sp:
        return False
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(0.25)
        s.connect(sp)
        s.close()
        return True
    except OSError:
        return False


class DaemonClient:
    """One connection to aicad-hostd. Thread-safe; reconnects once on EOF."""

    ENGINE_READY_BUDGET_S = 300.0  # cold OCCT load headroom

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._next_id = 1
        self._events: list[dict] = []
        self.server_info: dict = {}
        self._sock_path: Optional[str] = None

    # ---------------------------------------------------------------- connect

    def connect(self, spawn: bool = True, budget_s: float = 15.0) -> None:
        hostd = resolve_hostd_bin()
        self._sock_path = socket_path(hostd)
        if not self._sock_path:
            raise DaemonError("cannot resolve hostd socket (no binary / AICAD_HOSTD_BIN)")
        deadline = time.monotonic() + budget_s
        backoff = 0.05
        spawned = False
        while True:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(self._sock_path)
                self._sock = s
                self._buf = b""
                break
            except OSError as e:
                if not spawn:
                    raise DaemonError(f"hostd not running ({e})") from e
                if not spawned:
                    if not hostd:
                        raise DaemonError(
                            "aicad-hostd binary not found (set AICAD_HOSTD_BIN or build core)"
                        ) from e
                    # start_new_session detaches enough for a daemon that
                    # self-manages via its own flock singleton; the daemon must
                    # outlive us, and an orphaned child reparents to init.
                    subprocess.Popen(
                        [str(hostd)],
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    spawned = True
                if time.monotonic() > deadline:
                    raise DaemonError(f"hostd spawn timed out ({e})") from e
                time.sleep(backoff)
                backoff = min(backoff * 2, 0.5)
        self.server_info = self._call_raw(
            "hello", {"client": "mcp", "pid": os.getpid(), "proto": 1}
        )

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
            chunk = self._sock.recv(65536)
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
        self._sock.sendall(req.encode() + b"\n")
        while True:
            msg = self._read_msg()
            if "event" in msg:
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
