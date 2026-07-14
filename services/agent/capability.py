"""capability.py — capability profile (A/B/C) resolution for harness coercion.

Orthogonal to effort (eco/standard/deliberate/maximum = *how much* deliberation).
capability_profile = *how much coercion*:
  A local    — scaffold hard; force grounding + read-back
  B budget   — full tools + soft gates; structured plan
  C frontier — freedom; HARD floors only

Resolution (mirrors effort.resolve_effort):
  tiers.yaml default  <  model_manager registry override  <  config.json
  ai_capability.overrides.<tier>  (SOFT dials only — HARD floors ignore overrides)

Freedom rule: warn-never-block for SOFT dials; HARD floors always on.
Unknown tier → C with warning (never guess A/B).

Spec: local://harness-tiers-spec.md §0, §5 (WI-0 foundation).
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

CAPABILITY_SCHEMA = "aicad.capability/1"
CONFIG_KEY = "ai_capability"
LETTERS = ("A", "B", "C")
DEFAULT_LETTER = "C"

# Soft dial field names that config.json overrides may touch.
# hard_guardrails is NEVER overridable (always True).
SOFT_DIALS = frozenset({
    "tool_curation",
    "verify_cadence",
    "plan_format",
    "phase_caps",
    "structured_policy",
    "auto_checkpoint",
    "math_scratchpad",
    "example_retrieval",
    "workflow",
    "refine_passes",
})

# letter → full dial table (single source; not four YAML copies).
# Values follow harness-tiers-spec.md §5 + WI-1..10 dial matrix.
_LETTER_DIALS: dict[str, dict[str, Any]] = {
    "A": {
        "tool_curation": "curated_15",
        "verify_cadence": "per_mutation",
        "plan_format": "rigid_template",
        "phase_caps": {
            "intake": 2,
            "plan": 4,
            "specify": 6,
            "build": 20,
            "verify": 6,
        },
        "structured_policy": "always",
        "auto_checkpoint": "aggressive",
        "math_scratchpad": "hard",
        "example_retrieval": "always",
        "workflow": "supervised",
        "refine_passes": 0,
        "hard_guardrails": True,
    },
    "B": {
        "tool_curation": "full_minus_vision",
        "verify_cadence": "per_feature_group",
        "plan_format": "structured_fields",
        # HARD at 2× A.
        "phase_caps": {
            "intake": 4,
            "plan": 8,
            "specify": 12,
            "build": 40,
            "verify": 12,
        },
        "structured_policy": "tool_frame",
        "auto_checkpoint": "destructive_only",
        "math_scratchpad": "soft",
        "example_retrieval": "novel",
        "workflow": "autopilot_verify",
        "refine_passes": 1,
        "hard_guardrails": True,
    },
    "C": {
        "tool_curation": "full",
        "verify_cadence": "soft_only",
        "plan_format": "freeform",
        "phase_caps": None,  # soft only via effort/ponder; no hard phase cap
        "structured_policy": "free_retry_gbnf",
        "auto_checkpoint": "destructive_only",
        "math_scratchpad": "off",
        "example_retrieval": "on_demand",
        "workflow": "autopilot",
        "refine_passes": 2,
        "hard_guardrails": True,
    },
}


@dataclass(frozen=True)
class CapabilityProfile:
    """Fully-resolved capability dials for one (tier, model) pair.

    Schema: harness-tiers-spec.md §5.
    """
    letter: str                    # "A"|"B"|"C"
    tool_curation: str             # "curated_15"|"full_minus_vision"|"full"
    verify_cadence: str            # "per_mutation"|"per_feature_group"|"soft_only"
    plan_format: str               # "rigid_template"|"structured_fields"|"freeform"
    phase_caps: dict[str, int] | None  # None = soft only
    structured_policy: str         # "always"|"tool_frame"|"free_retry_gbnf"
    auto_checkpoint: str           # "aggressive"|"destructive_only"
    math_scratchpad: str           # "hard"|"soft"|"off"
    example_retrieval: str         # "always"|"novel"|"on_demand"
    workflow: str                  # "supervised"|"autopilot_verify"|"autopilot"
    refine_passes: int             # 0|1|2…
    hard_guardrails: bool          # always True
    warnings: list[str] = field(default_factory=list)
    # Provenance for debugging / ledger snapshots.
    tier: str = ""
    model_id: str | None = None
    sources: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Ledger / journal snapshot shape."""
        return {
            "letter": self.letter,
            "tool_curation": self.tool_curation,
            "verify_cadence": self.verify_cadence,
            "plan_format": self.plan_format,
            "phase_caps": dict(self.phase_caps) if self.phase_caps else None,
            "structured_policy": self.structured_policy,
            "auto_checkpoint": self.auto_checkpoint,
            "math_scratchpad": self.math_scratchpad,
            "example_retrieval": self.example_retrieval,
            "workflow": self.workflow,
            "refine_passes": self.refine_passes,
            "hard_guardrails": self.hard_guardrails,
            "tier": self.tier,
            "model_id": self.model_id,
            "sources": dict(self.sources),
            "warnings": list(self.warnings),
        }


