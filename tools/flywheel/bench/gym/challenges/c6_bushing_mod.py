"""C6 — bushing mod: re-bore the C1 flanged bushing to fit a 25 mm shaft.

A MODIFICATION of the C1 flanged bushing output — "fit a 25 mm shaft": re-bore to d25
sliding fit (H7) while keeping the 4-hole bolt circle. Its deliverable kind is
"modification" and its target_refs resolve to the existing C1 doc.

Ground-truth ACs the gym grades ITSELF:
  AC1  bore re-bored to d25 H7   — dimension "bore" value == 25.0, fit_class contains H7
  AC2  sliding clearance for d25 H7/h7 in [0, 0.046] mm  — decision fit_bore.clearance
  AC3  bolt circle preserved     — decision bolt_circle.holes == 4 (unchanged from C1)
  AC4  verify_battery PASS        — battery artifact verdict PASS

The scripted agent turns (seeds 1..3) exercise: a clean pass (seed 1), a context_loss
variant (seed 2 cites a nonexistent DIM88), and a grind variant (seed 3 — three
identical re-bore tool_calls, same node, same hypothesis, trip the grind threshold).
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value, coerce_number, dimension_fit_class)

# H7 on d25: +0.021 / 0 ; h7 shaft on d25: 0 / -0.021 → clearance band [0, 0.042].
# The manifest/AC band is widened to [0, 0.046] to cover IT7 at 25 mm generously.
BORE_NOM = 25.0
SLIDING_CLEARANCE_BAND = (0.0, 0.046)   # mm, min..max clearance for d25 H7 / h7
BOLT_HOLES = 4                          # unchanged from C1


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
    return "PASS", f"bore re-bored to d{BORE_NOM} {fit}"


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
    bc_n = coerce_number(bc)
    if bc_n is None or int(bc_n) != BOLT_HOLES:
        return "FAIL", f"bolt circle has {bc} holes, expected {BOLT_HOLES} (preserved)"
    return "PASS", f"bolt circle preserved: {BOLT_HOLES} holes"


def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


CHECKS = [
    AcceptanceCheck("AC1", "bore re-bored to d25 H7 (sliding-fit basis)", _grade_bore),
    AcceptanceCheck("AC2", "sliding-fit clearance for d25 h7 shaft in band", _grade_fit),
    AcceptanceCheck("AC3", "bolt circle preserved (4 holes, unchanged from C1)",
                    _grade_bolt_circle),
    AcceptanceCheck("AC4", "verify_battery PASS (coherent geometry)", _grade_battery),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Modify the flanged bushing to fit a 25 mm shaft: re-bore to a d25 sliding fit "
        "and keep the existing 4-hole bolt circle.",
    "deliverables": [{
        "id": "DL1", "kind": "modification", "name": "bushing mod (fit 25 mm shaft)",
        "description": "re-bore the C1 flanged bushing to d25 H7 sliding fit; keep bolt circle",
        "acceptance_criteria": [
            {"id": "AC1", "text": "bore re-bored to d25 H7",
             "check": {"tool": "fit_report", "params_hint": {"nominal": 25, "hole": "H7"},
                       "pass_rule": "bore==25 and H7"}},
            {"id": "AC2", "text": "sliding-fit clearance for d25 h7 shaft in band",
             "check": {"tool": "fit_report", "pass_rule": "clearance in [0,0.046]"}},
            {"id": "AC3", "text": "bolt circle preserved (4 holes)",
             "check": {"tool": "measure_part", "pass_rule": "4 holes unchanged"}},
            {"id": "AC4", "text": "geometry coherent",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
        ],
        "depends_on": [],
        # modification deliverable: target_refs must resolve to the existing C1 doc.
        "target_refs": {"doc_id": "doc_C1", "features": ["bore"]},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "base part is the C1 flanged bushing; only the bore changes",
         "basis": "modification target", "needs_confirm": False},
    ],
}


# ---- scripted agent turns (per seed) ----------------------------------------
def _base_turns() -> list[dict]:
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "bore_diameter", "value": BORE_NOM,
         "unit": "mm", "driver": "re-bore to d25 sliding fit for the 25 mm shaft",
         "provenance": "fit_report: H7 hole for a d25 h7 shaft (sliding basis)",
         "tolerance": {"fit_class": "H7", "plus": 0.021, "minus": 0.0}, "status": "confirmed"},
        {"_kind": "decision", "id": "fit_bore", "stage": "specify", "for_ac": "AC2",
         "text": "bore d25 H7 sliding fit on d25 h7 shaft", "clearance": 0.021,
         "rationale": "fit_report: H7/h7 sliding, mean clearance 0.021 mm",
         "node": "n_spec_DL1"},
        {"_kind": "tool_call", "tool": "fit_report", "for_ac": "AC2", "node": "n_spec_DL1",
         "args": {"nominal": 25, "hole": "H7", "shaft": "h7"},
         "hypothesis": "verify sliding clearance for the re-bore"},
        {"_kind": "decision", "id": "bolt_circle", "stage": "plan", "for_ac": "AC3",
         "text": "keep the C1 4-hole bolt circle unchanged", "holes": BOLT_HOLES,
         "rationale": "bolt circle inherited from C1; only the bore is modified",
         "node": "n_plan_DL1"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean after re-bore"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: context_loss — a decision cites a nonexistent DIM88.
    ctxloss = [dict(t) for t in _base_turns()]
    ctxloss.append({"_kind": "decision", "id": "note1", "stage": "build",
                    "text": "flange face offset follows DIM88 (base thickness) + 1.0",
                    "rationale": "derive flange offset from DIM88", "node": "n_build_DL1"})

    # seed 3: grind — three identical re-bore tool_calls, same node, same hypothesis.
    grind = [dict(t) for t in _base_turns()]
    for _ in range(3):
        grind.append({"_kind": "tool_call", "tool": "add_features", "for_ac": "AC1",
                      "node": "n_build_DL1",
                      "args": {"op": "bore", "diameter": 25.0, "through": True},
                      "hypothesis": "rebore unchanged"})

    # seed 4: execution_stall — the shape observed in real runs, LABELED: real
    # agent turns whose every tool call failed to execute (resolution gaps), no
    # dimension/decision ever recorded → every AC fails with nothing built.
    # Distinct tools (no repeated fingerprint → not grind) and resolution-failure
    # errors (not kwarg rejections → not tool_misuse).
    stall = [
        {"_kind": "agent_turn", "_actor": "spec_writer", "event": "tool_call",
         "tool": "ledger_get", "args": {}, "ok": False,
         "error": "\"tool 'ledger_get' could not be resolved\""},
        {"_kind": "agent_turn", "_actor": "spec_writer", "event": "tool_call",
         "tool": "part_search", "args": {"query": "25 mm shaft"}, "ok": False,
         "error": "\"tool 'part_search' could not be resolved\""},
        {"_kind": "agent_turn", "_actor": "feature_modeler", "event": "tool_call",
         "tool": "ask_user", "args": {"questions": ["bore fit class?"]}, "ok": False,
         "error": "\"tool 'ask_user' could not be resolved\""},
    ]

    return {
        1: {"DL1": clean},
        2: {"DL1": ctxloss},
        3: {"DL1": grind},
        4: {"DL1": stall},
    }


CHALLENGE = Challenge(
    id="C6", name="bushing mod (fit 25mm shaft)", lane="parts-sourcing",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3, 4), scripts=_seed_scripts(), modifies="C1",
    notes="re-bore C1 to d25 H7, keep bolt circle; seed2=context_loss, seed3=grind.",
)
