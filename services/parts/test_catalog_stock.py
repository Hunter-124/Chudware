"""Tests for the sealing + raw-stock catalog adapters.

Covers:
  - OringsAdapter (AS568B O-rings, domain "seal")
  - RawStockAdapter (metal/plastic bar/tube/sheet stock, domain "section")
  - ExtrusionAdapter (T-slot aluminum extrusion profiles, domain "section")

Verifies: nonempty record generation, shared PartRecord shape, part_id
uniqueness, AS568B dash-number ID/CS correctness against the published
standard, and end-to-end PartsHub search behavior (the accuracy/coverage
bugs this workstream exists to fix: "stepper nema 17"-style false-positive
suppression and zero-hit coverage gaps for o-rings/6061/etc.).
"""
from __future__ import annotations

import pytest

from services.parts.adapters.orings_adapter import OringsAdapter, _iter_dash_table
from services.parts.adapters.raw_stock_adapter import RawStockAdapter
from services.parts.adapters.extrusion_adapter import ExtrusionAdapter

_REQUIRED_KEYS = {
    "part_id", "domain", "title", "standard", "designation",
    "params", "geometry_ref", "sourcing", "provenance", "license",
}


def _assert_record_shape(rec: dict) -> None:
    assert _REQUIRED_KEYS.issubset(rec.keys())
    assert isinstance(rec["part_id"], str) and rec["part_id"]
    assert isinstance(rec["domain"], str) and rec["domain"]
    assert isinstance(rec["title"], str) and rec["title"]
    assert isinstance(rec["params"], dict)
    assert isinstance(rec["sourcing"], list)
    assert isinstance(rec["provenance"], str) and rec["provenance"]
    assert rec["license"] in ("clean-room", "public-domain", "user-supplied")


# ---------------------------------------------------------------------------
# OringsAdapter
# ---------------------------------------------------------------------------

class TestOringsAdapter:
    def setup_method(self):
        self.adapter = OringsAdapter()

    def test_iter_records_nonempty(self):
        records = list(self.adapter.iter_records())
        assert len(records) > 0
        assert self.adapter.count() == len(records)

    def test_record_shape(self):
        for rec in self.adapter.iter_records():
            _assert_record_shape(rec)
            assert rec["domain"] == "seal"

    def test_part_ids_unique(self):
        records = list(self.adapter.iter_records())
        ids = [r["part_id"] for r in records]
        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize("dash,expected_id,expected_cs", [
        (14, 0.489, 0.070),    # AS568-014 (Parker "2-014"): 000 series
        (6, 0.114, 0.070),     # AS568-006: 000 series
        (214, 0.984, 0.139),   # AS568-214: 200 series
        (112, 0.487, 0.103),   # AS568-112: 100 series
        (325, 1.475, 0.210),   # AS568-325: 300 series
        (445, 7.975, 0.275),   # AS568-445: 400 series
    ])
    def test_as568_spot_check_dash_values(self, dash, expected_id, expected_cs):
        """Cross-checked against the published AS568B standard dimensional
        table (Parker ORD-5700 / Boyd / Applied Fluid Power / Spec Seals)."""
        table = {d: (id_in, cs_in) for d, id_in, cs_in in _iter_dash_table()}
        assert table[dash] == (expected_id, expected_cs)

    def test_get_round_trips(self):
        rec = self.adapter.get("seal:AS568-014:nbr")
        assert rec is not None
        assert rec["params"]["id_in"] == 0.489
        assert rec["params"]["cs_in"] == 0.070
        assert rec["params"]["material"] == "nbr"

    def test_get_unknown_returns_none(self):
        assert self.adapter.get("seal:AS568-999:nbr") is None
        assert self.adapter.get("bogus") is None
        assert self.adapter.get("") is None

    def test_materials_present(self):
        codes = {r["params"]["material"] for r in self.adapter.iter_records()}
        assert codes == {"nbr", "fkm", "silicone", "epdm"}

    def test_designations_findable_by_common_forms(self):
        """The -014 record's title must contain every common way a user types
        this dash number: '2-014' (Parker/industry prefix), '-014', 'AS568-014'."""
        rec = self.adapter.get("seal:AS568-014:nbr")
        title = rec["title"].lower()
        for needle in ("2-014", "-014", "as568-014", "o-ring", "oring"):
            assert needle in title, f"{needle!r} missing from title {title!r}"


# ---------------------------------------------------------------------------
# RawStockAdapter
# ---------------------------------------------------------------------------

