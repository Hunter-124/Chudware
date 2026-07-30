"""tool_categories.py — model-requestable tool category packs (anti-context-flood).

Restricted modes (ask/plan/edit) start with a shared floor of tools. The model can
call ``enable_tools(categories=[...])`` mid-turn to pull additional concrete tools
without stuffing the full catalog into the prompt. Category packs map a short name
to concrete tool names and/or ``family:X`` tokens (resolved via tool_registry).
"""
from __future__ import annotations

from typing import Iterable

# Soft cap on tools expanded from a single enable_tools call (and overall expansion).
CATEGORY_EXPAND_CAP = 40

# Meta-tools the model uses to request more surface area. Always allowed.
META_TOOLS: frozenset[str] = frozenset({
    "enable_tools",
    "list_tool_categories",
    "design_recipe",
})

# Logical packs → concrete names and/or family:X tokens. Pure data; family tokens
# resolve lazily against tool_registry.catalog() so this module stays import-cycle free.
CATEGORY_PACKS: dict[str, list[str]] = {
    "modeling": [
        "family:cad",
        "add_features",
        "new_part",
        "new_assembly",
        "save_part",
        "close_part",
        "import_part",
        "export_part",
        "undo",
        "redo",
        "set_material",
        "set_process",
        "make_standard_part",
        "preflight_ops",
    ],
    "sketch": [
        "family:gsl",
        "describe_sketch",
        "check_sketch",
        "sketch_health",
        "sketch_auto_constrain",
        "sketch_preview_text",
        "render_sketch",
        "render_sketch_ascii",
        "convert_face_to_sketch",
        "project_into_sketch",
        "suggest_dimension_scheme",
    ],
    "measure": [
        "measure_part",
        "measure_between",
        "measure_from_photo",
        "query_geometry",
        "query_adjacency",
        "hover",
    ],
    "inspect": [
        "inspect_part",
        "inspect_feature",
        "inspect_section",
        "look_around",
        "contact_sheet",
        "render_part",
        "render_views",
        "render_ascii",
        "wall_thickness",
        "draft_analysis",
        "check_interference",
        "check_symmetry",
        "check_feature_overlap",
        "check_metadata",
        "interface_check",
        "compare_parts",
        "design_review",
        "dfm_check",
        "verify_battery",
    ],
    "research": [
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
        "cad_guide",
    ],
    "sim": [
        "run_fea_static",
        "run_fea_modal",
        "run_fea_frequency_response",
        "run_fea_fatigue",
        "run_fea_thermal",
        "run_fea_buckling",
        "run_fea_contact",
        "run_cfd_flow",
        "run_cfd_accurate",
        "simulate_motion",
        "get_job",
        "describe_results",
        # Background solve lifecycle (2026-07-29). The synchronous run_* calls above block for
        # minutes and a live turn hit max_iters waiting on one; these let the model start a solve,
        # keep working, and collect it. Registered but unreachable until listed here — a tool the
        # dispatcher can bind is still invisible if no pack advertises it.
        "sim_run",
        "sim_status",
        "sim_cancel",
        "sim_results",
        "sim_jobs",
        "family:em",
    ],
    "cam": [
        "family:cam",
        "cam_new_job",
        "cam_job_tree",
        "cam_add_tool",
        "cam_add_setup",
        "cam_add_operation",
        "cam_generate",
        "cam_simulate",
        "cam_post",
        "cam_setup_sheet",
        "cam_tool_import",
        "feeds_speeds",
        "cam_audit",
        "cam_job_diff",
        "describe_toolpaths",
    ],
    "ecad": [
        "family:ecad",
        "ecad_new_schematic",
        "ecad_new_pcb",
        "ecad_place_symbol",
        "ecad_wire",
        "ecad_find_part",
        # Explicit, not left to `family:ecad` — that expansion is alphabetically trimmed at
        # CATEGORY_EXPAND_CAP (40) out of 66 ecad-family tools, so this name survives only by
        # sorting early. A curated entry keeps the pre-ERC geometry check reachable as more
        # ecad_a*/ecad_c* tools land (flagged by EcadSchematicQA, 2026-07-29).
        "ecad_lint_schematic",
        "ecad_status",
        "ecad_run_erc",
        "ecad_run_drc",
        "ecad_simulate",
        "board_from_schematic",
        "describe_board",
    ],
    "firmware": [
        "fw_status",
        "fw_new_project",
        "fw_sources",
        "fw_build",
        "fw_sim_run",
        "fw_sensor_scenario",
        "fw_verify_board",
        "fw_power_profile",
        "fw_flash",
        "fw_emulate_renode",
        "fw_emulate_simavr",
        "fw_avr_flash",
        "fw_serial_open",
        "fw_serial_read",
        "fw_serial_write",
        "fw_serial_close",
    ],
    "assembly": [
        "new_assembly",
        "asm_add_component",
        "asm_mate",
        "solve_mates",
        "describe_assembly",
        "check_interference",
        "stackup_analyze",
        "fit_calc",
        "fit_report",
        "recommend_fit",
    ],
    "sourcing": [
        "part_search",
        "part_get",
        "part_source",
        "parts_taxonomy",
        "match_model",
        "bom_rollup",
        "bom_price",
        "vendors_health",
        "scrape_enqueue",
        "scrape_queue_status",
        "import_artifact",
        "part_place",
    ],
    "drawing": [
        "make_drawing",
        "export_drawing",
        "review_drawing",
        "add_drawing_view",
        "add_section_view",
        "add_detail_view",
        "add_dimension",
        "add_gdt",
        "add_datum",
        "add_surface_finish",
        "set_view",
        "set_titleblock",
    ],
    # Reverse engineering. There was NO reveng pack at all until 2026-07-28, so
    # the general_agent lane — the universal entry point for every request — could
    # not reach this discipline by any route. A live reveng sweep watched the model
    # try reveng_start_session, reveng_status and reverse_engineer and be told
    # "not in allowed list" for all three: a whole advertised discipline
    # unreachable, not merely awkward.
    #
    # Explicit names, no family: token, because the registered names are NOT all
    # `reveng_`-prefixed — the session/query half is verb-first
    # (list_reveng_sessions, get_reveng_coverage, describe_reveng_result,
    # import_scan_mesh, export_reveng_bundle). Every name below was verified to
    # bind via agent_turn._bind_tool; the `reveng_`-prefixed spellings of the
    # verb-first tools do NOT exist, so do not "tidy" these into a prefix.
    "reveng": [
        # Session lifecycle + evidence intake.
        "reveng_status",
        "reveng_start_session",
        "reveng_add_images",
        "reveng_add_video",
        "reveng_ingest_datasheet",
        "reveng_set_scale",
        "reveng_print_scale_mat",
        "list_reveng_sessions",
        "get_reveng_session",
        # Reconstruction gate + build.
        "reveng_gate_status",
        "reveng_resolve_gate",
        "reveng_reconstruct",
        "reveng_single_photo",
        "reveng_fit_primitives",
        "reveng_build_parametric",
        "reveng_attach_result",
        "reveng_make_part_from_library",
        "reveng_research_source",
        # Results, verification, export.
        "get_reveng_coverage",
        "get_reveng_deviation",
        "describe_reveng_result",
        "list_reveng_dims",
        "export_reveng_bundle",
        "import_scan_mesh",
        # The recon-family entry path + the geometry checks an RE job needs.
        "reverse_engineer",
        "compare_parts",
        "convert_face_to_sketch",
        "project_into_sketch",
        "start_capture_session",
        "stop_capture_session",
        "import_part",
    ],
    "advisor": [
        "family:advisor",
        "advise",
        "stage_set",
        "advisory_dispute",
    ],
    "delegate": [
        "delegate",
        "ask_user",
        "dag_plan",
        "dag_status",
        # dag_run + its gate verbs. `mission_start`'s own success hint says
        # "execute with dag_run(graph_json=<graph>)", and `dag_run`'s note says
        # an `interrupted` result means dag_approve/dag_abort — but NO pack
        # carried any of the three, and the general lane's base loadout has
        # none either, so the whole mission path dead-ended at a compiled graph
        # nobody could execute. All three bind fine (`_bind_tool` resolves
        # them); this was purely a missing advertisement. Live 2026-07-29 (case
        # bench): "The tool note says the graph compiles but does not execute -
        # I need dag_run to execute it, which isn't in my current loadout."
        "dag_run",
        "dag_approve",
        "dag_abort",
        "ledger_get",
        "ledger_note",
        "buildgraph_plan",
        "buildgraph_status",
        # Corporate/coordination surface. These are NOT MCP-registered tools —
        # they are MissionDispatch bound methods resolved through
        # MissionDispatch.resolve (mission_dispatch.py `_handlers`), which is a
        # dispatch layer agent_turn._bind_tool never consults. A `_bind_tool`
        # probe therefore reports them unbindable and they are NOT: on any lane
        # that binds a dispatcher (including the general lane, which
        # run_linear_role_turn always gives one — mission_dispatch.py:2189-2210)
        # they resolve and run. They were briefly deleted from this pack on that
        # false-negative reading; if you are auditing pack entries, probe
        # MissionDispatch.resolve AND _bind_tool before calling anything dead.
        "corporate_channel_read",
        "corporate_chart",
        "coordinate_board",
        "coordinate_progress",
    ],
    "sheetmetal": [
        "add_features",  # sm_base_flange / sm_edge_flange / sm_flat_pattern via ops
        "set_process",
        "research_eng_calc",  # bend_allowance
        "dfm_check",
        "wall_thickness",
        "measure_part",
        "export_part",
    ],
}

