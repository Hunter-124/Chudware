"""Stage vocabulary, tool→stage inference, check batteries, exit gates (T-W5).

Contract: ``contracts/advisor.md`` §1 (stage vocabulary, FROZEN) and §9 (precedence).
Pillar 20: docs/ADVISOR_ARCHITECTURE.md §2.2 (the "foreman").

This module is **pure data + pure functions** — no I/O, no LLM, no doc handles.  It
defines *what the process stages are* and *which read-only checks each stage would
run*.  Whether a given battery can actually fire is a property of the observation
source, not the stage:

  - In the **loop.py hot path** the agent generates build123d *code*; there is no
    persistent doc, no sketch entity, no feature ids.  The only signals that exist
    are ``res.fit`` (interference/clearance by part name + volume) and ``res.error``
    (rebuild).  So only the ``interference`` / ``rebuild`` checks fire there.
  - In the **doc path** (``advise(doc_id)`` MCP tool and DAG nodes carrying a
    doc_id), the full batteries run against ``doc_eval_*`` / ``sketch_health``.

The full vocabulary is defined here regardless, because T-W7 (roles), the mission
pipeline, and the exit-gate machinery all cite these stage names and batteries.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# §1 Stage vocabulary (FROZEN — order matters for advance())
# ---------------------------------------------------------------------------

STAGES: tuple[str, ...] = (
    "intake",
    "spec",
    "layout",
    "sketch",
    "feature",
    "part_verify",
    "assembly",
    "sim",
    "cam",
    "deliver",
)
# ``fix`` is re-entrant (carries a ``return_to`` stage); it is not part of the
# linear order but is a valid stage value.
FIX_STAGE = "fix"
ALL_STAGES: frozenset[str] = frozenset(STAGES) | {FIX_STAGE}


def is_stage(name: str) -> bool:
    return name in ALL_STAGES


# ---------------------------------------------------------------------------
# Tool-name → stage inference table (deterministic; contract §1)
# ---------------------------------------------------------------------------
#
# The last matching tool call sets the stage.  ``stage_set`` overrides this.
# Only the *most stage-defining* tools are mapped; unmapped tools leave the
# stage unchanged (return None → caller keeps the current stage).

_TOOL_STAGE: dict[str, str] = {
    # intake / spec
    "describe_project": "intake",
    "req_create": "spec",
    "req_update": "spec",
    "resolve_process": "spec",
    "set_material": "spec",
    "research_material_lookup": "spec",
    # layout
    "add_datum": "layout",
    "list_world_bodies": "layout",
    # sketch
    "check_sketch": "sketch",
    "describe_sketch": "sketch",
    "sketch_health": "sketch",
    "sketch_auto_constrain": "sketch",
    "sketch_fully_define": "sketch",
    "convert_face_to_sketch": "sketch",
    # feature
    "add_features": "feature",
    "check_feature_overlap": "feature",
    "regen_diagnose": "feature",
    "regen_resolve": "feature",
    "fit_report": "part_verify",
    # part_verify
    "verify_battery": "part_verify",
    "design_review": "part_verify",
    "measure_part": "part_verify",
    "wall_thickness": "part_verify",
    # assembly
    "solve_mates": "assembly",
    "check_interference": "assembly",
    "part_place": "assembly",
    "describe_assembly": "assembly",
    # sim
    "run_fea_static": "sim",
    "run_fea_adaptive": "sim",
    "run_fea_modal": "sim",
    "run_cfd_flow": "sim",
    "describe_results": "sim",
    # cam
    "cam_generate": "cam",
    "cam_simulate": "cam",
    "cam_add_operation": "cam",
    "cam_new_job": "cam",
    # deliver
    "make_drawing": "deliver",
    "export_part": "deliver",
    "make_report": "deliver",
}


def infer_stage(tool_name: str) -> Optional[str]:
    """Return the stage a tool call implies, or ``None`` to keep the current stage.

    ``add_features`` is special-cased by the caller when its ops are sketch ops
    (see :func:`infer_stage_from_ops`); the bare mapping resolves it to ``feature``.
    """
    return _TOOL_STAGE.get(tool_name)


# Sketch-op op-codes that mean an ``add_features`` call is really a sketch edit.
_SKETCH_OPS: frozenset[str] = frozenset({
    "sketch", "sketch_line", "sketch_arc", "sketch_circle", "sketch_rect",
    "sketch_polygon", "sketch_spline", "add_sketch", "new_sketch",
    "sketch_constraint", "sketch_dimension",
})


def infer_stage_from_ops(tool_name: str, ops: Optional[list]) -> Optional[str]:
    """Refine :func:`infer_stage` using op contents.

    ``add_features`` with only sketch ops ⇒ ``sketch``; otherwise ``feature``
    (per docs §2.2: "add_features w/ sketch ops ⇒ sketch").
    """
    if tool_name == "add_features" and ops:
        kinds = {(op.get("op") or op.get("kind") or "") for op in ops if isinstance(op, dict)}
        if kinds and kinds <= _SKETCH_OPS:
            return "sketch"
    return infer_stage(tool_name)


# ---------------------------------------------------------------------------
# Check identifiers (the read-only checks a battery may run)
# ---------------------------------------------------------------------------

CHECK_SKETCH_HEALTH = "sketch_health"
CHECK_FEATURE_OVERLAP = "feature_overlap"
CHECK_REBUILD = "rebuild"
CHECK_INTERFERENCE = "interference"
CHECK_PART_VERIFY = "part_verify"
CHECK_SIM_RESULTS = "sim_results"
CHECK_SIM_BC = "sim_bc"
CHECK_SIM_MARGIN = "sim_margin"
CHECK_SIM_ANCHOR = "sim_anchor"
CHECK_SIM_DIVERGENCE = "sim_divergence"
CHECK_SIM_MESH = "sim_mesh"
CHECK_CAM_SIM = "cam_sim"
CHECK_OPEN_PROFILE = "open_profile"
# T-W22 org-layer: sender's stage exit is blocked until recipients ack
# every interface_change the sender posted (§18).
CHECK_INTERFACE_CHANGE = "interface_change_acked"
# T-DP6: diagnostic advisor checks — DFM, wall thickness, interface alignment
CHECK_DFM = "dfm"
CHECK_WALL_THICKNESS = "wall_thickness"
CHECK_INTERFACE_FITMENT = "interface_fitment"


# ---------------------------------------------------------------------------
# Per-stage battery / helper_tools / exit_gate / playbook (contract §1)
# ---------------------------------------------------------------------------
#
# ``check_battery``  : ordered read-only checks (cheap first).
# ``helper_tools``   : names advertised in advisories for this stage.
# ``exit_gate``      : checks that must PASS before the orchestrator advances.
#                      A ``blocker`` on any exit-gate check gates stage exit
#                      (§9.3) — it does NOT block the model's next action.
# ``playbook_default``: the playbook a role defaults to at this stage (T-W7).

_STAGE_SPEC: dict[str, dict] = {
    "intake": {
        "check_battery": [],
        "helper_tools": ["describe_project", "req_create"],
        "exit_gate": [],
        "playbook_default": None,
    },
    "spec": {
        "check_battery": [],
        "helper_tools": ["fit_calc", "research_material_lookup", "research_thread_spec",
                         "resolve_process"],
        "exit_gate": [],
        "playbook_default": None,
    },
    "layout": {
        "check_battery": [],
        "helper_tools": ["add_datum", "measure_between"],
        "exit_gate": [],
        "playbook_default": "layout_skeleton",
    },
    "sketch": {
        "check_battery": [CHECK_SKETCH_HEALTH, CHECK_OPEN_PROFILE],
        "helper_tools": ["sketch_health", "sketch_auto_constrain",
                         "sketch_fully_define", "describe_sketch"],
        "exit_gate": [CHECK_SKETCH_HEALTH, CHECK_OPEN_PROFILE],
        "playbook_default": "sketch_profile",
    },
    "feature": {
        "check_battery": [CHECK_REBUILD, CHECK_FEATURE_OVERLAP],
        "helper_tools": ["check_feature_overlap", "regen_diagnose", "describe_part"],
        "exit_gate": [CHECK_REBUILD, CHECK_FEATURE_OVERLAP],
        "playbook_default": None,
    },
    "part_verify": {
        # T-DP6: wall_thickness (fast ray-cast) and DFM (process gate) run at part_verify;
        # both are advisory — they never block exit (wall_thickness → action, DFM → blocker
        # only on a hard process failure). CHECK_PART_VERIFY is the legacy verify_battery gate.
        "check_battery": [CHECK_PART_VERIFY, CHECK_WALL_THICKNESS, CHECK_DFM],
        "helper_tools": ["verify_battery", "measure_part", "wall_thickness", "dfm_check"],
        "exit_gate": [CHECK_PART_VERIFY],
        "playbook_default": None,
    },
    "assembly": {
        # T-DP6: interface_fitment (hole-pattern / coaxiality / contact-plane) runs at assembly
        # alongside the hard-clash interference gate.
        "check_battery": [CHECK_INTERFERENCE, CHECK_INTERFACE_FITMENT],
        "helper_tools": ["check_interference", "fit_calc", "measure_between",
                         "solve_mates", "interface_check"],
        "exit_gate": [CHECK_INTERFERENCE],
        "playbook_default": None,
    },
    "sim": {
        # Battery order: BC lint first (cheap deterministic), then margin/divergence
        # (post-solve), then mesh hint (advisory).  Anchor (sim_anchor) is informational
        # only and not in the exit gate.
        "check_battery": [
            CHECK_SIM_BC,
            CHECK_SIM_RESULTS,
            CHECK_SIM_MARGIN,
            CHECK_SIM_DIVERGENCE,
            CHECK_SIM_MESH,
            CHECK_SIM_ANCHOR,
        ],
        "helper_tools": ["describe_results", "run_fea_static", "run_fea_adaptive",
                         "run_fea_modal", "run_cfd_flow"],
        # Exit gate: BC lint + margin must PASS before advancing.
        # Divergence is action-level (not a blocker) so the model may acknowledge and
        # proceed; it is NOT in the exit gate per §9.3.
        "exit_gate": [CHECK_SIM_BC, CHECK_SIM_RESULTS, CHECK_SIM_MARGIN],
        "playbook_default": None,
    },
    "cam": {
        "check_battery": [CHECK_CAM_SIM],
        "helper_tools": ["cam_simulate", "cam_generate"],
        "exit_gate": [CHECK_CAM_SIM],
        "playbook_default": None,
    },
    "deliver": {
        "check_battery": [],
        "helper_tools": ["make_drawing", "make_report", "export_part"],
        "exit_gate": [],
        "playbook_default": None,
    },
    FIX_STAGE: {
        "check_battery": [CHECK_REBUILD],
        "helper_tools": ["regen_diagnose", "regen_resolve"],
        "exit_gate": [],
        "playbook_default": None,
    },
}


def check_battery(stage: str) -> list[str]:
    """Ordered list of read-only checks for *stage* (empty for stages with none)."""
    return list(_STAGE_SPEC.get(stage, {}).get("check_battery", []))


def helper_tools(stage: str) -> list[str]:
    """Helper-tool names advertised in this stage's advisories."""
    return list(_STAGE_SPEC.get(stage, {}).get("helper_tools", []))


