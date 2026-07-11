"""services/browser/cef_client.py — Python client for the CEF host daemon.

The headless replacement for Playwright: instead of launching Playwright Chromium,
we drive our OWN CEF Chromium (``AI-CAD --cef-daemon``) over a local socket. CEF is
driven through its native C++ API, so there is no CDP ``Runtime.enable`` handshake —
the automation-protocol fingerprint that bot gates key on is absent (the whole
reason for the migration; see the plan + services/browser/cef_headless.cpp).

Lifecycle: the daemon is lazily spawned on the first request if nothing is
listening, owns the shared *warm* profile (so a login in the interactive CEF panel
carries into headless fetch), and idle-exits when unused — CEF/Chromium is not
resident when parts search is not running.

Transport mirrors services/mcp/daemon_bridge.py: connect-or-spawn against a
per-binary socket key, newline-delimited JSON-RPC, one request per connection.
POSIX (AF_UNIX) for now — Windows named-pipe parity is a follow-up, tracked with
the same _PipeIO shape daemon_bridge already implements.
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
from typing import Any, Optional

#: How long (seconds) the daemon stays up with no request before idle-exiting.
DEFAULT_IDLE_S = 120
#: Budget for a cold daemon spawn (CEF/Chromium bootstrap).
SPAWN_BUDGET_S = 30.0


class CefDaemonError(RuntimeError):
    """Transport-level failure talking to the CEF daemon (spawn/connect/protocol)."""


def _repo_root() -> Path:
    # services/browser/cef_client.py → repo root is three parents up.
    return Path(__file__).resolve().parents[2]


def _fnv1a64_hex(s: str) -> str:
    h = 0xCBF29CE484222325
    for b in s.encode("utf-8"):
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def resolve_cef_bin() -> Optional[Path]:
    """Locate the AI-CAD host binary that hosts CEF. env → dist → build dirs."""
    if p := os.environ.get("AI_CAD_BIN"):
        return Path(p)
    exe = "AI-CAD.exe" if sys.platform == "win32" else "AI-CAD"
    root = _repo_root()
    for cand in (
        Path(os.environ.get("AI_CAD_DIST") or "nonexistent") / exe,
        root / "host" / "build-cef" / "bin" / exe,
        root / "host" / "build-linux" / "bin" / exe,
    ):
        if cand.exists():
            return cand
    return None


def socket_path(cef_bin: Optional[Path]) -> Optional[str]:
    """The daemon endpoint (AF_UNIX path on POSIX). Keyed by the binary realpath so
    two checkouts don't collide, matching daemon_bridge's discovery scheme."""
    if p := os.environ.get("AICAD_CEF_SOCKET"):
        return p
    if not cef_bin:
        return None
    key = os.environ.get("AICAD_CEF_KEY") or _fnv1a64_hex(str(Path(cef_bin).resolve()))
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/aicad-{os.getuid()}"
    return str(Path(runtime) / "aicad" / f"cefd-{key}.sock")


def _spawn_daemon_detached(cef_bin: Path, sock: str, idle_s: int) -> None:
    """Launch the daemon so it outlives this client and self-manages via idle-exit.
    POSIX: start_new_session detaches; an orphaned child reparents to init."""
    Path(sock).parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(cef_bin), "--cef-daemon", sock, str(idle_s)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class CefDaemon:
    """Connect-or-spawn client for the CEF host daemon. Thread-safe."""

    def __init__(self, idle_s: int = DEFAULT_IDLE_S) -> None:
        self._lock = threading.Lock()
        self._idle_s = idle_s
        self._next_id = 1

    def _connect(self, spawn: bool = True, budget_s: float = SPAWN_BUDGET_S) -> socket.socket:
        cef_bin = resolve_cef_bin()
        sock_path = socket_path(cef_bin)
        if not sock_path:
            raise CefDaemonError("cannot resolve CEF daemon socket (set AI_CAD_BIN or build host)")
        if sys.platform == "win32":  # POSIX-only for now (see module docstring)
            raise CefDaemonError("CEF daemon client is POSIX-only pending named-pipe parity")
        deadline = time.monotonic() + budget_s
        backoff = 0.05
        spawned = False
        while True:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(sock_path)
                return s
            except OSError as e:
                if not spawn:
                    raise CefDaemonError(f"CEF daemon not running ({e})") from e
                if not spawned:
                    if not cef_bin:
                        raise CefDaemonError(
                            "AI-CAD binary not found (set AI_CAD_BIN or build host with CEF)"
                        ) from e
                    _spawn_daemon_detached(cef_bin, sock_path, self._idle_s)
                    spawned = True
                if time.monotonic() > deadline:
                    raise CefDaemonError(f"CEF daemon spawn timed out ({e})") from e
                time.sleep(backoff)
                backoff = min(backoff * 2, 0.5)

    def _call(self, method: str, params: dict, *, timeout_s: float = 90.0) -> dict:
        """One JSON-RPC round-trip on a fresh connection (daemon = one req/conn)."""
        with self._lock:
            rid = self._next_id
            self._next_id += 1
        s = self._connect()
        try:
            s.settimeout(timeout_s)
            s.sendall((json.dumps({"id": rid, "method": method, "params": params}) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        line = buf.split(b"\n", 1)[0]
        if not line:
            raise CefDaemonError(f"empty response from CEF daemon for {method}")
        msg = json.loads(line)
        if "error" in msg:
            raise CefDaemonError(f"CEF daemon error for {method}: {msg['error']}")
        return msg.get("result", {})

    # --------------------------------------------------------------- public API

    def ping(self) -> bool:
        try:
            return bool(self._call("ping", {}, timeout_s=5.0).get("ok"))
        except CefDaemonError:
            return False

    def fetch(self, url: str, *, settle_ms: int = 1200, profile: str = "") -> dict:
        """Load ``url`` in CEF and return {ok, status, final_url, html}. ``profile``
        empty = the shared warm profile (the authenticated-session jar)."""
        return self._call("fetch", {"url": url, "settle_ms": settle_ms, "profile": profile})

    def download(self, url: str, dest: str, *, profile: str = "") -> dict:
        """Fetch ``url``'s bytes into ``dest`` under the profile's cookies."""
        return self._call("download", {"url": url, "dest": dest, "profile": profile})

    def interact(self, url: str, script: str, *, sentinel: str = "__aicad_capture",
                 settle_ms: int = 4000, timeout_ms: int = 12000, profile: str = "",
                 timeout_s: float = 90.0) -> dict:
        """Load ``url`` under the warm session, run ``script`` in the page, and wait
        for a sentinel DOM element (id ``sentinel``) to publish a result. Returns
        {ok, status, final_url, html} — the caller extracts the sentinel's contents.

        The reusable primitive behind SPA configurators that mint a download URL
        client-side (McMaster CAD FilePath, Misumi series→SKU): ``script`` drives the
        page's own controls or fetches a same-origin config endpoint with the page's
        cookies+headers, then writes the result into ``<... id="sentinel">…</...>``."""
        return self._call("interact",
                          {"url": url, "script": script, "sentinel": sentinel,
                           "settle_ms": settle_ms, "timeout_ms": timeout_ms,
                           "profile": profile},
                          timeout_s=timeout_s)

    def import_session(self, cookies: list[dict], *, profile: str = "",
                       timeout_s: float = 60.0) -> dict:
        """Inject a cookie jar (exported from the interactive CEF panel) into the
        daemon's warm profile so a login there carries into headless fetch/download.
        ``profile`` empty = the warm profile. Returns {ok, count}. The panel and
        daemon own SEPARATE on-disk profiles (never the same store concurrently), so
        the session crosses via this cookie re-injection, not by sharing a dir."""
        return self._call("import_session", {"cookies": cookies, "profile": profile},
                          timeout_s=timeout_s)

    def login_probe(self, url: str, fill_js: str, *, settle_ms: int = 3000,
                    post_wait_ms: int = 12000, profile: str = "", timeout_s: float = 90.0) -> dict:
        """DIAGNOSTIC: load ``url``, run ``fill_js`` (fills + submits a login form in
        the page), and return {ok, status, final_url, html} of the response — so a
        bot-wall verdict (e.g. reCAPTCHA reject vs invalid-login) can be observed
        headlessly. Requires the daemon to run with AI_CAD_CEF_PROBE=1."""
        return self._call("login_probe",
                          {"url": url, "fill_js": fill_js, "settle_ms": settle_ms,
                           "post_wait_ms": post_wait_ms, "profile": profile},
                          timeout_s=timeout_s)


#: Process-wide client (the daemon itself serialises requests; one client is fine).
_CLIENT: Optional[CefDaemon] = None
_CLIENT_LOCK = threading.Lock()


def client() -> CefDaemon:
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = CefDaemon()
        return _CLIENT
