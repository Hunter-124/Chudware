"""Deterministic offline tests for tools.provision_renode (no network).

Monkeypatches PATH / known roots / GitHub fetch so present/absent cases are
reproducible. Prints wall-times (no expected-time gating).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import time
from pathlib import Path

import pytest

# Ensure repo root is importable when pytest is launched from elsewhere.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tools import provision_renode as pr  # noqa: E402


@pytest.fixture
def clean_path(monkeypatch, tmp_path):
    """Strip renode from PATH and known roots; isolate HOME/LOCALAPPDATA."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv("CHUDWARE_RENODE", raising=False)
    monkeypatch.delenv("RENODE_ROOT", raising=False)
    # Empty PATH so shutil.which cannot find a real renode.
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin"))
    (tmp_path / "empty_bin").mkdir()
    (tmp_path / "home").mkdir()
    # Rebuild known-root constants against the temp HOME.
    monkeypatch.setattr(
        pr, "_KNOWN_ROOTS_POSIX",
        (
            Path(tmp_path / "home") / ".local" / "opt" / "renode",
            Path(tmp_path / "home") / ".local" / "share" / "Chudware" / "renode",
        ),
    )
    monkeypatch.setattr(
        pr, "_KNOWN_ROOTS_WIN",
        (
            Path(tmp_path / "local") / "Programs" / "renode",
        ),
    )
    def no_network(*_args, **_kwargs):
        pytest.fail("Renode provisioning tests must not access the network")

    monkeypatch.setattr(pr.urllib.request, "urlopen", no_network)
    yield tmp_path

def test_report_absent_exits_1(clean_path, capsys):
    t0 = time.perf_counter()
    rc = pr.report()
    dt = time.perf_counter() - t0
    assert rc == 1
    out = capsys.readouterr().out
    assert "No Renode detected" in out
    print(f"WALL_TIME test_report_absent_exits_1={dt:.4f}s", file=sys.stderr)


def test_status_absent(clean_path):
    t0 = time.perf_counter()
    st = pr.status()
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_status_absent={dt:.4f}s", file=sys.stderr)
    assert st["tool"] == "renode"
    assert st["installed"] is False
    assert st["path"] is None
    assert st["hint"]


