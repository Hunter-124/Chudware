"""Advisory engine ("foreman") — findings → aicad.advisory/1 items.

Contract: ``contracts/advisor.md`` §3 (advisory block, FROZEN) and §9 (precedence).
Pillar 20: docs/design/ADVISOR_ARCHITECTURE.md §2.2.

Design:

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

Candidate-fix providers are feature-flagged individually via
``CHUDWARE_ADVISOR_FIX_<NAME>`` env vars, all default-on.
"""
from __future__ import annotations

import hashlib
import math
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
    # Trigger fields — all optional; default-empty keeps existing callers intact.
    output_text: str = ""           # model/user text for keyword matching (§20)
    tool_names: list[str] = field(default_factory=list)   # tool calls this turn
    events: list[str] = field(default_factory=list)       # geometry events this turn
    cooldown_state: Optional[dict] = None  # {trigger_id: last_fired_seq}; None=no cooldown


# ---------------------------------------------------------------------------
# Feature flags for candidate-fix providers
# ---------------------------------------------------------------------------

def _fix_enabled(name: str) -> bool:
    return os.environ.get(f"CHUDWARE_ADVISOR_FIX_{name.upper()}", "1") not in ("0", "false", "False")


# ---------------------------------------------------------------------------
# Candidate-fix providers (kernel/table-computed; §3 numbers-from-kernel rule)
# ---------------------------------------------------------------------------

def _adv_id(seq: int, kind: str, salt: str) -> str:
    h = hashlib.sha256(f"{seq}:{kind}:{salt}".encode()).hexdigest()[:8]
    return f"adv_{h}"


def _overlap_axis_from_bboxes(bbox_a: dict, bbox_b: dict) -> tuple[str, float]:
    """Derive the minimum-translation-separation axis and penetration depth from two AABBs.

    The kernel returns ``parts:[{name, bbox:{min:[x,y,z], max:[x,y,z], size:[...]}, volume}]``
    but no pre-computed axis.  We compute it here from the AABB intersection: the axis whose
    overlap extent is **smallest** is the direction the model must move the fewest mm to resolve
    the clash.

    Returns ``(axis_label, penetration_mm)`` where ``axis_label`` is e.g. ``"+X"``, ``"-Y"``
    (the direction body_b should move to exit body_a), and ``penetration_mm`` is the overlap
    depth along that axis.  Returns ``("?", 0.0)`` when bbox data is missing or degenerate.

    Source: kernel ``interference_report`` → ``parts[i].bbox.{min,max}``; calculation is
    purely from those numbers — no geometry guess-work.
    """
    try:
        mn_a = bbox_a.get("min", [])
        mx_a = bbox_a.get("max", [])
        mn_b = bbox_b.get("min", [])
        mx_b = bbox_b.get("max", [])
        if len(mn_a) < 3 or len(mx_a) < 3 or len(mn_b) < 3 or len(mx_b) < 3:
            return "?", 0.0
        axes = ["X", "Y", "Z"]
        overlaps: list[tuple[float, str, bool]] = []  # (depth, axis_name, b_is_positive)
        for i, ax in enumerate(axes):
            lo = max(mn_a[i], mn_b[i])
            hi = min(mx_a[i], mx_b[i])
            depth = hi - lo
            if depth <= 0.0:
                return "?", 0.0  # no AABB overlap on this axis → degenerate
            # positive direction: b's max is greater than a's max → b extends in +axis
            # relative to a, so moving b in +axis increases separation.
            b_positive = mx_b[i] > mx_a[i]
            overlaps.append((depth, ax, b_positive))
        # minimum-translation axis = smallest overlap depth
        overlaps.sort(key=lambda t: t[0])
        min_depth, min_ax, b_pos = overlaps[0]
        sign = "+" if b_pos else "-"
        return f"{sign}{min_ax}", round(min_depth, 3)
    except Exception:  # noqa: BLE001
        return "?", 0.0


