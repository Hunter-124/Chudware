"""Material names from the HERC rover audit (docs/dev/herc-rover-mcp-audit.md section 8).

Each name below failed to reach an engineering record, so set_material applied only a look
and measure_part weighed the part with an unrelated density. They must now resolve to their
own sourced registry rows, and a visual-only set_material must say density_known: false.

Run:  .venv-mcp/bin/python -X utf8 -m pytest services/mcp/test_material_resolution.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from services.mcp import cad_common as cc
from services.mcp import core_bridge as cb

_REGISTRY = Path(_REPO_ROOT) / "core" / "data" / "materials" / "registry.json"


# ------------------------------------------------------------ candidates (no engine needed)

@pytest.mark.parametrize("raw,stripped", [
    ("AISI 4140", "4140"), ("SAE 52100", "52100"), ("ASTM A500", "a500"),
    ("AISI-1045", "1045"), ("UNS G41400", "g41400"),
])
def test_designation_prefix_is_stripped_after_exact_spellings(raw, stripped):
    cands = cc._material_candidates(raw)
    assert stripped in cands, cands
    # The exact spelling stays first so a prefixed registry alias still wins.
    assert cands[0] == " ".join(raw.lower().split())
    assert cands.index(stripped) > cands.index(cc._material_norm(raw))


def test_astm_prefix_kept_when_the_alias_spells_it():
    assert cc._material_candidates("ASTM A106 Grade B")[0] == "astm a106 grade b"


def test_registry_aliases_are_unique_and_sourced():
    """Every alias resolves to one row (the engine's index is last-write-wins), and each new
    row names a real source URL."""
    mats = json.loads(_REGISTRY.read_text(encoding="utf-8"))["materials"]
    owner: dict = {}
    for key, rec in mats.items():
        for alias in [key, rec["name"], *rec.get("aliases", [])]:
            norm = alias.strip().lower()
            assert owner.setdefault(norm, key) == key, (alias, owner[norm], key)
    for key in ("steel_4140_annealed", "steel_4140_normalized", "steel_4140_qt_le100",
                "steel_52100", "steel_astm_a500_grade_b_round",
                "steel_astm_a500_grade_b_shaped", "steel_astm_a500_grade_c_round",
                "steel_astm_a500_grade_c_shaped", "zinc_zamak3_die_cast",
                "hdpe_generic_sheet", "tpu_95a_generic", "rubber_nr_60a",
                "rubber_neoprene_cr_60a"):
        prov = mats[key]["provenance"]
        assert str(prov.get("url", "")).startswith("https://"), key
        assert prov.get("source") and prov.get("source_type"), key


# ------------------------------------------------------------------ engine-backed resolution

needs_engine = pytest.mark.skipif(
    not cb.available(), reason="libchudware not staged; the materials registry RPCs are unavailable")


def _key(name: str):
    for cand in cc._material_candidates(name):
        rec = cc._material_registry_record(cand)
        if rec:
            return rec.get("key")
    return None


@needs_engine
@pytest.mark.parametrize("name,key,density", [
    ("4140", "steel_4140_annealed", 7.85),
    ("AISI 4140", "steel_4140_annealed", 7.85),
    ("4140 Q&T", "steel_4140_qt_le100", 7.85),
    ("A500", "steel_astm_a500_grade_b_round", 7.85),
    ("ASTM A500 Grade C", "steel_astm_a500_grade_c_round", 7.85),
    ("A500 Grade C shaped", "steel_astm_a500_grade_c_shaped", 7.85),
    ("52100", "steel_52100", 7.81),
    ("AISI 52100", "steel_52100", 7.81),
    ("HDPE", "hdpe_generic_sheet", 0.959),
    ("bronze", "bronze_c93200_b505_m07", 8.91),
    ("gray cast iron", "cast_iron_gray", 7.2),
    ("Zamak 3", "zinc_zamak3_die_cast", 6.6),
    ("TPU", "tpu_95a_generic", 1.15),
    ("rubber", "rubber_nr_60a", 1.12),
    ("neoprene", "rubber_neoprene_cr_60a", 1.4),
    ("AISI 1045", "steel_1045", 7.85),
    ("4130", "steel_4130", 7.85),
])
def test_audit_names_resolve_to_their_own_rows(name, key, density):
    assert _key(name) == key
    assert cc._material_registry_density(key) == pytest.approx(density)
    assert cc._resolve_material_density(name) == pytest.approx(density)


@needs_engine
@pytest.mark.parametrize("name,yield_mpa", [
    ("AISI 4140", 415), ("4140 normalized", 655), ("4140 Q&T", 740),
    ("A500", 290), ("A500 Grade B shaped", 315), ("A500 Grade C", 315),
    ("A500 Grade C shaped", 345), ("Zamak 3", 221),
])
def test_isotropic_rows_feed_fea_their_own_minima(name, yield_mpa):
    assert cc._resolve_material(name)["yield_mpa"] == yield_mpa


@needs_engine
@pytest.mark.parametrize("name", ["HDPE", "TPU", "rubber", "52100", "bronze"])
def test_density_only_rows_refuse_isotropic_fea(name):
    with pytest.raises(ValueError, match="constitutive_model"):
        cc._resolve_material(name)


@needs_engine
def test_visual_only_preset_has_no_registry_density():
    assert _key("plastic") is None
    assert cc._material_registry_density(None) is None


# ----------------------------------------------------------------- set_material result

def _call(name: str, args: dict):
    from services.mcp import server

    _content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    return json.loads(text) if isinstance(text, str) else None


@needs_engine
def test_set_material_reports_density_known():
    doc = _call("new_part", {"name": "density_known_probe", "fresh": True})["doc_id"]
    built = _call("add_features", {"doc_id": doc, "ops": [
        {"type": "box", "name": "Solid", "params": {"length": 10, "width": 10, "height": 10}}]})
    assert built["ok"], built
    real = _call("set_material", {"doc_id": doc, "material": "AISI 4140"})
    assert real["ok"] and real["material_key"] == "steel_4140_annealed", real
    assert real["density_known"] is True and real["density_g_cm3"] == 7.85, real
    visual = _call("set_material", {"doc_id": doc, "material": "plastic"})
    assert visual["ok"] and visual["material_key"] is None, visual
    assert visual["density_known"] is False, visual
    assert "density_g_cm3" in visual["warning"], visual
