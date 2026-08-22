#!/usr/bin/env python3
"""Web parity probe (stdlib-only) — enforces contracts/web_parity.md.

The web (WASM) client talks to chudware-hostd, a pure C++ engine daemon with no
Python-worker fallback. This probe spawns a scratch hostd, calls every method
listed in contracts/web_parity.md, and asserts:

  web_required    -> IS core-dispatched (any response that is NOT the
                     unknown/unsupported class; a param-validation error still
                     proves the method exists in the C++ engine)
  web_unsupported -> IS rejected with the unknown/unsupported class (the gap is
                     real and documented). If one starts succeeding, the
                     cutover landed — shrink the inventory (probe fails until
                     you do).

Usage: scripts/web_parity_probe.py [path/to/chudware-hostd]
Exit 0 = parity contract holds. Uses a scratch CHUDWARE_INSTANCE_KEY; never
touches a live daemon. Methods are called with {} — classification depends
only on the error KIND, never on side effects.
"""
from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(REPO, "contracts", "web_parity.md")

UNKNOWN_MARKERS = ("unknown method", "unsupported", "not implemented", "no such method")
WORKER_MARKERS = ("worker_error", "worker bridge", "python runtime not installed")


def find_hostd() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    for rel in ("core/build-occt-linux/bin/chudware-hostd", "core/build-linux/bin/chudware-hostd"):
        p = os.path.join(REPO, rel)
        if os.access(p, os.X_OK):
            return p
    sys.exit("web_parity_probe: no chudware-hostd binary found (build core first)")


def runtime_dir() -> str:
    """Where the daemon puts its socket. MUST agree with the C++ the daemon actually runs.

    Source of truth is services/ipc/ipc_discovery.cpp runtimeDir(); kept identical to
    scripts/hostd_smoke.py. The old env-only rule resolved to /tmp/chudware-<uid> whenever
    XDG_RUNTIME_DIR was unset, while the daemon it had just spawned was creating its socket
    under /run/user/<uid>/chudware -- so this probe timed out against a healthy daemon on
    exactly the hosts that matter, the systemd-launched CI runners.
    """
    if xdg := os.environ.get("XDG_RUNTIME_DIR"):
        return os.path.join(xdg, "chudware")
    run_user = f"/run/user/{os.getuid()}"
    try:
        st = os.lstat(run_user)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
            return os.path.join(run_user, "chudware")
    except OSError:
        pass
    return f"/tmp/chudware-{os.getuid()}"


def load_contract() -> dict:
    text = open(CONTRACT, encoding="utf-8").read()
    m = re.search(r"```web-parity\s*\n(.*?)\n```", text, re.S)
    if not m:
        sys.exit(f"web_parity_probe: no ```web-parity block in {CONTRACT}")
    return json.loads(m.group(1))


class Client:
    """Minimal mirror of chudware_ipc::EngineClient (connect-or-spawn + line RPC)."""

    def __init__(self, hostd: str, key: str, budget_s: float = 15.0):
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
                if not spawned:
                    subprocess.Popen([hostd], start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    spawned = True
                if time.monotonic() > deadline:
                    raise TimeoutError(f"hostd spawn timed out ({self.sock_path})")
                time.sleep(backoff)
                backoff = min(backoff * 2, 0.5)

    def call(self, method: str, params: dict | None = None, timeout_s: float = 10.0):
        self.sock.settimeout(timeout_s)
        self.f.write(json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n")
        self.f.flush()
        while True:
            line = self.f.readline()
            if not line:
                raise ConnectionError("daemon closed")
            msg = json.loads(line)
            if "event" in msg:
                continue
            return msg

    def close(self):
        if self.sock:
            self.f.close()
            self.sock.close()
            self.sock = None


def is_python_or_unknown(resp: dict) -> bool:
    """True when the call was NOT served by libchudware: either the method is
    unknown everywhere, or hostd tried the (disabled) Python worker bridge.
    With the bridge disabled this is exactly what the web client experiences."""
    err = resp.get("error") or {}
    res = resp.get("result")
    blob = (json.dumps(err) + " " + json.dumps(res)).lower()
    if any(marker in blob for marker in UNKNOWN_MARKERS):
        return True
    if str(err.get("code", "")).lower() == "worker_error":
        return True
    if isinstance(res, dict) and str(res.get("code", "")).lower() == "worker_error":
        return True
    return any(marker in blob for marker in WORKER_MARKERS)


def main() -> int:
    hostd = find_hostd()
    contract = load_contract()
    if not os.environ.get("CHUDWARE_LIBCHUDWARE"):
        lib = os.path.join(os.path.dirname(hostd), "libchudware.so")
        if os.path.exists(lib):
            os.environ["CHUDWARE_LIBCHUDWARE"] = lib
    os.environ["CHUDWARE_HOSTD_LINGER_SECS"] = "3"
    # Simulate the web deployment: no Python worker bridge. hostd's lazy
    # fallback (core/hostd/worker_bridge.cpp) honors CHUDWARE_PYTHON — point it at
    # a nonexistent interpreter so python-only methods fail exactly as they do
    # for the WASM client.
    os.environ["CHUDWARE_PYTHON"] = "/nonexistent-web-parity-probe"
    key = "parity-" + uuid.uuid4().hex[:8]
    os.environ["CHUDWARE_INSTANCE_KEY"] = key

    fails: list[str] = []
    c = Client(hostd, key)
    c.call("hello", {"client": "parity-probe", "pid": os.getpid(), "proto": 1})
    for _ in range(240):  # engine load can take a while cold
        state = c.call("daemon_status")["result"]["state"]
        if state != "loading":
            break
        time.sleep(0.5)
    if state != "ready":
        print(f"web_parity_probe: engine never became ready (state={state})")
        return 1

    print("== web_required (must be core-dispatched) ==")
    for entry in contract.get("web_required", []):
        method = entry["method"]
        try:
            resp = c.call(method, {})
        except (ConnectionError, socket.timeout, TimeoutError) as e:
            print(f"  FAIL  {method:<24} call error: {e}")
            fails.append(f"{method} (call error: {e})")
            continue
        if is_python_or_unknown(resp):
            print(f"  FAIL  {method:<24} NOT core-dispatched -> NEW web parity hole "
                  f"(move to web_unsupported or port it)")
            fails.append(f"{method} (required but not dispatched)")
        else:
            print(f"  PASS  {method:<24} dispatched")

    print("== web_unsupported (must fail loud) ==")
    for entry in contract.get("web_unsupported", []):
        method = entry["method"]
        try:
            resp = c.call(method, {})
        except (ConnectionError, socket.timeout, TimeoutError) as e:
            print(f"  FAIL  {method:<24} call error: {e}")
            fails.append(f"{method} (call error: {e})")
            continue
        if is_python_or_unknown(resp):
            print(f"  PASS  {method:<24} fails loud ({entry.get('family', '')}; {entry.get('remove_at', '')})")
        else:
            print(f"  FAIL  {method:<24} now DISPATCHED — cutover landed? "
                  f"remove it from web_unsupported in contracts/web_parity.md")
            fails.append(f"{method} (inventory stale: now dispatched)")

    c.close()
    print(f"web_parity_probe: {'OK' if not fails else 'FAILED: ' + '; '.join(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
