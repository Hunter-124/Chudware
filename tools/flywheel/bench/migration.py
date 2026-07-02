"""IP-18 migration gate — the frozen, executable promotion criteria for the regen-fix bench.

The criteria are pre-registered in ``training/benches/regen_fix/migration_criteria.json`` and
FROZEN before any IP-18 training data or local model exists (T-H11 / survey §6). This module
just makes them executable so a promotion decision is a function of measured numbers, not a
judgment call. It never mutates the criteria; a candidate row is checked against the frozen
bar.

A local model may replace the frontier path ONLY if ``evaluate`` returns ``passed=True`` AND a
human signs off (the T-H9 shadow-mode policy). This gate is necessary, not sufficient.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

_CRITERIA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "training", "benches", "regen_fix",
    "migration_criteria.json",
)


def load_criteria(path: str = _CRITERIA_PATH) -> dict:
    with open(os.path.abspath(path), encoding="utf-8") as f:
        c = json.load(f)
    assert c.get("frozen") and c.get("frozen_before_training"), \
        "migration criteria must be frozen before training"
    return c


@dataclass
class Candidate:
    """A measured local-model row on the frozen bench (all on the SAME frozen split)."""
    pct_fixed: float
    intent_drift: int
    cost_per_fix: float


@dataclass
class Reference:
    """The frontier + deterministic rows the candidate is judged against."""
    frontier_pct_fixed: float
    frontier_cost_per_fix: float
    deterministic_pct_fixed: float


@dataclass
class GateResult:
    passed: bool
    checks: dict = field(default_factory=dict)   # criterion id -> bool
    reasons: list = field(default_factory=list)   # human-readable failures


def evaluate(candidate: Candidate, reference: Reference,
             criteria: Optional[dict] = None) -> GateResult:
    """Return whether ``candidate`` clears every frozen migration criterion.

    Deliberately a pure function of the four measured numbers + the two reference rows — the
    same shape the frozen JSON documents. No goalpost logic; the bar is the file.
    """
    c = criteria or load_criteria()
    th = c["migration_gate"]["thresholds"]
    within = float(th["within_pts_of_frontier"])
    cost_ratio = float(th["cost_ratio_vs_frontier"])

    checks = {
        "within_5pts_of_frontier":
            candidate.pct_fixed >= reference.frontier_pct_fixed - within,
        "cheaper_than_frontier_50x":
            candidate.cost_per_fix <= reference.frontier_cost_per_fix / cost_ratio,
        "never_below_deterministic":
            candidate.pct_fixed >= reference.deterministic_pct_fixed,
        "zero_intent_drift":
            candidate.intent_drift == 0,
    }
    reasons = []
    if not checks["within_5pts_of_frontier"]:
        reasons.append(f"fix-rate {candidate.pct_fixed:.3f} is >5pts below frontier "
                       f"{reference.frontier_pct_fixed:.3f}")
    if not checks["cheaper_than_frontier_50x"]:
        reasons.append(f"cost/fix {candidate.cost_per_fix:g} exceeds 1/50th of frontier "
                       f"{reference.frontier_cost_per_fix:g}")
    if not checks["never_below_deterministic"]:
        reasons.append(f"fix-rate {candidate.pct_fixed:.3f} is below the deterministic "
                       f"kill-switch {reference.deterministic_pct_fixed:.3f}")
    if not checks["zero_intent_drift"]:
        reasons.append(f"{candidate.intent_drift} fix(es) altered unaffected geometry")
    return GateResult(passed=all(checks.values()), checks=checks, reasons=reasons)
