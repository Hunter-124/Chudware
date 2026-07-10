#!/usr/bin/env python3
"""aicad-hostd smoke gate (stdlib-only, check.py style).

Covers the daemon lifecycle contract from the hostd plan:
  1. spawn -> connect -> hello/ping -> engine call -> idle-exit within linger
  2. dual-client race: two clients connect-or-spawn simultaneously -> exactly
     ONE daemon serves both (same daemon_pid)
  3. --stop drains a running daemon
  4. kill -9 leaves a stale socket; the next connect-or-spawn recovers

Usage: scripts/hostd_smoke.py [path/to/aicad-hostd]
Defaults to core/build-occt-linux/bin/aicad-hostd, then core/build-linux.
Exit 0 = all pass. Uses a scratch AICAD_INSTANCE_KEY so a real daemon (dev or
installed) is never touched.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_hostd() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    for rel in ("core/build-occt-linux/bin/aicad-hostd", "core/build-linux/bin/aicad-hostd"):
        p = os.path.join(REPO, rel)
        if os.access(p, os.X_OK):
            return p
    sys.exit("hostd_smoke: no aicad-hostd binary found (build core first)")


def runtime_dir() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/aicad-{os.getuid()}"
    return os.path.join(base, "aicad") if os.environ.get("XDG_RUNTIME_DIR") else base


class Client:
    """Minimal mirror of aicad_ipc::EngineClient (connect-or-spawn + line RPC)."""

    def __init__(self, hostd: str, key: str, spawn: bool = True, budget_s: float = 15.0):
        self.sock_path = os.path.join(runtime_dir(), f"hostd-{key}.sock")
        self.sock = None
        deadline = time.monotonic() + budget_s
        backoff = 0.05
        spawned = False
        while True:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(self.sock_path)
                self.sock = s
                self.f = s.makefile("rw")
                return
            except OSError:
                if not spawn:
                    raise
                if not spawned:
                    subprocess.Popen([hostd], start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    spawned = True
                if time.monotonic() > deadline:
                    raise TimeoutError(f"hostd spawn timed out ({self.sock_path})")
                time.sleep(backoff)
                backoff = min(backoff * 2, 0.5)

    def call(self, method: str, params: dict | None = None):
        self.f.write(json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n")
        self.f.flush()
        while True:
            line = self.f.readline()
            if not line:
                raise ConnectionError("daemon closed")
            msg = json.loads(line)
            if "event" in msg:
                continue  # push lines (Phase 3+) are not responses
            return msg

    def close(self):
        # makefile() dups the fd — close BOTH or the daemon keeps seeing us as a
        # live client (its idle-exit then waits for our process to die).
        if self.sock:
            self.f.close()
            self.sock.close()
            self.sock = None


def wait_gone(pid: int, timeout_s: float) -> bool:
    """True when `pid` is dead. Daemons are Popen children of this script, so an
    exited daemon lingers as a ZOMBIE until reaped — reap and count Z as gone."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.waitpid(pid, os.WNOHANG)  # reap if it's our child (no-op otherwise)
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
            with open(f"/proc/{pid}/stat") as fh:
                if fh.read().split(")")[-1].split()[0] == "Z":
                    return True
        except (ProcessLookupError, FileNotFoundError):
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    hostd = find_hostd()
    fails = []
    env_linger = os.environ.get("AICAD_HOSTD_LINGER_SECS")
    os.environ["AICAD_HOSTD_LINGER_SECS"] = "2"
    if not os.environ.get("AI_CAD_LIBAICAD"):
        lib = os.path.join(os.path.dirname(hostd), "libaicad.so")
        if os.path.exists(lib):
            os.environ["AI_CAD_LIBAICAD"] = lib

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    # --- 1. basic lifecycle -------------------------------------------------
    key = "smoke-" + uuid.uuid4().hex[:8]
    os.environ["AICAD_INSTANCE_KEY"] = key
    c = Client(hostd, key)
    hello = c.call("hello", {"client": "test", "pid": os.getpid(), "proto": 1})
    check("hello", "result" in hello and hello["result"].get("proto") == 1)
    check("ping", c.call("ping")["result"].get("ok") is True)
    state = ""
    for _ in range(240):  # engine load can take a while cold
        state = c.call("daemon_status")["result"]["state"]
        if state != "loading":
            break
        time.sleep(0.5)
    check("engine ready", state == "ready", f"state={state}")
    r = c.call("doc_new", {"name": "hostd-smoke"})
    check("engine passthrough", "result" in r and "doc_id" in r["result"], str(r)[:80])
    pid = c.call("daemon_status")["result"]["pid"]
    c.close()
    check("idle exit", wait_gone(pid, 15.0), f"pid={pid} linger=2s")

    # --- 2. dual-client race ------------------------------------------------
    key = "smoke-" + uuid.uuid4().hex[:8]
    os.environ["AICAD_INSTANCE_KEY"] = key
    import threading
    results: dict[int, int] = {}
    def racer(i: int):
        cc = Client(hostd, key)
        results[i] = cc.call("daemon_status")["result"]["pid"]
        cc.close()
    ts = [threading.Thread(target=racer, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    pids = set(results.values())
    check("dual-client race -> one daemon", len(pids) == 1 and len(results) == 2, f"pids={pids}")
    race_pid = pids.pop() if pids else 0

    # --- 3. --stop drains ---------------------------------------------------
    stop = subprocess.run([hostd, "--stop"], capture_output=True, text=True,
                          env=os.environ.copy(), timeout=30)
    check("--stop accepted", stop.returncode == 0, stop.stderr.strip()[:80])
    check("--stop exits daemon", wait_gone(race_pid, 15.0), f"pid={race_pid}")

    # --- 4. stale-socket recovery after kill -9 ------------------------------
    key = "smoke-" + uuid.uuid4().hex[:8]
    os.environ["AICAD_INSTANCE_KEY"] = key
    c = Client(hostd, key)
    pid = c.call("daemon_status")["result"]["pid"]
    os.kill(pid, signal.SIGKILL)  # socket + lock files remain on disk
    time.sleep(0.5)
    c2 = Client(hostd, key)  # must unlink the stale socket and spawn fresh
    pid2 = c2.call("daemon_status")["result"]["pid"]
    check("stale-socket recovery", pid2 != pid and wait_gone(pid, 1.0), f"{pid}->{pid2}")
    c2.close()
    wait_gone(pid2, 15.0)

    if env_linger is None:
        os.environ.pop("AICAD_HOSTD_LINGER_SECS", None)
    print(f"hostd_smoke: {'OK' if not fails else 'FAILED: ' + ', '.join(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