class TestRawStockAdapter:
    def setup_method(self):
        self.adapter = RawStockAdapter()

    def test_iter_records_nonempty(self):
        records = list(self.adapter.iter_records())
        assert len(records) > 0
        assert self.adapter.count() == len(records)

    def test_record_shape(self):
        for rec in self.adapter.iter_records():
            _assert_record_shape(rec)
            assert rec["domain"] == "section"

    def test_part_ids_unique(self):
        records = list(self.adapter.iter_records())
        ids = [r["part_id"] for r in records]
        assert len(ids) == len(set(ids))

    def test_covers_required_materials(self):
        materials = {r["params"]["material"] for r in self.adapter.iter_records()}
        for required in ("6061-T6", "7075-T6", "1018", "4140", "304", "316",
                          "acetal", "hdpe", "ptfe", "acrylic", "polycarbonate"):
            assert required in materials

    def test_covers_required_shapes(self):
        shapes = {r["params"]["shape"] for r in self.adapter.iter_records()}
        assert {"round_bar", "hex_bar", "flat_bar", "plate",
                "sheet", "round_tube"}.issubset(shapes)

    def test_quarter_inch_aluminum_plate_present(self):
        matches = [
            r for r in self.adapter.iter_records()
            if r["params"].get("shape") == "plate"
            and r["params"].get("material") == "6061-T6"
            and r["params"].get("thickness_in") == 0.25
        ]
        assert len(matches) == 1

    def test_304_stainless_tube_present(self):
        matches = [
            r for r in self.adapter.iter_records()
            if r["params"].get("shape") == "round_tube"
            and r["params"].get("material") == "304"
        ]
        assert len(matches) > 0

    def test_get_round_trips(self):
        rec = next(self.adapter.iter_records())
        assert self.adapter.get(rec["part_id"]) == rec

    def test_get_unknown_returns_none(self):
        assert self.adapter.get("section:ROUND-BAR:bogus:99in") is None


# ---------------------------------------------------------------------------
# ExtrusionAdapter
# ---------------------------------------------------------------------------

class TestExtrusionAdapter:
    def setup_method(self):
        self.adapter = ExtrusionAdapter()

    def test_iter_records_nonempty(self):
        records = list(self.adapter.iter_records())
        assert len(records) > 0
        assert self.adapter.count() == len(records)

    def test_record_shape(self):
        for rec in self.adapter.iter_records():
            _assert_record_shape(rec)
            assert rec["domain"] == "section"

    def test_part_ids_unique(self):
        records = list(self.adapter.iter_records())
        ids = [r["part_id"] for r in records]
        assert len(ids) == len(set(ids))

    def test_covers_required_series(self):
        series = {r["designation"] for r in self.adapter.iter_records()}
        for required in ("2020", "2040", "3030", "3060", "4040", "4080",
                          "1010", "1515"):
            assert required in series

    def test_2020_dims_and_synonyms(self):
        rec = self.adapter.get("section:T-SLOT:2020")
        assert rec is not None
        assert rec["params"]["width_mm"] == 20
        assert rec["params"]["height_mm"] == 20
        title = rec["title"].lower()
        for needle in ("2020", "t-slot", "8020", "80/20", "aluminum"):
            assert needle in title

    def test_moment_of_inertia_present_and_positive(self):
        for rec in self.adapter.iter_records():
            assert rec["params"]["moment_of_inertia_ix_mm4"] > 0
            assert rec["params"]["moment_of_inertia_iy_mm4"] > 0

    def test_get_unknown_returns_none(self):
        assert self.adapter.get("section:T-SLOT:9999") is None


# ---------------------------------------------------------------------------
# End-to-end PartsHub search (the accuracy bugs this workstream fixes)
# ---------------------------------------------------------------------------

class TestPartsHubCatalogStockSearch:
    def setup_method(self):
        from services.parts.hub import PartsHub
        from services.parts.adapters.heatsinks_adapter import HeatsinksAdapter

        self.hub = PartsHub()
        self.hub.register(OringsAdapter())
        self.hub.register(RawStockAdapter())
        self.hub.register(ExtrusionAdapter())
        # Register a pre-existing adapter too, so the "6061 bar must beat a
        # heatsink" assertion is a real contest, not a vacuous one.
        self.hub.register(HeatsinksAdapter())
        self.hub._ensure_index()

    def test_oring_dash_number_top_hit(self):
        results = self.hub.search("o-ring 2-014", include_live=False)
        assert results, "expected local hits for 'o-ring 2-014'"
        top = results[0]
        assert top["domain"] == "seal"
        assert top["params"]["dash_number"] == "014"

    def test_aluminum_6061_bar_not_heatsink(self):
        results = self.hub.search("aluminum 6061 bar", include_live=False)
        assert results, "expected local hits for 'aluminum 6061 bar'"
        top = results[0]
        assert top["domain"] != "thermal", "top hit must not be a heatsink"
        assert top["params"].get("material") == "6061-T6"
        assert top["params"].get("shape") in ("round_bar", "hex_bar", "flat_bar")

    def test_previously_zero_hit_tokens_now_covered(self):
        """Baseline proved these tokens matched nothing in the 436-item corpus."""
        for query in ("o-ring", "oring", "seal", "6061", "steel",
                      "extrusion", "stainless", "t-slot", "8020"):
            results = self.hub.search(query, include_live=False)
            assert results, f"expected local hits for {query!r}"

    def test_extrusion_query_hits_tslot_domain(self):
        for query in ("2020 extrusion", "t-slot", "8020"):
            results = self.hub.search(query, include_live=False)
            assert results, f"expected local hits for {query!r}"
            assert results[0]["params"].get("series") is not None
