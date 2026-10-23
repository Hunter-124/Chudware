"""C4 — two-part hinge: assembly fit + motion clearance.

A two-part hinge (leaf A + leaf B sharing a pin) that must assemble with a running
clearance on the pin and swing 180 deg without the leaves colliding.

Ground-truth ACs the gym grades ITSELF:
  AC1  pin running clearance in [0.05, 0.20] mm  — decision pin_fit.clearance in band
  AC2  knuckle bore matches pin w/ running fit   — dimension "pin" value > 0, fit_class present
  AC3  motion clearance: swing arc clears        — decision motion.min_clearance_mm >= 0.5
       and motion.swept_deg >= 180
  AC4  assembly interference clean               — interference artifact clashes == 0

The scripted agent turns (seeds 1..3) exercise: a clean pass (seed 1), a wrong_question
variant (seed 2 asks a useless "what color?" question and leaves the manifest's blocking
material ambiguity unresolved), and a tool_misuse variant (seed 3 measures AC3 with
measure_part while the manifest AC3 check tool is check_interference). The material
ambiguity lives permanently in the manifest; seeds 1 and 3 include a proper "material"
question so wrong_question does NOT fire for them.
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value)

PIN_CLEARANCE_BAND = (0.05, 0.20)   # mm, running fit on the pin
MOTION_MIN_CLEARANCE = 0.5          # mm, leaves must clear through the swing
SWEEP_DEG = 180.0


# ---- ground-truth graders ---------------------------------------------------

def _grade_pin_fit(ledger: dict, ctx: dict) -> "tuple[str,str]":
    clr = ledger_decision_value(ledger, "pin_fit", "clearance")
    if clr is None:
        return "FAIL", "no pin_fit decision with a clearance value"
    lo, hi = PIN_CLEARANCE_BAND
    if not (lo <= float(clr) <= hi):
        return "FAIL", f"pin clearance {clr} outside running band {PIN_CLEARANCE_BAND}"
    return "PASS", f"pin running clearance {clr} mm in band"


def _grade_knuckle_bore(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dim = ledger_dimension(ledger, "pin")
    if not dim:
        return "FAIL", "no pin/knuckle-bore dimension recorded in ledger"
    if float(dim.get("value", 0)) <= 0:
        return "FAIL", f"pin bore value {dim.get('value')} not > 0"
    tol = dim.get("tolerance") or {}
    fit = str(tol.get("fit_class") or tol.get("class") or "")
    if not fit:
        return "FAIL", "knuckle-bore dimension has no fit_class (running fit expected)"
    return "PASS", f"knuckle bore d{dim.get('value')} {fit}"


def _grade_motion(ledger: dict, ctx: dict) -> "tuple[str,str]":
    mc = ledger_decision_value(ledger, "motion", "min_clearance_mm")
    sw = ledger_decision_value(ledger, "motion", "swept_deg")
    if mc is None or sw is None:
        return "FAIL", f"motion missing min_clearance_mm ({mc}) or swept_deg ({sw})"
    if float(mc) < MOTION_MIN_CLEARANCE:
        return "FAIL", f"min clearance {mc} mm < {MOTION_MIN_CLEARANCE} mm through swing"
    if float(sw) < SWEEP_DEG:
        return "FAIL", f"swept {sw} deg < {SWEEP_DEG} deg"
    return "PASS", f"clears {mc} mm over {sw} deg swing"


def _grade_interference(ledger: dict, ctx: dict) -> "tuple[str,str]":
    art = ledger_artifact(ledger, "interference")
    if not art:
        return "FAIL", "no interference artifact recorded"
    clashes = art.get("clashes")
    if clashes != 0:
        return "FAIL", f"interference reports {clashes} clashes"
    return "PASS", "assembly interference clean (0 clashes)"


CHECKS = [
    AcceptanceCheck("AC1", "pin running clearance in [0.05, 0.20] mm", _grade_pin_fit),
    AcceptanceCheck("AC2", "knuckle bore matches pin with a running fit", _grade_knuckle_bore),
    AcceptanceCheck("AC3", "swing arc clears >= 0.5 mm over 180 deg", _grade_motion),
    AcceptanceCheck("AC4", "assembly interference clean (0 clashes)", _grade_interference),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a two-part hinge (leaf A + leaf B sharing a pin) that assembles with a "
        "running clearance on the pin and swings 180 deg without the leaves colliding.",
    "deliverables": [{
        "id": "DL1", "kind": "assembly", "name": "two-part hinge",
        "description": "leaf A + leaf B on a shared pin, running fit, 180 deg swing clean",
        "acceptance_criteria": [
            {"id": "AC1", "text": "pin has a running clearance",
             "check": {"tool": "fit_report", "pass_rule": "clearance in [0.05,0.20]"}},
            {"id": "AC2", "text": "knuckle bore matches pin with a running fit",
             "check": {"tool": "measure_part", "pass_rule": "pin bore > 0 with fit_class"}},
            {"id": "AC3", "text": "swing arc clears without collision",
             # NOTE: intentionally check_interference (seed 3 exercises tool misuse against it).
             "check": {"tool": "check_interference", "pass_rule": "min_clearance>=0.5 @180deg"}},
            {"id": "AC4", "text": "assembly interference clean",
             "check": {"tool": "check_interference", "pass_rule": "clashes==0"}},
        ],
        "depends_on": [], "target_refs": {"doc_id": None, "features": []},
        "status": "pending",
    }],
    "ambiguities": [
        {"text": "pin material / process not specified", "blocking": True,
         "question_id": None},
    ],
    "assumptions": [],
}


# ---- scripted agent turns (per seed) ----------------------------------------
def _base_turns() -> list[dict]:
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "pin_bore_diameter", "value": 5.0,
         "unit": "mm", "driver": "d5 pin running fit in the knuckles",
         "provenance": "fit_report: H8/f7 running fit on the d5 pin",
         "tolerance": {"fit_class": "H8", "plus": 0.018, "minus": 0.0}, "status": "confirmed"},
        {"_kind": "decision", "id": "pin_fit", "stage": "specify", "for_ac": "AC1",
         "text": "d5 H8/f7 running fit on the shared pin", "clearance": 0.10,
         "rationale": "fit_report: H8/f7 mean clearance 0.10 mm (running)",
         "node": "n_spec_DL1"},
        {"_kind": "decision", "id": "motion", "stage": "verify", "for_ac": "AC3",
         "text": "leaves swept 180 deg about the pin axis", "min_clearance_mm": 0.8,
         "swept_deg": 180.0,
         "rationale": "check_interference swept: min gap 0.8 mm over the full 180 deg arc",
         "node": "n_verify_DL1"},
        {"_kind": "tool_call", "tool": "check_interference", "for_ac": "AC3",
         "node": "n_verify_DL1", "args": {"sweep_deg": 180, "step_deg": 5},
         "hypothesis": "sweep the leaves and check for collision"},
        {"_kind": "artifact", "kind": "interference", "id": "interf_DL1", "clashes": 0,
         "stage": "verify", "note": "static assembly interference clean"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean"},
    ]


def _material_question() -> dict:
    return {"_kind": "question", "id": "Q_material", "for_ac": None,
            "text": "What material / process for the hinge pin and leaves? "
                    "It drives the running-fit tolerance.",
            "bearing": "critical", "node": "n_intake_DL1"}


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    # seed 1: clean + asks the proper (blocking) material question.
    clean = _base_turns() + [_material_question()]

    # seed 2: wrong_question — asks a useless "what color?" and does NOT ask the
    # blocking material question, so the manifest's blocking ambiguity stays open.
    wrong = _base_turns() + [
        {"_kind": "question", "id": "Q_color", "for_ac": None,
         "text": "What color should the hinge be?", "bearing": "none",
         "node": "n_intake_DL1"},
    ]

    # seed 3: tool_misuse — AC3 measured with measure_part (manifest AC3 tool is
    # check_interference). Includes the proper material question so wrong_question does
    # NOT fire. Grading stays PASS (motion decision is still recorded).
    misuse = [dict(t) for t in _base_turns()]
    for t in misuse:
        if t.get("tool") == "check_interference" and t.get("for_ac") == "AC3":
            t["tool"] = "measure_part"   # wrong tool for the AC3 interference check
    misuse.append(_material_question())

    return {
        1: {"DL1": clean},
        2: {"DL1": wrong},
        3: {"DL1": misuse},
    }


CHALLENGE = Challenge(
    id="C4", name="two-part hinge", lane="assemblies-drawings",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes="pin fit + 180deg motion; seed2=wrong_question, seed3=tool_misuse.",
)
