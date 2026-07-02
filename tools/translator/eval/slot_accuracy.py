#!/usr/bin/env python3
"""SER-style slot-accuracy scorer — the primary numeric-fidelity metric (Pillar 13).

`score(caption, state)` aligns every numeral in a caption to the ground-truth slots
(`gsl_ser.enumerate_slots(state)`) and classifies each REFERENCE slot as:

    correct   — a caption numeral with the right label + value (unit-normalised)
    wrong     — a caption numeral for this slot's label but a different value  (INTRINSIC
                hallucination: wrong value for a real feature)
    missing   — no caption numeral for this slot

and every UNCONSUMED caption numeral as:

    extrinsic — ungrounded (invented feature; verdict from the groundedness seam)
    extra     — grounded to some slot but not the aligned one (double-mention, not a lie)

Metrics (master-plan-mandated): slot accuracy = correct / |reference|; intrinsic and
extrinsic counts REPORTED SEPARATELY; numeric accuracy stratified by magnitude bucket
and unit; a per-example hallucination score for ROC-AUC. Reference-free and exact — we
hold the true state, so this needs no gold caption and no learned metric.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from tools.translator import groundedness as _gnd
from tools.translator.gsl_ser import (Slot, _BARE_UNITS, _PROSE_NUM_RE, _UNIT_ALIAS,
                                       enumerate_slots)

# FERMAT-style magnitude buckets (subword tokenisation degrades numeracy by scale).
_MAG_BUCKETS = ((0.0, 1.0, "<1"), (1.0, 10.0, "1-10"), (10.0, 100.0, "10-100"),
                (100.0, 1000.0, "100-1k"), (1000.0, float("inf"), ">=1k"))

# Non-distinguishing tokens: section/domain prefixes + filler. Keywords are drawn from
# the last two path segments MINUS these, so a per-line prefix like `fea`/`cam` (which
# appears in every clause and would match everything) never becomes a keyword.
_STOP = {"is", "the", "a", "of", "on", "at", "and", "with", "part", "feat", "op",
         "trace", "bore", "mode", "dim", "sym", "fea", "cam", "ecad", "sketch", "brep"}

_CLAUSE_RE = re.compile(r"[.\n;,]")


def _mag_bucket(value: float) -> str:
    v = abs(float(value))
    for lo, hi, name in _MAG_BUCKETS:
        if lo <= v < hi:
            return name
    return ">=1k"


def _keywords(path: str) -> set[str]:
    """Distinguishing label tokens for a slot: tokens of the last TWO path segments,
    stop/prefix words dropped (so `cam.op.Rough.machined` -> {rough, machined})."""
    segs = path.split(".")
    toks = re.split(r"[\-_.]", ".".join(segs[-2:]).lower())
    kw = {t for t in toks if t and t not in _STOP and not t.replace(".", "").isdigit()}
    kw.discard("")
    return kw or {re.split(r"[\-_.]", path.lower())[-1]}


@dataclass
class _Numeral:
    value: float
    unit: str
    pos: int          # char offset of the numeral in the (lowercased) caption
    clause: str       # lowercased text of the numeral's own clause (both sides)
    clause_start: int
    consumed: bool = False

    def keyword_distance(self, kw: set[str]) -> Optional[int]:
        """Char distance from this numeral to the NEAREST slot keyword within its own
        clause (both sides), or None if no keyword occurs in the clause."""
        best: Optional[int] = None
        rel = self.pos - self.clause_start
        for word in kw:
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])",
                                  self.clause):
                d = abs(m.start() - rel)
                best = d if best is None else min(best, d)
        return best


def _scan_numerals(caption: str) -> list[_Numeral]:
    low = caption.lower()
    # clause boundaries so keyword proximity never crosses a sentence/comma boundary.
    bounds = [0] + [m.start() for m in _CLAUSE_RE.finditer(low)] + [len(low)]
    out: list[_Numeral] = []
    for m in _PROSE_NUM_RE.finditer(caption):
        num = float(m.group("num"))
        unit_raw = (m.group("unit") or "none").lower()
        base_u, factor = _UNIT_ALIAS.get(unit_raw, ("none", 1.0))
        start = m.start("num")
        lo = max(b for b in bounds if b <= start)
        hi = min(b for b in bounds if b > start)
        out.append(_Numeral(num * factor, base_u, start, low[lo:hi], lo))
    return out


@dataclass
class SlotVerdict:
    path: str
    unit: str
    kind: str
    status: str                      # correct | wrong | missing
    expected: Any = None
    got: Optional[float] = None


@dataclass
class ScoreReport:
    n_reference: int
    correct: int
    wrong: int                       # intrinsic hallucinations
    missing: int
    extrinsic: int                   # invented numerals (ungrounded)
    extra: int                       # grounded but duplicate mentions
    verdicts: list[SlotVerdict] = field(default_factory=list)
    by_magnitude: dict[str, dict[str, int]] = field(default_factory=dict)
    by_unit: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def slot_accuracy(self) -> float:
        return (self.correct / self.n_reference) if self.n_reference else 1.0

    @property
    def intrinsic_rate(self) -> float:
        return (self.wrong / self.n_reference) if self.n_reference else 0.0

    @property
    def hallucination_score(self) -> float:
        """Per-example hallucination signal for ROC-AUC: intrinsic + extrinsic per
        reference slot (0.0 == clean). Higher == more hallucinated."""
        denom = max(1, self.n_reference)
        return (self.wrong + self.extrinsic) / denom

    def to_dict(self) -> dict:
        return {
            "n_reference": self.n_reference, "correct": self.correct, "wrong": self.wrong,
            "missing": self.missing, "extrinsic": self.extrinsic, "extra": self.extra,
            "slot_accuracy": round(self.slot_accuracy, 4),
            "intrinsic_rate": round(self.intrinsic_rate, 4),
            "hallucination_score": round(self.hallucination_score, 4),
            "by_magnitude": self.by_magnitude, "by_unit": self.by_unit,
        }


def _bump(table: dict[str, dict[str, int]], key: str, status: str) -> None:
    row = table.setdefault(key, {"correct": 0, "wrong": 0, "missing": 0})
    row[status] = row.get(status, 0) + 1


def score(caption: str, state: dict, *, checker=None) -> ScoreReport:
    """Score one caption against the ground-truth slots of `state`."""
    reference = enumerate_slots(state)
    numerals = _scan_numerals(caption)
    rep = ScoreReport(n_reference=sum(1 for s in reference if s.numeric),
                      correct=0, wrong=0, missing=0, extrinsic=0, extra=0)

    for slot in reference:
        if not slot.numeric:
            continue                      # categorical slots aren't part of numeric fidelity
        kw = _keywords(slot.path)
        mag = _mag_bucket(slot.value)
        cand = _select(numerals, slot, kw)
        if cand is None:
            status = "missing"
            rep.missing += 1
            rep.verdicts.append(SlotVerdict(slot.path, slot.unit, slot.kind, status,
                                            expected=slot.value))
        else:
            cand.consumed = True
            if slot.matches(cand.value, cand.unit):
                status = "correct"
                rep.correct += 1
            else:
                status = "wrong"
                rep.wrong += 1
            rep.verdicts.append(SlotVerdict(slot.path, slot.unit, slot.kind, status,
                                            expected=slot.value, got=cand.value))
        _bump(rep.by_magnitude, mag, status)
        _bump(rep.by_unit, slot.unit, status)

    # unconsumed numerals: extrinsic (ungrounded) vs extra (grounded duplicate).
    leftover = [n for n in numerals if not n.consumed]
    if leftover:
        chk = checker or _gnd.get_checker()
        grounded_vals = {(round(v.value, 6), v.unit) for v in chk.check(caption, state)
                         if v.grounded}
        for n in leftover:
            if (round(n.value, 6), n.unit) in grounded_vals or _grounds(n, reference):
                rep.extra += 1
            else:
                rep.extrinsic += 1
    return rep


def _unit_ok(n: _Numeral, slot: Slot) -> bool:
    if slot.unit in _BARE_UNITS:
        return n.unit in (slot.unit, "none")
    return n.unit == slot.unit


def _clause_tokens(clause: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", clause) if t}


def _select(numerals: list[_Numeral], slot: Slot, kw: set[str]) -> Optional[_Numeral]:
    """Choose the caption numeral for `slot`. Among unit-compatible unconsumed numerals,
    rank by keyword DISTINCTIVENESS first (a keyword shared by many candidate clauses,
    like "tool", is weak; a rare one, like the op name "rough" or the axis "x", is the
    real anchor even when it sits farther from the numeral), then by proximity, then a
    value match. When no keyword is present at all, fall back to a pure value match
    (a paraphrase that dropped the label).

    Returning a keyword-anchored candidate with a WRONG value is intentional: that is an
    intrinsic hallucination the caller records as `wrong`, not a miss."""
    cands = [n for n in numerals if not n.consumed and _unit_ok(n, slot)]
    if not cands:
        return None
    # document frequency of each keyword across the candidate clauses (rarer == stronger).
    df = {w: sum(1 for n in cands if w in _clause_tokens(n.clause)) for w in kw}

    def rank(n: _Numeral) -> tuple:
        toks = _clause_tokens(n.clause)
        present = [w for w in kw if w in toks and df[w] > 0]
        weight = max((1.0 / df[w] for w in present), default=0.0)
        dist = n.keyword_distance(kw)
        return (-weight, dist if dist is not None else 1 << 30,
                0 if slot.matches(n.value, n.unit) else 1, n.pos)

    best = min(cands, key=rank)
    if _clause_tokens(best.clause) & {w for w in kw if df[w] > 0}:
        return best
    # no keyword anywhere -> value match only.
    for n in cands:
        if slot.matches(n.value, n.unit):
            return n
    return None


def _grounds(n: _Numeral, reference: list[Slot]) -> bool:
    return any(s.numeric and s.matches(n.value, n.unit) for s in reference)


__all__ = ["score", "ScoreReport", "SlotVerdict"]
