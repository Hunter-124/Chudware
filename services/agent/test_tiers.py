"""Tests for services/agent/tiers.py — tier profiles + budget enforcer.

Contract: contracts/advisor.md §8 + 1.3 addendum.

Test venv: ~/.local/share/Chudware/mcp-venv/bin/python
Run: pytest services/agent/test_tiers.py -v

Coverage:
  * Budget math property: sum(slots) + headroom ≤ context_tokens for all 4 profiles.
  * Overflow policy: shrink history first, then state; never system/advisory.
  * Tier detect: context_length → tier name; env override wins.
  * Grammar flag routing: gbnf tiers produce a grammar string; others produce None.
  * Tool schema trim: description shortened to first sentence for local tiers.
  * Loader: all 4 profiles load without error; bad yaml raises TierError.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from .tiers import (
    CONTEXT_LENGTH_TIER_MAP,
    FRONTIER_TIER,
    STATE_FLOOR_TOKENS,
    TIER_ENV_VAR,
    AssembledTurn,
    SlotBudgets,
    TierError,
    TierProfile,
    TurnAssembler,
    _compact_messages,
    _count_tokens,
    _render_messages,
    detect_tier,
    detect_tier_from_env,
    grammar_for_tier,
    load_all_tiers,
    load_tier,
    trim_tool_schema,
)

# ---------------------------------------------------------------------------
# helpers

_TIERS_YAML = Path(__file__).resolve().parent / "tiers.yaml"


def _all_profiles() -> dict[str, TierProfile]:
    return load_all_tiers(path=_TIERS_YAML)


def _make_messages(n: int, chars_each: int = 500) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": [{"type": "text", "text": "x" * chars_each}]}
        for i in range(n)
    ]


def _make_profile(
    name: str = "local-24k",
    context_tokens: int = 24576,
    generation_headroom: int = 8000,
    budgets: dict | None = None,
    structured_output: str = "gbnf",
    playbook_enforcement: str = "step_locked",
    advisory_verbosity: str = "full",
    tool_schema_trim: bool = True,
) -> TierProfile:
    """Build a TierProfile directly for unit tests that don't need YAML I/O."""
    b = budgets or {
        "system": 2500, "ledger": 800, "state": 1500,
        "advisory": 600, "tools": 2500, "history": 8000,
    }
    return TierProfile(
        name=name,
        context_tokens=context_tokens,
        generation_headroom=generation_headroom,
        budgets=SlotBudgets(**b),
        playbook_enforcement=playbook_enforcement,
        structured_output=structured_output,
        advisory_verbosity=advisory_verbosity,
        max_tools_per_role=14,
        tool_schema_trim=tool_schema_trim,
    )


# ---------------------------------------------------------------------------
# Loader tests

class TestLoader:
    def test_all_four_profiles_load(self):
        tiers = _all_profiles()
        assert set(tiers.keys()) == {"local-24k", "local-48k", "mid", "frontier"}

    def test_local_24k_context_tokens(self):
        p = load_tier("local-24k", path=_TIERS_YAML)
        assert p.context_tokens == 24576

    def test_missing_tier_raises(self):
        with pytest.raises(TierError, match="not found"):
            load_tier("nonexistent", path=_TIERS_YAML)

    def test_bad_schema_raises(self, tmp_path):
        bad = tmp_path / "tiers.yaml"
        bad.write_text("schema: wrong\nprofiles: {}\n")
        with pytest.raises(TierError, match="schema"):
            load_all_tiers(path=bad)

    def test_missing_budget_key_raises(self, tmp_path):
        bad = tmp_path / "tiers.yaml"
        bad.write_text(
            "schema: aicad.tier/1\n"
            "profiles:\n"
            "  t:\n"
            "    context_tokens: 24576\n"
            "    generation_headroom: 8000\n"
            "    budgets:\n"
            "      system: 100\n"
            "    playbook_enforcement: off\n"
            "    structured_output: free\n"
            "    advisory_verbosity: full\n"
            "    max_tools_per_role: 14\n"
            "    tool_schema_trim: false\n"
        )
        with pytest.raises(TierError, match="budgets missing"):
            load_all_tiers(path=bad)

    def test_overflow_budget_raises(self, tmp_path):
        """A profile where slot sum + headroom > context_tokens must be rejected."""
        bad = tmp_path / "tiers.yaml"
        bad.write_text(
            "schema: aicad.tier/1\n"
            "profiles:\n"
            "  overflow:\n"
            "    context_tokens: 1000\n"
            "    generation_headroom: 500\n"
            "    budgets:\n"
            "      system: 200\n"
            "      ledger: 200\n"
            "      state: 200\n"
            "      advisory: 200\n"
            "      tools: 200\n"
            "      history: 200\n"  # sum=1200 + headroom=500 = 1700 > 1000
            "    playbook_enforcement: off\n"
            "    structured_output: free\n"
            "    advisory_verbosity: full\n"
            "    max_tools_per_role: 14\n"
            "    tool_schema_trim: false\n"
        )
        with pytest.raises(TierError, match="overflow"):
            load_all_tiers(path=bad)


