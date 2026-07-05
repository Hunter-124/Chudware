"""Advisory engine ("foreman") — findings → aicad.advisory/1 items (T-W5).

Contract: ``contracts/advisor.md`` §3 (advisory block, FROZEN) and §9 (precedence).
Pillar 20: docs/ADVISOR_ARCHITECTURE.md §2.2.

Design (per the advisor review):

  * The **engine is a pure function** over a *normalized observation* — it never
    sees ``res.fit`` or a raw RPC.  Two thin adapters (``adapters.py``) turn a
    host-specific result (loop.py ``ModelResult`` / ``AssemblyResult``, or a
    doc_id via ``doc_eval_*``) into a list of :class:`Finding` objects.
  * The engine maps findings → advisory items, attaching **kernel/table-computed
    candidate fixes** where the data exists, and named ``helper_tools`` always.
  * Deterministic, synchronous, no LLM in the hot path.

Engine invariant (contract §3): ``candidate_fixes`` may be empty ONLY for
``info`` severity.  Any ``hint`` / ``action`` / ``blocker`` item MUST carry at
least one candidate fix.  This is enforced in :func:`build_advisory` and has a
dedicated test — it is the easiest §3 rule to breach.

Candidate-fix providers are feature-flagged individually (task note: "T-W2/T-W3
may merge mid-task; feature-flag the candidate-fix providers individually") via
``AI_CAD_ADVISOR_FIX_<NAME>`` env vars, all default-on.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from . import stages

SCHEMA = "aicad.advisory/1"

# Severity ordering (low → high); used by the renderer's drop policy.
SEVERITY_ORDER: dict[str, int] = {"info": 0, "hint": 1, "action": 2, "blocker": 3}


# ---------------------------------------------------------------------------
# Normalized observation (adapter output → engine input)
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """One normalized check result, host-agnostic.

    Adapters produce these from ``res.fit`` / ``res.error`` (loop path) or from
    ``doc_eval_*`` (doc path).  ``kind`` selects the engine's fix provider.
    """
    kind: str                       # interference | overlap | open_profile | rebuild | ...
    check: str                      # the check id (stages.CHECK_*)
    severity: str                   # info | hint | action | blocker (proposed; engine may adjust)
    finding: str                    # human-readable one-liner
    numbers: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, Any] = field(default_factory=dict)
    source_kind: str = "kernel"     # kernel | table | calc | heuristic | llm
    source_detail: str = ""         # e.g. "doc_eval_interference"
    # Optional extra context an adapter can pass to a fix provider (e.g. measured
    # bore/shaft diameters on the doc path — absent on the loop path).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    """Everything the engine needs for one advisory pass, host-agnostic."""
    stage: str
    findings: list[Finding] = field(default_factory=list)
    seq: int = 0
    doc_id: str = ""
    user_override: bool = False     # §9.2: a live user instruction conflicts


# ---------------------------------------------------------------------------
# Feature flags for candidate-fix providers
# ---------------------------------------------------------------------------

def _fix_enabled(name: str) -> bool:
    return os.environ.get(f"AI_CAD_ADVISOR_FIX_{name.upper()}", "1") not in ("0", "false", "False")


# ---------------------------------------------------------------------------
# Candidate-fix providers (kernel/table-computed; §3 numbers-from-kernel rule)
# ---------------------------------------------------------------------------

def _adv_id(seq: int, kind: str, salt: str) -> str:
    h = hashlib.sha256(f"{seq}:{kind}:{salt}".encode()).hexdigest()[:8]
    return f"adv_{h}"


def _fix_interference(f: Finding) -> tuple[list[dict], list[str]]:
    """Interference → fit-based bore/shaft suggestion when diameters are known.

    On the **doc path** the adapter measures cylindrical-face diameters and puts
    them in ``f.extra`` (``bore_d`` / ``shaft_d`` / ``fit`` / ``feature``): we run
    ``fit_calc`` (T-W3) and emit an ``edit_feature_param`` fix with real numbers.

    On the **loop path** no diameters exist (``res.fit`` carries volume + names
    only), so we emit a heuristic move/resize fix and advertise ``fit_calc`` as a
    helper — keeping the §3 "non-info ⇒ non-empty candidate_fixes" invariant.
    """
    helper = ["fit_calc", "measure_between"]
    bore_d = f.extra.get("bore_d")
    shaft_d = f.extra.get("shaft_d")
    feature = f.extra.get("feature")

    if _fix_enabled("fit") and bore_d is not None and shaft_d is not None and feature:
        fit = f.extra.get("fit") or "H7/g6"
        nominal = f.extra.get("nominal") or round(float(shaft_d), 3)
        try:
            from engine_oracle.research.fits import fit_calc
            calc = fit_calc(nominal, fit, prefilled={"nominal": f.source_detail})
        except Exception:  # noqa: BLE001
            calc = None
        if calc and calc.get("ok") and calc.get("answer", {}).get("hole_absolute"):
            hole = calc["answer"]["hole_absolute"]
            clr = calc["answer"].get("clearance")
            new_bore = round((hole[0] + hole[1]) / 2, 3)
            basis = f"ISO 286 {fit} on Ø{nominal} → hole {hole[0]:.3f}..{hole[1]:.3f}"
            pred = (f"clearance {clr[0]:.3f}..{clr[1]:.3f} mm" if clr
                    else f"hole {hole[0]:.3f}..{hole[1]:.3f} mm")
            return [{
                "action": "edit_feature_param",
                "params": {"feature": feature, "param": "d", "value": new_bore},
                "basis": basis,
                "predicted_effect": pred,
                "confidence": "table",
            }], helper

    # Loop path (or fit provider off / calc failed): heuristic move/resize.
    a = f.refs.get("bodies", ["A", "B"])[0] if f.refs.get("bodies") else "one body"
    return [{
        "action": "resize_or_move_body",
        "params": {"body": a, "hint": "shrink the overlapping feature or move it clear"},
        "basis": f"bodies overlap {f.numbers.get('overlap_mm3', '?')} mm³",
        "predicted_effect": "removes the interference volume",
        "confidence": "heuristic",
    }], helper


def _fix_overlap(f: Finding) -> tuple[list[dict], list[str]]:
    """Feature self-overlap → reorder / param hint from the overlap classification."""
    helper = ["check_feature_overlap", "describe_part"]
    cls = f.extra.get("classification") or f.numbers.get("classification")
    feats = f.refs.get("features") or []
    later = feats[-1] if feats else None
    if cls == "swallowed" and later:
        return [{
            "action": "reorder_feature",
            "params": {"feature": later, "before": feats[0] if feats else None},
            "basis": "later feature is swallowed by an earlier solid; build it first",
            "predicted_effect": "feature contributes geometry instead of being absorbed",
            "confidence": "heuristic",
        }], helper
    if later:
        return [{
            "action": "edit_feature_param",
            "params": {"feature": later, "param": "position", "value": None},
            "basis": "redundant/overlapping cut; adjust its extent or position",
            "predicted_effect": "removes the redundant overlap",
            "confidence": "heuristic",
        }], helper
    return [{
        "action": "review_feature_order",
        "params": {},
        "basis": "overlapping features detected; inspect build order",
        "predicted_effect": "resolves the overlap once the offending feature is found",
        "confidence": "heuristic",
    }], helper


def _fix_open_profile(f: Finding) -> tuple[list[dict], list[str]]:
    """Open sketch profile → auto_constrain proposal (T-W2)."""
    helper = ["doc_sketch_auto_constrain", "doc_sketch_health"]
    return [{
        "action": "auto_constrain",
        "params": {"doc_id": f.refs.get("doc_id", ""), "sketch": f.refs.get("sketch")},
        "basis": "endpoint pairs within tolerance can be made coincident to close the profile",
        "predicted_effect": f"{f.numbers.get('open_chains', 'the open')} chain(s) close",
        "confidence": "calc",
    }], helper


def _fix_rebuild(f: Finding) -> tuple[list[dict], list[str]]:
    """Rebuild error → wrap regen_diagnose (first failure-card producer)."""
    helper = ["regen_diagnose", "regen_resolve"]
    diag = f.extra.get("diagnosis")
    if diag:
        return [{
            "action": "apply_regen_fix",
            "params": diag if isinstance(diag, dict) else {"detail": str(diag)},
            "basis": "regen_diagnose identified the rebuild cause",
            "predicted_effect": "the feature rebuilds",
            "confidence": "calc",
        }], helper
    return [{
        "action": "diagnose_rebuild",
        "params": {"doc_id": f.refs.get("doc_id", "")},
        "basis": f.finding,
        "predicted_effect": "regen_diagnose returns the cause + candidate repair",
        "confidence": "heuristic",
    }], helper


_FIX_PROVIDERS = {
    "interference": _fix_interference,
    "overlap": _fix_overlap,
    "open_profile": _fix_open_profile,
    "rebuild": _fix_rebuild,
}


# ---------------------------------------------------------------------------
# Engine core
# ---------------------------------------------------------------------------

def _compute_fixes(f: Finding) -> tuple[list[dict], list[str]]:
    """Return (candidate_fixes, extra_helper_tools) for a finding, if a provider
    exists.  Providers never raise — a failing provider yields no computed fix."""
    provider = _FIX_PROVIDERS.get(f.kind)
    if provider is None:
        return [], []
    try:
        return provider(f)
    except Exception:  # noqa: BLE001
        return [], []


def _finding_to_item(seq: int, stage: str, f: Finding) -> dict:
    fixes, extra_helpers = _compute_fixes(f)

    # Engine invariant (§3): non-info items MUST carry ≥1 candidate fix.  If a
    # provider produced none, downgrade to info rather than emit an illegal item.
    severity = f.severity
    if severity != "info" and not fixes:
        severity = "info"

    helpers = list(dict.fromkeys(stages.helper_tools(stage) + extra_helpers))

    return {
        "id": _adv_id(seq, f.kind, f.finding),
        "severity": severity,
        "check": f.check,
        "finding": f.finding,
        "refs": {"doc_id": f.refs.get("doc_id", ""), **f.refs},
        "numbers": f.numbers,
        "candidate_fixes": fixes,
        "helper_tools": helpers,
        "source": {"kind": f.source_kind, "detail": f.source_detail},
    }


def build_advisory(obs: Observation) -> dict:
    """Convert a normalized :class:`Observation` into an aicad.advisory/1 block.

    Returns ``{schema, seq, stage, items, user_override}``.  ``items`` is the
    materialized list (renderer applies the token budget + drop policy).  Items
    are sorted highest-severity-first so the renderer drops from the tail.
    """
    items = [_finding_to_item(obs.seq, obs.stage, f) for f in obs.findings]

    # §9.2: on user-override conflict, advisories downgrade to info (the renderer
    # also prepends the "user override active" banner).
    if obs.user_override:
        for it in items:
            it["severity"] = "info"

    items.sort(key=lambda it: -SEVERITY_ORDER.get(it["severity"], 0))

    return {
        "schema": SCHEMA,
        "seq": obs.seq,
        "stage": obs.stage,
        "items": items,
        "user_override": obs.user_override,
    }


def max_severity(advisory: dict) -> str:
    """Highest severity across the block's items (``"info"`` if empty)."""
    best = 0
    for it in advisory.get("items", []):
        best = max(best, SEVERITY_ORDER.get(it.get("severity", "info"), 0))
    for name, rank in SEVERITY_ORDER.items():
        if rank == best:
            return name
    return "info"


def has_blocker(advisory: dict) -> bool:
    """True if any item is a ``blocker`` — gates stage exit per §9.3."""
    return any(it.get("severity") == "blocker" for it in advisory.get("items", []))
