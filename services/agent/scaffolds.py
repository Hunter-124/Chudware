"""scaffolds.py — capability dial scaffolding (WI-2/3/4/6-extras/7/8/10).

HARD floors live in guardrails.py. This module implements A/B scaffolding
that MUST no-op for capability letter C (frontier-freedom rule WI-18), except
where a dial reuses HARD machinery already required for all tiers.

Hard scaffold fires emit ``capability_dial`` with ``forced: true``; auditable
soft advisories use ``forced: false``. WI-2 read injection additionally emits
``auto_verify_inject``.

Callers pass a resolved CapabilityProfile (or letter / tier string). C profiles
return no-ops / None everywhere so ablation sees zero forced scaffold events.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CALC_TOOLS: frozenset[str] = frozenset({
    "fit_calc", "research_eng_calc", "list_calcs", "tap_drill",
    "recommend_fit", "wall_thickness", "research_unit_convert",
})

# Feature-group boundary stages (sketch → solid feature transitions).
_FEATURE_STAGE_MARKERS: frozenset[str] = frozenset({
    "feature", "sketch",
})

# Ops that count as dimensioned feature construction (WI-7).
_DIMENSIONED_OP_KINDS: frozenset[str] = frozenset({
    "extrude", "revolve", "loft", "sweep", "hole", "counterbore", "countersink",
    "bolt_circle", "fillet", "chamfer", "shell", "rib", "draft",
    "linear_pattern", "circular_pattern", "pattern", "mirror",
    "pad", "pocket", "cut", "boss",
})

# Aggressive auto-checkpoint op kinds (WI-6 A extras beyond HARD destructive).
AGGRESSIVE_CHECKPOINT_OPS: frozenset[str] = frozenset({
    "fillet", "chamfer",
    "pattern", "linear_pattern", "circular_pattern", "pattern_linear",
    "pattern_circular", "mirror",
    "boolean", "boolean_cut", "cut", "subtract", "cut_extrude", "cut_revolve",
    "extrude_cut", "revolve_cut",
    "hole", "counterbore", "countersink", "bolt_circle",
})

# Phase name aliases → capability.phase_caps keys.
_PHASE_ALIASES: dict[str, str] = {
    "intake": "intake",
    "plan": "plan",
    "planning": "plan",
    "specify": "specify",
    "spec": "specify",
    "decompose": "specify",
    "geometry": "specify",
    "linkage": "specify",
    "buildpath": "build",
    "build": "build",
    "feature": "build",
    "sketch": "build",
    "layout": "build",
    "assembly": "build",
    "cam": "build",
    "sim": "build",
    "verify": "verify",
    "review": "verify",
    "deliver": "verify",
    "fix": "build",
}

# Plan template markers (must never appear in C freeform prompts).
RIGID_PLAN_MARKER = "[PLAN_TEMPLATE_RIGID]"
STRUCTURED_PLAN_MARKER = "[PLAN_TEMPLATE_STRUCTURED]"
REFERENCE_BUILD_MARKER = "[REFERENCE_BUILD]"

MSG_CALC_REQUIRED = (
    "Capability A calc gate: dimensioned feature ops require a prior successful "
    "calc tool (fit_calc / research_eng_calc / tap_drill / …) in this turn/node. "
    "Run a calc tool first, then re-issue the feature call."
)

# Minimal reference-build exemplars for WI-8 injection (no network).
_REFERENCE_BUILDS: list[dict[str, str]] = [
    {
        "id": "flanged_bushing",
        "task_class": "simple_part",
        "summary": (
            "Flanged bushing: revolve OD/ID profile → flange plate extrude → "
            "bolt circle 4×M clearance on flange → fillet flange OD last."
        ),
    },
    {
        "id": "l_bracket",
        "task_class": "multi_feature_part",
        "summary": (
            "L-bracket: two plates at 90° → gusset optional → clearance holes "
            "per leg → fillets on outer vertical edges last."
        ),
    },
    {
        "id": "bearing_pedestal",
        "task_class": "multi_feature_part",
        "summary": (
            "Bearing pedestal: base plate + upright → bearing bore sized via "
            "fit_calc to OD → bolt pattern on base → fillet last."
        ),
    },
    {
        "id": "two_part_assembly",
        "task_class": "assembly",
        "summary": (
            "Two-part mate: place components → concentric+coincident mates → "
            "check_interference clean → interface_check on hole pattern."
        ),
    },
    {
        "id": "enclosure_shell",
        "task_class": "enclosure",
        "summary": (
            "Enclosure: outer box minus inner cavity → bosses/standoffs → "
            "vent slots → wall_thickness check → fillets last."
        ),
    },
]


# ---------------------------------------------------------------------------
# Profile resolution helpers
# ---------------------------------------------------------------------------

def coerce_profile(capability: Any = None, *,
                   tier: str | None = None,
                   model_id: str | None = None,
                   letter: str | None = None) -> Any:
    """Return a CapabilityProfile from any of the accepted call shapes.

    Prefer an already-resolved profile; else letter; else tier+model.
    Defaults to C (freedom) when nothing is supplied — never invents A scaffolding.
    """
    if capability is not None and hasattr(capability, "letter"):
        return capability
    try:
        from services.agent.capability import (  # noqa: PLC0415
            profile_for_letter, resolve_capability, DEFAULT_LETTER,
        )
    except Exception:  # noqa: BLE001
        return None
    if letter:
        try:
            return profile_for_letter(str(letter).upper())
        except Exception:  # noqa: BLE001
            return profile_for_letter(DEFAULT_LETTER)
    if tier is not None or model_id is not None:
        try:
            return resolve_capability(tier or "", model_id=model_id)
        except Exception:  # noqa: BLE001
            return profile_for_letter(DEFAULT_LETTER)
    return profile_for_letter(DEFAULT_LETTER)


def _letter(profile: Any) -> str:
    if profile is None:
        return "C"
    return str(getattr(profile, "letter", "C") or "C").upper()


def is_frontier(profile: Any) -> bool:
    """True when scaffolds must no-op (letter C)."""
    return _letter(profile) == "C"


# ---------------------------------------------------------------------------
# Ledger instrumentation (WI-17)
# ---------------------------------------------------------------------------

def _emit(ledger: Any, actor: str, kind: str, payload: dict) -> None:
    if ledger is None:
        return
    try:
        ledger.append(actor or "scaffolds", kind, payload)
    except Exception:  # noqa: BLE001
        pass


def emit_capability_dial(ledger: Any, dial: str, value: Any, *,
                         forced: bool = True,
                         actor: str = "scaffolds",
                         extra: dict | None = None) -> None:
    """WI-17: capability_dial {dial, value, forced}."""
    payload: dict[str, Any] = {
        "dial": dial,
        "value": value,
        "forced": bool(forced),
    }
    if extra:
        payload.update(extra)
    _emit(ledger, actor, "capability_dial", payload)


def emit_auto_verify_inject(ledger: Any, tool: str, *,
                            reason: str = "",
                            actor: str = "scaffolds",
                            extra: dict | None = None) -> None:
    """WI-17: auto_verify_inject after a WI-2 read injection."""
    payload: dict[str, Any] = {"tool": tool, "reason": reason}
    if extra:
        payload.update(extra)
    _emit(ledger, actor, "auto_verify_inject", payload)


# ---------------------------------------------------------------------------
# WI-2 — Verification cadence auto-injection
# ---------------------------------------------------------------------------

def stage_appropriate_verify_tool(tool: str, args: dict | None = None) -> str:
    """Pick describe_part / check_sketch / describe_assembly for the mutation."""
    args = args or {}
    try:
        from services.agent.advisor.stages import infer_stage_from_ops  # noqa: PLC0415
        from services.agent.guardrails import extract_ops  # noqa: PLC0415
        ops = extract_ops(args)
        stage = infer_stage_from_ops(tool, ops)
    except Exception:  # noqa: BLE001
        stage = None
    if stage == "sketch" or tool in ("sketch3d_add", "sketch3d_edit",
                                     "convert_face_to_sketch"):
        return "check_sketch"
    if tool in ("new_assembly", "asm_add_component", "asm_mate") or stage == "assembly":
        return "describe_assembly"
    return "describe_part"


def _ops_list(args: dict | None) -> list:
    if not isinstance(args, dict):
        return []
    ops = args.get("ops")
    return ops if isinstance(ops, list) else []


def feature_group_boundary(tool: str, args: dict | None,
                           prev_stage: str | None) -> tuple[bool, str | None]:
    """Detect sketch→feature (or feature-group) boundary for WI-2 B.

    Returns (is_boundary, new_stage). Boundary fires when the new stage is a
    solid feature after a sketch (or after a different feature-group marker).
    """
    try:
        from services.agent.advisor.stages import infer_stage_from_ops  # noqa: PLC0415
        stage = infer_stage_from_ops(tool, _ops_list(args))
    except Exception:  # noqa: BLE001
        stage = None
    if stage is None:
        # Bare mutating tools without stage map: treat add_features solids as feature.
        if tool in ("add_features", "doc_apply_ops"):
            stage = "feature"
        elif tool in ("new_part", "part_place"):
            stage = "feature"
        else:
            return False, prev_stage
    if prev_stage is None:
        return False, stage
    # Boundary: left sketch into feature, or left feature into a new feature after
    # a non-feature intervening stage, or feature after different feature chain end.
    if prev_stage == "sketch" and stage == "feature":
        return True, stage
    if prev_stage in _FEATURE_STAGE_MARKERS and stage == "feature" and prev_stage != stage:
        return True, stage
    # Also fire when a feature mutator follows another feature mutator of a
    # different family after an intermediate non-mutating read — handled by caller
    # only when stages differ across the feature/sketch set.
    if prev_stage == "feature" and stage == "sketch":
        # Starting a new sketch after features — not a "group complete" boundary
        # for inject-after; inject happens when group CLOSES (sketch→feature).
        return False, stage
    return False, stage


def should_auto_verify(profile: Any, tool: str, args: dict | None,
                       *, ok: bool,
                       prev_stage: str | None = None,
                       ) -> tuple[bool, str | None, str | None]:
    """Return (inject?, verify_tool, new_stage) for WI-2.

    C / soft_only → never. A per_mutation → every successful mutating tool.
    B per_feature_group → only at feature-group boundary.
    """
    if not ok or profile is None:
        return False, None, prev_stage
    try:
        from services.agent.guardrails import is_mutating_tool  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False, None, prev_stage
    if not is_mutating_tool(tool):
        return False, None, prev_stage

    cadence = str(getattr(profile, "verify_cadence", "soft_only") or "soft_only")
    is_boundary, new_stage = feature_group_boundary(tool, args, prev_stage)

    if cadence == "soft_only" or is_frontier(profile):
        return False, None, new_stage
    if cadence == "per_mutation":
        return True, stage_appropriate_verify_tool(tool, args), new_stage
    if cadence == "per_feature_group":
        if is_boundary:
            return True, stage_appropriate_verify_tool(tool, args), new_stage
        return False, None, new_stage
    return False, None, new_stage


def run_auto_verify(dispatch: Callable[[str, dict], Any],
                    verify_tool: str,
                    args: dict | None,
                    *,
                    ledger: Any = None,
                    actor: str = "scaffolds",
                    profile: Any = None,
                    reason: str = "verify_cadence") -> Any:
    """Synchronously run the verify tool and emit dial + inject events."""
    vargs: dict[str, Any] = {}
    if isinstance(args, dict) and args.get("doc_id"):
        vargs["doc_id"] = args["doc_id"]
    result = None
    try:
        result = dispatch(verify_tool, vargs)
    except Exception as exc:  # noqa: BLE001 — inject still records the failure
        result = {"ok": False, "error": str(exc)}
    cadence = getattr(profile, "verify_cadence", None) if profile else None
    emit_capability_dial(
        ledger, "verify_cadence", cadence or reason,
        forced=True, actor=actor,
        extra={"inject_tool": verify_tool, "reason": reason},
    )
    emit_auto_verify_inject(
        ledger, verify_tool, reason=reason, actor=actor,
        extra={"cadence": cadence},
    )
    return result


# ---------------------------------------------------------------------------
# WI-3 — Plan format
# ---------------------------------------------------------------------------

PLAN_FORMAT_TO_ENFORCEMENT = {
    "rigid_template": "step_locked",
    "structured_fields": "checklist",
    "freeform": "off",
}

# A rigid plan: feature-tree skeleton with required dim slots.
RIGID_PLAN_TEMPLATE = f"""\
{RIGID_PLAN_MARKER}
Produce a RIGID plan. Every slot is required; missing slots fail handoff validation.

