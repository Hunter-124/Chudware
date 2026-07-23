"""Tests for the OpenFOAM runner abstraction (services.sim.foam_runner).

These are pure-Python and do NOT require an OpenFOAM install — they exercise runner
selection, capability-dict shape, path translation, and the LinuxRunner detection/exec
harness against a stub install tree.  A live end-to-end solve is covered by
tools/foam_validation.py (opt-in, needs OpenFOAM).
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from services.sim import foam_runner as fr

_POSIX = os.name == "posix"


def test_win_drive_translation():
    assert fr._win_drives_prefix(r"C:\a\b") == "/mnt/c/a/b"
    assert fr._win_drives_prefix(r"D:\x\y z") == "/mnt/d/x/y z"
    # already-posix stays untouched
    assert fr._win_drives_prefix("/tmp/case") == "/tmp/case"


def test_capabilities_shape():
    caps = fr.capabilities()
    assert caps["lbm"] is True
    assert set(caps) == {"lbm", "openfoam", "openfoam_kind", "openfoam_version"}
    assert isinstance(caps["openfoam"], bool)
    assert caps["openfoam_kind"] in (None, "native", "wsl", "linux")
    # consistency: kind set <=> openfoam true
    assert (caps["openfoam_kind"] is not None) == caps["openfoam"]


def test_get_runner_cached():
    fr.reset_cache()
    a = fr.get_runner()
    b = fr.get_runner()
    assert a is b  # cached identity
    if a is not None:
        assert a.kind in ("native", "wsl", "linux")
        assert a.available() is True
    fr.reset_cache()


def test_native_runner_offline_safe():
    # Constructing + probing a native runner must never raise even with no install.
    n = fr.NativeRunner(root="C:/definitely/not/here")
    assert n.available() is False
    assert n.to_case_path(r"C:\foo") == r"C:\foo"  # native = no translation


def test_linux_runner_offline_safe():
    # A bogus root must probe False without raising, on every platform.
    r = fr.LinuxRunner(root="/definitely/not/here")
    assert r.available() is False
    assert r.to_case_path("/tmp/case") == "/tmp/case"  # posix = no translation


# ---------------------------------------------------------------- Linux stub install

def _stub_install(tmp: Path) -> Path:
    """Fake an OpenFOAM install tree: etc/bashrc extends PATH to a bin/ of stub apps."""
    (tmp / "etc").mkdir(parents=True)
    bindir = tmp / "bin"
    bindir.mkdir()
    for app, body in (
        ("simpleFoam", 'echo "stub simpleFoam"\n'),
        ("blockMesh", 'echo "stub blockMesh in $(pwd)"\n'),
        ("failFoam", 'echo "boom" >&2\nexit 3\n'),
    ):
        p = bindir / app
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    (tmp / "etc" / "bashrc").write_text(
        f'export PATH="{bindir}:$PATH"\nexport WM_PROJECT_VERSION=stub1\n')
    return tmp


@pytest.mark.skipif(not _POSIX, reason="LinuxRunner is posix-only")
def test_linux_runner_detects_stub_tree():
    root = _stub_install(Path(tempfile.mkdtemp()))
    r = fr.LinuxRunner(root=str(root))
    assert r.available() is True
    assert r.kind == "linux"
    assert "stub1" in r.version()


@pytest.mark.skipif(not _POSIX, reason="LinuxRunner is posix-only")
def test_linux_runner_runs_and_streams():
    root = _stub_install(Path(tempfile.mkdtemp()))
    r = fr.LinuxRunner(root=str(root))
    assert r.available()
    case = tempfile.mkdtemp()
    lines: list[str] = []
    r.run(case, "blockMesh", on_line=lines.append)
    assert any("stub blockMesh" in ln and case in ln for ln in lines)  # ran IN the case dir
    # non-zero exit surfaces as FoamError with the stderr tail
    with pytest.raises(fr.FoamError) as ei:
        r.run(case, "failFoam")
    assert ei.value.code == 3
    assert "boom" in ei.value.tail


@pytest.mark.skipif(not _POSIX, reason="LinuxRunner is posix-only")
def test_linux_runner_env_var_root(monkeypatch):
    root = _stub_install(Path(tempfile.mkdtemp()))
    monkeypatch.setenv("CHUDWARE_OPENFOAM", str(root))
    r = fr.LinuxRunner()          # no explicit root: must find it via CHUDWARE_OPENFOAM
    # PATH may already carry a real OpenFOAM; only assert when the stub is what resolves.
    if not fr.LinuxRunner()._probe(""):
        assert r.available() is True
