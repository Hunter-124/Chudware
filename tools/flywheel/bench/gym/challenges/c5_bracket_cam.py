"""C5 — bracket + CAM program: zero-gouge.

An L-bracket plus a CAM program to machine it from 6061 with zero gouges.

Ground-truth ACs the gym grades ITSELF:
  AC1  bracket built                — dimension "thickness" value > 0, provenance non-empty
  AC2  CAM program gouge-free       — decision cam_sim.gouges == 0
  AC3  machined fraction >= 0.90    — decision cam_sim.machined_fraction >= 0.90
  AC4  verify_battery PASS          — battery artifact verdict PASS

The scripted agent turns (seeds 1..3) exercise: a clean pass (seed 1), an
incoherent_geometry variant via a CAM gouge (seed 2 — cam_sim gouges=2 → AC2 FAIL and
the battery reports FAIL; values MUTATE the base payloads in place so the first-matching
graders see them), and a tool_misuse variant (seed 3 measures AC2 with measure_part
while the manifest AC2 check tool is cam_simulate; cam_sim stays clean so grading passes
and only tool_misuse fires).
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value)

MACHINED_FRACTION_MIN = 0.90


# ---- ground-truth graders ---------------------------------------------------

def _grade_bracket(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dim = ledger_dimension(ledger, "thickness")
    if not dim:
        return "FAIL", "no thickness dimension recorded in ledger"
    if float(dim.get("value", 0)) <= 0:
        return "FAIL", f"thickness value {dim.get('value')} not > 0"
    if not str(dim.get("provenance") or "").strip():
        return "FAIL", "thickness dimension has empty provenance"
    return "PASS", f"bracket thickness {dim.get('value')} mm (provenance present)"


def _grade_gouges(ledger: dict, ctx: dict) -> "tuple[str,str]":
    g = ledger_decision_value(ledger, "cam_sim", "gouges")
    if g is None:
        return "FAIL", "no cam_sim decision with a gouges value"
    if int(g) != 0:
        return "FAIL", f"cam_sim reports {g} gouges"
    return "PASS", "CAM program gouge-free (0 gouges)"


def _grade_machined(ledger: dict, ctx: dict) -> "tuple[str,str]":
    mf = ledger_decision_value(ledger, "cam_sim", "machined_fraction")
    if mf is None:
        return "FAIL", "no cam_sim decision with a machined_fraction value"
    if float(mf) < MACHINED_FRACTION_MIN:
        return "FAIL", f"machined fraction {mf} < {MACHINED_FRACTION_MIN}"
    return "PASS", f"machined fraction {mf} >= {MACHINED_FRACTION_MIN}"


def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


CHECKS = [
    AcceptanceCheck("AC1", "L-bracket built (thickness > 0, provenance)", _grade_bracket),
    AcceptanceCheck("AC2", "CAM program gouge-free", _grade_gouges),
    AcceptanceCheck("AC3", "machined fraction >= 0.90", _grade_machined),
    AcceptanceCheck("AC4", "verify_battery PASS (coherent geometry)", _grade_battery),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design an L-bracket and a CAM program to machine it from 6061 with zero gouges.",
    "deliverables": [{
        "id": "DL1", "kind": "cam_program", "name": "L-bracket + CAM program",
        "description": "an L-bracket in 6061 plus a gouge-free CAM program to machine it",
        "acceptance_criteria": [
            {"id": "AC1", "text": "L-bracket built",
             "check": {"tool": "measure_part", "pass_rule": "thickness > 0"}},
            {"id": "AC2", "text": "CAM program is gouge-free",
             "check": {"tool": "cam_simulate", "pass_rule": "gouges == 0"}},
            {"id": "AC3", "text": "machined fraction >= 0.90",
             "check": {"tool": "cam_simulate", "pass_rule": "machined_fraction >= 0.90"}},
            {"id": "AC4", "text": "geometry coherent",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
        ],
        "depends_on": [], "target_refs": {"doc_id": None, "features": []},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "material 6061-T6 aluminum", "basis": "requested material",
         "needs_confirm": False},
    ],
}


# ---- scripted agent turns (per seed) ----------------------------------------
def _base_turns() -> list[dict]:
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "bracket_thickness", "value": 6.0,
         "unit": "mm", "driver": "L-bracket web/flange thickness in 6061",
         "provenance": "measure_part: wall thickness of the L-bracket web",
         "tolerance": {"fit_class": None, "plus": 0.1, "minus": 0.1}, "status": "confirmed"},
        {"_kind": "decision", "id": "cam_sim", "stage": "verify", "for_ac": "AC2",
         "text": "3-axis CAM sim of the L-bracket program", "gouges": 0,
         "machined_fraction": 0.95,
         "rationale": "cam_simulate: 0 gouges, 95% of stock removed to the model",
         "node": "n_build_DL1"},
        {"_kind": "tool_call", "tool": "cam_simulate", "for_ac": "AC2",
         "node": "n_build_DL1", "args": {"program": "L_bracket_6061", "stock": "6061"},
         "hypothesis": "simulate toolpaths for gouges and coverage"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: incoherent_geometry via a CAM gouge — MUTATE in place so the first-matching
    # graders see it: cam_sim gouges=2 (AC2 FAIL) and the battery reports FAIL (AC4).
    gouge = [dict(t) for t in _base_turns()]
    for t in gouge:
        if t.get("id") == "cam_sim":
            t["gouges"] = 2
            t["rationale"] = "cam_simulate: 2 gouges into the model — incoherent toolpath"
        if t.get("kind") == "battery":
            t["verdict"] = "FAIL"
            t["note"] = "verify_battery FAIL: CAM gouges breach the model surface"

    # seed 3: tool_misuse — AC2 checked with measure_part (manifest AC2 tool is
    # cam_simulate). cam_sim stays clean so grading PASSes; only tool_misuse fires.
    misuse = [dict(t) for t in _base_turns()]
    for t in misuse:
        if t.get("tool") == "cam_simulate" and t.get("for_ac") == "AC2":
            t["tool"] = "measure_part"   # wrong tool for a CAM gouge check

    return {
        1: {"DL1": clean},
        2: {"DL1": gouge},
        3: {"DL1": misuse},
    }


CHALLENGE = Challenge(
    id="C5", name="bracket + CAM program", lane="cam",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes="L-bracket + zero-gouge CAM; seed2=incoherent_geometry(gouge), seed3=tool_misuse.",
)
