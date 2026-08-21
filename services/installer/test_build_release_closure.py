from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from services.installer import build_release, payload_manifest
from services.installer.payload_manifest import build_manifest


_REQUIRED = build_release.REQUIRED_WINDOWS_RUNTIME


def _runtime(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    root.mkdir()
    for name in _REQUIRED:
        (root / name).write_bytes(name.encode())
    (root / "assets" / "icons").mkdir(parents=True)
    (root / "assets" / "icons" / "atlas.png").write_bytes(b"atlas")
    (root / "locales").mkdir()
    (root / "locales" / "en-US.pak").write_bytes(b"locale")
    (root / "swiftshader").mkdir()
    (root / "swiftshader" / "vk_swiftshader.dll").write_bytes(b"vk")
    return root


def _sidecars(tmp_path: Path) -> dict[str, Path]:
    result = {}
    for arg in ("hostd_bin", "worker_bin", "mcp_bin", "omp_bridge_bin", "uninstall_helper_bin"):
        path = tmp_path / f"{arg}.exe"
        path.write_bytes(arg.encode())
        result[arg] = path
    return result


def test_windows_stage_and_manifest_own_complete_runtime_closure(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    dst = tmp_path / "payload"
    dst.mkdir()
    build_release.stage_compiled_payload(
        dst, host_bin=runtime / "chudware.exe", **_sidecars(tmp_path)
    )
    expected = {
        *(Path(name).as_posix() for name in _REQUIRED),
        "assets/icons/atlas.png",
        "locales/en-US.pak",
        "swiftshader/vk_swiftshader.dll",
        "chudware-hostd.exe",
        "chudware-worker.exe",
        "chudware-mcp.exe",
        "chudware-omp-bridge.exe",
        "chudware-uninstall-helper.exe",
    }
    actual = {p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()}
    assert actual == expected
    manifest, _, _ = build_manifest(
        dst, version="1.2.3", platform="windows", arch="x86_64", key_id="release-v1"
    )
    assert {row["path"] for row in manifest["files"]} == expected


@pytest.mark.parametrize("missing", ["libcef.dll", "chudware_cef_helper.exe", "assets", "locales"])
def test_windows_stage_fails_closed_on_missing_runtime_closure(tmp_path: Path, missing: str) -> None:
    runtime = _runtime(tmp_path)
    target = runtime / missing
    if target.is_dir():
        import shutil
        shutil.rmtree(target)
    else:
        target.unlink()
    dst = tmp_path / "payload"
    dst.mkdir()
    with pytest.raises(RuntimeError, match="incomplete Windows GUI runtime"):
        build_release.stage_compiled_payload(
            dst, host_bin=runtime / "chudware.exe", **_sidecars(tmp_path)
        )
    assert not (dst / "payload-manifest.v1.json").exists()


@pytest.mark.parametrize("relative", ["debug.pdb", "source.cpp", ".git/config", "assets/test_fixture.dat"])
def test_windows_stage_excludes_debug_source_and_repo_metadata(tmp_path: Path, relative: str) -> None:
    runtime = _runtime(tmp_path)
    forbidden = runtime / relative
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_bytes(b"forbidden")
    dst = tmp_path / "payload"
    dst.mkdir()
    build_release.stage_compiled_payload(
        dst, host_bin=runtime / "chudware.exe", **_sidecars(tmp_path)
    )
    assert not (dst / relative).exists()


def test_snapshot_survives_a_metadata_change_that_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose timestamps settle late must still be snapshotted.

    Windows reported one mtime through the path and another through the handle
    for a freshly copied DLL, which failed a byte-correct release. Only a
    transient disagreement is tolerated: the accepted snapshot is still proven
    self-consistent.
    """
    root = tmp_path / "payload"
    root.mkdir()
    (root / "chudware.exe").write_bytes(b"payload bytes")
    real_fstat = os.fstat
    calls = {"n": 0}

    def flaky_fstat(descriptor: int) -> os.stat_result:
        calls["n"] += 1
        result = real_fstat(descriptor)
        if calls["n"] == 1:
            fields = list(result)
            fields[8] = fields[8] + 5  # st_mtime the path stat did not see yet
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(payload_manifest.os, "fstat", flaky_fstat)
    snapshot = payload_manifest._snapshot_file(root, "chudware.exe", "executable")
    assert calls["n"] > 1
    assert snapshot.size == len(b"payload bytes")
    assert snapshot.sha256 == hashlib.sha256(b"payload bytes").hexdigest()


def _restat(result: os.stat_result, **overrides: int) -> os.stat_result:
    """Rebuild a stat_result, keeping the nanosecond fields intact."""
    fields = list(result)
    named = {
        "st_atime": result.st_atime, "st_mtime": result.st_mtime, "st_ctime": result.st_ctime,
        "st_atime_ns": result.st_atime_ns, "st_mtime_ns": result.st_mtime_ns,
        "st_ctime_ns": result.st_ctime_ns,
    }
    for name, value in overrides.items():
        index = {"st_ino": 1, "st_dev": 2, "st_size": 6}[name]
        fields[index] = value
    return os.stat_result(fields, named)


def test_snapshot_tolerates_the_windows_cross_api_file_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.stat() and os.fstat() report different file ids for the same Windows file.

    The path form reports FileIdInfo, the handle form the older 64-bit index, so
    comparing them across the two flavours rejected every payload file and no
    Windows release could be staged. Every other field is still compared.
    """
    root = tmp_path / "payload"
    root.mkdir()
    (root / "FreeImage.dll").write_bytes(b"dll bytes")
    real_fstat = os.fstat

    def other_flavour_fstat(descriptor: int) -> os.stat_result:
        result = real_fstat(descriptor)
        return _restat(result, st_ino=result.st_ino + 2**64, st_dev=result.st_dev + 1)

    monkeypatch.setattr(payload_manifest.os, "fstat", other_flavour_fstat)
    monkeypatch.setattr(payload_manifest, "_CROSS_FLAVOUR_IGNORED", ("dev", "ino"))
    snapshot = payload_manifest._snapshot_file(root, "FreeImage.dll", "library", attempts=1)
    assert snapshot.sha256 == hashlib.sha256(b"dll bytes").hexdigest()

    monkeypatch.setattr(payload_manifest, "_CROSS_FLAVOUR_IGNORED", ())
    with pytest.raises(payload_manifest.PayloadManifestError, match="ino="):
        payload_manifest._snapshot_file(root, "FreeImage.dll", "library", attempts=1)
