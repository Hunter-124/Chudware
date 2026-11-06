"""Catalog identities and dimensional facts used by native fitting placement."""
from __future__ import annotations

import math

import pytest

from services.parts.adapters.piping_fittings_adapter import PipingFittingsAdapter
from services.piping import catalog, schema


def _catalog_id(row: dict) -> str:
    base = f"{row['type']}:NPS{row['nps']}"
    if row["type"] == "reducer":
        return f"{base}x{row['nps2']}"
    if row["type"] == "flange":
        return f"{base}:cl{int(row['flange_class'])}"
    return base


def test_every_fitting_record_preserves_catalog_identity_and_facts():
    adapter = PipingFittingsAdapter()
    rows = catalog._load_rows()
    expected = {_catalog_id(row): row for row in rows}
    records = list(adapter.iter_records())
    assert rows and len(expected) == len(rows) == len(records) == adapter.count()
    assert {row["type"] for row in rows} == {"elbow", "tee", "reducer", "flange"}
    assert len({rec["part_id"] for rec in records}) == len(records)

    for rec in records:
        ref = rec["geometry_ref"]
        # This kind is also the piping-workspace schema route, not just a
        # standalone mechanical part recipe.
        assert ref == {"kind": "fitting_id", "fitting_id": rec["part_id"][len("fitting:"):]}
        row = expected[ref["fitting_id"]]
        assert rec["domain"] == "fitting"
        assert adapter.get(rec["part_id"]) == rec
        assert rec["license"] == "clean-room"
        assert "dimensional facts" in rec["provenance"]
        resolved = catalog.fitting_dims(row["type"], row["nps"], row["nps2"], row["flange_class"])
        assert resolved == row
        for key, value in row.items():
            if key == "type":
                continue
            if value is None:
                assert key not in rec["params"]
            else:
                assert rec["params"][key] == value


@pytest.mark.parametrize(("catalog_id", "facts"), [
    ("elbow:NPS0.5", {"nps": 0.5, "center_to_end": 38.1}),
    ("tee:NPS0.75", {"nps": 0.75, "center_to_end": 28.45}),
    ("reducer:NPS2.0x1.5", {"nps": 2.0, "nps2": 1.5, "length": 76.2}),
    ("flange:NPS0.5:cl150", {"nps": 0.5, "flange_class": 150, "od": 88.9,
                                "thickness": 11.18, "bolt_circle": 60.45,
                                "bolt_holes": 4, "bolt_dia": 15.75}),
])
def test_published_fitting_spot_checks(catalog_id, facts):
    rec = PipingFittingsAdapter().get("fitting:" + catalog_id)
    assert rec is not None
    assert rec["geometry_ref"] == {"kind": "fitting_id", "fitting_id": catalog_id}
    for field, value in facts.items():
        assert rec["params"][field] == pytest.approx(value)


@pytest.mark.parametrize("part_id", [
    "fitting:elbow:NPS0.5001", "fitting:elbow:NPS0.5junk",
    "fitting:tee:NPS0.6", "fitting:reducer:NPS2.0x1.25",
    "fitting:reducer:NPS2.0", "fitting:flange:NPS0.5:cl300",
    "fitting:flange:NPS0.5", "fitting:flange:NPS0.5:cl150junk",
])
def test_unknown_and_incomplete_adapter_identities_do_not_substitute(part_id):
    assert PipingFittingsAdapter().get(part_id) is None


@pytest.mark.parametrize("catalog_id", [
    "elbow:NPS0.5", "tee:NPS0.75", "reducer:NPS2.0x1.5", "flange:NPS0.5:cl150",
])
def test_fitting_id_still_places_into_piping_schema(catalog_id):
    from services.mcp import core_bridge

    if not core_bridge.available():
        pytest.skip("native core unavailable")
    rec = PipingFittingsAdapter().get("fitting:" + catalog_id)
    result = core_bridge.call("part_place", {
        "part_id": rec["part_id"], "record": rec, "doc": schema.new_piping("Catalog fitting"),
    })
    assert result["ok"] and result["editable"]
    assert result["domain"] == "fitting_id"
    assert result["doc"]["schema"] == "aicad.piping/1"
    [placed] = result["doc"]["fittings"]
    assert placed["catalog_ref"] == catalog_id
    assert placed["type"] == catalog_id.split(":", 1)[0]
    assert placed["nps"] == rec["params"]["nps"]
    if "nps2" in rec["params"]:
        assert placed["nps2"] == rec["params"]["nps2"]
    if "flange_class" in rec["params"]:
        assert placed["params"]["flange_class"] == rec["params"]["flange_class"]