_CATEGORY_DESCS: dict[str, str] = {
    "modeling": "Part/assembly geometry: add_features ops, new/save/export docs",
    "sketch": "2D sketch health, constraints, project/convert faces",
    "measure": "Mass props, distances, geometry queries, hover",
    "inspect": "Visual inspect/render, DFM, interference, design review",
    "research": "Web search, materials, standards, eng calcs, datasheets",
    "sim": "FEA/CFD/motion and EM analysis jobs",
    "cam": "CAM jobs, ops, generate/post, feeds & speeds",
    "ecad": "Schematic/PCB place-wire-board and ERC/DRC/sim",
    "firmware": "Embedded firmware: scaffold/build/emulate, board co-verify, flash & serial",
    "assembly": "Components, mates, stackup and fit calcs",
    "sourcing": "Parts catalog search/place, BOM, vendors, scrape import",
    "drawing": "2D drawings, GD&T, export PDF/DXF",
    "reveng": "Reverse engineering: scan/photo/datasheet intake, reconstruct, verify deviation",
    "advisor": "Stage advisory checks and dispute",
    "delegate": "Subagents, ask_user, ledger/dag/buildgraph",
    "sheetmetal": "Sheet-metal flanges/bends via add_features + bend calcs",
}


def category_catalog() -> list[tuple[str, str]]:
    """Return ``[(name, one_line_desc), ...]`` sorted by name for the prompt menu."""
    out: list[tuple[str, str]] = []
    for name in sorted(CATEGORY_PACKS.keys()):
        out.append((name, _CATEGORY_DESCS.get(name, f"{name} tools")))
    return out


