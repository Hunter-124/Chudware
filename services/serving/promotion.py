"""services/serving/promotion.py — canary promotion gate + rollback (T-H9).

Encodes Erik's RATIFIED promotion criteria (2026-07-02, binding):

    A challenger adapter may be promoted only when it beats-or-matches the incumbent on
    BOTH verifier-score AND accept-rate over N real shadow jobs (T-H7's shadow log is the
    evidence artifact), AND Erik manually signs off on THAT promotion. Automated evidence,
    human trigger. There is NO auto-promote code path in v1.

Two independent conditions, both necessary, neither sufficient alone:
  1. AUTOMATED EVIDENCE — :func:`evaluate_promotion` computes eligibility from a shadow log.
  2. HUMAN SIGN-OFF     — a :class:`SignOff` token (approver + a reference to the signed-off
     report) must be supplied to :func:`promote`. Metrics passing does NOT promote anything;
     only a human calling promote with a valid sign-off does.

Rollback (:func:`rollback`) is the always-safe inverse: a routing-table flip back to a
previous DVC-versioned adapter. Nothing is overwritten and no confirmation is required —
rolling back is never dangerous (contract §7).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from .routing import RouteEntry, RoutingError, RoutingTable

# The verifier score key inside a shadow row's *_scores dict (T-H7 / shadow_report).
DEFAULT_VERIFIER_KEY = "verifier"
DEFAULT_MIN_CALLS = 100


class PromotionRefused(RuntimeError):
    """Raised whenever a promotion is not permitted. Promote NEVER silently no-ops."""


# ---------------------------------------------------------------------------
# Automated evidence (half 1 of the gate)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PromotionEvidence:
    """The automated half of the gate — computed from a shadow log, never sufficient alone."""

    insertion_point: str
    n: int
    min_calls: int
    incumbent_verifier: float
    challenger_verifier: float
    incumbent_accept_rate: float
    challenger_accept_rate: float

    @property
    def enough_calls(self) -> bool:
        return self.n >= self.min_calls

    @property
    def verifier_pass(self) -> bool:
        # beat-or-match
        return self.challenger_verifier >= self.incumbent_verifier

    @property
    def accept_pass(self) -> bool:
        return self.challenger_accept_rate >= self.incumbent_accept_rate

    @property
    def eligible(self) -> bool:
        """True only if there is enough data AND the challenger wins BOTH metrics."""
        return self.enough_calls and self.verifier_pass and self.accept_pass

    @property
    def reason(self) -> str:
        if not self.enough_calls:
            return f"insufficient shadow calls: {self.n} < {self.min_calls}"
        fails = []
        if not self.verifier_pass:
            fails.append(
                f"verifier {self.challenger_verifier:.4f} < {self.incumbent_verifier:.4f}")
        if not self.accept_pass:
            fails.append(
                f"accept-rate {self.challenger_accept_rate:.4f} < {self.incumbent_accept_rate:.4f}")
        return "eligible" if not fails else "; ".join(fails)

    def to_dict(self) -> dict:
        return {
            "insertion_point": self.insertion_point,
            "n": self.n,
            "min_calls": self.min_calls,
            "incumbent_verifier": self.incumbent_verifier,
            "challenger_verifier": self.challenger_verifier,
            "incumbent_accept_rate": self.incumbent_accept_rate,
            "challenger_accept_rate": self.challenger_accept_rate,
            "verifier_pass": self.verifier_pass,
            "accept_pass": self.accept_pass,
            "enough_calls": self.enough_calls,
            "eligible": self.eligible,
            "reason": self.reason,
        }


def evaluate_promotion(
    shadow_rows: Iterable,
    insertion_point: str,
    *,
    min_calls: int = DEFAULT_MIN_CALLS,
    verifier_key: str = DEFAULT_VERIFIER_KEY,
) -> PromotionEvidence:
    """Aggregate a T-H7 shadow log into :class:`PromotionEvidence` for one insertion point.

    ``shadow_rows`` are :class:`tools.flywheel.shadow_report.ShadowRow` (or any objects with
    the same attributes). Only rows matching *insertion_point* are counted. Uses the same
    ``_Agg`` aggregation as ``shadow_report`` so evidence == the human-read report.
    """
    from tools.flywheel.shadow_report import _Agg  # reuse the report's aggregation

    inc, ch = _Agg(), _Agg()
    for r in shadow_rows:
        if getattr(r, "insertion_point", None) != insertion_point:
            continue
        inc.add(r.incumbent_latency_ms, r.incumbent_accepted, r.incumbent_scores)
        ch.add(r.challenger_latency_ms, r.challenger_accepted, r.challenger_scores)

    return PromotionEvidence(
        insertion_point=insertion_point,
        n=inc.n(),
        min_calls=min_calls,
        incumbent_verifier=inc.mean_scores().get(verifier_key, 0.0),
        challenger_verifier=ch.mean_scores().get(verifier_key, 0.0),
        incumbent_accept_rate=inc.accept_rate(),
        challenger_accept_rate=ch.accept_rate(),
    )


def write_evidence_report(
    shadow_rows: list,
    insertion_point: str,
    out_path: Path,
    *,
    min_calls: int = DEFAULT_MIN_CALLS,
    verifier_key: str = DEFAULT_VERIFIER_KEY,
) -> PromotionEvidence:
    """Write the human-readable shadow report + a machine verdict JSON to *out_path*.

    This is the artifact Erik reviews before signing off. It is evidence only — writing it
    promotes NOTHING. Returns the computed :class:`PromotionEvidence`.
    """
    import json

    from tools.flywheel.shadow_report import generate_report

    ev = evaluate_promotion(
        shadow_rows, insertion_point, min_calls=min_calls, verifier_key=verifier_key)
    ip_rows = [r for r in shadow_rows if getattr(r, "insertion_point", None) == insertion_point]
    text = generate_report(
        ip_rows, min_calls=min_calls, title=f"Promotion Evidence — {insertion_point}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    verdict = {
        "verdict": ev.to_dict(),
        "NOTE": (
            "Automated evidence only. Promotion additionally REQUIRES Erik's manual sign-off "
            "(a SignOff referencing this file). Eligible=true is necessary, NOT sufficient."
        ),
    }
    out_path.write_text(
        text + "\n\n--- MACHINE VERDICT ---\n" + json.dumps(verdict, indent=2) + "\n",
        encoding="utf-8",
    )
    return ev


# ---------------------------------------------------------------------------
# Human sign-off (half 2 of the gate)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignOff:
    """The human half of the gate. Constructed by/for Erik at promotion time.

    ``report_ref`` is the path to the signed-off evidence report (written by
    :func:`write_evidence_report`). A SignOff with no approver or no report is invalid and
    :func:`promote` refuses it.
    """

    approver: str
    report_ref: str
    note: str = ""

    def valid(self) -> bool:
        return bool(self.approver.strip()) and bool(self.report_ref.strip())


# ---------------------------------------------------------------------------
# Promote (gated) + rollback (always safe)
# ---------------------------------------------------------------------------

def promote(
    table: RoutingTable,
    insertion_point: str,
    adapter_version: str,
    *,
    evidence: PromotionEvidence | None,
    signoff: SignOff | None,
    report_exists_fn: Callable[[str], bool] = os.path.exists,
) -> RouteEntry:
    """Promote *adapter_version* at *insertion_point* to full production (100%).

    Refuses (raises :class:`PromotionRefused`) unless ALL hold — there is deliberately NO
    branch that promotes from evidence alone:

    1. a human ``signoff`` is supplied and valid (approver + report_ref);
    2. the ``signoff.report_ref`` file exists (the signed-off evidence artifact);
    3. ``evidence`` is present, for this insertion point, and ``evidence.eligible`` (challenger
       beats-or-matches incumbent on BOTH verifier-score AND accept-rate over ≥ min_calls).

    On success the routing table flips to the new adapter at 100%, pushing the outgoing
    version onto ``history`` so a later :func:`rollback` can restore it. Nothing is deleted.
    """
    # (1) human trigger — the non-negotiable half. No sign-off => never promote.
    if signoff is None:
        raise PromotionRefused(
            "promotion requires Erik's manual sign-off per promotion (ratified 2026-07-02); "
            "there is no auto-promote path."
        )
    if not signoff.valid():
        raise PromotionRefused("sign-off invalid: both an approver and a report_ref are required")
    if not report_exists_fn(signoff.report_ref):
        raise PromotionRefused(f"sign-off report_ref not found: {signoff.report_ref!r}")

    # (2) automated evidence — necessary but NOT sufficient on its own.
    if evidence is None:
        raise PromotionRefused("no shadow evidence supplied")
    if evidence.insertion_point != insertion_point:
        raise PromotionRefused(
            f"evidence is for {evidence.insertion_point!r}, not {insertion_point!r}")
    if not evidence.eligible:
        raise PromotionRefused(f"challenger not eligible: {evidence.reason}")

    entry = table.get(insertion_point)
    if entry is None:
        raise PromotionRefused(f"no route entry for {insertion_point!r} to promote into")

    prev = entry.adapter_version
    new_history = entry.history + ((prev,) if prev and prev != adapter_version else ())
    new_entry = replace(
        entry, adapter_version=adapter_version, canary_pct=100.0, history=new_history)
    table.put(new_entry)
    return new_entry


def set_canary(
    table: RoutingTable,
    insertion_point: str,
    pct: float,
    *,
    opt_in: Iterable[str] | None = None,
) -> RouteEntry:
    """Set the canary traffic percentage (and optional opt-in list) — the ladder's middle rung.

    Not gated: dialing canary traffic up/down is a routing knob, not a promotion. Promotion to
    100% production is :func:`promote` and IS gated.
    """
    entry = table.get(insertion_point)
    if entry is None:
        raise RoutingError(f"no route entry for {insertion_point!r}")
    if not (0.0 <= float(pct) <= 100.0):
        raise RoutingError(f"canary pct must be in [0,100]; got {pct!r}")
    kw = {"canary_pct": float(pct)}
    if opt_in is not None:
        kw["canary_opt_in"] = tuple(opt_in)
    new_entry = replace(entry, **kw)
    table.put(new_entry)
    return new_entry


def rollback(
    table: RoutingTable,
    insertion_point: str,
    *,
    to_version: str | None = None,
) -> RouteEntry:
    """Roll back to a previous DVC-versioned adapter — always safe, no confirmation.

    - ``to_version`` given  -> flip straight to that version (history untouched).
    - ``to_version`` None    -> pop the most recent entry off ``history`` and restore it; if
      history is empty, drop to the base model (``adapter_version=None``), which makes
      :meth:`RoutingTable.resolve` serve the kill-switch fallback.

    This only flips routing state; the adapter artifacts themselves are never overwritten or
    deleted (they live under DVC versioning), so a rollback is reversible and instantaneous.
    """
    entry = table.get(insertion_point)
    if entry is None:
        raise RoutingError(f"no route entry for {insertion_point!r}")

    if to_version is not None:
        new_entry = replace(entry, adapter_version=to_version)
    elif entry.history:
        target = entry.history[-1]
        new_entry = replace(entry, adapter_version=target, history=entry.history[:-1])
    else:
        # nothing to roll back to -> base model; resolve() will serve the fallback.
        new_entry = replace(entry, adapter_version=None)

    table.put(new_entry)
    return new_entry
