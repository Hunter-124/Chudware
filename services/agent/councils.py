"""councils.py — Council primitive + build-strategy council (aicad.council/1).

Contract: contracts/advisor.md §19 (FROZEN) + §12 ponder amendment.

## Design

A council is a seated deliberation run entirely on the SAME model with different
persona system-prompts (cheap ponder delegations per §12).  It is NOT freeform
chat: each seat produces a *typed position* in round 1; round 2 receives those
typed positions as structured input (not chat history) and either integrates or
dissents (with a trigger_condition).

T-W14 (delegation) is not yet merged.  Seats run as plain provider calls behind a
delegation-shaped seam (`_run_seat`).  Swap in the real ponder delegation later
without changing call sites.  Flagged in FEATURE_FLAGGED_TOOLS (`council_convene`).

T-W10 (tiers.yaml) is not yet merged.  Default deliberation dial = ``full``
(councils always on), per the 2026-07-04 advisor amendment.

T-W19 (buildgraph pipeline) is not yet merged.  Phase-3 gate is feature-flagged
behind ``AI_CAD_COUNCIL_BUILDGRAPH`` (default on).

## Public API

- ``council_convene(kind, topic, inputs, provider, ledger=None)``
  Runs a full 2-round council and returns the council record dict.
  Signature is the seam T-W22's org.py will call; matching T-W22's stub.

- ``DissentsRegistry``   — live store of active dissents with trigger matching.
- ``check_dissents(observation, registry)``  — foreman watch-hook (call after every
  turn); returns advisory items for any fired dissents.
- ``must_haves_to_validator_findings(council, buildgraph_nodes)``
  — surfaces must_haves as aicad.advisory/1 findings in the buildgraph.
- ``build_strategy_needed(feature_count)``
  — Phase-3 gate: True if a build-strategy council is required.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COUNCIL_SCHEMA = "aicad.council/1"
ADVISORY_SCHEMA = "aicad.advisory/1"

# Feature-count threshold above which a build-strategy council is required.
# Configurable via AI_CAD_COUNCIL_FEATURE_THRESHOLD env var.
_DEFAULT_FEATURE_THRESHOLD = 5

# Phase-3 buildgraph seam feature-flag (default on; off = disable must_have
# injection into buildgraph, e.g. while T-W19 is integrating).
_BUILDGRAPH_SEAM_ENABLED = os.environ.get("AI_CAD_COUNCIL_BUILDGRAPH", "1") != "0"

# Deliberation dial: "full" = always on, "minimal" = risk-trigger only.
# Read from T-W10 tiers.yaml when merged; else default full per 2026-07-04 amendment.
_DELIBERATION_DEFAULT = os.environ.get("AI_CAD_COUNCIL_DELIBERATION", "full")

# Seat personas YAML (authored, canonical — loaded verbatim, never rewritten here).
_SEATS_YAML = (
    __import__("pathlib").Path(__file__).resolve().parent
    / "roles" / "council_seats.yaml"
)

_SEATS_CACHE: Optional[dict] = None


# ---------------------------------------------------------------------------
# Seats loader — verbatim YAML, never mutated
# ---------------------------------------------------------------------------

def _load_seats() -> dict:
    """Load council_seats.yaml verbatim (cached after first call)."""
    global _SEATS_CACHE
    if _SEATS_CACHE is not None:
        return _SEATS_CACHE
    try:
        raw = _SEATS_YAML.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except FileNotFoundError:
        raise RuntimeError(
            f"council_seats.yaml not found at {_SEATS_YAML}. "
            "It is a canonical authored file; check your checkout."
        )
    assert data.get("schema") == "aicad.council_seats/1", (
        "council_seats.yaml schema mismatch — expected aicad.council_seats/1"
    )
    _SEATS_CACHE = data
    return data


def get_seat_personas() -> dict[str, dict]:
    """Return {persona_name: {focus, prompt}} verbatim from council_seats.yaml."""
    data = _load_seats()
    return dict(data.get("seats", {}))


# ---------------------------------------------------------------------------
# Typed-position JSON schema + GBNF emitter (T-W7 contract)
# ---------------------------------------------------------------------------

#: The typed position schema each seat must output.
TYPED_POSITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["position", "risks", "must_haves"],
    "properties": {
        "position":   {"type": "string"},
        "risks":      {"type": "array", "items": {"type": "string"}},
        "must_haves": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


def _schema_to_gbnf(schema: dict) -> str:
    """Minimal JSON-schema → GBNF emitter for typed positions.

    Delegates to roles.py's emitter when that module is importable (T-W7 merged
    and real); falls back to a hand-written grammar for the typed-position schema.

    Only the typed-position schema is needed here — the fallback covers exactly
    that shape.
    """
    try:
        from services.agent.roles import schema_to_gbnf  # type: ignore
        return schema_to_gbnf(schema)
    except (ImportError, AttributeError):
        pass
    # Fallback: hard-coded GBNF for {position:str, risks:[str,...], must_haves:[str,...]}
    return r'''
root   ::= "{" ws "\"position\"" ws ":" ws string ws "," ws "\"risks\"" ws ":" ws str-array ws "," ws "\"must_haves\"" ws ":" ws str-array ws "}"
str-array ::= "[" ws "]" | "[" ws string (ws "," ws string)* ws "]"
string ::= "\"" char* "\""
char   ::= [^"\\] | "\\" ["\\/bfnrt]
ws     ::= [ \t\n\r]*
'''


def validate_typed_position(raw: Any) -> tuple[bool, str]:
    """Validate a parsed seat output against TYPED_POSITION_SCHEMA.

    Returns (ok, error_message).  error_message is empty string on ok=True.
    Uses roles.py's validate_handoff when available; else hand-checks required keys.
    """
    if not isinstance(raw, dict):
        return False, f"Position must be a JSON object, got {type(raw).__name__}"
    required = {"position", "risks", "must_haves"}
    missing = required - raw.keys()
    if missing:
        return False, f"Missing required keys: {sorted(missing)}"
    if not isinstance(raw.get("position"), str):
        return False, "'position' must be a string"
    if not isinstance(raw.get("risks"), list):
        return False, "'risks' must be an array"
    if not isinstance(raw.get("must_haves"), list):
        return False, "'must_haves' must be an array"
    extra = set(raw.keys()) - {"position", "risks", "must_haves"}
    if extra:
        return False, f"Extra keys not allowed: {sorted(extra)}"
    return True, ""


# ---------------------------------------------------------------------------
# Council record builder
# ---------------------------------------------------------------------------

def _council_id() -> str:
    return f"cn_{uuid.uuid4().hex[:10]}"


def _new_council_record(kind: str, topic: str, inputs: dict) -> dict:
    """Return a fresh aicad.council/1 record (unfilled positions/synthesis)."""
    return {
        "schema": COUNCIL_SCHEMA,
        "id": _council_id(),
        "kind": kind,
        "topic": topic,
        "seats": [
            {"persona": "manufacturing", "focus": "setup count, tool access, stock"},
            {"persona": "tolerancing",   "focus": "datums, stackups, critical dims"},
            {"persona": "fitment",       "focus": "interfaces, clearances, assembly order"},
            {"persona": "simplicity",    "focus": "fewest features that satisfy the ACs"},
        ],
        "rounds": 2,
        "inputs": inputs,
        "positions": [],     # filled by round-2 seats (final typed positions)
        "synthesis": {"decision": "", "rationale": "", "rejected_alternatives": []},
        "dissents": [],
        "chair": "department_manager",
    }


# ---------------------------------------------------------------------------
# Seat runner — delegation-shaped seam (T-W14 not merged)
# ---------------------------------------------------------------------------

def _run_seat(
    persona: str,
    persona_cfg: dict,
    round_num: int,
    topic: str,
    inputs: dict,
    round1_positions: Optional[list[dict]],
    provider: Any,
) -> tuple[dict, Optional[str]]:
    """Run one seat for one round.

    Returns (typed_position_dict, dissent_or_None).

    T-W14 SEAM: seats run as plain provider.complete() calls here.
    When T-W14 lands, replace with a ponder delegation of kind="ponder"
    (auto-approved, read-only, budget-bounded per §12 amendment).
    The signature of this function is the seam — callers don't change.
    """
    prompt_text = persona_cfg["prompt"]

    # Build user message
    parts: list[str] = [
        f"COUNCIL TOPIC: {topic}",
        "",
        "INPUTS:",
        json.dumps(inputs, indent=2, ensure_ascii=False),
        "",
    ]
    if round_num == 2 and round1_positions:
        parts += [
            "ROUND-1 POSITIONS FROM OTHER SEATS (read as typed input, not chat):",
            json.dumps(round1_positions, indent=2, ensure_ascii=False),
            "",
            "Round 2: review the above positions. Either integrate (update your position) "
            "or dissent. A dissent REQUIRES a trigger_condition — a deterministic geometry "
            "or dimension predicate (e.g. 'revisit if wall_thickness < 2mm'). "
            "Free-text-only conditions are stored but flagged unwatchable.",
            "",
        ]
        parts += [
            "Output your FINAL typed position as a JSON object with exactly these keys:",
            '  {"position": "...", "risks": ["..."], "must_haves": ["..."]}',
            "",
            "If dissenting, also append a separate JSON object:",
            '  {"dissent": "...", "trigger_condition": "..."}',
            "(Append it AFTER the position JSON on its own line.)",
        ]
    else:
        parts += [
            "Output your typed position as a JSON object with exactly these keys:",
            '  {"position": "...", "risks": ["..."], "must_haves": ["..."]}',
        ]

    user_message = "\n".join(parts)

    reply = provider.complete(
        system=prompt_text,
        messages=[{"role": "user", "content": [{"type": "text", "text": user_message}]}],
        max_tokens=1500,
    )

    # Parse typed position
    position_dict, dissent = _parse_seat_reply(reply, persona)
    return position_dict, dissent


def _parse_seat_reply(reply: str, persona: str) -> tuple[dict, Optional[str]]:
    """Extract typed position (and optional dissent) from a seat's raw text reply.

    Returns (position_dict, dissent_dict_or_None).
    position_dict always has 'seat' key injected.
    Raises ValueError on parse failure (→ typed-position rejection).
    """
    # Find all JSON objects in the reply (greedy block-extraction)
    json_blocks = _extract_json_objects(reply)

    if not json_blocks:
        raise ValueError(
            f"Seat '{persona}' produced no JSON object in its reply. "
            f"Reply (first 200 chars): {reply[:200]!r}"
        )

    position_raw = json_blocks[0]
    ok, err = validate_typed_position(position_raw)
    if not ok:
        raise ValueError(
            f"Seat '{persona}' typed-position rejected: {err}. "
            f"Raw position: {position_raw!r}"
        )

    position_dict = dict(position_raw)
    position_dict["seat"] = persona

    # Look for a dissent block (second JSON object with "dissent" key)
    dissent = None
    for block in json_blocks[1:]:
        if "dissent" in block:
            dissent = _validate_dissent(block, persona)
            break

    return position_dict, dissent


def _extract_json_objects(text: str) -> list[dict]:
    """Extract all top-level JSON objects from text (simple brace-matching)."""
    results = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            start = i
            for j in range(i, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(text[start:j + 1])
                            if isinstance(obj, dict):
                                results.append(obj)
                        except json.JSONDecodeError:
                            pass
                        i = j + 1
                        break
            else:
                i += 1
        else:
            i += 1
    return results


# ---------------------------------------------------------------------------
# Dissent validation + registry
# ---------------------------------------------------------------------------

# Simple predicate pattern: "<dimension_name> <op> <value><unit?>"
# Examples: "wall_thickness < 2mm", "pocket_depth > 4x_tool_diameter", "boss_od >= 25mm"
_WATCHABLE_PREDICATE_RE = re.compile(
    r"^\s*\w[\w_.]*\s*(?:<|>|<=|>=|==|!=)\s*[\d.]+\s*\w*\s*$",
    re.IGNORECASE,
)


def _is_watchable_condition(trigger_condition: str) -> bool:
    """True if the trigger_condition is a deterministic geometry/dimension predicate."""
    return bool(_WATCHABLE_PREDICATE_RE.match(trigger_condition.strip()))


def _validate_dissent(raw: dict, persona: str) -> dict:
    """Validate and annotate a dissent block.

    Returns an annotated dissent dict.  Stores free-text conditions but flags them
    unwatchable (store-and-flag rule — same discipline as must_haves).
    """
    dissent_text = raw.get("dissent", "")
    trigger_condition = raw.get("trigger_condition", "")

    watchable = _is_watchable_condition(trigger_condition) if trigger_condition else False

    return {
        "seat": persona,
        "dissent": str(dissent_text),
        "trigger_condition": str(trigger_condition),
        "watchable": watchable,
        # Free-text conditions are stored but flagged — not silently dropped
        "_unwatchable_note": (
            "" if watchable
            else "trigger_condition is free-text; stored but not auto-watched by foreman"
        ),
    }


@dataclass
class DissentsRegistry:
    """Live store of active dissents from one or more councils.

    The foreman calls ``check_dissents(observation)`` after every model turn
    and tool call to re-raise any dissent whose trigger fires.

    Only dissents with ``watchable=True`` are pattern-matched; free-text ones
    surface as a note in the return advisory but do not auto-fire.
    """
    _dissents: list[dict] = field(default_factory=list)

    def register(self, council_id: str, dissent: dict) -> None:
        """Add a dissent from a completed council."""
        self._dissents.append({
            "council_id": council_id,
            **dissent,
        })

    def active_dissents(self) -> list[dict]:
        return list(self._dissents)

    def check(self, observation: str) -> list[dict]:
        """Match observation text against watchable dissent trigger_conditions.

        Returns aicad.advisory/1 items for any fired dissents.
        ``observation`` is the concatenated model output + tool call summary for a turn.
        """
        fired: list[dict] = []
        for d in self._dissents:
            if not d.get("watchable"):
                continue
            tc = d.get("trigger_condition", "")
            if _trigger_fires(tc, observation):
                fired.append(_dissent_advisory(d))
        return fired


def _trigger_fires(trigger_condition: str, observation: str) -> bool:
    """Deterministic predicate match against observation text.

    Parses "<dim_name> <op> <value><unit>" and looks for the dimension name
    followed by a numeric value in observation, then evaluates the predicate.
    Falls back to substring match of the dimension name when numeric extraction fails.
    """
    m = _WATCHABLE_PREDICATE_RE.match(trigger_condition.strip())
    if not m:
        return False

    # Extract components
    parts = trigger_condition.strip().split()
    if len(parts) < 3:
        return False
    dim_name = parts[0]
    op = parts[1]
    threshold_str = parts[2]
    # Strip trailing unit (e.g. "2mm" → 2.0)
    threshold_num_m = re.match(r"([\d.]+)", threshold_str)
    if not threshold_num_m:
        # Can't parse threshold — fall back to name presence
        return dim_name.lower() in observation.lower()

    threshold = float(threshold_num_m.group(1))

    # Look for the dimension name followed by a number in the observation
    pattern = re.compile(
        re.escape(dim_name) + r"[\s:=]*([0-9]+(?:\.[0-9]+)?)",
        re.IGNORECASE,
    )
    match = pattern.search(observation)
    if not match:
        return False

    observed_val = float(match.group(1))
    return _eval_op(observed_val, op, threshold)


def _eval_op(a: float, op: str, b: float) -> bool:
    if op == "<":  return a < b
    if op == ">":  return a > b
    if op == "<=": return a <= b
    if op == ">=": return a >= b
    if op == "==": return a == b
    if op == "!=": return a != b
    return False


def _dissent_advisory(dissent: dict) -> dict:
    """Build an aicad.advisory/1 item for a fired dissent."""
    return {
        "schema": ADVISORY_SCHEMA,
        "seq": int(time.time() * 1000) % 100000,
        "stage": "build_plan",
        "items": [{
            "id": f"adv_{uuid.uuid4().hex[:8]}",
            "severity": "action",
            "check": "council_dissent_triggered",
            "finding": (
                f"Council dissent from seat '{dissent.get('seat')}' "
                f"(council {dissent.get('council_id')}) fired: "
                f"{dissent.get('dissent')}. "
                f"Condition: {dissent.get('trigger_condition')}"
            ),
            "refs": {
                "council_id": dissent.get("council_id", ""),
                "seat": dissent.get("seat", ""),
                "trigger_condition": dissent.get("trigger_condition", ""),
            },
            "numbers": {},
            "candidate_fixes": [
                {
                    "summary": "Re-run the build-strategy council with the triggered condition in scope",
                    "kind": "rerun_council",
                }
            ],
            "helper_tools": ["council_convene"],
            "source": {"kind": "calc", "detail": "dissents_registry"},
        }],
    }


# ---------------------------------------------------------------------------
# Foreman watch hook
# ---------------------------------------------------------------------------

def check_dissents(observation: str, registry: DissentsRegistry) -> list[dict]:
    """Foreman hook: evaluate all active dissent trigger_conditions against an observation.

    Call after every model turn and tool call (additive to the existing trigger
    evaluation in advisor/triggers.py — does not replace it).

    Returns aicad.advisory/1 items for any fired dissents (may be empty).
    """
    return registry.check(observation)


# ---------------------------------------------------------------------------
# must_haves → buildgraph validator findings
# ---------------------------------------------------------------------------

def must_haves_to_validator_findings(
    council_record: dict,
    buildgraph_nodes: list[dict] | None = None,
) -> list[dict]:
    """Surface council must_haves as aicad.advisory/1 findings.

    HONEST RESOLUTION (no NL checker): every must_have becomes an advisory
    finding of severity="action" so it is tracked and human/planner-visible.
    Only must_haves that match the simple predicate pattern get a deterministic
    check; the rest are stored-and-flagged (same discipline as trigger_conditions).

    Returns a list of aicad.advisory/1 items.
    """
    if not _BUILDGRAPH_SEAM_ENABLED:
        return []

    findings: list[dict] = []
    council_id = council_record.get("id", "")

    for pos in council_record.get("positions", []):
        seat = pos.get("seat", "")
        for mh in pos.get("must_haves", []):
            watchable = _is_watchable_condition(mh)
            finding_text = (
                f"[Council {council_id} seat={seat}] must_have: {mh}"
            )
            if not watchable:
                finding_text += (
                    " [stored; not auto-checkable — free-text predicate]"
                )
            findings.append({
                "schema": ADVISORY_SCHEMA,
                "seq": int(time.time() * 1000) % 100000,
                "stage": "build_plan",
                "items": [{
                    "id": f"adv_{uuid.uuid4().hex[:8]}",
                    "severity": "action",
                    "check": "council_must_have",
                    "finding": finding_text,
                    "refs": {
                        "council_id": council_id,
                        "seat": seat,
                        "must_have": mh,
                        "watchable": watchable,
                    },
                    "numbers": {},
                    "candidate_fixes": [
                        {
                            "summary": (
                                "Verify this must_have is addressed in the build plan "
                                "before proceeding to Phase 3 execution"
                            ),
                            "kind": "manual_verify",
                        }
                    ],
                    "helper_tools": [],
                    "source": {
                        "kind": "calc",
                        "detail": "council_must_haves",
                    },
                }],
            })

    return findings


# ---------------------------------------------------------------------------
# Chair synthesis
# ---------------------------------------------------------------------------

def _synthesize(
    positions: list[dict],
    dissents: list[dict],
    topic: str,
    provider: Any,
) -> dict:
    """Chair synthesizes round-2 positions into a decision + rejected alternatives.

    Runs as a plain provider call (same delegation-seam pattern as seats).
    """
    system = (
        "You are the CHAIR of a build-strategy council. "
        "Read the seated positions and synthesize a single recommended build strategy. "
        "Output a JSON object with exactly these keys: "
        '{"decision": "...", "rationale": "...", "rejected_alternatives": ["..."]}. '
        "Be concise. rejected_alternatives lists strategies that were considered but not chosen."
    )

    user_text = (
        f"TOPIC: {topic}\n\n"
        "ROUND-2 POSITIONS:\n"
        + json.dumps(positions, indent=2, ensure_ascii=False)
        + ("\n\nDISSENTS:\n" + json.dumps(dissents, indent=2, ensure_ascii=False)
           if dissents else "")
        + "\n\nOutput synthesis JSON:"
    )

    reply = provider.complete(
        system=system,
        messages=[{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        max_tokens=600,
    )

    blocks = _extract_json_objects(reply)
    if not blocks:
        return {
            "decision": reply[:500],
            "rationale": "(chair reply was not valid JSON — stored verbatim)",
            "rejected_alternatives": [],
        }

    raw = blocks[0]
    return {
        "decision": str(raw.get("decision", "")),
        "rationale": str(raw.get("rationale", "")),
        "rejected_alternatives": list(raw.get("rejected_alternatives", [])),
    }


# ---------------------------------------------------------------------------
# Phase-3 gate
# ---------------------------------------------------------------------------

def build_strategy_needed(feature_count: int) -> bool:
    """True if a build-strategy council is required before Phase 3 execution.

    Threshold configured via AI_CAD_COUNCIL_FEATURE_THRESHOLD env var
    (default 5).  Also checks the deliberation dial: 'minimal' skips unless
    threshold exceeded AND a risk trigger has fired (risk trigger not implemented
    here — callers pass the result of their own risk check in that mode).
    """
    threshold = int(
        os.environ.get("AI_CAD_COUNCIL_FEATURE_THRESHOLD", str(_DEFAULT_FEATURE_THRESHOLD))
    )
    return feature_count >= threshold


# ---------------------------------------------------------------------------
# Main entry point — council_convene
# ---------------------------------------------------------------------------

def council_convene(
    kind: str,
    topic: str,
    inputs: dict,
    provider: Any,
    ledger: Any = None,
    *,
    actor: str = "council",
    registry: Optional[DissentsRegistry] = None,
) -> dict:
    """Convene a council and return the completed aicad.council/1 record.

    This is the T-W22 seam: org.py will call council_convene(kind, topic, inputs).
    The signature is stable.

    Args:
        kind:     Council kind (e.g. "build_strategy").
        topic:    Free-text topic for all seats.
        inputs:   dict with "ledger_refs" and/or "buildgraph" keys.
        provider: A Provider instance (mock or real).  Seats + chair call it.
        ledger:   Optional Ledger instance.  Council record written as artifact
                  event; synthesis as decision event.
        actor:    Ledger actor string.
        registry: Optional DissentsRegistry to register dissents into.

    Returns:
        Completed aicad.council/1 record dict.

    Raises:
        ValueError: if a seat produces a malformed typed position (typed-position
                    rejection — the council aborts with partial data).
    """
    personas = get_seat_personas()
    record = _new_council_record(kind, topic, inputs)

    # ── Round 1 ──────────────────────────────────────────────────────────────
    round1_positions: list[dict] = []
    for seat_cfg in record["seats"]:
        persona = seat_cfg["persona"]
        p_cfg = personas.get(persona)
        if p_cfg is None:
            raise RuntimeError(
                f"Persona '{persona}' not found in council_seats.yaml. "
                "Check the authored file."
            )
        pos, _dissent_r1 = _run_seat(
            persona=persona,
            persona_cfg=p_cfg,
            round_num=1,
            topic=topic,
            inputs=inputs,
            round1_positions=None,
            provider=provider,
        )
        round1_positions.append(pos)
        # Round-1 positions are ledger notes (not final positions)
        if ledger is not None:
            ledger.append(actor, "artifact", {
                "id": f"{record['id']}_r1_{persona}",
                "kind": "council_round1",
                "council_id": record["id"],
                "seat": persona,
                "position": pos,
            })

    # ── Round 2 ──────────────────────────────────────────────────────────────
    final_positions: list[dict] = []
    final_dissents: list[dict] = []

    for seat_cfg in record["seats"]:
        persona = seat_cfg["persona"]
        p_cfg = personas[persona]
        pos, dissent = _run_seat(
            persona=persona,
            persona_cfg=p_cfg,
            round_num=2,
            topic=topic,
            inputs=inputs,
            round1_positions=round1_positions,
            provider=provider,
        )
        final_positions.append(pos)
        if dissent is not None:
            final_dissents.append(dissent)

    record["positions"] = final_positions
    record["dissents"] = final_dissents

    # ── Chair synthesis ───────────────────────────────────────────────────────
    synthesis = _synthesize(final_positions, final_dissents, topic, provider)
    record["synthesis"] = synthesis

    # ── Ledger writes ─────────────────────────────────────────────────────────
    if ledger is not None:
        # Full council record as artifact
        ledger.append(actor, "artifact", {
            "id": record["id"],
            "kind": "council",
            "council": record,
        })
        # Synthesis as decision (with rejected_alternatives)
        ledger.append(actor, "decision", {
            "id": f"dec_{record['id']}",
            "stage": "build_plan",
            "text": synthesis["decision"],
            "rationale": synthesis["rationale"],
            "actor": actor,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rejected_alternatives": synthesis["rejected_alternatives"],
            "council_id": record["id"],
        })

    # ── Register dissents ─────────────────────────────────────────────────────
    if registry is not None:
        for d in final_dissents:
            registry.register(record["id"], d)

    return record
