"""``packaging/linux/install.sh`` — the script customers pipe into a shell.

The tests run the customer script with a real ``sh`` and a loopback HTTP gateway.
Fixture bytes are inert and never executed. Positive installs use a disposable,
clearly test-only script copy with one pinned public key replaced; the customer
script has no key override. Ed25519 verification still runs through local OpenSSL.

Loopback smoke (no production, credentials or release signing key):
``python -m pytest Webservice/gateway/tests/test_install_script.py -q``
exercises signed AppImage placement in a temporary prefix, deb/rpm handoff to
recording package-manager shims, truthful dry-run plans and rejection before
placement/escalation for unsigned, malformed, tampered and substituted releases.
It never launches the installed artifact or invokes a real package manager.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Iterator

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "packaging" / "linux" / "install.sh"

VERSION = "0.1.1"
APPIMAGE = f"Chudware-{VERSION}-linux-x86_64.AppImage"
DEB = f"chudware_{VERSION}_amd64.deb"
RPM = f"chudware-{VERSION}-1.x86_64.rpm"
APPIMAGE_BYTES = b"Chudware inert installer fixture; this is not an executable.\n" + b"payload" * 512
APPIMAGE_SHA = hashlib.sha256(APPIMAGE_BYTES).hexdigest()
# Shaped like a real one (base64url email, expiry, MAC) but never verified here:
# the stub gateway only checks that the script PRESENTS it.
TOKEN = "dGVzdEBleGFtcGxlLmNvbQ.9999999999.deadbeef"

# Public, deterministic TEST-ONLY material, never a product release signing key.
TEST_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
TEST_PUBLIC_KEY_DER = base64.b64encode(
    TEST_PRIVATE_KEY.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
).decode("ascii")
PRODUCT_PUBLIC_KEY_DER = "MCowBQYDK2VwAyEAKay64UG8yvCyLhqU000LxzYeUm0L/hLIl5S8kyKWbdc="


def signed_fixture(
    *, key: Ed25519PrivateKey = TEST_PRIVATE_KEY, key_id: str = "release-v1",
    version: str = VERSION, platform: str = "linux", arch: str = "x86_64",
    digest: str = APPIMAGE_SHA,
) -> str:
    preimage = b"\0".join(
        value.encode("ascii")
        for value in ("chudware-artifact-v1", key_id, version, platform, arch, digest)
    )
    return base64.b64encode(key.sign(preimage)).decode("ascii")


# ---------------------------------------------------------------------------
# Stub gateway
# ---------------------------------------------------------------------------


class _Gateway:
    """The three routes install.sh talks to, plus the lies each test needs."""

    def __init__(self) -> None:
        self.advertised_sha = APPIMAGE_SHA
        self.served_bytes = APPIMAGE_BYTES
        # None means the manifest and catalogue advertise the same digest.
        self.manifest_sha: str | None = None
        self.formats: tuple[str, ...] = ("default",)
        self.requests: list[str] = []
        self.catalogue_version = VERSION
        self.manifest_overrides: dict[str, object] = {}
        self.omitted_manifest_fields: set[str] = set()
        self._server: http.server.ThreadingHTTPServer | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args: object) -> None:
                pass

            def _json(self, payload: dict[str, object], status: int = 200) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
                gateway.requests.append(self.path)
                path, _, query = self.path.partition("?")
                has_token = "download_token=" in query
                if path == "/api/releases/available":
                    self._json(
                        {
                            "channel": "beta" if "channel=beta" in query else "stable",
                            "builds": [
                                {
                                    "platform": "linux",
                                    "arch": "x86_64",
                                    "format": fmt,
                                    "version": gateway.catalogue_version,
                                    "size": len(gateway.served_bytes),
                                    "sha256": gateway.advertised_sha,
                                    "notes_url": "https://chudware.com/releases",
                                }
                                for fmt in gateway.formats
                            ],
                        }
                    )
                    return
                if path == "/api/releases/latest":
                    fmt = next(
                        (item for item in ("deb", "rpm") if f"format={item}" in query),
                        "default",
                    )
                    manifest: dict[str, object] = {
                        "version": VERSION,
                        "min_supported": "0.1.0",
                        "platform": "linux",
                        "arch": "x86_64",
                        "format": fmt,
                        "key_id": "release-v1",
                        "sha256": gateway.manifest_sha or gateway.advertised_sha,
                        "size": len(gateway.served_bytes),
                        "signature": signed_fixture(),
                        "notes_url": "https://chudware.com/releases",
                    }
                    if has_token:
                        manifest["url"] = (
                            f"{gateway.base_url}/api/releases/artifact/"
                            f"stable/{VERSION}/{ {'deb': DEB, 'rpm': RPM}.get(fmt, APPIMAGE)}"
                        )
                    manifest.update(gateway.manifest_overrides)
                    for field in gateway.omitted_manifest_fields:
                        manifest.pop(field, None)
                    self._json(manifest)
                    return
                if path.startswith("/api/releases/artifact/"):
                    if not has_token:
                        self._json({"detail": "entitlement required"}, status=403)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(gateway.served_bytes)))
                    self.end_headers()
                    self.wfile.write(gateway.served_bytes)
                    return
                self._json({"detail": "not found"}, status=404)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def gateway() -> Iterator[_Gateway]:
    stub = _Gateway()
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


def run_install(
    *args: str, home: Path, env: dict[str, str] | None = None,
    product_pins: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Use private HOME and, normally, a TEST-ONLY pinned-key script copy."""
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
    }
    if env:
        environment.update(env)
    script = SCRIPT
    if not product_pins:
        script = home / "TEST-ONLY-install.sh"
        body = SCRIPT.read_text()
        assert body.count(PRODUCT_PUBLIC_KEY_DER) == 1
        script.write_text(
            body.replace(PRODUCT_PUBLIC_KEY_DER, TEST_PUBLIC_KEY_DER)
        )
    return subprocess.run(
        ["sh", str(script), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        check=False,
    )


def recording_package_managers(home: Path) -> tuple[dict[str, str], Path]:
    """Never invoke real package managers or sudo, even on a root test runner."""
    shim = home / "package-shims"
    shim.mkdir()
    log = home / "package-manager.log"
    (shim / "id").write_text("#!/bin/sh\nprintf '0\\n'\n")
    (shim / "id").chmod(0o755)
    for tool in ("apt-get", "dpkg", "dnf", "yum", "zypper", "rpm", "sudo"):
        executable = shim / tool
        executable.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$PACKAGE_MANAGER_LOG"\nexit 0\n'
        )
        executable.chmod(0o755)
    return {
        "PATH": f"{shim}:{os.environ['PATH']}",
        "PACKAGE_MANAGER_LOG": str(log),
    }, log


