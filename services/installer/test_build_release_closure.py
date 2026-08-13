from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.installer import build_release
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