def _expand_token(token: str) -> set[str]:
    """Expand one pack entry (concrete name or ``family:X``) to concrete names."""
    t = (token or "").strip()
    if not t:
        return set()
    if t.startswith("family:"):
        fam = t[len("family:"):].strip()
        if not fam:
            return set()
        try:
            from services.mcp import tool_registry as _tr  # noqa: PLC0415 — lazy
            _tr.warm_catalog()
            cat = _tr.catalog() or {}
        except Exception:  # noqa: BLE001 — catalog optional at import/test time
            return set()
        return {
            name for name, tags in cat.items()
            if isinstance(tags, dict) and tags.get("family") == fam
        }
    return {t}


def resolve_categories(
    names: Iterable[str] | None,
    *,
    cap: int | None = None,
) -> set[str]:
    """Expand category names to concrete tool names (deduped, capped).

    Unknown category names are ignored. ``family:X`` tokens inside packs expand
    against the live tool_registry catalog when available. Cap defaults to
    :data:`CATEGORY_EXPAND_CAP`.

    Explicit pack entries are CURATED and never trimmed: the cap applies only
    to ``family:X`` expansion results. Live defect (2026-07-27): the modeling
    pack leads with ``family:cad`` (112 tools), so the old alphabetical 40-cap
    trim silently dropped ``new_part``/``new_assembly``/``save_part``/
    ``set_material``/``preflight_ops`` — models enabled ``modeling`` and then
    could not call ``new_part``, going straight to ``add_features`` on whatever
    doc happened to be active.
    """
    limit = CATEGORY_EXPAND_CAP if cap is None else max(0, int(cap))
    explicit: set[str] = set()
    expanded: set[str] = set()
    for raw in names or []:
        key = str(raw or "").strip().lower()
        if not key:
            continue
        pack = CATEGORY_PACKS.get(key)
        if not pack:
            continue
        for token in pack:
            t = (token or "").strip()
            if t.startswith("family:"):
                expanded |= _expand_token(t)
            else:
                explicit |= _expand_token(t)
    # Explicit entries are curated and take PRIORITY; family expansion fills
    # whatever room is left. The cap stays a HARD ceiling either way.
    kept_explicit = sorted(explicit)[:limit]
    room = max(0, limit - len(kept_explicit))
    kept_family = sorted(expanded.difference(kept_explicit))[:room]
    return set(kept_explicit) | set(kept_family)
