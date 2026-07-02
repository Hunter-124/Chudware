"""Candidate translator evaluation + winner pick (T-F11 DoD item 1).

The T-F10 harness scores the deterministic TEMPLATE (the ~1.0 ceiling / scorer self-check).
This adds the missing piece: score a CANDIDATE MODEL's raw captions over the gold set and
apply the ship gates. Reuses `slot_accuracy.score` and the gold loader — never re-implements
scoring.

Framing (advisor-corrected — NOT a residual instrument): `render_caption` scores ~1.0 by
construction, so a model can at best TIE it on fidelity. The gates here are therefore
ABSOLUTE thresholds (fidelity ≥99.5%, coverage ≥98%, unsupported <0.1% on held-out
STRUCTURES) plus fidelity NON-REGRESSION vs the template. The model's reason to ship is the
kill-switch WIN axis (downstream task success), which is a DIFFERENT metric measured by a
separate harness (see the verdict spec) and is unmeasurable offline.

Proven offline by mock candidates (echo-template -> ties ceiling; hallucinating -> fails the
gates), so the path is real numbers, never all-skip. Real candidates need a served model
(pending-provision offline).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .eval.harness import load_gold, GOLD_DIR, heldout_structures
from .eval.slot_accuracy import score
from .gate import FIDELITY_MIN, COVERAGE_MIN, UNSUPPORTED_MAX
from .groundedness import GroundednessChecker, H4GroundednessAdapter
from .gsl_ser import render_caption, roundtrip_diff

# complete_fn: build_prompt already applied by the caller? No — candidate eval calls the model
# with the state's serialization directly via the passed captioner: (state) -> caption.
Captioner = Callable[[dict], str]


@dataclass
class CandidateReport:
    name: str
    available: bool
    n: int
    fidelity: float               # mean slot_accuracy (numeric fidelity)
    coverage: float               # mean 1 - missing/n_reference
    unsupported_rate: float       # mean extrinsic / n_reference
    intrinsic_rate: float         # mean wrong / n_reference
    roundtrip_ok: bool
    passes_gates: bool
    template_fidelity: float      # the ceiling, for the non-regression check
    note: str = ""
    per_example: list = field(default_factory=list)

    def as_row(self) -> dict:
        return {"candidate": self.name, "available": self.available,
                "fidelity": round(self.fidelity, 4), "coverage": round(self.coverage, 4),
                "unsupported_rate": round(self.unsupported_rate, 4),
                "intrinsic_rate": round(self.intrinsic_rate, 4),
                "roundtrip_ok": self.roundtrip_ok, "passes_gates": self.passes_gates,
                "non_regression_vs_template": round(self.fidelity - self.template_fidelity, 4),
                "note": self.note}


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def evaluate_candidate(name: str, captioner: Optional[Captioner], *,
                       checker: Optional[GroundednessChecker] = None,
                       held_out_only: bool = False) -> CandidateReport:
    """Score a candidate's captions over the gold set against the ship gates. `captioner`
    None (no served model) -> pending-provision report, never a fabricated score."""
    if captioner is None:
        return CandidateReport(name, False, 0, 0, 0, 0, 0, False, False, 0.0,
                               note="pending-provision (no served model); see provision_instructions()")
    chk = checker or H4GroundednessAdapter()
    examples = load_gold(GOLD_DIR)
    if held_out_only:
        ho = set(heldout_structures(examples))
        examples = [e for e in examples if e.get("structure_key", e.get("domain")) in ho]

    fids, covs, unsup, intr, tmpl_fids = [], [], [], [], []
    roundtrip_ok = True
    per = []
    for ex in examples:
        state = ex["state"]
        if roundtrip_diff(state):
            roundtrip_ok = False
        cap = captioner(state)
        rep = score(cap, state, checker=chk)
        n = max(1, rep.n_reference)
        fids.append(rep.slot_accuracy)
        covs.append(1.0 - rep.missing / n)
        unsup.append(rep.extrinsic / n)
        intr.append(rep.wrong / n)
        tmpl_fids.append(score(render_caption(state), state, checker=chk).slot_accuracy)
        per.append({"example": ex.get("id", ex.get("domain")), "fidelity": round(rep.slot_accuracy, 4),
                    "extrinsic": rep.extrinsic, "wrong": rep.wrong})

    fidelity, coverage = _mean(fids), _mean(covs)
    unsupported, intrinsic = _mean(unsup), _mean(intr)
    template_fidelity = _mean(tmpl_fids)
    passes = (fidelity >= FIDELITY_MIN and coverage >= COVERAGE_MIN
              and unsupported < UNSUPPORTED_MAX and roundtrip_ok
              and fidelity >= template_fidelity - 1e-9)   # non-regression vs template
    return CandidateReport(name, True, len(examples), fidelity, coverage, unsupported,
                           intrinsic, roundtrip_ok, passes, template_fidelity, per_example=per)


def pick_winner(reports: list[CandidateReport]) -> dict:
    """Pick the winner among evaluated candidates: highest fidelity among those passing the
    gates; ties broken by coverage then lower unsupported. Returns a verdict dict. If none
    are available (offline), the verdict is pending-provision — NOT a fabricated pick."""
    available = [r for r in reports if r.available]
    if not available:
        return {"winner": None, "status": "pending-provision",
                "note": "no candidate produced captions offline; provision + re-run"}
    passing = [r for r in available if r.passes_gates]
    pool = passing or available
    winner = max(pool, key=lambda r: (r.fidelity, r.coverage, -r.unsupported_rate))
    return {"winner": winner.name, "status": "measured" if passing else "no-candidate-passes-gates",
            "rows": [r.as_row() for r in available]}
