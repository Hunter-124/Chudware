"""AS568 records map to exact, editable native O-ring geometry."""
from __future__ import annotations

import copy
import math

import pytest

from services.parts.adapters.orings_adapter import (
    OringsAdapter,
    _MATERIALS,
    _iter_dash_table,
)


def test_every_dash_and_material_has_exact_nominal_dimensions_and_geometry_ref():
    table = {dash: (id_in, cs_in) for dash, id_in, cs_in in _iter_dash_table()}
    materials = {material[0] for material in _MATERIALS}
    adapter = OringsAdapter()
    records = list(adapter.iter_records())
    assert len(table) == 336
    assert len(records) == adapter.count() == len(table) * len(materials)
    seen = set()
    for record in records:
        params = record["params"]
        dash = int(params["dash_number"])
        material = params["material"]
        key = (dash, material)
        assert key not in seen
        seen.add(key)
        assert material in materials
        id_in, cs_in = table[dash]
        assert params["id_in"] == id_in
        assert params["cs_in"] == cs_in
        assert params["od_in"] == pytest.approx(id_in + 2 * cs_in, rel=1e-12, abs=1e-12)
        assert params["id_mm"] == pytest.approx(id_in * 25.4, rel=1e-12, abs=1e-12)
        assert params["cs_mm"] == pytest.approx(cs_in * 25.4, rel=1e-12, abs=1e-12)
        assert params["od_mm"] == pytest.approx((id_in + 2 * cs_in) * 25.4, rel=1e-12, abs=1e-12)
        assert record["geometry_ref"] == {
            "kind": "core_family", "family": "oring_as568", "size": f"AS568-{dash:03d}"
        }
        assert adapter.get(record["part_id"]) == record
    assert seen == {(dash, material) for dash in table for material in materials}


def test_as568_001_keeps_exact_nominal_millimetres():
    record = OringsAdapter().get("seal:AS568-001:nbr")
    assert record is not None
    assert record["params"]["id_mm"] == pytest.approx(0.7366, rel=1e-12, abs=1e-12)
    assert record["params"]["cs_mm"] == pytest.approx(1.016, rel=1e-12, abs=1e-12)
    assert record["params"]["od_mm"] == pytest.approx(2.7686, rel=1e-12, abs=1e-12)


def test_old_seal_snapshot_is_rebuilt_with_placeable_refs(monkeypatch, tmp_path):
    from services.parts import hub as hub_module

    class OldOrings(OringsAdapter):
        def iter_records(self):
            for record in super().iter_records():
                record["geometry_ref"] = None
                yield record

    version = hub_module._INDEX_CODE_VERSION
    assert version >= 8
    monkeypatch.setattr(hub_module, "_index_cache_path", lambda: tmp_path / "parts.sqlite")
    monkeypatch.setattr(hub_module, "_fingerprint_sources", lambda: [])
    monkeypatch.setattr(hub_module, "_watched_library_root", lambda: None)
    monkeypatch.setattr(hub_module, "_INDEX_CODE_VERSION", 7)
    old_hub = hub_module.PartsHub()
    old_hub.register(OldOrings())
    part_id = "seal:AS568-001:nbr"
    assert old_hub.get(part_id, include_live=False)["geometry_ref"] is None
    old_fingerprint = old_hub._index.fingerprint
    assert old_fingerprint

    monkeypatch.setattr(hub_module, "_INDEX_CODE_VERSION", version)
    fresh_hub = hub_module.PartsHub()
    fresh_hub.register(OringsAdapter())
    assert fresh_hub._provider_fingerprint() != old_fingerprint
    record = fresh_hub.get(part_id, include_live=False)
    assert record["geometry_ref"] == {
        "kind": "core_family", "family": "oring_as568", "size": "AS568-001"
    }
    assert not fresh_hub._index.loaded_from_cache
    cached_hub = hub_module.PartsHub()
    cached_hub.register(OringsAdapter())
    assert cached_hub.get(part_id, include_live=False) == record
    assert cached_hub._index.loaded_from_cache


@pytest.fixture
def native_core():
    from services.mcp import core_bridge

    if not core_bridge.available():
        pytest.skip("native CAD engine unavailable")
    return core_bridge


def test_native_catalog_matches_all_adapter_source_rows(native_core):
    families = native_core.call("parts_catalog_list", {})["families"]
    family = next((f for f in families if f["key"] == "oring_as568"), None)
    assert family is not None, "native catalog lacks oring_as568 (rebuild libchudware)"
    table = {f"AS568-{dash:03d}": (id_in, cs_in)
             for dash, id_in, cs_in in _iter_dash_table()}
    assert not family["needs_length"]
    assert set(family["sizes"]) == set(table)
    assert len(family["sizes"]) == len(table)
    assert set(family["size_sources"]) == set(table)
    for size, (id_in, cs_in) in table.items():
        source = family["size_sources"][size]
        assert source["id_in"] == pytest.approx(id_in, rel=1e-12, abs=1e-12), size
        assert source["cs_in"] == pytest.approx(cs_in, rel=1e-12, abs=1e-12), size
        assert "AS568B" in source["source"]


