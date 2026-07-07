"""C8 — motor bracket + catalog fasteners: custom parts mated via standard hardware.

CUSTOM PARTS + STANDARD CATALOG PARTS: a custom bracket and a custom motor-plate stand-in
(representing a small motor's mounting face) that register to each other on a NEMA-11
style pattern (d8 h7 pilot boss / H7 bore + a 4x M3 hole pattern on a 21 mm square), then
FOUR STANDARD M3 socket-head cap screws (``make_standard_part``) must be sized and placed
to actually clamp the stack — long enough to clear the bracket thickness plus a full
thread-diameter-and-a-half of engagement into the motor-plate's tapped holes, but not so
long they bottom out or protrude through and clash the assembly.

Ground-truth ACs the gym grades ITSELF:
  AC1  pilot register fit: bracket bore d8 H7 / motor-plate boss d8 h7, clearance in
       [0.005, 0.035] mm                                                   — dimensions +
       decision pilot_fit.clearance in band
  AC2  bolt pattern match: bracket has 4x d3.4 (M3 clearance) holes on a 21 mm square
       pattern, motor-plate has 4x M3 tapped holes on the SAME 21 mm square pattern
                                                                             — decision
       bolt_pattern.holes/.hole_d/.pattern_mm (bracket) matches
       bolt_pattern_mate.pattern_mm (motor-plate)
  AC3  standard screw correctly specified: thread M3, length >= bracket thickness (6 mm)
       + 1.5x diameter (4.5 mm) = 10.5 mm engagement minimum, catalog part sourced via
       make_standard_part                                                  — decision
       screw_spec.thread == "M3" and length_mm >= 10.5, artifact standard_part.family
       contains "screw"/"bolt"
  AC4  assembly solved + interference clean, excluding the intentional screw/tapped-hole
       thread engagement                                                   — artifact
       interference.clashes == 0
  AC5  verify_battery PASS

The scripted agent turns (seeds 1..3) exercise: a clean pass (seed 1), an
incoherent_geometry variant (seed 2 — the screws are specified too short, under-engaging
the tapped holes, and the battery reports FAIL), and a context_loss variant (seed 3 — a
build-stage decision cites a nonexistent DIM referencing the pattern).
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value)

PILOT_NOM = 8.0                        # mm, NEMA-11-style pilot register
PILOT_CLEARANCE_BAND = (0.005, 0.035)  # mm, H7/h6 register clearance band
PATTERN_HOLES = 4
CLEARANCE_HOLE_D = 3.4                 # mm, M3 clearance
PATTERN_MM = 21.0                      # mm, square bolt-pattern side
SCREW_THREAD = "M3"
BRACKET_THK = 6.0                      # mm
MIN_ENGAGEMENT = 4.5                   # mm, 1.5x M3 diameter
MIN_SCREW_LEN = BRACKET_THK + MIN_ENGAGEMENT   # 10.5 mm


# ---- ground-truth graders ---------------------------------------------------

def _grade_pilot_fit(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bore = ledger_dimension(ledger, "pilot_bore")
    boss = ledger_dimension(ledger, "pilot_boss")
    if not bore or not boss:
        return "FAIL", f"missing pilot dimension (bore={bore}, boss={boss})"
    if abs(float(bore.get("value", 0)) - PILOT_NOM) > 1e-6:
        return "FAIL", f"pilot bore {bore.get('value')} != {PILOT_NOM}"
    if abs(float(boss.get("value", 0)) - PILOT_NOM) > 1e-6:
        return "FAIL", f"pilot boss {boss.get('value')} != {PILOT_NOM}"
    clr = ledger_decision_value(ledger, "pilot_fit", "clearance")
    if clr is None:
        return "FAIL", "no pilot_fit decision with a clearance value"
    lo, hi = PILOT_CLEARANCE_BAND
    if not (lo <= float(clr) <= hi):
        return "FAIL", f"pilot clearance {clr} outside band {PILOT_CLEARANCE_BAND}"
    return "PASS", f"pilot register d{PILOT_NOM} clearance {clr} mm in band"


def _grade_bolt_pattern(ledger: dict, ctx: dict) -> "tuple[str,str]":
    holes = ledger_decision_value(ledger, "bolt_pattern", "holes")
    hole_d = ledger_decision_value(ledger, "bolt_pattern", "hole_d")
    pattern = ledger_decision_value(ledger, "bolt_pattern", "pattern_mm")
    mate_pattern = ledger_decision_value(ledger, "bolt_pattern_mate", "pattern_mm")
    if holes != PATTERN_HOLES:
        return "FAIL", f"bracket bolt pattern has {holes} holes, expected {PATTERN_HOLES}"
    if hole_d is None or abs(float(hole_d) - CLEARANCE_HOLE_D) > 1e-6:
        return "FAIL", f"clearance hole d{hole_d} != {CLEARANCE_HOLE_D} (M3 clearance)"
    if pattern is None or abs(float(pattern) - PATTERN_MM) > 1e-6:
        return "FAIL", f"bracket pattern {pattern} != {PATTERN_MM} mm square"
    if mate_pattern is None or abs(float(mate_pattern) - PATTERN_MM) > 1e-6:
        return "FAIL", f"motor-plate tapped pattern {mate_pattern} does not match bracket {PATTERN_MM}"
    return "PASS", f"{PATTERN_HOLES}xM3 clearance on {PATTERN_MM}mm square, matched to motor-plate tapped pattern"


def _grade_screw_spec(ledger: dict, ctx: dict) -> "tuple[str,str]":
    thread = ledger_decision_value(ledger, "screw_spec", "thread")
    length = ledger_decision_value(ledger, "screw_spec", "length_mm")
    if thread != SCREW_THREAD:
        return "FAIL", f"screw thread {thread} != {SCREW_THREAD}"
    if length is None or float(length) < MIN_SCREW_LEN:
        return "FAIL", f"screw length {length} mm < required min {MIN_SCREW_LEN} mm"
    part = ledger_artifact(ledger, "standard_part")
    fam = str((part or {}).get("family", "")).lower()
    if not part or ("screw" not in fam and "bolt" not in fam):
        return "FAIL", f"no standard_part (screw/bolt) artifact recorded (family={fam or 'none'})"
    return "PASS", f"{SCREW_THREAD} x {length} mm catalog screw, engagement adequate"


def _grade_interference(ledger: dict, ctx: dict) -> "tuple[str,str]":
    art = ledger_artifact(ledger, "interference")
    if not art:
        return "FAIL", "no interference artifact recorded"
    clashes = art.get("clashes")
    if clashes != 0:
        return "FAIL", f"interference reports {clashes} clashes"
    return "PASS", "assembly interference clean (0 clashes, thread engagement excluded)"


def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


CHECKS = [
    AcceptanceCheck("AC1", "pilot register d8 H7/h6 clearance in band", _grade_pilot_fit),
    AcceptanceCheck("AC2", "4xM3 clearance bolt pattern matches motor-plate tapped pattern",
                    _grade_bolt_pattern),
    AcceptanceCheck("AC3", "catalog M3 screw sized for adequate thread engagement",
                    _grade_screw_spec),
    AcceptanceCheck("AC4", "assembly interference clean (0 unintended clashes)",
                    _grade_interference),
    AcceptanceCheck("AC5", "verify_battery PASS (coherent geometry)", _grade_battery),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a motor-mount bracket assembly: a custom bracket that registers to a "
        "custom motor-plate stand-in on a d8 pilot (H7/h6) and a 4-hole M3 bolt pattern "
        "on a 21 mm square, then fasten the stack with four standard M3 socket-head cap "
        "screws sourced from the catalog, sized long enough to fully engage the "
        "motor-plate's tapped holes without bottoming out or protruding through. Confirm "
        "the pilot registers, the hole patterns line up, and the solved assembly has no "
        "unintended interference.",
    "deliverables": [
        {
            "id": "DL1", "kind": "part", "name": "bracket",
            "description": "d8 H7 pilot bore + 4x d3.4 clearance holes on a 21mm square",
            "acceptance_criteria": [
                {"id": "AC1", "text": "pilot bore is d8 H7 (register fit basis)",
                 "check": {"tool": "fit_report", "params_hint": {"nominal": 8, "hole": "H7"},
                           "pass_rule": "bore==8 and tol==H7"}},
                {"id": "AC2", "text": "4xM3 clearance holes on a 21mm square pattern",
                 "check": {"tool": "measure_part",
                           "pass_rule": "4 holes d3.4 on 21mm square"}},
            ],
            "depends_on": [], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL2", "kind": "part", "name": "motor-plate",
            "description": "d8 h6 pilot boss + 4x M3 tapped holes on a matching 21mm square",
            "acceptance_criteria": [
                {"id": "AC1", "text": "pilot boss is d8 h6 (register fit basis)",
                 "check": {"tool": "fit_report", "params_hint": {"nominal": 8, "shaft": "h6"},
                           "pass_rule": "boss==8 and tol==h6"}},
            ],
            "depends_on": [], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL3", "kind": "part", "name": "M3 SHCS x4",
            "description": "four standard M3 socket-head cap screws, catalog-sourced",
            "acceptance_criteria": [
                {"id": "AC3", "text": "M3 screw length adequate for thread engagement",
                 "check": {"tool": "make_standard_part",
                           "pass_rule": "thread==M3 and length_mm>=10.5"}},
            ],
            "depends_on": [], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL4", "kind": "assembly", "name": "motor bracket assembly",
            "description": "bracket + motor-plate + 4x M3 screws, mated and solved",
            "acceptance_criteria": [
                {"id": "AC4", "text": "assembly interference clean",
                 "check": {"tool": "check_interference", "pass_rule": "clashes==0"}},
                {"id": "AC5", "text": "geometry coherent",
                 "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
            ],
            "depends_on": ["DL1", "DL2", "DL3"], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
    ],
    "ambiguities": [],
    "assumptions": [
        {"text": "bracket material 6061-T6, 6 mm thick; motor-plate stand-in for a NEMA-11 "
                 "class motor face", "basis": "default machined bracket", "needs_confirm": False},
    ],
}


# ---- scripted agent turns (per seed) ----------------------------------------
def _base_turns() -> list[dict]:
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "pilot_bore_diameter", "value": PILOT_NOM,
         "unit": "mm", "driver": "spec: d8 register against the motor-plate boss",
         "provenance": "fit_report: H7 bore for a d8 H7/h6 register",
         "tolerance": {"fit_class": "H7", "plus": 0.015, "minus": 0.0}, "status": "confirmed"},
        {"_kind": "dimension", "id": "DIM2", "name": "pilot_boss_diameter", "value": PILOT_NOM,
         "unit": "mm", "driver": "spec: d8 register against the bracket bore",
         "provenance": "fit_report: h6 boss for a d8 H7/h6 register",
         "tolerance": {"fit_class": "h6", "plus": 0.0, "minus": 0.009}, "status": "confirmed"},
        {"_kind": "decision", "id": "pilot_fit", "stage": "specify", "for_ac": "AC1",
         "text": "d8 H7/h6 register between bracket bore and motor-plate boss",
         "clearance": 0.015,
         "rationale": "fit_report: H7/h6 mean clearance 0.015 mm (locational register)",
         "node": "n_spec_DL1"},
        {"_kind": "tool_call", "tool": "fit_report", "for_ac": "AC1", "node": "n_spec_DL1",
         "args": {"nominal": 8, "hole": "H7", "shaft": "h6"},
         "hypothesis": "verify register clearance"},
        {"_kind": "decision", "id": "bolt_pattern", "stage": "plan", "for_ac": "AC2",
         "text": "4xM3 clearance holes on a 21mm square, bracket side", "holes": PATTERN_HOLES,
         "hole_d": CLEARANCE_HOLE_D, "pattern_mm": PATTERN_MM,
         "rationale": "NEMA-11-style 21mm square pattern, M3 clearance d3.4",
         "node": "n_plan_DL1"},
        {"_kind": "decision", "id": "bolt_pattern_mate", "stage": "plan", "for_ac": "AC2",
         "text": "4xM3 tapped holes on the matching 21mm square, motor-plate side",
         "holes": PATTERN_HOLES, "pattern_mm": PATTERN_MM,
         "rationale": "motor-plate tapped pattern mirrors the bracket clearance pattern",
         "node": "n_plan_DL2"},
        {"_kind": "decision", "id": "screw_spec", "stage": "plan", "for_ac": "AC3",
         "text": "M3 socket-head cap screw sized for full thread engagement",
         "thread": SCREW_THREAD, "length_mm": 12.0,
         "rationale": f"bracket {BRACKET_THK}mm + {MIN_ENGAGEMENT}mm engagement = "
                       f"{MIN_SCREW_LEN}mm min -> M3x12 catalog stock length",
         "node": "n_plan_DL3"},
        {"_kind": "tool_call", "tool": "make_standard_part", "for_ac": "AC3", "node": "n_build_DL3",
         "args": {"family": "socket_head_cap_screw", "thread": SCREW_THREAD, "length_mm": 12.0},
         "hypothesis": "source the catalog M3x12 SHCS"},
        {"_kind": "artifact", "kind": "standard_part", "id": "std_DL3",
         "family": "socket_head_cap_screw", "thread": SCREW_THREAD, "length_mm": 12.0,
         "stage": "build", "note": "make_standard_part: M3x12 SHCS x4"},
        {"_kind": "tool_call", "tool": "new_assembly", "for_ac": "AC4", "node": "n_build_DL4",
         "args": {"name": "motor_bracket_asm"}, "hypothesis": "create the assembly doc"},
        {"_kind": "tool_call", "tool": "asm_add_component", "for_ac": "AC4", "node": "n_build_DL4",
         "args": {"path": "motor_plate.step", "fixed": True}, "hypothesis": "ground the motor-plate"},
        {"_kind": "tool_call", "tool": "asm_add_component", "for_ac": "AC4", "node": "n_build_DL4",
         "args": {"path": "bracket.step"}, "hypothesis": "add the free bracket component"},
        {"_kind": "tool_call", "tool": "asm_mate", "for_ac": "AC4", "node": "n_build_DL4",
         "args": {"mate_type": "concentric",
                   "a": {"component": "motor_plate", "query": {"of": "face", "name": "pilot_boss"}},
                   "b": {"component": "bracket", "query": {"of": "face", "name": "pilot_bore"}}},
         "hypothesis": "register the bracket concentric on the pilot boss"},
        {"_kind": "tool_call", "tool": "asm_mate", "for_ac": "AC4", "node": "n_build_DL4",
         "args": {"mate_type": "coincident",
                   "a": {"component": "motor_plate", "query": {"of": "face", "side": "top"}},
                   "b": {"component": "bracket", "query": {"of": "face", "side": "bottom"}}},
         "hypothesis": "seat the bracket flush against the motor-plate face"},
        {"_kind": "tool_call", "tool": "asm_add_component", "for_ac": "AC4", "node": "n_build_DL4",
         "args": {"path": "screw_m3x12.step", "count": 4}, "hypothesis": "add the 4 catalog screws"},
        {"_kind": "tool_call", "tool": "asm_mate", "for_ac": "AC4", "node": "n_build_DL4",
         "args": {"mate_type": "concentric",
                   "a": {"component": "bracket", "query": {"of": "face", "name": "clearance_hole"}},
                   "b": {"component": "screw_m3x12", "query": {"of": "face", "name": "shank"}}},
         "hypothesis": "align each screw through its clearance hole into the tapped hole"},
        {"_kind": "tool_call", "tool": "check_interference", "for_ac": "AC4", "node": "n_verify_DL4",
         "args": {"exclude": ["screw_m3x12:motor_plate"]},
         "hypothesis": "confirm no unintended clashes, excluding intentional thread engagement"},
        {"_kind": "artifact", "kind": "interference", "id": "interf_DL4", "clashes": 0,
         "stage": "verify", "note": "solved assembly clean; thread engagement pairs excluded by design"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL4", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: incoherent_geometry — screws spec'd too short (M3x6, under-engaging the
    # tapped holes): AC3 FAILs and the battery reports FAIL.
    incoherent = [dict(t) for t in _base_turns()]
    for t in incoherent:
        if t.get("id") == "screw_spec":
            t["length_mm"] = 6.0
            t["rationale"] = ("bracket 6mm + 0mm engagement = screw bottoms at the bracket "
                               "face, 0mm thread engagement in the motor-plate (< 4.5mm min)")
        if t.get("kind") == "standard_part":
            t["length_mm"] = 6.0
        if t.get("tool") == "make_standard_part":
            t["args"]["length_mm"] = 6.0
        if t.get("kind") == "battery":
            t["verdict"] = "FAIL"
            t["note"] = "verify_battery FAIL: screws under-engaged, stack not clamped"

    # seed 3: context_loss — a build-stage decision cites a nonexistent DIM referencing
    # the pattern (dangling reference the classifier should catch).
    ctxloss = [dict(t) for t in _base_turns()]
    ctxloss.append({"_kind": "decision", "id": "note1", "stage": "build",
                    "text": "clearance-hole placement follows DIM77 (pattern) + M3 clearance",
                    "rationale": "derive hole placement from DIM77", "node": "n_build_DL1"})

    return {
        1: {"DL1": clean, "DL2": [], "DL3": [], "DL4": []},
        2: {"DL1": incoherent, "DL2": [], "DL3": [], "DL4": []},
        3: {"DL1": ctxloss, "DL2": [], "DL3": [], "DL4": []},
    }


CHALLENGE = Challenge(
    id="C8", name="motor bracket + catalog fasteners", lane="assemblies-drawings",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes=("custom bracket + motor-plate mated on a pilot register + bolt pattern, "
           "fastened with catalog M3 SHCS; seed2=incoherent_geometry, seed3=context_loss."),
)