def _fix_interference(f: Finding) -> tuple[list[dict], list[str]]:
    """Interference → targeted diagnosis with axis + magnitude + named fix.

    The finding names the colliding bodies, the overlap
    magnitude in mm³, the MTS (minimum-translation-separation) axis derived from
    the per-body AABBs already in the kernel return, and a targeted move/resize
    suggestion with a concrete distance.

    On the **doc path** the adapter may also carry cylindrical-face diameters
    (``bore_d`` / ``shaft_d`` / ``feature`` in ``f.extra``): when present we run
    ``fit_calc`` and emit an ``edit_feature_param`` fix with real numbers.

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
            from services.research.fits import fit_calc
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

    # Loop path (or fit provider off / calc failed): axis-derived move/resize fix.
    bodies = f.refs.get("bodies", [])
    a_name = bodies[0] if bodies else "body_a"
    b_name = bodies[1] if len(bodies) > 1 else "body_b"
    vol = f.numbers.get("overlap_mm3", "?")

    # Derive axis from per-body bboxes when the adapter captured them (doc path).
    bbox_a = f.extra.get("bbox_a") or {}
    bbox_b = f.extra.get("bbox_b") or {}
    axis, depth_mm = _overlap_axis_from_bboxes(bbox_a, bbox_b)

    if axis != "?" and depth_mm > 0.0:
        hint = (f"move or resize {b_name} by ≥{depth_mm:.3f} mm along {axis} "
                f"to eliminate the overlap")
        basis = (f"{a_name}↔{b_name} overlap {vol} mm³; "
                 f"MTS axis {axis}, depth {depth_mm:.3f} mm (from kernel AABB)")
        pred = "interference cleared along the minimum-translation axis"
        confidence = "calc"  # derived from kernel AABB numbers — not a pure heuristic
    else:
        hint = f"shrink or move {a_name} or {b_name} to eliminate the overlap"
        basis = f"bodies overlap {vol} mm³; axis unavailable (no bbox data)"
        pred = "removes the interference volume"
        confidence = "heuristic"

    return [{
        "action": "resize_or_move_body",
        "params": {"body": b_name, "hint": hint, "axis": axis, "depth_mm": depth_mm},
        "basis": basis,
        "predicted_effect": pred,
        "confidence": confidence,
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
    """Open sketch profile → auto_constrain proposal."""
    helper = ["sketch_auto_constrain", "sketch_health"]
    return [{
        "action": "auto_constrain",
        "params": {"doc_id": f.refs.get("doc_id", ""), "sketch": f.refs.get("sketch")},
        "basis": "endpoint pairs within tolerance can be made coincident to close the profile",
        "predicted_effect": f"{f.numbers.get('open_chains', 'the open')} chain(s) close",
        "confidence": "calc",
    }], helper


def _fix_sketch_quality(f: Finding) -> tuple[list[dict], list[str]]:
    """Keep actionable design-intent and solver findings from being downgraded."""
    sketch = f.refs.get("sketch")
    helpers = ["describe_sketch", "sketch_health", "check_sketch", "add_features"]
    if f.kind in ("under_dimensioned", "underconstrained"):
        return [{
            "action": "add_features",
            "params": {
                "doc_id": f.refs.get("doc_id", ""),
                "ops": [{"op": "sketch_fully_define", "sketch": sketch,
                         "relations": True, "dimensions": True, "anchor": "origin"}],
            },
            "basis": "after explicit design relations and dimensions, define residual DOF",
            "predicted_effect": "adds non-fix constraints; recheck DOF and pinned_only",
            "confidence": "heuristic",
        }], helpers
    actions = {
        "pinned_instead_of_constrained": (
            "replace_fixed_constraints",
            "remove fix relations from authored profile entities, preserve converted external "
            "references, and replace them with geometric relations and driving dimensions",
        ),
        "no_driving_dimensions": (
            "add_driving_dimensions",
            "choose critical lengths/radii and offsets as driving dimensions, not driven "
            "measurements; replace redundant relations where necessary",
        ),
        "unreferenced_profile": (
            "anchor_sketch_profile",
            "reference profile geometry to origin, sketch axes or converted model geometry",
        ),
        "sketch_conflict": (
            "resolve_sketch_constraints",
            "inspect conflicting constraints and remove or correct the contradictory relation "
            "before adding dimensions",
        ),
    }
    action, basis = actions[f.kind]
    return [{
        "action": action,
        "params": {"doc_id": f.refs.get("doc_id", ""), "sketch": sketch,
                   "entities": f.refs.get("entities", []),
                   "conflicting": f.extra.get("conflicting", [])},
        "basis": basis,
        "predicted_effect": "restores editable design intent; recheck sketch health afterwards",
        "confidence": "heuristic",
    }], helpers


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


def _fix_sim_bc(f: Finding) -> tuple[list[dict], list[str]]:
    """BC-lint issue → advisory fix stub (real fix in sim_advisor._bc_fix)."""
    helper = ["run_fea_static", "describe_part"]
    kind = f.extra.get("bc_kind", "no_fixture")
    fix = {
        "action": "fix_bc",
        "params": {"bc_issue": kind, "hint": "Correct the boundary condition before solving."},
        "basis": f.finding,
        "predicted_effect": "FEA solve proceeds with valid boundary conditions.",
        "confidence": "heuristic",
    }
    return [fix], helper


def _fix_sim_margin(f: Finding) -> tuple[list[dict], list[str]]:
    """Margin verdict → redesign suggestion."""
    helper = ["run_fea_static", "describe_results"]
    sf = f.numbers.get("sf_yield", 1.5)
    mat_class = f.numbers.get("mat_class", "general")
    fix = {
        "action": "redesign_cross_section",
        "params": {"hint": f"Reduce stress or increase section — target: max_stress × {sf} ≤ yield."},
        "basis": f"margin_policy [{mat_class}] SF={sf}",
        "predicted_effect": "Part passes margin check.",
        "confidence": "heuristic",
    }
    return [fix], helper


def _fix_sim_divergence(f: Finding) -> tuple[list[dict], list[str]]:
    """FEA/anchor divergence → BC review."""
    helper = ["run_fea_static", "describe_results"]
    fix = {
        "action": "review_bc_and_loads",
        "params": {"hint": "Verify fixture faces and load magnitudes match the physical setup."},
        "basis": "FEA result diverges from beam-anchor by > 5×; likely a BC error.",
        "predicted_effect": "Result converges with hand-calc estimate.",
        "confidence": "heuristic",
    }
    return [fix], helper


def _fix_wall_thickness(f: Finding) -> tuple[list[dict], list[str]]:
    """Thin wall → process-specific minimum wall advisory with resize hint.

    Emits the measured minimum, the process minimum, and a targeted
    param edit suggestion.  Numbers come from ``eval_thickness`` (kernel ray-cast)
    via the adapter — never invented.
    """
    helper = ["wall_thickness", "dfm_check", "describe_part"]
    mn = f.numbers.get("min_thickness_mm")
    limit = f.numbers.get("limit_mm")
    process = f.numbers.get("process", "the selected process")

    if mn is not None and limit is not None:
        delta = round(float(limit) - float(mn), 3)
        basis = (f"measured minimum wall {mn:.3f} mm < {process} minimum {limit:.3f} mm "
                 f"(Δ={delta:+.3f} mm) — from eval_thickness kernel ray-cast")
        fix_hint = (f"increase the thinnest wall by ≥{delta:.3f} mm "
                    f"(target ≥{limit:.3f} mm for {process})")
        pred = f"minimum wall ≥{limit:.3f} mm — {process} manufacturable"
        confidence = "table"  # process minimum from ProcessProfile table
    else:
        basis = f.finding
        fix_hint = "increase the thinnest wall to meet the process minimum"
        pred = "wall clears the process minimum"
        confidence = "heuristic"

    return [{
        "action": "thicken_wall",
        "params": {"hint": fix_hint, "min_thickness_mm": limit},
        "basis": basis,
        "predicted_effect": pred,
        "confidence": confidence,
    }], helper


def _fix_dfm(f: Finding) -> tuple[list[dict], list[str]]:
    """DFM check failure → per-issue targeted fix (wall or draft).

    DFM issues carry a ``checks`` list (each with ``check``, ``value``,
    ``limit``, ``note``).  We emit one fix per failed check so the fixer can
    apply them independently.  Numbers come from the DFM adapter — never invented.
    """
    helper = ["dfm_check", "wall_thickness", "draft_analysis", "describe_part"]
    checks = f.extra.get("checks") or []
    fixes: list[dict] = []

    for ck in checks:
        if ck.get("ok"):
            continue
        chk_name = ck.get("check", "unknown")
        note = ck.get("note", chk_name)
        if chk_name == "min_wall":
            val = ck.get("value_mm")
            lim = ck.get("limit_mm")
            delta = round(float(lim) - float(val), 3) if (val is not None and lim is not None) else None
            hint = (f"increase thinnest wall by ≥{delta:.3f} mm" if delta is not None
                    else "increase thinnest wall to process minimum")
            fixes.append({
                "action": "thicken_wall",
                "params": {"hint": hint, "target_mm": lim},
                "basis": note,
                "predicted_effect": "min_wall check passes",
                "confidence": "table",
            })
        elif chk_name == "draft":
            faces_below = ck.get("faces_below", "?")
            fixes.append({
                "action": "add_draft",
                "params": {"hint": f"{faces_below} face(s) need draft; add ≥1° per face"},
                "basis": note,
                "predicted_effect": "draft check passes",
                "confidence": "table",
            })
        else:
            fixes.append({
                "action": "fix_dfm_issue",
                "params": {"check": chk_name, "hint": note},
                "basis": note,
                "predicted_effect": f"{chk_name} check passes",
                "confidence": "heuristic",
            })

    if not fixes:
        # Fallback — §3 non-info invariant
        fixes = [{
            "action": "run_dfm_review",
            "params": {"hint": "re-run dfm_check after geometry changes"},
            "basis": f.finding,
            "predicted_effect": "DFM issues resolved",
            "confidence": "heuristic",
        }]
    return fixes, helper


def _fix_interface_fitment(f: Finding) -> tuple[list[dict], list[str]]:
    """Interface alignment failure → per-check targeted fix.

    interface_check findings carry a ``severity`` (fail/warn), a ``check``
    name (hole_pattern, coaxiality, contact_plane, fastener_passthrough), an
    ``error_mm`` or ``detail``, and a pre-computed ``fix`` string.
    We lift the pre-computed fix into a candidate_fix so the fixer loop can apply
    it directly without re-running the checker.
    """
    helper = ["interface_check", "measure_between", "fit_calc"]
    checks = f.extra.get("findings") or []
    fixes: list[dict] = []

    for ck in checks:
        if ck.get("severity") not in ("fail", "warn"):
            continue
        chk_name = ck.get("check", "interface")
        detail = ck.get("detail", chk_name)
        precomputed_fix = ck.get("fix", "")
        err_mm = ck.get("error_mm")
        params: dict = {"hint": precomputed_fix or detail}
        if err_mm is not None:
            params["error_mm"] = err_mm
        basis = detail
        if err_mm is not None:
            basis = f"{detail} (error = {err_mm:.3f} mm)"
        fixes.append({
            "action": f"fix_{chk_name}",
            "params": params,
            "basis": basis,
            "predicted_effect": f"{chk_name} passes",
            "confidence": "calc",  # pre-computed by interface_check geometry analysis
        })

    if not fixes:
        fixes = [{
            "action": "run_interface_check",
            "params": {"hint": "re-run interface_check after geometry changes"},
            "basis": f.finding,
            "predicted_effect": "interface alignment issues resolved",
            "confidence": "heuristic",
        }]
    return fixes, helper


def _fix_check_error(f: Finding) -> tuple[list[dict], list[str]]:
    """Keep an unknown/failed battery result visible and actionable."""
    detail = f.extra.get("error") or f.finding
    return [{
        "action": "inspect_advisor_check",
        "params": {"check": f.check, "error": detail},
        "basis": "advisor check did not produce a verdict",
        "predicted_effect": "check backend is restored and the verdict is rerun",
        "confidence": "harness",
    }], []


_FIX_PROVIDERS = {
    "interference": _fix_interference,
    "overlap": _fix_overlap,
    "open_profile": _fix_open_profile,
    "pinned_instead_of_constrained": _fix_sketch_quality,
    "no_driving_dimensions": _fix_sketch_quality,
    "unreferenced_profile": _fix_sketch_quality,
    "under_dimensioned": _fix_sketch_quality,
    "underconstrained": _fix_sketch_quality,
    "sketch_conflict": _fix_sketch_quality,
    "rebuild": _fix_rebuild,
    "sim_bc": _fix_sim_bc,
    "sim_margin": _fix_sim_margin,
    "sim_divergence": _fix_sim_divergence,
    # Diagnostic advisors
    "wall_thickness": _fix_wall_thickness,
    "dfm": _fix_dfm,
    "interface_fitment": _fix_interface_fitment,
    "check_error": _fix_check_error,
}


# ---------------------------------------------------------------------------
# Engine core
# ---------------------------------------------------------------------------

def _compute_fixes(f: Finding) -> tuple[list[dict], list[str], dict | None]:
    """Return fixes, helpers, and an explicit provider-fault record when needed."""
    provider = _FIX_PROVIDERS.get(f.kind)
    if provider is None:
        return [], [], None
    try:
        fixes, helpers = provider(f)
        return fixes, helpers, None
    except Exception as exc:  # noqa: BLE001
        error = str(exc).strip() or exc.__class__.__name__
        harness_error = {
            "component": "advisor_fix_provider",
            "finding_kind": f.kind,
            "exception": exc.__class__.__name__,
            "error": error,
        }
        fallback = [{
            "action": "harness_error",
            "params": harness_error,
            "basis": (
                "candidate-fix computation failed; original evidence severity "
                "is preserved"
            ),
            "predicted_effect": "harness fault is visible for repair",
            "confidence": "harness",
        }]
        return fallback, [], harness_error


def _finding_to_item(seq: int, stage: str, f: Finding) -> dict:
    fixes, extra_helpers, harness_error = _compute_fixes(f)

    # Engine invariant (§3): non-info items MUST carry ≥1 candidate fix. Unknown
    # kinds still downgrade, but a provider fault gets a harness-error fallback
    # and therefore never erases the evidence severity.
    severity = f.severity
    if severity != "info" and not fixes:
        severity = "info"

    helpers = list(dict.fromkeys(stages.helper_tools(stage) + extra_helpers))

    item = {
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
    if harness_error is not None:
        item["harness_error"] = harness_error
    return item


def build_advisory(obs: Observation) -> dict:
    """Convert a normalized :class:`Observation` into an aicad.advisory/1 block.

    Returns ``{schema, seq, stage, items, user_override, failure_cards}``.
    ``items`` is the materialized list (renderer applies the token budget + drop
    policy).  Items are sorted highest-severity-first so the renderer drops from
    the tail.

    ``failure_cards``: blocker-severity items are promoted to
    ``aicad.failure/1`` cards via :func:`failures.promote_advisory_blockers`.
    Cards are returned in the advisory block and are also available via
    :func:`build_advisory` for harness dispatch.  Non-blocker advisories are
    NOT promoted — they remain advisory items only.
    """
    items = [_finding_to_item(obs.seq, obs.stage, f) for f in obs.findings]

    # §9.2: on user-override conflict, advisories downgrade to info (the renderer
    # also prepends the "user override active" banner).
    if obs.user_override:
        for it in items:
            it["severity"] = "info"

    items.sort(key=lambda it: -SEVERITY_ORDER.get(it["severity"], 0))

    # §20: append trigger-based context items (severity=info, tail position
    # so the global drop-first policy drops them before findings).
    trigger_events: list[dict] = []
    if obs.output_text or obs.tool_names or obs.events:
        try:
            from .triggers import match_triggers  # noqa: PLC0415
            ctx_items, trigger_events = match_triggers(
                output_text=obs.output_text,
                tool_names=obs.tool_names,
                events=obs.events,
                seq=obs.seq,
                cooldown_state=obs.cooldown_state,
            )
            items.extend(ctx_items)
        except Exception:  # noqa: BLE001
            pass  # trigger layer never breaks the advisory pass

    advisory = {
        "schema": SCHEMA,
        "seq": obs.seq,
        "stage": obs.stage,
        "items": items,
        "user_override": obs.user_override,
        # §20: every firing is logged — the turn loop writes these to the run
        # ledger and streams one agent.context_injected event per rule.
        "trigger_events": trigger_events,
    }

    # Promote blocker advisories to failure cards (harness-owned).
    # Import locally to avoid circular import (failures.py imports nothing from engine.py).
    try:
        from .failures import promote_advisory_blockers
        advisory["failure_cards"] = promote_advisory_blockers(
            advisory, stage=obs.stage
        )
    except Exception:  # noqa: BLE001
        advisory["failure_cards"] = []

    return advisory


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
