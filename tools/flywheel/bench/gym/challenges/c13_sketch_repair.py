"""C13 — under-constrained sketch repair.

Intake loads a part whose sketch has known free DOF (under-constrained rectangle +
missing coincident). Model must read sketch health, constrain, and verify dof==0.

Ground-truth ACs:
  AC1  tool_call sketch_health OR check_sketch present
  AC2  artifact/decision sketch_status with dof==0 OR battery check_sketch PASS
  AC3  sketch_auto_constrain OR relation/dimension tool_calls after sketch_health
  AC4  battery PASS
  AC5  describe_sketch tool_call present (read-back)

Seeds: 1=clean PASS; 2=no constrain (dof stays >0); 3=tool_misuse (measure_part
instead of sketch_health) + missing describe_sketch.
"""
from __future__ import annotations

from . import Challenge, AcceptanceCheck, ledger_artifact


_HEALTH_TOOLS = frozenset({"sketch_health", "check_sketch"})
_CONSTRAIN_TOOLS = frozenset({
    "sketch_auto_constrain",
    "add_relation",
    "add_dimension",
    "sketch_add_relation",
    "sketch_add_dimension",
    "constrain_sketch",
})


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


def _grade_sketch_health_tool(ledger: dict, ctx: dict) -> "tuple[str,str]":
    for _, p in _tool_call_pairs(ledger):
        if str(p.get("tool") or "") in _HEALTH_TOOLS:
            return "PASS", f"tool_call {p.get('tool')} present"
    return "FAIL", "no sketch_health or check_sketch tool_call"


def _grade_dof_zero(ledger: dict, ctx: dict) -> "tuple[str,str]":
    # latest decision sketch_status.dof (walk events; last wins)
    dof = None
    for e in _events(ledger):
        p = e.get("payload") or {}
        if e.get("kind") == "decision" and p.get("id") == "sketch_status" and "dof" in p:
            dof = p.get("dof")
    if dof is not None:
        try:
            if int(dof) == 0:
                return "PASS", "sketch_status.dof == 0"
            return "FAIL", f"sketch_status.dof == {dof}, expected 0"
        except (TypeError, ValueError):
            return "FAIL", f"sketch_status.dof not numeric: {dof!r}"
    # artifact sketch_status / sketch with dof
    for e in _events(ledger):
        p = e.get("payload") or {}
        if e.get("kind") == "artifact" and p.get("kind") in {"sketch_status", "sketch"}:
            d = p.get("dof")
            if d is not None:
                try:
                    if int(d) == 0:
                        return "PASS", "artifact sketch_status dof==0"
                    return "FAIL", f"artifact sketch_status dof=={d}"
                except (TypeError, ValueError):
                    pass
    # battery includes check_sketch PASS
    bat = ledger_artifact(ledger, "battery")
    if bat:
        checks = bat.get("checks") or bat.get("results") or []
        if isinstance(checks, list):
            for c in checks:
                if not isinstance(c, dict):
                    continue
                name = str(c.get("name") or c.get("check") or c.get("id") or "")
                if "sketch" in name.lower():
                    verdict = str(
                        c.get("verdict") or c.get("result") or c.get("status") or ""
                    )
                    if verdict.upper() == "PASS":
                        return "PASS", f"battery check_sketch PASS ({name})"
        note = str(bat.get("note") or "")
        if "check_sketch" in note.lower() and "pass" in note.lower():
            return "PASS", "battery note records check_sketch PASS"
        if str(bat.get("verdict", "")).upper() == "PASS" and bat.get("check_sketch") == "PASS":
            return "PASS", "battery.check_sketch PASS"
    return "FAIL", "no sketch_status dof==0 and no battery check_sketch PASS"



