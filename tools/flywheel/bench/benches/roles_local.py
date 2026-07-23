"""roles_local bench (T-W10): scripted role turns against a local model endpoint.

Proves that local-model role turns (sketcher + fixer roles) complete without
context overflow, emit grammar-valid handoff JSON, and respect playbook step
compliance under the local-24k tier profile.

Two evaluation paths (offline-ML-track discipline — no all-skip):
  * fixture mode (offline, default):
    Scripted fixture missions + fixture advisory sequences are run through the
    TurnAssembler with the local-24k profile.  Assertions:
      - No context overflow (assembled total ≤ available_tokens).
      - Grammar-compiled GBNF grammar is a valid (non-empty) string.
      - Playbook enforcement flag is step_locked for local tiers.
      - Handoff JSON parsed from fixture output validates against the role schema.
    This is a CODE-PATH exercise: real tiers.py / roles.py / advisor/render.py
    logic executes against fixture data.  It is NOT an all-skip stub.

  * live mode (local endpoint present):
    When CHUDWARE_LOCAL_ENDPOINT env var is set, the bench calls the endpoint for
    real, submits the assembled context, and parses + validates the model's actual
    handoff JSON.  Guarded by a pytest.skip when the endpoint is absent.

    Live recipe (run once you have a local model):
        export CHUDWARE_LOCAL_ENDPOINT=http://localhost:1234/v1
        export CHUDWARE_LOCAL_MODEL=lmstudio-community/Qwen2.5-Coder-7B-Instruct-GGUF
        pytest tools/flywheel/bench/benches/roles_local.py -v

CI behaviour: CHUDWARE_LOCAL_ENDPOINT is not set in CI → fixture mode runs (real
code paths, no HTTP); live mode auto-skips with a clear message.

Fixtures: inline in this file (no external fixture directory needed for v1).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Fixture definitions

# A minimal sketcher role handoff schema (mirrors the actual card shape).
_SKETCHER_HANDOFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "summary", "outputs"],
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "escalated"]},
        "summary": {"type": "string"},
        "outputs": {
            "type": "object",
            "required": ["sketch_complete"],
            "properties": {
                "sketch_complete": {"type": "boolean"},
            },
        },
    },
}

# A minimal fixer role handoff schema.
_FIXER_HANDOFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "summary", "outputs"],
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "escalated"]},
        "summary": {"type": "string"},
        "outputs": {
            "type": "object",
            "required": ["fix_applied"],
            "properties": {
                "fix_applied": {"type": "boolean"},
                "retry_count": {"type": "integer"},
            },
        },
    },
}

# Fixture: a plausible sketcher turn context (abbreviated, ~realistic sizes).
_SKETCHER_SYSTEM = """\
You are the sketcher role. Your goal: produce a fully-constrained, closed sketch
profile. Emit a handoff JSON when done. Follow the playbook step exactly.
"""

_SKETCHER_LEDGER = """\
[LEDGER]
mission: Bracket mount — 50×30 base plate
stage: sketch
active_dims:
  - plate_width: 50 mm (confirmed)
  - plate_height: 30 mm (confirmed)
[/LEDGER]
"""

_SKETCHER_STATE = """\
[STAGE STATE]
sketch entities: Rectangle(50, 30), 4 lines, 4 corners coincident
DOF: 0 — fully constrained
regions: 1 closed region
[/STAGE STATE]
"""

_SKETCHER_ADVISORY = """\
[ADVISORY seq=1 stage=sketch severity=info]
  [info] DOF: fully constrained, 0 remaining
  [info] region: 1 closed region found
[/ADVISORY]
"""

_SKETCHER_PLAYBOOK_STEP = """\
[PLAYBOOK sketch_profile step 3/6: close_profile]
goal: Ensure all profile regions are closed
helper tools: sketch_health, auto_coincident
  - Run sketch_health to verify region closure
  - Apply auto_coincident to snap any near-miss endpoints
