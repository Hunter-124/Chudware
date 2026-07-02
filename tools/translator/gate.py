"""Inference-time groundedness OUTPUT GATE for the learned translator (T-F11 DoD item 4).

The translator (Pillar 13) only ever NARRATES facts the deterministic engine already
computed; it must never invent or misstate a numeral. This gate is the enforcement at
serve time: score a candidate caption's numerals against the ground-truth slots, and if any
numeral is ungrounded (invented) or wrong (intrinsic hallucination), DISCARD the model text
and fall back to the deterministic GSL template (`gsl_ser.render_caption`) — never to
fiction (master plan §Pillar 13: "on verification failure the system degrades to the
deterministic GSL template, never to fiction").

Contracts consumed (all FROZEN — T-F10 owns them, we import, never edit):
  * `slot_accuracy.score(caption, state, checker=...)` — the SER slot scorer.
  * `groundedness.H4GroundednessAdapter` — the T-H4 checker, passed PER CALL (we do NOT
    mutate the module's global stub default; a T-F10 test pins it).
  * `gsl_ser.render_caption(state)` — the deterministic fallback / kill-switch comparator.

Design note (why this is NOT a residual instrument): `render_caption` is generated FROM the
slots, so it scores ~1.0 fidelity by construction — it is the CEILING, not a floor to beat.
A learned model can at best TIE it on fidelity. So the gate uses ABSOLUTE safety criteria
(zero ungrounded, zero wrong numerals), and the model's only reason to exist is the
kill-switch WIN axis — downstream task success / more natural task-conditioned summaries —
which is a different, offline-unmeasurable metric (see verdict in RECOGNITION_RESIDUAL.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .eval.slot_accuracy import score, ScoreReport
from .groundedness import GroundednessChecker, H4GroundednessAdapter
from .gsl_ser import render_caption, roundtrip_diff

# Ship-gate thresholds (master plan §Pillar 13 ship gates). Aggregate versions are enforced
# by the eval harness; the per-caption gate below uses the strict safety subset.
FIDELITY_MIN = 0.995        # numeric fidelity: correct / numeric-reference-slots
COVERAGE_MIN = 0.98         # slot coverage: 1 - missing/n_reference (full-narration mode)
UNSUPPORTED_MAX = 0.001     # unsupported-claim rate: extrinsic / n_reference


@dataclass
class GateResult:
    passed: bool
    reason: str
    slot_accuracy: float
    coverage: float
    extrinsic: int          # invented/ungrounded numerals — the hard safety failure
    wrong: int              # intrinsic hallucinations (wrong value for a real slot)
    missing: int
    n_reference: int

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reason": self.reason,
                "slot_accuracy": round(self.slot_accuracy, 4),
                "coverage": round(self.coverage, 4), "extrinsic": self.extrinsic,
                "wrong": self.wrong, "missing": self.missing,
                "n_reference": self.n_reference}


def check_caption(caption: str, state: dict, *,
                  checker: Optional[GroundednessChecker] = None,
                  require_coverage: bool = False) -> GateResult:
    """Score `caption` against `state` and decide whether it is SAFE to surface.

    Safety criterion (always): **zero extrinsic AND zero wrong numerals** — no invented and
    no misstated numbers. A task-conditioned summary may legitimately omit slots, so coverage
    is NOT a reject reason unless `require_coverage=True` (full-narration mode / eval)."""
    chk = checker or H4GroundednessAdapter()
    rep: ScoreReport = score(caption, state, checker=chk)
    coverage = 1.0 - (rep.missing / rep.n_reference) if rep.n_reference else 1.0

    if rep.extrinsic > 0:
        return _fail(rep, coverage, f"{rep.extrinsic} ungrounded numeral(s)")
    if rep.wrong > 0:
        return _fail(rep, coverage, f"{rep.wrong} wrong numeral(s) (intrinsic hallucination)")
    if rep.n_reference and rep.slot_accuracy < FIDELITY_MIN:
        return _fail(rep, coverage, f"fidelity {rep.slot_accuracy:.4f} < {FIDELITY_MIN}")
    if require_coverage and coverage < COVERAGE_MIN:
        return _fail(rep, coverage, f"coverage {coverage:.4f} < {COVERAGE_MIN}")
    return GateResult(True, "grounded", rep.slot_accuracy, coverage,
                      rep.extrinsic, rep.wrong, rep.missing, rep.n_reference)


def _fail(rep: ScoreReport, coverage: float, reason: str) -> GateResult:
    return GateResult(False, reason, rep.slot_accuracy, coverage,
                      rep.extrinsic, rep.wrong, rep.missing, rep.n_reference)


def narrate(state: dict, model_caption: Optional[str], *,
            checker: Optional[GroundednessChecker] = None,
            require_coverage: bool = False) -> tuple[str, str, GateResult]:
    """The narration-layer decision. Returns `(text, source, gate)`:
      * model_caption present AND gate passes -> (model_caption, "model", gate)
      * otherwise                              -> (render_caption(state), "template", gate)
    `source` is "template" both when the model is absent (no local translator provisioned —
    the offline/CI reality) and when its caption fails the gate. Deterministic template is
    always a safe, fully-grounded answer, so narration never fails."""
    if not model_caption:
        rep = score(render_caption(state), state, checker=checker or H4GroundednessAdapter())
        cov = 1.0 - (rep.missing / rep.n_reference) if rep.n_reference else 1.0
        g = GateResult(False, "no model caption (template used)", rep.slot_accuracy, cov,
                       rep.extrinsic, rep.wrong, rep.missing, rep.n_reference)
        return render_caption(state), "template", g
    gate = check_caption(model_caption, state, checker=checker,
                         require_coverage=require_coverage)
    if gate.passed:
        return model_caption, "model", gate
    return render_caption(state), "template", gate


def state_roundtrips(state: dict) -> bool:
    """The round-trip re-extraction ship gate: serialize->extract reproduces the slots.
    A state that does not round-trip means the serialization itself is lossy — the model
    would be trained on ambiguous input, so this must hold before trusting any caption."""
    return roundtrip_diff(state) == []