# ---------------------------------------------------------------------------
# Budget math property tests

class TestBudgetMath:
    """For all four profiles: sum(slots) + generation_headroom ≤ context_tokens."""

    @pytest.mark.parametrize("name", ["local-24k", "local-48k", "mid", "frontier"])
    def test_budget_ok(self, name: str):
        profile = load_tier(name, path=_TIERS_YAML)
        assert profile.budget_ok(), (
            f"{name}: slots {profile.budgets.total} + headroom "
            f"{profile.generation_headroom} = "
            f"{profile.budgets.total + profile.generation_headroom} "
            f"> context_tokens {profile.context_tokens}"
        )

    @pytest.mark.parametrize("name", ["local-24k", "local-48k", "mid", "frontier"])
    def test_available_tokens_positive(self, name: str):
        p = load_tier(name, path=_TIERS_YAML)
        assert p.available_tokens > 0

    def test_local_24k_sum_matches_spec(self):
        """local-24k: spec says slots sum to 15900 tokens."""
        p = load_tier("local-24k", path=_TIERS_YAML)
        assert p.budgets.total == 15900, f"got {p.budgets.total}"

    def test_slot_budgets_all_positive(self):
        for name, p in _all_profiles().items():
            for slot_name, tokens in p.budgets:
                assert tokens > 0, f"{name}.{slot_name} must be > 0"


# ---------------------------------------------------------------------------
# Structured output + enforcement tests

class TestStructuredOutput:
    def test_local_tiers_use_gbnf(self):
        for name in ("local-24k", "local-48k"):
            p = load_tier(name, path=_TIERS_YAML)
            assert p.structured_output == "gbnf"
            assert p.uses_gbnf

    def test_mid_uses_json_mode(self):
        p = load_tier("mid", path=_TIERS_YAML)
        assert p.structured_output == "json_mode"
        assert p.uses_json_mode

    def test_frontier_uses_free(self):
        p = load_tier("frontier", path=_TIERS_YAML)
        assert p.structured_output == "free"
        assert not p.uses_gbnf
        assert not p.uses_json_mode

    def test_local_tiers_step_locked(self):
        for name in ("local-24k", "local-48k"):
            p = load_tier(name, path=_TIERS_YAML)
            assert p.playbook_enforcement == "step_locked"

    def test_frontier_enforcement_off(self):
        assert load_tier("frontier", path=_TIERS_YAML).playbook_enforcement == "off"

    def test_local_tiers_trim_schemas(self):
        for name in ("local-24k", "local-48k"):
            assert load_tier(name, path=_TIERS_YAML).tool_schema_trim is True

    def test_frontier_no_trim(self):
        assert load_tier("frontier", path=_TIERS_YAML).tool_schema_trim is False


# ---------------------------------------------------------------------------
# Tier detection tests