@pytest.mark.parametrize("dash,material", [
    (1, "nbr"), (2, "fkm"), (3, "epdm"), (4, "silicone"),
    (50, "nbr"), (102, "fkm"), (201, "nbr"), (319, "epdm"), (467, "silicone"),
])
def test_native_placement_builds_exact_torus(native_core, dash, material):
    record = OringsAdapter().get(f"seal:AS568-{dash:03d}:{material}")
    assert record is not None
    result = native_core.call("part_place", {"part_id": record["part_id"], "record": record})
    assert result["ok"] and result["editable"], result
    doc = result["doc_id"]
    try:
        id_mm, cs_mm = record["params"]["id_mm"], record["params"]["cs_mm"]
        major = (id_mm + cs_mm) / 2
        od_mm = id_mm + 2 * cs_mm
        assert result["detail"]["meta"]["specs"]["id_mm"] == pytest.approx(id_mm, abs=1e-10)
        assert result["detail"]["meta"]["specs"]["cs_mm"] == pytest.approx(cs_mm, abs=1e-10)
        bbox = native_core.call("doc_bbox", {"doc_id": doc})
        extent = [bbox["max"][i] - bbox["min"][i] for i in range(3)]
        assert extent == pytest.approx([od_mm, od_mm, cs_mm], abs=1e-5)
        mass = native_core.call("eval_mass", {"doc_id": doc})
        assert mass["volume_mm3"] == pytest.approx(2 * math.pi**2 * major * (cs_mm / 2)**2,
                                                   rel=1e-5)
        health = native_core.call("eval_solid_health", {"doc_id": doc})
        assert len(health["bodies"]) == 1, health
        assert health["bodies"][0]["solids"] == 1, health
        assert health["bodies"][0]["brep_valid"], health
        features = native_core.call("doc_tree", {"doc_id": doc})["tree"]["features"]
        sketch = next(feature for feature in features if feature["type"] == "sketch")
        revolve = next(feature for feature in features if feature["type"] == "revolve")
        assert sketch["refs"]["plane"] == "plane.front"
        assert sketch["params"]["entities"][0]["type"] == "circle"
        sketch_health = native_core.call("sketch_health", {"doc_id": doc, "sketch": sketch["id"]})
        assert sketch_health["converged"] and sketch_health["dof"] == 0, sketch_health
        assert not sketch_health["skipped_constraints"], sketch_health
        assert not sketch_health["conflicting"] and not sketch_health["redundant"], sketch_health
        assert revolve["refs"]["axis"] == {"kind": "datum", "feature": "axis.z"}
    finally:
        native_core.call("doc_close", {"doc_id": doc})


def test_native_ring_remains_editable_after_save_and_reopen(native_core, tmp_path):
    record = OringsAdapter().get("seal:AS568-001:fkm")
    placed = native_core.call("part_place", {"part_id": record["part_id"], "record": record})
    assert placed["ok"] and placed["editable"], placed
    doc = placed["doc_id"]
    try:
        path = str(tmp_path / "oring.step+")
        native_core.call("doc_save", {"doc_id": doc, "path": path})
        native_core.call("doc_close", {"doc_id": doc})
        doc = None
        doc = native_core.call("doc_open", {"path": path})["doc_id"]
        metadata = native_core.call("doc_get_meta", {"doc_id": doc, "key": "parts_catalog"})["value"]
        assert metadata["part_record"]["part_id"] == record["part_id"]
        features = native_core.call("doc_tree", {"doc_id": doc})["tree"]["features"]
        sketch = next(feature for feature in features if feature["type"] == "sketch")
        constraints = copy.deepcopy(sketch["params"]["constraints"])
        diameter = next(c for c in constraints if c["type"] == "diameter")
        diameter["value"]["value"] = 1.2
        edited = native_core.call("doc_apply_ops", {"doc_id": doc, "ops": [{
            "op": "edit_feature", "id": sketch["id"], "params": {"constraints": constraints}
        }]})
        assert "rejected_tree" not in edited, edited
        assert all(status["ok"] for status in edited["rebuild"]["feature_status"]), edited
        bbox = native_core.call("doc_bbox", {"doc_id": doc})
        # Changing cross-section leaves the constrained inner diameter unchanged.
        major = (0.7366 + 1.2) / 2
        extent = [bbox["max"][i] - bbox["min"][i] for i in range(3)]
        assert extent == pytest.approx([0.7366 + 2 * 1.2, 0.7366 + 2 * 1.2, 1.2], abs=1e-5)
        volume = native_core.call("eval_mass", {"doc_id": doc})["volume_mm3"]
        assert volume == pytest.approx(2 * math.pi**2 * major * 0.6**2, rel=1e-5)
        inner_diameter = next(c for c in constraints if c["type"] == "length")
        inner_diameter["value"]["value"] = 1.0
        edited = native_core.call("doc_apply_ops", {"doc_id": doc, "ops": [{
            "op": "edit_feature", "id": sketch["id"], "params": {"constraints": constraints}
        }]})
        assert "rejected_tree" not in edited, edited
        assert all(status["ok"] for status in edited["rebuild"]["feature_status"]), edited
        bbox = native_core.call("doc_bbox", {"doc_id": doc})
        extent = [bbox["max"][i] - bbox["min"][i] for i in range(3)]
        assert extent == pytest.approx([3.4, 3.4, 1.2], abs=1e-5)
        volume = native_core.call("eval_mass", {"doc_id": doc})["volume_mm3"]
        assert volume == pytest.approx(2 * math.pi**2 * 1.1 * 0.6**2, rel=1e-5)
    finally:
        if doc is not None:
            native_core.call("doc_close", {"doc_id": doc})
