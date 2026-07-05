"""C3 — bearing block: 6202 seat + FEA margin under load.

A pillow-block style bearing block seating a 6202 bearing (OD 35 mm) that must hold
an FEA safety margin >= 2.0 under a 500 N radial load.

Ground-truth ACs the gym grades ITSELF:
  AC1  bore seat == 35.0 mm with a press/locational fit  — dimension "seat"/"bore"
       value == 35.0, tolerance fit_class contains K6 or N6
  AC2  FEA safety factor >= 2.0                          — decision fea_margin.safety_factor
  AC3  max stress below yield                            — fea_margin.max_stress_mpa < yield_mpa
  AC4  verify_battery PASS                               — battery artifact verdict PASS

The scripted agent turns (seeds 1..3) exercise: a clean pass (seed 1), an
incoherent_geometry variant (seed 2 — margin FAILs at 1.4 and the battery reports
FAIL), and a grind variant (seed 3 — three identical run_fea_static calls with no
changed hypothesis trip the grind threshold). Seed 2's failing turns MUTATE the base
payloads in place (the graders read the first-matching decision/artifact).
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value)

SEAT_NOM = 35.0                # mm, 6202 outer diameter
SAFETY_MIN = 2.0
PRESS_FITS = ("K6", "N6")      # locational-transition / press-ish classes


# ---- ground-truth graders ---------------------------------------------------

def _grade_seat(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dim = ledger_dimension(ledger, "seat") or ledger_dimension(ledger, "bore")
    if not dim:
        return "FAIL", "no seat/bore dimension recorded in ledger"
    if abs(float(dim.get("value", 0)) - SEAT_NOM) > 1e-6:
        return "FAIL", f"seat value {dim.get('value')} != {SEAT_NOM}"
    tol = dim.get("tolerance") or {}
    fit = str(tol.get("fit_class") or tol.get("class") or "").upper()
    if not any(f in fit for f in PRESS_FITS):
        return "FAIL", f"seat fit {fit or 'none'}, expected one of {PRESS_FITS}"
    return "PASS", f"seat d{SEAT_NOM} {fit} (press/locational)"


def _grade_fea_margin(ledger: dict, ctx: dict) -> "tuple[str,str]":
    sf = ledger_decision_value(ledger, "fea_margin", "safety_factor")
    if sf is None:
        return "FAIL", "no fea_margin decision with a safety_factor value"
    if float(sf) < SAFETY_MIN:
        return "FAIL", f"safety factor {sf} < {SAFETY_MIN}"
    return "PASS", f"FEA safety factor {sf} >= {SAFETY_MIN}"


def _grade_stress(ledger: dict, ctx: dict) -> "tuple[str,str]":
    mx = ledger_decision_value(ledger, "fea_margin", "max_stress_mpa")
    yld = ledger_decision_value(ledger, "fea_margin", "yield_mpa")
    if mx is None or yld is None:
        return "FAIL", f"fea_margin missing stress ({mx}) or yield ({yld})"
    if float(mx) >= float(yld):
        return "FAIL", f"max stress {mx} MPa >= yield {yld} MPa"
    return "PASS", f"max stress {mx} MPa < yield {yld} MPa"


def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


CHECKS = [
    AcceptanceCheck("AC1", "bore seat d35 with press/locational fit (K6/N6)", _grade_seat),
    AcceptanceCheck("AC2", "FEA safety factor >= 2.0 under 500 N radial", _grade_fea_margin),
    AcceptanceCheck("AC3", "max stress below yield", _grade_stress),
    AcceptanceCheck("AC4", "verify_battery PASS (coherent geometry)", _grade_battery),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a pillow-block style bearing block seating a 6202 bearing (OD 35 mm) "
        "with an FEA safety margin >= 2.0 under a 500 N radial load.",
    "deliverables": [{
        "id": "DL1", "kind": "part", "name": "bearing block",
        "description": "pillow-block bearing block for a 6202 (OD 35) under 500 N radial",
        "acceptance_criteria": [
            {"id": "AC1", "text": "bore seat d35 with press/locational fit",
             "check": {"tool": "fit_report", "params_hint": {"nominal": 35, "hole": "K6"},
                       "pass_rule": "seat==35 and fit in {K6,N6}"}},
            {"id": "AC2", "text": "FEA safety factor >= 2.0 under 500 N radial load",
             "check": {"tool": "run_fea_static", "pass_rule": "safety_factor >= 2.0"}},
            {"id": "AC3", "text": "max stress below yield",
             "check": {"tool": "run_fea_static", "pass_rule": "max_stress_mpa < yield_mpa"}},
            {"id": "AC4", "text": "geometry coherent",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
        ],
        "depends_on": [], "target_refs": {"doc_id": None, "features": []},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "material 1045 steel (yield ~530 MPa)", "basis": "default machined block",
         "needs_confirm": False},
    ],
}


# ---- scripted agent turns (per seed) ----------------------------------------
def _base_turns() -> list[dict]:
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "bearing_seat_diameter",
         "value": SEAT_NOM, "unit": "mm", "driver": "6202 OD 35, press/locational seat",
         "provenance": "fit_report: K6 seat for a 6202 outer race",
         "tolerance": {"fit_class": "K6", "plus": 0.018, "minus": 0.002},
         "status": "confirmed"},
        {"_kind": "decision", "id": "fea_margin", "stage": "verify", "for_ac": "AC2",
         "text": "500 N radial FEA on the block", "safety_factor": 3.1,
         "max_stress_mpa": 171.0, "yield_mpa": 530.0,
         "rationale": "run_fea_static: peak vonMises 171 MPa vs 530 MPa yield → SF 3.1",
         "node": "n_build_DL1"},
        {"_kind": "tool_call", "tool": "run_fea_static", "for_ac": "AC2",
         "node": "n_build_DL1",
         "args": {"load_N": 500, "direction": "radial", "material": "1045"},
         "hypothesis": "verify safety factor under 500 N radial"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: incoherent_geometry — MUTATE in place so the first-matching grader sees
    # the failing values: SF drops to 1.4 (AC2 FAIL) and the battery reports FAIL (AC4).
    incoherent = [dict(t) for t in _base_turns()]
    for t in incoherent:
        if t.get("id") == "fea_margin":
            t["safety_factor"] = 1.4
            t["max_stress_mpa"] = 379.0
            t["rationale"] = "run_fea_static: peak 379 MPa vs 530 MPa yield → SF 1.4 (< 2.0)"
        if t.get("kind") == "battery":
            t["verdict"] = "FAIL"
            t["note"] = "verify_battery FAIL: under-margined section, incoherent geometry"

    # seed 3: grind — three identical run_fea_static calls, same node, same args, same
    # hypothesis; no changed hypothesis → grind fires at threshold 3. Grading stays PASS.
    grind = [dict(t) for t in _base_turns()]
    for _ in range(3):
        grind.append({"_kind": "tool_call", "tool": "run_fea_static", "for_ac": "AC2",
                      "node": "n_build_DL1",
                      "args": {"load_N": 500, "direction": "radial", "material": "1045"},
                      "hypothesis": "rerun fea unchanged"})
    # A SECOND node with exactly TWO identical attempts. This does NOT fire grind at the
    # default threshold 3, but DOES at 2 — so the assess-adjust-rerun loop's grind 3->2
    # tightening promotes exactly this node, giving a real, deterministic +1 delta (the
    # anti-grind sensitivity dial actually changing a classification). See report deltas.
    for _ in range(2):
        grind.append({"_kind": "tool_call", "tool": "run_fea_modal",
                      "node": "n_verify_DL1",
                      "args": {"modes": 6, "material": "1045"},
                      "hypothesis": "rerun modal unchanged"})

    return {
        1: {"DL1": clean},
        2: {"DL1": incoherent},
        3: {"DL1": grind},
    }


CHALLENGE = Challenge(
    id="C3", name="bearing block", lane="simulation",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes="6202 seat + FEA margin; seed2=incoherent_geometry, seed3=grind.",
)
