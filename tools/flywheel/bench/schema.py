"""Frozen result contract for the product-task eval benches.

Every bench emits example-level scores (ROC-AUC-style), never only a corpus average,
and records the held-out STRUCTURE each example belongs to (hold out structures,
not just samples, so scores measure generalization).

Pure stdlib. These dataclasses are the on-disk report schema — keep them append-only /
additive so old reports stay readable (mirror the contract discipline of contracts/).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SCHEMA_VERSION = "aicad.bench/1"


@dataclass
class ExampleScore:
    """One scored example.

    score:      normalized [0,1] quality from the verifier battery (higher = better).
    passed:     did this example meet the bench's success predicate.
    label:      ground-truth positive membership, for ROC-AUC style ranking metrics
                (e.g. regen-fix: 1 if the doc is genuinely fixable). Optional — set
                None when the bench has no binary ground-truth label.
    structure:  the held-out STRUCTURE key this example belongs to (split unit). Two
                examples sharing a structure must never straddle the train/test split.
    baselines:  {baseline_name -> score} for the same example (deterministic, frontier).
    detail:     per-check breakdown / diagnostics (kept small).
    """
    example_id: str
    score: float
    passed: bool
    structure: str
    label: Optional[bool] = None
    baselines: dict[str, float] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class BaselineRow:
    """A baseline comparator row for a bench.

    kind: 'deterministic' (kill-switch comparator, computed offline) or 'frontier'
          (model-backed; offline/CI records status='pending' with no scores — honest
          stub until frontier-provider logging + credentials exist).
    """
    name: str
    kind: str  # 'deterministic' | 'frontier'
    status: str  # 'measured' | 'pending'
    mean_score: Optional[float] = None
    pass_rate: Optional[float] = None
    note: str = ""


@dataclass
class BenchResult:
    bench_id: str
    dataset_id: str
    dataset_hash: str
    split: str  # 'test' | 'smoke' | 'all'
    n_examples: int
    examples: list[ExampleScore] = field(default_factory=list)
    baselines: list[BaselineRow] = field(default_factory=list)
    aggregate: dict[str, Any] = field(default_factory=dict)
    residual: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent, sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "BenchResult":
        d = json.loads(text)
        exs = [ExampleScore(**e) for e in d.pop("examples", [])]
        bls = [BaselineRow(**b) for b in d.pop("baselines", [])]
        return BenchResult(examples=exs, baselines=bls, **d)
