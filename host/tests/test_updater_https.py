#!/usr/bin/env python3
"""Exercise hostile updater artifacts over a real loopback TLS connection.

Release assets are served the way GitHub serves them: the asset URL answers
with a redirect to object storage, so the download must follow it and still
reject bytes that fail size, SHA-256 or Ed25519 verification.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading


_ARTIFACT = b"signed artifact bytes\n"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path.startswith("/releases/download/"):
            self.send_response(302)
            self.send_header("Location", "/objects/" + self.path.rsplit("/", 1)[-1])
            self.end_headers()
            return
        if self.path == "/redirect-plaintext":
            port = self.server.server_address[1]
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/objects/corrupt")
            self.end_headers()
            return
        if self.path not in ("/objects/corrupt", "/objects/wrong-key"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(_ARTIFACT)))
        self.end_headers()
        self.wfile.write(_ARTIFACT)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def _make_certificate(root: Path) -> tuple[Path, Path]:
    key = root / "server.key"
    cert = root / "server.crt"
    config = root / "openssl.cnf"
    config.write_text(
        "[req]\n"
        "distinguished_name=dn\n"
        "x509_extensions=ext\n"
        "prompt=no\n"
        "[dn]\nCN=localhost\n"
        "[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "basicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,digitalSignature,keyCertSign\n",
        encoding="utf-8",
    )
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-keyout", str(key), "-out", str(cert), "-config", str(config), cwd=root)
    return cert, key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("test_updater", type=Path)
    args = parser.parse_args()
    if shutil.which("openssl") is None:
        raise SystemExit("openssl is required for updater HTTPS regression test")

    with tempfile.TemporaryDirectory(prefix="chudware-updater-https-") as temporary:
        root = Path(temporary)
        cert, key = _make_certificate(root)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            env = os.environ.copy()
            env["CHUDWARE_UPDATE_CA_FILE"] = str(cert)
            for kind in ("corrupt", "wrong-key"):
                fixture = root / kind
                subprocess.run(
                    [str(args.test_updater), "--served", kind,
                     f"https://localhost:{port}/releases/download/v1.2.0/{kind}",
                     str(fixture)],
                    env=env, check=True,
                )
                assert not (fixture / "data" / "updates" / "staged.json").exists()

            # Even with the developer CA override, libcurl's protocol allowlist must
            # reject plaintext HTTP before accepting response bytes, whether it is
            # the artifact URL itself or a redirect target.
            for name, url in (
                ("plaintext", f"http://127.0.0.1:{port}/objects/corrupt"),
                ("redirect-plaintext", f"https://localhost:{port}/redirect-plaintext"),
            ):
                fixture = root / name
                failed = subprocess.run(
                    [str(args.test_updater), "--served", "corrupt", url, str(fixture)],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                if failed.returncode == 0:
                    raise AssertionError(f"updater accepted a plaintext HTTP artifact ({name})")
                if (fixture / "data" / "updates" / "staged.json").exists():
                    raise AssertionError(f"plaintext HTTP artifact was staged ({name})")

            # A missing release manifest (404) is the normal pre-release state and
            # must read as a quiet "up to date", not an HTTP error. The manifest
            # URL redirects first, exactly like GitHub's releases/latest/download.
            # The manifest fetch goes over plain loopback here: only artifact
            # downloads are pinned to TLS, and the manifest carries no secrets.
            class _NoRelease(http.server.BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802
                    if self.path.startswith("/releases/latest/download/"):
                        self.send_response(302)
                        self.send_header("Location", "/releases/download/v0/missing.json")
                        self.end_headers()
                        return
                    self.send_error(404)

                def log_message(self, *_args: object) -> None:
                    pass

            manifest_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _NoRelease)
            manifest_thread = threading.Thread(target=manifest_server.serve_forever, daemon=True)
            manifest_thread.start()
            try:
                manifest_port = manifest_server.server_address[1]
                subprocess.run(
                    [str(args.test_updater), "--check-once",
                     f"http://127.0.0.1:{manifest_port}"
                     "/releases/latest/download/update-linux-x86_64.json",
                     str(root / "no-release")],
                    env=env, check=True,
                )
            finally:
                manifest_server.shutdown()
                manifest_thread.join()
                manifest_server.server_close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
