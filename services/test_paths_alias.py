"""Appdata alias rule (services/paths.py active_root) — no destructive migration.

Post-Chudware-rebrand contract: new installs use ``Chudware``; machines with an
existing legacy data dir keep using it wholesale (it may hold a live venv, which
does not survive relocation), preferring the most recent legacy name ``Billet``
over the oldest ``AI-CAD``. A stray/cache-only new dir (no ``config.json`` install
marker) must never steal the root from live legacy data. Nothing is ever moved or
deleted automatically.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def paths_mod(tmp_path, monkeypatch):
    """Reload services.paths under an isolated APPDATA base; restore afterwards."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths
    importlib.reload(paths)
    yield paths, tmp_path
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(paths)


def test_fresh_install_uses_chudware(paths_mod):
    paths, base = paths_mod
    assert paths.ROOT == base / "Chudware"
    assert paths.active_root().name == "Chudware"


def test_legacy_billet_present_wins(paths_mod):
    paths, base = paths_mod
    (base / "Billet").mkdir()
    importlib.reload(paths)
    assert paths.ROOT == base / "Billet"
    # Nothing was moved or created in the new Chudware location.
    assert not (base / "Chudware").exists()


def test_legacy_aicad_present_wins(paths_mod):
    paths, base = paths_mod
    (base / "AI-CAD").mkdir()
    importlib.reload(paths)
    assert paths.ROOT == base / "AI-CAD"
    assert not (base / "Chudware").exists()


def test_billet_preferred_over_aicad(paths_mod):
    # Both legacy dirs present, neither with a marker: newest name (Billet) wins.
    paths, base = paths_mod
    (base / "Billet").mkdir()
    (base / "AI-CAD").mkdir()
    importlib.reload(paths)
    assert paths.ROOT == base / "Billet"


def test_bare_new_dir_without_marker_does_not_steal_from_legacy(paths_mod):
    # A stray Chudware dir (no config.json install marker) must NOT steal the root
    # from live legacy data holding a real install (config.json).
    paths, base = paths_mod
    (base / "Chudware").mkdir()
    (base / "Billet").mkdir()
    (base / "Billet" / "config.json").write_text("{}")
    importlib.reload(paths)
    assert paths.ROOT == base / "Billet"


def test_new_dir_with_marker_wins(paths_mod):
    # Once Chudware holds a real install marker it wins over any legacy dir.
    paths, base = paths_mod
    (base / "Chudware").mkdir()
    (base / "Chudware" / "config.json").write_text("{}")
    (base / "Billet").mkdir()
    (base / "Billet" / "config.json").write_text("{}")
    importlib.reload(paths)
    assert paths.ROOT == base / "Chudware"


def test_marker_beats_newer_but_empty_legacy(paths_mod):
    # Mirrors Hunter's real machine: a stray/empty Billet dir alongside a live
    # AI-CAD dir with config.json must resolve to AI-CAD, not the empty Billet —
    # the install marker wins over mere newest-name existence.
    paths, base = paths_mod
    (base / "Billet").mkdir()  # stray/empty, no marker
    (base / "AI-CAD").mkdir()
    (base / "AI-CAD" / "config.json").write_text("{}")
    importlib.reload(paths)
    assert paths.ROOT == base / "AI-CAD"


def test_data_roots_order_and_legacy_never_deleted(paths_mod):
    paths, base = paths_mod
    (base / "Billet").mkdir()
    (base / "Billet" / "config.json").write_text("{}")
    importlib.reload(paths)
    roots = paths.data_roots()
    assert roots == [base / "Chudware", base / "Billet", base / "AI-CAD"]
    # find_config falls back to the legacy config when the new one is absent.
    assert paths.find_config() == base / "Billet" / "config.json"


def test_constants_derive_from_active_root(paths_mod):
    paths, base = paths_mod
    (base / "Billet").mkdir()
    importlib.reload(paths)
    root = base / "Billet"
    assert paths.CONFIG == root / "config.json"
    assert paths.CACHE == root / "cache"
    assert paths.RUNTIME == root / "runtime"
    assert paths.MODELS == root / "models"
