"""councils.py — Council primitive + build-strategy council (aicad.council/1).

Contract: contracts/advisor.md §19 (FROZEN) + §12 ponder amendment.

## Design

A council is a seated deliberation run entirely on the SAME model with different
persona system-prompts (cheap ponder delegations per §12).  It is NOT freeform
chat: each seat produces a *typed position* in round 1; round 2 receives those
typed positions as structured input (not chat history) and either integrates or
dissents (with a trigger_condition).

Seats run as plain provider calls behind a delegation-shaped seam
(`_run_seat`).  A ponder delegation can be swapped in later without changing
call sites.  Flagged in FEATURE_FLAGGED_TOOLS (`council_convene`).

Buildgraph integration: the Phase-3 gate is feature-flagged behind
``CHUDWARE_COUNCIL_BUILDGRAPH`` (default on).

## Public API

- ``council_convene(kind, topic, inputs, provider, ledger=None, providers=None)``
  Runs a full 2-round council and returns the council record dict.
  Signature is the seam org.py calls.
  ``providers`` is an optional list of Provider instances for diverse-model
  selection; if omitted the single ``provider`` is used for all seats.

- ``resolve_policy(stage, tier, *, overrides=None, path=None)``
  Resolve the CouncilPolicy for a given (stage, tier).  Returns a CouncilPolicy
  dataclass with enabled/breadth/diverse_models/temperatures.

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
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml

from services.agent import protected_assets

# ---------------------------------------------------------------------------
# Council policy engine imports
# ---------------------------------------------------------------------------

from dataclasses import dataclass as _dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path as _Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COUNCIL_SCHEMA = "aicad.council/1"
ADVISORY_SCHEMA = "aicad.advisory/1"

# Phase-3 buildgraph seam feature-flag (default on; off = disable must_have
# injection into buildgraph).
_BUILDGRAPH_SEAM_ENABLED = os.environ.get("CHUDWARE_COUNCIL_BUILDGRAPH", "1") != "0"

_DEFAULT_PONDER_ROUND1_TOKENS = 1500
_DEFAULT_PONDER_ROUND2_TOKENS = 600

_LOG = logging.getLogger(__name__)

# Seat personas: authored YAML in developer runs, canonical compiled data in packages.
_SEATS_YAML = (
    __import__("pathlib").Path(__file__).resolve().parent
    / "roles" / "council_seats.yaml"
)

_SEATS_CACHE: Optional[dict] = None

# ---------------------------------------------------------------------------
# Council policy engine (aicad.council_policy/1)
# ---------------------------------------------------------------------------

_POLICY_YAML = (
    __import__("pathlib").Path(__file__).resolve().parent / "council_policy.yaml"
)
_POLICY_CACHE: Optional[dict] = None

# Ordered seat names (from council_seats.yaml).  Breadth slices from index 0.
_SEAT_ORDER: tuple[str, ...] = ("manufacturing", "tolerancing", "fitment", "simplicity")


@_dataclass(frozen=True)
class CouncilPolicy:
    """Resolved council policy for a (stage, tier) pair.

    Attributes:
        stage:          The pipeline stage (§1 vocabulary).
        tier:           The model tier name.
        enabled:        Whether the council is on by default.
        breadth:        Number of seats to use (1–4, clamped).
        diverse_models: True if multi-source model selection is requested.
        temperatures:   Per-seat temperature list (length == breadth).
                        When diverse_models=True AND multiple providers are
                        injected, temperatures are advisory hints only —
                        callers assign providers round-robin.
                        When diverse_models=False OR only 1 provider:
                        temperatures are the temperature-diversity fallback
                        (base + index * spread).
    """
    stage: str
    tier: str
    enabled: bool
    breadth: int
    diverse_models: bool
    temperatures: tuple[float, ...]

    @property
    def seat_names(self) -> tuple[str, ...]:
        """Return the slice of seat names for this policy's breadth."""
        return _SEAT_ORDER[: self.breadth]


