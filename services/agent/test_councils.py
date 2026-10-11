"""test_councils.py — Offline tests for the council primitive (aicad.council/1).

Discipline: mock provider only (no API key, no all-skip).  Every test runs the
real council code path; the mock provider supplies scripted seat replies.

Tests:
  1. test_build_strategy_round_trip — 4-seat build-strategy council produces a
     valid aicad.council/1 record; positions use authored personas verbatim.
  2. test_dissent_reraise_on_condition — a dissent with a watchable trigger_condition
     fires when the observation matches; free-text conditions do NOT auto-fire.
  3. test_must_have_validator_findings — must_haves surface as advisory findings;
     watchable ones are flagged; free-text ones are stored-and-flagged.
  4. test_typed_position_rejection — malformed seat output raises ValueError.
  5. test_round2_receives_typed_input — round-2 seat prompt contains round-1
     positions as structured JSON, not as chat history.
  6. test_council_record_schema_exact — record exactly matches aicad.council/1 shape.
  7. test_ledger_artifacts_written — council writes artifact + decision events.
  8. test_build_strategy_needed — Phase-3 feature-count gate logic.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from services.agent.councils import (
    COUNCIL_SCHEMA,
    DissentsRegistry,
    _extract_json_objects,
    _is_watchable_condition,
    _parse_seat_reply,
    _run_seat,
    _trigger_fires,
    build_strategy_needed,
    check_dissents,
    council_convene,
    get_seat_personas,
    must_haves_to_validator_findings,
    validate_typed_position,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _typed_position(position: str, risks: list[str], must_haves: list[str]) -> str:
    """Return a JSON string that is a valid typed position (no extra keys)."""
    return json.dumps({"position": position, "risks": risks, "must_haves": must_haves})


def _dissent_block(dissent: str, trigger_condition: str) -> str:
    return json.dumps({"dissent": dissent, "trigger_condition": trigger_condition})


class ScriptedProvider:
    """Mock provider that returns scripted replies in order (wraps last reply)."""

    name = "mock"

    def __init__(self, replies: list[str]):
        self._replies = replies
        self._calls: list[tuple[str, list]] = []

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        self._calls.append((system, messages))
        idx = min(len(self._calls) - 1, len(self._replies) - 1)
        return self._replies[idx]


def _make_provider_for_council(
    round1_replies: dict[str, str],
    round2_replies: dict[str, str],
    synthesis_reply: str,
):
    """Build a provider that routes seat calls by persona name (sniffed from system)."""

    class TrackingProvider:
        name = "mock"
        _r1 = dict(round1_replies)
        _r2 = dict(round2_replies)
        _synthesis = synthesis_reply
        _calls: list[tuple[str, str]] = []

        def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
            user_text = messages[0]["content"][0]["text"] if messages else ""
            is_round2 = "ROUND-1 POSITIONS" in user_text
            is_synthesis = "CHAIR" in system or "synthesize" in system.lower()

            # Detect persona from system prompt opening line
            persona = None
            for name in ("manufacturing", "tolerancing", "fitment", "simplicity"):
                if name.upper() in system.upper():
                    persona = name
                    break

            self.__class__._calls.append((system[:40], user_text[:40]))

            if is_synthesis or persona is None:
                return self._synthesis
            if is_round2:
                return self._r2.get(persona, _typed_position(
                    f"{persona} r2 default", [], []
                ))
            return self._r1.get(persona, _typed_position(
                f"{persona} r1 default", [], []
            ))

    return TrackingProvider()


# ---------------------------------------------------------------------------
# Fixture: valid 4-seat scripted replies
# ---------------------------------------------------------------------------

_R1_MFG = _typed_position(
    "3-setup mill: setup-1 datum faces, setup-2 pockets, setup-3 bore",
    ["pocket depth may exceed 4x tool diameter", "thin wall on south rib"],
    ["datum faces machined in setup 1"],
)
_R1_TOL = _typed_position(
    "Primary datum A (bottom face), B (bore CL), C (slot). Stack: ±0.05mm",
    ["bore and pattern stack if datum not shared"],
    ["bore and mounting pattern referenced to the same datum"],
)
_R1_FIT = _typed_position(
    "Lid locates on boss OD before fasteners engage. Min clearance 0.1mm.",
    ["clearance budget tight if tolerance stack is at worst case"],
    ["lid must locate before fasteners engage"],
)
_R1_SIM = _typed_position(
    "Remove decorative rib — no AC demands it. Saves one setup.",
    ["simplification may reduce stiffness at corner load"],
    ["confirm rib AC before removing"],
)

_R2_MFG = _typed_position(
    "Accept simplicity proposal on rib; retain 3-setup plan.",
    ["pocket still deep — confirm tooling"],
    ["datum faces machined in setup 1"],
)
_R2_TOL = _typed_position(
    "Integrate: shared datum survives rib removal.",
    [],
    ["bore and mounting pattern referenced to the same datum"],
)
_R2_FIT = _typed_position(
    "Integrate: lid locates correctly with or without rib.",
    [],
    ["lid must locate before fasteners engage"],
)
# Simplicity dissents in round 2 with a watchable condition
_R2_SIM = (
    _typed_position("Integrate: rib can go if AC confirms.", [], ["confirm rib AC before removing"])
    + "\n"
    + _dissent_block(
        "Revisit simplification if wall_thickness drops below 2mm during rib removal",
        "wall_thickness < 2",
    )
)

_SYNTHESIS = json.dumps({
    "decision": "3-setup mill plan with rib removed subject to AC confirmation.",
    "rationale": "Seats aligned after round 2. Simplicity and manufacturing agree.",
    "rejected_alternatives": ["Keep rib unconditionally", "4-setup plan"],
})


# ---------------------------------------------------------------------------
# Test 1: round-trip
# ---------------------------------------------------------------------------

def test_build_strategy_round_trip():
    """4-seat build-strategy council produces a valid aicad.council/1 record."""
    provider = _make_provider_for_council(
        {"manufacturing": _R1_MFG, "tolerancing": _R1_TOL,
         "fitment": _R1_FIT, "simplicity": _R1_SIM},
        {"manufacturing": _R2_MFG, "tolerancing": _R2_TOL,
         "fitment": _R2_FIT, "simplicity": _R2_SIM},
        _SYNTHESIS,
    )

    record = council_convene(
        kind="build_strategy",
        topic="how to build DL1 bottom-shell",
        inputs={"ledger_refs": [], "buildgraph": None},
        provider=provider,
    )

    # Schema check (aicad.council/1 exact)
    assert record["schema"] == COUNCIL_SCHEMA
    assert record["kind"] == "build_strategy"
    assert record["rounds"] == 2
    assert record["id"].startswith("cn_")
    assert record["chair"] == "department_manager"

    # 4 seats present
    assert len(record["seats"]) == 4
    seat_personas = {s["persona"] for s in record["seats"]}
    assert seat_personas == {"manufacturing", "tolerancing", "fitment", "simplicity"}

    # Final positions (round-2) — one per seat
    assert len(record["positions"]) == 4
    for pos in record["positions"]:
        assert "seat" in pos
        assert "position" in pos
        assert isinstance(pos["risks"], list)
        assert isinstance(pos["must_haves"], list)
        # No extra keys (validate_typed_position strips these; seat key is added)
        allowed = {"seat", "position", "risks", "must_haves"}
        assert set(pos.keys()) <= allowed, f"Extra keys in position: {pos.keys()}"

    # Personas loaded from authored council_seats.yaml verbatim
    personas = get_seat_personas()
    for seat_name in ("manufacturing", "tolerancing", "fitment", "simplicity"):
        assert seat_name in personas
        assert "prompt" in personas[seat_name]
        assert "focus" in personas[seat_name]

    # Synthesis shape
    synth = record["synthesis"]
    assert isinstance(synth["decision"], str) and synth["decision"]
    assert isinstance(synth["rationale"], str)
    assert isinstance(synth["rejected_alternatives"], list)

    # Simplicity seat has a dissent
    assert len(record["dissents"]) >= 1
    simplicity_dissents = [d for d in record["dissents"] if d["seat"] == "simplicity"]
    assert simplicity_dissents, "Simplicity seat should have a dissent"
    d = simplicity_dissents[0]
    assert d["trigger_condition"] == "wall_thickness < 2"
    assert d["watchable"] is True


# ---------------------------------------------------------------------------
# Test 2: dissent re-raise on condition
# ---------------------------------------------------------------------------

def test_dissent_reraise_on_condition():
    """Watchable dissent fires when observation contains the matching value.
    Free-text condition does NOT auto-fire."""
    registry = DissentsRegistry()

    # Register a watchable dissent
    watchable_dissent = {
        "seat": "simplicity",
        "dissent": "Wall too thin for rib removal",
        "trigger_condition": "wall_thickness < 2",
        "watchable": True,
        "_unwatchable_note": "",
    }
    registry.register("cn_test001", watchable_dissent)

    # Register a free-text (unwatchable) dissent
    free_text_dissent = {
        "seat": "manufacturing",
        "dissent": "Too complex for the shop",
        "trigger_condition": "if the machinist says it's too complex",
        "watchable": False,
        "_unwatchable_note": "trigger_condition is free-text",
    }
    registry.register("cn_test001", free_text_dissent)

    # Observation that triggers the watchable dissent (wall_thickness = 1.5)
    observation_fires = "Feature: wall_thickness=1.5mm applied to south rib"
    fired = check_dissents(observation_fires, registry)
    assert len(fired) == 1, f"Expected 1 fired dissent, got {len(fired)}: {fired}"
    item = fired[0]["items"][0]
    assert item["check"] == "council_dissent_triggered"
    assert "wall_thickness" in item["finding"]
    assert item["refs"]["trigger_condition"] == "wall_thickness < 2"

    # Observation that does NOT trigger (wall_thickness = 3.0)
    observation_safe = "Feature: wall_thickness=3.0mm on south rib"
    fired_safe = check_dissents(observation_safe, registry)
    assert len(fired_safe) == 0, f"Should not fire for 3mm: {fired_safe}"

    # Free-text dissent never fires automatically
    observation_any = "machinist says it's too complex; rejecting."
    fired_freetext = check_dissents(observation_any, registry)
    assert len(fired_freetext) == 0, "Free-text dissent must not auto-fire"


# ---------------------------------------------------------------------------
# Test 3: must_haves as validator findings
# ---------------------------------------------------------------------------

def test_must_have_validator_findings():
    """must_haves surface as advisory findings; watchable ones noted; free-text flagged."""
    council_record = {
        "schema": COUNCIL_SCHEMA,
        "id": "cn_test002",
        "kind": "build_strategy",
        "topic": "test",
        "seats": [],
        "rounds": 2,
        "inputs": {},
        "positions": [
            {
                "seat": "manufacturing",
                "position": "3-setup plan",
                "risks": [],
                "must_haves": [
                    "datum faces machined in setup 1",              # free-text
                    "wall_thickness >= 2",                          # watchable predicate
                ],
            },
            {
                "seat": "tolerancing",
                "position": "shared datum",
                "risks": [],
                "must_haves": ["bore referenced to same datum as pattern"],  # free-text
            },
        ],
        "synthesis": {"decision": "", "rationale": "", "rejected_alternatives": []},
        "dissents": [],
        "chair": "department_manager",
    }

    findings = must_haves_to_validator_findings(council_record)
    assert len(findings) == 3, f"Expected 3 findings (one per must_have), got {len(findings)}"

    all_items = [f["items"][0] for f in findings]
    checks = [i["check"] for i in all_items]
    assert all(c == "council_must_have" for c in checks)

    # All have candidate fixes
    for item in all_items:
        assert len(item["candidate_fixes"]) >= 1

    # Watchable predicate refs flag watchable=True
    watchable_items = [i for i in all_items if i["refs"].get("watchable") is True]
    assert len(watchable_items) == 1, "Exactly one must_have is a watchable predicate"
    assert watchable_items[0]["refs"]["must_have"] == "wall_thickness >= 2"

    # Free-text ones flagged not watchable
    free_items = [i for i in all_items if i["refs"].get("watchable") is False]
    assert len(free_items) == 2


# ---------------------------------------------------------------------------
# Test 4: typed-position rejection
# ---------------------------------------------------------------------------

def test_typed_position_rejection():
    """Malformed seat output raises ValueError (not silently accepted)."""
    # Missing 'risks' key
    bad_reply = json.dumps({"position": "some strategy", "must_haves": []})
    with pytest.raises(ValueError, match="risks"):
        _parse_seat_reply(bad_reply, "manufacturing")

    # Extra key
    bad_extra = json.dumps({
        "position": "ok",
        "risks": [],
        "must_haves": [],
        "extra_key": "not_allowed",
    })
    with pytest.raises(ValueError, match="Extra keys"):
        _parse_seat_reply(bad_extra, "tolerancing")

    # Not a JSON object at all
    with pytest.raises(ValueError, match="no JSON object"):
        _parse_seat_reply("Here is my strategy in plain text.", "fitment")

    # Valid one passes
    good = _typed_position("ok strategy", ["risk1"], ["mh1"])
    pos, dissent = _parse_seat_reply(good, "simplicity")
    assert pos["seat"] == "simplicity"
    assert pos["position"] == "ok strategy"
    assert dissent is None


# ---------------------------------------------------------------------------
# Test 5: round-2 gets typed input (not chat history)
# ---------------------------------------------------------------------------

def test_round2_receives_typed_input():
    """Round-2 seat prompt includes round-1 positions as structured JSON."""
    captured_messages: list[list[dict]] = []

    class CapturingProvider:
        name = "mock"
        _call_count = 0
        _r1_pos = _typed_position("r1 strategy", [], [])
        _r2_pos = _typed_position("r2 strategy", [], [])

        def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
            self.__class__._call_count += 1
            captured_messages.append(messages)
            # Synthesis call
            if "CHAIR" in system or "synthesize" in system.lower():
                return json.dumps({
                    "decision": "ok",
                    "rationale": "ok",
                    "rejected_alternatives": [],
                })
            # Round detection: round 2 messages include prior positions
            user_text = messages[0]["content"][0]["text"] if messages else ""
            if "ROUND-1 POSITIONS" in user_text:
                return self._r2_pos
            return self._r1_pos

    provider = CapturingProvider()
    council_convene(
        kind="build_strategy",
        topic="test round-2 input",
        inputs={},
        provider=provider,
    )

    # Round-2 calls must have structured positions in their user text, not appended
    # chat messages (there should be only ONE user message per seat call)
    round2_msgs = [
        m for m in captured_messages
        if m and "ROUND-1 POSITIONS" in (m[0]["content"][0]["text"] if m else "")
    ]
    assert len(round2_msgs) == 4, f"Expected 4 round-2 calls, got {len(round2_msgs)}"

    for msgs in round2_msgs:
        # Must be exactly 1 message (not a growing chat history)
        assert len(msgs) == 1, (
            f"Round-2 seat should receive 1 message (typed input), got {len(msgs)}"
        )
        user_text = msgs[0]["content"][0]["text"]
        # round-1 positions are embedded as JSON (not raw chat)
        assert "ROUND-1 POSITIONS FROM OTHER SEATS" in user_text
        # The embedded content must be parseable JSON — find the FIRST
        # complete JSON array (bracket-matched, not rfind which overshoots
        # into the inline JSON examples later in the prompt text).
        start = user_text.find("[")
        if start != -1:
            depth = 0
            end = start
            for ci, ch in enumerate(user_text[start:], start):
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        end = ci + 1
                        break
            if depth == 0:
                parsed = json.loads(user_text[start:end])
                assert isinstance(parsed, list)


# ---------------------------------------------------------------------------
# Test 6: council record schema exact
# ---------------------------------------------------------------------------

def test_council_record_schema_exact():
    """Validate the record shape matches aicad.council/1 required fields."""
    provider = _make_provider_for_council(
        {p: _typed_position(f"{p} r1", [], []) for p in ("manufacturing", "tolerancing", "fitment", "simplicity")},
        {p: _typed_position(f"{p} r2", [], []) for p in ("manufacturing", "tolerancing", "fitment", "simplicity")},
        _SYNTHESIS,
    )
    record = council_convene(
        kind="build_strategy",
        topic="schema check",
        inputs={"ledger_refs": [], "buildgraph": None},
        provider=provider,
    )

    required_top = {"schema", "id", "kind", "topic", "seats", "rounds",
                    "inputs", "positions", "synthesis", "dissents", "chair"}
    assert required_top <= set(record.keys()), (
        f"Missing fields: {required_top - set(record.keys())}"
    )

    required_synth = {"decision", "rationale", "rejected_alternatives"}
    assert required_synth <= set(record["synthesis"].keys())

    for seat in record["seats"]:
        assert "persona" in seat
        assert "focus" in seat

    for pos in record["positions"]:
        ok, err = validate_typed_position({k: v for k, v in pos.items() if k != "seat"})
        assert ok, f"Position failed validation: {err} — {pos}"


# ---------------------------------------------------------------------------
# Test 7: ledger artifacts written
# ---------------------------------------------------------------------------

def test_ledger_artifacts_written():
    """Council writes artifact event (full record) and decision event (synthesis)."""
    events: list[dict] = []

    class FakeLedger:
        def append(self, actor: str, kind: str, payload: dict) -> dict:
            ev = {"actor": actor, "kind": kind, "payload": payload}
            events.append(ev)
            return ev

    provider = _make_provider_for_council(
        {p: _typed_position(f"{p} r1", [], []) for p in ("manufacturing", "tolerancing", "fitment", "simplicity")},
        {p: _typed_position(f"{p} r2", [], []) for p in ("manufacturing", "tolerancing", "fitment", "simplicity")},
        _SYNTHESIS,
    )
    record = council_convene(
        kind="build_strategy",
        topic="ledger test",
        inputs={},
        provider=provider,
        ledger=FakeLedger(),
        actor="test_actor",
    )

    artifact_events = [e for e in events if e["kind"] == "artifact"]
    decision_events = [e for e in events if e["kind"] == "decision"]

    # Full council record artifact
    council_artifacts = [
        a for a in artifact_events
        if a["payload"].get("kind") == "council"
    ]
    assert len(council_artifacts) == 1
    assert council_artifacts[0]["payload"]["council"]["id"] == record["id"]
    assert council_artifacts[0]["payload"]["id"] == record["id"]

    # Synthesis as decision with rejected_alternatives
    assert len(decision_events) == 1
    dec = decision_events[0]["payload"]
    assert "text" in dec
    assert "rationale" in dec
    assert "rejected_alternatives" in dec
    assert dec["council_id"] == record["id"]

    # Round-1 artifacts (one per seat)
    r1_artifacts = [
        a for a in artifact_events
        if a["payload"].get("kind") == "council_round1"
    ]
    assert len(r1_artifacts) == 4


# ---------------------------------------------------------------------------
# Test 8: Phase-3 gate
# ---------------------------------------------------------------------------

def test_build_strategy_needed():
    """Mission councils start at five features, and double-check can disable them."""
    assert build_strategy_needed(4, cfg={}) is False
    assert build_strategy_needed(5, cfg={}) is True
    assert build_strategy_needed(
        10, cfg={"copilot": {"double_check": False}},
    ) is False


def test_double_check_off_prevents_council_provider_calls():
    class Provider:
        def complete(self, *args, **kwargs):
            pytest.fail("Disabled reviews must not call a provider")

    record = council_convene(
        kind="build_strategy", topic="build a multi-part assembly",
        inputs={"feature_count": 8}, provider=Provider(),
        cfg={"copilot": {"double_check": False}},
    )
    assert record["complete"] is False
    assert record["status"] == "incomplete"
    assert record["diagnostics"]["policy"]["executed"] is False


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------

def test_extract_json_objects():
    """_extract_json_objects finds all top-level JSON objects in mixed text."""
    text = 'Some prose {"a": 1, "b": [1,2]} more text {"c": "hello"} end'
    objs = _extract_json_objects(text)
    assert len(objs) == 2
    assert objs[0] == {"a": 1, "b": [1, 2]}
    assert objs[1] == {"c": "hello"}

    assert _extract_json_objects("no json here") == []
    assert _extract_json_objects('{"nested": {"x": 1}}') == [{"nested": {"x": 1}}]


def test_is_watchable_condition():
    assert _is_watchable_condition("wall_thickness < 2mm") is True
    assert _is_watchable_condition("pocket_depth > 20") is True
    assert _is_watchable_condition("boss_od >= 25.5") is True
    assert _is_watchable_condition("if the machinist says it's too complex") is False
    assert _is_watchable_condition("revisit if wall looks thin") is False
    assert _is_watchable_condition("") is False


def test_trigger_fires():
    """_trigger_fires matches dimension values against predicates."""
    assert _trigger_fires("wall_thickness < 2", "wall_thickness=1.5mm applied") is True
    assert _trigger_fires("wall_thickness < 2", "wall_thickness=3.0mm on part") is False
    assert _trigger_fires("boss_od >= 25", "boss_od: 30.0") is True
    assert _trigger_fires("boss_od >= 25", "boss_od: 20.0") is False
    # No match on dimension name → False
    assert _trigger_fires("wall_thickness < 2", "pocket_depth=1.5mm") is False


def test_validate_typed_position_unit():
    """validate_typed_position unit test."""
    ok, _ = validate_typed_position({"position": "ok", "risks": [], "must_haves": []})
    assert ok

    ok, err = validate_typed_position({"position": "ok", "risks": []})
    assert not ok
    assert "must_haves" in err

    ok, err = validate_typed_position({"position": 123, "risks": [], "must_haves": []})
    assert not ok
    assert "string" in err

    ok, err = validate_typed_position({"position": "ok", "risks": [], "must_haves": [], "extra": "x"})
    assert not ok
    assert "Extra" in err


class _CouncilMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args, **kwargs):
        def decorate(fn):
            self.tools[kwargs.get("name") or fn.__name__] = fn
            return fn
        return decorate


def test_mcp_council_convene_uses_bound_mission_context():
    """The registered council tool reaches the provider bound for this mission."""
    from types import SimpleNamespace
    from services.mcp import org_tools

    calls: list[str] = []

    class Provider:
        reasoning_effort = "medium"

        def complete(self, system: str, messages: list[dict],
                     max_tokens: int = 8000, **kwargs) -> str:
            calls.append(system)
            if "CHAIR" in system:
                return _SYNTHESIS
            return _typed_position("bound provider reached", [], [])

    mcp = _CouncilMCP()
    org_tools.register_org_tools(mcp)
    org_tools.bind_mission_context(SimpleNamespace(
        provider=Provider(), ledger=None, dissents_registry=None,
        stage=None, tier=None, providers=None,
    ))
    try:
        record = mcp.tools["council_convene"](
            kind="build_strategy", topic="bound path", inputs={})
    finally:
        org_tools.bind_mission_context(None)

    assert record["schema"] == COUNCIL_SCHEMA
    assert record["status"] == "complete"
    assert len(calls) == 9


def test_mcp_council_convene_refuses_without_mission_context():
    """The global MCP fallback reports a non-executed policy refusal."""
    from services.mcp import org_tools

    mcp = _CouncilMCP()
    org_tools.register_org_tools(mcp)
    org_tools.bind_mission_context(None)
    refused = mcp.tools["council_convene"](
        kind="build_strategy", topic="outside mission", inputs={})
    assert refused == {
        "ok": False,
        "policy_refusal": True,
        "executed": False,
        "status": "no_mission_context",
        "error": (
            "council_convene is only available inside a mission turn with "
            "a bound provider."
        ),
    }



def test_chair_policy_refusal_marks_council_incomplete_without_decision_event():
    """A refusal envelope is diagnostic data, never a persisted council decision."""
    events: list[dict] = []

    class Ledger:
        def append(self, actor: str, kind: str, payload: dict) -> dict:
            event = {"actor": actor, "kind": kind, "payload": payload}
            events.append(event)
            return event

    provider = _make_provider_for_council(
        {p: _typed_position(f"{p} r1", [], []) for p in (
            "manufacturing", "tolerancing", "fitment", "simplicity")},
        {p: _typed_position(f"{p} r2", [], []) for p in (
            "manufacturing", "tolerancing", "fitment", "simplicity")},
        json.dumps({
            "ok": False,
            "policy_refusal": True,
            "executed": False,
            "status": "policy_refusal",
            "error": "chair declined by policy",
        }),
    )
    record = council_convene(
        kind="build_strategy", topic="refusal", inputs={},
        provider=provider, ledger=Ledger())

    assert record["status"] == "incomplete"
    assert record["complete"] is False
    assert record["synthesis"]["decision"] == ""
    assert record["diagnostics"]["chair"]["status"] == "refused"
    assert record["diagnostics"]["chair"]["outcome_kind"] == "policy_refusal"
    assert "policy_refusal" in record["diagnostics"]["chair"]["raw_reply"]
    assert not [event for event in events if event["kind"] == "decision"]
    assert len([
        event for event in events
        if event["kind"] == "artifact"
        and event["payload"].get("kind") == "council"
    ]) == 1


def test_diagnosis_preserves_first_seat_when_second_seat_provider_call_fails():
    """A mid-round provider fault returns and persists an explicit partial council."""
    from services.agent.regen_diagnosis import diagnose

    calls: list[str] = []
    events: list[dict] = []

    class Provider:
        reasoning_effort = "medium"

        def complete(self, system: str, messages: list[dict],
                     max_tokens: int = 8000, **kwargs) -> str:
            if "CAD regeneration-repair" in system:
                return json.dumps({"diagnosis": "dangling ref", "fix_ops": []})
            calls.append(system)
            if len(calls) == 1:
                return _typed_position("manufacturing seat completed", [], [])
            raise RuntimeError("seat-2 provider unavailable")

    class Ledger:
        def append(self, actor: str, kind: str, payload: dict) -> dict:
            event = {"actor": actor, "kind": kind, "payload": payload}
            events.append(event)
            return event

    result = diagnose(
        {"tree": {"features": []}},
        Provider(),
        wrap_logging=False,
        council_tier="local-24k",
        ledger=Ledger(),
    )

    council = result.council_record
    assert council is not None
    assert council["status"] == "incomplete"
    assert council["complete"] is False
    assert council["seat_outcomes"][0]["status"] == "executed"
    assert council["seat_outcomes"][0]["outcome_kind"] == "ok"
    assert council["seat_outcomes"][0]["position"]["position"] == (
        "manufacturing seat completed")
    assert council["seat_outcomes"][1]["status"] == "failed"
    assert council["seat_outcomes"][1]["outcome_kind"] == "internal_error"
    assert council["seat_outcomes"][1]["executed"] is False
    assert "seat-2 provider unavailable" in council["seat_outcomes"][1]["error"]
    artifacts = [
        event for event in events
        if event["kind"] == "artifact"
        and event["payload"].get("kind") == "council"
    ]
    assert len(artifacts) == 1
    assert artifacts[0]["payload"]["council"]["status"] == "incomplete"


def test_council_normalizes_provider_reasoning_effort_without_mutating_ambient_provider():
    """Seat and chair calls see a normalized effort on a copy; the caller's provider is untouched.

    Top rungs (max/xhigh) pass through since 7fda58df0; the RPC boundary clamps per model.
    """
    observed: list[str] = []

    class Provider:
        reasoning_effort = "med"

        def complete(self, system: str, messages: list[dict],
                     max_tokens: int = 8000, **kwargs) -> str:
            observed.append(self.reasoning_effort)
            if "CHAIR" in system:
                return _SYNTHESIS
            return _typed_position("safe effort", [], [])

    provider = Provider()
    record = council_convene(
        kind="build_strategy", topic="effort", inputs={}, provider=provider)

    assert record["status"] == "complete"
    assert observed == ["medium"] * 9
    assert provider.reasoning_effort == "med"




def test_regen_council_harness_exception_returns_persisted_incomplete_marker(
    monkeypatch, caplog,
):
    """An unexpected council harness fault is observable and never becomes None."""
    from services.agent import councils
    from services.agent.regen_diagnosis import _maybe_convene_fix_council

    def fail_convene(**kwargs):
        raise RuntimeError("council harness exploded")

    monkeypatch.setattr(councils, "council_convene", fail_convene)
    events: list[dict] = []

    class Ledger:
        def append(self, actor: str, kind: str, payload: dict) -> dict:
            event = {"actor": actor, "kind": kind, "payload": payload}
            events.append(event)
            return event

    with caplog.at_level("ERROR"):
        record = _maybe_convene_fix_council(
            diagnosis_text="diagnosis",
            fix_ops=[],
            failures=[],
            provider=object(),
            tier="local-24k",
            ledger=Ledger(),
        )

    assert record is not None
    assert record["status"] == "incomplete"
    assert record["complete"] is False
    diagnostic = record["diagnostics"]["council_exception"]
    assert diagnostic["outcome_kind"] == "internal_error"
    assert diagnostic["executed"] is False
    assert "council harness exploded" in diagnostic["error"]
    assert any(item.exc_info for item in caplog.records)
    artifacts = [
        event for event in events
        if event["kind"] == "artifact"
        and event["payload"].get("kind") == "council"
    ]
    assert len(artifacts) == 1


def test_linear_turn_setup_binds_real_council_tool_to_run_provider(
    monkeypatch,
):
    """The production turn seam binds the global council closure only for the run."""
    from services.agent import agent_turn, mission_dispatch
    from services.mcp import org_tools

    provider_calls: list[str] = []
    captured: dict[str, dict] = {}

    class Provider:
        reasoning_effort = "medium"

        def complete(self, system: str, messages: list[dict],
                     max_tokens: int = 8000, **kwargs) -> str:
            provider_calls.append(system)
            if "CHAIR" in system:
                return _SYNTHESIS
            return _typed_position("live turn provider reached", [], [])

    class Result:
        success = True
        stop_reason = "confirmed"
        result = {"handoff": {"status": "done", "summary": "council ran"}}

    def fake_run_turn(*args, **kwargs):
        bound = agent_turn._bind_tool("council_convene")
        assert bound is not None
        captured["record"] = bound(
            kind="build_strategy",
            topic="production turn binding",
            inputs={},
        )
        return Result()

    monkeypatch.setattr(agent_turn, "run_turn", fake_run_turn)
    org_tools.bind_mission_context(None)
    mission_dispatch.run_linear_role_turn(
        "convene the council",
        "department_manager",
        Provider(),
        run_id="council-bind-test",
        ledger=MagicMock(),
    )

    assert captured["record"]["schema"] == COUNCIL_SCHEMA
    assert captured["record"]["status"] == "complete"
    assert len(provider_calls) == 9
    assert org_tools.MISSION_CONTEXT is None

    def fail_inside_turn(*args, **kwargs):
        assert org_tools.MISSION_CONTEXT is not None
        raise RuntimeError("turn failed")

    monkeypatch.setattr(agent_turn, "run_turn", fail_inside_turn)
    with pytest.raises(RuntimeError, match="turn failed"):
        mission_dispatch.run_linear_role_turn(
            "fail after binding",
            "department_manager",
            Provider(),
            run_id="council-bind-failure-test",
            ledger=MagicMock(),
        )
    assert org_tools.MISSION_CONTEXT is None
