"""``packaging/linux/install.sh`` — the script customers pipe into a shell.

Everything here runs the REAL script with a real ``sh``. A shell script that is
only read and never executed is a shell script with bugs in it, and this one is
handed to people as ``curl -fsSL … | sh``, so the properties that matter are
behavioural: it refuses an architecture it cannot serve, it verifies the digest
from the signed manifest before installing anything, and it installs nothing at
all when that digest does not match.

The gateway is replaced by a small stub HTTP server rather than a booted app.
The script only ever sees two JSON documents and a stream of bytes, so a stub
covers the same surface deterministically — and the mismatch case, which needs a
server that lies about a digest, cannot be produced by the real gateway at all.
The real gateway is exercised by hand; see the module docstring of
``test_releases_ci.py`` for the publication side.
"""

from __future__ import annotations

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

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "packaging" / "linux" / "install.sh"

VERSION = "0.1.1"
APPIMAGE = f"Chudware-{VERSION}-linux-x86_64.AppImage"
DEB = f"chudware_{VERSION}_amd64.deb"
APPIMAGE_BYTES = b"#!/bin/sh\n# stand-in for an AppImage\nexit 0\n" + b"payload" * 512
APPIMAGE_SHA = hashlib.sha256(APPIMAGE_BYTES).hexdigest()
# Shaped like a real one (base64url email, expiry, MAC) but never verified here:
# the stub gateway only checks that the script PRESENTS it.
TOKEN = "dGVzdEBleGFtcGxlLmNvbQ.9999999999.deadbeef"


# ---------------------------------------------------------------------------
# Stub gateway
# ---------------------------------------------------------------------------


class _Gateway:
    """The three routes install.sh talks to, plus the lies each test needs."""

    def __init__(self) -> None:
        self.advertised_sha = APPIMAGE_SHA
        self.served_bytes = APPIMAGE_BYTES
        # The digest the SIGNED MANIFEST claims, when it should differ from the
        # catalogue's. None means "the same", which is the normal case.
        self.manifest_sha: str | None = None
        self.formats: tuple[str, ...] = ("default",)
        self.requests: list[str] = []
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
                                    "version": VERSION,
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
                    wants_deb = "format=deb" in query
                    manifest: dict[str, object] = {
                        "version": VERSION,
                        "min_supported": "0.1.0",
                        "platform": "linux",
                        "arch": "x86_64",
                        "format": "deb" if wants_deb else "default",
                        "key_id": "release-v1",
                        "sha256": gateway.manifest_sha or gateway.advertised_sha,
                        "size": len(gateway.served_bytes),
                        "signature": "c2ln",
                        "notes_url": "https://chudware.com/releases",
                    }
                    if has_token:
                        manifest["url"] = (
                            f"{gateway.base_url}/api/releases/artifact/"
                            f"stable/{VERSION}/{DEB if wants_deb else APPIMAGE}"
                        )
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
    *args: str, home: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the script with a private HOME so nothing lands in the real one."""
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
    }
    if env:
        environment.update(env)
    return subprocess.run(
        ["sh", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
        check=False,
    )


# ---------------------------------------------------------------------------
# Static properties
# ---------------------------------------------------------------------------


def test_the_script_is_valid_posix_sh() -> None:
    assert subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode == 0
    assert SCRIPT.read_text().startswith("#!/bin/sh\n")
    # Executable in the tree: somebody who downloads it instead of piping it
    # should be able to run it.
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck absent")
def test_shellcheck_is_clean_in_posix_sh_mode() -> None:
    result = subprocess.run(
        ["shellcheck", "-s", "sh", str(SCRIPT)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_script_avoids_the_curl_pipe_sh_anti_patterns() -> None:
    """A script people are told to pipe into a shell has to be defensible on
    sight: no eval, a cleanup trap, a real digest check, and no bash-ism that
    would fail on dash — which is /bin/sh on Debian and Ubuntu."""
    body = SCRIPT.read_text()
    assert "set -eu" in body
    assert "mktemp -d" in body
    assert "trap cleanup EXIT" in body
    assert "SHA256 MISMATCH" in body
    # Code only. The header comment discusses these constructs by name, and a
    # naive substring scan over the whole file would fail on the documentation
    # of the very rule it is checking.
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert "eval" not in code
    for bashism in ("[[ ", "]]", "function ", "declare ", "local ", "${BASH"):
        assert bashism not in code, bashism


def test_help_documents_every_option(tmp_path: Path) -> None:
    result = run_install("--help", home=tmp_path)
    assert result.returncode == 0, result.stderr
    for option in (
        "--token", "--channel", "--format", "--prefix", "--base-url", "--dry-run",
    ):
        assert option in result.stdout, option


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
    """The property that makes `curl | sh` defensible at all. The manifest is
    the signed document; the bytes must match it or nothing happens."""
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
    """Two independent statements of one digest. If they disagree, one of them
    is wrong and neither is trustworthy enough to install from."""
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
    result = run_install(
        "--dry-run", "--base-url", gateway.base_url, "--format", "deb",
        "--token", TOKEN, home=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "no .deb package is published" in result.stderr
    assert "Falling back to the AppImage" in result.stdout
    assert "format    default" in result.stdout


def test_a_published_package_format_is_selected_when_present(
    tmp_path: Path, gateway: _Gateway
) -> None:
    gateway.formats = ("default", "deb")
    result = run_install(
        "--dry-run", "--base-url", gateway.base_url, "--format", "deb",
        "--token", TOKEN, home=tmp_path,
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
        "cp", "wc", "sha256sum", "uname", "id", "mkdir", "cat",
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