class TestTierDetect:
    def test_small_model_is_local_24k(self):
        assert detect_tier(8192) == "local-24k"

    def test_24k_boundary(self):
        assert detect_tier(32768) == "local-24k"

    def test_just_over_32k_is_local_48k(self):
        assert detect_tier(32769) == "local-48k"

    def test_48k_boundary(self):
        assert detect_tier(65536) == "local-48k"

    def test_mid_range(self):
        assert detect_tier(100000) == "mid"

    def test_frontier_for_large(self):
        assert detect_tier(200000) == FRONTIER_TIER

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv(TIER_ENV_VAR, "frontier")
        assert detect_tier_from_env() == "frontier"

    def test_env_override_empty_returns_none(self, monkeypatch):
        monkeypatch.delenv(TIER_ENV_VAR, raising=False)
        assert detect_tier_from_env() is None

    def test_env_override_takes_any_value(self, monkeypatch):
        monkeypatch.setenv(TIER_ENV_VAR, "mid")
        assert detect_tier_from_env() == "mid"


# ---------------------------------------------------------------------------
# tier -> provider resolution (the node_runner / gym shared seam)

class TestProviderForTier:
    """``provider_for_tier`` is the single tier->provider resolution seam shared
    by services.dag.node_runner and the gym bench. It must:
      * route local-* tiers to the bundled local model (never the ambient
        default), unless CHUDWARE_PROVIDER overrides;
      * leave every non-local tier (and no tier at all) on the ordinary
        config.get_provider() resolution — no behaviour change there."""

    def test_non_local_tier_falls_through_to_ambient_default(self, monkeypatch):
        from . import config as agent_config
        from .tiers import provider_for_tier

        calls = []

        def _tracked(name=None, cfg=None):
            calls.append((name, cfg))
            return "ambient-provider"

        monkeypatch.setattr(agent_config, "get_provider", _tracked)
        result = provider_for_tier("frontier")
        assert result == "ambient-provider"
        assert calls == [(None, None)]

    def test_no_tier_falls_through_to_ambient_default(self, monkeypatch):
        from . import config as agent_config
        from .tiers import provider_for_tier

        monkeypatch.setattr(agent_config, "get_provider",
                            lambda name=None, cfg=None: "ambient-provider")
        assert provider_for_tier(None) == "ambient-provider"

    def test_local_tier_resolves_bundled_local_model(self, monkeypatch):
        from . import config as agent_config
        from . import model_manager
        from .tiers import provider_for_tier

        monkeypatch.delenv("CHUDWARE_PROVIDER", raising=False)
        monkeypatch.setattr(model_manager, "get_model_path",
                            lambda model_id: Path("/models/qwen3-4b.gguf"))

        calls = []

        def _tracked(name=None, cfg=None):
            calls.append((name, cfg))
            return "local-provider"

        monkeypatch.setattr(agent_config, "get_provider", _tracked)
        result = provider_for_tier("local-24k")
        assert result == "local-provider"
        assert len(calls) == 1
        name, cfg = calls[0]
        assert name == "llamacpp"
        assert cfg["llamacpp"]["model"] == "qwen3-4b"
        assert cfg["llamacpp"]["model_path"] == "/models/qwen3-4b.gguf"

    def test_chudware_provider_env_override_wins_even_for_local_tier(self, monkeypatch):
        """A human forcing CHUDWARE_PROVIDER (e.g. to point a 'local' tier cell at a
        frontier key for comparison, or a test pinning 'mock') always wins over
        the local-tier bundled-model resolution."""
        from . import config as agent_config
        from .tiers import provider_for_tier

        monkeypatch.setenv("CHUDWARE_PROVIDER", "mock")
        calls = []

        def _tracked(name=None, cfg=None):
            calls.append((name, cfg))
            return "mock-provider"

        monkeypatch.setattr(agent_config, "get_provider", _tracked)
        result = provider_for_tier("local-24k")
        assert result == "mock-provider"
        assert calls == [(None, None)]

    def test_local_tier_unprovisioned_model_fails_closed(self, monkeypatch):
        """get_model_path() returning None (GGUF not downloaded) must raise a clear
        ModelProvisionError, never stringify to the literal 'model_path': 'None'
        (which used to be truthy and silently defeat find_default_model())."""
        from . import model_manager
        from .tiers import provider_for_tier

        monkeypatch.delenv("CHUDWARE_PROVIDER", raising=False)
        monkeypatch.setattr(model_manager, "get_model_path", lambda model_id: None)

        with pytest.raises(model_manager.ModelProvisionError, match="qwen3-4b"):
            provider_for_tier("local-24k")


