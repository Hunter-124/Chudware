"""Appdata root rule (services/paths.py active_root) — Chudware unconditional, one-time
copy migration from pre-rename roots.

Contract: new installs use ``Chudware``. A machine whose live data still sits under a
legacy ``Billet``/``AI-CAD`` root (marker: ``config.json``) gets it copied forward once
— copy-if-missing, skipping venvs and re-provisionable payloads (``runtime`` etc.,
which do not survive relocation). The old directory is never moved or deleted. Only a
failed copy keeps the legacy root in use.
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


def test_stray_legacy_dir_without_marker_does_not_migrate(paths_mod):
    # A legacy dir with no config.json install marker is ignored entirely.
    paths, base = paths_mod
    (base / "Billet").mkdir()
    importlib.reload(paths)
    assert paths.ROOT == base / "Chudware"
    assert not (base / "Chudware").exists()  # nothing created for a stray dir


def test_legacy_install_is_copied_forward(paths_mod):
    # Live legacy install (marker + data) → migrated into Chudware, legacy untouched.
    paths, base = paths_mod
    (base / "AI-CAD").mkdir()
    (base / "AI-CAD" / "config.json").write_text("{}")
    (base / "AI-CAD" / "chat_history.json").write_text("{}")
    importlib.reload(paths)
    assert paths.ROOT == base / "Chudware"
    assert (base / "Chudware" / "config.json").exists()
    assert (base / "Chudware" / "chat_history.json").exists()
    # The old dir is never moved or deleted.
    assert (base / "AI-CAD" / "config.json").exists()


def test_migration_skips_venvs_and_payloads(paths_mod):
    paths, base = paths_mod
    (base / "Billet" / "runtime").mkdir(parents=True)
    (base / "Billet" / "runtime" / "pyvenv.cfg").write_text("home = /x")
    (base / "Billet" / "config.json").write_text("{}")
    importlib.reload(paths)
    assert paths.ROOT == base / "Chudware"
    assert (base / "Chudware" / "config.json").exists()
    assert not (base / "Chudware" / "runtime").exists()  # venvs cannot relocate


def test_migration_merges_into_existing_stray_new_dir(paths_mod):
    # A stray Chudware dir (no marker) must not block migration: the legacy install
    # is merged into it copy-if-missing, never overwriting existing files.
    paths, base = paths_mod
    (base / "Chudware").mkdir()
    (base / "Chudware" / "keep.txt").write_text("new")
    (base / "AI-CAD").mkdir()
    (base / "AI-CAD" / "config.json").write_text("{}")
    (base / "AI-CAD" / "keep.txt").write_text("old")
    importlib.reload(paths)
    assert paths.ROOT == base / "Chudware"
    assert (base / "Chudware" / "config.json").exists()
    assert (base / "Chudware" / "keep.txt").read_text() == "new"  # target wins


def test_billet_preferred_over_aicad(paths_mod):
    # Both legacy installs present: the newest legacy name (Billet) migrates first.
    paths, base = paths_mod
    (base / "Billet").mkdir()
    (base / "Billet" / "config.json").write_text('{"from": "billet"}')
    (base / "AI-CAD").mkdir()
    (base / "AI-CAD" / "config.json").write_text('{"from": "ai-cad"}')
    importlib.reload(paths)
    assert paths.ROOT == base / "Chudware"
    assert (base / "Chudware" / "config.json").read_text() == '{"from": "billet"}'


def test_new_dir_with_marker_wins(paths_mod):
    # Once Chudware holds a real install marker, no migration runs at all.
    paths, base = paths_mod
    (base / "Chudware").mkdir()
    (base / "Chudware" / "config.json").write_text('{"from": "new"}')
    (base / "Billet").mkdir()
    (base / "Billet" / "config.json").write_text('{"from": "old"}')
    importlib.reload(paths)
    assert paths.ROOT == base / "Chudware"
    assert (base / "Chudware" / "config.json").read_text() == '{"from": "new"}'


def test_data_roots_order_and_legacy_never_deleted(paths_mod):
    paths, base = paths_mod
    (base / "Billet").mkdir()
    (base / "Billet" / "config.json").write_text("{}")
    importlib.reload(paths)
    roots = paths.data_roots()
    assert roots == [base / "Chudware", base / "Billet", base / "AI-CAD"]
    # After the migration the canonical config exists in the Chudware root.
    assert paths.find_config() == base / "Chudware" / "config.json"
    assert (base / "Billet" / "config.json").exists()  # never deleted


def test_constants_derive_from_active_root(paths_mod):
    paths, base = paths_mod
    (base / "Billet").mkdir()
    (base / "Billet" / "config.json").write_text("{}")
    importlib.reload(paths)
    root = base / "Chudware"
    assert paths.CONFIG == root / "config.json"
    assert paths.CACHE == root / "cache"
    assert paths.RUNTIME == root / "runtime"
    assert paths.MODELS == root / "models"
