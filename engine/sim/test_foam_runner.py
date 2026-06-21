"""Tests for the OpenFOAM runner abstraction (engine.sim.foam_runner).

These are pure-Python and do NOT require an OpenFOAM install — they exercise the runner
selection, capability-dict shape, and path translation.  A live end-to-end solve is covered
by tools/foam_validation.py (opt-in, needs OpenFOAM).
"""

from __future__ import annotations

from engine.sim import foam_runner as fr


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
    # kind is null OR one of the two runners
    assert caps["openfoam_kind"] in (None, "native", "wsl")
    # consistency: kind set <=> openfoam true
    assert (caps["openfoam_kind"] is not None) == caps["openfoam"]


def test_get_runner_cached():
    fr.reset_cache()
    a = fr.get_runner()
    b = fr.get_runner()
    assert a is b  # cached identity
    if a is not None:
        assert a.kind in ("native", "wsl")
        assert a.available() is True


def test_native_runner_offline_safe():
    # Constructing + probing a native runner must never raise even with no install.
    n = fr.NativeRunner(root="C:/definitely/not/here")
    assert n.available() is False
    assert n.to_case_path(r"C:\foo") == r"C:\foo"  # native = no translation
