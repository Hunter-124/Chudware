"""Per-role tool-call floors — ONE definition for both dispatch paths.

The harness has two independent ways to run a sub-agent turn:

  * ``mission_dispatch.MissionDispatch._run_subagent`` — reached by the
    ``delegate`` tool (and corporate spawns).
  * ``services.dag.node_runner._default_agent_fn`` — reached by executing a
    compiled ``aicad.dag/1`` graph node.

They historically computed tool-call budgets separately, and the role list that
earns a raised floor was hardcoded in the first one only. That drift is not
hypothetical: ``model3d``/``sketch2d`` — the roles the system prompt itself
tells the agent to delegate 3D/2D work to — were missing from it and died at
``max_iters`` after 6 calls, and the DAG path never consulted the floor at all
(``context_pack["max_tools_per_turn"]`` has no writer anywhere, so every node
fell through to ``agent_turn``'s bare default of 16 regardless of role).

Keep the knowledge here, and detect on the TOOL GRANT first: role names drift,
but a role holding ``cam_generate`` is a CAM producer whatever its card is
called.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

__all__ = [
    "PRODUCER_TOOLS",
    "BUILD_ROLES",
    "BUILD_PHASES",
    "ROLE_MIN_CALLS",
    "PRODUCER_MIN_CALLS",
    "DEFAULT_MAX_CALLS",
    "completion_cap_for_subagent",
    "is_producer_role",
    "floor_for",
]

# Tools that produce a DELIVERABLE (not merely open a doc or read state).
# Holding any of these means the role is expected to ship an artifact, and the
# turn must have room for its whole mandated chain.
PRODUCER_TOOLS: frozenset[str] = frozenset({
    # mechanical geometry
    "new_part", "add_features",
    # CAM
    "cam_generate", "cam_simulate", "cam_post",
    # simulation
    "run_fea_static", "run_fea_modal", "run_cfd_flow",
    # drawings
    "make_drawing", "export_drawing",
    # ECAD
    "ecad_new_schematic", "board_from_schematic", "ecad_run_drc", "ecad_run_erc",
    # firmware
    "fw_new_project", "fw_build",
})

# Legacy name list, kept as a belt-and-braces fallback for cards whose
# allowlist is narrowed to nothing recognisable. The tool-grant check above is
# the primary signal — prefer adding tools here, not names.
BUILD_ROLES: frozenset[str] = frozenset({
    "feature_modeler", "geometry_designer", "sketcher",
    "assembly_fit_engineer", "layout_engineer",
    "model3d", "sketch2d", "re_modeler", "fixer",
    "cam_programmer", "sim_engineer", "drawing_detailer", "ecad_engineer",
    "firmware_engineer", "spec_writer",
})

BUILD_PHASES: frozenset[str] = frozenset({
    "build", "feature", "geometry", "layout", "assembly",
    "cam", "sim", "simulation", "drawing", "ecad", "firmware",
})

# Role-specific SOFT convergence thresholds, counted from the mandatory calls in
# each role card (optional retries excluded):
#   ecad_engineer   ledger + schematic + find×7 + place×7 + wire×6 + power×2
#                   + ERC + ERC note + simulate + board + DRC + DRC note
#                   + artifact notes×2 = 32; antenna design + analysis + note = 35
#   model3d         delegate + render + preflight + add + post-check for each of
#                   four features, then new_part + material + battery + overlap = 24
#   feature_modeler mandatory build/verification chain                         = 18
#   spec_writer     research plus nine ledger/spec entries                     = 18
# These are warnings to converge, not dispatch caps. The runaway hard ceiling
# remains owned by agent_turn.
ROLE_MIN_CALLS: dict[str, int] = {
    "ecad_engineer": 35,
    "model3d": 24,
    "feature_modeler": 18,
    "spec_writer": 18,
}

# Fallback for other producer roles whose shorter cards still exceed the bare
# default. Keep this separate from the audited per-role thresholds above.
PRODUCER_MIN_CALLS = 16

# agent_turn.run_turn's bare default when max_tool_calls is None.
DEFAULT_MAX_CALLS = 16


def completion_cap_for_subagent(profile: Any | None) -> int:
    """Return one child completion's generation headroom.

    Tier profiles own their cap. Untiered child turns use the same linear-role
    headroom as the delegate lane; importing that constant here prevents the two
    dispatch paths from drifting to separate magic numbers again.
    """
    try:
        headroom = getattr(profile, "generation_headroom", None)
        if headroom is not None and int(headroom) > 0:
            return int(headroom)
    except Exception:  # noqa: BLE001 — malformed optional profile uses fallback
        pass
    from services.agent.mission_dispatch import (  # noqa: PLC0415
        LINEAR_ROLE_MAX_OUTPUT_TOKENS,
    )
    return int(LINEAR_ROLE_MAX_OUTPUT_TOKENS)


def is_producer_role(
    role: str | None,
    allowed: Optional[Iterable[str]] = None,
    phase: str | None = None,
) -> bool:
    """True when *role* is expected to ship an artifact this turn.

    Tool grant wins over the name list — a card renamed tomorrow keeps its
    floor as long as it still holds a producer tool.
    """
    if allowed:
        for tool in allowed:
            if tool in PRODUCER_TOOLS:
                return True
    role_s = (role or "").strip().lower()
    if role_s in BUILD_ROLES:
        return True
    phase_s = (phase or "").strip().lower()
    return bool(phase_s) and phase_s in BUILD_PHASES


def floor_for(
    role: str | None,
    allowed: Optional[Iterable[str]] = None,
    phase: str | None = None,
    base: int | None = None,
) -> int:
    """Tool-call budget for *role*, never below the producer floor.

    *base* is the caller's derived budget (token-share, env override, …);
    omit it to get the plain default.
    """
    budget = int(base) if base else DEFAULT_MAX_CALLS
    role_s = (role or "").strip().lower()
    role_floor = ROLE_MIN_CALLS.get(role_s)
    if role_floor is not None:
        return max(budget, role_floor)
    if is_producer_role(role_s, allowed, phase):
        return max(budget, PRODUCER_MIN_CALLS)
    return budget
