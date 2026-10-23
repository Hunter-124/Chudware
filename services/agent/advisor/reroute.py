"""Overseer reroute routing — a targeted correction to the OWNING design-pipeline
stage.

Contract: ``docs/design/AGENT_DESIGN_PIPELINE.md`` §3.1 (overseer audit + error correction:
"route a *targeted correction back to the specific stage that owns it* — never a
blind full retry") and ``contracts/design_pipeline.md`` §2/§4 (the frozen DP-stage ↔
ledger-kind ↔ role table).  Additive amendment to ``contracts/advisor.md`` §11: the
audit verdict now carries an optional ``targeted_reentry`` object (documented there;
minor bump, additive field — never mutate the frozen shape).

What this module is
-------------------
The **deterministic routing core**.  It computes *where a correction re-enters* from
the audit EVIDENCE, not from the model's prose (the same discipline change_request.py
states: "the LLM audits the plan, it does not invent the scope").  Both the audit
reroute (overseer) and the user-change mapping (change_request) consume this one map,
so a geometry error and a "make the wall thicker" request route through identical
logic.

The routing rule (the whole game)
---------------------------------
The owning stage is **upstream of where the error was detected** — that is what makes
this a *targeted* correction rather than a blind restart.  So we key off the ledger
**event kind** cited in the verdict's evidence (``feature`` / ``geometry_spec`` /
``linkage`` / ``build_node``), resolved seq→kind against the ledger, using the frozen
§2/§4 table:

    feature       → DECOMPOSE  / feature_architect
    geometry_spec → GEOMETRY   / geometry_designer
    linkage       → LINKAGE    / linkage_planner
    build_node    → BUILDPATH  / orchestrator          (§84: build_strategist role
                                                         does not exist; orchestrator
                                                         owns build-graph construction)

The verdict ``class`` is a *fallback* signal only — ``spec_drift`` / ``plan_gap`` do
not map 1:1 to a stage, so evidence-kind wins whenever it is present.  The model's
``required_action.dispatch_role`` is a hint we validate against, never the authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# The frozen DP-stage ↔ ledger-kind ↔ role table (design_pipeline.md §2/§4)
# ---------------------------------------------------------------------------

# ledger event kind → (phases.py Phase value, owning role id).  Phase values are the
# lowercase enum values from services.agent.pipeline.phases.Phase — NOT the stages.py
# STAGES vocabulary (feature/sketch/part_verify); the design-pipeline reroute targets
# the phase machine's DP stages.
_KIND_TO_STAGE: dict[str, tuple[str, str]] = {
    "feature":       ("decompose", "feature_architect"),
    "geometry_spec": ("geometry",  "geometry_designer"),
    "linkage":       ("linkage",   "linkage_planner"),
    "build_node":    ("buildpath", "orchestrator"),
}

# Fallback: verdict class → stage, used ONLY when no evidence ledger-kind resolves.
# Deliberately conservative — an ambiguous class (spec_drift, plan_gap) that names no
# artifact routes to the earliest stage whose product could be wrong, so the fix is
# re-derived from the right rung rather than patched at the point of detection.
_CLASS_TO_STAGE: dict[str, tuple[str, str]] = {
    "missed_requirement": ("decompose", "feature_architect"),
    "plan_gap":           ("decompose", "feature_architect"),
    "wrong_order":        ("buildpath", "orchestrator"),
    "bad_fix":            ("geometry",  "geometry_designer"),
    # spec_drift / cot_error are handled specially (drift → re-ground, see below);
    # they fall through to None so the caller keeps them at scope.stage.
}

# The DP stages, in ladder order — used to pick the EARLIEST owning stage when a
# verdict cites evidence of several kinds (fix the deepest-upstream rung first).
_STAGE_ORDER: tuple[str, ...] = ("decompose", "geometry", "linkage", "buildpath")
_STAGE_RANK: dict[str, int] = {s: i for i, s in enumerate(_STAGE_ORDER)}


@dataclass
class Reentry:
    """Where a targeted correction re-enters the pipeline (the reroute decision).

    ``stage`` is a phases.py Phase value (``"geometry"`` …); ``role`` is the owning
    role id; ``basis`` records WHY this target was chosen (which evidence kind, or the
    class fallback) so the routing is auditable, not a black box.  ``feature`` is the
    specific artifact id to re-enter at when the evidence pinned one (a user change to
    "the bore" carries the bore's geometry_spec/dimension id).

    There is no ``full_restart`` field: a ``Reentry`` is *always* a targeted correction
    to an owning stage (§3.1 "never a blind full retry").  When nothing resolves,
    ``route_verdict`` returns ``None`` rather than a blind-retry Reentry.
    """
    stage: str
    role: str
    basis: str = ""
    feature: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "role": self.role,
            "basis": self.basis,
            "feature": self.feature,
        }


def stage_for_kind(kind: str) -> Optional[tuple[str, str]]:
    """(stage, role) that owns a given ledger event *kind*, or ``None``."""
    return _KIND_TO_STAGE.get(kind)


def _kinds_from_evidence(evidence: dict, ledger: Any | None) -> list[tuple[str, Any]]:
    """Resolve the ledger events a verdict cites into ``[(kind, ref)]`` pairs.

    ``evidence.ledger_events`` are event refs — either integer ``seq`` values or dicts
    carrying ``kind``/``id``.  We resolve seq→kind against ``ledger.events`` (the audit
    surface); a ref that already carries its own ``kind`` is used directly (so the map
    works even without a live ledger, e.g. in the change-request path).
    """
    out: list[tuple[str, Any]] = []
    refs = (evidence or {}).get("ledger_events") or []
    events = list(getattr(ledger, "events", []) or []) if ledger is not None else []
    by_seq = {ev.get("seq"): ev for ev in events if isinstance(ev, dict)}
    for ref in refs:
        if isinstance(ref, dict):
            kind = ref.get("kind")
            rid = ref.get("id") or ref.get("seq")
            if kind:
                out.append((kind, rid))
                continue
            ref = ref.get("seq")
        ev = by_seq.get(ref)
        if ev is not None:
            out.append((ev.get("kind"), _event_artifact_id(ev)))
    return out


def _event_artifact_id(ev: dict) -> Any:
    """The stable artifact id a DP event carries (``payload.id``), or its seq."""
    payload = ev.get("payload") or {}
    return payload.get("id") or ev.get("seq")


def _ledger_has_dp_artifacts(ledger: Any | None) -> bool:
    """True iff the run has produced ≥1 design-pipeline artifact.

    A legacy audit (design_pipeline flag OFF) never writes a feature / geometry_spec /
    linkage / build_node into ledger.state, so an empty DP state is the deterministic
    signal that no DP stage owns anything — the class fallback must NOT invent a
    re-entry into a phase machine that isn't running.
    """
    state = getattr(ledger, "state", None)
    if not isinstance(state, dict):
        return False
    return any(state.get(section) for section in
               ("features", "geometry_specs", "linkages", "build_nodes"))


def route_verdict(verdict: dict, *, ledger: Any | None = None,
                  dp_active: bool = False) -> Optional[Reentry]:
    """Compute the targeted re-entry for one audit verdict.

    Priority (the routing rule):
      1. **Evidence ledger-kind** — the strongest signal.  Resolve every cited event
         to its kind; if any maps to a DP stage, re-enter the EARLIEST such stage
         (fix the deepest-upstream rung).  Carries the artifact id as ``feature``.
         This branch is self-gating: it only fires when a DP event kind is cited, so
         a non-DP run (which has no such events) never reaches it.
      2. **Verdict class fallback** — when no evidence kind resolves, a class → stage
         map covers the common cases (``missed_requirement`` → decompose, …).  Unlike
         (1) this branch has no intrinsic DP signal, so it is GATED on the run actually
         being in the design pipeline: it fires only when ``dp_active`` is passed OR the
         ledger already carries a DP artifact.  A legacy audit thus never routes to a
         DP phase that isn't running.
      3. ``None`` — an ``ok`` verdict, a non-DP run, or a verdict whose class/evidence
         names no DP stage (the caller keeps it where it was detected — no blind
         restart is invented).

    A ``spec_drift`` verdict is intent-drift: it re-grounds at its OWNING stage (from
    evidence) rather than restarting, so the fresh-context stage is re-run against the
    pinned master intent (§3.1 "re-grounds the master intent").
    """
    if not isinstance(verdict, dict):
        return None
    cls = verdict.get("class")
    if cls == "ok":
        return None

    # (1) Evidence-kind routing — earliest owning DP stage wins.
    kinds = _kinds_from_evidence(verdict.get("evidence") or {}, ledger)
    best: Optional[tuple[str, str, Any]] = None  # (stage, role, artifact_id)
    for kind, art in kinds:
        mapped = _KIND_TO_STAGE.get(kind or "")
        if mapped is None:
            continue
        stage, role = mapped
        if best is None or _STAGE_RANK[stage] < _STAGE_RANK[best[0]]:
            best = (stage, role, art)
    if best is not None:
        stage, role, art = best
        return Reentry(stage=stage, role=role,
                       basis=f"evidence cites a {_stage_kind(stage)} event",
                       feature=art if isinstance(art, str) else None)

    # (2) Class fallback — only within the design pipeline (see docstring).  A legacy
    # audit with no DP artifacts and no dp_active flag falls through to (3).
    if dp_active or _ledger_has_dp_artifacts(ledger):
        mapped = _CLASS_TO_STAGE.get(cls or "")
        if mapped is not None:
            stage, role = mapped
            return Reentry(stage=stage, role=role,
                           basis=f"class={cls} (no evidence artifact) → owning stage")

    # (3) No DP stage owns this — keep it where it was detected, no restart.
    return None


def _stage_kind(stage: str) -> str:
    """The ledger kind a DP stage owns (inverse of _KIND_TO_STAGE) — for basis text."""
    for kind, (st, _role) in _KIND_TO_STAGE.items():
        if st == stage:
            return kind
    return stage


def route_audit(audit: dict, *, ledger: Any | None = None,
                dp_active: bool = False) -> dict:
    """Attach a ``targeted_reentry`` to every non-``ok`` verdict in an audit record.

    Deterministic post-validation step (the model's reroute prose is stripped by
    ``validate_audit``; the harness computes the real target here).  Returns the same
    audit dict with each verdict's ``targeted_reentry`` filled in — per-verdict, since
    each finding may own a different stage (audit events carry ``{stage, finding,
    targeted_reentry}``).  ``ok`` verdicts get no reentry.  ``dp_active`` forces the
    class-fallback branch on for an explicitly-DP run; otherwise a DP artifact in the
    ledger opens that gate (see ``route_verdict``).
    """
    for v in audit.get("verdicts", []):
        re = route_verdict(v, ledger=ledger, dp_active=dp_active)
        v["targeted_reentry"] = re.to_dict() if re is not None else None
    return audit


def route_change(affected: Any, dispatch: list[dict]) -> list[dict]:
    """Add DP-stage + feature re-entry to a change-request dispatch list.

    change_request.py maps a user change onto EXECUTION roles (feature_modeler,
    assembly_fit_engineer) — the "who edits the doc" layer.  This adds the DESIGN-STAGE
    re-entry: which DP stage/feature the change re-derives from, so a "thicker wall"
    re-enters GEOMETRY on the wall's geometry_spec rather than being blindly re-modeled.

    ``affected`` is a ``change_request.AffectedSet``.  We route from the affected
    artifacts: a touched ``geometry_spec`` / ``feature`` / ``linkage`` id pins the DP
    stage; a touched dimension routes to GEOMETRY (dimensions realize geometry).
    Returns the dispatch list with a ``stage_reentry`` object appended per row that
    resolves to a DP stage (rows that don't are left unchanged).
    """
    stage_reentry = _stage_reentry_from_affected(affected)
    if stage_reentry is None:
        return dispatch
    for row in dispatch:
        row["stage_reentry"] = stage_reentry.to_dict()
    return dispatch


def _stage_reentry_from_affected(affected: Any) -> Optional[Reentry]:
    """Pick the DP stage + feature a change-request affected-set re-enters at.

    A change that touches a **dimension** is a geometry-realization change — the value
    lives on a ``geometry_spec`` and is re-derived at GEOMETRY, even though the
    affected-set also pulls in the ``features`` those dimensions ``ref`` (in
    change_request.py the ``features`` list is ALWAYS reached via a dimension's refs,
    never matched directly).  So dimensions take precedence: "make the wall thicker"
    → GEOMETRY on the wall's dimension, carrying the bound feature id when there is
    one.  Only an affected set with a feature but NO dimension (a purely functional
    change) re-enters DECOMPOSE.
    """
    dims = getattr(affected, "dimensions", None) or []
    feats = getattr(affected, "features", None) or []
    if dims:
        # A dimension change re-derives its geometry — re-enter GEOMETRY.  Prefer the
        # bound feature id as the re-entry handle (the geometry_spec the dim realizes),
        # else the dimension id.
        did = dims[0].get("id")
        fid = feats[0].get("feature") if feats else None
        return Reentry(stage="geometry", role="geometry_designer",
                       basis="change touches a dimension → its geometry_spec",
                       feature=fid or did)
    if feats:
        # A functional feature touched without any dimension — the deepest-upstream
        # owner is DECOMPOSE.
        fid = feats[0].get("feature")
        return Reentry(stage="decompose", role="feature_architect",
                       basis="change touches a feature[] artifact (no dimension)",
                       feature=fid)
    return None
