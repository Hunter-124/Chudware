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
        # Schema vocabulary must be callable where schema failures happen. In live run
        # run-135571474839343 (2026-08-04), modeling+sketch resolved add_features but
        # not this zero-risk guide, so the model could not inspect the canonical shape.
        "cad_guide",
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
        # Explicit, not left to `family:cad`: that expansion is 118 tools trimmed to
        # whatever room the 40-cap leaves, and `get_part` sorts too late to survive.
        # It is the single most-named tool in the harness's own prose — every
        # _CONCEPT_REDIRECTS reply ends "get_part() or describe_part() first to see the
        # feature ids" — so an unreachable get_part makes the teaching errors unusable.
        # `recover_parts` is the same story on the doc-lifecycle side (the daemon-restart
        # hint names it). advertised_tool_audit, 2026-07-30.
        "get_part",
        "recover_parts",
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
        # The 3D free-space sketch discipline. cad_guide's SKETCH playbook walks the
        # whole sketch3d_new -> add_curves -> add_constraints -> preview -> pipe /
        # as_sweep_guide chain and the cad guidance notes name sketch3d_add_curves and
        # sketch3d_pipe directly, but no pack carried ANY of it: `family:cad` is capped
        # and `sketch3d_*` sorts far past the cut. A whole advertised discipline
        # unreachable, exactly like the reveng hole below. advertised_tool_audit,
        # 2026-07-30.
        "sketch3d_new",
        "sketch3d_list",
        "sketch3d_get",
        "sketch3d_clear",
        "sketch3d_add_curves",
        "sketch3d_add_constraints",
        "sketch3d_path_from_points",
        "sketch3d_ref_vertex",
        "sketch3d_sample_at",
        "sketch3d_section_at",
        "sketch3d_preview",
        "sketch3d_pipe",
        "sketch3d_as_sweep_guide",
        "sketch3d_to_ledger",
        "doc_sketch_contact_sheet",
    ],
    "measure": [
        "measure_part",
        "measure_between",
        "measure_from_photo",
        "query_geometry",
        "query_adjacency",
        "hover",
        "wall_thickness",
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
        # Named in prose, previously in no pack: doc_diff_tool is what the doc-compare
        # guidance points at, and the engine_profile pair is how a slow rebuild gets
        # diagnosed instead of guessed at.
        "doc_diff_tool",
        "engine_profile",
        "engine_profile_tree",
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
        # The closed-form tier cad_guide sends the model to BEFORE meshing anything
        # ("struct_estimate/struct_check first", bearing_life + gear_check for
        # mechanical sizing) plus the motion->FEA bridge. All advertised in the
        # playbooks, none of it in a pack. advertised_tool_audit, 2026-07-30.
        "struct_analyze",
        "struct_estimate",
        "struct_check",
        "bearing_life",
        "gear_check",
        "motion_fea_handoff",
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
        "cam_cost_estimate",  # named by the parts/quoting playbook; was in no pack
    ],
    # ECAD, fully curated: no `family:ecad` token. That expansion is 67 tools trimmed
    # ALPHABETICALLY to whatever the 40-cap leaves after explicit entries, so which half
    # of the board flow reached the model depended on where the cut happened to land —
    # and every explicit name added (the thermal/reliability trio below) evicted another
    # advertised tool off the tail. Two rounds of that cascade were watched live
    # (ecad_to_3d/ecad_update_from_sch, then ecad_sweep/ecad_thermal_handoff, then
    # ecad_set_net_class/ecad_stackup_solve) before the token came out. These 40 are
    # exactly the ecad-family tools the harness NAMES to the model somewhere in
    # services/**; the remaining 27 are internal/rarely-advertised and stay behind a
    # deliberate future entry rather than an alphabetical accident.
    # advertised_tool_audit, 2026-07-30.
    "ecad": [
        "board_from_schematic",
        "describe_board",
        "ecad_add_power",
        "ecad_add_zone",
        "ecad_airflow_handoff",
        "ecad_autoplace",
        "ecad_autoroute",
        "ecad_check_impedance",
        "ecad_design",
        "ecad_export",
        "ecad_fill_zones",
        "ecad_find_part",
        "ecad_fpga_check",
        "ecad_lint_schematic",
        "ecad_make_board",
        "ecad_make_part",
        "ecad_new_pcb",
        "ecad_new_schematic",
        "ecad_optimize_traces",
        "ecad_place_symbol",
        "ecad_route_pad_to_pad",
        "ecad_run_dfm",
        "ecad_run_drc",
        "ecad_run_erc",
        "ecad_run_hostcheck",
        "ecad_run_pdn",
        "ecad_run_sicheck",
        "ecad_set_net_class",
        "ecad_simulate",
        "ecad_stackup_solve",
        "ecad_status",
        "ecad_sweep",
        "ecad_thermal_handoff",
        "ecad_to_3d",
        "ecad_update_from_sch",
        "ecad_verify_airflow",
        "ecad_verify_thermal",
        "ecad_vibe_check",
        "ecad_wire",
        "heatsink_select",
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
        # Live-target debug. The fw_debug_* guidance notes cross-reference each other
        # ("halt first, then read_mem"), so advertising none of them left the whole
        # debug loop unreachable. advertised_tool_audit, 2026-07-30.
        "fw_debug_halt",
        "fw_debug_read_mem",
        "fw_debug_gdb_halt",
        "fw_debug_gdb_step",
        "fw_debug_gdb_read_mem",
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
    # Photo intake / first-try fit. The `intake` registry family had no pack and no
    # role loadout, so four of its eleven tools were reachable by NO route while the
    # harness advertised them constantly: `submit_part_photos` alone is named in the
    # intake_status hint ("Populate it first, in this order: 1)
    # submit_part_photos(claims=[...])"), in measure_from_photo, in fit_report, and in
    # the cad_guide INTAKE playbook — a numbered procedure whose step 1 the model
    # could not execute. Same class as the run-42508173312214 dag_run defect.
    #
    # The whole loop is listed, not just the four holes: a model that enables this
    # pack to record photo measurements needs the fit half in the same breath
    # (confirm_dimensions closes the open_demands submit_part_photos returns, and
    # fit_report is what proves the fit), and the reachable-elsewhere members cost
    # nothing to repeat.
    "intake": [
        "intake_status",
        "request_part_photos",
        "submit_part_photos",
        "measure_from_photo",
        "confirm_dimensions",
        "make_fit_coupon",
        "resolve_process",
        "set_process",
        "fit_calc",
        "fit_report",
        "recommend_fit",
        "dfm_check",
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
        # Vendor session + quoting. The parts guidance says "check vendor_status before
        # scrape_enqueue to pick an already-logged-in site" and scrape_enqueue's own note
        # points at vendor_login — an instruction the model could not carry out because
        # neither tool was in any pack. advertised_tool_audit, 2026-07-30.
        "vendor_status",
        "vendor_login",
        "request_quote",
        "list_spec_templates",
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
        # The WORKER half of the org request protocol. `request_resolve` (the
        # manager half) sits in department_manager.yaml, but no role card and no
        # pack carried `request_raise` — managers could resolve requests that
        # nothing in the harness was able to raise. Registered in the `org` family,
        # which has no family: token anywhere, so this explicit entry is its only
        # route (advertised_tool_audit, 2026-07-30).
        "request_raise",
        # Mission/ledger read surface named by the mission guidance notes
        # (mission_status for progress, replan_request for a scope change,
        # ledger_events for the raw stream) and carried by no pack.
        "mission_status",
        "replan_request",
        "ledger_events",
    ],
    # PLM / item lifecycle + the product graph. `family:plm` exists and the plm role
    # card uses it, but concrete_tools trims that role to MAX_TOOLS_PER_ROLE (14) and
    # the plm_graph_* tools are registry-tagged `cad`, so 16 tools cad_guide's PLM
    # playbook walks through were reachable by NO route. `plm_timeline` is the worst
    # of them: the undo_denied refusal — the one place an agent is TOLD how to roll a
    # document back — names plm_timeline then plm_rollback, and only the second half
    # was reachable. advertised_tool_audit, 2026-07-30.
    "plm": [
        "plm_timeline",
        "plm_checkpoint",
        "plm_rollback",
        "plm_rollback_check",
        "plm_restore",
        "plm_compare",
        "plm_release",
        "plm_item_set",
        "plm_add_remote",
        "plm_set_ai_mode",
        "plm_graph_search",
        "plm_graph_keyword",
        "plm_graph_neighbors",
        "plm_graph_links",
        "plm_graph_changes",
        "plm_graph_brief",
        "plm_graph_suggest",
        "plm_graph_watch",
        "plm_graph_configure",
        # Box.com mirror — optional cloud copy of saved project files.
        "box_sync",
        "box_status",
    ],
    # Fluid power: hydraulic circuits + piping networks. Registry-tagged `sim`, which no
    # pack reaches by family and which the sim pack lists explicitly, so all 17 tools
    # cad_guide's HYDRAULIC and PIPING playbooks name were unreachable — a numbered
    # procedure ("hydraulic_new, then size_pump/size_cylinder, then verify_piping") whose
    # every step was refused. Its own pack rather than more entries on `sim`: the sim
    # pack is already 22 explicit names under a 40-cap ceiling, and a model asking for
    # fluid power does not need the FEA/CFD job surface. advertised_tool_audit, 2026-07-30.
    "fluid": [
        "hydraulic_new",
        "hydraulic_add_component",
        "hydraulic_add_hose",
        "hydraulic_size_pump",
        "hydraulic_size_cylinder",
        "hydraulic_bom",
        "hydraulic_verify_piping",
        "hydraulic_verify_fea",
        "hydraulic_piping_handoff",
        "hydraulic_fea_handoff",
        "piping_fluid",
        "piping_solve",
        "piping_operating_point",
        "piping_internal_volume",
        "piping_report",
        "piping_verify_cfd",
        "piping_cfd_handoff",
    ],
    # Requirements + layout plan. cad_guide's LAYOUT playbook is an ordered chain
    # (layout_plan -> req_update -> req_verify_by_id -> req_report) whose middle and end
    # were unreachable while req_create/req_update were not: half a protocol.
    # advertised_tool_audit, 2026-07-30.
    "requirements": [
        "layout_plan",
        "layout_plan_report",
        "req_create",
        "req_update",
        "req_status",
        "req_report",
        "req_verify_by_id",
    ],
    # Documents. The office guidance notes chain read -> extract -> edit
    # ("office_read_pdf first, then office_edit_pdf"), and only the read half was in the
    # SHARED floor. advertised_tool_audit, 2026-07-30.
    "office": [
        "office_read_pdf",
        "office_read_docx",
        "office_extract",
        "office_edit_docx",
        "office_edit_pdf",
        "render_document",
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
    "measure": "Mass props, wall thickness, distances, geometry queries, hover",
    "inspect": "Visual inspect/render, DFM, interference, design review",
    "research": "Web search, materials, standards, eng calcs, datasheets",
    "sim": "FEA/CFD/motion and EM analysis jobs",
    "cam": "CAM jobs, ops, generate/post, feeds & speeds",
    "ecad": "Schematic/PCB place-wire-board and ERC/DRC/sim",
    "firmware": "Embedded firmware: scaffold/build/emulate, board co-verify, flash & serial",
    "assembly": "Components, mates, stackup and fit calcs",
    "intake": "Photo intake of real parts: measure, confirm dims, prove first-try fit",
    "sourcing": "Parts catalog search/place, BOM, vendors, scrape import",
    "drawing": "2D drawings, GD&T, export PDF/DXF",
    "reveng": "Reverse engineering: scan/photo/datasheet intake, reconstruct, verify deviation",
    "advisor": "Stage advisory checks and dispute",
    "delegate": "Subagents, ask_user, ledger/dag/buildgraph, mission + org coordination",
    "plm": "Item lifecycle, checkpoints/rollback, release, product-graph queries",
    "fluid": "Hydraulic circuits and piping networks: size, solve, verify",
    "requirements": "Layout plan, requirements capture and auto-verification",
    "office": "PDF/DOCX read, extract, edit and render",
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
