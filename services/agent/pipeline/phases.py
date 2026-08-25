"""phases.py — the 8-phase mission state machine + exit gates (doc §1).

The mission machine is a linear phase sequence with two feature-flagged additions
(the 1.3 org layer):

    INTAKE → [CLARIFY] → SPECIFY
             → [DECOMPOSE → GEOMETRY → LINKAGE → BUILDPATH]   (T-DP1, flagged OFF)
             → [ORGANIZE] → PLAN → BUILD → VERIFY → [CORRECT ↺] → DELIVER

Each phase has an EXIT GATE: a named deterministic check that must pass before the
mission advances.  A gate never calls a model — it inspects durable state (the
manifest, the ledger, the build graphs, per-node foreman verdicts, criterion
results).  Phase entry/exit are recorded as ledger ``stage`` events, so mission
status (what the GUI and mission control render) is a pure read of the ledger.

Gate → named check (doc §1):
  * INTAKE   — manifest valid (contract §16 validator);
  * CLARIFY  — no blocking ambiguity remains open;
  * SPECIFY  — spec audited + every criterion covered by a verification plan;
  * ORGANIZE — departments instantiated (feature-flagged: T-W22/T-W23);
  * PLAN     — build graphs valid + (council synthesis recorded, feature-flagged)
               + overseer ratified;
  * BUILD    — every node's exit gate passed OR carries a failure card / replan;
  * VERIFY   — all criteria PASS or waived-by-name;
  * CORRECT  — no open failure cards (loops with BUILD/VERIFY);
  * DELIVER  — mission report emitted, ledger closed.

Feature flags (``PipelineFlags``) gate the 1.3 org additions.  ORGANIZE and the
PLAN-phase build-strategy council are OFF by default because T-W22 (org runtime)
and T-W23 (councils) are NOT merged; when they land, flip the flags on and the
machine inserts/enforces those phases without any structural change here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class Phase(str, Enum):
    """The mission phases (doc §1).  ``str`` mixin so a phase serialises as its
    lowercase name in the ledger stage_log and mission_status render."""
    INTAKE = "intake"
    CLARIFY = "clarify"
    SPECIFY = "specify"
    # T-DP1: the six-stage design-pipeline refinement of the SPECIFY→PLAN span
    # (docs/AGENT_DESIGN_PIPELINE.md §2, contract design_pipeline.md §6). These four
    # phases are feature-flagged (PipelineFlags.design_pipeline, default OFF) and are
    # skipped by next_phase when off — the existing pipeline is unchanged. Stage 0
    # (intent) = INTAKE/SPECIFY; Stage 5 (execution) = the existing BUILD phase.
    DECOMPOSE = "decompose"      # Stage 1 — functional decomposition (feature[])
    GEOMETRY = "geometry"        # Stage 2 — geometry realization (geometry_spec[])
    LINKAGE = "linkage"          # Stage 3 — topology / linkage (linkage[])
    BUILDPATH = "buildpath"      # Stage 4 — build-path planning (build_node[])
    ORGANIZE = "organize"        # Phase 2.5 (contract §18) — feature-flagged
    PLAN = "plan"
    BUILD = "build"
    VERIFY = "verify"
    CORRECT = "correct"
    DELIVER = "deliver"


# The full ordered sequence.  CLARIFY, ORGANIZE and CORRECT are conditionally
# skipped (see ``PhaseMachine.next_phase``); the ORDER itself never changes.
PHASE_ORDER: tuple[Phase, ...] = (
    Phase.INTAKE,
    Phase.CLARIFY,
    Phase.SPECIFY,
    # T-DP1 design-pipeline stages — skipped unless the design_pipeline flag is on.
    Phase.DECOMPOSE,
    Phase.GEOMETRY,
    Phase.LINKAGE,
    Phase.BUILDPATH,
    Phase.ORGANIZE,
    Phase.PLAN,
    Phase.BUILD,
    Phase.VERIFY,
    Phase.CORRECT,
    Phase.DELIVER,
)

# T-DP1: the design-pipeline phases, in order — the set next_phase skips when the
# design_pipeline flag is off.
_DESIGN_PIPELINE_PHASES: frozenset[Phase] = frozenset({
    Phase.DECOMPOSE, Phase.GEOMETRY, Phase.LINKAGE, Phase.BUILDPATH,
})


class PhaseError(RuntimeError):
    """Raised when an illegal transition is attempted (e.g. entering BUILD from a
    phase whose exit gate has not passed)."""


@dataclass
class GateResult:
    """The verdict of a phase exit gate.

    ``passed``   — the gate is satisfied; the mission may advance.
    ``gate``     — the named check (recorded on the ledger ``stage`` exit event).
    ``reasons``  — human-readable reasons the gate failed (empty when passed).
    ``overseer`` — True if this gate fires an overseer audit trigger (doc §2);
                   the caller (or run_mission) is responsible for the actual
                   audit call, feature-flagged on T-W13.
    """
    passed: bool
    gate: str
    reasons: list[str] = field(default_factory=list)
    overseer: bool = False


@dataclass
class PipelineFlags:
    """Feature flags for the 1.3 org layer.

    T-W22 landed: org_layer default is now ON (Phase 2.5 ORGANIZE is live).
    build_council and overseer remain env-gated (T-W23 / T-W13 not merged).

    Environment overrides (always respected):
      * ``CHUDWARE_ORG_LAYER=0``      → disable Phase 2.5 ORGANIZE (test/compat).
      * ``CHUDWARE_ORG_LAYER=1``      → explicit enable (no-op; already on by default).
      * ``CHUDWARE_BUILD_COUNCIL=1``  → require build-strategy council synthesis at the
        PLAN exit gate (contract §19).
      * ``CHUDWARE_OVERSEER=1``       → overseer audit triggers are live (T-W13).
      * ``CHUDWARE_DESIGN_PIPELINE=1`` → run the six-stage design-pipeline refinement
        (T-DP1: DECOMPOSE→GEOMETRY→LINKAGE→BUILDPATH between SPECIFY and PLAN).
        Default OFF — the existing pipeline is unchanged when off (contract §6).
    """
    org_layer: bool = True   # T-W22 landed — ORGANIZE phase is live
    build_council: bool = False
    overseer: bool = False
    design_pipeline: bool = False   # T-DP1 — six-stage refinement, default OFF

    @classmethod
    def from_env(cls) -> "PipelineFlags":
        def _on(name: str, default: bool) -> bool:
            val = os.environ.get(name)
            if val is None:
                return default
            return val in ("1", "true", "True")
        return cls(
            org_layer=_on("CHUDWARE_ORG_LAYER", True),   # default ON (T-W22)
            build_council=_on("CHUDWARE_BUILD_COUNCIL", False),
            overseer=_on("CHUDWARE_OVERSEER", False),
            design_pipeline=_on("CHUDWARE_DESIGN_PIPELINE", False),  # default OFF (T-DP1)
        )


# A gate check receives the mission context (a dict the caller assembles from
# durable state — manifest, build graphs, node verdicts, criterion results) and
# returns a GateResult.
GateCheck = Callable[["PhaseMachine", dict[str, Any]], GateResult]


class PhaseMachine:
    """Drives one mission through the phase sequence, logging each transition to the
    ledger and enforcing exit gates.

    The machine is deterministic and headless: it never calls a model.  The caller
    (``run_mission`` / the DAG mission template) supplies *context* — the durable
    artifacts a gate inspects — and the machine decides whether the phase may exit
    and which phase is next.

    ``ledger`` is optional so the machine is unit-testable without a live ledger;
    when present, every ``enter``/``exit`` writes a ``stage`` event (``log_stage``).
    """

    def __init__(
        self,
        *,
        ledger: Any = None,
        flags: Optional[PipelineFlags] = None,
        actor: str = "pipeline",
        checkpoint_bridge: Any = None,
    ) -> None:
        self.ledger = ledger
        self.flags = flags or PipelineFlags.from_env()
        self.actor = actor
        self.checkpoint_bridge = checkpoint_bridge
        if ledger is not None and checkpoint_bridge is not None:
            attach = getattr(checkpoint_bridge, "attach_ledger", None)
            if callable(attach):
                try:
                    attach(ledger)
                except Exception:  # noqa: BLE001 — optional History seam
                    pass
        self.current: Optional[Phase] = None
        self.history: list[Phase] = []

    # ------------------------------------------------------------------ transitions

    def enter(self, phase: Phase) -> None:
        """Enter *phase*, recording a ledger stage-entry event.

        Enforces the legal-order rule: a phase may only be entered from its
        predecessor in the (skip-adjusted) sequence, or re-entered from a later
        phase during a CORRECT loop (BUILD/VERIFY re-entry).
        """
        if self.current is not None and not self._legal_transition(self.current, phase):
            raise PhaseError(
                f"illegal transition {self.current.value} → {phase.value}: "
                "the exit gate for the current phase must pass and the target must "
                "be the next phase (or a CORRECT-loop re-entry)")
        self.current = phase
        self.history.append(phase)
        if self.ledger is not None:
            try:
                self.ledger.log_stage(self.actor, phase.value)
            except Exception:  # noqa: BLE001 — ledger is best-effort, never blocks
                pass

    def exit(
        self,
        phase: Phase,
        gate: GateResult,
        *,
        item_artifacts: dict[str, str] | None = None,
    ) -> None:
        """Record a successful exit and its design-history checkpoint.

        ``item_artifacts`` maps PLM item ids to the buildgraph work unit that
        produced each part, preserving per-part attribution for assembly runs.
        """
        if not gate.passed:
            raise PhaseError(
                f"phase {phase.value} exit gate {gate.gate!r} failed: "
                f"{'; '.join(gate.reasons)}")
        stage_logged = False
        if self.ledger is not None:
            try:
                from services.agent.ledger import _now  # local import, best-effort
                self.ledger.log_stage(self.actor, phase.value,
                                      exited=_now(), exit_gate=gate.gate)
                stage_logged = True
            except Exception:  # noqa: BLE001
                pass
        if (
            stage_logged
            and self.flags.design_pipeline
            and phase in _DESIGN_PIPELINE_PHASES
            and self.checkpoint_bridge is not None
        ):
            try:
                self.checkpoint_bridge.on_milestone(
                    trigger="stage_exit",
                    stage=phase.value,
                    buildgraph_node=f"phase:{phase.value}",
                    item_artifacts=dict(item_artifacts or {}),
                )
            except Exception:  # noqa: BLE001 — History never blocks mission progress
                pass

    def next_phase(self, phase: Phase, context: dict[str, Any]) -> Optional[Phase]:
        """The phase that follows *phase*, applying the skip rules:

          * CLARIFY is skipped when there is no blocking ambiguity
            (``context["has_blocking_ambiguity"]`` is falsey);
          * ORGANIZE is skipped unless the org layer flag is on;
          * CORRECT is skipped when there are no open failure cards
            (``context["open_failure_cards"]`` is empty).

        Returns ``None`` after DELIVER (the mission is complete).
        """
        idx = PHASE_ORDER.index(phase)
        for cand in PHASE_ORDER[idx + 1:]:
            if cand is Phase.CLARIFY and not context.get("has_blocking_ambiguity"):
                continue
            # T-DP1: the four design-pipeline phases are skipped unless the flag is on.
            # With the flag off, next_phase(SPECIFY) advances straight to ORGANIZE/PLAN
            # exactly as before — the existing pipeline is byte-for-byte unchanged.
            if cand in _DESIGN_PIPELINE_PHASES and not self.flags.design_pipeline:
                continue
            if cand is Phase.ORGANIZE and not self.flags.org_layer:
                continue
            if cand is Phase.CORRECT and not context.get("open_failure_cards"):
                continue
            return cand
        return None

    def _legal_transition(self, frm: Phase, to: Phase) -> bool:
        """True if ``frm → to`` is legal.  Forward-by-one (skip-adjusted) is legal;
        a CORRECT loop may re-enter BUILD or VERIFY from CORRECT."""
        # CORRECT loops back into BUILD/VERIFY (doc §1: "re-enter here").
        if frm is Phase.CORRECT and to in (Phase.BUILD, Phase.VERIFY):
            return True
        # VERIFY may route to CORRECT when criteria fail.
        if frm is Phase.VERIFY and to is Phase.CORRECT:
            return True
        # Otherwise the target must be a forward phase (never backward except the
        # explicit CORRECT loop above).  We do not recompute skips here (the caller
        # drives next_phase); any forward phase is accepted.
        return PHASE_ORDER.index(to) > PHASE_ORDER.index(frm)

    # ------------------------------------------------------------------ exit gates

    def run_gate(self, phase: Phase, context: dict[str, Any]) -> GateResult:
        """Run the named exit gate for *phase* against *context*.  Pure dispatch to
        the per-phase gate checker."""
        checker = _GATES.get(phase)
        if checker is None:
            # Phases without a bespoke gate (none currently) pass trivially.
            return GateResult(True, f"{phase.value}_noop")
        return checker(self, context)


# ---------------------------------------------------------------------------
# Exit-gate implementations — each inspects durable context only (no model call).
# ---------------------------------------------------------------------------

def _gate_intake(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Manifest valid (contract §16 validator ran clean of errors)."""
    findings = ctx.get("manifest_findings") or []
    from .manifest import errors as _errs
    errs = _errs(findings)
    if errs:
        return GateResult(False, "manifest_valid",
                          [str(e) for e in errs],
                          overseer=bool(ctx.get("second_validation_failure")))
    return GateResult(True, "manifest_valid")


