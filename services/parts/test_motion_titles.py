"""Visible motion catalog names and direct alias discovery."""

import pytest

from services.parts.adapters.belts_pulleys_adapter import BeltsPulleysAdapter
from services.parts.adapters.shaft_couplings_adapter import ShaftCouplingsAdapter
from services.parts.adapters.stepper_motors_adapter import StepperMotorsAdapter


def test_motion_titles_preserve_variant_dimensions():
    belts = list(BeltsPulleysAdapter().iter_records())
    belt = next(r for r in belts if r["part_id"] == "motion:belt:GT2-160mm-10mm")
    assert belt["title"] == "GT2 Timing Belt 160 mm × 10 mm (80T)"
    assert belt["family"] == "GT2 Timing Belt"
    pulley = next(r for r in belts if r["part_id"] == "motion:pulley:GT2-20T-5mm")
    assert pulley["title"] == "GT2 Timing Pulley 20T, 5 mm Bore, 6/9/10 mm Belt Width"

    motors = list(StepperMotorsAdapter().iter_records())
    motor = next(r for r in motors if r["part_id"] == "motor:stepper:NEMA11-19mm-0p9deg")
    assert motor["title"] == "NEMA 11 Stepper Motor 28×28×19 mm, 0.9°"
    assert motor["family"] == "NEMA 11 Stepper Motor"

    couplings = list(ShaftCouplingsAdapter().iter_records())
    step_bore = next(r for r in couplings if r["part_id"] == "motion:coupler:COUPLER-clamp-5mmx8mm")
    assert "5mm to 8mm step bore" in step_bore["title"]
    assert step_bore["family"] == "Split-Clamp Rigid Shaft Coupler"
    collar = next(r for r in couplings if r["part_id"] == "motion:collar:COLLAR-setscrew-3mm")
    assert "one-piece set-screw shaft collar" in collar["title"]
    assert collar["family"] == "One-Piece Set-Screw Shaft Collar"


@pytest.mark.parametrize("adapter, query", [
    (BeltsPulleysAdapter, "idler pulley"),
    (BeltsPulleysAdapter, "endless loop belt"),
    (StepperMotorsAdapter, "17hs40"),
    (StepperMotorsAdapter, "high-res half-step"),
    (ShaftCouplingsAdapter, "leadscrew coupler"),
    (ShaftCouplingsAdapter, "locking collar"),
])
def test_direct_search_finds_aliases(adapter, query):
    results = adapter().search(query)
    assert results
    assert all(any(query in alias.lower() for alias in rec["aliases"]) for rec in results)
    assert all(query not in rec["title"].lower() for rec in results)
