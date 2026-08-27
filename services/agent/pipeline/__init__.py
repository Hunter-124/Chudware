"""Reusable deterministic components for the intended copilot mission pipeline.

The package contains the deliverable-manifest validator, phase state machine,
exit gates, linkage checks, and replan routing specified by the mission-pipeline
contracts.  These components are ready for runtime integration, but the
production ``mission_start`` / mission-template path does not currently
instantiate :class:`PhaseMachine`: ``services/mcp/mission_tools.py`` compiles the
DAG directly.

Accordingly, importing this package does not imply that live missions are
phase-enforced.  Wiring the machine into those front doors remains a separate
product decision.
"""
from __future__ import annotations

from .manifest import (
    ManifestFinding,
    validate_manifest,
    manifest_valid,
    blocking_ambiguities,
    manifest_template,
    manifest_draft,
    normalize_manifest,
    BounceTracker,
)
from .phases import (
    Phase,
    PHASE_ORDER,
    PhaseMachine,
    PhaseError,
    GateResult,
    TransitionResult,
    PipelineFlags,
)
from .replan import (
    RoutingDecision,
    route_replan,
    apply_replan,
    validate_replan_request,
    ReplanError,
)

__all__ = [
    # manifest
    "ManifestFinding",
    "validate_manifest",
    "manifest_valid",
    "blocking_ambiguities",
    "manifest_template",
    "manifest_draft",
    "normalize_manifest",
    "BounceTracker",
    # phases
    "Phase",
    "PHASE_ORDER",
    "PhaseMachine",
    "PhaseError",
    "GateResult",
    "PipelineFlags",
    "TransitionResult",
    # replan
    "RoutingDecision",
    "route_replan",
    "apply_replan",
    "validate_replan_request",
    "ReplanError",
]
