#!/usr/bin/env python3
"""Exercise updater staging and hostile artifacts over loopback TLS.

Release assets are served the way GitHub serves them: the asset URL answers
with a redirect to object storage. Legacy minimum-version metadata must never
block the app or bypass size, SHA-256, outer Ed25519 or inner payload verification.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import http.server
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import zipfile


_ARTIFACT = b"signed artifact bytes\n"


class _Handler(http.server.BaseHTTPRequestHandler):
    signed_artifact = b""

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
        if self.path == "/objects/signed-update":
            body = self.signed_artifact
        elif self.path in ("/objects/corrupt", "/objects/wrong-key"):
            body = _ARTIFACT
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


def _make_signed_update(root: Path) -> tuple[bytes, dict[str, object]]:
    # Public deterministic regression key, seed 00..1f. Packaged builds never
    # trust dev-test-v1; no release credentials are used by this fixture.
    key = root / "developer-signing-key.der"
    key.write_bytes(bytes.fromhex("302e020100300506032b657004220420") + bytes(range(32)))

    def sign(name: str, preimage: bytes) -> str:
        source = root / f"{name}.preimage"
        signature = root / f"{name}.sig"
        source.write_bytes(preimage)
        _openssl("pkeyutl", "-sign", "-inkey", str(key), "-keyform", "DER",
                 "-rawin", "-in", str(source), "-out", str(signature), cwd=root)
        return base64.b64encode(signature.read_bytes()).decode("ascii")

    target = "macos" if sys.platform == "darwin" else "linux"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    payload = b"verified updater payload\n"
    inner = json.dumps({
        "schema": "chudware-payload-v1",
        "product": "Chudware",
        "version": "1.2.2",
        "platform": target,
        "arch": arch,
        "created_at": "2026-08-12T00:00:00Z",
        "key_id": "dev-test-v1",
        "files": [{
            "path": "fixture.txt",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": 0o644,
            "kind": "resource",
        }],
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    inner_signature = sign("payload", b"chudware-payload-v1\0dev-test-v1\0" + inner)
    if target == "macos":
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            for name, data in (
                ("fixture.txt", payload),
                ("payload-manifest.v1.json", inner),
                ("payload-manifest.v1.sig", inner_signature.encode("ascii")),
            ):
                entry = zipfile.ZipInfo(f"Chudware.app/{name}")
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                bundle.writestr(entry, data)
        artifact = archive.getvalue()
    else:
        # A signed, inspectable AppImage-style fixture exercises the same
        # extraction and full inner-payload verification as a network update.
        artifact = (
            "#!/bin/sh\n"
            "[ \"$1\" = --appimage-extract ] || exit 2\n"
            "mkdir -p squashfs-root\n"
            f"printf '%s' '{inner.decode('utf-8')}' > squashfs-root/payload-manifest.v1.json\n"
            f"printf '%s' '{inner_signature}' > squashfs-root/payload-manifest.v1.sig\n"
            "printf 'verified updater payload\\n' > squashfs-root/fixture.txt\n"
            "chmod 0644 squashfs-root/fixture.txt\n"
        ).encode("utf-8")
    digest = hashlib.sha256(artifact).hexdigest()
    outer_signature = sign("artifact", b"\0".join(field.encode("ascii") for field in (
        "chudware-artifact-v1", "dev-test-v1", "1.2.2", target, arch, digest,
    )))
    return artifact, {
        "version": "1.2.2",
        "min_supported": "999.0.0",
        "platform": target,
        "arch": arch,
        "key_id": "dev-test-v1",
        "sha256": digest,
        "size": len(artifact),
        "signature": outer_signature,
        "notes_url": "https://updates.example.test/releases/1.2.2",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("test_updater", type=Path)
    args = parser.parse_args()
    if shutil.which("openssl") is None:
        raise SystemExit("openssl is required for updater HTTPS regression test")

    with tempfile.TemporaryDirectory(prefix="chudware-updater-https-") as temporary:
        root = Path(temporary)
        cert, key = _make_certificate(root)
        _Handler.signed_artifact, signed_manifest = _make_signed_update(root)
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

            # Manifest metadata itself carries no secrets; loopback HTTP keeps
            # this fixture independent of the host HTTP client's CA handling.
            # Artifact downloads and redirects still traverse real TLS.
            signed_manifest["url"] = (
                f"https://localhost:{port}/releases/download/v1.2.2/signed-update"
            )
            manifests = {
                "/metadata/staged.json": signed_manifest,
                "/metadata/current.json": signed_manifest,
                "/metadata/available.json": {**signed_manifest, "url": ""},
                "/metadata/invalid-signature.json": {
                    **signed_manifest,
                    "signature": base64.b64encode(bytes(64)).decode("ascii"),
                },
                "/metadata/no-minimum.json": {
                    key: value for key, value in signed_manifest.items()
                    if key != "min_supported"
                },
            }

            class _Manifests(http.server.BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802
                    if self.path.startswith("/releases/latest/download/"):
                        self.send_response(302)
                        self.send_header("Location", "/releases/download/v0/missing.json")
                        self.end_headers()
                        return
                    if self.path in manifests:
                        body = json.dumps(manifests[self.path]).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_error(404)

                def log_message(self, *_args: object) -> None:
                    pass

            manifest_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Manifests)
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
                # A much newer legacy minimum cannot pin a mandatory state:
                # valid updates stage, current builds stay current, and a
                # notes-only update stays optional. The same metadata cannot
                # make an invalid signature acceptable.
                for kind, name in (
                    ("staged", "staged"),
                    ("staged", "no-minimum"),
                    ("current", "current"),
                    ("available", "available"),
                    ("invalid-signature", "invalid-signature"),
                ):
                    subprocess.run(
                        [str(args.test_updater), "--manifest-metadata", kind,
                         f"http://127.0.0.1:{manifest_port}/metadata/{name}.json",
                         str(root / f"metadata-{name}")],
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
