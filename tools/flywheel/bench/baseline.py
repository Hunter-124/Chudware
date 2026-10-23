"""Baseline comparators for the bench suite.

Every bench carries TWO baseline rows:

  * `deterministic` — the kill-switch comparator: a rule-based, no-model strategy that is
    always available offline. A candidate model must beat this to earn a promotion (the
    kill-switch rule: never ship below the deterministic path).
  * `frontier` — a frontier-model strategy. Offline / in CI there are no credentials and no
    network, so this row is recorded honestly as `status='pending'` (no fabricated scores),
    to be filled once frontier-provider logging + credentials are available. No score is
    ever fabricated for an unavailable comparator.

A Baseline is just a callable strategy the bench applies per example; the bench owns what
"apply" means (propose a fix, produce a brief, etc.). This module only fixes the two-row
contract and the pending-frontier convention.
"""
from __future__ import annotations

from typing import Callable, Optional

from .schema import BaselineRow


class Baseline:
    def __init__(self, name: str, kind: str, fn: Optional[Callable] = None,
                 *, available: bool = True, note: str = ""):
        assert kind in ("deterministic", "frontier"), kind
        self.name = name
        self.kind = kind
        self.fn = fn
        self.available = available and fn is not None
        self.note = note

    def apply(self, example):
        if not self.available or self.fn is None:
            raise RuntimeError(f"baseline {self.name!r} is not available (status pending)")
        return self.fn(example)

    def row(self, *, mean_score: Optional[float] = None,
            pass_rate: Optional[float] = None) -> BaselineRow:
        if self.available:
            return BaselineRow(name=self.name, kind=self.kind, status="measured",
                               mean_score=mean_score, pass_rate=pass_rate, note=self.note)
        return BaselineRow(name=self.name, kind=self.kind, status="pending",
                           note=self.note or "no credentials/network offline; frontier run required")


def pending_frontier(name: str = "frontier",
                     note: str = "frontier model; pending provider logging + credentials") -> Baseline:
    """The standard honest-stub frontier row every bench uses offline."""
    return Baseline(name=name, kind="frontier", fn=None, available=False, note=note)