# ---------------------------------------------------------------------------
# Static properties
# ---------------------------------------------------------------------------



@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("--channel", "nightly"), "--channel must be stable or beta"),
        (("--format", "snap"), "--format must be auto, deb, rpm or appimage"),
        (("--base-url", "ftp://example.com"), "--base-url must be an http(s) URL"),
        (("--token",), "--token needs a value"),
        (("--frobnicate",), "unknown option"),
    ],
)
def test_bad_arguments_fail_loudly(
    tmp_path: Path, args: tuple[str, ...], expected: str
) -> None:
    result = run_install(*args, home=tmp_path)
    assert result.returncode != 0
    assert expected in result.stderr


def test_a_non_x86_64_machine_is_refused_with_a_clear_message(tmp_path: Path) -> None:
    """Auto-detection must never install an x86_64 binary on ARM."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text(
        '#!/bin/sh\ncase "$1" in\n  -m) echo aarch64 ;;\n  *) echo Linux ;;\nesac\n'
    )
    uname.chmod(0o755)
    result = run_install(
        "--dry-run", home=tmp_path,
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "ARM64" in result.stderr
    assert "aarch64" in result.stderr
    # It says what to do instead of only failing.
    assert "browser workspace" in result.stderr


# ---------------------------------------------------------------------------
# Against a stub gateway
# ---------------------------------------------------------------------------


def test_dry_run_resolves_a_real_plan_and_changes_nothing(
    tmp_path: Path, gateway: _Gateway
) -> None:
    prefix = tmp_path / "bin"
    result = run_install(
        "--dry-run", "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix), "--token", TOKEN,
        home=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert VERSION in result.stdout
    assert APPIMAGE_SHA in result.stdout
    assert "Dry run" in result.stdout
    assert "Would download" in result.stdout
    assert "Artifact NOT authenticated" in result.stdout
    assert "Ed25519" in result.stdout
    assert not prefix.exists()
    # It really asked the gateway rather than guessing, and downloaded nothing.
    assert any(path.startswith("/api/releases/available") for path in gateway.requests)
    assert any(path.startswith("/api/releases/latest") for path in gateway.requests)
    assert not any("/artifact/" in path for path in gateway.requests)


def test_dry_run_without_a_token_says_so_instead_of_failing(
    tmp_path: Path, gateway: _Gateway
) -> None:
    result = run_install(
        "--dry-run", "--base-url", gateway.base_url, "--format", "appimage",
        home=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "No download token was given" in result.stdout


def test_a_stale_tmpdir_does_not_break_the_install(
    tmp_path: Path, gateway: _Gateway
) -> None:
    """Found by running this against a live gateway: a TMPDIR pointing at a
    directory that does not exist made mktemp fail with "failed to create
    directory via template" and no hint which variable was wrong. Service
    accounts and CI shells export stale TMPDIRs routinely, so the installer
    warns and falls back to /tmp instead of dying."""
    prefix = tmp_path / "bin"
    result = run_install(
        "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix), "--token", TOKEN,
        home=tmp_path, env={"TMPDIR": str(tmp_path / "gone")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TMPDIR is not a writable directory" in result.stderr
    assert (prefix / "chudware").read_bytes() == APPIMAGE_BYTES
    # The fallback scratch directory is still removed by the trap.
    assert not list(Path("/tmp").glob("chudware-install.*"))


def test_a_real_appimage_install_lands_in_the_prefix_with_a_desktop_entry(
    tmp_path: Path, gateway: _Gateway
) -> None:
    prefix = tmp_path / "opt" / "bin"
    result = run_install(
        "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix), "--token", TOKEN,
        home=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    installed = prefix / "chudware"
    assert installed.is_file()
    assert installed.read_bytes() == APPIMAGE_BYTES
    assert os.access(installed, os.X_OK)
    assert "sha256 OK" in result.stdout
    assert "Ed25519 artifact signature OK" in result.stdout

    desktop = tmp_path / ".local" / "share" / "applications" / "chudware.desktop"
    assert desktop.is_file()
    entry = desktop.read_text()
    assert f"Exec={installed} %f" in entry
    assert "StartupWMClass=chudware" in entry

    # No partial download and no scratch directory survive the run.
    assert list((tmp_path / "tmp").glob("chudware-install.*")) == []
    assert list(prefix.glob("*.part")) == []
    assert list(prefix.glob("*.new")) == []

    # The artifact fetch carried the token; the route 403s without it.
    assert any(
        "/artifact/" in path and "download_token=" in path
        for path in gateway.requests
    )


def test_a_sha256_mismatch_aborts_and_installs_nothing(
    tmp_path: Path, gateway: _Gateway
) -> None:
    """A mismatched download must be rejected before any install."""
    gateway.served_bytes = b"substituted bytes that are not the release" * 64
    prefix = tmp_path / "bin"
    result = run_install(
        "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix), "--token", TOKEN,
        home=tmp_path,
    )
    assert result.returncode != 0
    assert "SHA256 MISMATCH" in result.stderr
    assert APPIMAGE_SHA in result.stderr
    assert "Nothing has been installed" in result.stderr
    assert not (prefix / "chudware").exists()
    assert not (
        tmp_path / ".local" / "share" / "applications" / "chudware.desktop"
    ).exists()
    assert list((tmp_path / "tmp").glob("chudware-install.*")) == []


def test_a_catalogue_that_disagrees_with_the_manifest_aborts(
    tmp_path: Path, gateway: _Gateway
) -> None:
    """Consistency is required but is not proof of authenticity."""
    gateway.manifest_sha = "0" * 64
    prefix = tmp_path / "bin"
    result = run_install(
        "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix), "--token", TOKEN,
        home=tmp_path,
    )
    assert result.returncode != 0
    assert "disagree about the sha256" in result.stderr
    assert not prefix.exists()
    assert not any("/artifact/" in path for path in gateway.requests)


def test_no_token_refuses_before_downloading_and_says_how_to_get_one(
    tmp_path: Path, gateway: _Gateway
) -> None:
    prefix = tmp_path / "bin"
    result = run_install(
        "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix),
        home=tmp_path,
    )
    assert result.returncode != 0
    assert "no download token" in result.stderr
    assert "Downloads" in result.stderr
    assert not prefix.exists()
    assert not any("/artifact/" in path for path in gateway.requests)


def test_the_token_may_come_from_the_environment(
    tmp_path: Path, gateway: _Gateway
) -> None:
    prefix = tmp_path / "bin"
    result = run_install(
        "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix),
        home=tmp_path, env={"CHUDWARE_TOKEN": TOKEN},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (prefix / "chudware").read_bytes() == APPIMAGE_BYTES


def test_a_missing_package_format_falls_back_to_the_appimage(
    tmp_path: Path, gateway: _Gateway
) -> None:
    """Only the AppImage is published in this fixture, so asking for a .deb has
    to degrade to something installable instead of dead-ending."""
    environment, _ = recording_package_managers(tmp_path)
    result = run_install(
        "--dry-run", "--base-url", gateway.base_url, "--format", "deb",
        "--token", TOKEN, home=tmp_path, env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "no .deb package is published" in result.stderr
    assert "Falling back to the AppImage" in result.stdout
    assert "format    default" in result.stdout


def test_a_published_package_format_is_selected_when_present(
    tmp_path: Path, gateway: _Gateway
) -> None:
    gateway.formats = ("default", "deb")
    environment, _ = recording_package_managers(tmp_path)
    result = run_install(
        "--dry-run", "--base-url", gateway.base_url, "--format", "deb",
        "--token", TOKEN, home=tmp_path, env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "format    deb" in result.stdout
    assert "/opt/chudware" in result.stdout
    assert DEB in result.stdout


def test_the_channel_reaches_the_gateway(tmp_path: Path, gateway: _Gateway) -> None:
    result = run_install(
        "--dry-run", "--base-url", gateway.base_url, "--channel", "beta",
        "--format", "appimage", "--token", TOKEN, home=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "/api/releases/available?channel=beta" in gateway.requests
    assert any(
        "channel=beta" in path for path in gateway.requests if "latest" in path
    )


def test_an_unreachable_gateway_fails_with_the_url_it_tried(tmp_path: Path) -> None:
    result = run_install(
        "--dry-run", "--base-url", "http://127.0.0.1:1", "--format", "appimage",
        home=tmp_path,
    )
    assert result.returncode != 0
    assert "could not reach http://127.0.0.1:1/api/releases/available" in result.stderr


@pytest.mark.skipif(shutil.which("wget") is None, reason="wget absent")
def test_the_script_works_with_wget_and_no_curl(
    tmp_path: Path, gateway: _Gateway
) -> None:
    """A minimal container usually has one of the two, never both."""
    shim = tmp_path / "nocurl"
    shim.mkdir()
    for tool in (
        "sh", "wget", "sed", "grep", "tr", "cut", "mktemp", "mv", "rm", "chmod",
        "cp", "wc", "sha256sum", "uname", "id", "mkdir", "cat", "openssl",
    ):
        found = shutil.which(tool)
        if found:
            (shim / tool).symlink_to(found)
    assert shutil.which("curl", path=str(shim)) is None
    prefix = tmp_path / "bin"
    result = run_install(
        "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix), "--token", TOKEN,
        home=tmp_path, env={"PATH": str(shim)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (prefix / "chudware").read_bytes() == APPIMAGE_BYTES


# ---------------------------------------------------------------------------
# Trust boundary: matching mutable hashes are not artifact authentication
# ---------------------------------------------------------------------------


def invalid_release(gateway: _Gateway, attack: str) -> None:
    if attack == "unsigned":
        gateway.omitted_manifest_fields.add("signature")
    elif attack == "missing-key":
        gateway.omitted_manifest_fields.add("key_id")
    elif attack == "empty-signature":
        gateway.manifest_overrides["signature"] = ""
    elif attack == "malformed-signature":
        gateway.manifest_overrides["signature"] = "c2ln"
    elif attack == "invalid-base64":
        gateway.manifest_overrides["signature"] = "!" * 86 + "=="
    elif attack == "noncanonical-base64":
        signature = signed_fixture()
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        # Change only unused padding bits: same decoded 64-byte signature.
        last = alphabet[alphabet.index(signature[-3]) + 1]
        gateway.manifest_overrides["signature"] = signature[:-3] + last + "=="
    elif attack == "unknown-key":
        gateway.manifest_overrides.update(
            key_id="attacker",
            signature=signed_fixture(key_id="attacker"),
            public_key=TEST_PUBLIC_KEY_DER,
        )
    elif attack == "wrong-pinned-key":
        gateway.manifest_overrides.update(
            key_id="release-ci-v1",
            signature=signed_fixture(key_id="release-ci-v1"),
            public_key=TEST_PUBLIC_KEY_DER,
        )
    elif attack == "wrong-signing-key":
        other_key = Ed25519PrivateKey.from_private_bytes(b"\x99" * 32)
        gateway.manifest_overrides["signature"] = signed_fixture(key=other_key)
    elif attack == "modified-signature":
        signature = bytearray(base64.b64decode(signed_fixture()))
        signature[0] ^= 1
        gateway.manifest_overrides["signature"] = base64.b64encode(signature).decode()
    elif attack == "modified-bytes-matching-hashes":
        gateway.served_bytes = b"inert substituted bytes\n" * 128
        gateway.advertised_sha = hashlib.sha256(gateway.served_bytes).hexdigest()
        gateway.manifest_sha = gateway.advertised_sha
    elif attack == "version-substitution":
        gateway.catalogue_version = "9.9.9"
        gateway.manifest_overrides["version"] = "9.9.9"
    elif attack in {"platform-substitution", "arch-substitution", "format-substitution"}:
        field = attack.partition("-")[0]
        gateway.manifest_overrides[field] = {
            "platform": "windows", "arch": "arm64", "format": "substituted"
        }[field]
    elif attack == "signature-for-other-version":
        gateway.manifest_overrides["signature"] = signed_fixture(version="9.9.9")
    elif attack == "signature-for-other-platform":
        gateway.manifest_overrides["signature"] = signed_fixture(platform="windows")
    elif attack == "signature-for-other-arch":
        gateway.manifest_overrides["signature"] = signed_fixture(arch="arm64")
    elif attack == "signature-without-domain":
        signature = TEST_PRIVATE_KEY.sign(APPIMAGE_BYTES)
        gateway.manifest_overrides["signature"] = base64.b64encode(signature).decode()
    else:
        raise AssertionError(f"unknown test attack: {attack}")


@pytest.mark.parametrize("fmt", ["appimage", "deb", "rpm"])
@pytest.mark.parametrize(
    "attack",
    [
        "unsigned", "missing-key", "empty-signature", "malformed-signature",
        "invalid-base64", "noncanonical-base64", "unknown-key", "wrong-pinned-key",
        "wrong-signing-key", "modified-signature", "modified-bytes-matching-hashes",
        "version-substitution", "platform-substitution", "arch-substitution",
        "format-substitution", "signature-for-other-version",
        "signature-for-other-platform", "signature-for-other-arch",
        "signature-without-domain",
    ],
)
def test_untrusted_artifact_never_installs_or_escalates(
    tmp_path: Path, gateway: _Gateway, fmt: str, attack: str,
) -> None:
    gateway.formats = ("default", "deb", "rpm")
    invalid_release(gateway, attack)
    environment, package_log = recording_package_managers(tmp_path)
    prefix = tmp_path / "prefix"
    result = run_install(
        "--base-url", gateway.base_url, "--format", fmt,
        "--prefix", str(prefix), "--token", TOKEN,
        home=tmp_path, env=environment,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert not prefix.exists()
    assert not package_log.exists()
    assert not (tmp_path / ".local/share/applications/chudware.desktop").exists()
    assert not list((tmp_path / "tmp").glob("chudware-install.*"))
    if attack in {
        "modified-bytes-matching-hashes", "version-substitution",
        "wrong-pinned-key", "wrong-signing-key", "modified-signature",
        "signature-for-other-version", "signature-for-other-platform",
        "signature-for-other-arch", "signature-without-domain",
    }:
        assert "sha256 OK" in result.stdout
        assert "ARTIFACT SIGNATURE VERIFICATION FAILED" in result.stderr


@pytest.mark.parametrize(
    ("fmt", "package_manager", "filename"),
    [("deb", "apt-get", "chudware.deb"), ("rpm", "dnf", "chudware.rpm")],
)
def test_signed_package_reaches_only_the_recording_package_manager(
    tmp_path: Path, gateway: _Gateway, fmt: str, package_manager: str, filename: str,
) -> None:
    gateway.formats = ("default", "deb", "rpm")
    environment, package_log = recording_package_managers(tmp_path)
    result = run_install(
        "--base-url", gateway.base_url, "--format", fmt, "--token", TOKEN,
        home=tmp_path, env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ed25519 artifact signature OK" in result.stdout
    calls = package_log.read_text().splitlines()
    assert len(calls) == 1
    assert f"/{package_manager} install -y " in calls[0]
    assert calls[0].endswith(filename)
    assert not list((tmp_path / "tmp").glob("chudware-install.*"))


def test_product_script_rejects_test_key_despite_environment_and_manifest_keys(
    tmp_path: Path, gateway: _Gateway,
) -> None:
    gateway.manifest_overrides["public_key"] = TEST_PUBLIC_KEY_DER
    prefix = tmp_path / "prefix"
    result = run_install(
        "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix), "--token", TOKEN, home=tmp_path,
        product_pins=True,
        env={
            "CHUDWARE_RELEASE_PUBLIC_KEY": TEST_PUBLIC_KEY_DER,
            "CHUDWARE_RELEASE_PUBLIC_KEY_HEX":
                TEST_PRIVATE_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
            "CHUDWARE_RELEASE_KEY_FILE": str(tmp_path / "attacker-key"),
            "CHUDWARE_DEVELOPER_BUILD": "1",
        },
    )
    assert result.returncode != 0
    assert "ARTIFACT SIGNATURE VERIFICATION FAILED" in result.stderr
    assert not prefix.exists()


def test_missing_local_verifier_refuses_without_downloading_one(
    tmp_path: Path, gateway: _Gateway,
) -> None:
    shim = tmp_path / "no-openssl"
    shim.mkdir()
    for tool in ("sh", "curl", "wget", "uname", "id", "sed", "tr", "grep"):
        found = shutil.which(tool)
        if found:
            (shim / tool).symlink_to(found)
    prefix = tmp_path / "prefix"
    result = run_install(
        "--base-url", gateway.base_url, "--format", "appimage",
        "--prefix", str(prefix), "--token", TOKEN,
        home=tmp_path, env={"PATH": str(shim)},
    )
    assert result.returncode != 0
    assert "OpenSSL 3 or newer" in result.stderr
    assert not prefix.exists()
    assert not any("/artifact/" in path for path in gateway.requests)
