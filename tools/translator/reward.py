"""Deterministic multi-term GRPO reward for the translator (T-F11 Stage-2 scaffold).

The verifiable-reward regime (master plan §Pillar 13): no critic, no learned reward model —
we HOLD the ground-truth state, so a caption's numerals are checkable exactly and for free.
Pure and testable OFFLINE (no model, no training); only the training STEP that consumes it
is CI-guarded (see recipes/guard.py).

Multi-term = groundedness + slot coverage + format, per the master plan's explicit
requirement to "block reward-hacking-by-omission": a groundedness-only reward is maximized
by a model that says nothing (0 numerals → 0 ungrounded → perfect groundedness). The
coverage term makes silence costly; the format term keeps the SER header + prose shape.

Groundedness is a HARD term: any ungrounded (extrinsic) or wrong (intrinsic) numeral applies
a large per-numeral penalty, so no amount of coverage can buy back a hallucination — the
same "precision is king" spirit as the T-H8/T-H10 benches, here as an RL signal.
"""
from __future__ import annotations

from .eval.slot_accuracy import score
from .groundedness import GroundednessChecker, H4GroundednessAdapter
from .gsl_ser import SER_VERSION

# Per-numeral penalty for an ungrounded/wrong claim. Large enough that one hallucination
# outweighs full coverage — a fluent-but-wrong rollout scores below a terse grounded one.
HALLUCINATION_PENALTY = 1.0

# Term weights (groundedness dominates; coverage counters omission; format is a nudge).
W_GROUND, W_COVERAGE, W_FORMAT = 1.0, 0.5, 0.1


def _terms(caption: str, state: dict, checker: GroundednessChecker) -> dict:
    rep = score(caption, state, checker=checker)
    n = max(1, rep.n_reference)
    groundedness = 1.0 - (rep.wrong + rep.extrinsic) / n     # in (-inf, 1]
    coverage = rep.correct / n                                # rewards MENTIONING grounded slots
    has_header = SER_VERSION in caption or caption.strip().startswith("[")
    format_ok = 1.0 if (has_header and caption.strip().endswith((".", "]"))) else 0.0
    return {"groundedness": groundedness, "coverage": coverage, "format": format_ok,
            "wrong": rep.wrong, "extrinsic": rep.extrinsic, "n_reference": rep.n_reference}


def translator_reward(caption: str, state: dict, *,
                      checker: GroundednessChecker | None = None) -> float:
    """Scalar reward for one rollout. Weighted sum of the three terms minus a hard
    per-hallucination penalty. Checker passed per-call (we never mutate the global default)."""
    chk = checker or H4GroundednessAdapter()
    t = _terms(caption, state, chk)
    base = W_GROUND * t["groundedness"] + W_COVERAGE * t["coverage"] + W_FORMAT * t["format"]
    return base - HALLUCINATION_PENALTY * (t["wrong"] + t["extrinsic"])


def reward_breakdown(caption: str, state: dict, *,
                     checker: GroundednessChecker | None = None) -> dict:
    """Term-by-term reward (for eval/inspection and reward-hacking audits)."""
    chk = checker or H4GroundednessAdapter()
    t = _terms(caption, state, chk)
    t["reward"] = (W_GROUND * t["groundedness"] + W_COVERAGE * t["coverage"]
                   + W_FORMAT * t["format"] - HALLUCINATION_PENALTY * (t["wrong"] + t["extrinsic"]))
    return t
