"""Deterministic render-grounded reward for the GRPO leg (T-H10 recipe scaffold).

The whole reason online RL beats DPO here (cadrille precedent, VLM_OFFLINE_TRACK §2): we
KNOW the geometry behind every synthetic render, so a rollout's claims can be scored
PROGRAMMATICALLY — no learned reward model, no human labels. This is that reward function.

It is pure and testable OFFLINE (no model, no training) — it just maps (claims, known
geometry) -> scalar reward using the SAME "precision is king" scoring as the T-H8 bench, so
what the GRPO trainer optimizes is exactly what the bench measures. Computing the reward is
allowed anywhere; only the training STEP that consumes it is CI-guarded (see guard.py)."""
from __future__ import annotations

from ...bench.benches.vlm import _score_claims, RECALL_TARGET

# A hallucination is punished harder than a miss — the locked division of labor says the
# VLM's failure mode we most need to suppress is a confident wrong claim reaching a human.
HALLUCINATION_PENALTY = 0.5


def render_reward(claims: list[dict], known_geometry: list[dict]) -> float:
    """Reward in [-inf, 1]. F1 of grounded claims minus a per-hallucination penalty, so a
    fluent-but-wrong rollout scores BELOW a terse-but-grounded one. Strips the intake-only
    `confidence` attr before matching (it is not a geometry fact)."""
    scored = [{k: v for k, v in c.items() if k != "confidence"} for c in claims]
    s = _score_claims(scored, known_geometry)
    return s["f1"] - HALLUCINATION_PENALTY * s["hallucinations"]


def passed_bar(claims: list[dict], known_geometry: list[dict]) -> bool:
    """The bench's own pass bar, reused so RL 'success' == bench 'pass'."""
    scored = [{k: v for k, v in c.items() if k != "confidence"} for c in claims]
    s = _score_claims(scored, known_geometry)
    return bool(s["recall"] >= RECALL_TARGET and s["precision"] == 1.0)
