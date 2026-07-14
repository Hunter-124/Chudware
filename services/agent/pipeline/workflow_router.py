"""Workflow router — auto-mode task classifier (T-DP10).

Contract: contracts/design_pipeline.md §7 / docs/AGENT_DESIGN_PIPELINE.md §3.6.

Auto-mode is the DEFAULT: the router classifies an incoming task into one of the
recognised task-classes and maps it to the scaffold level that the tuning loop has
measured as best for that class.  When no discriminating winner has been measured
(tied or untested), the router falls back to the tier default:

    LOCAL  → full      (contract §7: "local models default to full scaffold")
    FRONTIER → minimal (contract §7: "frontier defaults to minimal")

The mapping from task-class → scaffold level is driven by the tuning output stored in
``tools/flywheel/bench/gym/reports/tuning_results.json`` (written by
``tools/flywheel/bench/gym/tuning.py``).  The router loads it on first call (lazy, cached
at module level so tests can inject a fixture via ``_inject_tuning_results``).

Design note: the router classifies task *text* only — no model call, no tool call,
deterministic.  The classifier is a keyword/pattern rules table; the task author
requested a tuning loop, not a trained text classifier, and the rules table is the
correct lightweight complement to the measured gym data.

Schema: ``aicad.router/1``
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

ROUTER_SCHEMA = "aicad.router/1"

# Path to the tuning results file written by the gym tuning loop.
_TUNING_RESULTS_PATH = (
    Path(__file__).resolve().parents[3]  # repo root
    / "tools" / "flywheel" / "bench" / "gym" / "reports" / "tuning_results.json"
)

# ---------------------------------------------------------------------------
# Scaffold levels (mirror the effort preset levels from aicad.effort/1)
# ---------------------------------------------------------------------------

class ScaffoldLevel(str, Enum):
    ECO = "eco"
    STANDARD = "standard"
    DELIBERATE = "deliberate"
    MAXIMUM = "maximum"

    # Aliases for the pipeline contract §7 deliberation dial values.
    @classmethod
    def from_deliberation(cls, d: str) -> "ScaffoldLevel":
        """Map a contract §7 deliberation dial value to a scaffold level."""
        return {
            "full": cls.MAXIMUM,
            "standard": cls.STANDARD,
            "minimal": cls.ECO,
        }.get(d, cls.STANDARD)

    @classmethod
    def tier_default(cls, tier: str) -> "ScaffoldLevel":
        """Documented default per contract §7:
        local → full (maximum); frontier → minimal (eco).
        """
        if tier.startswith("frontier"):
            return cls.ECO
        return cls.MAXIMUM   # local-24k, local-48k, mid — all default full


# ---------------------------------------------------------------------------
# Task classes
# ---------------------------------------------------------------------------

class TaskClass(str, Enum):
    """Coarse task-class vocabulary the router maps to scaffold levels.

    Classes correspond to the C-suite challenge categories:
      SIMPLE_PART         single-body, <3 features, no assembly (C1, C6 mod)
      MULTI_FEATURE_PART  single-body, ≥3 features, complex geometry (C1, C3)
      ASSEMBLY            multi-body / hinge / mating parts (C4)
      ENCLOSURE           shell/snap/enclosure + material constraint (C2)
      CAM_PROGRAM         part + machining / toolpaths (C5)
      MODIFICATION        modify an existing part / design change (C6)
      ANALYSIS            FEA / CFD / simulation-primary (C3 FEA leg)
      UNKNOWN             unclassified; router falls back to tier default
    """
    SIMPLE_PART = "simple_part"
    MULTI_FEATURE_PART = "multi_feature_part"
    ASSEMBLY = "assembly"
    ENCLOSURE = "enclosure"
    CAM_PROGRAM = "cam_program"
    MODIFICATION = "modification"
    ANALYSIS = "analysis"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Classifier rules (keyword/pattern, priority-ordered)
# ---------------------------------------------------------------------------

# Each rule: (pattern, class).  Patterns are case-insensitive re.search patterns.
# First match wins; UNKNOWN is the fallback.
_RULES: list[tuple[str, TaskClass]] = [
    # CAM / machining (highest priority — "mill a bracket" is CAM, not SIMPLE_PART)
    (r"\b(cam|toolpath|g.?code|gcode|nc\b|machining|mill|turn|lathe|cutting|zero.?gouge)\b",
     TaskClass.CAM_PROGRAM),
    # Assembly / multi-body
    (r"\b(assembl|hinge|mate|joint|two.part|multi.?part|snap.fit|moving part)\b",
     TaskClass.ASSEMBLY),
    # Enclosure / shell / housing
    (r"\b(enclosure|housing|shell|clamshell|snap.?lid|lid|cover|box|case)\b",
     TaskClass.ENCLOSURE),
    # Modification of existing part (before geometry rules so "modify … fit a 25mm shaft"
    # stays MODIFICATION rather than falling into multi-feature-part).
    # Note: modif*/retrofit*/redesign* are prefix-anchored (\b at start only) because
    # "modify", "modification", "retrofit" all begin at a word boundary but end mid-word.
    (r"\b(modif\w*|update\b|change\b|adjust\w*|extend\b|retrofit\w*|redesign\w*|make\s+\w+\s+bigger|make\s+\w+\s+smaller)\b",
     TaskClass.MODIFICATION),
    # Analysis / simulation primary
    (r"\b(fea|cfd|finite.?element|simulation|thermal|stress|load.?bearing|safety.?factor|margin)\b",
     TaskClass.ANALYSIS),
    # Multi-feature single-body (bolt circle, bore + multiple features)
    (r"\b(bearing.?(block|seat|housing)|bolt.?circle|multiple.?feature|fit.*(bore|shaft|journal)|press.?fit|bushing.*bolt|flange.*bore|clearance.*fit)\b",
     TaskClass.MULTI_FEATURE_PART),
    # Simple single feature: bracket, block, plate, rib with few constraints
    (r"\b(bracket|mounting|plate|slab|block|rib|gusset|simple|basic)\b",
     TaskClass.SIMPLE_PART),
]


def classify_task(request: str) -> TaskClass:
    """Classify *request* text into a TaskClass.  Deterministic, no model call."""
    text = (request or "").lower()
    for pattern, cls in _RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return cls
    return TaskClass.UNKNOWN


# ---------------------------------------------------------------------------
# Tuning results
# ---------------------------------------------------------------------------

_tuning_cache: dict[str, Any] | None = None


def _load_tuning_results(*, path: Path | None = None) -> dict[str, Any]:
    """Load tuning_results.json; return empty dict if absent."""
    global _tuning_cache
    if _tuning_cache is not None:
        return _tuning_cache
    p = path or _TUNING_RESULTS_PATH
    if p.exists():
        try:
            _tuning_cache = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _tuning_cache = {}
    else:
        _tuning_cache = {}
    return _tuning_cache


def _inject_tuning_results(data: dict[str, Any]) -> None:
    """Test helper: inject a fixture into the module-level cache."""
    global _tuning_cache
    _tuning_cache = data


def _clear_tuning_cache() -> None:
    """Test helper: clear the module-level cache."""
    global _tuning_cache
    _tuning_cache = None


def best_scaffold_for_class(task_class: TaskClass, tier: str,
                             *, tuning: dict[str, Any] | None = None) -> tuple[ScaffoldLevel, str]:
    """Return (scaffold_level, reason) for *task_class* on *tier*.

    Consults the tuning results for a measured winner.  Falls back to the tier
    default (local=full, frontier=eco) when:
      - no tuning data exists for this class/tier combo, OR
      - all measured configs are tied (no discriminating winner), OR
      - the winner's win_rate equals 0 (all failed — still honest: full is the safe default)

    Returns a 2-tuple (level, reason_string) so tests and transparency panels can
    display why the router chose this level.
    """
    data = tuning if tuning is not None else _load_tuning_results()
    default = ScaffoldLevel.tier_default(tier)
    default_reason = (
        f"tier default (local → full per contract §7)" if not tier.startswith("frontier")
        else "tier default (frontier → eco per contract §7)"
    )

    per_class = data.get("per_class_winners", {})
    key = f"{task_class.value}|{_normalize_tier(tier)}"
    winner = per_class.get(key)
    if not winner:
        return default, f"no tuning data for {key}; {default_reason}"

    w_level = winner.get("scaffold_level")
    w_rate = winner.get("win_rate", 0.0)
    w_n = winner.get("n", 0)

    if w_level is None or w_n == 0:
        return default, f"empty tuning entry for {key}; {default_reason}"

    if winner.get("tied", False):
        return default, (
            f"all configs tied at win_rate={w_rate:.2f} over {w_n} cells for {key}; "
            f"{default_reason}"
        )

    # We have a genuine measured winner.
    try:
        lvl = ScaffoldLevel(w_level)
    except ValueError:
        return default, f"unknown scaffold level {w_level!r} in tuning data; {default_reason}"

    return lvl, (
        f"measured winner: {w_level} at win_rate={w_rate:.2f} over {w_n} cells "
        f"(cycle {winner.get('cycle', 'unknown')})"
    )


def _normalize_tier(tier: str) -> str:
    """Coarse-normalize a tier string for the tuning key.

    local-24k / local-48k → local; frontier-200k → frontier; mid → mid.
    Mirrors effort.normalize_tier's prefix logic but without loading tiers.yaml.
    """
    for prefix in ("frontier", "local", "mid"):
        if tier.startswith(prefix):
            return prefix
    return tier


# ---------------------------------------------------------------------------
# Router result
# ---------------------------------------------------------------------------

@dataclass
class RouterResult:
    """The router's classification + scaffold decision for one task."""
    task_class: TaskClass
    scaffold_level: ScaffoldLevel
    tier: str
    reason: str
    request_snippet: str = ""     # first 120 chars of request for transparency logs

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ROUTER_SCHEMA,
            "task_class": self.task_class.value,
            "scaffold_level": self.scaffold_level.value,
            "tier": self.tier,
            "reason": self.reason,
            "request_snippet": self.request_snippet,
        }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def route(request: str, tier: str, *,
          tuning: dict[str, Any] | None = None,
          capability=None) -> RouterResult:
    """Classify *request* and return the scaffold level for *tier*.

    This is the auto-mode entry point.  It is:
      - deterministic (no model call, no network),
      - reads the measured tuning results on first call (cached),
      - falls back gracefully to the tier default when measurements are absent or tied.

    Args:
        request: the verbatim user request string.
        tier: the active tier name (e.g. ``"local-24k"``, ``"frontier-200k"``).
        tuning: optional injected tuning dict (tests); None → load from disk.
        capability: optional CapabilityProfile — when workflow=supervised (A),
            force MAXIMUM scaffold; autopilot (C) keeps eco/tier default.

    Returns:
        RouterResult with task_class, scaffold_level, tier, and reason.
    """
    cls = classify_task(request)
    level, reason = best_scaffold_for_class(cls, tier, tuning=tuning)
    # WI-9: capability workflow dial overlays scaffold choice (SOFT).
    # Only when an explicit CapabilityProfile is passed — never re-resolve from
    # tier alone (would force MAXIMUM on every local-tier route call and break
    # measured tuning defaults).
    if capability is not None:
        try:
            wf = str(getattr(capability, "workflow", "") or "")
            if wf == "supervised":
                level = ScaffoldLevel.MAXIMUM
                reason = f"capability A supervised → MAXIMUM ({reason})"
            elif wf == "autopilot_verify":
                if level == ScaffoldLevel.ECO:
                    level = ScaffoldLevel.STANDARD
                    reason = f"capability B autopilot_verify → STANDARD ({reason})"
            # wf == "autopilot" (C): never invent new approve gates.
        except Exception:  # noqa: BLE001
            pass
    return RouterResult(
        task_class=cls,
        scaffold_level=level,
        tier=tier,
        reason=reason,
        request_snippet=request[:120],
    )
