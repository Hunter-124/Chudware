"""C7 — shaft/housing assembly: custom-part running fit + retaining groove.

ALL-CUSTOM multi-part assembly (no catalog/imported parts): a shaft that must run in a
housing bore with a controlled RUNNING clearance fit, plus a retaining feature (a snap-
ring groove sized for a standard DIN 471 d12 external retaining ring) that axially locks
the shaft against the housing shoulder. Both parts are built from scratch by the model
and must be placed into a TRUE assembly (new_assembly/asm_add_component/asm_mate) that
solves clean.

Ground-truth ACs the gym grades ITSELF:
  AC1  shaft OD == 12.0 mm with an h6 shaft tolerance (running-fit basis)   — dimension
  AC2  housing bore == 12.0 mm with an H7 hole tolerance (running-fit basis) — dimension
  AC3  running clearance fit in [0.006, 0.044] mm (H7/h6)                  — decision
       shaft_fit.clearance in band
  AC4  retaining groove sized for a DIN 471 d12 ring: groove diameter == 11.5 mm and
       groove width == 1.1 mm                                              — decision
       retaining_ring.groove_d / .width
  AC5  assembly solved + interference clean (0 clashes)                    — artifact
       interference.clashes == 0

The scripted agent turns (seeds 1..3) exercise: a clean pass (seed 1), an
incoherent_geometry variant (seed 2 — the fit is machined too tight, clearance collapses
to 0.0 mm and the solved assembly reports an interference clash), and a tool_misuse
variant (seed 3 — AC5 is checked with measure_part instead of check_interference).
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value)

SHAFT_OD = 12.0                 # mm
HOUSING_BORE = 12.0             # mm
RUNNING_CLEARANCE_BAND = (0.006, 0.044)   # mm, H7/h6 running clearance band
GROOVE_D = 11.5                 # mm, DIN 471 d12 retaining-ring groove diameter
GROOVE_W = 1.1                  # mm, DIN 471 d12 retaining-ring groove width


# ---- ground-truth graders ---------------------------------------------------

def _grade_shaft_od(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dim = ledger_dimension(ledger, "shaft_od") or ledger_dimension(ledger, "shaft")
    if not dim:
        return "FAIL", "no shaft OD dimension recorded in ledger"
    if abs(float(dim.get("value", 0)) - SHAFT_OD) > 1e-6:
        return "FAIL", f"shaft OD {dim.get('value')} != {SHAFT_OD}"
    tol = dim.get("tolerance") or {}
    fit = str(tol.get("fit_class") or tol.get("class") or "").upper()
    if "H6" not in fit:
        return "FAIL", f"shaft tolerance is {fit or 'none'}, expected h6 (running-fit basis)"
    return "PASS", f"shaft d{SHAFT_OD} {fit}"


def _grade_housing_bore(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dim = ledger_dimension(ledger, "housing_bore") or ledger_dimension(ledger, "bore")
    if not dim:
        return "FAIL", "no housing bore dimension recorded in ledger"
    if abs(float(dim.get("value", 0)) - HOUSING_BORE) > 1e-6:
        return "FAIL", f"housing bore {dim.get('value')} != {HOUSING_BORE}"
    tol = dim.get("tolerance") or {}
    fit = str(tol.get("fit_class") or tol.get("class") or "").upper()
    if "H7" not in fit:
        return "FAIL", f"housing tolerance is {fit or 'none'}, expected H7 (running-fit basis)"
    return "PASS", f"housing bore d{HOUSING_BORE} {fit}"


def _grade_running_fit(ledger: dict, ctx: dict) -> "tuple[str,str]":
    clr = ledger_decision_value(ledger, "shaft_fit", "clearance")
    if clr is None:
        return "FAIL", "no shaft_fit decision with a clearance value"
    lo, hi = RUNNING_CLEARANCE_BAND
    if not (lo <= float(clr) <= hi):
        return "FAIL", f"clearance {clr} outside running band {RUNNING_CLEARANCE_BAND}"
    return "PASS", f"running-fit clearance {clr} mm in band"


def _grade_retaining_groove(ledger: dict, ctx: dict) -> "tuple[str,str]":
    gd = ledger_decision_value(ledger, "retaining_ring", "groove_d")
    gw = ledger_decision_value(ledger, "retaining_ring", "width")
    if gd is None or gw is None:
        return "FAIL", f"retaining_ring missing groove_d ({gd}) or width ({gw})"
    if abs(float(gd) - GROOVE_D) > 1e-6:
        return "FAIL", f"groove diameter {gd} != {GROOVE_D} (DIN 471 d12)"
    if abs(float(gw) - GROOVE_W) > 1e-6:
        return "FAIL", f"groove width {gw} != {GROOVE_W} (DIN 471 d12)"
    return "PASS", f"retaining groove d{GROOVE_D} w{GROOVE_W} (DIN 471 d12)"


def _grade_interference(ledger: dict, ctx: dict) -> "tuple[str,str]":
    art = ledger_artifact(ledger, "interference")
    if not art:
        return "FAIL", "no interference artifact recorded"
    clashes = art.get("clashes")
    if clashes != 0:
        return "FAIL", f"interference reports {clashes} clashes"
    return "PASS", "assembly interference clean (0 clashes)"


CHECKS = [
    AcceptanceCheck("AC1", "shaft OD d12 h6 (running-fit basis) recorded", _grade_shaft_od),
    AcceptanceCheck("AC2", "housing bore d12 H7 (running-fit basis) recorded", _grade_housing_bore),
    AcceptanceCheck("AC3", "running clearance fit in [0.006,0.044] mm", _grade_running_fit),
    AcceptanceCheck("AC4", "retaining groove d11.5 w1.1 (DIN 471 d12 ring)", _grade_retaining_groove),
    AcceptanceCheck("AC5", "assembly solved + interference clean (0 clashes)", _grade_interference),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a two-part shaft-and-housing assembly: a custom shaft (d12 h6) that runs "
        "in a custom housing bore (d12 H7) with a controlled running clearance fit, plus "
        "a snap-ring groove on the shaft sized for a standard DIN 471 d12 external "
        "retaining ring that axially locates the shaft against the housing shoulder. "
        "Place both parts into a real assembly (mates), solve it, and confirm no "
        "interference.",
    "deliverables": [
        {
            "id": "DL1", "kind": "part", "name": "shaft",
            "description": "d12 h6 shaft with a DIN 471 d12 retaining-ring groove",
            "acceptance_criteria": [
                {"id": "AC1", "text": "shaft OD is d12 h6 (running-fit basis)",
                 "check": {"tool": "fit_report", "params_hint": {"nominal": 12, "shaft": "h6"},
                           "pass_rule": "shaft==12 and tol==h6"}},
                {"id": "AC4", "text": "retaining groove sized for DIN 471 d12 ring",
                 "check": {"tool": "measure_part",
                           "pass_rule": "groove_d==11.5 and groove_w==1.1"}},
            ],
            "depends_on": [], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL2", "kind": "part", "name": "housing",
            "description": "d12 H7 bore housing with a locating shoulder",
            "acceptance_criteria": [
                {"id": "AC2", "text": "housing bore is d12 H7 (running-fit basis)",
                 "check": {"tool": "fit_report", "params_hint": {"nominal": 12, "hole": "H7"},
                           "pass_rule": "bore==12 and tol==H7"}},
            ],
            "depends_on": [], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL3", "kind": "assembly", "name": "shaft-housing assembly",
            "description": "shaft mated concentric+coincident into the housing bore",
            "acceptance_criteria": [
                {"id": "AC3", "text": "running clearance fit in band",
                 "check": {"tool": "fit_report", "pass_rule": "clearance in [0.006,0.044]"}},
                {"id": "AC5", "text": "assembly interference clean",
                 "check": {"tool": "check_interference", "pass_rule": "clashes==0"}},
            ],
            "depends_on": ["DL1", "DL2"], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
    ],
    "ambiguities": [],
    "assumptions": [
        {"text": "material 1045 steel shaft, 6061-T6 housing (default machined pair)",
         "basis": "default machined", "needs_confirm": False},
    ],
}


# ---- scripted agent turns (per seed) ----------------------------------------
def _base_turns() -> list[dict]:
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "shaft_od_diameter", "value": SHAFT_OD,
         "unit": "mm", "driver": "spec: running fit d12 in housing bore",
         "provenance": "fit_report: h6 shaft for a d12 H7/h6 running fit",
         "tolerance": {"fit_class": "h6", "plus": 0.0, "minus": 0.011}, "status": "confirmed"},
        {"_kind": "dimension", "id": "DIM2", "name": "housing_bore_diameter", "value": HOUSING_BORE,
         "unit": "mm", "driver": "spec: running fit d12 for the shaft",
         "provenance": "fit_report: H7 bore for a d12 H7/h6 running fit",
         "tolerance": {"fit_class": "H7", "plus": 0.018, "minus": 0.0}, "status": "confirmed"},
        {"_kind": "decision", "id": "shaft_fit", "stage": "specify", "for_ac": "AC3",
         "text": "d12 H7/h6 running fit between shaft and housing bore", "clearance": 0.025,
         "rationale": "fit_report: H7/h6 mean clearance 0.025 mm (running)",
         "node": "n_spec_DL3"},
        {"_kind": "tool_call", "tool": "fit_report", "for_ac": "AC3", "node": "n_spec_DL3",
         "args": {"nominal": 12, "hole": "H7", "shaft": "h6"},
         "hypothesis": "verify running clearance"},
        {"_kind": "decision", "id": "retaining_ring", "stage": "plan", "for_ac": "AC4",
         "text": "DIN 471 d12 external retaining ring groove on the shaft",
         "groove_d": GROOVE_D, "width": GROOVE_W,
         "rationale": "DIN 471 d12 ring spec: groove d11.5 -0/-0.11, width 1.1 +0.14/0",
         "node": "n_plan_DL1"},
        {"_kind": "tool_call", "tool": "new_assembly", "for_ac": "AC5", "node": "n_build_DL3",
         "args": {"name": "shaft_housing_asm"}, "hypothesis": "create the assembly doc"},
        {"_kind": "tool_call", "tool": "asm_add_component", "for_ac": "AC5", "node": "n_build_DL3",
         "args": {"path": "housing.step", "fixed": True},
         "hypothesis": "ground the housing"},
        {"_kind": "tool_call", "tool": "asm_add_component", "for_ac": "AC5", "node": "n_build_DL3",
         "args": {"path": "shaft.step"}, "hypothesis": "add the free shaft component"},
        {"_kind": "tool_call", "tool": "asm_mate", "for_ac": "AC5", "node": "n_build_DL3",
         "args": {"mate_type": "concentric", "a": {"component": "housing", "query": {"of": "face", "name": "bore"}},
                   "b": {"component": "shaft", "query": {"of": "face", "name": "od"}}},
         "hypothesis": "seat the shaft concentric in the bore"},
        {"_kind": "tool_call", "tool": "asm_mate", "for_ac": "AC5", "node": "n_build_DL3",
         "args": {"mate_type": "coincident", "a": {"component": "housing", "query": {"of": "face", "side": "shoulder"}},
                   "b": {"component": "shaft", "query": {"of": "face", "side": "shoulder"}}},
         "hypothesis": "axially locate the shaft against the housing shoulder"},
        {"_kind": "tool_call", "tool": "check_interference", "for_ac": "AC5", "node": "n_verify_DL3",
         "args": {}, "hypothesis": "confirm no clashes after solving the mates"},
        {"_kind": "artifact", "kind": "interference", "id": "interf_DL3", "clashes": 0,
         "stage": "verify", "note": "solved assembly clean, min clearance 0.025 mm on the bore"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL3", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: incoherent_geometry — the fit is machined too tight (clearance collapses to
    # 0.0, outside the running band) and the solved assembly reports a real clash.
    incoherent = [dict(t) for t in _base_turns()]
    for t in incoherent:
        if t.get("id") == "shaft_fit":
            t["clearance"] = 0.0
            t["rationale"] = "fit_report: bore machined undersize, clearance collapsed to 0.0 mm"
        if t.get("kind") == "interference":
            t["clashes"] = 1
            t["note"] = "solved assembly interferes: shaft OD clashes the tight housing bore"
        if t.get("kind") == "battery":
            t["verdict"] = "FAIL"
            t["note"] = "verify_battery FAIL: shaft/housing interference"

    # seed 3: tool_misuse — AC5 checked with measure_part instead of check_interference
    # (manifest AC5 check tool is check_interference).
    misuse = [dict(t) for t in _base_turns()]
    for t in misuse:
        if t.get("tool") == "check_interference" and t.get("for_ac") == "AC5":
            t["tool"] = "measure_part"   # wrong tool for the interference check

    return {
        1: {"DL1": clean, "DL2": [], "DL3": []},
        2: {"DL1": incoherent, "DL2": [], "DL3": []},
        3: {"DL1": misuse, "DL2": [], "DL3": []},
    }


CHALLENGE = Challenge(
    id="C7", name="shaft-housing fit + retaining groove", lane="assemblies-drawings",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes=("all-custom 2-part assembly: running fit + DIN471 retaining groove; "
           "seed2=incoherent_geometry, seed3=tool_misuse."),
)