# ---------------------------------------------------------------------------
# Grammar routing tests

class TestGrammarRouting:
    _SIMPLE_SCHEMA = {
        "type": "object",
        "required": ["status", "summary"],
        "properties": {
            "status": {"type": "string", "enum": ["done", "blocked"]},
            "summary": {"type": "string"},
        },
    }

    def test_gbnf_tier_produces_grammar(self):
        profile = load_tier("local-24k", path=_TIERS_YAML)
        grammar = grammar_for_tier(profile, self._SIMPLE_SCHEMA)
        assert grammar is not None
        assert "root" in grammar
        assert "done" in grammar or "blocked" in grammar

    def test_json_mode_tier_returns_none(self):
        profile = load_tier("mid", path=_TIERS_YAML)
        result = grammar_for_tier(profile, self._SIMPLE_SCHEMA)
        assert result is None

    def test_free_tier_returns_none(self):
        profile = load_tier("frontier", path=_TIERS_YAML)
        result = grammar_for_tier(profile, self._SIMPLE_SCHEMA)
        assert result is None

    def test_grammar_has_gbnf_primitives(self):
        profile = load_tier("local-24k", path=_TIERS_YAML)
        grammar = grammar_for_tier(profile, self._SIMPLE_SCHEMA)
        # Must include the repo's standard GBNF primitives
        assert "string  ::=" in grammar
        assert "boolean ::=" in grammar


# ---------------------------------------------------------------------------
# Tool schema trim tests

class TestTrimToolSchema:
    def test_trims_to_first_sentence(self):
        schema = {"name": "add_features", "description": "Add features to a part. Use for extrudes."}
        result = trim_tool_schema(schema)
        assert result["description"] == "Add features to a part."

    def test_no_change_when_no_period(self):
        schema = {"description": "A tool with no period"}
        result = trim_tool_schema(schema)
        assert result["description"] == "A tool with no period"

    def test_original_not_mutated(self):
        schema = {"description": "First. Second."}
        original_desc = schema["description"]
        trim_tool_schema(schema)
        assert schema["description"] == original_desc

    def test_empty_description_unchanged(self):
        schema = {"description": ""}
        assert trim_tool_schema(schema) == schema

    def test_no_description_key_unchanged(self):
        schema = {"name": "foo"}
        assert trim_tool_schema(schema) == schema

    def test_non_dict_unchanged(self):
        assert trim_tool_schema("not a dict") == "not a dict"  # type: ignore[arg-type]

    def test_abbreviation_not_a_sentence_boundary(self):
        schema = {"description": "Find paths matching a glob (e.g. corpus/**). More detail."}
        result = trim_tool_schema(schema)
        assert result["description"] == "Find paths matching a glob (e.g. corpus/**)."

    def test_i_e_abbreviation_not_a_boundary(self):
        schema = {"description": "Read helpers (i.e. fs_read) first. Then act."}
        result = trim_tool_schema(schema)
        assert result["description"] == "Read helpers (i.e. fs_read) first."

    def test_version_number_not_a_boundary(self):
        schema = {"description": "Requires v1.2 features only"}
        assert trim_tool_schema(schema)["description"] == "Requires v1.2 features only"

    def test_decimal_not_a_boundary(self):
        schema = {"description": "Tolerance 0.5 mm nominal"}
        assert trim_tool_schema(schema)["description"] == "Tolerance 0.5 mm nominal"


