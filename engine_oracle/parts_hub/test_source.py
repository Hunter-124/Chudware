"""Tests for engine_oracle/parts_hub/source.py — deterministic sourcing fan-out.

Fixture-provider scenarios (no network, no libaicad): the rover tube need ranks
the spec-matching tube first with a facet-fit rationale; the bearing need finds
the 12 mm-bore record; artifact plans follow the contracts/sourcing.md vocabulary;
same input → same output (determinism).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from engine_oracle.parts_hub import source as src  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture records (contracts/parts.md PartRecord shape)
# ---------------------------------------------------------------------------

TUBE_GOOD = {
    "part_id": "catalog:onlinemetals::https://onlinemetals.example/4130-tube",
    "domain": "catalog",
    "title": "4130 Chromoly Round Tube 25.4mm OD x 1.25mm wall",
    "params": {"od_mm": 25.4, "wall_mm": 1.25, "alloy": "4130"},
    "sourcing": [{"price": 18.4, "currency": "USD", "stock": 12}],
    "source": "live",
}
TUBE_WRONG_OD = {
    "part_id": "catalog:onlinemetals::https://onlinemetals.example/4130-tube-38",
    "domain": "catalog",
    "title": "4130 Chromoly Round Tube 38.1mm OD x 1.25mm wall",
    "params": {"od_mm": 38.1, "wall_mm": 1.25, "alloy": "4130"},
    "sourcing": [{"price": 24.0, "currency": "USD"}],
    "source": "live",
}
BAR_6061 = {
    "part_id": "catalog:metalsdepot::https://metalsdepot.example/6061-bar",
    "domain": "catalog",
    "title": "6061 Aluminum Round Bar 25.4mm",
    "params": {"diameter_mm": 25.4, "alloy": "6061"},
    "sourcing": [],
    "source": "live",
}
BEARING_6001 = {
    "part_id": "bearing:ISO 15:6001",
    "domain": "bearing",
    "title": "Deep-Groove Ball Bearing 6001",
    "params": {"bore": 12.0, "outer_d": 28.0, "width": 8.0},
    "geometry_ref": {"kind": "core_family", "family": "bearing", "size": "6001"},
    "sourcing": [],
}
BEARING_6205 = {
    "part_id": "bearing:ISO 15:6205",
    "domain": "bearing",
    "title": "Deep-Groove Ball Bearing 6205",
    "params": {"bore": 25.0, "outer_d": 52.0, "width": 15.0},
    "geometry_ref": {"kind": "core_family", "family": "bearing", "size": "6205"},
    "sourcing": [],
}
FASTENER_NO_GREF = {
    "part_id": "fastener:ISO4762:M8",
    "domain": "fastener",
    "title": "Socket Head Cap Screw M8",
    "params": {"d": 8.0},
    "sourcing": [],
}

_ALL = [TUBE_WRONG_OD, BAR_6061, TUBE_GOOD, BEARING_6205, BEARING_6001,
        FASTENER_NO_GREF]


def _fixture_search(query, domain=None, limit=20, **_kw):
    return list(_ALL)[:limit]


# ---------------------------------------------------------------------------
# Hint / category / facet extraction
# ---------------------------------------------------------------------------

class TestExtraction:
    def test_numeric_hints_key_first_and_value_first(self):
        hints = src.extract_hints("tube OD 25.4 wall 1.25 with 12mm bore")
        assert hints["od"] == 25.4
        assert hints["wall"] == 1.25
        assert hints["bore"] == 12.0

    def test_inch_conversion(self):
        assert src.extract_hints("od 1 in")["od"] == 25.4

    def test_category_match_tube(self):
        cats = src.match_categories("4130 chromoly tube od 25.4 wall 1.25")
        assert cats and cats[0] == "structural_stock.metal_tube"

    def test_category_match_bearing(self):
        cats = src.match_categories("bearing 12mm bore")
        assert cats and cats[0].startswith("bearings_motion")

    def test_build_facets_tube(self):
        f = src.build_facets("structural_stock.metal_tube",
                             "4130 chromoly tube OD 25.4 wall 1.25")
        assert f == {"alloy": "4130", "od_mm": 25.4, "wall_mm": 1.25}

    def test_facet_params_view_aliases(self):
        view = src.facet_params_view({"bore": 12.0, "outer_d": 28.0},
                                     ["bore_mm", "od_mm", "width_mm"])
        assert view == {"bore_mm": 12.0, "od_mm": 28.0}


# ---------------------------------------------------------------------------
# The fan-out
# ---------------------------------------------------------------------------

class TestSource:
    def test_rover_tube_ranks_spec_match_first(self):
        r = src.source("4130 chromoly tube OD 25.4 wall 1.25", limit=5,
                       search_fn=_fixture_search)
        assert r["ok"] is True
        it = r["interpreted"]
        assert it["category_id"] == "structural_stock.metal_tube"
        assert it["facets"]["od_mm"] == 25.4 and it["facets"]["alloy"] == "4130"
        top = r["candidates"][0]
        assert top["part_id"] == TUBE_GOOD["part_id"]
        assert "spec fit" in top["rationale"]
        assert "od_mm=25.4" in top["rationale"]
        # wrong-OD tube scores strictly lower
        wrong = next(c for c in r["candidates"]
                     if c["part_id"] == TUBE_WRONG_OD["part_id"])
        assert top["score"] > wrong["score"]

    def test_bearing_bore_facet_matches_local_record(self):
        r = src.source("bearing 12mm bore", limit=5, search_fn=_fixture_search)
        assert r["ok"] is True
        assert r["interpreted"]["facets"] == {"bore_mm": 12.0}
        assert r["candidates"][0]["part_id"] == "bearing:ISO 15:6001"
        assert "bore_mm=12.0" in r["candidates"][0]["rationale"]

    def test_artifact_plans(self):
        r = src.source("bearing 12mm bore", limit=10, search_fn=_fixture_search)
        plans = {c["part_id"]: c["artifact_plan"] for c in r["candidates"]}
        assert plans["bearing:ISO 15:6001"]["how"] == "part_place"
        assert plans[TUBE_GOOD["part_id"]]["how"] == "scrape_import"
        assert plans[TUBE_GOOD["part_id"]]["site"] == "onlinemetals"
        assert plans[TUBE_GOOD["part_id"]]["query"]
        assert plans["fastener:ISO4762:M8"]["how"] == "make_standard_part"

    def test_deterministic(self):
        a = src.source("4130 chromoly tube OD 25.4 wall 1.25",
                       search_fn=_fixture_search)
        b = src.source("4130 chromoly tube OD 25.4 wall 1.25",
                       search_fn=_fixture_search)
        assert a == b

    def test_empty_need_is_contract_error(self):
        r = src.source("", search_fn=_fixture_search)
        assert r["ok"] is False and r["reason"]

    def test_failing_search_degrades(self):
        def _boom(*a, **k):
            raise RuntimeError("no backend")
        r = src.source("bearing 12mm bore", search_fn=_boom)
        assert r["ok"] is True and r["candidates"] == []

    def test_coverage_counts(self):
        r = src.source("bearing 12mm bore", limit=3, search_fn=_fixture_search)
        cov = r["coverage"]
        assert cov["categories_matched"] >= 1
        assert cov["records_considered"] == len(_ALL)
        assert cov["facets_queried"] == 1
        assert cov["local_candidates"] + cov["live_candidates"] == len(r["candidates"])