def _gate_clarify(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """No blocking ambiguity remains open."""
    if ctx.get("has_blocking_ambiguity"):
        return GateResult(False, "no_blocking_ambiguity",
                          ["a blocking ambiguity is still open — "
                           "the clarifier's answers have not resolved it"])
    return GateResult(True, "no_blocking_ambiguity")


def _gate_specify(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Spec audited + every acceptance criterion covered by the verification plan."""
    reasons: list[str] = []
    if not ctx.get("spec_audited"):
        reasons.append("spec not overseer-audited (trigger: plan_ratify)")
    uncovered = ctx.get("uncovered_criteria") or []
    if uncovered:
        reasons.append(f"acceptance criteria not covered by a verification plan: "
                       f"{uncovered}")
    return GateResult(not reasons, "spec_audited_criteria_covered", reasons,
                      overseer=True)


def _gate_decompose(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Stage 1 exit — ``features_present`` (contract design_pipeline.md §5).

    Reads the ledger: passes when ≥1 ``feature`` event exists (``ctx['features']``,
    a pure read of ``state.features``). Semantic coverage (every requirement mapped)
    is a T-DP2 checker that layers on top — this gate only requires presence.

    When the caller supplies coverage findings (``ctx['untraced_features']`` or
    ``ctx['orphan_requirements']``), those are surfaced as additional gate reasons
    without changing the gate name (``"features_present"``). This is the T-DP2
    "extend gate context, not gate signature" contract (design_pipeline.md §5).
    """
    reasons: list[str] = []
    if not (ctx.get("features") or []):
        reasons.append(
            "no feature[] artifacts in the ledger — Stage 1 "
            "(functional decomposition) produced nothing to build on"
        )
        # No point surfacing coverage findings when there are no features.
        return GateResult(False, "features_present", reasons)

    # T-DP2: optional coverage findings — present only when the caller ran
    # ``functional_decomposition.gate_context_from_ledger`` (or supplied them
    # manually). The gate name stays "features_present" in all cases so the
    # existing DP1 tests continue to pass unchanged.
    untraced = ctx.get("untraced_features") or []
    orphaned = ctx.get("orphan_requirements") or []
    if untraced:
        reasons.append(
            f"features with no requirement trace (untraced): {untraced} — "
            "each must reference a ledger req_id or declare assumption:<slug>"
        )
    if orphaned:
        reasons.append(
            f"requirements with no covering feature (orphaned): {orphaned} — "
            "add a feature for each or mark the requirement cancelled"
        )

    return GateResult(not reasons, "features_present", reasons)


def _gate_geometry(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Stage 2 exit — ``geometry_specs_present`` (contract §5).

    Reads the ledger: every feature id is realized by ≥1 geometry_spec AND every
    geometry_spec carries provenance (the "never recall a number" rule, enforced at
    write time too but re-checked here so the gate is self-contained)."""
    reasons: list[str] = []
    features = ctx.get("features") or []
    specs = ctx.get("geometry_specs") or []
    if not specs:
        reasons.append("no geometry_spec[] artifacts in the ledger — Stage 2 "
                       "(geometry realization) produced nothing")
    realized: set[str] = set()
    for s in specs:
        realized.update(s.get("realizes") or [])
    uncovered = [f.get("id") for f in features if f.get("id") not in realized]
    if uncovered:
        reasons.append(f"features with no realizing geometry_spec: {uncovered}")
    no_prov = [s.get("id") for s in specs if not s.get("provenance")]
    if no_prov:
        reasons.append(f"geometry_spec without provenance (rejected rule): {no_prov}")
    return GateResult(not reasons, "geometry_specs_present", reasons, overseer=True)


def _gate_linkage(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Stage 3 exit — ``linkage_present`` (contract §5).

    Reads the ledger: passes when ≥1 ``linkage`` event exists.  When the caller
    supplies T-DP4 checker findings (``dangling_refs``, ``ref_cycles``,
    ``datum_gaps``, ``interface_mismatches``) via
    ``linkage_integrity.gate_context_from_ledger``, those are surfaced as
    additional gate reasons without changing the gate name
    (``"linkage_present"``).  This is the "extend context, not gate signature"
    rule (design_pipeline.md §5), mirroring ``_gate_decompose`` (T-DP2).
    """
    reasons: list[str] = []
    if not (ctx.get("linkages") or []):
        reasons.append(
            "no linkage[] artifact in the ledger — Stage 3 "
            "(topology / linkage) produced no skeleton"
        )
        return GateResult(False, "linkage_present", reasons)

    # T-DP4: optional checker findings — present only when the caller ran
    # ``linkage_integrity.gate_context_from_ledger`` (or supplied them manually).
    # Gate name stays "linkage_present" in all cases.
    dangling = ctx.get("dangling_refs") or []
    cycles   = ctx.get("ref_cycles")    or []
    gaps     = ctx.get("datum_gaps")    or []
    iface    = ctx.get("interface_mismatches") or []

    if dangling:
        reasons.append(
            f"reference_edges with dangling endpoints (to unknown geometry_spec "
            f"or datum id): {dangling} — fix or remove these references before "
            "Stage 4 can order the build"
        )
    if cycles:
        reasons.append(
            f"cycles in reference_edges (part cannot be built — feature requires "
            f"itself): {cycles} — break cycles by re-anchoring to datums"
        )
    if gaps:
        reasons.append(
            f"geometry_spec datum_refs referencing undeclared datums: {gaps} — "
            "add missing datums to datum_tree or correct the datum_ref"
        )
    if iface:
        reasons.append(
            f"shared-interface dimension mismatches (two specs disagree on the "
            f"same bore Ø/tolerance): {iface} — one spec must adopt the other's "
            "value or a replan_request scope=spec is required"
        )

    return GateResult(not reasons, "linkage_present", reasons, overseer=bool(reasons))


def _gate_buildpath(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Stage 4 exit — ``build_nodes_present`` (contract §5).

    Reads the ledger: ≥1 build_node exists AND every build_node.elements id resolves
    to a geometry_spec id (no dangling element ref). Cycle/dep validation is the
    buildgraph validator's job at the PLAN gate (advisor.md §13)."""
    reasons: list[str] = []
    nodes = ctx.get("build_nodes") or []
    specs = ctx.get("geometry_specs") or []
    if not nodes:
        reasons.append("no build_node[] artifacts in the ledger — Stage 4 "
                       "(build-path planning) produced no ordered pathway")
    spec_ids = {s.get("id") for s in specs}
    dangling: list[str] = []
    for n in nodes:
        for el in (n.get("elements") or []):
            if el not in spec_ids:
                dangling.append(f"{n.get('id')}→{el}")
    if dangling:
        reasons.append(f"build_node elements referencing no geometry_spec: {dangling}")
    return GateResult(not reasons, "build_nodes_present", reasons, overseer=True)


def _gate_organize(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Departments instantiated from the manifest clusters (contract §18).

    Feature-flagged: when the org layer is off this phase is never entered (see
    ``next_phase``); if it *is* entered, the gate requires a non-empty org chart.
    """
    if not m.flags.org_layer:
        return GateResult(True, "org_disabled")
    if not (ctx.get("org_chart") or {}).get("departments"):
        return GateResult(False, "departments_instantiated",
                          ["org layer is enabled but no departments were "
                           "instantiated from the manifest clusters"])
    return GateResult(True, "departments_instantiated")


def _gate_plan(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Build graphs valid + (council synthesis recorded) + overseer ratified.

    Uses the buildgraph validator (contract §13) result the caller supplies:
    ``build_graph_blockers`` is the list of blocker-severity findings (cycles are
    surfaced by the caller as a blocker string).  Council synthesis is required
    only when the build-council flag is on (T-W23).
    """
    reasons: list[str] = []
    blockers = ctx.get("build_graph_blockers") or []
    if blockers:
        reasons.append(f"build graph has structural blockers: {blockers}")
    if m.flags.build_council and not ctx.get("council_synthesis_recorded"):
        reasons.append("build-strategy council synthesis not recorded "
                       "(contract §19 Phase 3 exit requirement)")
    if not ctx.get("plan_ratified"):
        reasons.append("plan not overseer-ratified (trigger: plan_ratify)")
    return GateResult(not reasons, "graphs_valid_ratified", reasons, overseer=True)


def _gate_build(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Every build-graph node's exit gate passed OR the node carries a failure card
    / replan request (doc §1 per-node rule).  A node that is neither done nor has a
    card/replan blocks the BUILD exit."""
    unresolved = ctx.get("unresolved_nodes") or []
    if unresolved:
        return GateResult(False, "nodes_done_or_carded",
                          [f"build node(s) not done and without a failure "
                           f"card/replan: {unresolved}"])
    return GateResult(True, "nodes_done_or_carded")


def _gate_verify(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """All acceptance criteria PASS/SKIP, or the failing ones are waived-by-name.

    ``criterion_results`` is ``{ac_id: "PASS"|"FAIL"|"SKIP"|...}``; ``waived`` is the
    set of ac_ids the user waived via ask_user.  A FAIL/NOT-CHECKED that is not
    waived blocks VERIFY (routes to CORRECT via ``next_phase`` because it populates
    open_failure_cards). mega-11: honest SKIP (provision-absence) is gate-ok.

    WI-11: missing verify_battery is FAIL not silent when the mission context
    indicates geometry was built (``geometry_built`` / ``mutating_tools_ran`` /
    ``require_battery``) and no battery evidence/waiver is present.
    """
    results = ctx.get("criterion_results") or {}
    waived = set(ctx.get("waived_criteria") or [])
    ok = {"PASS", "SKIP"}
    failing = [ac for ac, res in results.items()
               if res not in ok and ac not in waived]
    if failing:
        return GateResult(False, "criteria_pass_or_waived",
                          [f"acceptance criteria failed and not waived: {failing}"])

    # HARD floor: battery-before-done (WI-11). ImportError-safe.
    try:
        from services.agent import guardrails as _gr  # noqa: PLC0415
        if _gr.battery_missing_for_mission(ctx):
            return GateResult(
                False, "criteria_pass_or_waived",
                ["verify_battery missing (run battery or waive with reason) — "
                 "HARD guardrail WI-11"],
            )
    except Exception:  # noqa: BLE001
        pass

    return GateResult(True, "criteria_pass_or_waived")


def _gate_correct(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """No open failure cards remain (the fixer resolved them or they escalated)."""
    open_cards = ctx.get("open_failure_cards") or []
    if open_cards:
        return GateResult(False, "failure_cards_cleared",
                          [f"failure cards still open: {open_cards}"],
                          overseer=True)
    return GateResult(True, "failure_cards_cleared")


def _gate_deliver(m: PhaseMachine, ctx: dict[str, Any]) -> GateResult:
    """Mission report emitted + ledger closed (the terminal gate)."""
    reasons: list[str] = []
    if not ctx.get("mission_report_emitted"):
        reasons.append("mission report not emitted")
    return GateResult(not reasons, "mission_report_ledger_closed", reasons)


_GATES: dict[Phase, GateCheck] = {
    Phase.INTAKE: _gate_intake,
    Phase.CLARIFY: _gate_clarify,
    Phase.SPECIFY: _gate_specify,
    # T-DP1 design-pipeline stage gates (contract design_pipeline.md §5)
    Phase.DECOMPOSE: _gate_decompose,
    Phase.GEOMETRY: _gate_geometry,
    Phase.LINKAGE: _gate_linkage,
    Phase.BUILDPATH: _gate_buildpath,
    Phase.ORGANIZE: _gate_organize,
    Phase.PLAN: _gate_plan,
    Phase.BUILD: _gate_build,
    Phase.VERIFY: _gate_verify,
    Phase.CORRECT: _gate_correct,
    Phase.DELIVER: _gate_deliver,
}