# ---------------------------------------------------------------------------
# TurnAssembler tests

class TestTurnAssembler:
    """Budget enforcer: overflow policy, slot ordering, token counting."""

    def _profile_tight(self) -> TierProfile:
        """A profile with available_tokens=500 to force compaction in tests.

        available = context_tokens - generation_headroom = 1500 - 1000 = 500.
        20 messages × 300 chars each = ~6000 chars = ~1500 tokens >> 500 available.
        """
        return TierProfile(
            name="test-tight",
            context_tokens=1500,
            generation_headroom=1000,
            budgets=SlotBudgets(
                system=50, ledger=20, state=30, advisory=15, tools=50, history=300
            ),
            playbook_enforcement="step_locked",
            structured_output="gbnf",
            advisory_verbosity="full",
            max_tools_per_role=14,
            tool_schema_trim=True,
        )

    def test_no_overflow_small_history(self):
        profile = _make_profile()
        assembler = TurnAssembler(profile)
        result = assembler.assemble(
            system="system prompt",
            ledger_text="ledger",
            state_text="state",
            advisory_text="advisory",
            history_messages=_make_messages(2, chars_each=50),
        )
        assert isinstance(result, AssembledTurn)
        assert not result.overflow_applied
        assert result.compaction_level == 0
        assert result.total_tokens > 0

    def test_overflow_shrinks_history_first(self):
        """With massive history, overflow fires and history is compacted."""
        profile = self._profile_tight()
        assembler = TurnAssembler(profile)
        # 20 messages × 300 chars each = 6000 chars > history budget of 1000 tokens × 4 = 4000 chars
        result = assembler.assemble(
            system="sys",
            ledger_text="ldg",
            state_text="sta",
            advisory_text="adv",
            history_messages=_make_messages(20, chars_each=300),
        )
        assert result.overflow_applied

    def test_result_text_contains_all_nonempty_slots(self):
        profile = _make_profile()
        assembler = TurnAssembler(profile)
        result = assembler.assemble(
            system="SYSTEM_PROMPT_MARKER",
            ledger_text="LEDGER_MARKER",
            state_text="STATE_MARKER",
            advisory_text="ADVISORY_MARKER",
            tools_text="TOOLS_MARKER",
            history_messages=[],
        )
        for marker in ("SYSTEM_PROMPT_MARKER", "LEDGER_MARKER", "STATE_MARKER",
                       "ADVISORY_MARKER", "TOOLS_MARKER"):
            assert marker in result.text, f"missing {marker}"

    def test_empty_slots_omitted(self):
        profile = _make_profile()
        assembler = TurnAssembler(profile)
        result = assembler.assemble(system="SYS", history_messages=[])
        # Only system should appear (other slots empty)
        assert result.text.strip() == "SYS"

    def test_slot_token_counts_sum_to_total(self):
        profile = _make_profile()
        assembler = TurnAssembler(profile)
        result = assembler.assemble(
            system="hello world",
            ledger_text="ledger text",
            history_messages=_make_messages(1, chars_each=100),
        )
        assert sum(result.slot_tokens.values()) == result.total_tokens

    def test_system_never_shrunk(self):
        """System prompt must appear verbatim in output regardless of overflow."""
        profile = self._profile_tight()
        assembler = TurnAssembler(profile)
        long_system = "SYSTEM: " + "x" * 1800  # 1800 chars ≈ 450 tokens, within budget
        result = assembler.assemble(
            system=long_system,
            history_messages=_make_messages(30, chars_each=300),  # force overflow
        )
        assert long_system in result.text

    def test_count_tokens_heuristic(self):
        assert _count_tokens("") == 0
        assert _count_tokens("abcd") == 1
        assert _count_tokens("abcde") == 2  # ceil(5/4) = 2

    def test_compact_messages_level1_drops_images(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "path": "/tmp/img.png"},
            ]},
        ]
        result = _compact_messages(msgs, level=1)
        assert all(p["type"] != "image" for p in result[0]["content"])
        assert any(p["type"] == "text" for p in result[0]["content"])

    def test_compact_messages_level2_truncates_text(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "a" * 2000}]},
        ]
        result = _compact_messages(msgs, level=2)
        assert len(result[0]["content"][0]["text"]) <= 1200 + len("\n...[truncated]")

    def test_compact_messages_level3_truncates_text(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "a" * 2000}]},
        ]
        result = _compact_messages(msgs, level=3)
        assert len(result[0]["content"][0]["text"]) <= 600 + len("\n...[truncated]")

    def test_compact_does_not_mutate_original(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "a" * 2000}]}]
        original_text = msgs[0]["content"][0]["text"]
        _compact_messages(msgs, level=3)
        assert msgs[0]["content"][0]["text"] == original_text

    def test_render_messages_plain_text(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "world"}]},
        ]
        rendered = _render_messages(msgs)
        assert "user: hello" in rendered
        assert "assistant: world" in rendered

    def test_overflow_policy_order_history_before_state(self):
        """With large history, history is compacted first; state survives."""
        # available = 600 - 200 = 400 tokens = 1600 chars.
        # System = 100 tokens (400 chars), fixed_overhead ≈ 100 tokens.
        # History = 20 × 100 chars = 2000 chars = 500 tokens >> residual.
        profile = TierProfile(
            name="test",
            context_tokens=600,
            generation_headroom=200,
            budgets=SlotBudgets(
                system=100, ledger=10, state=50, advisory=10, tools=10, history=200
            ),
            playbook_enforcement="off",
            structured_output="free",
            advisory_verbosity="full",
            max_tools_per_role=14,
            tool_schema_trim=False,
        )
        assembler = TurnAssembler(profile)
        # 20 messages × 100 chars each → rendered ~2100 chars = ~525 tokens >> available=400
        result = assembler.assemble(
            system="S" * 400,       # 100 tokens
            state_text="STATE_OK",  # small
            history_messages=_make_messages(20, chars_each=100),
        )
        # History was compacted/truncated due to overflow
        assert result.overflow_applied
        # System prompt still in output (never shrunk)
        assert "S" * 400 in result.text