def _grade_constrain_after_health(ledger: dict, ctx: dict) -> "tuple[str,str]":
    health_seq = None
    for e, p in _tool_call_pairs(ledger):
        if str(p.get("tool") or "") in _HEALTH_TOOLS:
            seq = int(e.get("seq") or 0)
            health_seq = seq if health_seq is None else min(health_seq, seq)
    if health_seq is None:
        return "FAIL", "no sketch_health/check_sketch to order constrain after"
    for e, p in _tool_call_pairs(ledger):
        tool = str(p.get("tool") or "")
        seq = int(e.get("seq") or 0)
        if seq <= health_seq:
            continue
        if tool in _CONSTRAIN_TOOLS:
            return "PASS", f"{tool} after sketch_health at seq {seq}"
        # also accept add_features with relation/dimension ops
        args = p.get("args") or {}
        op = str(args.get("op") or args.get("kind") or "").lower()
        if tool == "add_features" and op in {
            "relation", "dimension", "coincident", "horizontal", "vertical",
            "equal", "parallel", "perpendicular",
        }:
            return "PASS", f"add_features op={op} after sketch_health at seq {seq}"
    return "FAIL", "no sketch_auto_constrain / relation / dimension tool after sketch_health"


def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


def _grade_describe_sketch(ledger: dict, ctx: dict) -> "tuple[str,str]":
    for _, p in _tool_call_pairs(ledger):
        if str(p.get("tool") or "") == "describe_sketch":
            return "PASS", "describe_sketch tool_call present"
    return "FAIL", "no describe_sketch tool_call (read-back required)"


