"""User change-request routing (`change_request`) — Pillar 20 §11 / §17.

Contract: ``contracts/advisor.md`` §11 (change-request trigger: every user-issued
fix/change first produces an audit-scoped change plan — affected requirements/
dimensions/features/parts listed with ledger refs — BEFORE any mutation is
dispatched) and §17 (scope→approver routing).  Role prompt: overseer.yaml
"CHANGE REQUESTS".

Scope boundary: this module implements ONLY the **user change-request
path** — user asks for a fix/change → map onto ledger refs → scoped change plan →
audit → dispatch recommendation.  Role-emitted ``replan_request`` routing (§17,
``aicad.replan/1``) is handled separately by ``services/agent/pipeline/replan.py``.

The mapping is the whole game (role card rule): a change routed to the wrong scope
becomes two bugs.  So the affected-set is derived **deterministically from the
ledger** (requirements / dimensions / features / parts, each with its ledger ref)
rather than guessed — the LLM audits the plan, it does not invent the scope.

Design: deterministic ``map_change_request`` builds the affected-set + change plan;
:class:`~services.agent.advisor.overseer.Overseer` then self-audits the plan
(``trigger="change_request"``); the result carries a dispatch recommendation the
orchestrator acts on (the overseer never dispatches — freedom rule).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

CHANGE_PLAN_SCHEMA = "aicad.change_plan/1"

# ---------------------------------------------------------------------------
# Affected-set extraction (deterministic, ledger-driven)
# ---------------------------------------------------------------------------


@dataclass
class AffectedSet:
    """The ledger entries a change touches, each with its ref.  The scope map."""
    requirements: list[dict] = field(default_factory=list)   # [{req_id, text}]
    dimensions: list[dict] = field(default_factory=list)     # [{id, name, value, unit}]
    features: list[dict] = field(default_factory=list)       # [{feature, via}] (from dim.refs)
    parts: list[dict] = field(default_factory=list)          # [{part, via}]
    deliverables: list[dict] = field(default_factory=list)   # [{id, name}] (manifest artifacts)
    acceptance_criteria: list[dict] = field(default_factory=list)  # [{dl, ac, text}]

    def is_empty(self) -> bool:
        return not any((self.requirements, self.dimensions, self.features,
                        self.parts, self.deliverables, self.acceptance_criteria))


# Words in a request that name a physical feature we can match against dimensions.
# Deliberately small + explicit — this is a scope MAP, so precision beats recall;
# an over-broad match routes the change to the wrong place (two bugs).
_FEATURE_SYNONYMS = {
    "bore": ("bore", "hole", "id", "inner diameter", "inside diameter"),
    "shaft": ("shaft", "pin", "spindle", "od", "outer diameter"),
    "wall": ("wall", "thickness"),
    "fillet": ("fillet", "round"),
    "chamfer": ("chamfer",),
    "pocket": ("pocket", "cavity"),
    "boss": ("boss",),
    "slot": ("slot",),
    "thread": ("thread", "tapped"),
    "flange": ("flange",),
}

_DIM_MM = re.compile(r"(\d+(?:\.\d+)?)\s*mm\b", re.IGNORECASE)


def _canon_group_of(name: str) -> str | None:
    """The canonical feature group a dimension name belongs to (bore/shaft/...)."""
    nl = (name or "").lower()
    for canon, syns in _FEATURE_SYNONYMS.items():
        if any(s in nl for s in (canon, *syns)):
            return canon
    return None


def _text_names(text: str, name: str) -> bool:
    """True if *name* (a dimension/requirement name) is referenced by *text*."""
    if not name:
        return False
    nl = name.lower()
    tl = text.lower()
    if nl in tl:
        return True
    # Synonym match: e.g. request says "bore", dimension name contains "hole".
    for canon, syns in _FEATURE_SYNONYMS.items():
        if any(s in tl for s in syns) and any(s in nl for s in (canon, *syns)):
            return True
    return False


def map_affected_set(text: str, ledger_state: dict) -> AffectedSet:
    """Map a change request onto the exact ledger entries it touches.

    Deterministic: walks the ledger state (dimensions, requirements, artifacts) and
    keeps only entries whose name/text the request references.  A dimension pulls in
    the features/parts it ``refs`` (that is how a bore-diameter change reaches the
    F7 feature).  Precision-first — no fuzzy over-matching.
    """
    aff = AffectedSet()
    tl = text.lower()

    # Which canonical feature words does the request mention?
    mentioned = {canon for canon, syns in _FEATURE_SYNONYMS.items()
                 if any(s in tl for s in syns)}

    for dim in ledger_state.get("dimensions", []):
        name = dim.get("name", "")
        if _text_names(text, name):
            aff.dimensions.append({
                "id": dim.get("id"), "name": name,
                "value": dim.get("value"), "unit": dim.get("unit"),
                "tolerance": dim.get("tolerance") or {},
            })
            refs = dim.get("refs") or {}
            for feat in refs.get("features", []) or []:
                aff.features.append({"feature": feat, "via": dim.get("id")})
            for part in refs.get("parts", []) or []:
                aff.parts.append({"part": part, "via": dim.get("id")})

    for req in ledger_state.get("requirements", []):
        if _text_names(text, req.get("text", "")) or any(m in req.get("text", "").lower() for m in mentioned):
            aff.requirements.append({"req_id": req.get("req_id"), "text": req.get("text")})

    # Deliverables / acceptance criteria live as manifest artifacts in the ledger.
    for art in ledger_state.get("artifacts", []):
        if art.get("kind") != "manifest":
            continue
        manifest = art.get("manifest") or art.get("payload") or {}
        for dl in manifest.get("deliverables", []) or []:
            dl_text = f"{dl.get('name','')} {dl.get('description','')}".lower()
            if any(m in dl_text for m in mentioned) or _text_names(text, dl.get("name", "")):
                aff.deliverables.append({"id": dl.get("id"), "name": dl.get("name")})
                for ac in dl.get("acceptance_criteria", []) or []:
                    ac_text = ac.get("text", "")
                    if any(m in ac_text.lower() for m in mentioned):
                        aff.acceptance_criteria.append({
                            "dl": dl.get("id"), "ac": ac.get("id"), "text": ac_text,
                        })

    # De-dup features/parts (a feature reached via two dims should appear once).
    aff.features = _dedup(aff.features, key=lambda f: f["feature"])
    aff.parts = _dedup(aff.parts, key=lambda p: p["part"])
    return aff


def _dedup(items: list[dict], key) -> list[dict]:
    seen: set = set()
    out = []
    for it in items:
        k = key(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# Change-plan drafting (ordered edits + only-the-affected checks)
# ---------------------------------------------------------------------------

# Which re-checks a touched feature-kind implies.  ONLY the affected ones — a bore
# change re-runs its fit + interference checks, not the whole battery (role card).
_CHECK_RULES = {
    "bore": ["fit_report", "check_interference"],
    "shaft": ["fit_report", "check_interference"],
    "wall": ["wall_thickness"],
    "fillet": ["check_feature_overlap"],
    "chamfer": ["check_feature_overlap"],
    "pocket": ["check_feature_overlap"],
    "boss": ["check_feature_overlap"],
    "thread": ["fit_report"],
}

# Which role owns a given kind of edit (dispatch recommendation).
_DISPATCH_RULES = {
    "dimension": "feature_modeler",   # a dimension change is a feature edit
    "fit": "assembly_fit_engineer",
}


@dataclass
class ChangePlan:
    """A scoped, auditable change plan — the thing dispatched after audit passes."""
    id: str
    request_verbatim: str
    affected: AffectedSet
    ordered_edits: list[dict]
    checks_to_rerun: list[str]
    manifest_updates: list[dict]     # criteria that change ⇒ needs user confirm (flagged)
    scope: str                       # approach | spec | requirement (§17 routing)
    dispatch: list[dict]             # [{role, edit_refs}]  — recommendation, not an action
    needs_user_confirm: bool

    def to_dict(self) -> dict:
        return {
            "schema": CHANGE_PLAN_SCHEMA,
            "id": self.id,
            "request_verbatim": self.request_verbatim,
            "affected": {
                "requirements": self.affected.requirements,
                "dimensions": self.affected.dimensions,
                "features": self.affected.features,
                "parts": self.affected.parts,
                "deliverables": self.affected.deliverables,
                "acceptance_criteria": self.affected.acceptance_criteria,
            },
            "ordered_edits": self.ordered_edits,
            "checks_to_rerun": self.checks_to_rerun,
            "manifest_updates": self.manifest_updates,
            "scope": self.scope,
            "dispatch": self.dispatch,
            "needs_user_confirm": self.needs_user_confirm,
        }


def _extract_target_value(text: str) -> tuple[float | None, str]:
    """Pull a target dimension out of the request, e.g. '25mm shaft' → (25.0,'mm')."""
    m = _DIM_MM.search(text)
    if m:
        return float(m.group(1)), "mm"
    return None, ""


def _value_bound_feature(text: str) -> str | None:
    """Which canonical feature the numeric target attaches to, by text proximity.

    "make the bore fit a 25mm shaft" → the 25mm binds to *shaft* (nearest feature
    word to the number), NOT the bore.  This is what stops the resize value landing
    on the wrong dimension — the single most important precision rule here.
    """
    m = _DIM_MM.search(text)
    if not m:
        return None
    num_pos = m.start()
    best, best_dist = None, None
    tl = text.lower()
    for canon, syns in _FEATURE_SYNONYMS.items():
        for s in (canon, *syns):
            idx = tl.find(s)
            while idx != -1:
                dist = abs(idx - num_pos)
                if best_dist is None or dist < best_dist:
                    best, best_dist = canon, dist
                idx = tl.find(s, idx + 1)
    return best


def draft_change_plan(text: str, ledger_state: dict) -> ChangePlan:
    """Build a scoped change plan from the request + the affected ledger set.

    Ordered edits target the affected dimensions/features (with their refs); checks
    are ONLY those the touched kinds imply; manifest criteria changes are flagged
    for user confirmation.  Scope (§17) is derived: an acceptance-criterion change ⇒
    ``spec`` (overseer + user); a requirement change ⇒ ``requirement`` (user); else
    ``approach`` (orchestrator).
    """
    aff = map_affected_set(text, ledger_state)
    tl = text.lower()
    mentioned = {canon for canon, syns in _FEATURE_SYNONYMS.items()
                 if any(s in tl for s in syns)}
    target_value, target_unit = _extract_target_value(text)
    value_feature = _value_bound_feature(text)  # which feature the number attaches to

    # --- ordered edits: dimension first (it drives the geometry), then dependent
    #     recalcs.  Each edit cites its ledger ref so dispatch is unambiguous.
    ordered_edits: list[dict] = []
    checks: list[str] = []

    for dim in aff.dimensions:
        # The dimension the request re-sizes gets an explicit new-value edit; a
        # dependent dimension (e.g. the bore that must fit the resized shaft) gets a
        # recalc edit rather than a raw value.  The explicit value lands ONLY on the
        # dimension the number is bound to by proximity ("25mm shaft" → shaft), so a
        # co-mentioned bore is recalced, never given the shaft's number.
        dim_group = _canon_group_of(dim.get("name", ""))
        directly_named = (target_value is not None and value_feature is not None
                          and dim_group == value_feature)
        edit = {
            "op": "edit_dimension",
            "target": {"dim_id": dim["id"], "name": dim["name"]},
            "ref": dim["id"],
        }
        if directly_named:
            edit["new_value"] = target_value
            edit["unit"] = target_unit or dim.get("unit")
            edit["basis"] = "user-specified target"
        else:
            # A fit-driven recalc: the bore is recomputed from the (new) shaft + fit.
            edit["op"] = "recalc_from_fit"
            edit["basis"] = "recompute from the resized mating dimension + its fit class"
            edit["helper"] = "fit_report"
        ordered_edits.append(edit)

    # Add the feature edits (each dimension edit lands on a feature via refs).
    for feat in aff.features:
        for kind in mentioned:
            checks.extend(_CHECK_RULES.get(kind, []))

    # checks are de-duped, order-stable.
    checks_to_rerun = list(dict.fromkeys(checks))

    # --- manifest updates: any touched acceptance criterion changes ⇒ user confirm.
    manifest_updates = [{
        "deliverable": ac["dl"], "criterion": ac["ac"],
        "was": ac["text"], "needs_user_confirm": True,
    } for ac in aff.acceptance_criteria]

    # --- scope (§17).
    if aff.acceptance_criteria:
        scope = "spec"
    elif aff.requirements and not aff.dimensions:
        scope = "requirement"
    else:
        scope = "approach"
    needs_confirm = bool(manifest_updates) or scope in ("spec", "requirement")

    # --- dispatch recommendation (overseer recommends; orchestrator dispatches).
    dispatch: list[dict] = []
    if aff.dimensions or aff.features:
        dispatch.append({
            "role": _DISPATCH_RULES["dimension"],
            "edit_refs": [e["ref"] for e in ordered_edits],
        })
    if aff.parts or "fit_report" in checks_to_rerun:
        dispatch.append({
            "role": _DISPATCH_RULES["fit"],
            "edit_refs": [p["part"] for p in aff.parts] or ["fit-recheck"],
        })

    return ChangePlan(
        id=f"chg_{uuid.uuid4().hex[:8]}",
        request_verbatim=text,
        affected=aff,
        ordered_edits=ordered_edits,
        checks_to_rerun=checks_to_rerun,
        manifest_updates=manifest_updates,
        scope=scope,
        dispatch=dispatch,
        needs_user_confirm=needs_confirm,
    )


# ---------------------------------------------------------------------------
# Full routing: draft → audit → recommendation
# ---------------------------------------------------------------------------

@dataclass
class ChangeRequestResult:
    plan: dict                         # ChangePlan.to_dict()
    audit: dict                        # aicad.audit/1 (or {} if no provider)
    advisory: dict                     # verdicts as review findings
    dispatch: list[dict]               # who to route to (empty if user-confirm gates it)
    needs_user_confirm: bool
    user_summary: str                  # plain-language, written to ledger questions/decisions
    empty_scope: bool = False          # request matched nothing in the ledger


def route_change_request(
    text: str,
    ledger_state: dict,
    *,
    overseer: Any | None = None,
    ledger: Any | None = None,
    ledger_text: str = "",
    buildgraph: dict | None = None,
    run_id: str = "",
    stage: str = "fix",
) -> ChangeRequestResult:
    """Full change-request flow: map → draft plan → self-audit → recommendation.

    ``overseer`` is a :class:`~services.agent.advisor.overseer.Overseer`; when
    ``None`` (no provider configured) the plan is still produced and returned
    un-audited — the mapping is deterministic and useful on its own.  Nothing is
    dispatched here: the overseer never dispatches (freedom rule); the returned
    ``dispatch`` list is a recommendation for the orchestrator.  The plan + a
    plain-language summary are written to the ledger (questions/decisions §11).

    ``ledger_text`` is the caller's ``ledger_render(ledger, budget)`` — passed into
    the self-audit so the overseer can judge the map against the ACTUAL ledger (a
    mis-scoped map — a missed dimension — is only catchable if the audit sees the
    ledger, not just the plan).  When the caller has a ``ledger`` but no rendered
    text, we render a compact view here as a fallback.
    """
    plan_obj = draft_change_plan(text, ledger_state)
    # Add the DESIGN-STAGE re-entry (which DP stage/feature the change
    # re-derives from) onto each dispatch row, on top of the existing execution-role
    # mapping.  A "thicker wall" then re-enters GEOMETRY on the wall's geometry_spec
    # rather than being blindly re-modeled (doc §3.1 user-change → stage mapping).
    from .reroute import route_change
    route_change(plan_obj.affected, plan_obj.dispatch)
    plan = plan_obj.to_dict()
    empty = plan_obj.affected.is_empty()

    audit: dict = {}
    advisory: dict = {"schema": "aicad.advisory/1", "seq": 0, "stage": stage,
                      "items": [], "user_override": False}

    if overseer is not None and not empty:
        from .overseer import AuditScope  # local import: avoid cycle at module load
        # The self-audit must see the ledger to catch a mis-scoped map (a missed
        # dimension) — the plan alone cannot prove completeness against the ledger.
        lt = ledger_text
        if not lt and ledger is not None:
            try:
                from services.agent.ledger import ledger_render
                lt = ledger_render(ledger, 2000)
            except Exception:  # noqa: BLE001
                lt = ""
        scope = AuditScope(
            run_id=run_id, stage=stage,
            roles=[d["role"] for d in plan_obj.dispatch],
            buildgraph_nodes=[],
        )
        res = overseer.audit("change_request", scope, ledger_text=lt,
                             buildgraph=buildgraph, change_plan=plan)
        audit = res.audit
        advisory = res.advisory

    # A plan the audit found a blocker in should NOT auto-dispatch — surface it.
    audit_blocked = any(v.get("severity") == "blocker" for v in audit.get("verdicts", []))
    dispatch = [] if (plan_obj.needs_user_confirm or audit_blocked or empty) else plan_obj.dispatch

    user_summary = _summarize_for_user(plan_obj, audit_blocked, empty)

    # Persist to the ledger (§11 user-visible summary → questions/decisions).
    if ledger is not None:
        try:
            if plan_obj.needs_user_confirm or audit_blocked:
                ledger.add_question("overseer", f"q_chg_{plan_obj.id[4:]}",
                                    user_summary, blocking=empty is False and audit_blocked)
            else:
                ledger.add_decision("overseer", f"dec_chg_{plan_obj.id[4:]}",
                                    stage, f"Change request scoped: {text}", user_summary)
            # The full change plan lives as an artifact for provenance.
            ledger.append("overseer", "change_plan", plan)
        except Exception:  # noqa: BLE001
            pass

    return ChangeRequestResult(
        plan=plan, audit=audit, advisory=advisory,
        dispatch=dispatch, needs_user_confirm=plan_obj.needs_user_confirm,
        user_summary=user_summary, empty_scope=empty,
    )


def _summarize_for_user(plan: ChangePlan, audit_blocked: bool, empty: bool) -> str:
    """Plain-language summary of what the change touches (the user-visible §11 text)."""
    if empty:
        return (f'Your request "{plan.request_verbatim}" did not match any dimension, '
                "requirement, or deliverable in this mission's ledger. I need you to point "
                "me at what to change (which feature or number).")
    dims = ", ".join(f"{d['name']} ({d['id']})" for d in plan.affected.dimensions) or "—"
    feats = ", ".join(f["feature"] for f in plan.affected.features) or "—"
    checks = ", ".join(plan.checks_to_rerun) or "—"
    lines = [
        f'Change: "{plan.request_verbatim}"',
        f"This touches dimensions: {dims}",
        f"Features affected: {feats}",
        f"I will re-run only these checks: {checks}",
    ]
    if plan.manifest_updates:
        lines.append("This changes acceptance criteria — I need you to confirm before I proceed.")
    if audit_blocked:
        lines.append("The overseer flagged a problem with this plan — see the review findings.")
    if plan.scope == "requirement":
        lines.append("This changes a requirement, so it needs your sign-off.")
    return "\n".join(lines)
