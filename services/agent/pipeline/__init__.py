"""services.agent.pipeline — the copilot mission pipeline glue (T-W19).

The deterministic front-door helpers that turn a user request into delivered
parts through the 8-phase mission machine specified in
``docs/COPILOT_MISSION_PIPELINE.md``.  Everything here composes from already-built
pieces (ledger T-W1, foreman T-W5, roles T-W7, buildgraph T-W15) — this package
owns only the deterministic glue:

  * ``manifest``  — the ``aicad.deliverables/1`` validator (contract §16).
  * ``phases``    — the phase state machine + exit gates (doc §1), state logged
                    as ledger ``stage`` events.
  * ``replan``    — the ``aicad.replan/1`` approval routing (contract §17).

The DAG mission template (``services/dag/mission_template.py``) turns a validated
manifest into an ``aicad.dag/1`` graph whose nodes carry role assignments; the
MCP tools (``services/mcp/mission_tools.py``) and ``services.agent.loop.run_mission``
are the callable front doors.
"""
from __future__ import annotations

from .manifest import (
    ManifestFinding,
    validate_manifest,
    manifest_valid,
    blocking_ambiguities,
    manifest_template,
    BounceTracker,
)
from .phases import (
    Phase,
    PHASE_ORDER,
    PhaseMachine,
    PhaseError,
    GateResult,
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
    "BounceTracker",
    # phases
    "Phase",
    "PHASE_ORDER",
    "PhaseMachine",
    "PhaseError",
    "GateResult",
    "PipelineFlags",
    # replan
    "RoutingDecision",
    "route_replan",
    "apply_replan",
    "validate_replan_request",
    "ReplanError",
]
