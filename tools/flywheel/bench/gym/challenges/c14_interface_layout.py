"""C14 — interface / mating-face layout.

Two parts (bracket + cover) that must mate: 4×M4 bolt pattern, pilot register,
face flatness/clearance. Assembly must pass interface_check, solve_mates, and
zero interference; battery PASS on both parts.

Ground-truth ACs:
  AC1  decision bolt_pattern.pattern_mm == bolt_pattern_mate.pattern_mm
  AC2  artifact interface OR tool_call interface_check with pass
  AC3  interference clashes == 0
  AC4  solve_mates / mates tool_call ok (decision mates_ok=true)
  AC5  battery PASS both parts (bracket + cover)

Seeds: 1=clean PASS; 2=mismatched patterns + clash; 3=no interface_check / mates.
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_artifact, final_check_absence,
               final_checks, last_mutation_index, ledger_decision_value, coerce_number)

PATTERN_MM = 28.0
PATTERN_HOLES = 4
M4_CLEARANCE_D = 4.5
PILOT_NOM = 10.0


def _events(ledger: dict) -> list[dict]:
    return list(ledger.get("events", []) or [])


def _tool_call_pairs(ledger: dict) -> list[tuple[dict, dict]]:
    out: list[tuple[dict, dict]] = []
    for e in _events(ledger):
        p = e.get("payload") or {}
        if e.get("kind") == "tool_call":
            out.append((e, p))
        elif e.get("kind") == "agent_turn" and p.get("event") == "tool_call":
            out.append((e, p))
    return out


def _all_artifacts(ledger: dict, kind: str) -> list[dict]:
    """The final checks of *kind*: successful ones recorded after the last mutation."""
    return final_checks(ledger, kind)


def _grade_matching_pattern(ledger: dict, ctx: dict) -> "tuple[str,str]":
    a = ledger_decision_value(ledger, "bolt_pattern", "pattern_mm")
    b = ledger_decision_value(ledger, "bolt_pattern_mate", "pattern_mm")
    if a is None or b is None:
        return "FAIL", f"bolt_pattern.pattern_mm={a!r} bolt_pattern_mate.pattern_mm={b!r}"
    na, nb = coerce_number(a), coerce_number(b)
    if na is None or nb is None:
        return "FAIL", f"non-numeric pattern_mm a={a!r} b={b!r}"
    if abs(na - nb) > 1e-6:
        return "FAIL", f"pattern mismatch {na} != {nb}"
    return "PASS", f"matching bolt patterns pattern_mm={na}"


def _grade_interface_check(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """The LAST successful interface check after the last mutation decides: an interface
    artifact if one was recorded, else the last interface_check tool call."""
    for art in reversed(_all_artifacts(ledger, "interface")):
        verdict = str(art.get("verdict") or art.get("result") or art.get("status") or "")
        if verdict.upper() in {"PASS", "OK", "TRUE", "1"} or art.get("pass") is True:
            return "PASS", f"interface artifact pass ({art.get('id') or ''})"
        if art.get("pass") is False or verdict.upper() in {"FAIL", "FALSE"}:
            return "FAIL", f"interface artifact failed: {verdict or art}"
        if "pass" in art or verdict:
            return "PASS", f"interface artifact {art.get('id')}"
        # a bare artifact with no verdict decides nothing; look at an earlier one
    cut = last_mutation_index(ledger)
    calls = [p for i, e in enumerate(_events(ledger)) if i > cut
             for p in [e.get("payload") or {}]
             if (e.get("kind") == "tool_call"
                 or (e.get("kind") == "agent_turn" and p.get("event") == "tool_call"))
             and str(p.get("tool") or "") == "interface_check"
             and p.get("ok") is not False and not p.get("error")]
    if calls:
        result = calls[-1].get("result") or {}
        if isinstance(result, dict) and (result.get("pass") is False or str(
                result.get("verdict", "PASS")).upper() not in {"PASS", "OK"}):
            return "FAIL", f"final interface_check reported {result}"
        return "PASS", "final interface_check tool_call passed"
    return "FAIL", ("no passing interface artifact or interface_check tool_call after the "
                    "last model mutation")


def _grade_interference_clean(ledger: dict, ctx: dict) -> "tuple[str,str]":
    art = ledger_artifact(ledger, "interference")
    if not art:
        return "FAIL", final_check_absence(ledger, "interference")
    clashes = art.get("clashes")
    if clashes is None:
        return "FAIL", "interference artifact missing clashes field"
    try:
        n = int(clashes)
    except (TypeError, ValueError):
        return "FAIL", f"interference.clashes not int: {clashes!r}"
    if n != 0:
        return "FAIL", f"interference clashes={n}, expected 0"
    return "PASS", "assembly interference clean (0 clashes)"


def _grade_mates_ok(ledger: dict, ctx: dict) -> "tuple[str,str]":
    mates_ok = ledger_decision_value(ledger, "mates_ok", "mates_ok")
    if mates_ok is None:
        mates_ok = ledger_decision_value(ledger, "mates_ok", "ok")
    if mates_ok is True or str(mates_ok).lower() in {"true", "1", "pass", "ok"}:
        return "PASS", "decision mates_ok=true"
    # solve_mates / asm_mate tool success
    saw_solve = False
    for _, p in _tool_call_pairs(ledger):
        tool = str(p.get("tool") or "")
        if tool in {"solve_mates", "asm_mate"}:
            if p.get("ok") is False or p.get("error"):
                continue
            saw_solve = True
            result = p.get("result") or {}
            if isinstance(result, dict) and result.get("ok") is False:
                continue
            if tool == "solve_mates":
                return "PASS", "solve_mates tool_call ok"
    if saw_solve:
        # asm_mate alone without mates_ok decision is weak; require decision or solve_mates
        for _, p in _tool_call_pairs(ledger):
            if str(p.get("tool") or "") == "solve_mates" and p.get("ok") is not False:
                return "PASS", "solve_mates present"
    if mates_ok is False or str(mates_ok).lower() in {"false", "0", "fail"}:
        return "FAIL", "mates_ok is false"
    return "FAIL", "no mates_ok=true decision and no successful solve_mates"


def _grade_battery_both_parts(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bats = _all_artifacts(ledger, "battery")
    if not bats:
        return "FAIL", "no verify_battery artifact recorded"
    # Prefer per-part batteries tagged by id/name/part; else require ≥2 PASS batteries
    # or one battery covering both parts.
    passed = []
    for b in bats:
        if str(b.get("verdict", "")).upper() != "PASS":
            continue
        passed.append(b)
    if not passed:
        return "FAIL", f"no battery PASS ({len(bats)} battery artifact(s))"
    # Identify parts covered
    parts_covered: set[str] = set()
    for b in passed:
        for key in ("part", "name", "for_dl", "dl", "doc"):
            v = b.get(key)
            if v:
                parts_covered.add(str(v).lower())
        bid = str(b.get("id") or "").lower()
        for token in ("bracket", "cover", "dl1", "dl2"):
            if token in bid:
                parts_covered.add(token)
        note = str(b.get("note") or "").lower()
        for token in ("bracket", "cover"):
            if token in note:
                parts_covered.add(token)
    has_bracket = any(t in parts_covered for t in ("bracket", "dl1"))
    has_cover = any(t in parts_covered for t in ("cover", "dl2"))
    if has_bracket and has_cover:
        return "PASS", "battery PASS for bracket and cover"
    if len(passed) >= 2 and has_bracket or has_cover:
        # two PASSes with at least one named part
        if len(passed) >= 2 and (has_bracket or has_cover):
            # if both unnamed but two PASSes, still accept when notes mention both
            pass
    if has_bracket and has_cover:
        return "PASS", "battery PASS both parts"
    # single battery that explicitly covers both
    for b in passed:
        note = str(b.get("note") or "").lower()
        parts = b.get("parts") or b.get("docs") or []
        if isinstance(parts, list) and len(parts) >= 2:
            return "PASS", f"battery covers {len(parts)} parts"
        if "bracket" in note and "cover" in note:
            return "PASS", "battery note covers bracket and cover"
        if b.get("both_parts") is True:
            return "PASS", "battery both_parts=true"
    if has_bracket and has_cover:
        return "PASS", "battery PASS bracket+cover"
    if len(passed) >= 2:
        # two separate PASS batteries without clear names — accept as both parts
        return "PASS", f"{len(passed)} battery PASS artifacts (both parts)"
    return "FAIL", (
        f"need battery PASS for both bracket and cover; covered={sorted(parts_covered) or 'none'}"
    )


CHECKS = [
    AcceptanceCheck("AC1", "matching bolt_pattern.pattern_mm on both mating faces",
                    _grade_matching_pattern),
    AcceptanceCheck("AC2", "interface_check pass (artifact or tool)",
                    _grade_interface_check),
    AcceptanceCheck("AC3", "interference clashes == 0", _grade_interference_clean),
    AcceptanceCheck("AC4", "solve_mates ok / mates_ok=true", _grade_mates_ok),
    AcceptanceCheck("AC5", "verify_battery PASS both parts", _grade_battery_both_parts),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a bracket and a cover that mate on a flat face with a d10 pilot "
        "register and a matching 4xM4 bolt pattern on a 28 mm square. Solve the "
        "assembly mates, run interface_check, and confirm zero interference. "
        "Verify both parts with the battery.",
    "deliverables": [
        {
            "id": "DL1", "kind": "part", "name": "bracket",
            "description": "bracket with d10 pilot bore + 4xM4 clearance on 28mm square",
            "acceptance_criteria": [
                {"id": "AC1", "text": "4xM4 pattern on 28mm square",
                 "check": {"tool": "measure_part", "pass_rule": "pattern_mm==28"}},
            ],
            "depends_on": [], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL2", "kind": "part", "name": "cover",
            "description": "cover with d10 pilot boss + matching 4xM4 tapped pattern",
            "acceptance_criteria": [
                {"id": "AC1", "text": "matching 4xM4 pattern on 28mm square",
                 "check": {"tool": "measure_part", "pass_rule": "pattern_mm==28"}},
            ],
            "depends_on": [], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
        {
            "id": "DL3", "kind": "assembly", "name": "bracket-cover assembly",
            "description": "bracket + cover mated on pilot + bolt pattern",
            "acceptance_criteria": [
                {"id": "AC2", "text": "interface_check pass",
                 "check": {"tool": "interface_check", "pass_rule": "PASS"}},
                {"id": "AC3", "text": "interference clean",
                 "check": {"tool": "check_interference", "pass_rule": "clashes==0"}},
                {"id": "AC4", "text": "mates solved",
                 "check": {"tool": "solve_mates", "pass_rule": "ok"}},
                {"id": "AC5", "text": "battery PASS both parts",
                 "check": {"tool": "verify_battery", "pass_rule": "PASS both"}},
            ],
            "depends_on": ["DL1", "DL2"], "target_refs": {"doc_id": None, "features": []},
            "status": "pending",
        },
    ],
    "ambiguities": [],
    "assumptions": [
        {"text": "M4 clearance d4.5; cover side tapped; material 6061-T6",
         "basis": "default machined interface", "needs_confirm": False},
    ],
}


def _base_turns() -> list[dict]:
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "pilot_bore", "value": PILOT_NOM,
         "unit": "mm", "driver": "d10 pilot register", "provenance": "spec",
         "status": "confirmed"},
        {"_kind": "decision", "id": "bolt_pattern", "stage": "plan", "for_ac": "AC1",
         "text": "4xM4 clearance on 28mm square, bracket side",
         "holes": PATTERN_HOLES, "hole_d": M4_CLEARANCE_D, "pattern_mm": PATTERN_MM,
         "rationale": "bracket mating face bolt pattern",
         "node": "n_plan_DL1"},
        {"_kind": "decision", "id": "bolt_pattern_mate", "stage": "plan", "for_ac": "AC1",
         "text": "4xM4 tapped on matching 28mm square, cover side",
         "holes": PATTERN_HOLES, "pattern_mm": PATTERN_MM,
         "rationale": "cover pattern mirrors bracket",
         "node": "n_plan_DL2"},
        {"_kind": "tool_call", "tool": "new_part", "node": "n_build_DL1",
         "args": {"name": "bracket"}, "hypothesis": "create bracket"},
        {"_kind": "tool_call", "tool": "add_features", "node": "n_build_DL1",
         "args": {"op": "box", "w": 50, "d": 50, "h": 8}, "hypothesis": "bracket body"},
        {"_kind": "tool_call", "tool": "new_part", "node": "n_build_DL2",
         "args": {"name": "cover"}, "hypothesis": "create cover"},
        {"_kind": "tool_call", "tool": "add_features", "node": "n_build_DL2",
         "args": {"op": "box", "w": 50, "d": 50, "h": 4}, "hypothesis": "cover body"},
        {"_kind": "tool_call", "tool": "new_assembly", "for_ac": "AC4", "node": "n_build_DL3",
         "args": {"name": "bracket_cover_asm"}, "hypothesis": "assembly doc"},
        {"_kind": "tool_call", "tool": "asm_add_component", "node": "n_build_DL3",
         "args": {"path": "bracket.step", "fixed": True}, "hypothesis": "ground bracket"},
        {"_kind": "tool_call", "tool": "asm_add_component", "node": "n_build_DL3",
         "args": {"path": "cover.step"}, "hypothesis": "add cover"},
        {"_kind": "tool_call", "tool": "asm_mate", "for_ac": "AC4", "node": "n_build_DL3",
         "args": {"mate_type": "concentric",
                  "a": {"component": "bracket", "query": {"of": "face", "name": "pilot"}},
                  "b": {"component": "cover", "query": {"of": "face", "name": "pilot"}}},
         "hypothesis": "pilot concentric"},
        {"_kind": "tool_call", "tool": "asm_mate", "for_ac": "AC4", "node": "n_build_DL3",
         "args": {"mate_type": "coincident",
                  "a": {"component": "bracket", "query": {"of": "face", "side": "top"}},
                  "b": {"component": "cover", "query": {"of": "face", "side": "bottom"}}},
         "hypothesis": "mating faces coincident"},
        {"_kind": "tool_call", "tool": "solve_mates", "for_ac": "AC4", "node": "n_build_DL3",
         "args": {}, "hypothesis": "solve assembly mates", "ok": True},
        {"_kind": "decision", "id": "mates_ok", "stage": "verify", "for_ac": "AC4",
         "text": "mates solved cleanly", "mates_ok": True, "ok": True,
         "rationale": "solve_mates returned ok", "node": "n_verify_DL3"},
        {"_kind": "tool_call", "tool": "interface_check", "for_ac": "AC2",
         "node": "n_verify_DL3",
         "args": {"a": "bracket", "b": "cover"},
         "hypothesis": "hole pattern + pilot coaxiality", "ok": True,
         "result": {"pass": True, "verdict": "PASS"}},
        {"_kind": "artifact", "kind": "interface", "id": "iface_DL3", "verdict": "PASS",
         "pass": True, "stage": "verify", "note": "patterns align; pilot coaxial"},
        {"_kind": "tool_call", "tool": "check_interference", "for_ac": "AC3",
         "node": "n_verify_DL3", "args": {},
         "hypothesis": "confirm zero clashes"},
        {"_kind": "artifact", "kind": "interference", "id": "interf_DL3", "clashes": 0,
         "stage": "verify", "note": "assembly clean"},
        {"_kind": "tool_call", "tool": "describe_assembly", "node": "n_verify_DL3",
         "args": {}, "hypothesis": "read-back assembly"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1_bracket",
         "verdict": "PASS", "part": "bracket", "for_dl": "DL1",
         "stage": "verify", "note": "verify_battery PASS bracket"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL2_cover",
         "verdict": "PASS", "part": "cover", "for_dl": "DL2",
         "stage": "verify", "note": "verify_battery PASS cover"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: mismatched pattern + interference clash; interface fail.
    bad = [dict(t) for t in _base_turns()]
    for t in bad:
        if t.get("id") == "bolt_pattern_mate":
            t["pattern_mm"] = 32.0
            t["text"] = "4xM4 on 32mm square (MISMATCH)"
            t["rationale"] = "cover pattern drifted to 32mm"
        if t.get("kind") == "interference":
            t["clashes"] = 2
            t["note"] = "cover holes miss bracket; faces clash at pilot"
        if t.get("kind") == "interface":
            t["verdict"] = "FAIL"
            t["pass"] = False
        if t.get("tool") == "interface_check":
            t["result"] = {"pass": False, "verdict": "FAIL"}
            t["ok"] = False
            t["error"] = "pattern mismatch 28 vs 32"
        if t.get("id") == "mates_ok":
            t["mates_ok"] = False
            t["ok"] = False
        if t.get("tool") == "solve_mates":
            t["ok"] = False
            t["error"] = "overconstrained after pattern drift"
        if t.get("kind") == "battery":
            t["verdict"] = "FAIL"
            t["note"] = "battery FAIL after interface mismatch"

    # seed 3: patterns match but never ran interface_check / solve_mates / interference.
    incomplete = [
        {"_kind": "decision", "id": "bolt_pattern", "stage": "plan", "for_ac": "AC1",
         "text": "4xM4 on 28mm", "holes": PATTERN_HOLES, "pattern_mm": PATTERN_MM,
         "node": "n_plan_DL1"},
        {"_kind": "decision", "id": "bolt_pattern_mate", "stage": "plan", "for_ac": "AC1",
         "text": "matching 4xM4 on 28mm", "holes": PATTERN_HOLES, "pattern_mm": PATTERN_MM,
         "node": "n_plan_DL2"},
        {"_kind": "tool_call", "tool": "new_part", "node": "n_build_DL1",
         "args": {"name": "bracket"}, "hypothesis": "bracket only"},
        {"_kind": "tool_call", "tool": "new_part", "node": "n_build_DL2",
         "args": {"name": "cover"}, "hypothesis": "cover only"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1_bracket",
         "verdict": "PASS", "part": "bracket", "for_dl": "DL1",
         "stage": "verify", "note": "bracket only"},
        # cover battery missing → AC5 fail; also AC2–4 fail
    ]

    return {
        1: {"DL1": clean, "DL2": [], "DL3": []},
        2: {"DL1": bad, "DL2": [], "DL3": []},
        3: {"DL1": incomplete, "DL2": [], "DL3": []},
    }


CHALLENGE = Challenge(
    id="C14", name="interface / mating-face layout", lane="assemblies-drawings",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes=("bracket+cover 4xM4@28mm + pilot; interface_check/solve_mates/interference; "
           "seed2=pattern mismatch+clash, seed3=skipped interface/mates."),
)
CHALLENGE.divisions = ("local", "open")  # type: ignore[attr-defined]
CHALLENGE.calibration = False  # type: ignore[attr-defined]
CHALLENGE.eyes = "either"  # type: ignore[attr-defined]