Required top-level keys:
  name, summary, parts[], datums[], build_order[], risks[], dimensions{{}}

Each part MUST include:
  name, purpose, dimensions{{length,width,thickness (or OD/ID/height as applicable)}},
  features[] (each feature string MUST include a concrete numeric size)

dimensions{{}} MUST list every driving dim name → numeric value (mm).
datums[]: ≥1 named datum (plane/axis/point) with defined_by.
build_order[]: ordered feature-tree steps (strings).
risks[]: list (may be empty only if explicitly justified in summary).

Reply with ONE ```json block matching this skeleton exactly (fill numbers):\
"""

STRUCTURED_PLAN_TEMPLATE = f"""\
{STRUCTURED_PLAN_MARKER}
Produce a STRUCTURED plan with required fields:
  datums[] — reference geometry the build hangs on
  build_order[] — ordered feature sequence
  risks[] — known risks / open questions

Also include name, summary, parts[] with dimensions and features.
Reply with ONE ```json block.\
"""

# Freeform C: only ordered feature list + checkpoint names — no rigid skeleton.
FREEFORM_PLAN_NOTE = (
    "Produce a freeform BUILD PLAN: ordered feature list and checkpoint names. "
    "No rigid template required."
)

# Required slots for rigid handoff validation (WI-3 A).
RIGID_REQUIRED_SLOTS: tuple[str, ...] = (
    "name", "parts", "dimensions", "datums", "build_order", "risks",
)
STRUCTURED_REQUIRED_SLOTS: tuple[str, ...] = (
    "datums", "build_order", "risks",
)


def playbook_enforcement_for(profile: Any) -> str:
    """Map plan_format dial → existing playbook_enforcement enum."""
    if profile is None or is_frontier(profile):
        return "off"
    fmt = str(getattr(profile, "plan_format", "freeform") or "freeform")
    return PLAN_FORMAT_TO_ENFORCEMENT.get(fmt, "off")


def plan_prompt_for(profile: Any, base_prompt: str = "") -> str:
    """Return plan system prompt with capability template (or freeform for C).

    C: never inject rigid/structured template text (WI-18 / acceptance).
    """
    if profile is None or is_frontier(profile):
        # Freeform only — assert no template markers.
        body = base_prompt or FREEFORM_PLAN_NOTE
        assert RIGID_PLAN_MARKER not in body
        assert STRUCTURED_PLAN_MARKER not in body
        return body
    fmt = str(getattr(profile, "plan_format", "freeform") or "freeform")
    if fmt == "rigid_template":
        prefix = RIGID_PLAN_TEMPLATE
    elif fmt == "structured_fields":
        prefix = STRUCTURED_PLAN_TEMPLATE
    else:
        return base_prompt or FREEFORM_PLAN_NOTE
    if base_prompt:
        return prefix + "\n\n" + base_prompt
    return prefix


def plan_first_for(profile: Any) -> bool:
    """A forces plan_first; B/C leave caller default."""
    if profile is None:
        return False
    return str(getattr(profile, "plan_format", "")) == "rigid_template"


def validate_plan_slots(plan: dict | None, profile: Any) -> list[str]:
    """Validate plan/handoff slots for A/B. Empty list = ok. C always []."""
    if not plan or not isinstance(plan, dict):
        if profile is not None and not is_frontier(profile):
            fmt = str(getattr(profile, "plan_format", "freeform"))
            if fmt in ("rigid_template", "structured_fields"):
                return ["plan missing or not an object"]
        return []
    if profile is None or is_frontier(profile):
        return []
    fmt = str(getattr(profile, "plan_format", "freeform") or "freeform")
    errs: list[str] = []
    if fmt == "rigid_template":
        for slot in RIGID_REQUIRED_SLOTS:
            if slot not in plan or plan[slot] in (None, "", [], {}):
                errs.append(f"missing required plan slot {slot!r}")
        # Dimension slots: every part needs a dimensions map/string with content.
        parts = plan.get("parts") or []
        if isinstance(parts, list):
            for i, p in enumerate(parts):
                if not isinstance(p, dict):
                    errs.append(f"parts[{i}] not an object")
                    continue
                dims = p.get("dimensions")
                if dims in (None, "", {}, []):
                    errs.append(f"parts[{i}] missing dimensions slot")
        dims_top = plan.get("dimensions")
        if not isinstance(dims_top, dict) or not dims_top:
            # Allow dimensions embedded only in parts if top-level empty — still
            # require the key present (checked above); warn if empty dict.
            if dims_top == {}:
                errs.append("dimensions slot is empty")
    elif fmt == "structured_fields":
        for slot in STRUCTURED_REQUIRED_SLOTS:
            if slot not in plan or plan[slot] in (None, ""):
                errs.append(f"missing required plan field {slot!r}")
            elif slot in ("datums", "build_order", "risks") and not isinstance(
                    plan[slot], list):
                errs.append(f"plan field {slot!r} must be a list")
    return errs


def validate_plan_handoff(card: Any, envelope: dict, profile: Any) -> list[str]:
    """roles.validate_handoff + capability plan-slot checks for plan nodes.

    Fail-hard for A/B (return errors); C only structural freeform (no template).
    """
    errs: list[str] = []
    try:
        from services.agent.roles import validate_handoff  # noqa: PLC0415
        if card is not None:
            errs.extend(validate_handoff(card, envelope))
    except Exception as exc:  # noqa: BLE001
        errs.append(f"validate_handoff error: {exc}")
    # Pull plan-like payload from handoff outputs or top-level.
    plan = None
    if isinstance(envelope, dict):
        outs = envelope.get("outputs") or {}
        if isinstance(outs, dict):
            plan = outs.get("plan") or outs.get("build_plan") or outs
        if plan is None:
            plan = envelope.get("plan")
    slot_errs = validate_plan_slots(plan if isinstance(plan, dict) else None, profile)
    errs.extend(slot_errs)
    if slot_errs and profile is not None and not is_frontier(profile):
        # Caller should fail handoff; we only report.
        pass
    return errs


# ---------------------------------------------------------------------------
# WI-4 — Per-phase step caps
# ---------------------------------------------------------------------------

def resolve_phase_key(phase: str | None) -> str | None:
    if not phase:
        return None
    return _PHASE_ALIASES.get(str(phase).lower().strip())


def phase_cap(profile: Any, phase: str | None) -> Optional[int]:
    """HARD tool-call cap for *phase*, or None when soft-only (C)."""
    if profile is None or is_frontier(profile):
        return None
    caps = getattr(profile, "phase_caps", None)
    if not caps:
        return None
    key = resolve_phase_key(phase)
    if key is None:
        return None
    val = caps.get(key)
    return int(val) if val is not None else None


def apply_phase_cap(profile: Any, phase: str | None,
                    current_max: int | None,
                    *, ledger: Any = None,
                    actor: str = "scaffolds") -> tuple[int | None, bool]:
    """Return (effective_max_tool_calls, capped).

    When a HARD phase cap exists, take min(current, cap). Emits capability_dial
    when the cap is applied (forced). C never caps.
    """
    cap = phase_cap(profile, phase)
    if cap is None:
        return current_max, False
    if current_max is None:
        effective = cap
    else:
        effective = min(int(current_max), cap)
    emit_capability_dial(
        ledger, "phase_caps", cap, forced=True, actor=actor,
        extra={"phase": resolve_phase_key(phase) or phase,
               "effective": effective, "reason": "phase_step_cap"},
    )
    return effective, True


def phase_cap_stop_reason() -> str:
    return "phase_step_cap"


# ---------------------------------------------------------------------------
# WI-6 A-extras — aggressive auto-checkpoint scope
# ---------------------------------------------------------------------------

def ops_need_aggressive_checkpoint(tool: str, args: dict | None) -> bool:
    """True when tool/args include fillet/pattern/boolean/hole (A extras)."""
    if tool in ("delete_feature", "remove_feature"):
        return True
    if tool not in ("add_features", "doc_apply_ops"):
        return False
    for op in _ops_list(args):
        if not isinstance(op, dict):
            continue
        kind = (op.get("op") or op.get("kind") or "").lower()
        if kind in AGGRESSIVE_CHECKPOINT_OPS:
            return True
        feat = op.get("feature") if isinstance(op.get("feature"), dict) else {}
        ftype = (feat.get("type") or "").lower()
        if ftype in AGGRESSIVE_CHECKPOINT_OPS:
            return True
    return False


def pre_dispatch_aggressive_checkpoint(
    tool: str, args: dict | None, *,
    profile: Any,
    ledger: Any = None,
    actor: str = "scaffolds",
    cfg: dict | None = None,
    checkpoint_fn: Callable[..., Any] | None = None,
    vault_bound: bool = False,
) -> Optional[str]:
    """WI-6 A: checkpoint before fillet/pattern/boolean/hole.

    Reuses guardrails.checkpoint_verdict so HARD and aggressive scopes share
    the same per-document retry escape. Returns an error string to block, or None.
    No-op for B/C (destructive-only path stays in guardrails.pre_dispatch).
    """
    if profile is None or is_frontier(profile):
        return None
    mode = str(getattr(profile, "auto_checkpoint", "destructive_only") or "")
    if mode != "aggressive":
        return None
    if not ops_need_aggressive_checkpoint(tool, args):
        return None
    # If already covered by HARD destructive classifier, guardrails handles it.
    try:
        from services.agent import guardrails as _gr  # noqa: PLC0415
        if _gr.is_destructive_tool(tool, args):
            return None  # HARD path owns this call
        verdict = _gr.checkpoint_verdict(
            tool, args, ledger=ledger, actor=actor, cfg=cfg,
            checkpoint_fn=checkpoint_fn, vault_bound=vault_bound,
        )
        emit_capability_dial(
            ledger, "auto_checkpoint", "aggressive", forced=True, actor=actor,
            extra={"trigger_tool": tool, "blocked": verdict is not None},
        )
        return verdict.message if verdict is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# WI-7 — Math scratchpad / calc gate
# ---------------------------------------------------------------------------

def _looks_dimensioned(op: dict) -> bool:
    kind = (op.get("op") or op.get("kind") or "").lower()
    feat = op.get("feature") if isinstance(op.get("feature"), dict) else {}
    ftype = (feat.get("type") or "").lower()
    if kind in _DIMENSIONED_OP_KINDS or ftype in _DIMENSIONED_OP_KINDS:
        return True
    # Numeric params present on a construction feature.
    params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
    if not params and isinstance(op.get("params"), dict):
        params = op["params"]
    for k, v in (params or {}).items():
        if k in ("mode", "name", "unit"):
            continue
        if isinstance(v, (int, float)):
            return True
        if isinstance(v, dict) and isinstance(v.get("value"), (int, float)):
            return True
    return False


def is_dimensioned_feature_call(tool: str, args: dict | None) -> bool:
    """True for add_features/hole ops that carry concrete dimensions."""
    if tool in ("hole", "counterbore", "countersink", "bolt_circle"):
        return True
    if tool not in ("add_features", "doc_apply_ops"):
        return False
    ops = _ops_list(args)
    if not ops:
        return False
    return any(isinstance(op, dict) and _looks_dimensioned(op) for op in ops)


def turn_had_successful_calc(ledger: Any, calls: list[dict] | None = None) -> bool:
    """True if this turn/node already succeeded at a calc tool."""
    if calls:
        for c in calls:
            if c.get("ok") and str(c.get("tool") or "") in CALC_TOOLS:
                return True
    if ledger is None:
        return False
    try:
        from services.agent.guardrails import _tool_call_events  # noqa: PLC0415
        for p in _tool_call_events(ledger):
            tool = str(p.get("tool") or "")
            if tool in CALC_TOOLS and p.get("ok", True):
                return True
    except Exception:  # noqa: BLE001
        # Fallback: walk ledger.events directly.
        try:
            events = list(getattr(ledger, "events", []) or [])
            for ev in events:
                p = ev.get("payload") if isinstance(ev, dict) else None
                if not isinstance(p, dict):
                    continue
                if p.get("event") == "tool_call" and p.get("ok", True):
                    if str(p.get("tool") or "") in CALC_TOOLS:
                        return True
                if str(ev.get("kind") or "") in ("tool_call",) and p.get("ok", True):
                    if str(p.get("tool") or "") in CALC_TOOLS:
                        return True
        except Exception:  # noqa: BLE001
            pass
    return False


def pre_dispatch_calc_gate(
    tool: str, args: dict | None, *,
    profile: Any,
    ledger: Any = None,
    calls: list[dict] | None = None,
    actor: str = "scaffolds",
    effective_allow_set: set[str] | frozenset[str] | None = None,
) -> Optional[str]:
    """WI-7 A calc gate; unavailable calc remedies downgrade to an advisory."""
    if profile is None or is_frontier(profile):
        return None
    mode = str(getattr(profile, "math_scratchpad", "off") or "off")
    if mode != "hard":
        return None
    if not is_dimensioned_feature_call(tool, args):
        return None
    if turn_had_successful_calc(ledger, calls):
        return None
    if (effective_allow_set is not None
            and not CALC_TOOLS.intersection(effective_allow_set)):
        # The role cannot satisfy the hard gate from its effective tool list.
        # Downgrade to an auditable advisory instead of creating a permanent
        # wall. tool_categories.CATEGORY_PACKS["research"] contains
        # research_eng_calc, list_calcs, and research_unit_convert; record the
        # real expansion route for operators without blocking this call.
        emit_capability_dial(
            ledger, "math_scratchpad", "advisory", forced=False, actor=actor,
            extra={
                "tool": tool,
                "reason": "no_callable_calc",
                "remedy": (
                    "enable_tools(categories=['research']), then call a calc "
                    "tool, then retry"
                ),
            },
        )
        return None
    emit_capability_dial(
        ledger, "math_scratchpad", "hard", forced=True, actor=actor,
        extra={"tool": tool, "reason": "no_calc_before_dim"},
    )
    try:
        from services.agent import guardrails as _gr  # noqa: PLC0415
        _gr.emit_hard_guardrail(
            ledger, "math_scratchpad", "blocked",
            reason="no_calc_before_dim", actor=actor,
        )
    except Exception:  # noqa: BLE001
        pass
    return MSG_CALC_REQUIRED


# ---------------------------------------------------------------------------
# WI-8 — Example retrieval (reference builds)
# ---------------------------------------------------------------------------

def _nearest_reference_builds(request: str, task_class: str | None,
                              n: int = 2) -> list[dict[str, str]]:
    """Pick 1–2 nearest reference builds by task_class / keyword overlap."""
    scored: list[tuple[int, dict[str, str]]] = []
    req = (request or "").lower()
    tc = (task_class or "").lower()
    for ref in _REFERENCE_BUILDS:
        score = 0
        if tc and ref.get("task_class") == tc:
            score += 10
        for tok in re.findall(r"[a-z0-9_]+", ref["id"] + " " + ref["summary"].lower()):
            if len(tok) > 3 and tok in req:
                score += 1
        scored.append((score, ref))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    # Always return up to n (even low score) so A always injects something.
    return [r for _, r in scored[:n]]


def should_inject_reference_builds(profile: Any, *,
                                   task_class: str | None = None,
                                   phase: str | None = None,
                                   on_demand: bool = False) -> bool:
    """WI-8 gate: A always (specify), B novel task-class, C off unless on_demand."""
    if profile is None:
        return False
    mode = str(getattr(profile, "example_retrieval", "on_demand") or "on_demand")
    phase_key = resolve_phase_key(phase) or (phase or "").lower()
    # Prefer specify context; also allow plan/build when phase unknown.
    in_specify = phase_key in ("specify", "plan", "build", "") or phase is None
    if mode == "always":
        return in_specify and not is_frontier(profile)
    if mode == "novel":
        if is_frontier(profile):
            return False
        # Novel = unknown / multi_feature / assembly / enclosure (not simple_part).
        novel = (task_class or "unknown") in (
            "unknown", "multi_feature_part", "assembly", "enclosure",
            "cam_program", "analysis", "modification",
        )
        return novel and in_specify
    # on_demand (C default): only explicit flag
    return bool(on_demand)


def reference_build_context(profile: Any, request: str, *,
                            task_class: str | None = None,
                            phase: str | None = None,
                            on_demand: bool = False,
                            ledger: Any = None,
                            actor: str = "scaffolds") -> str:
    """Return inject text with [REFERENCE_BUILD] markers, or '' for C default."""
    if not should_inject_reference_builds(
            profile, task_class=task_class, phase=phase, on_demand=on_demand):
        return ""
    refs = _nearest_reference_builds(request, task_class, n=2)
    if not refs:
        return ""
    lines = [REFERENCE_BUILD_MARKER + " worked examples (nearest):"]
    for r in refs:
        lines.append(f"- {r['id']}: {r['summary']}")
    text = "\n".join(lines)
    mode = getattr(profile, "example_retrieval", None)
    emit_capability_dial(
        ledger, "example_retrieval", mode, forced=True, actor=actor,
        extra={"count": len(refs), "ids": [r["id"] for r in refs]},
    )
    return text


# ---------------------------------------------------------------------------
# WI-10 — Refine pass caps
# ---------------------------------------------------------------------------

def refine_pass_limit(profile: Any) -> Optional[int]:
    """Maximum post-settlement geometry reworks permitted.

    A: refine_passes=0 → no rework after the first settled build.
    B: refine_passes=1 → one rework after the first settled build.
    C: None (untouched — existing DEFAULT_MAX_ITERS / runaway kills).
    """
    if profile is None or is_frontier(profile):
        return None
    try:
        n = int(getattr(profile, "refine_passes", 2))
    except (TypeError, ValueError):
        return None
    # The first settled build is not a refine pass. The caller proposes the
    # next post-settlement rework number, so the configured value is the limit
    # directly rather than one plus the configured value.
    return max(0, n)


def should_stop_refine(profile: Any, proposed_refine_round_count: int,
                       *, ledger: Any = None,
                       actor: str = "scaffolds") -> bool:
    """True when a proposed post-settlement rework would exceed the A/B cap."""
    limit = refine_pass_limit(profile)
    if limit is None:
        return False
    if proposed_refine_round_count > limit:
        emit_capability_dial(
            ledger, "refine_passes",
            getattr(profile, "refine_passes", None),
            forced=True, actor=actor,
            extra={"proposed_refine_round_count": proposed_refine_round_count,
                   "limit": limit, "reason": "refine_cap"},
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Combined pre-dispatch (scaffolds only; HARD floors stay in guardrails)
# ---------------------------------------------------------------------------

def pre_dispatch_scaffolds(
    tool: str, args: dict | None, *,
    profile: Any,
    ledger: Any = None,
    calls: list[dict] | None = None,
    actor: str = "scaffolds",
    cfg: dict | None = None,
    checkpoint_fn: Callable[..., Any] | None = None,
    vault_bound: bool = False,
    effective_allow_set: set[str] | frozenset[str] | None = None,
) -> Optional[str]:
    """Run A/B scaffold pre-dispatch gates. First block wins. C → always None."""
    if profile is None or is_frontier(profile):
        return None
    msg = pre_dispatch_calc_gate(
        tool, args, profile=profile, ledger=ledger, calls=calls, actor=actor,
        effective_allow_set=effective_allow_set,
    )
    if msg:
        return msg
    msg = pre_dispatch_aggressive_checkpoint(
        tool, args, profile=profile, ledger=ledger, actor=actor, cfg=cfg,
        checkpoint_fn=checkpoint_fn, vault_bound=vault_bound,
    )
    if msg:
        return msg
    return None


# ---------------------------------------------------------------------------
# C-freedom snapshot helper (tests)
# ---------------------------------------------------------------------------

def forced_scaffold_events(ledger: Any) -> list[dict]:
    """Return capability_dial events with forced=true (scaffold fires)."""
    if ledger is None:
        return []
    out: list[dict] = []
    try:
        events = list(getattr(ledger, "events", []) or [])
    except Exception:  # noqa: BLE001
        return []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind")
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        if kind == "capability_dial" and p.get("forced"):
            out.append(ev)
        if kind == "auto_verify_inject":
            out.append(ev)
    return out