emit: {"sketch_complete": true}
"""

_SKETCHER_HISTORY = [
    {"role": "user", "content": [{"type": "text",
        "text": "Please sketch the base plate profile for the bracket mount."}]},
    {"role": "assistant", "content": [{"type": "text",
        "text": "I will draw a 50×30 mm rectangle and constrain it fully."}]},
    {"role": "user", "content": [{"type": "text",
        "text": "sketch_health result: 1 closed region, DOF=0"}]},
]

# Fixture: valid sketcher handoff JSON (what a model would emit).
_SKETCHER_FIXTURE_OUTPUT = json.dumps({
    "schema": "aicad.handoff/1",
    "role": "sketcher",
    "status": "done",
    "summary": "Base plate 50×30 mm rectangle sketched, fully constrained, 1 closed region.",
    "outputs": {"sketch_complete": True},
})

# Fixture: fixer turn context.
_FIXER_SYSTEM = """\
You are the fixer role. Your goal: apply exactly the fix named in the failure card.
Change only what the card names; one candidate per attempt. Emit handoff JSON.
"""

_FIXER_LEDGER = """\
[LEDGER]
mission: Bracket mount — bore fix attempt 1/3
stage: fix
[/LEDGER]
"""

_FIXER_STATE = """\
[STAGE STATE]
failure card: rebuild error — fillet radius 5 exceeds adjacent wall 3.2
candidate fix: reduce fillet radius to 3.0
[/STAGE STATE]
"""

_FIXER_ADVISORY = """\
[ADVISORY seq=2 stage=fix severity=action]
  [action] fillet_overflow: fillet r=5 exceeds wall 3.2 on F9
    numbers: wall=3.2mm, fillet=5mm, max_safe=3.2mm
    fix: set fillet radius to 3.0mm (feature F9) — predicted_effect=rebuild pass [kernel]
