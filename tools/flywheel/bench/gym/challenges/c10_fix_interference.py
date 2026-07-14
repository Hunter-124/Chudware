"""C10 — find-and-fix-interference: planted-clash assembly repair.

Fixture: two-body assembly with a planted clash (offset boss into mating part).
Agent must detect via check_interference, then mutate to clear, then re-verify.

Ground-truth ACs the gym grades ITSELF:
  AC1  initial interference found: artifact/decision with clashes ≥ 1
  AC2  final interference artifact clashes == 0 (latest wins)
  AC3  battery PASS
  AC4  check_interference tool_call appears ≥ 1
  AC5  a mutating edit tool_call seq > first check_interference seq (repair after detect)

Seeds:
  1  clean PASS (detect → repair → clean recheck → battery)
  2  incoherent_geometry — final still clashing, battery FAIL
  3  grind — repeated identical check_interference without repair change
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_artifact,
               ledger_decision_value, coerce_number)

_MUTATE_TOOLS = frozenset({
    "add_features", "delete_feature", "edit_feature", "boolean",
    "extrude", "revolve", "hole", "fillet", "chamfer", "pattern", "shell",
    "asm_mate", "move_component", "offset_feature",
})


def _tool_calls(ledger: dict) -> list[tuple[int, str, dict]]:
    out: list[tuple[int, str, dict]] = []
    for e in ledger.get("events", []) or []:
        p = e.get("payload") or {}
        if e.get("kind") == "tool_call":
            tool = str(p.get("tool") or "")
            if tool:
                out.append((int(e.get("seq") or 0), tool, p))
        elif e.get("kind") == "agent_turn" and p.get("event") == "tool_call":
            tool = str(p.get("tool") or "")
            if tool:
                out.append((int(e.get("seq") or 0), tool, p))
    return out


def _interference_artifacts(ledger: dict) -> list[dict]:
    arts = []
    for e in ledger.get("events", []) or []:
        if e.get("kind") != "artifact":
            continue
        p = e.get("payload") or {}
        if p.get("kind") == "interference":
            arts.append(p)
    return arts


# ---- ground-truth graders ---------------------------------------------------

def _grade_initial_clash(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """AC1: evidence of initial clashes ≥ 1 (artifact or decision)."""
    found = ledger_decision_value(ledger, "interference_found", "clashes")
    found_n = coerce_number(found)
    if found_n is not None and found_n >= 1:
        return "PASS", f"interference_found.clashes={found_n}"
    for art in _interference_artifacts(ledger):
        c = coerce_number(art.get("clashes"))
        if c is not None and c >= 1:
            return "PASS", f"interference artifact clashes={c} (≥1 detected)"
        ic = coerce_number(art.get("initial_clashes"))
        if ic is not None and ic >= 1:
            return "PASS", f"interference.initial_clashes={ic}"
    return "FAIL", "no initial clash evidence (decision interference_found or artifact clashes≥1)"


def _grade_final_clean(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """AC2: latest interference artifact has clashes == 0."""
    arts = _interference_artifacts(ledger)
    if not arts:
        return "FAIL", "no interference artifact recorded"
    final = arts[-1]  # latest wins (ledger append order)
    clashes = final.get("clashes")
    c_n = coerce_number(clashes)
    if c_n is None:
        return "FAIL", f"final interference clashes {clashes!r} not numeric"
    if c_n != 0:
        return "FAIL", f"final interference reports {c_n} clashes (expected 0)"
    return "PASS", "final interference clean (0 clashes)"


def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


def _grade_check_called(ledger: dict, ctx: dict) -> "tuple[str,str]":
    n = sum(1 for _s, tool, _p in _tool_calls(ledger) if tool == "check_interference")
    if n < 1:
        return "FAIL", "check_interference never called"
    return "PASS", f"check_interference called {n}×"


def _grade_repair_after_detect(ledger: dict, ctx: dict) -> "tuple[str,str]":
    calls = _tool_calls(ledger)
    first_check_seq = None
    for seq, tool, _p in calls:
        if tool == "check_interference":
            first_check_seq = seq
            break
    if first_check_seq is None:
        return "FAIL", "no check_interference to anchor repair-after-detect"
    for seq, tool, _p in calls:
        if tool in _MUTATE_TOOLS and seq > first_check_seq:
            return "PASS", f"mutate {tool}@seq={seq} after check@seq={first_check_seq}"
    return "FAIL", "no mutating edit tool_call after first check_interference"


CHECKS = [
    AcceptanceCheck("AC1", "initial clash detected (clashes ≥ 1)", _grade_initial_clash),
    AcceptanceCheck("AC2", "final interference clean (clashes == 0)", _grade_final_clean),
    AcceptanceCheck("AC3", "verify_battery PASS (coherent geometry)", _grade_battery),
    AcceptanceCheck("AC4", "check_interference called ≥ 1", _grade_check_called),
    AcceptanceCheck("AC5", "repair mutate after first check_interference",
                    _grade_repair_after_detect),
]


_REQUEST = (
    "An assembly of two bodies ships with a planted clash: an offset boss collides with "
    "the mating part. Load the fixture, run check_interference to find the clash, repair "
    "the geometry (shorten/offset the boss or move the mate), re-check interference until "
    "clashes==0, and verify_battery PASS."
)

MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim": _REQUEST,
    "deliverables": [{
        "id": "DL1", "kind": "assembly", "name": "planted-clash assembly",
        "description": "two-body assembly with planted boss clash; repair until clean",
        "acceptance_criteria": [
            {"id": "AC1", "text": "initial clashes ≥ 1 detected",
             "check": {"tool": "check_interference", "pass_rule": "clashes>=1 initial"}},
            {"id": "AC2", "text": "final interference clean",
             "check": {"tool": "check_interference", "pass_rule": "clashes==0"}},
            {"id": "AC3", "text": "geometry coherent",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
            {"id": "AC4", "text": "check_interference used",
             "check": {"tool": "check_interference", "pass_rule": "called>=1"}},
            {"id": "AC5", "text": "repair after detect",
             "check": {"tool": "edit_feature", "pass_rule": "mutate seq > first check"}},
        ],
        "depends_on": [],
        "target_refs": {"doc_id": "doc_C10_clash", "features": ["boss_offset"]},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "fixture loads with boss offset into mating face (planted clash)",
         "basis": "challenge fixture", "needs_confirm": False},
    ],
}


# ---- scripted agent turns (per seed) ----------------------------------------

def _base_turns() -> list[dict]:
    return [
        {"_kind": "tool_call", "tool": "describe_assembly", "for_ac": "AC1",
         "node": "n_spec_DL1", "args": {},
         "hypothesis": "read the planted-clash assembly layout"},
        {"_kind": "tool_call", "tool": "check_interference", "for_ac": "AC1",
         "node": "n_verify_DL1", "args": {},
         "hypothesis": "detect the planted boss clash"},
        {"_kind": "decision", "id": "interference_found", "stage": "verify", "for_ac": "AC1",
         "text": "planted boss clashes with mating face", "clashes": 1,
         "rationale": "check_interference: 1 clash, boss into mating part",
         "node": "n_verify_DL1"},
        {"_kind": "artifact", "kind": "interference", "id": "interf_initial",
         "clashes": 1, "stage": "verify",
         "note": "initial planted clash detected"},
        # Repair after detect.
        {"_kind": "tool_call", "tool": "edit_feature", "for_ac": "AC5",
         "node": "n_build_DL1",
         "args": {"feature": "boss_offset", "offset_mm": -2.0},
         "hypothesis": "shorten boss to clear the mating face"},
        {"_kind": "tool_call", "tool": "check_interference", "for_ac": "AC2",
         "node": "n_verify_DL1", "args": {},
         "hypothesis": "re-check after repair"},
        {"_kind": "artifact", "kind": "interference", "id": "interf_final",
         "clashes": 0, "stage": "verify",
         "note": "post-repair assembly clean"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean after interference repair"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: incoherent_geometry — final still clashing, battery FAIL.
    incoherent = [dict(t) for t in _base_turns()]
    for t in incoherent:
        if t.get("id") == "interf_final":
            t["clashes"] = 1
            t["note"] = "post-repair still clashing (under-cut boss)"
        if t.get("kind") == "battery":
            t["verdict"] = "FAIL"
            t["note"] = "verify_battery FAIL: residual interference after attempted repair"

    # seed 3: grind — three identical check_interference after detect, no meaningful repair.
    grind = [dict(t) for t in _base_turns()]
    for _ in range(3):
        grind.append({
            "_kind": "tool_call", "tool": "check_interference", "for_ac": "AC2",
            "node": "n_verify_DL1", "args": {},
            "hypothesis": "recheck interference unchanged",
        })

    return {
        1: {"DL1": clean},
        2: {"DL1": incoherent},
        3: {"DL1": grind},
    }


def _challenge(**kwargs) -> Challenge:
    fields = getattr(Challenge, "__dataclass_fields__", {})
    extra = {k: v for k, v in {
        "divisions": ("local", "open"),
        "calibration": True,
        "eyes": "either",
    }.items() if k in fields}
    return Challenge(**kwargs, **extra)


CHALLENGE = _challenge(
    id="C10", name="find-and-fix interference", lane="assemblies-drawings",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes=("planted-clash assembly repair; seed2=incoherent_geometry (residual clash), "
           "seed3=grind (repeated check_interference)."),
)