def _load_policy_yaml(path: Optional["_Path"] = None) -> dict:
    """Load and cache council_policy.yaml.  Thread-unsafe (single-process)."""
    global _POLICY_CACHE
    if _POLICY_CACHE is not None and path is None:
        return _POLICY_CACHE
    import pathlib
    p = path or _POLICY_YAML
    if not isinstance(p, pathlib.Path):
        p = pathlib.Path(p)
    try:
        raw = p.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except FileNotFoundError:
        raise RuntimeError(
            f"council_policy.yaml not found at {p}. "
            "It is a canonical shipped file; check your checkout."
        )
    expected = "aicad.council_policy/1"
    if data.get("schema") != expected:
        raise RuntimeError(
            f"council_policy.yaml schema mismatch: expected {expected!r}, "
            f"got {data.get('schema')!r}"
        )
    if path is None:
        _POLICY_CACHE = data
    return data


def _build_temperatures(
    breadth: int,
    base_temp: float,
    spread: float,
) -> tuple[float, ...]:
    """Build per-seat temperatures for temperature-diversity fallback.

    Seat 0 gets base_temp; seat i gets base_temp + i*spread, clamped to [0.0, 1.0].
    """
    temps: list[float] = []
    for i in range(breadth):
        t = base_temp + i * spread
        temps.append(max(0.0, min(1.0, t)))
    return tuple(temps)


def resolve_policy(
    stage: str,
    tier: str,
    *,
    overrides: Optional[dict] = None,
    path: Optional["_Path"] = None,
) -> CouncilPolicy:
    """Resolve the council policy for a (stage, tier) pair.

    Resolution order (lowest wins = last applied):
      1. Shipped defaults from council_policy.yaml (stage row → defaults row).
      2. ``overrides`` dict (Advanced mode / effort preset layer).

    The ``deliberation`` field on TierProfile (full/standard/minimal) is NOT
    read here — ``enabled`` and ``breadth`` encode its intent directly in the
    yaml matrix, keeping the resolver self-contained.  Callers wanting to
    force-disable based on the tier profile can pass
    ``overrides={"enabled": False}``.

    Args:
        stage:     Pipeline stage name (§1 vocabulary; unknown stages use '*' fallback).
        tier:      Tier name (local-24k | local-48k | mid | frontier).
        overrides: Optional dict with any subset of {enabled, breadth, diverse_models}.
                   Boolean and int values only — no nested structure.
        path:      Override path for council_policy.yaml (tests).

    Returns:
        CouncilPolicy dataclass.

    Raises:
        RuntimeError: if council_policy.yaml is missing or malformed.
    """
    data = _load_policy_yaml(path)

    base_temp: float = float(data.get("base_temperature", 0.7))
    spread: float = float(data.get("temperature_spread", 0.1))

    # Look up (stage, tier) row; fall back to defaults[tier].
    stage_block = data.get("stages", {}).get(stage, {})
    tier_row: Optional[dict] = stage_block.get(tier)
    if tier_row is None:
        # Fall back to the defaults block for this tier.
        tier_row = data.get("defaults", {}).get(tier)
    if tier_row is None:
        # Last resort: safe defaults — local wide, frontier narrow.
        import re as _re
        is_local = tier.startswith("local")
        tier_row = {
            "enabled": True,
            "breadth": 4 if is_local else 1,
            "diverse_models": is_local,
        }

    enabled: bool = bool(tier_row.get("enabled", False))
    breadth: int = max(1, min(4, int(tier_row.get("breadth", 1))))
    diverse_models: bool = bool(tier_row.get("diverse_models", False))

    # Apply overrides (Advanced mode layer).
    if overrides:
        if "enabled" in overrides:
            enabled = bool(overrides["enabled"])
        if "breadth" in overrides:
            breadth = max(1, min(4, int(overrides["breadth"])))
        if "diverse_models" in overrides:
            diverse_models = bool(overrides["diverse_models"])

    temperatures = _build_temperatures(breadth, base_temp, spread)

    return CouncilPolicy(
        stage=stage,
        tier=tier,
        enabled=enabled,
        breadth=breadth,
        diverse_models=diverse_models,
        temperatures=temperatures,
    )


