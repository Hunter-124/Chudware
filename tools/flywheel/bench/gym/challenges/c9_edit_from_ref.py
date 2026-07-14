"""C9 — edit-from-reference: re-bore a C1-like flanged bushing from a structured delta.

LOCAL: structured-delta request ("bore → 22 H7; keep bolt circle").
OPEN: same delta + screenshot/contact_sheet wording for visual target.

Ground-truth ACs the gym grades ITSELF:
  AC1  bore diameter == 22 (±ε) via ledger_dimension("bore")
  AC2  bolt_circle holes still 4 (ledger_decision_value)
  AC3  artifact battery PASS
  AC4  describe_part OR measure_part tool_call BEFORE first mutating feature tool
  AC5  (OPEN only / soft-skip LOCAL) inspect_part|look_around|render_part present

Seeds:
  1  clean PASS (read → mutate → verify)
  2  tool_misuse — mutate without prior read (AC4 FAIL); taxonomy tool_misuse
  3  context_loss — cites nonexistent DIM77 (labeled failure)
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value, coerce_number)

BORE_NOM = 22.0
BORE_EPS = 1e-6
BOLT_HOLES = 4

# Tools that count as "read before mutate" for AC4.
_READ_TOOLS = frozenset({"describe_part", "measure_part", "query_geometry"})
# Feature-mutating tools (first of these must follow a read).
_MUTATE_TOOLS = frozenset({
    "add_features", "delete_feature", "edit_feature", "boolean",
    "extrude", "revolve", "hole", "fillet", "chamfer", "pattern", "shell",
})
# Vision tools required only in OPEN division (AC5).
_VISION_TOOLS = frozenset({"inspect_part", "look_around", "render_part", "contact_sheet"})


# ---- tool-call walk helpers -------------------------------------------------

def _tool_calls(ledger: dict) -> list[tuple[int, str, dict]]:
    """Return (seq, tool_name, payload) for every tool_call / agent_turn tool_call."""
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


# ---- ground-truth graders ---------------------------------------------------

def _grade_bore(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dim = ledger_dimension(ledger, "bore")
    if not dim:
        return "FAIL", "no bore dimension recorded in ledger"
    val = coerce_number(dim.get("value", 0))
    if val is None:
        return "FAIL", f"bore value {dim.get('value')!r} is not numeric"
    if abs(val - BORE_NOM) > BORE_EPS:
        return "FAIL", f"bore value {dim.get('value')} != {BORE_NOM}"
    return "PASS", f"bore d{BORE_NOM} after edit"


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


def _grade_read_before_mutate(ledger: dict, ctx: dict) -> "tuple[str,str]":
    calls = _tool_calls(ledger)
    first_mutate_seq = None
    first_read_seq = None
    for seq, tool, _p in calls:
        if tool in _READ_TOOLS and first_read_seq is None:
            first_read_seq = seq
        if tool in _MUTATE_TOOLS and first_mutate_seq is None:
            first_mutate_seq = seq
    if first_mutate_seq is None:
        return "FAIL", "no mutating feature tool_call recorded"
    if first_read_seq is None:
        return "FAIL", "no describe_part/measure_part before mutate"
    if first_read_seq >= first_mutate_seq:
        return "FAIL", (f"read tool seq={first_read_seq} not before "
                        f"first mutate seq={first_mutate_seq}")
    return "PASS", f"read@seq={first_read_seq} before mutate@seq={first_mutate_seq}"


def _grade_open_vision(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """AC5: OPEN-only vision inspect; soft-skip when division is local or unset."""
    division = str(ctx.get("division") or "local").lower()
    if division != "open":
        return "PASS", f"AC5 soft-skip (division={division!r}, not open)"
    calls = _tool_calls(ledger)
    for _seq, tool, _p in calls:
        if tool in _VISION_TOOLS:
            return "PASS", f"open-division vision tool {tool} present"
    return "FAIL", "open division requires inspect_part|look_around|render_part tool_call"


CHECKS = [
    AcceptanceCheck("AC1", f"bore re-bored to d{BORE_NOM} (±ε)", _grade_bore),
    AcceptanceCheck("AC2", f"bolt circle preserved ({BOLT_HOLES} holes)", _grade_bolt_circle),
    AcceptanceCheck("AC3", "verify_battery PASS (coherent geometry)", _grade_battery),
    AcceptanceCheck("AC4", "read (describe/measure) before first mutate tool",
                    _grade_read_before_mutate),
    AcceptanceCheck("AC5", "OPEN: vision inspect present (soft-skip LOCAL)",
                    _grade_open_vision),
]


# LOCAL structured-delta wording; OPEN request appends screenshot wording via notes.
_REQUEST_LOCAL = (
    "Edit the existing flanged bushing from the reference part: change the bore to "
    "22 H7 (structured delta: bore → 22 H7) and keep the 4-hole bolt circle unchanged. "
    "Read the current geometry (describe_part or measure_part) before mutating."
)
_REQUEST_OPEN_SUFFIX = (
    " OPEN/vision: a contact_sheet / screenshot of the target edited bushing is "
    "attached — inspect_part or look_around the target before editing."
)

MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim": _REQUEST_LOCAL,
    "deliverables": [{
        "id": "DL1", "kind": "modification", "name": "edited bushing",
        "description": "C1-like flanged bushing re-bored to d22 H7; bolt circle preserved",
        "acceptance_criteria": [
            {"id": "AC1", "text": "bore diameter == 22",
             "check": {"tool": "measure_part", "pass_rule": "bore==22"}},
            {"id": "AC2", "text": "bolt circle still 4 holes",
             "check": {"tool": "measure_part", "pass_rule": "holes==4"}},
            {"id": "AC3", "text": "geometry coherent",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
            {"id": "AC4", "text": "read before mutate",
             "check": {"tool": "describe_part", "pass_rule": "seq before add_features"}},
            {"id": "AC5", "text": "OPEN vision inspect present",
             "check": {"tool": "inspect_part", "pass_rule": "present when division=open"}},
        ],
        "depends_on": [],
        "target_refs": {"doc_id": "doc_C1", "features": ["bore"]},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "base part is the C1 flanged bushing fixture; only the bore changes",
         "basis": "edit-from-reference target", "needs_confirm": False},
    ],
    # OPEN request wording lives alongside the local request for division runners.
    "request_open": _REQUEST_LOCAL + _REQUEST_OPEN_SUFFIX,
}


# ---- scripted agent turns (per seed) ----------------------------------------

def _base_turns() -> list[dict]:
    """Seed-1 clean path: read → mutate → dim/decision → battery (+ vision for OPEN)."""
    return [
        {"_kind": "tool_call", "tool": "describe_part", "for_ac": "AC4",
         "node": "n_spec_DL1", "args": {},
         "hypothesis": "read current bore and bolt circle before edit"},
        {"_kind": "tool_call", "tool": "measure_part", "for_ac": "AC4",
         "node": "n_spec_DL1", "args": {"query": "bore diameter"},
         "hypothesis": "confirm measured bore before re-bore"},
        # Vision call is present so OPEN AC5 PASSes; LOCAL AC5 soft-skips either way.
        {"_kind": "tool_call", "tool": "render_part", "for_ac": "AC5",
         "node": "n_spec_DL1", "args": {"view": "iso"},
         "hypothesis": "OPEN: screenshot/inspect target before edit"},
        {"_kind": "tool_call", "tool": "add_features", "for_ac": "AC1",
         "node": "n_build_DL1",
         "args": {"op": "bore", "diameter": BORE_NOM, "through": True},
         "hypothesis": "re-bore to d22 H7 per structured delta"},
        {"_kind": "dimension", "id": "DIM1", "name": "bore_diameter", "value": BORE_NOM,
         "unit": "mm", "driver": "structured delta: bore → 22 H7",
         "provenance": "measure_part after re-bore",
         "tolerance": {"fit_class": "H7", "plus": 0.021, "minus": 0.0},
         "status": "confirmed"},
        {"_kind": "decision", "id": "bolt_circle", "stage": "plan", "for_ac": "AC2",
         "text": "keep the C1 4-hole bolt circle unchanged", "holes": BOLT_HOLES,
         "rationale": "bolt circle inherited from reference; only the bore is edited",
         "node": "n_plan_DL1"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean after edit"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: tool_misuse — mutate without a prior read (AC4 FAIL). Drop read tools;
    # keep wrong-tool measure of fit criterion for taxonomy tool_misuse via manifest.
    misuse = [dict(t) for t in _base_turns()
              if t.get("tool") not in ("describe_part", "measure_part")]
    # Wrong tool for AC1 check surface (manifest wants measure_part for bore).
    for t in misuse:
        if t.get("tool") == "add_features" and t.get("for_ac") == "AC1":
            # Leave add_features, but inject a fit_report used where measure is required.
            pass
    misuse.insert(0, {
        "_kind": "tool_call", "tool": "fit_report", "for_ac": "AC1",
        "node": "n_spec_DL1",
        "args": {"nominal": 22, "hole": "H7"},
        "hypothesis": "wrong tool for AC1 bore measure (tool_misuse)",
    })
    # Bore still correct so AC1/AC2/AC3 can PASS while AC4 FAILs → cell fails.
    # Taxonomy: tool_misuse fires on wrong check tool vs manifest AC1.

    # seed 3: context_loss — decision cites nonexistent DIM77.
    ctxloss = [dict(t) for t in _base_turns()]
    ctxloss.append({
        "_kind": "decision", "id": "note1", "stage": "build",
        "text": "flange offset follows DIM77 (base thickness) + 1.0",
        "rationale": "derive flange offset from DIM77", "node": "n_build_DL1",
    })

    return {
        1: {"DL1": clean},
        2: {"DL1": misuse},
        3: {"DL1": ctxloss},
    }


def _challenge(**kwargs) -> Challenge:
    """Construct Challenge, accepting optional division fields when landed."""
    fields = getattr(Challenge, "__dataclass_fields__", {})
    extra = {k: v for k, v in {
        "divisions": ("local", "open"),
        "calibration": False,
        "eyes": "either",
    }.items() if k in fields}
    return Challenge(**kwargs, **extra)


CHALLENGE = _challenge(
    id="C9", name="edit from reference (bore→22)", lane="parts-sourcing",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(), modifies="C1",
    notes=("edit-from-ref: LOCAL structured delta bore→22 H7 keep bolt circle; "
           "OPEN adds screenshot wording; seed2=tool_misuse (no read-before-mutate), "
           "seed3=context_loss."),
)
