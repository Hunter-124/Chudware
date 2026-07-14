"""Tests for services/agent/capability.py — capability profile resolution.

Covers harness-tiers-spec §0.2 acceptance:
  local-24k → A, mid → B, frontier/openrouter:*/xai:* → C,
  unknown tier → C with warning; registry override reclass; config SOFT
  overrides only; hard_guardrails always True.
No network.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from services.agent.capability import (
    CONFIG_KEY,
    DEFAULT_LETTER,
    LETTERS,
    SOFT_DIALS,
    CapabilityProfile,
    profile_for_letter,
    resolve_capability,
)
from services.agent.model_manager import _REGISTRY, capability_profile_for
from services.agent.tiers import load_all_tiers, load_tier

_TIERS_YAML = Path(__file__).resolve().parent / "tiers.yaml"


# ---------------------------------------------------------------------------
# Letter dial table
# ---------------------------------------------------------------------------


class TestProfileForLetter:
    def test_letters_abc(self):
        for letter in LETTERS:
            p = profile_for_letter(letter)
            assert p.letter == letter
            assert p.hard_guardrails is True

    def test_a_dials(self):
        p = profile_for_letter("A")
        assert p.tool_curation == "curated_15"
        assert p.verify_cadence == "per_mutation"
        assert p.plan_format == "rigid_template"
        assert p.phase_caps is not None
        assert p.phase_caps["build"] == 20
        assert p.structured_policy == "always"
        assert p.auto_checkpoint == "aggressive"
        assert p.math_scratchpad == "hard"
        assert p.example_retrieval == "always"
        assert p.workflow == "supervised"
        assert p.refine_passes == 0

    def test_b_dials(self):
        p = profile_for_letter("B")
        assert p.tool_curation == "full_minus_vision"
        assert p.verify_cadence == "per_feature_group"
        assert p.plan_format == "structured_fields"
        assert p.phase_caps is not None
        assert p.phase_caps["build"] == 40  # 2× A
        assert p.structured_policy == "tool_frame"
        assert p.auto_checkpoint == "destructive_only"
        assert p.math_scratchpad == "soft"
        assert p.example_retrieval == "novel"
        assert p.workflow == "autopilot_verify"
        assert p.refine_passes == 1

    def test_c_dials(self):
        p = profile_for_letter("C")
        assert p.tool_curation == "full"
        assert p.verify_cadence == "soft_only"
        assert p.plan_format == "freeform"
        assert p.phase_caps is None
        assert p.structured_policy == "free_retry_gbnf"
        assert p.auto_checkpoint == "destructive_only"
        assert p.math_scratchpad == "off"
        assert p.example_retrieval == "on_demand"
        assert p.workflow == "autopilot"
        assert p.refine_passes == 2

    def test_invalid_letter_raises(self):
        with pytest.raises(ValueError, match="A|B|C"):
            profile_for_letter("Z")

    def test_frozen(self):
        p = profile_for_letter("A")
        with pytest.raises(Exception):
            p.letter = "C"  # type: ignore[misc]

    def test_to_payload_roundtrip_keys(self):
        p = profile_for_letter("B")
        payload = p.to_payload()
        assert payload["letter"] == "B"
        assert payload["hard_guardrails"] is True
        assert payload["phase_caps"]["build"] == 40


# ---------------------------------------------------------------------------
# tiers.yaml binding
# ---------------------------------------------------------------------------


class TestTierYamlBinding:
    def test_local_24k_is_a(self):
        p = load_tier("local-24k", path=_TIERS_YAML)
        assert p.capability_profile == "A"

    def test_local_48k_is_a(self):
        p = load_tier("local-48k", path=_TIERS_YAML)
        assert p.capability_profile == "A"

    def test_mid_is_b(self):
        p = load_tier("mid", path=_TIERS_YAML)
        assert p.capability_profile == "B"

    def test_frontier_is_c(self):
        p = load_tier("frontier", path=_TIERS_YAML)
        assert p.capability_profile == "C"

    def test_all_profiles_have_valid_letter(self):
        for name, prof in load_all_tiers(path=_TIERS_YAML).items():
            assert prof.capability_profile in LETTERS, name


# ---------------------------------------------------------------------------
# resolve_capability — acceptance §0.2
# ---------------------------------------------------------------------------


class TestResolveCapability:
    def test_local_24k_a(self):
        p = resolve_capability("local-24k", config={})
        assert p.letter == "A"
        assert p.tool_curation == "curated_15"
        assert p.hard_guardrails is True
        assert p.tier == "local-24k"

    def test_local_48k_a(self):
        p = resolve_capability("local-48k", config={})
        assert p.letter == "A"

    def test_local_prefix_alias_a(self):
        # gym-style local-24k-<model> collapses to local-24k → A
        p = resolve_capability("local-24k-gemma3-qat", config={})
        assert p.letter == "A"
        assert p.tier == "local-24k"

    def test_mid_b(self):
        p = resolve_capability("mid", config={})
        assert p.letter == "B"
        assert p.plan_format == "structured_fields"

    def test_frontier_c(self):
        p = resolve_capability("frontier", config={})
        assert p.letter == "C"
        assert p.phase_caps is None
        assert p.workflow == "autopilot"

    def test_openrouter_c(self):
        p = resolve_capability("openrouter:anthropic/claude-opus-4", config={})
        assert p.letter == "C"
        assert p.tier == "frontier"

    def test_xai_c(self):
        p = resolve_capability("xai:grok-4.5", config={})
        assert p.letter == "C"
        assert p.tier == "frontier"

    def test_unknown_tier_defaults_c_with_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            p = resolve_capability("totally-unknown-tier-xyz", config={})
        assert p.letter == "C"
        assert p.hard_guardrails is True
        assert any("unknown tier" in w for w in p.warnings)
        assert any(issubclass(w.category, UserWarning) for w in caught)

    def test_hard_guardrails_always_true(self):
        for tier in ("local-24k", "mid", "frontier", "openrouter:x"):
            assert resolve_capability(tier, config={}).hard_guardrails is True


# ---------------------------------------------------------------------------
# Registry override (model_manager)
# ---------------------------------------------------------------------------


class TestRegistryOverride:
    def test_capability_profile_for_unset(self):
        # Shipped models do not set capability_profile → None (inherit tier).
        assert capability_profile_for("qwen3-4b") is None
        assert capability_profile_for("gemma3-4b-qat") is None
        assert capability_profile_for("no-such-model") is None

    def test_registry_override_reclass(self, monkeypatch):
        # Inject a temporary per-model override without mutating the real
        # shipped registry permanently.
        entry = dict(_REGISTRY["qwen3-4b"])
        entry["capability_profile"] = "C"
        monkeypatch.setitem(_REGISTRY, "qwen3-4b", entry)

        assert capability_profile_for("qwen3-4b") == "C"
        p = resolve_capability("local-24k", model_id="qwen3-4b", config={})
        assert p.letter == "C"
        assert p.sources.get("letter") == "registry"
        assert p.model_id == "qwen3-4b"

    def test_registry_override_a_on_frontier(self, monkeypatch):
        entry = dict(_REGISTRY["gemma3-4b-qat"])
        entry["capability_profile"] = "A"
        monkeypatch.setitem(_REGISTRY, "gemma3-4b-qat", entry)

        p = resolve_capability("frontier", model_id="gemma3-4b-qat", config={})
        assert p.letter == "A"
        assert p.tool_curation == "curated_15"


# ---------------------------------------------------------------------------
# config.json SOFT overrides
# ---------------------------------------------------------------------------


class TestConfigOverrides:
    def test_soft_dial_override(self):
        cfg = {
            CONFIG_KEY: {
                "overrides": {
                    "mid": {"refine_passes": 5, "workflow": "supervised"},
                }
            }
        }
        p = resolve_capability("mid", config=cfg)
        assert p.letter == "B"
        assert p.refine_passes == 5
        assert p.workflow == "supervised"
        assert p.sources["refine_passes"] == "override"
        assert p.sources["workflow"] == "override"
        # Un-overridden dials stay shipped.
        assert p.tool_curation == "full_minus_vision"

    def test_hard_guardrails_override_ignored(self):
        cfg = {
            CONFIG_KEY: {
                "overrides": {
                    "frontier": {"hard_guardrails": False},
                }
            }
        }
        p = resolve_capability("frontier", config=cfg)
        assert p.hard_guardrails is True
        assert any("hard_guardrails" in w for w in p.warnings)

    def test_c_soft_override_warns(self):
        cfg = {
            CONFIG_KEY: {
                "overrides": {
                    "frontier": {"example_retrieval": "always"},
                }
            }
        }
        p = resolve_capability("frontier", config=cfg)
        assert p.example_retrieval == "always"  # applied (warn-never-block)
        assert any("SOFT override" in w for w in p.warnings)

    def test_letter_reclass_via_override(self):
        cfg = {
            CONFIG_KEY: {
                "overrides": {
                    "mid": {"letter": "A"},
                }
            }
        }
        p = resolve_capability("mid", config=cfg)
        assert p.letter == "A"
        assert p.tool_curation == "curated_15"
        assert p.sources.get("letter") == "override"

    def test_c_to_a_reclass_warns(self):
        cfg = {
            CONFIG_KEY: {
                "overrides": {
                    "frontier": {"capability_profile": "A"},
                }
            }
        }
        p = resolve_capability("frontier", config=cfg)
        assert p.letter == "A"
        assert any("C→A" in w or "reclass" in w for w in p.warnings)

    def test_unknown_override_key_ignored(self):
        cfg = {
            CONFIG_KEY: {
                "overrides": {
                    "mid": {"not_a_real_dial": 1},
                }
            }
        }
        p = resolve_capability("mid", config=cfg)
        assert any("unknown override key" in w for w in p.warnings)

    def test_phase_caps_override(self):
        cfg = {
            CONFIG_KEY: {
                "overrides": {
                    "local-24k": {"phase_caps": {"build": 99, "plan": 3}},
                }
            }
        }
        p = resolve_capability("local-24k", config=cfg)
        assert p.phase_caps == {"build": 99, "plan": 3}

    def test_empty_config_is_fine(self):
        p = resolve_capability("mid", config={})
        assert p.letter == "B"
        assert p.warnings == [] or all("unknown" not in w for w in p.warnings)


# ---------------------------------------------------------------------------
# Precedence: tier < registry < config
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_registry_beats_tier(self, monkeypatch):
        entry = dict(_REGISTRY["qwen3-4b"])
        entry["capability_profile"] = "C"
        monkeypatch.setitem(_REGISTRY, "qwen3-4b", entry)
        p = resolve_capability("local-24k", model_id="qwen3-4b", config={})
        assert p.letter == "C"

    def test_config_letter_beats_registry(self, monkeypatch):
        entry = dict(_REGISTRY["qwen3-4b"])
        entry["capability_profile"] = "C"
        monkeypatch.setitem(_REGISTRY, "qwen3-4b", entry)
        cfg = {
            CONFIG_KEY: {
                "overrides": {
                    "local-24k": {"letter": "B"},
                }
            }
        }
        p = resolve_capability("local-24k", model_id="qwen3-4b", config=cfg)
        assert p.letter == "B"
        assert p.sources.get("letter") == "override"

    def test_config_soft_on_registry_letter(self, monkeypatch):
        entry = dict(_REGISTRY["qwen3-4b"])
        entry["capability_profile"] = "B"
        monkeypatch.setitem(_REGISTRY, "qwen3-4b", entry)
        cfg = {
            CONFIG_KEY: {
                "overrides": {
                    "local-24k": {"refine_passes": 7},
                }
            }
        }
        p = resolve_capability("local-24k", model_id="qwen3-4b", config=cfg)
        assert p.letter == "B"  # from registry
        assert p.refine_passes == 7  # from config


# ---------------------------------------------------------------------------
# Soft dials inventory
# ---------------------------------------------------------------------------


class TestSoftDials:
    def test_soft_dials_cover_profile_fields(self):
        # Every SOFT dial is a real CapabilityProfile field.
        sample = profile_for_letter("A")
        for name in SOFT_DIALS:
            assert hasattr(sample, name), name

    def test_hard_guardrails_not_in_soft(self):
        assert "hard_guardrails" not in SOFT_DIALS
        assert "letter" not in SOFT_DIALS