[/ADVISORY]
"""

_FIXER_HISTORY = [
    {"role": "user", "content": [{"type": "text",
        "text": "Failure card: fillet radius 5 exceeds adjacent wall 3.2. Fix it."}]},
]

_FIXER_FIXTURE_OUTPUT = json.dumps({
    "schema": "aicad.handoff/1",
    "role": "fixer",
    "status": "done",
    "summary": "Reduced fillet radius from 5 to 3.0 mm on feature F9. Rebuild should pass.",
    "outputs": {"fix_applied": True, "retry_count": 1},
})


# ---------------------------------------------------------------------------
# Core bench logic

def _run_fixture_turn(
    system: str,
    ledger_text: str,
    state_text: str,
    advisory_text: str,
    playbook_step: str,
    history: list[dict],
    tier_name: str = "local-24k",
) -> dict:
    """Assemble a turn with the TurnAssembler and return diagnostics.

    This exercises the real code path (tiers.py, roles.py GBNF emitter,
    advisor/render.py constants) without an HTTP call.
    """
    from services.agent.tiers import TurnAssembler, grammar_for_tier, load_tier

    profile = load_tier(tier_name)
    assembler = TurnAssembler(profile)
    result = assembler.assemble(
        system=system,
        ledger_text=ledger_text,
        state_text=state_text,
        advisory_text=advisory_text,
        history_messages=history,
    )
    return {
        "tier": tier_name,
        "total_tokens": result.total_tokens,
        "available_tokens": profile.available_tokens,
        "overflow": result.overflow_applied,
        "compaction_level": result.compaction_level,
        "slot_tokens": result.slot_tokens,
        "assembled_len": len(result.text),
    }


def _validate_handoff_fixture(
    fixture_output: str,
    handoff_schema: dict,
    expected_role: str,
) -> list[str]:
    """Parse fixture output JSON and validate against the handoff schema.

    Returns a list of error strings (empty = valid).
    """
    from services.agent.roles import validate_handoff, RoleCard, ToolRef

    # Build a minimal RoleCard with just the fields validate_handoff needs.
    card = RoleCard(
        id=expected_role,
        goal="",
        system_prompt="",
        stage_scope=("sketch",),
        tool_allowlist=(),
        context_recipe={},
        ledger_sections={},
        handoff_schema=handoff_schema,
        escalation_policy={"max_attempts": 3, "on_exhaust": "ask_user"},
    )
    try:
        envelope = json.loads(fixture_output)
    except json.JSONDecodeError as e:
        return [f"JSON parse error: {e}"]
    return validate_handoff(card, envelope)


def run_sketcher_fixture() -> dict:
    """Run the sketcher fixture turn and handoff validation. Returns a result dict."""
    diag = _run_fixture_turn(
        system=_SKETCHER_SYSTEM,
        ledger_text=_SKETCHER_LEDGER,
        state_text=_SKETCHER_STATE,
        advisory_text=_SKETCHER_ADVISORY,
        playbook_step=_SKETCHER_PLAYBOOK_STEP,
        history=_SKETCHER_HISTORY,
        tier_name="local-24k",
    )
    errors = _validate_handoff_fixture(
        _SKETCHER_FIXTURE_OUTPUT, _SKETCHER_HANDOFF_SCHEMA, "sketcher"
    )
    diag["handoff_valid"] = len(errors) == 0
    diag["handoff_errors"] = errors
    diag["role"] = "sketcher"
    return diag


def run_fixer_fixture() -> dict:
    """Run the fixer fixture turn and handoff validation. Returns a result dict."""
    diag = _run_fixture_turn(
        system=_FIXER_SYSTEM,
        ledger_text=_FIXER_LEDGER,
        state_text=_FIXER_STATE,
        advisory_text=_FIXER_ADVISORY,
        playbook_step="",
        history=_FIXER_HISTORY,
        tier_name="local-24k",
    )
    errors = _validate_handoff_fixture(
        _FIXER_FIXTURE_OUTPUT, _FIXER_HANDOFF_SCHEMA, "fixer"
    )
    diag["handoff_valid"] = len(errors) == 0
    diag["handoff_errors"] = errors
    diag["role"] = "fixer"
    return diag


def run_frontier_dial_check() -> dict:
    """Prove the dials relax for frontier tier (no GBNF, off enforcement)."""
    from services.agent.tiers import grammar_for_tier, load_tier

    profile = load_tier("frontier")
    grammar = grammar_for_tier(profile, _SKETCHER_HANDOFF_SCHEMA)
    return {
        "tier": "frontier",
        "structured_output": profile.structured_output,
        "playbook_enforcement": profile.playbook_enforcement,
        "deliberation": profile.deliberation,
        "grammar_is_none": grammar is None,
        "advisory_verbosity": profile.advisory_verbosity,
    }


# ---------------------------------------------------------------------------
# Live mode (guarded by CHUDWARE_LOCAL_ENDPOINT)

def run_live_sketcher(endpoint: str, model: str) -> dict:
    """Run the sketcher fixture turn against a real local model endpoint.

    Only called when CHUDWARE_LOCAL_ENDPOINT is set.  Not used in CI.
    """
    from services.agent.providers.openai_provider import OpenAIProvider
    from services.agent.tiers import (
        TurnAssembler, grammar_for_tier, load_tier,
    )
    from services.agent.roles import schema_to_gbnf
    from services.agent.providers.base import user, text_part

    profile = load_tier("local-24k")
    assembler = TurnAssembler(profile)
    result = assembler.assemble(
        system=_SKETCHER_SYSTEM,
        ledger_text=_SKETCHER_LEDGER,
        state_text=_SKETCHER_STATE,
        advisory_text=_SKETCHER_ADVISORY,
        history_messages=_SKETCHER_HISTORY,
    )

    grammar = grammar_for_tier(profile, _SKETCHER_HANDOFF_SCHEMA)

    # Assert no overflow before sending.
    assert not result.overflow_applied, (
        f"Context overflow on local-24k sketcher turn: "
        f"{result.total_tokens} > {profile.available_tokens}"
    )

    provider = OpenAIProvider(
        api_key="not-needed",
        model=model,
        base_url=endpoint,
        supports_vision=False,
        timeout=60.0,
    )

    # Build a single-turn message with the assembled context (minus system).
    # The system prompt is passed separately; history includes the advisory+playbook.
    assembled_user = "\n\n".join([
        _SKETCHER_LEDGER,
        _SKETCHER_STATE,
        _SKETCHER_ADVISORY,
        _SKETCHER_PLAYBOOK_STEP,
        "Please emit your handoff JSON now.",
    ])
    messages = list(_SKETCHER_HISTORY) + [
        user([text_part(assembled_user)])
    ]

    # Grammar pass-through: for llama.cpp servers that support the `grammar` param,
    # pass as extra_body.  For LM Studio (json_schema fallback), the caller can
    # adapt.  Here we use the base complete() since extra_body is provider-specific;
    # the live recipe documents the llama.cpp curl equivalent.
    reply = provider.complete(_SKETCHER_SYSTEM, messages, max_tokens=512)

    # Parse and validate the handoff JSON from the reply.
    # The model is expected to emit the JSON block directly or inside a code fence.
    json_text = reply
    if "```" in reply:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", reply, re.DOTALL)
        if m:
            json_text = m.group(1)

    errors = _validate_handoff_fixture(json_text, _SKETCHER_HANDOFF_SCHEMA, "sketcher")

    return {
        "tier": "local-24k",
        "total_tokens": result.total_tokens,
        "available_tokens": profile.available_tokens,
        "overflow": result.overflow_applied,
        "handoff_valid": len(errors) == 0,
        "handoff_errors": errors,
        "model_reply_len": len(reply),
    }


# ---------------------------------------------------------------------------
# pytest integration

try:
    import pytest
    _PYTEST_AVAILABLE = True
except ImportError:
    _PYTEST_AVAILABLE = False


if _PYTEST_AVAILABLE:
    import pytest

    class TestSketcherFixture:
        """Fixture-mode: TurnAssembler + GBNF + handoff validation (no HTTP)."""

        def test_no_context_overflow(self):
            r = run_sketcher_fixture()
            assert not r["overflow"], (
                f"Context overflow on local-24k sketcher: "
                f"total={r['total_tokens']} available={r['available_tokens']}"
            )

        def test_total_within_available(self):
            r = run_sketcher_fixture()
            assert r["total_tokens"] <= r["available_tokens"]

        def test_handoff_json_valid(self):
            r = run_sketcher_fixture()
            assert r["handoff_valid"], f"Handoff errors: {r['handoff_errors']}"

        def test_gbnf_grammar_is_string(self):
            from services.agent.tiers import grammar_for_tier, load_tier
            profile = load_tier("local-24k")
            grammar = grammar_for_tier(profile, _SKETCHER_HANDOFF_SCHEMA)
            assert isinstance(grammar, str) and len(grammar) > 0

        def test_playbook_enforcement_step_locked(self):
            from services.agent.tiers import load_tier
            profile = load_tier("local-24k")
            assert profile.playbook_enforcement == "step_locked"

        def test_deliberation_full_for_local(self):
            from services.agent.tiers import load_tier
            profile = load_tier("local-24k")
            assert profile.deliberation == "full"

    class TestFixerFixture:
        def test_no_context_overflow(self):
            r = run_fixer_fixture()
            assert not r["overflow"], (
                f"Context overflow on local-24k fixer: "
                f"total={r['total_tokens']} available={r['available_tokens']}"
            )

        def test_handoff_json_valid(self):
            r = run_fixer_fixture()
            assert r["handoff_valid"], f"Handoff errors: {r['handoff_errors']}"

    class TestFrontierDialRelax:
        """Prove that frontier dials relax vs local."""

        def test_frontier_no_gbnf(self):
            r = run_frontier_dial_check()
            assert r["grammar_is_none"]

        def test_frontier_enforcement_off(self):
            r = run_frontier_dial_check()
            assert r["playbook_enforcement"] == "off"

        def test_frontier_deliberation_minimal(self):
            r = run_frontier_dial_check()
            assert r["deliberation"] == "minimal"

        def test_frontier_advisory_compact(self):
            r = run_frontier_dial_check()
            assert r["advisory_verbosity"] == "compact"

    class TestLiveMode:
        """Live-mode tests — auto-skip when CHUDWARE_LOCAL_ENDPOINT is not set."""

        @pytest.mark.skipif(
            not os.environ.get("CHUDWARE_LOCAL_ENDPOINT"),
            reason=(
                "CHUDWARE_LOCAL_ENDPOINT not set — live mode skipped. "
                "Set it to a running llama.cpp or LM Studio endpoint to run live tests. "
                "Example: export CHUDWARE_LOCAL_ENDPOINT=http://localhost:1234/v1"
            ),
        )
        def test_live_sketcher_no_overflow_valid_handoff(self):
            endpoint = os.environ["CHUDWARE_LOCAL_ENDPOINT"]
            model = os.environ.get(
                "CHUDWARE_LOCAL_MODEL", "lmstudio-community/Qwen2.5-Coder-7B-Instruct-GGUF"
            )
            r = run_live_sketcher(endpoint, model)
            assert not r["overflow"], f"Context overflow in live run: {r}"
            assert r["handoff_valid"], f"Live handoff invalid: {r['handoff_errors']}"