def exit_gate(stage: str) -> list[str]:
    """Checks that must pass before the orchestrator advances past *stage*."""
    return list(_STAGE_SPEC.get(stage, {}).get("exit_gate", []))


def playbook_default(stage: str) -> Optional[str]:
    return _STAGE_SPEC.get(stage, {}).get("playbook_default")


def next_stage(stage: str) -> Optional[str]:
    """Return the linearly-following stage, or ``None`` at the end / for ``fix``."""
    try:
        i = STAGES.index(stage)
    except ValueError:
        return None
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


# ---------------------------------------------------------------------------
# Org-layer exit-gate helper (T-W22, §18)
# ---------------------------------------------------------------------------

def check_interface_change_gate(
    org_state,   # services.agent.org.OrgState — duck-typed to avoid circular import
    from_dept: str,
) -> bool:
    """Return True (BLOCKED) if *from_dept* has unacked interface_change messages.

    The foreman calls this at stage exit: if blocked, the sender's stage advance
    is gated until every recipient in the channel has acknowledged the change (§18).

    ``org_state`` is the ``OrgState`` instance for the current mission; pass
    ``None`` (or any falsy value) to skip the check — safe to call unconditionally
    even when the org layer is disabled (``PipelineFlags.org_layer=False``).
    """
    if org_state is None:
        return False
    # Duck-typed call — works with real OrgState and any compatible mock.
    fn = getattr(org_state, "has_unacked_interface_changes", None)
    if fn is None:
        return False
    return bool(fn(from_dept))