def test_flange_standalone_has_native_editable_dimensions_and_assumed_bore():
    from services.mcp import core_bridge

    if not core_bridge.available():
        pytest.skip("native core unavailable")
    rec = PipingFittingsAdapter().get("fitting:flange:NPS0.5:cl150")
    result = core_bridge.call("part_place", {"part_id": rec["part_id"], "record": rec})
    assert result["ok"] and result["editable"]
    doc_id = result["doc_id"]
    try:
        detail = result["detail"]
        specs = detail["meta"]["specs"]
        assert specs["schedule"] == "40" and specs["schedule_assumed"] is True
        assert specs["bore_mm"] == pytest.approx(15.76)
        assert specs["pipe_od_mm"] == pytest.approx(21.3)
        assert specs["bolt_holes"] == 4
        assert specs["bolt_circle"] == pytest.approx(60.45)
        assert specs["bolt_dia"] == pytest.approx(15.75)
        bbox = detail["bbox"]
        extents = [hi - lo for lo, hi in zip(bbox["min"], bbox["max"])]
        assert extents == pytest.approx([88.9, 88.9, 11.18], abs=1e-4)
        features = core_bridge.call("doc_tree", {"doc_id": doc_id})["tree"]["features"]
        holes = [feature for feature in features if feature["name"].startswith("Bolt hole ")]
        assert len(holes) == 4
        for hole in holes:
            assert hole["type"] == "cylinder"
            assert hole["params"]["mode"] == "cut"
            assert hole["params"]["radius"]["value"] == pytest.approx(15.75 / 2)
            x, y, _ = hole["params"]["at"]
            assert math.hypot(x, y) == pytest.approx(60.45 / 2)
    finally:
        core_bridge.call("doc_close", {"doc_id": doc_id})


@pytest.mark.parametrize(("catalog_id", "feature_types", "extents"), [
    ("elbow:NPS0.5", ["sketch", "revolve"], [48.75, 21.3, 48.75]),
    ("tee:NPS0.75", ["cylinder", "cylinder", "cylinder", "cylinder"],
     [56.9, 26.7, 41.8]),
    ("reducer:NPS2.0x1.5", ["cone", "cone"], [60.3, 60.3, 76.2]),
])
def test_other_fittings_place_as_distinct_editable_geometry(catalog_id, feature_types, extents):
    from services.mcp import core_bridge

    if not core_bridge.available():
        pytest.skip("native core unavailable")
    rec = PipingFittingsAdapter().get("fitting:" + catalog_id)
    result = core_bridge.call("part_place", {"part_id": rec["part_id"], "record": rec})
    assert result["ok"] and result["editable"]
    doc_id = result["doc_id"]
    try:
        detail = result["detail"]
        bbox = detail["bbox"]
        assert [hi - lo for lo, hi in zip(bbox["min"], bbox["max"])] == pytest.approx(
            extents, abs=1e-4)
        specs = detail["meta"]["specs"]
        assert specs["catalog_id"] == catalog_id
        assert specs["schedule"] == "40" and specs["schedule_assumed"] is True
        assert specs["assumptions"] and specs["omissions"]
        features = core_bridge.call("doc_tree", {"doc_id": doc_id})["tree"]["features"]
        assert [feature["type"] for feature in features] == feature_types
    finally:
        core_bridge.call("doc_close", {"doc_id": doc_id})


def test_explicit_schedule_and_mismatched_fitting_identity():
    from services.mcp import core_bridge

    if not core_bridge.available():
        pytest.skip("native core unavailable")
    rec = PipingFittingsAdapter().get("fitting:flange:NPS0.5:cl150")
    args = {"part_id": rec["part_id"], "record": rec}
    result = core_bridge.call("part_place", {**args, "params": {"schedule": "80"}})
    assert result["ok"] and result["editable"]
    try:
        specs = result["detail"]["meta"]["specs"]
        assert specs["schedule"] == "80" and specs["schedule_assumed"] is False
        assert specs["bore_mm"] == pytest.approx(13.84)
    finally:
        core_bridge.call("doc_close", {"doc_id": result["doc_id"]})

    for params in ({"schedule": "does-not-exist"}, {"nps": 0.75}, {"flange_class": 300}):
        rejected = core_bridge.call("part_place", {**args, "params": params})
        assert not rejected["ok"], params
