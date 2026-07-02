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


# --------------------------------------------------------------------------- CAM seam (T-M4)

import os                                                           # noqa: E402

# The 12 registry materials that carry a CAM fold-in (Issue #2), keyed reg_id -> cutting-group id.
_CAM_MAPPING = {
    "aluminum_6061_t6": "aluminum", "brass_c360": "brass", "copper_c110": "copper",
    "steel_1018": "steel_mild", "stainless_304": "stainless_304", "cast_iron_gray": "cast_iron",
    "titanium_ti6al4v": "titanium", "inconel_718": "inconel", "magnesium_az31b": "magnesium",
    "abs": "plastic", "acrylic_pmma": "acrylic", "carbon_fiber_ud": "carbon_fiber",
}


def test_cam_sections_present_and_shaped():
    cam = materials.cam_materials()
    assert set(cam) == set(_CAM_MAPPING), "expected exactly the 12 folded materials"
    for reg_id, group in _CAM_MAPPING.items():
        sec = cam[reg_id]
        assert sec["id"] == group
        for f in materials.CAM_FIELDS:
            assert f in sec and isinstance(sec[f], (int, float))
        # Provenance is mandatory (Driver notes: cite CuttingDB's source; never fabricate).
        assert sec["provenance"]["source"]
        assert sec["provenance"]["source_type"] == "first_party"


def test_cam_accessors_resolve_via_alias():
    # A registry alias ("6061") reaches the cam section + its cutting-group id.
    assert materials.cam_material_id("6061") == "aluminum"
    assert materials.cam_data("6061")["sfm_mill"] == 900.0
    # A material with no cam fold-in returns None (never fabricated).
    assert materials.cam_material_id("peek") is None
    assert materials.cam_data("peek") is None
    assert materials.cam_material_id("nonsense") is None


def _core_cam_table():
    """The C++ CuttingDB in-code table via the cam_materials RPC, or None if the core is absent."""
    so = os.environ.get("AI_CAD_LIBAICAD")
    if not so or not os.path.exists(so):
        return None
    import sys
    for p in ("services/mcp", "."):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        import core_bridge as cb  # type: ignore
        r = cb.call("cam_materials", {})
        mats = r["materials"] if isinstance(r, dict) else r
        return {m["id"]: m for m in mats}
    except Exception:  # noqa: BLE001
        return None


def test_cam_values_byte_identical_to_core_table():
    """The DoD's byte-identical guarantee: each registry cam section reproduces the C++
    CuttingDB's in-code values EXACTLY (all 10 fields). Skips only when the core .so is
    absent; run with AI_CAD_LIBAICAD set (as the gate does)."""
    core = _core_cam_table()
    if core is None:
        pytest.skip("core .so absent (set AI_CAD_LIBAICAD); byte-identical check needs the engine")
    cam = materials.cam_materials()
    for reg_id, group in _CAM_MAPPING.items():
        assert group in core, f"cutting group {group} missing from core table"
        sec, ref = cam[reg_id], core[group]
        for f in materials.CAM_FIELDS:
            assert sec[f] == ref[f], f"{reg_id}.cam.{f} {sec[f]} != core {group}.{f} {ref[f]}"
