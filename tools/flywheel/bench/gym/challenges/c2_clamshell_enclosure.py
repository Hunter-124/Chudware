"""C2 — clamshell enclosure: snap-fit lid, PETG.

A two-part clamshell enclosure for a 60x40x15 mm PCB with a snap-fit lid, printed
in PETG. The enclosure must clear the PCB, use a PETG-safe wall thickness, and the
snap must engage within PETG's allowable strain.

Ground-truth ACs the gym grades ITSELF:
  AC1  PCB clearance >= 0.5 mm            — decision pcb_clearance.clearance_mm >= 0.5
  AC2  wall thickness >= 1.5 mm (PETG)    — dimension "wall" value >= 1.5
  AC3  snap engages within PETG strain    — decision snap_fit.strain <= 0.04 & engaged
  AC4  verify_battery PASS                — battery artifact verdict PASS

The scripted agent turns (seeds 1..3) exercise: a clean pass (seed 1), an
advisory_ignored variant (seed 2 proceeds past an advisor PETG-snap warning without
replan/dispute), and an abstraction_skip variant (seed 3 emits a ladder_bounce with
geometry before the function_map checklist). Grading stays PASS on all three; the
extra turns are LABELED classifier signal, not graded-as-model.
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value)

PCB_CLEARANCE_MIN = 0.5     # mm
WALL_MIN = 1.5              # mm, PETG minimum printable/structural wall
PETG_STRAIN_MAX = 0.04     # ~4% allowable strain for a PETG snap


# ---- ground-truth graders ---------------------------------------------------

def _grade_pcb_clearance(ledger: dict, ctx: dict) -> "tuple[str,str]":
    clr = ledger_decision_value(ledger, "pcb_clearance", "clearance_mm")
    if clr is None:
        return "FAIL", "no pcb_clearance decision with a clearance_mm value"
    if float(clr) < PCB_CLEARANCE_MIN:
        return "FAIL", f"PCB clearance {clr} mm < {PCB_CLEARANCE_MIN} mm"
    return "PASS", f"PCB clearance {clr} mm >= {PCB_CLEARANCE_MIN} mm"


def _grade_wall(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dim = ledger_dimension(ledger, "wall")
    if not dim:
        return "FAIL", "no wall dimension recorded in ledger"
    if float(dim.get("value", 0)) < WALL_MIN:
        return "FAIL", f"wall {dim.get('value')} mm < PETG min {WALL_MIN} mm"
    return "PASS", f"wall {dim.get('value')} mm >= {WALL_MIN} mm"


def _grade_snap(ledger: dict, ctx: dict) -> "tuple[str,str]":
    strain = ledger_decision_value(ledger, "snap_fit", "strain")
    engaged = ledger_decision_value(ledger, "snap_fit", "engaged")
    if strain is None:
        return "FAIL", "no snap_fit decision with a strain value"
    if float(strain) > PETG_STRAIN_MAX:
        return "FAIL", f"snap strain {strain} > PETG allowable {PETG_STRAIN_MAX}"
    if engaged is not True:
        return "FAIL", f"snap not engaged (engaged={engaged})"
    return "PASS", f"snap strain {strain} <= {PETG_STRAIN_MAX} and engaged"


def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


CHECKS = [
    AcceptanceCheck("AC1", "PCB clearance >= 0.5 mm", _grade_pcb_clearance),
    AcceptanceCheck("AC2", "wall thickness >= 1.5 mm (PETG min)", _grade_wall),
    AcceptanceCheck("AC3", "snap engages within PETG allowable strain", _grade_snap),
    AcceptanceCheck("AC4", "verify_battery PASS (coherent geometry)", _grade_battery),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a two-part clamshell enclosure for a 60x40x15 mm PCB with a snap-fit "
        "lid, printed in PETG.",
    "deliverables": [{
        "id": "DL1", "kind": "assembly", "name": "clamshell enclosure",
        "description": "two-part clamshell (base + snap-fit lid) for a 60x40x15 PCB in PETG",
        "acceptance_criteria": [
            {"id": "AC1", "text": "PCB has >= 0.5 mm clearance to walls",
             "check": {"tool": "measure_part", "pass_rule": "clearance_mm >= 0.5"}},
            {"id": "AC2", "text": "wall thickness >= 1.5 mm for PETG",
             "check": {"tool": "wall_thickness", "pass_rule": "wall >= 1.5"}},
            {"id": "AC3", "text": "snap engages within PETG allowable strain",
             "check": {"tool": "run_fea_static", "pass_rule": "strain <= 0.04 and engaged"}},
            {"id": "AC4", "text": "geometry coherent",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
        ],
        "depends_on": [], "target_refs": {"doc_id": None, "features": []},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "PETG print (FDM), 0.4 mm nozzle", "basis": "requested material",
         "needs_confirm": False},
    ],
}


# ---- scripted agent turns (per seed) ----------------------------------------
def _base_turns() -> list[dict]:
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "wall_thickness", "value": 2.0,
         "unit": "mm", "driver": "PETG structural min + margin",
         "provenance": "wall_thickness check on base+lid shells",
         "tolerance": {"fit_class": None, "plus": 0.2, "minus": 0.0}, "status": "confirmed"},
        {"_kind": "dimension", "id": "DIM2", "name": "cavity_length", "value": 61.0,
         "unit": "mm", "driver": "PCB 60 + 2x0.5 clearance",
         "provenance": "measure_part: internal cavity vs PCB envelope",
         "tolerance": {"fit_class": None, "plus": 0.2, "minus": 0.0}, "status": "confirmed"},
        {"_kind": "decision", "id": "pcb_clearance", "stage": "specify", "for_ac": "AC1",
         "text": "0.5 mm clearance all around the 60x40x15 PCB", "clearance_mm": 0.5,
         "rationale": "measure_part: min gap cavity-wall to PCB edge = 0.5 mm",
         "node": "n_spec_DL1"},
        {"_kind": "decision", "id": "snap_fit", "stage": "specify", "for_ac": "AC3",
         "text": "cantilever snap on lid engages base lip", "strain": 0.032,
         "engaged": True,
         "rationale": "run_fea_static: peak snap strain 3.2% < PETG 4% allowable",
         "node": "n_spec_DL1"},
        {"_kind": "tool_call", "tool": "run_fea_static", "for_ac": "AC3",
         "node": "n_spec_DL1", "args": {"feature": "snap_cantilever", "material": "PETG"},
         "hypothesis": "verify snap strain under engagement deflection"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: advisory_ignored — the advisor warns PETG snap is marginal, agent proceeds
    # WITHOUT a replan or dispute. Grading still PASS (geometry unchanged).
    ignored = [dict(t) for t in _base_turns()]
    ignored.append({"_kind": "advisory", "id": "ADV1", "topic": "petg_snap_marginal",
                    "severity": "warn", "node": "n_spec_DL1",
                    "warning": "PETG snaps are marginal; the cantilever strain is near "
                               "the 4% allowable and PETG is notch-sensitive."})
    ignored.append({"_kind": "decision", "id": "proceed_snap", "stage": "specify",
                    "topic": "petg_snap_marginal", "action": "proceed",
                    "text": "proceed with the PETG snap as designed",
                    "rationale": "strain within nominal allowable; ship as-is",
                    "node": "n_spec_DL1"})

    # seed 3: abstraction_skip — a ladder_bounce: geometry_plan emitted before the
    # function_map omissions checklist (skipped an abstraction rung).
    skip = [dict(t) for t in _base_turns()]
    skip.append({"_kind": "artifact", "kind": "ladder_bounce", "id": "bounce_DL1",
                 "stage": "ladder_geometry", "bounce": True, "node": "n_spec_DL1",
                 "finding": "geometry_plan emitted before function_map omissions checklist"})

    return {
        1: {"DL1": clean},
        2: {"DL1": ignored},
        3: {"DL1": skip},
    }


CHALLENGE = Challenge(
    id="C2", name="clamshell enclosure", lane="modeling-core",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes="snap-fit PETG clamshell; seed2=advisory_ignored, seed3=abstraction_skip.",
)