# ---------------------------------------------------------------------------
# Seats loader — canonical authored data, cached and never mutated
# ---------------------------------------------------------------------------

def _load_seats() -> dict:
    """Load the canonical council seats from compiled or developer assets."""
    global _SEATS_CACHE
    if _SEATS_CACHE is not None:
        return _SEATS_CACHE
    if protected_assets.packaged():
        raw = protected_assets.role_yaml("council_seats.yaml")
        if raw is None:
            raise RuntimeError("compiled council_seats.yaml asset is unavailable")
        data = json.loads(raw)
    else:
        try:
            data = yaml.safe_load(_SEATS_YAML.read_text(encoding="utf-8"))
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
# Typed-position JSON schema + GBNF emitter
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

    Delegates to roles.py's emitter when that module is importable; falls
    back to a hand-written grammar for the typed-position schema.

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
        "status": "in_progress",
        "complete": False,
        "seat_outcomes": [],
        "diagnostics": {},
    }


# ---------------------------------------------------------------------------
# Seat runner — delegation-shaped seam
# ---------------------------------------------------------------------------
class _SeatReplyError(ValueError):
    def __init__(self, message: str, raw_reply: str) -> None:
        super().__init__(message)
        self.raw_reply = raw_reply
        self.policy_refusal = _is_policy_refusal_reply(raw_reply)


class _ChairReplyError(ValueError):
    def __init__(self, message: str, raw_reply: str) -> None:
        super().__init__(message)
        self.raw_reply = raw_reply
        self.policy_refusal = _is_policy_refusal_reply(raw_reply)


def _is_policy_refusal_reply(reply: str) -> bool:
    """Recognize explicit refusal envelopes and unstructured refusal statements."""
    text = str(reply).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("policy_refusal") is True:
        return True
    lowered = text.lower()
    return (
        "policy refusal" in lowered
        or "refused by policy" in lowered
        or "cannot comply" in lowered
        or "must refuse" in lowered
    )