def test_report_present_via_path(clean_path, monkeypatch, capsys):
    bin_dir = clean_path / "empty_bin"
    fake = bin_dir / ("renode.exe" if os.name == "nt" else "renode")
    fake.write_text("#!/bin/sh\necho 'Renode v9.9.9-test'\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    def _fake_version(binary):
        return "Renode v9.9.9-test"

    monkeypatch.setattr(pr, "probe_version", _fake_version)

    t0 = time.perf_counter()
    rc = pr.report()
    dt = time.perf_counter() - t0
    assert rc == 0
    out = capsys.readouterr().out
    assert "Renode detected" in out
    assert "v9.9.9-test" in out
    print(f"WALL_TIME test_report_present_via_path={dt:.4f}s", file=sys.stderr)


def test_status_present_via_env_root(clean_path, monkeypatch):
    root = clean_path / "custom_renode"
    root.mkdir()
    binary = root / ("renode.exe" if os.name == "nt" else "renode")
    binary.write_text("x", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("CHUDWARE_RENODE", str(root))
    monkeypatch.setattr(pr, "probe_version", lambda _b: "Renode v1.0-env")

    t0 = time.perf_counter()
    st = pr.status()
    dt = time.perf_counter() - t0
    print(f"WALL_TIME test_status_present_via_env_root={dt:.4f}s", file=sys.stderr)
    assert st["installed"] is True
    assert Path(st["path"]) == binary
    assert st["version"] == "Renode v1.0-env"
    assert st["hint"] is None


def test_main_json_absent(clean_path, capsys):
    t0 = time.perf_counter()
    rc = pr.main(["--json"])
    dt = time.perf_counter() - t0
    assert rc == 1
    out = capsys.readouterr().out
    assert '"installed": false' in out or '"installed":false' in out.replace(" ", "")
    print(f"WALL_TIME test_main_json_absent={dt:.4f}s", file=sys.stderr)


def test_install_idempotent_when_present(clean_path, monkeypatch, capsys):
    bin_dir = clean_path / "empty_bin"
    fake = bin_dir / ("renode.exe" if os.name == "nt" else "renode")
    fake.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(pr, "probe_version", lambda _b: "Renode v9.9.9")

    called = {"n": 0}

    def _no_network(*_a, **_k):
        called["n"] += 1
        raise AssertionError("must not hit network when already present")

    monkeypatch.setattr(pr, "_release_asset", _no_network)

    t0 = time.perf_counter()
    ok = pr.install()
    dt = time.perf_counter() - t0
    assert ok is True
    assert called["n"] == 0
    out = capsys.readouterr().out
    assert "already available" in out
    print(f"WALL_TIME test_install_idempotent_when_present={dt:.4f}s", file=sys.stderr)


@pytest.fixture
def portable_archive(clean_path, monkeypatch):
    """Provide a tiny portable release entirely through a mocked downloader."""
    binary_name = pr._BINARY_NAMES[0]
    binary_contents = b"fixture Renode executable\n"
    contents = io.BytesIO()
    with tarfile.open(fileobj=contents, mode="w:gz") as archive:
        member = tarfile.TarInfo(f"portable/{binary_name}")
        member.size = len(binary_contents)
        member.mode = 0o755
        archive.addfile(member, io.BytesIO(binary_contents))
    archive_bytes = contents.getvalue()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    calls = []
    asset = {
        "name": "renode-test-portable.tar.gz",
        "browser_download_url": "https://example.invalid/renode-test-portable.tar.gz",
    }

    def release_asset(tag):
        assert tag == "vfixture"
        calls.append("resolve")
        return asset

    def download(url, dest):
        assert url == asset["browser_download_url"]
        calls.append("download")
        dest.write_bytes(archive_bytes)

    monkeypatch.setattr(pr, "_release_asset", release_asset)
    monkeypatch.setattr(pr, "_download", download)
    monkeypatch.setattr(pr, "probe_version", lambda _binary: "Renode fixture")
    return digest, binary_name, binary_contents, calls


@pytest.mark.parametrize("digest", [None, "", "abc", "0" * 63, "0" * 65, "g" * 64])
def test_install_refuses_missing_or_invalid_digest(
    clean_path, portable_archive, monkeypatch, capsys, digest
):
    def unexpected_extract(*_args):
        pytest.fail("An archive without a valid digest pin must not be extracted")

    monkeypatch.setattr(pr, "_extract_archive", unexpected_extract)
    assert pr.install(release="vfixture", sha256=digest) is False
    assert portable_archive[3] == []
    assert not pr._install_root().exists()
    assert "SHA256 pin" in capsys.readouterr().out


@pytest.mark.parametrize("release", [None, "", "latest", "LATEST"])
def test_install_refuses_unpinned_release(clean_path, portable_archive, release, capsys):
    assert pr.install(release=release, sha256=portable_archive[0]) is False
    assert portable_archive[3] == []
    assert not pr._install_root().exists()
    assert "release pin" in capsys.readouterr().out


def test_install_digest_mismatch_preserves_install_root(
    clean_path, portable_archive, monkeypatch, capsys
):
    root = pr._install_root()
    root.mkdir(parents=True)
    sentinel = root / "existing-install-data"
    sentinel.write_bytes(b"must survive rejection")
    digest, _binary_name, _contents, calls = portable_archive
    wrong_digest = ("0" if digest[0] != "0" else "1") + digest[1:]

    def unexpected_extract(*_args):
        pytest.fail("Digest-mismatched archive must not be extracted")

    def unexpected_use(*_args):
        pytest.fail("Digest-mismatched archive must not be used")

    monkeypatch.setattr(pr, "_extract_archive", unexpected_extract)
    monkeypatch.setattr(pr, "_ensure_path_symlink", unexpected_use)
    monkeypatch.setattr(pr, "probe_version", unexpected_use)
    assert pr.install(release="vfixture", sha256=wrong_digest) is False
    assert calls == ["resolve", "download"]
    assert sentinel.read_bytes() == b"must survive rejection"
    assert list(root.iterdir()) == [sentinel]
    out = capsys.readouterr().out
    assert "SHA256 mismatch" in out
    assert f"expected {wrong_digest}, got {digest}" in out


def test_install_verified_archive_is_provisioned(clean_path, portable_archive):
    digest, binary_name, contents, calls = portable_archive
    assert pr.install(release="vfixture", sha256=digest.upper()) is True
    assert calls == ["resolve", "download"]
    binary = pr._install_root() / binary_name
    assert binary.read_bytes() == contents
    assert pr.find_renode() == binary


def test_release_asset_uses_explicit_tag_not_latest(clean_path, monkeypatch):
    asset = {"name": "renode-test-portable.tar.gz"}
    requested = []

    def mock_urlopen(request, timeout):
        requested.append(request.full_url)
        assert timeout == 60
        return io.BytesIO(json.dumps({"assets": [asset]}).encode("utf-8"))

    monkeypatch.setattr(pr.urllib.request, "urlopen", mock_urlopen)
    monkeypatch.setattr(pr, "_platform_asset_name", lambda assets: assets[0])
    assert pr._release_asset("vfixture/tag") == asset
    assert requested == [
        "https://api.github.com/repos/renode/renode/releases/tags/vfixture%2Ftag"
    ]


def test_main_install_missing_digest_fails_before_download(
    clean_path, portable_archive, capsys
):
    assert pr.main(["--install", "--release", "vfixture"]) == 1
    assert portable_archive[3] == []
    assert "No authoritative digest is shipped" in capsys.readouterr().out


def test_main_install_supplies_release_and_digest(clean_path, portable_archive):
    assert pr.main([
        "--install", "--release", "vfixture", "--sha256", portable_archive[0]
    ]) == 0
    assert portable_archive[3] == ["resolve", "download"]


def test_main_install_without_pins_fails_closed(clean_path, portable_archive, capsys):
    assert pr.main(["--install"]) == 1
    assert portable_archive[3] == []
    out = capsys.readouterr().out
    assert "--release TAG" in out
    assert "--sha256 HEX" in out
    assert "No authoritative release/archive digest pair is shipped" in out
