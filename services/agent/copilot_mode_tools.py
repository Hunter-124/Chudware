"""copilot_mode_tools.py — per-mode tool allowlists for Agent|Plan|Swarm|Edit|Ask.

Contract (mode-overhaul + shared floor):
  options.mode ∈ agent|plan|swarm|edit|ask
  Mode gates the tool set applied at agent_turn.run_turn (intersected with any
  role-card allowlist the caller already computed).

Policies:
  * ask/plan/edit — restricted mode lists UNION SHARED_DEFAULT_TOOLS (doc
    lifecycle, web, advisor, ask_user, baseline read/help) plus meta-tools
    enable_tools / list_tool_categories so the model can pull more categories.
  * plan  — agent-broad tool access MINUS geometry commit mutators; shared
            defaults (incl. make/save/open/close) stay so plan can build.
  * edit  — doc-scoped mutating tools; shared defaults still include new_part
            / close / import so edit can finish a simple part.
  * agent — full (None = unrestricted at mode layer; role card still applies)
  * swarm — full + explicit corporate/delegate tools kept in the set

Subagents (``delegate``) stay available in agent/swarm/edit/plan. Ask also
gets shared defaults (doc lifecycle + advise + web) so it never dead-ends.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

# Canonical mode vocabulary (frozen with host/core).
COPILOT_MODES = frozenset({"agent", "plan", "swarm", "edit", "ask"})
DEFAULT_MODE = "agent"

# Always-available floor across restricted modes so the agent can get work done,
# talk to the user, reach the advisor, search the web, and manage docs.
SHARED_DEFAULT_TOOLS: frozenset[str] = frozenset({
    # Doc lifecycle (make / save / open / close)
    "new_part",
    "save_part",
    "close_part",
    "import_part",
    "export_part",
    # Web + advisor + interaction
    "research_web_search",
    "advise",
    "ask_user",
    # Baseline read / help
    "cad_guide",
    "list_parts",
    "describe_part",
    "measure_part",
    # Meta: request more tool categories mid-turn
    "enable_tools",
    "list_tool_categories",
})

# ---------------------------------------------------------------------------
# Allowlists (None = no mode-level restriction)
# ---------------------------------------------------------------------------

# Ask: pure read / research. No mutation, no mission, no new docs.
ASK_TOOLS: frozenset[str] = frozenset({
    "cad_guide",
    "describe_part",
    "describe_assembly",
    "describe_sketch",
    "describe_scene",
    "describe_board",
    "describe_results",
    "describe_toolpaths",
    "describe_project",
    "measure_part",
    "measure_between",
    "measure_from_photo",
    "query_geometry",
    "query_adjacency",
    "hover",
    "list_parts",
    "list_world_bodies",
    "get_part",
    "research_web_search",
    "research_fetch_page",
    "research_material_lookup",
    "research_standard_section",
    "research_thread_spec",
    "research_unit_convert",
    "research_eng_calc",
    "research_datasheet_fetch",
    "list_calcs",
    "list_standard_parts",
    "part_search",
    "part_get",
    "parts_taxonomy",
})

# Plan: same breadth as agent for research/sim/inspect/sourcing/delegate, but
# NEVER geometry/document commit mutators. Used when role_tools is empty (a
# concrete list is required because None cannot express a denylist). When a
# role card is present, apply_mode_tools subtracts mutators from that card
# instead of intersecting with this set — callers that must stay broad should
# pass role_tools=None for plan.
PLAN_TOOLS: frozenset[str] = frozenset({
    *ASK_TOOLS,
    # Plan / ledger / status
    "buildgraph_plan",
    "buildgraph_status",
    "dag_plan",
    "dag_status",
    "ledger_get",
    "ledger_note",
    "ledger_events",
    "layout_plan",
    "layout_plan_report",
    "preflight_ops",
    "ask_user",
    "delegate",
    "workspace_status",
    "intake_status",
    "suggest_dimension_scheme",
    "advise",
    # Describe / inspect / look
    "inspect_part",
    "inspect_feature",
    "inspect_section",
    "look_around",
    "contact_sheet",
    "render_part",
    "render_views",
    "render_sketch",
    "render_ascii",
    "render_sketch_ascii",
    "wall_thickness",
    "draft_analysis",
    # Checks / compare / fit
    "check_sketch",
    "check_sketch_d",
    "check_interference",
    "check_symmetry",
    "check_feature_overlap",
    "check_metadata",
    "interference_check",
    "interface_check",
    "compare_parts",
    "stackup_analyze",
    "fit_calc",
    "fit_report",
    "recommend_fit",
    "gear_check",
    "bearing_life",
    "tap_drill",
    "feeds_speeds",
    "sketch_health",
    "sketch_preview_text",
    # Sourcing / BOM
    "part_source",
    "bom_rollup",
    "bom_price",
    # Design review
    "design_review",
    "dfm_check",
    "verify_battery",
    # Sim read/run (analysis is non-commit)
    "run_fea_static",
    "run_fea_modal",
    "run_fea_frequency_response",
    "run_fea_fatigue",
    "run_fea_thermal",
    "run_fea_buckling",
    "run_fea_contact",
    "run_cfd_flow",
    "run_cfd_accurate",
    "get_job",
    "simulate_motion",
    # Corporate / coordinate read helpers
    "corporate_channel_read",
    "corporate_chart",
    "coordinate_board",
    "coordinate_progress",
    "coordinate_claim",
    "coordinate_release",
})

# Edit: mutate current doc; no new_part / mission / corporate activate.
EDIT_TOOLS: frozenset[str] = frozenset({
    *ASK_TOOLS,
    "add_features",
    "edit_feature",
    "undo",
    "redo",
    "save_part",
    "export_part",
    "set_material",
    "set_process",
    "set_config_override",
    "set_config_suppression",
    "list_configurations",
    "activate_configuration",
    "add_configuration",
    "check_feature_overlap",
    "check_interference",
    "check_symmetry",
    "wall_thickness",
    "dfm_check",
    "draft_analysis",
    "verify_battery",
    "design_review",
    "inspect_part",
    "inspect_feature",
    "inspect_section",
    "render_part",
    "render_views",
    "contact_sheet",
    "look_around",
    "convert_face_to_sketch",
    "project_into_sketch",
    "sketch_auto_constrain",
    "preflight_ops",
    "ledger_get",
    "ledger_note",
    "advise",
    # Subagents stay available outside pure Swarm, but edit stays doc-local —
    # no corporate_activate / mission spawn. delegate remains for scoped help.
    "delegate",
})

# Explicit denylists used when intersecting open-ended role cards.
_MUTATING_TOOLS: frozenset[str] = frozenset({
    "add_features",
    "edit_feature",
    "new_part",
    "new_assembly",
    "close_part",
    "import_part",
    "import_artifact",
    "asm_add_component",
    "asm_mate",
    "solve_mates",
    "cam_generate",
    "cam_post",
    "cam_audit",
    "cam_add_operation",
    "cam_add_setup",
    "cam_add_tool",
    "ecad_place_symbol",
    "ecad_wire",
    "ecad_new_schematic",
    "ecad_new_pcb",
    "board_from_schematic",
    "fw_new_project",
    "fw_build",
    "fw_flash",
})

_EDIT_DENIED: frozenset[str] = frozenset({
    "new_part",
    "new_assembly",
    "close_part",
    "import_part",
    "import_artifact",
    "corporate_activate",
    "corporate_spawn_worker",
    "dag_run",
    "dag_replan",
    "mission_start",
    "scrape_enqueue",
})

# Swarm extras always kept when mode is swarm (unioned onto an open allowlist).
SWARM_EXTRA_TOOLS: frozenset[str] = frozenset({
    "delegate",
    "corporate_activate",
    "corporate_channel_post",
    "corporate_channel_read",
    "corporate_chart",
    "corporate_spawn_worker",
    "corporate_hold_meeting",
    "corporate_record_completion",
    "coordinate_claim",
    "coordinate_release",
    "coordinate_progress",
    "coordinate_board",
    "dag_plan",
    "dag_status",
    "ask_user",
    "ledger_get",
    "ledger_note",
})


def normalize_mode(mode: str | None) -> str:
    """Return a canonical mode string (default ``agent``)."""
    m = (mode or DEFAULT_MODE).strip().lower()
    return m if m in COPILOT_MODES else DEFAULT_MODE


def tools_for_mode(mode: str | None) -> Optional[frozenset[str]]:
    """Return the mode allowlist, or None when the mode is unrestricted.

    ``agent`` and ``swarm`` return None (full catalog at the mode layer). Callers
    that need swarm extras should use :func:`apply_mode_tools` which unions
    ``SWARM_EXTRA_TOOLS`` onto any existing allowlist.
    """
    m = normalize_mode(mode)
    if m == "ask":
        return ASK_TOOLS
    if m == "plan":
        return PLAN_TOOLS
    if m == "edit":
        return EDIT_TOOLS
    # agent / swarm — unrestricted at mode layer
    return None


def apply_mode_tools(
    mode: str | None,
    role_tools: Iterable[str] | None = None,
    *,
    plan_only: bool | None = None,
) -> Optional[list[str]]:
    """Intersect *role_tools* with the mode allowlist, then floor with shared defaults.

    Returns:
      * None  — no restriction (agent mode with no role card, or empty inputs
        that mean "full").
      * list  — concrete allowed tool names (sorted for stability in tests).

    Rules:
      * ``plan_only=True`` forces plan semantics even if mode is agent.
      * plan = agent-broad minus geometry commit mutators, then UNION
        :data:`SHARED_DEFAULT_TOOLS` (doc lifecycle stays so plan can build).
      * ask / edit: mode list ∩ role (or mode list), mode denylists applied,
        then UNION shared defaults (shared wins over denylists for the floor).
      * When *role_tools* is None/empty and mode is open → None (unrestricted).
      * swarm unions SWARM_EXTRA_TOOLS onto the role set (or leaves unrestricted).
    """
    m = normalize_mode(mode)
    if plan_only:
        m = "plan"

    mode_set = tools_for_mode(m)
    role_list = [str(t) for t in (role_tools or []) if t]
    role_set = set(role_list)

    if m == "swarm":
        if not role_set:
            # Unrestricted + ensure corporate/delegate names are known to callers
            # that materialize from this helper for prompts.
            return None
        out = sorted(role_set | SWARM_EXTRA_TOOLS)
        return out

    if m == "agent":
        if not role_set:
            return None
        return sorted(role_set)

    # Plan: open like agent minus geometry commit mutators (add_features etc.),
    # then floor with SHARED_DEFAULT_TOOLS so make/save/open/close stay.
    if m == "plan":
        _plan_deny = _MUTATING_TOOLS | {"new_assembly", "edit_feature"}
        # new_part / close_part / import_part live in SHARED_DEFAULT_TOOLS and
        # are re-added below (product: plan can build simple parts).
        if role_set:
            allowed = role_set - _plan_deny
        else:
            allowed = set(PLAN_TOOLS) - _plan_deny
        allowed |= SHARED_DEFAULT_TOOLS
        return sorted(allowed)

    # Restricted modes: ask / edit
    assert mode_set is not None
    if not role_set:
        allowed = set(mode_set)
    else:
        allowed = role_set & mode_set

    if m == "edit":
        allowed -= _EDIT_DENIED
    if m == "ask":
        allowed -= _MUTATING_TOOLS

    # Shared floor always wins for restricted modes (doc lifecycle + advise +
    # web + meta enable_tools / list_tool_categories).
    allowed |= SHARED_DEFAULT_TOOLS
    return sorted(allowed)


def is_tool_allowed(
    tool: str,
    mode: str | None,
    *,
    role_tools: Iterable[str] | None = None,
    plan_only: bool | None = None,
) -> bool:
    """True when *tool* may run under *mode* (+ optional role intersection)."""
    name = (tool or "").strip()
    if not name:
        return False
    allowed = apply_mode_tools(mode, role_tools, plan_only=plan_only)
    if allowed is None:
        return True
    return name in allowed


def mode_options_from_mapping(options: Any | None) -> dict[str, Any]:
    """Normalize a host/core ``options`` mapping into mode-related fields.

    Keys (all optional):
      mode, plan_only, approved_plan, execute_as, corporate_force, scope, advisor
    """
    if not isinstance(options, dict):
        options = {}
    mode = normalize_mode(options.get("mode"))
    plan_only = bool(options.get("plan_only", False))
    if mode == "plan" and "plan_only" not in options:
        plan_only = True
    execute_as = str(options.get("execute_as") or "").strip().lower()
    if execute_as not in ("agent", "swarm", ""):
        execute_as = ""
    corporate_force = bool(options.get("corporate_force", False))
    if mode == "swarm":
        corporate_force = True
    advisor = options.get("advisor")
    if advisor is None:
        advisor = True
    else:
        advisor = bool(advisor)
    scope = options.get("scope")
    if scope is not None and not isinstance(scope, dict):
        scope = None
    approved_plan = options.get("approved_plan")
    if approved_plan is not None and not isinstance(approved_plan, str):
        approved_plan = str(approved_plan)
    return {
        "mode": mode,
        "plan_only": plan_only,
        "approved_plan": approved_plan,
        "execute_as": execute_as or None,
        "corporate_force": corporate_force,
        "scope": scope,
        "advisor": advisor,
    }