def _run_seat(
    persona: str,
    persona_cfg: dict,
    round_num: int,
    topic: str,
    inputs: dict,
    round1_positions: Optional[list[dict]],
    provider: Any,
    temperature: Optional[float] = None,
    max_tokens: int = _DEFAULT_PONDER_ROUND1_TOKENS,
) -> tuple[dict, Optional[str]]:
    """Run one seat for one round.

    Returns (typed_position_dict, dissent_or_None).

    Seats run as plain provider.complete() calls here.  A ponder delegation
    of kind="ponder" (auto-approved, read-only, budget-bounded per §12
    amendment) can replace this body.
    The signature of this function is the seam — callers don't change.

    ``temperature`` is passed through to provider.complete() via
    kwargs when provided.  Providers that don't accept temperature silently
    ignore the kwarg (base.Provider.complete has no temperature param;
    concrete providers that support it accept **kwargs).
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

    # Pass temperature as kwarg when set; providers that don't accept
    # temperature (e.g. the base mock) gracefully fall back to the no-temp call.
    if temperature is not None:
        try:
            reply = provider.complete(
                system=prompt_text,
                messages=[{"role": "user", "content": [{"type": "text", "text": user_message}]}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except TypeError:
            # Provider doesn't accept temperature — fall back to base signature.
            reply = provider.complete(
                system=prompt_text,
                messages=[{"role": "user", "content": [{"type": "text", "text": user_message}]}],
                max_tokens=max_tokens,
            )
    else:
        reply = provider.complete(
            system=prompt_text,
            messages=[{"role": "user", "content": [{"type": "text", "text": user_message}]}],
            max_tokens=max_tokens,
        )

    # Parse typed position while retaining the raw model response for diagnostics.
    try:
        return _parse_seat_reply(reply, persona)
    except ValueError as exc:
        raise _SeatReplyError(str(exc), reply) from exc


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
    max_tokens: int = _DEFAULT_PONDER_ROUND2_TOKENS,
) -> dict:
    """Chair synthesizes round-2 positions into a strictly typed decision."""
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
        max_tokens=max_tokens,
    )

    try:
        raw = json.loads(reply)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _ChairReplyError("chair reply is not a JSON object", str(reply)) from exc

    expected = {"decision", "rationale", "rejected_alternatives"}
    if not isinstance(raw, dict) or set(raw) != expected:
        raise _ChairReplyError(
            "chair reply does not match the required synthesis keys", str(reply))
    if not isinstance(raw["decision"], str) or not raw["decision"].strip():
        raise _ChairReplyError(
            "chair synthesis decision must be a non-empty string", str(reply))
    if not isinstance(raw["rationale"], str):
        raise _ChairReplyError(
            "chair synthesis rationale must be a string", str(reply))
    rejected = raw["rejected_alternatives"]
    if (
        not isinstance(rejected, list)
        or any(not isinstance(item, str) for item in rejected)
    ):
        raise _ChairReplyError(
            "chair rejected_alternatives must be an array of strings", str(reply))

    return {
        "decision": raw["decision"],
        "rationale": raw["rationale"],
        "rejected_alternatives": list(rejected),
    }


# ---------------------------------------------------------------------------
# Phase-3 gate
# ---------------------------------------------------------------------------

def build_strategy_needed(
    feature_count: int, *, cfg: Optional[dict] = None,
) -> bool:
    """Derive the mission council gate from the user's Copilot preferences."""
    from services.agent.preferences import resolve_turn_preferences

    return resolve_turn_preferences(
        "", cfg=cfg, feature_count=feature_count, task_kind="mission",
    ).council


# ---------------------------------------------------------------------------
# Main entry point — council_convene
# ---------------------------------------------------------------------------

def _pick_provider(providers: list[Any], seat_index: int) -> Any:
    """Select a provider for a seat using round-robin over the provider list.

    With 1 provider: always returns that provider (temperature-diversity fallback
    handles diversity via the ``temperature`` kwarg).
    With N providers: cycles by seat index so consecutive seats use different
    model sources (diverse-model selection).
    """
    if not providers:
        raise ValueError("_pick_provider: empty providers list")
    return providers[seat_index % len(providers)]

def _persist_council_record(record: dict, ledger: Any, actor: str) -> None:
    if ledger is not None:
        ledger.append(actor, "artifact", {
            "id": record["id"],
            "kind": "council",
            "council": record,
        })


def _incomplete_council(record: dict, ledger: Any, actor: str) -> dict:
    """Mark and persist a partial council without creating a decision event."""
    record["status"] = "incomplete"
    record["complete"] = False
    _persist_council_record(record, ledger, actor)
    return record




