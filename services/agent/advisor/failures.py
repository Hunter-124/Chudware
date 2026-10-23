"""Failure cards and bounded fixer loop.

Schema: ``aicad.failure/1`` (frozen in contracts/advisor.md §5).
Pillar 20: docs/design/ADVISOR_ARCHITECTURE.md §2.7.

Design
------
Every failure class (rebuild, solver, interference, overlap, dfm, sim, cam, export)
produces an ``aicad.failure/1`` card.  The card id is a deterministic hash of
``(class, where-key, why-detail)`` — stable across sessions so attempts accumulate
on the SAME card rather than creating a new card on each pass (which would silently
reset the budget and recreate the endless guess-loop this task exists to kill).

The fixer (``services/agent/roles/fixer.yaml``) is stateless and single-shot: it
applies ONE candidate, records prediction + measured delta, hands off.  The harness
in this module owns the outer loop: dispatch the fixer up to ``budget.max_attempts``
times, then escalate.  The model never loops; the harness does.

Verbatim-splice policy (merge-session memory): ``regen_diagnose``'s ``card.diagnosis``
is embedded as-is into the rebuild card's ``why.detail`` and ``candidate_fixes``.
Never paraphrase tool diagnoses into cards.

Escalation
----------
- ``ask_user``: fires a ``class:ask_user`` advisory item (same shape as §3 items)
  that the renderer surfaces to the human with the card and latest render as evidence.
- ``dag_replan``: calls ``services.dag.replan.dag_replan`` to splice a fixer node
  back into the running DAG with a bounded retry back-edge.
- Config default is ``ask_user``.  Cards declare their own ``on_exhaust`` from the
  per-class defaults; the harness never overrides a card's declared ``on_exhaust``.

Ledger
------
Card creation and attempt records are written as ``"artifact"`` events (the only
ledger kind that survives materialization and renders in ``ledger_render``).  The
``kind`` field within the artifact payload is ``"failure_card"`` / ``"fixer_attempt"``
so consumers can filter without parsing the full card JSON.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

SCHEMA = "aicad.failure/1"

# Per-class attempt budgets (contract §5: rebuild 3, interference 3, sim 2 default)
_DEFAULT_BUDGETS: dict[str, dict] = {
    "rebuild":      {"max_attempts": 3, "on_exhaust": "ask_user"},
    "solver":       {"max_attempts": 3, "on_exhaust": "ask_user"},
    "interference": {"max_attempts": 3, "on_exhaust": "ask_user"},
    "overlap":      {"max_attempts": 3, "on_exhaust": "ask_user"},
    "dfm":          {"max_attempts": 3, "on_exhaust": "ask_user"},
    "sim":          {"max_attempts": 2, "on_exhaust": "ask_user"},
    "cam":          {"max_attempts": 3, "on_exhaust": "ask_user"},
    "export":       {"max_attempts": 2, "on_exhaust": "ask_user"},
}

VALID_CLASSES = frozenset(_DEFAULT_BUDGETS)
VALID_RESULTS = frozenset({"same", "improved", "worse", "new_failure"})


# ---------------------------------------------------------------------------
# Deterministic card id
# ---------------------------------------------------------------------------

def _fc_id(cls: str, where_key: str, why_detail: str) -> str:
    """Stable id from (class, where-key, why-detail).

    Stability is LOAD-BEARING: the same failure on re-check must get the same id
    so attempts accumulate instead of resetting.  Do not use uuid4 here.
    """
    digest = hashlib.sha256(
        f"{cls}\x00{where_key}\x00{why_detail}".encode()
    ).hexdigest()[:12]
    return f"fc_{digest}"


def _where_key(where: dict) -> str:
    """Canonical string representation of the ``where`` dict for id hashing."""
    doc = where.get("doc_id", "")
    feats = ",".join(sorted(where.get("features") or []))
    bodies = ",".join(sorted(where.get("bodies") or []))
    return f"{doc}|{feats}|{bodies}"


# ---------------------------------------------------------------------------
# Card factory helpers
# ---------------------------------------------------------------------------

def _now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _make_card(
    cls: str,
    stage: str,
    what: str,
    where: dict,
    why: dict,
    candidate_fixes: list[dict],
    helper_tools: list[str],
    *,
    budget_override: Optional[dict] = None,
) -> dict:
    """Assemble a frozen-§5 failure card."""
    assert cls in VALID_CLASSES, f"unknown failure class: {cls!r}"
    budget = dict(_DEFAULT_BUDGETS[cls])
    if budget_override:
        budget.update(budget_override)

    fc_id = _fc_id(cls, _where_key(where), why.get("detail", ""))

    return {
        "schema": SCHEMA,
        "id": fc_id,
        "class": cls,
        "stage": stage,
        "what": what,
        "where": {
            "doc_id": where.get("doc_id", ""),
            "features": list(where.get("features") or []),
            "bodies": list(where.get("bodies") or []),
            "entities": list(where.get("entities") or []),
            "study": where.get("study"),
        },
        "why": {
            "kind": why.get("kind", "unknown"),
            "detail": why.get("detail", ""),
        },
        "candidate_fixes": list(candidate_fixes),
        "helper_tools": list(helper_tools),
        "attempts": [],
        "budget": budget,
        "ts": _now_ts(),
    }


# ---------------------------------------------------------------------------
# Per-class card factories
# ---------------------------------------------------------------------------

def card_rebuild(
    doc_id: str,
    feature_id: str,
    engine_error: str,
    *,
    regen_card: Optional[dict] = None,
    stage: str = "feature",
) -> dict:
    """Failure card for a rebuild failure.

    ``regen_card`` is the ``card`` dict returned by ``regen_diagnose`` /
    ``diagnose_failed_document``.  Its ``diagnosis`` field is verbatim-spliced
    into ``why.detail`` and its ``fix_ops`` into ``candidate_fixes`` — never
    paraphrased (verbatim-splice policy).
    """
    where = {"doc_id": doc_id, "features": [feature_id] if feature_id else []}

    if regen_card and isinstance(regen_card, dict):
        # Verbatim-splice: embed regen_card.diagnosis as-is.
        # build_fix_preview may store the Diagnosis dataclass object directly;
        # convert to dict if it has __dict__ (dataclass), else str-wrap.
        diagnosis_raw = regen_card.get("diagnosis")
        if isinstance(diagnosis_raw, dict):
            diagnosis = diagnosis_raw
        elif hasattr(diagnosis_raw, "__dict__"):
            # Dataclass instances have an instance __dict__; for old-style classes
            # (class attributes only, no __init__), fall back to type().__dict__.
            d = vars(diagnosis_raw)
            if not d:
                d = {k: v for k, v in vars(type(diagnosis_raw)).items()
                     if not k.startswith("_")}
            diagnosis = d  # still verbatim
        elif diagnosis_raw is not None:
            diagnosis = {"raw": str(diagnosis_raw)}
        else:
            diagnosis = {"raw": engine_error}
        fix_ops = regen_card.get("fix_ops") or []
        why_detail = diagnosis
        candidate_fixes = [
            {
                "action": op.get("op") or op.get("action", "apply_regen_fix"),
                "params": op,
                "basis": "regen_diagnose identified the rebuild cause",
                "predicted_effect": op.get("predicted_effect", "the feature rebuilds"),
                "confidence": "calc",
            }
            for op in fix_ops
            if isinstance(op, dict)
        ]
        if not candidate_fixes:
            # regen card has no fix_ops — fall back to a single diagnose action
            candidate_fixes = [{
                "action": "apply_regen_fix",
                "params": {"card": regen_card},
                "basis": "regen_diagnose identified the rebuild cause",
                "predicted_effect": "the feature rebuilds",
                "confidence": "calc",
            }]
        why_kind = "deterministic"
    else:
        why_detail = {"raw": engine_error} if engine_error else {}
        candidate_fixes = [{
            "action": "diagnose_rebuild",
            "params": {"doc_id": doc_id, "feature": feature_id},
            "basis": engine_error or "feature failed to rebuild",
            "predicted_effect": "regen_diagnose returns the cause + candidate repair",
            "confidence": "heuristic",
        }]
        why_kind = "heuristic" if engine_error else "unknown"

    return _make_card(
        cls="rebuild",
        stage=stage,
        what=f"feature {feature_id} failed to rebuild" if feature_id else "rebuild failure",
        where=where,
        why={"kind": why_kind, "detail": why_detail},
        candidate_fixes=candidate_fixes,
        helper_tools=["regen_diagnose", "regen_resolve"],
    )


def card_solver(
    doc_id: str,
    sketch_id: str,
    conflicting: list[dict],
    *,
    stage: str = "sketch",
) -> dict:
    """Failure card for a sketch solver conflict (over-constrained).

    ``conflicting`` is the ``conflicting`` list from ``sketch_health`` — each
    entry is a dict ``{"id": "c7", "type": "parallel", "suggestion": "..."}``.
    The list is verbatim-spliced into ``why.detail`` and each entry becomes a
    ``delete_constraint`` candidate fix with the kernel's own suggestion text.

    Note: the kernel key is ``"conflicting"`` (not ``"conflicting_tags"`` — that
    is an internal solver symbol that surfaces in ``source.detail`` only).
    """
    where = {"doc_id": doc_id, "entities": [sketch_id] if sketch_id else []}

    if conflicting:
        candidate_fixes = [
            {
                "action": "delete_constraint",
                "params": {"doc_id": doc_id, "sketch": sketch_id, "id": c.get("id")},
                "basis": c.get("suggestion") or (
                    f"constraint {c.get('id')!r} ({c.get('type')}) flagged as conflicting by sketch_health"
                ),
                "predicted_effect": (
                    f"removing {c.get('id')!r} resolves the over-constraint"
                ),
                "confidence": "kernel",
            }
            for c in conflicting
            if isinstance(c, dict)
        ]
        why_kind = "deterministic"
        why_detail = {"conflicting": list(conflicting)}  # verbatim-splice
    else:
        candidate_fixes = [{
            "action": "inspect_sketch_constraints",
            "params": {"doc_id": doc_id, "sketch": sketch_id},
            "basis": "sketch solver conflict detected",
            "predicted_effect": "identify and remove the conflicting constraint",
            "confidence": "heuristic",
        }]
        why_kind = "heuristic"
        why_detail = {"raw": "solver conflict — no constraint detail available"}

    return _make_card(
        cls="solver",
        stage=stage,
        what=f"sketch {sketch_id} over-constrained" if sketch_id else "sketch solver conflict",
        where=where,
        why={"kind": why_kind, "detail": why_detail},
        candidate_fixes=candidate_fixes,
        helper_tools=["sketch_health", "sketch_auto_constrain"],
    )


def card_interference(advisory_item: dict, *, stage: str = "assembly") -> dict:
    """Promote an ``aicad.advisory/1`` interference item to a failure card.

    Reuses the ``candidate_fixes`` already computed by engine.py's
    ``_fix_interference`` — no duplication of fix logic.
    """
    refs = advisory_item.get("refs", {})
    numbers = advisory_item.get("numbers", {})
    bodies = refs.get("bodies") or []
    features = refs.get("features") or []
    doc_id = refs.get("doc_id", "")
    finding = advisory_item.get("finding", "interference detected")

    where = {"doc_id": doc_id, "bodies": bodies, "features": features}
    why_detail: Any = {"overlap_mm3": numbers.get("overlap_mm3")}
    if numbers.get("bore_d") is not None:
        why_detail["bore_d"] = numbers["bore_d"]
        why_detail["shaft_d"] = numbers["shaft_d"]

    candidate_fixes = list(advisory_item.get("candidate_fixes") or [])
    if not candidate_fixes:
        candidate_fixes = [{
            "action": "resize_or_move_body",
            "params": {"body": bodies[0] if bodies else "unknown", "hint": "shrink or move the overlapping feature"},
            "basis": finding,
            "predicted_effect": "removes the interference volume",
            "confidence": "heuristic",
        }]

    return _make_card(
        cls="interference",
        stage=stage,
        what=finding,
        where=where,
        why={"kind": "deterministic" if numbers.get("overlap_mm3") is not None else "heuristic",
             "detail": why_detail},
        candidate_fixes=candidate_fixes,
        helper_tools=list(advisory_item.get("helper_tools") or ["fit_calc", "measure_between"]),
    )


def card_overlap(advisory_item: dict, *, stage: str = "feature") -> dict:
    """Promote an ``aicad.advisory/1`` overlap item to a failure card.

    Reuses ``candidate_fixes`` from engine.py's ``_fix_overlap``.
    """
    refs = advisory_item.get("refs", {})
    features = refs.get("features") or []
    doc_id = refs.get("doc_id", "")
    finding = advisory_item.get("finding", "feature overlap detected")
    numbers = advisory_item.get("numbers", {})

    where = {"doc_id": doc_id, "features": features}
    why_detail: Any = {}
    if numbers.get("classification"):
        why_detail["classification"] = numbers["classification"]

    candidate_fixes = list(advisory_item.get("candidate_fixes") or [])
    if not candidate_fixes:
        candidate_fixes = [{
            "action": "review_feature_order",
            "params": {},
            "basis": finding,
            "predicted_effect": "resolves the overlap once the offending feature is identified",
            "confidence": "heuristic",
        }]

    why_kind = "deterministic" if numbers.get("classification") else "heuristic"

    return _make_card(
        cls="overlap",
        stage=stage,
        what=finding,
        where=where,
        why={"kind": why_kind, "detail": why_detail},
        candidate_fixes=candidate_fixes,
        helper_tools=list(advisory_item.get("helper_tools") or ["check_feature_overlap", "describe_part"]),
    )


def card_dfm(
    doc_id: str,
    checks: list[dict],
    *,
    stage: str = "part_verify",
) -> list[dict]:
    """One card per FAILED DFM check from ``dfm_check``.

    ``checks`` is the ``checks`` list from the ``dfm_check`` tool response —
    each entry is ``{"check": "min_wall", "value_mm": 1.2, "limit_mm": 1.0,
    "ok": True, "note": "..."}``.  Only failed checks (``ok`` is ``False``)
    produce cards.  The full check dict is verbatim-spliced into ``why.detail``.
    """
    cards = []
    for f in checks:
        if not isinstance(f, dict):
            continue
        if f.get("ok", True):
            continue  # pass — no card needed

        check_name = f.get("check") or "dfm_check"
        note = f.get("note") or check_name
        where = {"doc_id": doc_id}
        why_detail = dict(f)  # verbatim-splice the full check dict

        # Build a concrete fix from the check type + numeric gap
        value = f.get("value_mm")
        limit = f.get("limit_mm")
        if check_name == "min_wall" and value is not None and limit is not None:
            candidate_fixes = [{
                "action": "increase_wall_thickness",
                "params": {"doc_id": doc_id, "target_mm": limit},
                "basis": note,
                "predicted_effect": f"wall increases from {value:.2f} to ≥{limit:.2f} mm",
                "confidence": "table",
            }]
        elif check_name == "draft" and f.get("faces_below", 0) > 0:
            candidate_fixes = [{
                "action": "add_draft_angle",
                "params": {"doc_id": doc_id, "min_degrees": 1.0},
                "basis": note,
                "predicted_effect": "draft angle meets process minimum",
                "confidence": "heuristic",
            }]
        else:
            candidate_fixes = [{
                "action": "review_dfm_check",
                "params": {"doc_id": doc_id, "check": check_name},
                "basis": note,
                "predicted_effect": "DFM violation resolved once addressed",
                "confidence": "heuristic",
            }]

        why_kind = "deterministic" if value is not None else "heuristic"
        cards.append(_make_card(
            cls="dfm",
            stage=stage,
            what=note,
            where=where,
            why={"kind": why_kind, "detail": why_detail},
            candidate_fixes=candidate_fixes,
            helper_tools=["dfm_check", "wall_thickness"],
        ))
    return cards


def card_sim(
    study_id: str,
    doc_id: str,
    margin_misses: list[dict],
    *,
    stage: str = "sim",
) -> list[dict]:
    """One card per simulation margin miss from ``describe_results``.

    ``margin_misses`` is the ``margin_misses`` or ``failures`` list from the
    ``describe_results`` / ``run_fea_static`` response.  The tool's numbers are
    verbatim-spliced into ``why.detail``.
    """
    cards = []
    for miss in margin_misses:
        what = miss.get("summary") or miss.get("description") or "simulation margin miss"
        where = {"doc_id": doc_id, "features": list(miss.get("features") or []),
                 "bodies": list(miss.get("bodies") or []), "study": study_id}
        why_detail = dict(miss)  # verbatim-splice

        candidate_fixes = [{
            "action": "increase_section",
            "params": {
                "doc_id": doc_id,
                "body": miss.get("bodies", [None])[0],
                "hint": "increase wall thickness or add gussets to raise safety factor",
            },
            "basis": miss.get("summary") or what,
            "predicted_effect": f"safety factor improves from {miss.get('sf', '?')} toward target",
            "confidence": "heuristic",
        }]

        cards.append(_make_card(
            cls="sim",
            stage=stage,
            what=what,
            where=where,
            why={"kind": "deterministic" if miss.get("sf") is not None else "heuristic",
                 "detail": why_detail},
            candidate_fixes=candidate_fixes,
            helper_tools=["describe_results", "run_fea_static", "measure_part"],
        ))
    return cards


def card_cam(
    job_id: str,
    doc_id: str,
    sim_results: list[dict],
    *,
    stage: str = "cam",
) -> list[dict]:
    """One card per CAM simulation violation from ``cam_simulate``.

    ``sim_results`` is the ``results`` list from ``cam_simulate``'s response —
    each entry is a ``SimResult`` dict with keys ``setup_id``, ``gouges``,
    ``max_gouge_mm``, ``rapid_collisions``, ``holder_collisions``, ``warnings``.

    One card is produced per setup that has gouges or rapid/holder collisions.
    The full per-setup result is verbatim-spliced into ``why.detail``.
    """
    cards = []
    for result in sim_results:
        if not isinstance(result, dict):
            continue

        setup_id = result.get("setup_id", "")
        gouges = result.get("gouges", 0)
        rapid_cols = result.get("rapid_collisions", 0)
        holder_cols = result.get("holder_collisions", 0)
        max_gouge = result.get("max_gouge_mm", 0.0)
        warnings = result.get("warnings") or []

        violations = []
        if gouges:
            violations.append(("gouge", gouges, max_gouge))
        if rapid_cols:
            violations.append(("rapid_collision", rapid_cols, None))
        if holder_cols:
            violations.append(("holder_collision", holder_cols, None))

        if not violations and not warnings:
            continue  # clean simulation — no card

        why_detail = dict(result)  # verbatim-splice the full SimResult

        for vtype, count, extra in violations:
            if vtype == "gouge":
                what = f"gouge in setup {setup_id}: {count} gouge(s), max {max_gouge:.3f} mm"
                candidate_fixes = [{
                    "action": "adjust_toolpath_clearance",
                    "params": {"job_id": job_id, "setup_id": setup_id,
                               "hint": "increase clearance or reduce depth of cut"},
                    "basis": what,
                    "predicted_effect": "gouge count goes to 0",
                    "confidence": "heuristic",
                }]
            elif vtype == "rapid_collision":
                what = f"rapid collision in setup {setup_id}: {count} collision(s)"
                candidate_fixes = [{
                    "action": "add_lead_in",
                    "params": {"job_id": job_id, "setup_id": setup_id,
                               "hint": "replace rapid move through material with a feed move"},
                    "basis": what,
                    "predicted_effect": "rapid-through-material collision count goes to 0",
                    "confidence": "heuristic",
                }]
            else:
                what = f"holder collision in setup {setup_id}: {count} collision(s)"
                candidate_fixes = [{
                    "action": "increase_holder_clearance",
                    "params": {"job_id": job_id, "setup_id": setup_id,
                               "hint": "select a longer reach holder or tilt the workpiece"},
                    "basis": what,
                    "predicted_effect": "holder collision count goes to 0",
                    "confidence": "heuristic",
                }]

            cards.append(_make_card(
                cls="cam",
                stage=stage,
                what=what,
                where={"doc_id": doc_id},
                why={"kind": "deterministic", "detail": why_detail},
                candidate_fixes=candidate_fixes,
                helper_tools=["cam_simulate", "cam_update_operation", "cam_generate"],
            ))

        # Warnings-only (no hard violation): one card for warnings
        if not violations and warnings:
            what = f"CAM warnings in setup {setup_id}: {'; '.join(str(w) for w in warnings[:3])}"
            cards.append(_make_card(
                cls="cam",
                stage=stage,
                what=what,
                where={"doc_id": doc_id},
                why={"kind": "heuristic", "detail": why_detail},
                candidate_fixes=[{
                    "action": "review_cam_warnings",
                    "params": {"job_id": job_id, "setup_id": setup_id},
                    "basis": what,
                    "predicted_effect": "warnings resolved after toolpath review",
                    "confidence": "heuristic",
                }],
                helper_tools=["cam_simulate", "cam_update_operation", "cam_generate"],
            ))
    return cards


def card_export(
    doc_id: str,
    format_: str,
    error: str,
    *,
    stage: str = "deliver",
) -> dict:
    """Failure card for an export failure."""
    where = {"doc_id": doc_id}
    why_detail = {"format": format_, "error": error}

    candidate_fixes = [{
        "action": "retry_export",
        "params": {"doc_id": doc_id, "format": format_},
        "basis": error or "export failed",
        "predicted_effect": "export succeeds",
        "confidence": "heuristic",
    }]

    return _make_card(
        cls="export",
        stage=stage,
        what=f"export to {format_!r} failed",
        where=where,
        why={"kind": "heuristic" if not error else "deterministic",
             "detail": why_detail},
        candidate_fixes=candidate_fixes,
        helper_tools=["export_part"],
    )


# ---------------------------------------------------------------------------
# Attempt protocol
# ---------------------------------------------------------------------------

def record_attempt(
    card: dict,
    fix_applied: dict,
    result: str,
    delta: dict,
    *,
    predicted_effect: str = "",
) -> dict:
    """Append one attempt to *card* and return it (mutates in place).

    Enforces one-candidate-per-attempt: ``fix_applied`` must be a single fix dict.
    ``result`` must be in VALID_RESULTS.  ``delta`` is the measured change (e.g.
    ``{"overlap_mm3_before": 340, "overlap_mm3_after": 0}``).

    The card's ``candidate_fixes`` are NOT mutated — the attempt records which fix
    was applied, not what remains.  The harness decides whether to dispatch again.
    """
    if result not in VALID_RESULTS:
        raise ValueError(f"result must be one of {sorted(VALID_RESULTS)}, got {result!r}")
    if not isinstance(fix_applied, dict):
        raise TypeError("fix_applied must be a dict (one candidate fix)")

    n = len(card["attempts"]) + 1
    attempt = {
        "n": n,
        "fix_applied": fix_applied,
        "predicted_effect": predicted_effect,
        "result": result,
        "delta": delta,
        "ts": _now_ts(),
    }
    card["attempts"].append(attempt)
    return card


def budget_exhausted(card: dict) -> bool:
    """True when the number of recorded attempts equals the card's max_attempts.

    The harness MUST check this after every attempt and escalate exactly when True.
    Never before (premature escalation) and never after (infinite loop).
    """
    return len(card["attempts"]) >= card["budget"]["max_attempts"]


# ---------------------------------------------------------------------------
# Escalation executor (harness-owned — the model never calls this directly)
# ---------------------------------------------------------------------------

def escalate(
    card: dict,
    *,
    run_id: Optional[str] = None,
    after_node_id: Optional[str] = None,
    runs_dir=None,
    ledger=None,
    render_url: Optional[str] = None,
) -> dict:
    """Fire the escalation declared in ``card.budget.on_exhaust``.

    This is harness logic — called by the outer fixer loop after ``budget_exhausted``
    is True.  The model never calls this function.

    Supported escalation targets:
    - ``"ask_user"``: return an advisory item the renderer surfaces to the human.
    - ``"dag_replan"``: splice a bounded fixer node into the running DAG.

    Returns a dict with ``{"escalation": "ask_user"|"dag_replan", ...details...}``.
    """
    on_exhaust = card["budget"].get("on_exhaust", "ask_user")

    if on_exhaust == "dag_replan" and run_id and after_node_id:
        return _escalate_dag_replan(card, run_id=run_id, after_node_id=after_node_id,
                                    runs_dir=runs_dir)

    # Default: ask_user (also fires when dag_replan params are missing)
    return _escalate_ask_user(card, render_url=render_url)


def _escalate_ask_user(card: dict, *, render_url: Optional[str] = None) -> dict:
    """Surface the exhausted card to the human as a deterministic advisory item."""
    attempts_summary = "; ".join(
        f"attempt {a['n']}: {a['result']}" for a in card["attempts"]
    )
    finding = (
        f"{card['class']} failure {card['id']} exhausted {card['budget']['max_attempts']} "
        f"attempts without resolution. {attempts_summary}. Human input required."
    )
    advisory_item = {
        "id": f"esc_{card['id']}",
        "severity": "blocker",
        "check": f"{card['class']}_exhausted",
        "finding": finding,
        "refs": card["where"],
        "numbers": {"attempts": len(card["attempts"])},
        "candidate_fixes": [],
        "helper_tools": card.get("helper_tools", []),
        "source": {"kind": "harness", "detail": "budget_exhausted"},
        "card": card,
    }
    if render_url:
        advisory_item["render_url"] = render_url
    return {"escalation": "ask_user", "advisory_item": advisory_item, "card": card}


def _escalate_dag_replan(
    card: dict,
    *,
    run_id: str,
    after_node_id: str,
    runs_dir=None,
) -> dict:
    """Splice a bounded fixer node into the running DAG sidecar.

    The spliced node satisfies ``replan.py``'s splice requirements:
    - non-empty ``end_condition``
    - a retry back-edge with ``retry_budget`` matching the card's remaining budget
    """
    node_id = f"fix_{card['id']}"
    splice_nodes = [{
        "id": node_id,
        "type": "agent",
        "name": f"fixer: {card['class']} {card['id']}",
        "inputs": {"failure_card": card},
        "on_fail": "pause",
        "autonomy": "auto",
        "end_condition": (
            f"failure card {card['id']} resolved or human escalation requested"
        ),
    }]
    splice_edges = [
        {"from": node_id, "to": after_node_id,
         "retry_budget": card["budget"]["max_attempts"]},
    ]
    try:
        from services.dag.replan import dag_replan
        merged = dag_replan(
            run_id=run_id,
            splice_nodes=splice_nodes,
            splice_edges=splice_edges,
            after_node_id=after_node_id,
            runs_dir=runs_dir,
        )
        return {"escalation": "dag_replan", "merged_graph": merged, "card": card}
    except Exception as exc:  # noqa: BLE001
        # Graceful degradation: if DAG replan fails fall back to ask_user
        result = _escalate_ask_user(card)
        result["dag_replan_error"] = str(exc)
        return result


# ---------------------------------------------------------------------------
# Ledger integration
# ---------------------------------------------------------------------------

def ledger_note_card(ledger, actor: str, card: dict) -> None:
    """Write a card-creation event to the ledger as an ``artifact`` event.

    The ``artifact`` kind is the only one that survives materialization and
    renders in ``ledger_render`` (all other unknown kinds are silently dropped).
    """
    if ledger is None:
        return
    try:
        ledger.append(actor, "artifact", {
            "kind": "failure_card",
            "id": card["id"],
            "path": "",
            "stage": card.get("stage", ""),
            "note": f"class={card['class']} what={card['what']!r}",
            "card": card,
        })
    except Exception:  # noqa: BLE001
        pass  # ledger is advisory — never break a call


def ledger_note_attempt(ledger, actor: str, card: dict, attempt: dict) -> None:
    """Write an attempt event to the ledger."""
    if ledger is None:
        return
    try:
        ledger.append(actor, "artifact", {
            "kind": "fixer_attempt",
            "id": f"{card['id']}_attempt_{attempt['n']}",
            "path": "",
            "stage": card.get("stage", ""),
            "note": (
                f"card={card['id']} n={attempt['n']} result={attempt['result']} "
                f"delta={attempt.get('delta', {})}"
            ),
            "attempt": attempt,
        })
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Engine integration: promote blocker advisories to failure cards
# ---------------------------------------------------------------------------

def promote_advisory_blockers(
    advisory: dict,
    *,
    stage: Optional[str] = None,
) -> list[dict]:
    """Convert all ``blocker`` advisory items in *advisory* to failure cards.

    Called by the engine after ``build_advisory`` when any item has
    ``severity == "blocker"``.  The stage defaults to the advisory's own stage.
    Returns a (possibly empty) list of cards.
    """
    effective_stage = stage or advisory.get("stage", "feature")
    cards = []
    for item in advisory.get("items", []):
        if item.get("severity") != "blocker":
            continue
        cls = _item_class(item)
        if cls == "interference":
            cards.append(card_interference(item, stage=effective_stage))
        elif cls == "overlap":
            cards.append(card_overlap(item, stage=effective_stage))
        # Other classes (open_profile, underconstrained) are not promoted to
        # failure cards automatically — they require explicit factory calls.
    return cards


def _item_class(item: dict) -> str:
    """Map an advisory item's ``check`` field to a failure class."""
    check = item.get("check", "")
    if "interference" in check:
        return "interference"
    if "overlap" in check:
        return "overlap"
    if "rebuild" in check:
        return "rebuild"
    if "sketch" in check or "profile" in check:
        return "solver"
    return "unknown"