def profile_for_letter(letter: str) -> CapabilityProfile:
    """Build a CapabilityProfile from the letter→dials table alone."""
    letter = (letter or DEFAULT_LETTER).upper()
    if letter not in _LETTER_DIALS:
        raise ValueError(f"capability letter must be A|B|C, got {letter!r}")
    dials = _LETTER_DIALS[letter]
    phase = dials["phase_caps"]
    return CapabilityProfile(
        letter=letter,
        tool_curation=dials["tool_curation"],
        verify_cadence=dials["verify_cadence"],
        plan_format=dials["plan_format"],
        phase_caps=dict(phase) if phase is not None else None,
        structured_policy=dials["structured_policy"],
        auto_checkpoint=dials["auto_checkpoint"],
        math_scratchpad=dials["math_scratchpad"],
        example_retrieval=dials["example_retrieval"],
        workflow=dials["workflow"],
        refine_passes=int(dials["refine_passes"]),
        hard_guardrails=True,
        sources={k: "shipped" for k in (
            "letter", "tool_curation", "verify_cadence", "plan_format",
            "phase_caps", "structured_policy", "auto_checkpoint",
            "math_scratchpad", "example_retrieval", "workflow",
            "refine_passes", "hard_guardrails",
        )},
    )


def _capability_config(config: dict | None) -> dict:
    """Extract + default the ai_capability block from a config.json dict."""
    if config is None:
        try:
            from services.agent.config import load_config
            config = load_config()
        except Exception:  # noqa: BLE001 — never block resolution on config IO
            config = {}
    block = (config or {}).get(CONFIG_KEY) or {}
    if not isinstance(block, dict):
        return {"overrides": {}}
    overrides = block.get("overrides") or {}
    if not isinstance(overrides, dict):
        return {"overrides": {}}
    return {"overrides": overrides}


def _resolve_letter_from_tier(
    tier: str,
    *,
    tiers_path: Path | None = None,
) -> tuple[str, str, list[str]]:
    """Map a gym/CLI tier string onto (letter, normalized_tier_name, warnings).

    Uses effort_tier_for so openrouter:*/xai:* → frontier profile (C).
    Unknown tier → C with a warning.
    """
    warns: list[str] = []
    from services.agent.tiers import (
        effort_tier_for,
        load_all_tiers,
        parse_openrouter_tier,
        parse_xai_tier,
    )

    # Parameterized paid tiers are C by definition (frontier class).
    if parse_openrouter_tier(tier) is not None or parse_xai_tier(tier) is not None:
        return "C", effort_tier_for(tier) or "frontier", warns

    try:
        tiers = load_all_tiers(path=tiers_path)
    except Exception as exc:  # noqa: BLE001
        msg = f"capability: failed to load tiers ({exc}); defaulting to C"
        warns.append(msg)
        log.warning(msg)
        return DEFAULT_LETTER, tier or "unknown", warns

    # Exact match.
    if tier in tiers:
        letter = tiers[tier].capability_profile
        if letter not in LETTERS:
            msg = (
                f"capability: tier {tier!r} has invalid capability_profile "
                f"{letter!r}; defaulting to C"
            )
            warns.append(msg)
            log.warning(msg)
            return DEFAULT_LETTER, tier, warns
        return letter, tier, warns

    # Prefix alias (local-24k-gemma3-qat → local-24k). Prefer longest match.
    for known in sorted(tiers, key=len, reverse=True):
        if tier.startswith(known):
            letter = tiers[known].capability_profile
            if letter not in LETTERS:
                letter = DEFAULT_LETTER
            return letter, known, warns

    # effort_tier_for may still map something (passthrough for unknown).
    mapped = effort_tier_for(tier)
    if mapped and mapped in tiers:
        letter = tiers[mapped].capability_profile
        if letter not in LETTERS:
            letter = DEFAULT_LETTER
        return letter, mapped, warns

    msg = (
        f"capability: unknown tier {tier!r}; defaulting to C "
        f"(freedom floor). Known: {sorted(tiers)}"
    )
    warns.append(msg)
    log.warning(msg)
    # Also surface via warnings module so tests can catch it.
    warnings.warn(msg, UserWarning, stacklevel=3)
    return DEFAULT_LETTER, tier or "unknown", warns


def _registry_letter(model_id: str | None) -> str | None:
    """Optional per-model capability_profile override from model_manager."""
    if not model_id:
        return None
    try:
        from services.agent.model_manager import capability_profile_for
        return capability_profile_for(model_id)
    except ImportError:
        return None
    except Exception:  # noqa: BLE001
        return None