def council_convene(
    kind: str,
    topic: str,
    inputs: dict,
    provider: Any,
    ledger: Any = None,
    *,
    actor: str = "council",
    registry: Optional[DissentsRegistry] = None,
    providers: Optional[list[Any]] = None,
    stage: Optional[str] = None,
    tier: Optional[str] = None,
    policy_overrides: Optional[dict] = None,
    policy_path: Optional[Any] = None,
    cfg: Optional[dict] = None,
) -> dict:
    """Convene a council and return the completed aicad.council/1 record.

    This is the seam org.py calls: council_convene(kind, topic, inputs).
    The signature is stable; policy params are keyword-only with defaults.

    Args:
        kind:     Council kind (e.g. "build_strategy").
        topic:    Free-text topic for all seats.
        inputs:   dict with "ledger_refs" and/or "buildgraph" keys.
        provider: A Provider instance (mock or real).  Seats + chair call it.
                  Used as the sole provider when ``providers`` is not given.
        ledger:   Optional Ledger instance.  Council record written as artifact
                  event; synthesis as decision event.
        actor:    Ledger actor string.
        registry: Optional DissentsRegistry to register dissents into.

        Keyword-only policy params:
        providers:       Optional list of Provider instances.  When given with
                         >1 element, seats are assigned round-robin (diverse-model
                         selection).  When given with 1 element or omitted, a
                         single provider is used with temperature-diversity fallback.
        stage:           Pipeline stage name (§1 vocabulary).  Used to look up the
                         resolved CouncilPolicy.  When None, the policy defaults
                         (all seats from ``_new_council_record``) are used.
        tier:            Tier name (local-24k | local-48k | mid | frontier).
                         When None, no policy breadth capping is applied.
        policy_overrides: Optional per-call policy passed to ``resolve_policy``.
        policy_path:     Override path for council_policy.yaml (tests).
        cfg:             Optional Copilot configuration for this review.

    Returns:
        A complete or explicitly incomplete aicad.council/1 record.
    """
    from services.agent.preferences import resolve_preferences

    record = _new_council_record(kind, topic, inputs)
    if not resolve_preferences(cfg).double_check:
        record["diagnostics"] = {
            "policy": {
                "executed": False,
                "reason": "Double-check my work is turned off.",
            },
        }
        return _incomplete_council(record, ledger, actor)
    personas = get_seat_personas()

    # Authored seat and token budgets are implementation policy, not user knobs.
    seat_cap = len(record["seats"])
    round1_tokens = _DEFAULT_PONDER_ROUND1_TOKENS
    round2_tokens = _DEFAULT_PONDER_ROUND2_TOKENS

    # Resolve policy when stage+tier provided; apply breadth + diversity.
    policy: Optional[CouncilPolicy] = None
    if stage is not None and tier is not None:
        policy = resolve_policy(
            stage,
            tier,
            overrides=policy_overrides,
            path=policy_path,
        )
        seat_cap = min(seat_cap, policy.breadth)
        record["seats"] = list(record["seats"][:seat_cap])
        record["policy"] = {
            "stage": policy.stage,
            "tier": policy.tier,
            "breadth": seat_cap,
            "diverse_models": policy.diverse_models,
        }
    else:
        record["seats"] = list(record["seats"][:seat_cap])

    # Clamp copies of every seat/chair provider. The ambient provider objects
    # remain untouched even when their configured effort is above "high".
    from services.agent.role_models import clamp_provider_effort
    raw_provider_pool = list(providers) if providers else [provider]
    provider_pool = [
        clamp_provider_effort(candidate) for candidate in raw_provider_pool
    ]

    use_temp_diversity = len(provider_pool) == 1
    if policy is not None:
        temps = policy.temperatures
    else:
        temps = _build_temperatures(len(record["seats"]), 0.7, 0.1)

    # ── Round 1 ──────────────────────────────────────────────────────────────
    round1_positions: list[dict] = []
    for seat_idx, seat_cfg in enumerate(record["seats"]):
        persona = seat_cfg["persona"]
        p_cfg = personas.get(persona)
        seat_temp: Optional[float] = (
            temps[seat_idx] if use_temp_diversity and seat_idx < len(temps) else None
        )
        try:
            if p_cfg is None:
                raise RuntimeError(
                    f"Persona '{persona}' not found in council_seats.yaml")
            pos, _dissent_r1 = _run_seat(
                persona=persona,
                persona_cfg=p_cfg,
                round_num=1,
                topic=topic,
                inputs=inputs,
                round1_positions=None,
                provider=_pick_provider(provider_pool, seat_idx),
                temperature=seat_temp,
                max_tokens=round1_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — return the partial council
            refused = (
                isinstance(exc, _SeatReplyError) and exc.policy_refusal
            )
            outcome = {
                "seat": persona,
                "round": 1,
                "status": "refused" if refused else "failed",
                "outcome_kind": (
                    "policy_refusal"
                    if refused
                    else "validation_error"
                    if isinstance(exc, _SeatReplyError)
                    else "internal_error"
                ),
                "executed": False,
                "error": str(exc),
            }
            record["seat_outcomes"].append(outcome)
            diagnostic = dict(outcome)
            if isinstance(exc, _SeatReplyError):
                diagnostic["raw_reply"] = exc.raw_reply
            record["diagnostics"].setdefault("seat_errors", []).append(diagnostic)
            _LOG.exception(
                "Council %s seat %s failed in round 1", record["id"], persona)
            return _incomplete_council(record, ledger, actor)

        round1_positions.append(pos)
        record["seat_outcomes"].append({
            "seat": persona,
            "round": 1,
            "status": "executed",
            "outcome_kind": "ok",
            "executed": True,
            "position": pos,
        })
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
    for seat_idx, seat_cfg in enumerate(record["seats"]):
        persona = seat_cfg["persona"]
        seat_temp = (
            temps[seat_idx] if use_temp_diversity and seat_idx < len(temps) else None
        )
        try:
            pos, dissent = _run_seat(
                persona=persona,
                persona_cfg=personas[persona],
                round_num=2,
                topic=topic,
                inputs=inputs,
                round1_positions=round1_positions,
                provider=_pick_provider(provider_pool, seat_idx),
                temperature=seat_temp,
                max_tokens=round2_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — return the partial council
            refused = (
                isinstance(exc, _SeatReplyError) and exc.policy_refusal
            )
            outcome = {
                "seat": persona,
                "round": 2,
                "status": "refused" if refused else "failed",
                "outcome_kind": (
                    "policy_refusal"
                    if refused
                    else "validation_error"
                    if isinstance(exc, _SeatReplyError)
                    else "internal_error"
                ),
                "executed": False,
                "error": str(exc),
            }
            record["seat_outcomes"].append(outcome)
            diagnostic = dict(outcome)
            if isinstance(exc, _SeatReplyError):
                diagnostic["raw_reply"] = exc.raw_reply
            record["diagnostics"].setdefault("seat_errors", []).append(diagnostic)
            record["positions"] = final_positions
            record["dissents"] = final_dissents
            _LOG.exception(
                "Council %s seat %s failed in round 2", record["id"], persona)
            return _incomplete_council(record, ledger, actor)

        final_positions.append(pos)
        record["seat_outcomes"].append({
            "seat": persona,
            "round": 2,
            "status": "executed",
            "outcome_kind": "ok",
            "executed": True,
            "position": pos,
        })
        if dissent is not None:
            final_dissents.append(dissent)

    record["positions"] = final_positions
    record["dissents"] = final_dissents

    # ── Chair synthesis ───────────────────────────────────────────────────────
    try:
        synthesis = _synthesize(
            final_positions,
            final_dissents,
            topic,
            provider_pool[0],
            max_tokens=round2_tokens,
        )
    except Exception as exc:  # noqa: BLE001 — invalid/refused chair is incomplete
        refused = isinstance(exc, _ChairReplyError) and exc.policy_refusal
        diagnostic = {
            "status": "refused" if refused else "failed",
            "outcome_kind": (
                "policy_refusal"
                if refused
                else "validation_error"
                if isinstance(exc, _ChairReplyError)
                else "internal_error"
            ),
            "executed": False,
            "error": str(exc),
        }
        if isinstance(exc, _ChairReplyError):
            diagnostic["raw_reply"] = exc.raw_reply
        record["diagnostics"]["chair"] = diagnostic
        _LOG.exception("Council %s chair synthesis failed", record["id"])
        return _incomplete_council(record, ledger, actor)

    record["synthesis"] = synthesis
    record["status"] = "complete"
    record["complete"] = True

    # ── Ledger writes ─────────────────────────────────────────────────────────
    _persist_council_record(record, ledger, actor)
    if ledger is not None:
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

    if registry is not None:
        for dissent in final_dissents:
            registry.register(record["id"], dissent)

    return record