CHECKS = [
    AcceptanceCheck("AC1", "sketch_health or check_sketch tool_call present",
                    _grade_sketch_health_tool),
    AcceptanceCheck("AC2", "sketch fully constrained (dof==0 or check_sketch PASS)",
                    _grade_dof_zero),
    AcceptanceCheck("AC3", "constrain tool after sketch_health",
                    _grade_constrain_after_health),
    AcceptanceCheck("AC4", "verify_battery PASS", _grade_battery),
    AcceptanceCheck("AC5", "describe_sketch read-back tool_call present",
                    _grade_describe_sketch),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Repair the under-constrained rectangle sketch on the provided part: read "
        "sketch health, add the missing coincident/relations or auto-constrain until "
        "DOF is zero, then read back the sketch.",
    "deliverables": [{
        "id": "DL1", "kind": "part", "name": "sketch repair part",
        "description": "part with a fully constrained rectangle sketch (dof 0)",
        "acceptance_criteria": [
            {"id": "AC1", "text": "sketch health inspected",
             "check": {"tool": "sketch_health", "pass_rule": "called"}},
            {"id": "AC2", "text": "sketch fully constrained (dof==0)",
             "check": {"tool": "check_sketch", "pass_rule": "dof==0"}},
            {"id": "AC3", "text": "constraints applied after health read",
             "check": {"tool": "sketch_auto_constrain", "pass_rule": "after health"}},
            {"id": "AC4", "text": "geometry coherent",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
            {"id": "AC5", "text": "sketch read-back via describe_sketch",
             "check": {"tool": "describe_sketch", "pass_rule": "called"}},
        ],
        "depends_on": [], "target_refs": {"doc_id": None, "features": [],
                                          "sketch_id": "SK1"},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "fixture sketch is an open rectangle missing one coincident",
         "basis": "intake fixture", "needs_confirm": False},
    ],
    "fixture": {
        "kind": "underconstrained_sketch",
        "sketch_id": "SK1",
        "entities": "rectangle 40x20 open corner",
        "initial_dof": 2,
        "missing": ["coincident_corner", "horizontal_top"],
    },
}


def _base_turns() -> list[dict]:
    return [
        {"_kind": "tool_call", "tool": "sketch_health", "for_ac": "AC1",
         "node": "n_build_DL1", "args": {"sketch_id": "SK1"},
         "hypothesis": "diagnose free DOF on fixture sketch"},
        {"_kind": "decision", "id": "sketch_status", "stage": "build", "for_ac": "AC2",
         "text": "fixture sketch under-constrained: dof=2, missing coincident at corner",
         "dof": 2, "rationale": "sketch_health initial read",
         "node": "n_build_DL1"},
        {"_kind": "tool_call", "tool": "sketch_auto_constrain", "for_ac": "AC3",
         "node": "n_build_DL1", "args": {"sketch_id": "SK1"},
         "hypothesis": "auto-constrain free DOF"},
        {"_kind": "tool_call", "tool": "add_relation", "for_ac": "AC3",
         "node": "n_build_DL1",
         "args": {"sketch_id": "SK1", "type": "coincident",
                  "a": "end_1", "b": "start_4"},
         "hypothesis": "close open corner coincident"},
        {"_kind": "tool_call", "tool": "sketch_health", "for_ac": "AC2",
         "node": "n_build_DL1", "args": {"sketch_id": "SK1"},
         "hypothesis": "re-check DOF after constrain"},
        # final status overwrites earlier dof via latest decision id
        {"_kind": "decision", "id": "sketch_status", "stage": "build", "for_ac": "AC2",
         "text": "sketch fully constrained", "dof": 0,
         "rationale": "sketch_health after auto_constrain + coincident",
         "node": "n_build_DL1"},
        {"_kind": "tool_call", "tool": "describe_sketch", "for_ac": "AC5",
         "node": "n_verify_DL1", "args": {"sketch_id": "SK1"},
         "hypothesis": "read-back constrained sketch"},
        {"_kind": "tool_call", "tool": "check_sketch", "for_ac": "AC2",
         "node": "n_verify_DL1", "args": {"sketch_id": "SK1"},
         "hypothesis": "battery-style sketch check"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean; check_sketch PASS",
         "check_sketch": "PASS",
         "checks": [{"name": "check_sketch", "verdict": "PASS", "dof": 0}]},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: health read + describe but never constrain; dof stays 2; battery FAIL.
    no_constrain = [
        {"_kind": "tool_call", "tool": "sketch_health", "for_ac": "AC1",
         "node": "n_build_DL1", "args": {"sketch_id": "SK1"},
         "hypothesis": "diagnose free DOF"},
        {"_kind": "decision", "id": "sketch_status", "stage": "build", "for_ac": "AC2",
         "text": "still under-constrained", "dof": 2,
         "rationale": "left free DOF unfixed", "node": "n_build_DL1"},
        {"_kind": "tool_call", "tool": "describe_sketch", "for_ac": "AC5",
         "node": "n_verify_DL1", "args": {"sketch_id": "SK1"},
         "hypothesis": "read-back without fixing"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "FAIL",
         "stage": "verify", "note": "check_sketch FAIL dof=2",
         "check_sketch": "FAIL",
         "checks": [{"name": "check_sketch", "verdict": "FAIL", "dof": 2}]},
    ]

    # seed 3: tool_misuse — measure_part instead of sketch_health; no describe_sketch.
    misuse = [
        {"_kind": "tool_call", "tool": "measure_part", "for_ac": "AC1",
         "node": "n_build_DL1", "args": {},
         "hypothesis": "wrong tool for sketch DOF"},
        {"_kind": "decision", "id": "sketch_status", "stage": "build", "for_ac": "AC2",
         "text": "assumed constrained without health read", "dof": 0,
         "rationale": "guessed dof", "node": "n_build_DL1"},
        {"_kind": "tool_call", "tool": "sketch_auto_constrain", "for_ac": "AC3",
         "node": "n_build_DL1", "args": {"sketch_id": "SK1"},
         "hypothesis": "constrain without prior health"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "battery PASS (mock)"},
    ]

    return {
        1: {"DL1": clean},
        2: {"DL1": no_constrain},
        3: {"DL1": misuse},
    }


CHALLENGE = Challenge(
    id="C13", name="under-constrained sketch repair", lane="parts-sourcing",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes=("fixture under-constrained rectangle; sketch_health→constrain→dof0; "
           "seed2=no constrain, seed3=tool_misuse (no health/describe)."),
)
CHALLENGE.divisions = ("local", "open")  # type: ignore[attr-defined]
CHALLENGE.calibration = True  # type: ignore[attr-defined]
CHALLENGE.eyes = "either"  # type: ignore[attr-defined]
