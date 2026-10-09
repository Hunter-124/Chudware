"""C1 — flanged bushing: fit bore + bolt circle.

A flanged bushing that press-fits nothing but must provide a SLIDING fit bore for a
20 mm h7 shaft (bore d20 H7) and a 4-hole bolt circle on the flange for M4 clearance.

Ground-truth ACs the gym grades ITSELF:
  AC1  bore diameter recorded == 20 mm with an H7 hole tolerance (sliding fit basis)
  AC2  bore fit clearance for the d20 h7 shaft is in the sliding-fit band [0, 0.042] mm
  AC3  bolt circle: 4 holes, M4 clearance (d4.5), on the declared PCD
  AC4  verify_battery recorded PASS (no incoherent geometry)

The scripted agent turns (seeds 1..3) exercise: a clean pass (seed 1), a tool_misuse
variant (seed 2 uses measure_part instead of fit_report for AC2), and a context_loss
variant (seed 3 cites a nonexistent DIM). These let the classifier prove it fires the
right class on the right seed — honest, since the turns are LABELED, not graded-as-model.
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value, coerce_number, dimension_fit_class)

# H7 on d20: +0.021 / 0 ; h7 shaft on d20: 0 / -0.021 → clearance band [0, 0.042].
BORE_NOM = 20.0
SLIDING_CLEARANCE_BAND = (0.0, 0.042)   # mm, min..max clearance for d20 H7 / h7
BOLT_HOLES = 4
M4_CLEARANCE_D = 4.5                     # mm, close clearance for M4
PCD = 34.0                               # mm, declared bolt-circle pitch diameter


# ---- ground-truth graders ---------------------------------------------------

def _grade_bore(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dim = ledger_dimension(ledger, "bore")
    if not dim:
        return "FAIL", "no bore dimension recorded in ledger"
    val = coerce_number(dim.get("value", 0))
    if val is None:
        return "FAIL", f"bore value {dim.get('value')!r} is not numeric"
    if abs(val - BORE_NOM) > 1e-6:
        return "FAIL", f"bore value {dim.get('value')} != {BORE_NOM}"
    fit = dimension_fit_class(dim)
    if "H7" not in fit:
        return "FAIL", f"bore tolerance is {fit or 'none'}, expected H7 (sliding-fit basis)"
    return "PASS", f"bore d{BORE_NOM} {fit}"


def _grade_fit(ledger: dict, ctx: dict) -> "tuple[str,str]":
    clr = ledger_decision_value(ledger, "fit_bore", "clearance")
    if clr is None:
        return "FAIL", "no fit_bore decision with a clearance value"
    clr_n = coerce_number(clr)
    if clr_n is None:
        return "FAIL", f"clearance {clr!r} is not numeric"
    lo, hi = SLIDING_CLEARANCE_BAND
    if not (lo <= clr_n <= hi):
        return "FAIL", f"clearance {clr} outside sliding band {SLIDING_CLEARANCE_BAND}"
    return "PASS", f"sliding-fit clearance {clr_n} mm in band"


def _grade_bolt_circle(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bc = ledger_decision_value(ledger, "bolt_circle", "holes")
    hole_d = ledger_decision_value(ledger, "bolt_circle", "hole_d")
    pcd = ledger_decision_value(ledger, "bolt_circle", "pcd")
    bc_n = coerce_number(bc)
    if bc_n is None or int(bc_n) != BOLT_HOLES:
        return "FAIL", f"bolt circle has {bc} holes, expected {BOLT_HOLES}"
    hole_n = coerce_number(hole_d)
    if hole_n is None or abs(hole_n - M4_CLEARANCE_D) > 1e-6:
        return "FAIL", f"hole d {hole_d} != M4 clearance {M4_CLEARANCE_D}"
    pcd_n = coerce_number(pcd)
    if pcd_n is None or abs(pcd_n - PCD) > 1e-6:
        return "FAIL", f"PCD {pcd} != {PCD}"
    return "PASS", f"{BOLT_HOLES}xM4 clearance on PCD {PCD}"


def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


CHECKS = [
    AcceptanceCheck("AC1", "bore d20 H7 (sliding-fit basis) recorded", _grade_bore),
    AcceptanceCheck("AC2", "sliding-fit clearance for d20 h7 shaft in band", _grade_fit),
    AcceptanceCheck("AC3", f"{BOLT_HOLES}xM4 clearance bolt circle on PCD {PCD}",
                    _grade_bolt_circle),
    AcceptanceCheck("AC4", "verify_battery PASS (coherent geometry)", _grade_battery),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a flanged bushing with a sliding-fit bore for a 20 mm shaft and a "
        "4-hole M4 bolt circle on the flange.",
    "deliverables": [{
        "id": "DL1", "kind": "part", "name": "flanged bushing",
        "description": "bushing with a d20 sliding-fit bore and a 4xM4 flange bolt circle",
        "acceptance_criteria": [
            {"id": "AC1", "text": "bore is a sliding fit on the d20 h7 shaft",
             "check": {"tool": "fit_report", "params_hint": {"nominal": 20, "hole": "H7"},
                       "pass_rule": "clearance in [0,0.042]"}},
            {"id": "AC2", "text": "sliding-fit clearance in band",
             "check": {"tool": "fit_report", "pass_rule": "clearance in [0,0.042]"}},
            {"id": "AC3", "text": "4xM4 clearance bolt circle on PCD 34",
             "check": {"tool": "measure_part", "pass_rule": "4 holes d4.5 on PCD34"}},
            {"id": "AC4", "text": "geometry coherent",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
        ],
        "depends_on": [], "target_refs": {"doc_id": None, "features": []},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "material 6061-T6 (machined bushing)", "basis": "default machined",
         "needs_confirm": False},
    ],
}


# ---- scripted agent turns (per seed) ----------------------------------------
# Each entry is a ledger-event payload the runner appends with a matching kind. The
# runner reads the "_kind" key to pick the ledger event kind, then strips it.
def _base_turns() -> list[dict]:
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "bore_diameter", "value": BORE_NOM,
         "unit": "mm", "driver": "spec: sliding fit d20", "provenance": "fit_report",
         "tolerance": {"fit_class": "H7"}, "status": "confirmed"},
        {"_kind": "decision", "id": "fit_bore", "stage": "specify", "for_ac": "AC2",
         "text": "bore d20 H7 sliding fit on d20 h7 shaft", "clearance": 0.021,
         "rationale": "fit_report: H7/h7 sliding, mean clearance 0.021 mm",
         "node": "n_spec_DL1"},
        {"_kind": "tool_call", "tool": "fit_report", "for_ac": "AC2", "node": "n_spec_DL1",
         "args": {"nominal": 20, "hole": "H7", "shaft": "h7"},
         "hypothesis": "verify sliding clearance"},
        {"_kind": "decision", "id": "bolt_circle", "stage": "plan", "for_ac": "AC3",
         "text": "4xM4 clearance on PCD34", "holes": BOLT_HOLES, "hole_d": M4_CLEARANCE_D,
         "pcd": PCD, "rationale": "bolt_circle helper: 4 x d4.5 @ PCD34",
         "node": "n_plan_DL1"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: tool_misuse — AC2 checked with measure_part instead of fit_report.
    misuse = [dict(t) for t in _base_turns()]
    for t in misuse:
        if t.get("tool") == "fit_report" and t.get("for_ac") == "AC2":
            t["tool"] = "measure_part"   # wrong tool for a fit criterion
    # keep grading PASS (the fit decision is still recorded) so this isolates tool_misuse.

    # seed 3: context_loss — a decision cites a nonexistent DIM99.
    ctxloss = [dict(t) for t in _base_turns()]
    ctxloss.append({"_kind": "decision", "id": "note1", "stage": "build",
                    "text": "flange thickness follows DIM99 (bore) + 2*wall",
                    "rationale": "derive flange from DIM99", "node": "n_build_DL1"})

    return {
        1: {"DL1": clean},
        2: {"DL1": misuse},
        3: {"DL1": ctxloss},
    }


CHALLENGE = Challenge(
    id="C1", name="flanged bushing", lane="parts-sourcing",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes="fit bore + bolt circle; seed2=tool_misuse, seed3=context_loss (labeled).",
)