def resolve_capability(
    tier: str,
    model_id: str | None = None,
    *,
    config: dict | None = None,
    tiers_path: Path | None = None,
) -> CapabilityProfile:
    """Resolve the effective CapabilityProfile for *tier* (+ optional *model_id*).

    Precedence
    ----------
    1. tiers.yaml ``capability_profile`` (via effort_tier_for mapping)
    2. model_manager registry ``capability_profile`` when set for *model_id*
    3. config.json ``ai_capability.overrides.<tier>`` — SOFT dials only

    HARD floors (``hard_guardrails=True``) ignore overrides. Freedom rule:
    warn-never-block for SOFT dials; never clamp.
    """
    warns: list[str] = []
    sources: dict[str, str] = {}

    letter, tname, tier_warns = _resolve_letter_from_tier(tier, tiers_path=tiers_path)
    warns.extend(tier_warns)
    sources["letter"] = "tier"

    # Per-model registry override wins over tier default.
    reg = _registry_letter(model_id)
    if reg is not None:
        reg = reg.upper()
        if reg in LETTERS:
            if reg != letter:
                warns.append(
                    f"capability: model {model_id!r} overrides letter "
                    f"{letter} → {reg} (registry)"
                )
            letter = reg
            sources["letter"] = "registry"
        else:
            msg = (
                f"capability: model {model_id!r} registry capability_profile "
                f"{reg!r} invalid; ignoring"
            )
            warns.append(msg)
            log.warning(msg)

    profile = profile_for_letter(letter)
    # Stamp provenance: dials are "shipped" for the letter, but letter itself
    # keeps its real source (tier|registry). Don't let profile.sources clobber it.
    letter_src = sources.get("letter", "tier")
    sources.update(profile.sources)
    sources["letter"] = letter_src

    # config.json SOFT dial overrides (never hard_guardrails, never letter reclass
    # unless the override explicitly sets letter — and even then only as SOFT
    # reclass with warning; C reclass to A/B is warn-never-block).
    cap_cfg = _capability_config(config)
    user_kv = (cap_cfg["overrides"].get(tname)
               or cap_cfg["overrides"].get(tier)
               or {})
    if not isinstance(user_kv, dict):
        user_kv = {}

    # Optional letter reclass via override (SOFT — warn, apply).
    if "letter" in user_kv or "capability_profile" in user_kv:
        want = str(user_kv.get("letter") or user_kv.get("capability_profile") or "").upper()
        if want in LETTERS and want != letter:
            # Freedom rule: reclassing C toward more coercion is warned; apply.
            if letter == "C" and want in ("A", "B"):
                msg = (
                    f"capability: config override reclassifies C→{want} for "
                    f"tier {tname!r} (warn-never-block; applied)"
                )
                warns.append(msg)
                log.warning(msg)
            letter = want
            profile = profile_for_letter(letter)
            sources = dict(profile.sources)
            sources["letter"] = "override"

    # Apply SOFT dial overrides field-by-field.
    updates: dict[str, Any] = {}
    for key, value in user_kv.items():
        if key in ("letter", "capability_profile"):
            continue
        if key == "hard_guardrails":
            msg = (
                f"capability: ignoring override of hard_guardrails={value!r} "
                f"for tier {tname!r} (HARD floor)"
            )
            warns.append(msg)
            log.warning(msg)
            continue
        if key not in SOFT_DIALS:
            msg = (
                f"capability: unknown override key {key!r} for tier {tname!r}; "
                f"ignored"
            )
            warns.append(msg)
            log.warning(msg)
            continue
        # Freedom rule for C: SOFT overrides that would *increase* coercion
        # still apply (warn-never-block), but we always warn on C overrides.
        if letter == "C":
            msg = (
                f"capability: applying SOFT override {key}={value!r} on C "
                f"tier {tname!r} (warn-never-block)"
            )
            warns.append(msg)
            log.warning(msg)
        updates[key] = value
        sources[key] = "override"

    if updates:
        # phase_caps must be dict|None; coerce carefully.
        if "phase_caps" in updates and updates["phase_caps"] is not None:
            if not isinstance(updates["phase_caps"], dict):
                msg = (
                    f"capability: phase_caps override must be dict|null, "
                    f"got {type(updates['phase_caps']).__name__}; ignored"
                )
                warns.append(msg)
                log.warning(msg)
                updates.pop("phase_caps")
                sources.pop("phase_caps", None)
            else:
                updates["phase_caps"] = {
                    str(k): int(v) for k, v in updates["phase_caps"].items()
                }
        if "refine_passes" in updates:
            updates["refine_passes"] = int(updates["refine_passes"])
        profile = replace(profile, **updates)

    # hard_guardrails is always True — ignore any path that tried to clear it.
    profile = replace(
        profile,
        hard_guardrails=True,
        letter=letter,
        warnings=list(warns),
        tier=tname,
        model_id=model_id,
        sources=sources,
    )
    return profile
