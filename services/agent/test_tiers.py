"""Tests for services/agent/tiers.py — tier profiles, tier->provider, schema trim.

Contract: contracts/advisor.md §8 + 1.3 addendum.

Test venv: ~/.local/share/Chudware/mcp-venv/bin/python
Run: pytest services/agent/test_tiers.py -v

Coverage:
  * Budget math property: sum(slots) + headroom ≤ context_tokens for all 4 profiles.
  * Tier env override: CHUDWARE_TIER wins, unset returns None.
  * Tool schema trim: description shortened to first sentence for local tiers.
  * Loader: all 4 profiles load without error; bad yaml raises TierError.
  * tier -> provider resolution (the gym/bench seam).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from .tiers import (
    TIER_ENV_VAR,
    TierError,
    TierProfile,
    detect_tier_from_env,
    load_all_tiers,
    load_tier,
    trim_tool_schema,
)

# ---------------------------------------------------------------------------
# helpers

_TIERS_YAML = Path(__file__).resolve().parent / "tiers.yaml"


def _all_profiles() -> dict[str, TierProfile]:
    return load_all_tiers(path=_TIERS_YAML)


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
            for slot_name, tokens in dataclasses.asdict(p.budgets).items():
                assert tokens > 0, f"{name}.{slot_name} must be > 0"


# ---------------------------------------------------------------------------
# Tool-schema trim flag

class TestToolSchemaTrimFlag:
    def test_local_tiers_trim_schemas(self):
        for name in ("local-24k", "local-48k"):
            assert load_tier(name, path=_TIERS_YAML).tool_schema_trim is True

    def test_frontier_no_trim(self):
        assert load_tier("frontier", path=_TIERS_YAML).tool_schema_trim is False


# ---------------------------------------------------------------------------
# Tier env override

class TestTierEnv:
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
# tier -> provider resolution (the gym/bench seam)

class TestProviderForTier:
    """``provider_for_tier`` is the tier->provider resolution seam used by the
    gym bench. It must:
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
