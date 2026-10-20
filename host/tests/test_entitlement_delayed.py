#!/usr/bin/env python3
"""Drive the native account-panel stale-completion scenario over owned loopback HTTP.

The launcher creates a private Linux network namespace with only loopback enabled.
The fixture generates no trusted entitlement/policy/enforcement signatures: its bearer
is deliberately unsigned and its only refresh response is a held HTTP 503.
"""
from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import signal
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid


_BEARER = "unsigned-owned-fixture-bearer"
_FIELDS = {
    "token", "request_nonce", "sequence", "app_version", "platform",
    "integrity_digest", "last_acked_event_sequence", "customer_receipt", "signature",
}


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _loopback_namespace(outside_fd: int) -> None:
    outside = os.fstat(outside_fd)
    current = os.stat("/proc/self/ns/net")
    if not os.readlink(f"/proc/self/fd/{outside_fd}").startswith("net:["):
        raise RuntimeError("inherited descriptor is not a network namespace")
    if (outside.st_dev, outside.st_ino) == (current.st_dev, current.st_ino):
        raise RuntimeError("run in a private network namespace, not the caller namespace")
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("private network namespace must contain only loopback")


def _verify_request(root: Path, request: dict, nonce: str | None) -> dict:
    if set(request) != _FIELDS or request["token"] != _BEARER:
        raise AssertionError("unexpected refresh envelope or non-fixture bearer")
    if not nonce or request["request_nonce"] != nonce:
        raise AssertionError("request nonce header is not bound to the real envelope")
    if not isinstance(request["sequence"], int) or request["sequence"] <= 0:
        raise AssertionError("refresh did not reserve a real device sequence")
    signature = request["signature"]
    signature_bytes = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    if len(signature_bytes) != 64:
        raise AssertionError("refresh signature is not Ed25519")
    secret = (root / "data" / "Chudware" / "secrets" /
              "com_chudware_desktop-device-signing-v1.secret").read_bytes()
    if len(secret) != 64:
        raise AssertionError("owned local device identity was not persisted")
    # Verify the actual wire envelope against this fixture's local device public
    # key, never against (or in place of) the embedded entitlement authority key.
    control = root / "control"
    public_key = control / "device-public.der"
    public_key.write_bytes(bytes.fromhex("302a300506032b6570032100") + secret[32:])
    unsigned = {key: value for key, value in request.items() if key != "signature"}
    message = control / "request-envelope.json"
    message.write_bytes(json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8"))
    signed = control / "request-signature.bin"
    signed.write_bytes(signature_bytes)
    subprocess.run(
        ["openssl", "pkeyutl", "-verify", "-pubin", "-keyform", "DER",
         "-inkey", str(public_key), "-rawin", "-in", str(message),
         "-sigfile", str(signed)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    return {"signature_verified": True, "sequence": request["sequence"],
            "fixture_bearer": True, "nonce_bound": True}


def run(binary: Path, root: Path, python: Path | None, outside_fd: int) -> int:
    _loopback_namespace(outside_fd)
    for executable in ("openssl", "xvfb-run"):
        if shutil.which(executable) is None:
            raise RuntimeError(f"{executable} is required")
    if not binary.is_file():
        raise RuntimeError(f"native GUI binary is missing: {binary}")
    # Never delete/reuse a user's directory, fixture, workspace or session store.
    root.mkdir(parents=True, exist_ok=False)
    for name in ("home", "data", "config", "cache", "run", "work", "sessions", "control"):
        (root / name).mkdir(mode=0o700)
    control = root / "control"
    failure: list[str] = []
    requests: list[dict] = []
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            # The updater is redirected here too; an empty manifest is not a
            # refresh completion and must never satisfy the scenario markers.
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            try:
                if self.path != "/refresh":
                    raise AssertionError(f"unexpected endpoint: {self.path}")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise AssertionError("unexpected request length")
                request = json.loads(self.rfile.read(length))
                observed = _verify_request(root, request,
                                           self.headers.get("X-Chudware-Refresh-Nonce"))
                with lock:
                    requests.append(observed)
                    index = len(requests)
                if index != 1:
                    raise AssertionError("scenario launched more than one refresh")
                observed["request_index"] = index
                _write_json(control / "received.json", observed)
                deadline = time.monotonic() + 6
                while not (control / "release").exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("account scenario never released held HTTP response")
                    time.sleep(0.005)
                body = b'{"error":"owned held response; no entitlement authority"}\n'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True
                _write_json(control / "sent.json", {"status": 503, "released": True})
            except Exception as error:
                with lock:
                    failure.append(str(error))
                _write_json(control / "server_error.json", {"error": str(error)})
                self.close_connection = True

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/refresh"
    _write_json(control / "fixture.json", {"scenario": "entitlement_delayed",
                "loopback_only": True, "refresh_url": endpoint})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Reuse the gate's HOME/XDG + private working-directory shot isolation; do
    # not inherit provider keys, production URLs, daemon sockets or customer state.
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH")
           if name in os.environ}
    env.update({
        "HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
        "APPDATA": str(root / "data"), "LOCALAPPDATA": str(root / "data"),
        "XDG_DATA_HOME": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"), "XDG_RUNTIME_DIR": str(root / "run"),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={root / 'run' / 'absent-bus'}",
        "WAYLAND_DISPLAY": "absent-owned-wayland", "CHUDWARE_NO_FILE_DIALOGS": "1",
        "CHUDWARE_ENGINE_DAEMON": "0", "CHUDWARE_INSTANCE_KEY": "entitlement-" + uuid.uuid4().hex,
        "CHUDWARE_SESSIONS_DIR": str(root / "sessions"),
        "CHUDWARE_SHOT": "entitlement_delayed", "CHUDWARE_HIDDEN": "1",
        "CHUDWARE_ENTITLEMENT_DELAYED_ROOT": str(root),
        "CHUDWARE_ENTITLEMENT_REFRESH_URL": endpoint,
        "CHUDWARE_UPDATE_MANIFEST_URL": endpoint.removesuffix("/refresh") + "/empty-updates",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "LIBGL_ALWAYS_SOFTWARE": "1",
    })
    if python:
        env["CHUDWARE_PYTHON"] = str(python)
    screenshot = root / "entitlement_delayed.png"
    try:
        with (root / "app.log").open("w", encoding="utf-8") as output:
            process = subprocess.Popen(
                ["xvfb-run", "-a", str(binary), "--ui-screenshot", str(screenshot)],
                cwd=root / "work", env=env, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=90)
            except BaseException:
                # Own process group only: never leave the GUI behind when its
                # xvfb-run wrapper times out, and never signal another session.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
        report = json.loads((root / "report.json").read_text(encoding="utf-8"))
        if failure or len(requests) != 1 or not (control / "sent.json").is_file():
            raise AssertionError(f"real held response did not complete exactly once: {failure}")
        if returncode != 0 or not report.get("ok"):
            raise AssertionError(f"account scenario failed: {report.get('failure')}; see {root / 'app.log'}")
        if not screenshot.is_file():
            raise AssertionError("account scenario did not capture the real UI")
        print(f"PASS real delayed HTTP completion -> signout -> tick: {root / 'report.json'}")
        return 0
    finally:
        (control / "release").touch(exist_ok=True)
        server.shutdown()
        thread.join()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chudware", type=Path)
    parser.add_argument("--output", type=Path, required=True,
                        help="new owned output directory (must not already exist)")
    parser.add_argument("--python", type=Path, help="provisioned CHUDWARE_PYTHON runtime")
    parser.add_argument("--outside-netns-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.outside_netns_fd is None:
        outside_fd = os.open("/proc/self/ns/net", os.O_RDONLY)
        try:
            command = [
                "unshare", "--user", "--map-root-user", "--net", "sh", "-c",
                'ip link set dev lo up && exec "$@"', "sh",
                sys.executable, str(Path(__file__).resolve()), *sys.argv[1:],
                "--outside-netns-fd", str(outside_fd),
            ]
            return subprocess.call(command, pass_fds=(outside_fd,))
        finally:
            os.close(outside_fd)
    return run(args.chudware.resolve(), args.output.resolve(),
               args.python.absolute() if args.python else None, args.outside_netns_fd)


if __name__ == "__main__":
    raise SystemExit(main())
