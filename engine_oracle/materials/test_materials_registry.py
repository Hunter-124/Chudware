"""Tests for the unified material registry (engine.materials, T-F2) and its
engine/sim/materials.json mirror (engine.materials.export_sim_json).

No network: this module is stdlib-only (json + pathlib) reading bundled JSON, so nothing
here touches the network.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from engine import materials
from engine.materials import export_sim_json as ex


def test_fea_table_steel_row():
    assert materials.fea_table()["steel"] == {
        "name": "Mild steel (A36)",
        "E_mpa": 200000,
        "nu": 0.29,
        "density_g_cm3": 7.85,
        "yield_mpa": 250,
        "ultimate_mpa": 400,
        "endurance_mpa": 200,
    }


def test_resolve_key_aliases():
    assert materials.resolve_key("alu_6061") == "aluminum_6061_t6"
    assert materials.resolve_key("6061") == "aluminum_6061_t6"
    assert materials.resolve_key("nope") is None


def test_density_and_get():
    assert materials.density("6061") == pytest.approx(2.70)
    assert materials.get("titanium")["key"] == "titanium_ti6al4v"


def test_all_materials_count_and_fea_alias_consistency():
    assert len(materials.all_materials()) >= 25
    fea = materials.fea_table()
    for alias_value in materials.fea_aliases().values():
        assert alias_value in fea


def test_export_sim_json_matches_disk_mirror():
    # Anti-drift guard: engine/sim/materials.json on disk must be exactly what the
    # registry would regenerate — if this fails, someone edited the registry (or the
    # mirror) without running `python -m engine.materials.export_sim_json`.
    disk = json.loads(ex.sim_json_path().read_text())
    assert disk == ex.render()


def test_every_sim_json_key_resolves_in_registry():
    disk = json.loads(pathlib.Path(ex.sim_json_path()).read_text())
    for key in disk:
        assert materials.resolve_key(key) is not None
