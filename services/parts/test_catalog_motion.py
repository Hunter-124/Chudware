"""Tests for the new motion/power-transmission local catalog adapters:
StepperMotorsAdapter, BeltsPulleysAdapter, ShaftCouplingsAdapter.

Covers:
  - Each adapter yields >0 records.
  - Every record satisfies the shared PartRecord shape (same envelope contract
    the BoltsAdapter/HeatsinksAdapter tests check).
  - part_ids are unique within each adapter (and across all three).
  - A PartsHub bootstrap (adapters are wired into PartsHub.bootstrap() alongside
    the other built-in adapters) followed by search("nema 17") returns a
    stepper motor as the TOP hit — not a bearing (the proven accuracy bug:
    the bare token "17" spuriously matches bearing designations like 6203).

Run with:
    python3 -X utf8 -m pytest services/parts/test_catalog_motion.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.parts.adapters.stepper_motors_adapter import StepperMotorsAdapter
from services.parts.adapters.belts_pulleys_adapter import BeltsPulleysAdapter
from services.parts.adapters.shaft_couplings_adapter import ShaftCouplingsAdapter

# ---------------------------------------------------------------------------
# Shared PartRecord envelope validation (mirrors test_parts_hub.py)
# ---------------------------------------------------------------------------

REQUIRED_RECORD_FIELDS = {
    "part_id", "domain", "title", "params",
    "sourcing", "provenance", "license",
}


def _validate_record(rec: dict) -> None:
    missing = REQUIRED_RECORD_FIELDS - set(rec.keys())
    assert not missing, f"PartRecord missing fields: {missing!r}"
    assert rec["part_id"], "part_id must be non-empty"
    assert isinstance(rec["part_id"], str)
    assert rec["domain"], "domain must be non-empty"
    assert rec["title"], "title must be non-empty"
    assert isinstance(rec["params"], dict), "params must be a dict"
    assert isinstance(rec["sourcing"], list), "sourcing must be a list"
    assert rec["provenance"], "provenance must be non-empty"
    assert rec["license"], "license must be non-empty"
    if rec.get("geometry_ref") is not None:
        gr = rec["geometry_ref"]
        assert isinstance(gr, dict), "geometry_ref must be a dict"
        assert "kind" in gr, "geometry_ref must have 'kind'"


_ADAPTERS = [
    ("StepperMotorsAdapter", StepperMotorsAdapter),
    ("BeltsPulleysAdapter", BeltsPulleysAdapter),
    ("ShaftCouplingsAdapter", ShaftCouplingsAdapter),
]


# ---------------------------------------------------------------------------
# Per-adapter shape / count / uniqueness
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,cls", _ADAPTERS)
class TestMotionAdapterShape:
    def test_yields_records(self, name, cls):
        adapter = cls()
        records = list(adapter.iter_records())
        assert len(records) > 0, f"{name} produced zero records"

    def test_records_satisfy_partrecord_shape(self, name, cls):
        adapter = cls()
        records = list(adapter.iter_records())
        for rec in records:
            _validate_record(rec)

    def test_part_ids_unique(self, name, cls):
        adapter = cls()
        ids = [rec["part_id"] for rec in adapter.iter_records()]
        assert len(ids) == len(set(ids)), f"{name} emitted duplicate part_ids"

    def test_count_matches_iter_records(self, name, cls):
        adapter = cls()
        assert adapter.count() == len(list(adapter.iter_records()))

    def test_get_roundtrip(self, name, cls):
        adapter = cls()
        first = next(iter(adapter.iter_records()))
        got = adapter.get(first["part_id"])
        assert got is not None
        assert got["part_id"] == first["part_id"]


# ---------------------------------------------------------------------------
# part_id uniqueness ACROSS all three new adapters (no cross-family collisions)
# ---------------------------------------------------------------------------

def test_part_ids_unique_across_new_adapters():
    all_ids: list[str] = []
    for _, cls in _ADAPTERS:
        all_ids.extend(rec["part_id"] for rec in cls().iter_records())
    assert len(all_ids) == len(set(all_ids)), "part_id collision across new motion adapters"


# ---------------------------------------------------------------------------
# Content spot checks (real standards, not fabricated)
# ---------------------------------------------------------------------------

def test_stepper_motor_covers_nema_frames_and_step_angles():
    records = list(StepperMotorsAdapter().iter_records())
    frames = {rec["params"]["nema_size"] for rec in records}
    assert frames == {8, 11, 14, 17, 23, 34}
    angles = {rec["params"]["step_angle_deg"] for rec in records}
    assert angles == {1.8, 0.9}
    nema17 = [rec for rec in records if rec["params"]["nema_size"] == 17]
    assert any(rec["params"]["body_square_mm"] == pytest.approx(42.3) for rec in nema17)
    assert any(rec["params"]["shaft_dia_mm"] == pytest.approx(5.0) for rec in nema17)
    # Search text carries the synonyms users actually type.
    titles = " ".join(rec["title"].lower() for rec in nema17)
    for token in ("nema17", "nema 17", "17hs", "stepper motor"):
        assert token in titles


def test_belts_pulleys_covers_profiles_and_geometry():
    records = list(BeltsPulleysAdapter().iter_records())
    profiles = {rec["params"]["profile"] for rec in records}
    assert profiles == {"GT2", "GT3", "HTD3M", "HTD5M", "T5", "MXL"}
    belts = [rec for rec in records if rec["params"]["part_type"] == "belt"]
    pulleys = [rec for rec in records if rec["params"]["part_type"] == "pulley"]
    assert belts and pulleys
    # Closed-loop belt length is exactly teeth * pitch.
    for rec in belts:
        p = rec["params"]
        assert p["length_mm"] == pytest.approx(p["teeth"] * p["pitch_mm"])
    titles = " ".join(rec["title"].lower() for rec in records)
    for token in ("gt2", "idler pulley", "toothed belt", "timing pulley"):
        assert token in titles


def test_shaft_couplings_covers_couplers_and_collars():
    records = list(ShaftCouplingsAdapter().iter_records())
    couplers = [rec for rec in records if rec["params"]["part_type"] == "shaft_coupler"]
    collars = [rec for rec in records if rec["params"]["part_type"] == "shaft_collar"]
    assert couplers and collars
    coupler_types = {rec["params"]["coupler_type"] for rec in couplers}
    assert coupler_types == {"setscrew", "clamp"}
    collar_types = {rec["params"]["collar_type"] for rec in collars}
    assert collar_types == {"setscrew", "clamp"}
    # Step-bore (asymmetric) couplers are present.
    assert any(rec["params"]["bore_a_mm"] != rec["params"]["bore_b_mm"] for rec in couplers)


# ---------------------------------------------------------------------------
# End-to-end: PartsHub.bootstrap() (which now registers these adapters
# alongside the other built-in adapters) — search("nema 17") ranks a
# stepper motor first, not a bearing (the proven "17" token accuracy bug).
# ---------------------------------------------------------------------------

def _make_hub():
    from services.parts.hub import PartsHub
    hub = PartsHub()
    hub.bootstrap()
    return hub


class TestPartsHubMotionSearch:
    def setup_method(self):
        self.hub = _make_hub()

    def test_nema_17_top_hit_is_stepper_not_bearing(self):
        results = self.hub.search("nema 17", include_live=False, limit=10)
        assert results, "expected local results for 'nema 17'"
        top = results[0]
        assert top["domain"] == "motor", (
            f"top hit for 'nema 17' should be a stepper motor (domain='motor'), "
            f"got domain={top['domain']!r} part_id={top['part_id']!r}"
        )
        assert "bearing" not in top["title"].lower()

    def test_stepper_motor_query_finds_records(self):
        results = self.hub.search("stepper motor", include_live=False, limit=10)
        assert results
        assert any(r["domain"] == "motor" for r in results)

    def test_belt_and_pulley_queries_find_records(self):
        belt_results = self.hub.search("timing belt", include_live=False, limit=10)
        assert belt_results
        assert any(r["domain"] == "motion" for r in belt_results)

        pulley_results = self.hub.search("gt2 pulley", include_live=False, limit=10)
        assert pulley_results
        assert any(r["domain"] == "motion" for r in pulley_results)

    def test_shaft_coupler_and_collar_queries_find_records(self):
        coupler_results = self.hub.search("shaft coupler", include_live=False, limit=10)
        assert coupler_results
        assert any(r["domain"] == "motion" for r in coupler_results)

        collar_results = self.hub.search("shaft collar", include_live=False, limit=10)
        assert collar_results
        assert any(r["domain"] == "motion" for r in collar_results)