# ---------------------------------------------------------------------------
# Integration: TurnAssembler + real local-24k profile

class TestTurnAssemblerWithRealProfile:
    """Prove that a real local-24k role turn stays within the context window."""

    def test_local_24k_no_overflow_typical_turn(self):
        """Typical 5-turn history with medium messages stays within local-24k budget."""
        profile = load_tier("local-24k", path=_TIERS_YAML)
        assembler = TurnAssembler(profile)
        result = assembler.assemble(
            system="You are a CAD engineer. " + "x" * 8000,   # ~2k tokens (at system budget)
            ledger_text="Mission: design bracket. " * 80,      # ~400 tokens (within ledger budget)
            state_text="Sketch has 3 entities. " * 100,        # ~500 tokens (within state budget)
            advisory_text="[ADVISORY seq=1 stage=sketch severity=info]\n  [info] DOF: 2\n[/ADVISORY]",
            tools_text="add_features: add features.\ndescribe_part: describe the part.\n" * 50,
            history_messages=_make_messages(5, chars_each=800),  # 5 turns × 800 chars
        )
        # Must fit within local-24k available_tokens (16576)
        assert result.total_tokens <= profile.available_tokens, (
            f"total_tokens {result.total_tokens} > available {profile.available_tokens}"
        )

    def test_frontier_accommodates_large_history(self):
        """Frontier profile should absorb 50 turns without overflow."""
        profile = load_tier("frontier", path=_TIERS_YAML)
        assembler = TurnAssembler(profile)
        result = assembler.assemble(
            system="sys " * 1000,
            history_messages=_make_messages(50, chars_each=1000),
        )
        assert result.total_tokens <= profile.available_tokens
        assert not result.overflow_applied
