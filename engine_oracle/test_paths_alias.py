"""T-G10 appdata alias rule (engine/paths.py active_root) — no destructive migration.

Ratified approach (work/questions/T-G10.md): machines with an existing legacy
``AI-CAD`` data dir keep using it wholesale; fresh installs get ``ShopCraft``;
nothing is ever moved or deleted automatically (the legacy dir may hold a live
venv, which does not survive relocation).
"""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture()
def paths_mod(tmp_path, monkeypatch):
    """Reload engine.paths under an isolated APPDATA base; restore afterwards."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths
    importlib.reload(paths)
    yield paths, tmp_path
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(paths)


def test_fresh_install_uses_shopcraft(paths_mod):
    paths, base = paths_mod
    assert paths.ROOT == base / "ShopCraft"
    assert paths.active_root().name == "ShopCraft"


def test_legacy_dir_present_wins(paths_mod):
    paths, base = paths_mod
    (base / "AI-CAD").mkdir()
    importlib.reload(paths)
    assert paths.ROOT == base / "AI-CAD"
    # Nothing was moved or created in the new location.
    assert not (base / "ShopCraft").exists()
    assert (base / "AI-CAD").exists()


def test_both_dirs_present_legacy_wins_without_marker(paths_mod):
    # Hardened 2026-07-04: a bare ShopCraft dir (no config.json install marker)
    # must NOT steal the root from live legacy data — stray cache dirs happen.
    paths, base = paths_mod
    (base / "AI-CAD").mkdir()
    (base / "ShopCraft").mkdir()
    importlib.reload(paths)
    assert paths.ROOT == base / "AI-CAD"

def test_both_dirs_present_new_wins_with_marker(paths_mod):
    paths, base = paths_mod
    (base / "AI-CAD").mkdir()
    (base / "ShopCraft").mkdir()
    (base / "ShopCraft" / "config.json").write_text("{}")
    importlib.reload(paths)
    assert paths.ROOT == base / "ShopCraft"

def test_data_roots_order_and_legacy_never_deleted(paths_mod):
    paths, base = paths_mod
    (base / "AI-CAD").mkdir()
    (base / "AI-CAD" / "config.json").write_text("{}")
    importlib.reload(paths)
    roots = paths.data_roots()
    assert roots == [base / "ShopCraft", base / "AI-CAD"]
    # find_config falls back to the legacy config when the new one is absent.
    assert paths.find_config() == base / "AI-CAD" / "config.json"


def test_constants_derive_from_active_root(paths_mod):
    paths, base = paths_mod
    (base / "AI-CAD").mkdir()
    importlib.reload(paths)
    root = base / "AI-CAD"
    assert paths.CONFIG == root / "config.json"
    assert paths.CACHE == root / "cache"
    assert paths.RUNTIME == root / "runtime"
    assert paths.MODELS == root / "models"
