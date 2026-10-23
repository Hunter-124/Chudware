"""tool_categories.py — the model-requestable tool surface (anti-context-flood).

Two layers:

* :data:`CATEGORY_PACKS` — curated packs used by DAG-node / sub-agent roles
  (``enable_tools(categories=[...])`` from a capped role card).
* The main-chat surface — :data:`MAIN_CHAT_CORE_TOOLS` (always loaded) plus the
  live, grouped, name-level menu of EVERYTHING else (:func:`render_tool_menu`),
  resolvable by group or by exact name through :func:`resolve_tool_requests`.

Family tokens resolve lazily against ``tool_registry.catalog()`` so this module
stays import-cycle free.
"""
from __future__ import annotations

from typing import Iterable

# Soft cap on tools expanded from a single enable_tools call (and overall expansion).
CATEGORY_EXPAND_CAP = 64

# Human history APIs remain registered, but agent writers are always refused.
# Keep them out of every agent discovery/loading path, including family expansion.
AGENT_DENIED_TOOLS: frozenset[str] = frozenset({"undo", "redo"})

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
        "inspect_native_files",
        "read_native_file",
        "browse_models",
        "relocate_native_files",
        "set_material",
        "set_process",
        "make_standard_part",
        "preflight_ops",
        # Explicit, not left to `family:cad`: that expansion is 118 tools trimmed to
        # whatever room the category cap leaves, and `get_part` sorts too late to survive.
        # It is the single most-named tool in the harness's own prose — every
        # _CONCEPT_REDIRECTS reply ends "get_part() or describe_part() first to see the
        # feature ids" — so an unreachable get_part makes the teaching errors unusable.
        # `recover_parts` is the same story on the doc-lifecycle side (the daemon-restart
        # hint names it). advertised_tool_audit, 2026-07-30.
        "get_part",
        "recover_parts",
        # Displaced from the capped `family:cad` expansion when the 2026-08-26
        # drawing-overhaul tools registered (family:cad is 40-cap trimmed, and
        # 16 new cad-family names pushed these past the cut). Same crowding
        # class as get_part above. advertised_tool_audit, 2026-08-26.
        "code_check",
    ],
    "sketch": [
        "family:gsl",
        "describe_sketch",
        "check_sketch",
        "sketch_health",
        "sketch_auto_constrain",
        "sketch_fully_define",
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
        "doc_sketch_contact_sheet",
        # Displaced from the capped `family:cad` expansion by the 2026-08-26
        # drawing-overhaul registrations — see the modeling pack note.
        "check_sketch3d",
    ],
    "measure": [
        "measure_part",
        "measure_between",
        "fit_cylinder_axis",
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
        "inspect_model_integrity",
        "check_symmetry",
        "check_feature_overlap",
        "check_metadata",
        "interface_check",
        "compare_parts",
        "design_review",
        "dfm_check",
        "verify_battery",
        # Displaced from the capped `family:cad` expansion by the 2026-08-26
        # drawing-overhaul registrations — see the modeling pack note.
        "check_geometry_health",
        # The repair half of the health pair: check_geometry_health diagnoses and refuses
        # to fix, heal_body records the ShapeFix repair as a feature. Same pack, because a
        # model that just got an unsound verdict needs the fix in the same breath.
        "heal_body",
        # Named in prose, previously in no pack: doc_diff_tool is what the doc-compare
        # guidance points at, and the engine_profile pair is how a slow rebuild gets
        # diagnosed instead of guessed at.
        "doc_diff_tool",
        "engine_profile",
        "engine_profile_tree",
        # The render family's moving half (2026-09-05): a turntable/explode/motion/fea
        # film is a review artifact, so it belongs beside render_part/render_views.
        "render_animation",
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
        # Machining reference (2026-08-26): curated cited handbook-class tables
        # plus the user-supplied-PDF page index.
        "machining_ref_search",
        "machining_ref_get",
        "machining_ref_index_pdf",
        "machining_ref_search_pdf",
    ],
    # Manufacturing planning (2026-08-26): the typed plan scratchpad every
    # machined/fabricated deliverable is planned in before modeling. Kept as
    # its own requestable pack so any lane can enable_tools(['mfgplan']).
    "mfgplan": [
        "mfg_plan_upsert",
        "mfg_plan_get",
        "mfg_plan_check",
        "mfg_plan_render",
        "resolve_process",
        "set_process",
        "dfm_check",
        "fit_calc",
        "recommend_fit",
        "machining_ref_search",
        "tap_drill",
        "feeds_speeds",
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
        "wait_job",
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
        "topology_capabilities",
        "run_topology_optimization",
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
        "motion_fea_handoff_sweep",
        # Release evidence (2026-09-04): the three-mesh convergence ladder the runbook
        # requires instead of a single-mesh peak, the packet that ships inputs/result/
        # convergence/hashes/provenance/checklist as one directory, and the independent
        # Polyhedral-FEA CLI cross-check lane. Advertised here only — the main-chat core
        # stays as-is, since ordinary part work does not release an FEA case every turn.
        "run_fea_convergence",
        "sim_evidence_packet",
        # verify_packet is the read-back: a packet folder can only be cited once its
        # files are proven to describe the same run (2026-09-06).
        "verify_packet",
        "polymesh_cli",
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
    # Additive CAM: the only supported path from a part to printer G-code (stock
    # PrusaSlicer, vendor presets) plus the deterministic checks around it.
    "print": [
        "dfm_check",
        "optimize_print_orientation",
        "slice_part",
        "verify_gcode",
        "list_print_profiles",
        "slicer_status",
        # Printer media delivery: read-only inventory first, then the managed publish
        # (raw root-level files or one project folder, readback-verified, old managed G-code
        # deleted after verified commit). Mutating publish is denied in Ask/Plan by the contract.
        "print_media_inventory",
        "publish_print_media",
        "wall_thickness",
        "check_geometry_health",
        "export_part",
    ],
    # ECAD is fully curated: no `family:ecad` token. The explicit pack is deliberately
    # comprehensive because this discipline spans schematic authoring/readback/export,
    # board layout, SI/PI, thermal, power, and antennas. Keep it under the 64-tool category
    # cap so no promised tool is silently trimmed.
    "ecad": [
        "apply_ascii_edits",
        "board_from_schematic",
        "describe_board",
        "ecad_add_power",
        "ecad_add_zone",
        "ecad_airflow_handoff",
        "ecad_annotate",
        "ecad_antenna_analyze",
        "ecad_antenna_design",
        "ecad_autoplace",
        "ecad_autoroute",
        "ecad_bom",
        "ecad_bus",
        "ecad_bus_entry",
        "ecad_check_diff_impedance",
        "ecad_check_impedance",
        "ecad_delete",
        "ecad_design",
        "ecad_edit_symbol",
        "ecad_export",
        "ecad_export_kicad",
        "ecad_fill_zones",
        "ecad_find_part",
        "ecad_fpga_check",
        "ecad_import_kicad",
        "ecad_junction",
        "ecad_label",
        "ecad_load_symbol_library",
        "ecad_lint_schematic",
        "ecad_make_board",
        "ecad_make_part",
        "ecad_netlist",
        "ecad_new_pcb",
        "ecad_new_schematic",
        "ecad_no_connect",
        "ecad_note",
        "ecad_optimize_traces",
        "ecad_place_symbol",
        "ecad_plot",
        "ecad_route_diff_pair",
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
        "ecad_symbols",
        "ecad_tf",
        "ecad_thermal_handoff",
        "ecad_to_3d",
        "ecad_update_from_sch",
        "ecad_verify_airflow",
        "ecad_verify_thermal",
        "ecad_vibe_check",
        "ecad_wire",
        "heatsink_select",
        "power_budget",
        "render_schematic_ascii",
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
        "inspect_model_integrity",
        "fit_cylinder_axis",
        "stackup_analyze",
        "fit_calc",
        "fit_report",
        "recommend_fit",
        # Mechanism evidence over a mated assembly (cad_motion_tools): the swept
        # envelope/solid and the closed-form Ackermann turning radius. Both compose
        # simulate_motion, so they belong wherever the assembly itself is reachable.
        "sweep_envelope",
        "steering_turning_radius",
        "motion_fea_handoff_sweep",
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
        "browser_profiles",
        "request_quote",
        "list_spec_templates",
    ],
    "drawing": [
        "make_drawing",
        "export_drawing",
        "review_drawing",
        "audit_drawing",
        "add_drawing_view",
        "add_section_view",
        "add_detail_view",
        "add_dimension",
        "add_hole_callout",
        "add_chamfer_dimension",
        "add_ordinate_dimensions",
        "add_gdt",
        "add_datum",
        "add_datum_target",
        "add_surface_finish",
        "add_weld_symbol",
        "add_balloon",
        "drawing_bom",
        "add_drawing_sheet",
        "arrange_drawing",
        "list_drawing_sheets",
        "set_active_drawing_sheet",
        "add_drawing_table",
        "edit_drawing_annotation",
        "set_view",
        "set_titleblock",
        "suggest_drawing_dimension_scheme",
    ],
    # Shop documentation writers (2026-08-26): reports, work instructions,
    # weld travelers, office conversion. Kept separate from "drawing" so a
    # drafter lane can request sheet tools without the writer family and
    # vice versa.
    "docs": [
        "make_report",
        "make_presentation",
        "make_workbook",
        "make_work_instruction",
        "make_weldment_traveler",
        "render_document",
        "list_artifacts",
        "office_convert",
        "cam_setup_sheet",
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
        # Local Gaussian capture, training, inspection, and object isolation.
        "reveng_start_splat",
        "reveng_splat_status",
        "reveng_finish_splat",
        "reveng_cancel_splat",
        "reveng_crop_splat",
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
        # Team sync + advisory locks and live co-editing. Registering
        # a remote with plm_add_remote was reachable while every tool that remote
        # exists to enable was not, so the model could configure sharing and then
        # never sync, lock, or join it — the same advertised-but-uncallable dead end
        # this pack's note above documents. advertised_tool_audit, 2026-08-27.
        "plm_sync",
        "plm_lock",
        "plm_unlock",
        "plm_live_join",
        "plm_live_propose",
        "plm_live_poll",
        "plm_live_presence",
        "plm_live_leave",
        "plm_set_ai_mode",
        "plm_graph_search",
        "plm_graph_keyword",
        "plm_graph_neighbors",
        "plm_graph_links",
        # Displaced from the capped `family:cad` expansion by the 2026-08-26
        # drawing-overhaul registrations — see the modeling pack note.
        "cad_graph",
        "cad_graph_link",
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
    # pack already carries a broad FEA/CFD job surface, and a model asking for fluid
    # power does not need it. advertised_tool_audit, 2026-07-30.
    "fluid": [
        "hydraulic_new",
        "hydraulic_templates",
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
        # Piping design + industrial sizing (2026-10-05): orthogonal obstacle-avoiding
        # routing, ASME B31.3 spec check, BOM/spools, persisted-network reload, VIII-1
        # vessel and equal-friction duct sizing, and native internal-flow CFD over a pipe
        # bore. cad_guide('piping') / cad_guide('industrial') and the piping-design skill
        # name them as one workflow, so they ride the same pack as piping_solve.
        "piping_route",
        "piping_edit",
        "piping_fitting_k",
        "piping_fitting_k_cfd",
        "piping_spec_check",
        "piping_bom",
        "piping_load",
        "vessel_size",
        "duct_size",
        "run_pipe_flow",
        # Fluid power design + simulation (2026-10-05): transient circuit simulation under a
        # valve schedule, hydraulic-motor sizing, electric pump-drive sizing (hydraulic or a
        # piping network's centrifugal pump), and persisted-circuit reload. cad_guide
        # ('hydraulic') and the hydraulic-design skill name them in one workflow with the
        # circuit-building tools above, so they ride the same pack.
        "hydraulic_simulate",
        "hydraulic_size_motor",
        "pump_drive_size",
        "hydraulic_load",
        "hydraulic_layout_3d",
        "piping_pump_drive",
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
    # Civil / site / utility infrastructure (contracts/civil_schema.md): CRS-aware site model,
    # terrain + grading, alignments, water distribution + fire flow, sewer/storm + hydrology
    # + detention + culverts, buried pipe / thrust / supports, QTO/cost/schedule and the
    # plant tie-in coupling to piping. cad_guide('civil') and the civil-design skill name
    # them as one workflow; registry family `sim`, so the pack lists them explicitly.
    "civil": [
        "civil_site_new",
        "civil_load",
        "civil_site_edit",
        "civil_site_summary",
        "civil_import",
        "civil_export",
        "civil_crs_transform",
        "civil_surface_build",
        "civil_surface_analyze",
        "civil_grade_pad",
        "civil_watershed",
        "civil_alignment",
        "civil_alignment_analyze",
        "civil_quantities",
        "civil_cost_estimate",
        "civil_schedule",
        "civil_water_solve",
        "civil_fire_flow",
        "civil_gravity_analyze",
        "civil_gravity_design",
        "civil_open_channel",
        "civil_hydrology",
        "civil_detention_route",
        "civil_culvert",
        "civil_buried_pipe_check",
        "civil_thrust_restraint",
        "civil_pipe_supports",
        "civil_piping_couple",
        "civil_fea_handoff",
        "civil_requirement_evidence",
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
    "docs": "Reports, work instructions, weld travelers, office conversion",
    "mfgplan": "Manufacturing plan scratchpad: process, datums, critical dims, DfM, references",
    "reveng": "Reverse engineering: scan/photo/datasheet intake, reconstruct, verify deviation",
    "advisor": "Stage advisory checks and dispute",
    "delegate": "Subagents, ask_user, ledger/dag/buildgraph, mission + org coordination",
    "plm": "Item lifecycle, checkpoints/rollback, release, product-graph queries",
    "fluid": ("Hydraulic circuits (build, size pumps/motors/cylinders and drives, transient "
              "simulate), piping networks (route/solve/B31.3 check/BOM, pipe-bore CFD) and "
              "vessel/duct sizing"),
    "civil": ("Civil/site/utility design: CRS + GIS/LandXML/IFC exchange, terrain/grading/"
              "earthwork, alignments, water distribution + fire flow, sewer/storm hydraulics, "
              "hydrology/detention/culverts, buried pipe/thrust/supports, QTO/cost/schedule, "
              "plant tie-in coupling to piping"),
    "requirements": "Layout plan, requirements capture and auto-verification",
    "office": "PDF/DOCX read, extract, edit and render",
    "sheetmetal": "Sheet-metal flanges/bends via add_features + bend calcs",
    "print": ("3D printing: printability checks, orientation, slicing to G-code, G-code proof, "
              "printer-media inventory and managed publish"),
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
            if name not in AGENT_DENIED_TOOLS
            and isinstance(tags, dict) and tags.get("family") == fam
        }
    return {t} if t not in AGENT_DENIED_TOOLS else set()


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


# --------------------------------------------------------------------------- main chat
#
# The interactive main chat (``general_agent``) is NOT a capped DAG-node role. It
# runs with one Chudcode turn per message and the model decides everything —
# inline work, a subagent, or a mission. Two things bound its surface:
#
#   * :data:`MAIN_CHAT_CORE_TOOLS` — always advertised with full schemas. Picked so
#     ordinary part/assembly/drawing/PLM/file work never needs a load step.
#   * :func:`render_tool_menu` — EVERY other registered tool, by exact name, grouped,
#     rendered into the system prompt (names only, ~2k tokens). Nothing is hidden:
#     the model loads any tool with ``enable_tools(tools=[...])`` or a whole group
#     with ``enable_tools(categories=[...])`` and its schema arrives mid-turn.
#
# The full catalog (~460 tools) is ~150k tokens of schema, which is why the menu
# carries names and the schemas load on demand.

MAIN_CHAT_CORE_TOOLS: tuple[str, ...] = (
    # conversation / harness
    "ask_user",
    "task_list",
    "delegate",
    "mission_start",
    "enable_tools",
    "list_tool_categories",
    "design_recipe",
    "cad_guide",
    # The fetch half of the [SKILLS] catalog every online role prompt now carries.
    # The catalog names each pack in one line and instructs the model to call this
    # for the body, so a turn that reaches assembly work or a simulation needs it
    # THAT turn — an enable_tools round-trip to reach a read-only text lookup is
    # exactly the wasted turn the catalog exists to prevent. Its schema is two
    # string params, so it costs the prompt almost nothing.
    "cad_skill",
    "advise",
    # files (project jail)
    "fs_read",
    "fs_glob",
    "fs_grep",
    "fs_list",
    # The model-facing thumbnail explorer: every CAD file's card + ASCII preview.
    "browse_models",
    "fs_write",
    "fs_edit",
    # documents
    "new_part",
    "new_assembly",
    "list_parts",
    "get_part",
    "describe_part",
    "describe_assembly",
    "describe_project",
    "workspace_status",
    "save_part",
    "export_part",
    "close_part",
    "import_part",
    "set_material",
    # Saved .step+ files read without opening them. c211-c233 agents shelled out to
    # `unzip -p X.step+ document.json | jq` ~40 times for features/import params/
    # components/mates/materials; inspect_native_files was enable_tools-loaded in 5
    # of the 19 sessions.
    "inspect_native_files",
    "read_native_file",
    # background jobs: a heavy call hands back a job_id; wait_job replaces the
    # `sleep 600; ps -p <pid>` loops agents wrote in bash.
    "get_job",
    "wait_job",
    # modeling
    "add_features",
    "preflight_ops",
    "verify_battery",
    "regen_diagnose",
    # sketch
    "describe_sketch",
    "check_sketch",
    "render_sketch",
    # measure / inspect
    "measure_part",
    "measure_between",
    "query_geometry",
    "wall_thickness",
    "render_part",
    "inspect_part",
    "inspect_feature",
    "inspect_section",
    "check_geometry_health",
    "check_interference",
    # Printability: every "optimize for 3D printing" turn loaded these on demand. Slicing
    # is part of the main chat because "print this for my Ender 3" is an ordinary ask.
    "dfm_check",
    "analyze_stl_geometry",
    "optimize_print_orientation",
    "slice_part",
    "verify_gcode",
    # Delivering G-code to a printer stick is part of "print this": without these in the
    # core the agent shell-copied revisions next to stale ones the printer shows with no
    # timestamps. Inventory is read-only; publish is mutating (Ask/Plan refuse it).
    "print_media_inventory",
    "publish_print_media",
    # assembly
    "asm_add_component",
    "asm_mate",
    "solve_mates",
    # Per-solid assembly census and fit checks; the two most-loaded on-demand
    # tools across the c211-c233 sessions (7 and 4 sessions).
    "list_world_bodies",
    "interface_check",
    # Motion must be callable alongside mates so mechanisms stay driveable,
    # not just positioned by fixed component transforms.
    "simulate_motion",
    "render_animation",
    # drawing
    "make_drawing",
    "export_drawing",
    # plm
    "plm_open",
    "plm_save",
    "plm_checkpoint",
    "plm_item_add",
    "plm_status",
    "plm_history",
    "plm_timeline",
    "plm_rollback",
    # reference / sourcing
    "part_search",
    "part_get",
    "part_source",
    "match_model",
    "make_standard_part",
    "list_standard_parts",
    "fetch_cad_model",
    "import_artifact",
    # research
    # Chudcode owns its builtin web_search/read; the distinctly named CAD
    # research tools coexist with them (we do not hide Chudcode's builtins).
    "research_web_search",
    "research_fetch_page",
    "research_material_lookup",
)


class _MenuGroup:
    """One menu group: claims registry tools by family, optionally narrowed by name.

    ``patterns`` are matched against the tool name — a trailing ``_`` is a prefix
    match, anything else is exact. The first group (in :data:`_MENU_GROUPS` order)
    that claims a tool wins; a group with no patterns claims its families' remainder.
    """

    __slots__ = ("name", "families", "patterns", "description")

    def __init__(
        self,
        name: str,
        families: tuple[str, ...],
        patterns: tuple[str, ...],
        description: str,
    ) -> None:
        self.name = name
        self.families = families
        self.patterns = patterns
        self.description = description

    def claims(self, tool: str, family: str) -> bool:
        if family not in self.families:
            return False
        if not self.patterns:
            return True
        return any(
            tool.startswith(p) if p.endswith("_") else tool == p for p in self.patterns
        )


_MENU_GROUPS: tuple[_MenuGroup, ...] = (
    _MenuGroup("harness", ("workspace", "shell"), (),
               "Session helpers, task list, bug report, run_shell, live doc events"),
    _MenuGroup("files", ("files",), (),
               "Project-jailed file read/search/write/edit"),
    _MenuGroup("files", ("cad",),
               ("inspect_native_files", "read_native_file", "relocate_native_files"),
               "Native STEP+ metadata/tree reads and copy/rename plans without geometry evaluation"),
    _MenuGroup("sketch", ("cad", "gsl"),
               ("sketch_", "sketch3d_", "check_sketch", "check_sketch3d", "render_sketch",
                "render_sketch_ascii", "describe_sketch", "doc_sketch_contact_sheet",
                "sketch_preview_text"),
               "2D/3D sketches: constraints, health, previews, 3D paths and pipes"),
    _MenuGroup("assembly", ("cad",),
               ("asm_", "new_assembly", "solve_mates", "stackup_analyze", "interface_check",
                "check_interference", "assembly_plan_review", "describe_assembly",
                "list_world_bodies", "inspect_model_integrity"),
               "Components, mates, per-solid integrity, stackups and interface/fit checks"),
    _MenuGroup("drawing", ("cad",),
               ("add_balloon", "add_chamfer_dimension", "add_datum", "add_datum_target",
                "add_detail_view", "add_dimension", "add_drawing_", "add_gdt",
                "add_hole_callout", "add_ordinate_dimensions", "add_section_view",
                "add_surface_finish", "add_weld_symbol", "audit_drawing", "drawing_bom",
                "edit_drawing_annotation", "export_drawing", "list_drawing_sheets",
                "make_drawing", "review_drawing", "set_active_drawing_sheet",
                "set_titleblock", "set_view", "suggest_drawing_dimension_scheme",
                "suggest_dimension_scheme"),
               "2D drawings: views, dimensions, GD&T, tables, title block, PDF/DXF export"),
    _MenuGroup("docs", ("cad",),
               ("make_report", "make_presentation", "make_work_instruction",
                "make_weldment_traveler", "make_workbook", "render_document"),
               "Reports, work instructions, travelers, presentations, workbooks"),
    _MenuGroup("mfgplan", ("cad",), ("mfg_plan_",),
               "Manufacturing plan scratchpad: process, datums, critical dims, DfM"),
    _MenuGroup("requirements", ("cad",), ("req_", "layout_plan", "layout_plan_report"),
               "Requirements capture/verification and layout plans"),
    _MenuGroup("measure", ("cad", "gsl"),
               ("measure", "measure_", "query_", "wall_thickness", "hover", "is_watertight",
                "fit_cylinder_axis"),
               "Mass properties, distances, wall thickness, geometry/adjacency queries"),
    _MenuGroup("inspect", ("cad", "gsl"),
               ("check_", "inspect_", "render_", "look_around", "contact_sheet",
                "design_review", "design_audit", "draft_analysis", "verify_battery",
                "doc_diff_tool", "engine_profile", "engine_profile_tree",
                "assess_mesh_quality", "analyze_stl_geometry", "health_check",
                "describe_", "apply_ascii_edits", "show_in_app", "get_job", "wait_job",
                "list_artifacts"),
               "Render/inspect views, health/DFM/overlap checks, describers, ASCII views"),
    _MenuGroup("plm", ("cad",), ("plm_graph_",),
               "Vault items, checkpoints/rollback, release, product graph, live collab, Box"),
    _MenuGroup("plm", ("plm",), (), ""),
    _MenuGroup("modeling", ("cad",), (),
               "Part geometry beyond add_features: configurations, standard parts, regen repair"),
    _MenuGroup("coordination", ("mission", "ledger", "org", "advisor"), (),
               "Missions/DAG, ledger, org requests, advisory checks"),
    _MenuGroup("intake", ("intake",), (),
               "Photo intake of real parts: measure, confirm dims, fit coupons"),
    _MenuGroup("research", ("research",), (),
               "Web, materials, standards, threads, unit conversion, machining references"),
    _MenuGroup("sourcing", ("parts",), (),
               "Parts catalog search/place, BOM cost, vendors, quotes, scrape import"),
    _MenuGroup("sim", ("sim",), (),
               "FEA (static/modal/fatigue), CFD, motion, thermal, fluid/piping circuits"),
    _MenuGroup("cam", ("cam",), (),
               "CAM jobs, ops, toolpaths, post, feeds & speeds, cost; 3D-print slicing, "
               "print orientation, G-code verification"),
    _MenuGroup("ecad", ("ecad", "em"), (),
               "Schematic/PCB place-wire-board, ERC/DRC, simulation, antenna/EM, firmware"),
    _MenuGroup("reveng", ("reveng", "recon"), (),
               "Reverse engineering: scan/photo/datasheet intake, reconstruct, verify deviation"),
    _MenuGroup("office", ("office",), (),
               "PDF/DOCX/XLSX/PPTX/CSV read, extract, edit, convert"),
    _MenuGroup("manufacture", ("manufacture",), (),
               "Machine devices, runs, approvals and telemetry (default-off family)"),
)


def _live_catalog() -> dict[str, dict]:
    try:
        from services.mcp import tool_registry as _tr  # noqa: PLC0415 — lazy
        _tr.warm_catalog()
        return {
            name: meta for name, meta in (_tr.catalog() or {}).items()
            if name not in AGENT_DENIED_TOOLS
        }
    except Exception:  # noqa: BLE001 — catalog optional at import/test time
        return {}


def loadable_tool_names(advertised: Iterable[str]) -> list[str]:
    """Every live catalog tool that is not in *advertised*, sorted.

    ``enable_tools`` can load each of these names. Chudcode uses the list to answer
    ``xd://<name>`` for an unloaded tool with the load step.
    """
    mounted = set(advertised)
    return sorted(name for name in _live_catalog() if name and name not in mounted)


def tool_menu_groups() -> list[tuple[str, str, list[str]]]:
    """``[(group, description, [tool, ...]), ...]`` over the LIVE registry.

    Every registered tool lands in exactly one group (first :data:`_MENU_GROUPS`
    rule that claims it wins; several rules may share a group name, the first
    carries the description); a tool whose family no rule names lands in
    ``other``. Group order is first-rule order; tool names are sorted within a group.
    """
    order: list[str] = []
    descs: dict[str, str] = {}
    buckets: dict[str, list[str]] = {}
    for g in _MENU_GROUPS:
        if g.name not in buckets:
            order.append(g.name)
            descs[g.name] = g.description
            buckets[g.name] = []
    other: list[str] = []
    for tool, meta in _live_catalog().items():
        family = str((meta or {}).get("family") or "")
        for group in _MENU_GROUPS:
            if group.claims(tool, family):
                buckets[group.name].append(tool)
                break
        else:
            other.append(tool)
    out = [(name, descs[name], sorted(buckets[name])) for name in order if buckets[name]]
    if other:
        out.append(("other", "Tools outside every group above", sorted(other)))
    return out


def menu_group_names() -> list[str]:
    return list(dict.fromkeys(g.name for g in _MENU_GROUPS))


def _compact_names(names: list[str]) -> str:
    """``a, pre_{x|y|z}, b``: names sharing a ``word_`` prefix (3+) folded into one entry.

    Every exact name stays recoverable (``pre_`` + each alternative); the menu is
    re-read on every request of a conversation, so ``ecad_`` spelled 80 times was
    most of its bytes. :func:`expand_menu_entry` is the inverse.
    """
    buckets: dict[str, list[str]] = {}
    for name in names:
        head, sep, _ = name.partition("_")
        buckets.setdefault(head + sep if sep else name, []).append(name)
    out: list[str] = []
    emitted: set[str] = set()
    for name in names:
        head, sep, _ = name.partition("_")
        key = head + sep if sep else name
        group = buckets[key]
        if len(group) < 3:
            out.append(name)
        elif key not in emitted:
            emitted.add(key)
            out.append(key + "{" + "|".join(n[len(key):] for n in group) + "}")
    return ", ".join(out)


def expand_menu_entry(entry: str) -> list[str]:
    """The exact tool names one :func:`_compact_names` entry stands for."""
    entry = entry.strip()
    if entry.endswith("}") and "{" in entry:
        prefix, _, rest = entry.partition("{")
        return [prefix + alt for alt in rest[:-1].split("|")]
    return [entry] if entry else []


def render_tool_menu(loaded: Iterable[str] | None = None) -> str:
    """The prompt block that makes the WHOLE catalog visible to the main chat.

    *loaded* names are already advertised with schemas this turn and are left out
    of the listing; a group with nothing left is omitted. Returns "" when the
    registry is cold (unit tests) so the prompt simply has no menu.
    """
    skip = {str(t) for t in (loaded or ()) if t}
    lines: list[str] = []
    for name, desc, tools in tool_menu_groups():
        rest = [t for t in tools if t not in skip]
        if not rest:
            continue
        lines.append(f"- {name} ({len(rest)}) — {desc}:")
        lines.append("  " + _compact_names(rest))
    if not lines:
        return ""
    head = (
        "MORE TOOLS (registered, not loaded yet). Load exact names with "
        "enable_tools(tools=[...]) or a whole group with "
        "enable_tools(categories=[<group>]); the schemas arrive for the rest of "
        "the turn. `pre_{a|b}` means pre_a and pre_b. Every tool below is "
        "available to you — nothing else exists."
    )
    return head + "\n" + "\n".join(lines)


def resolve_tool_requests(tokens: Iterable[str] | None) -> tuple[set[str], list[str]]:
    """Resolve ``enable_tools`` tokens to ``(tool_names, unknown_tokens)``.

    Each token resolves, in order, as a live menu group (the menu is the truth
    the model read), a legacy :data:`CATEGORY_PACKS` pack, a ``family:<name>``
    registry family, or an exact registry tool name. Explicit requests are never
    capped: the model saw the exact list it asked for. Non-registry meta names
    (``delegate``) are accepted as-is so a mission-scoped tool is never "unknown".
    """
    catalog = _live_catalog()
    groups = {name: set(items) for name, _desc, items in tool_menu_groups()}
    out: set[str] = set()
    unknown: list[str] = []
    expanded = [name for raw in tokens or () for name in expand_menu_entry(str(raw or ""))]
    for raw in expanded:
        token = str(raw or "").strip()
        key = token.lower()
        if not key:
            continue
        if key in groups:
            out |= groups[key]
        elif key in CATEGORY_PACKS:
            out |= resolve_categories([key], cap=10_000)
        elif key.startswith("family:"):
            found = _expand_token(key)
            if found:
                out |= found
            else:
                unknown.append(token)
        elif token in catalog or token in META_TOOLS or token == "delegate":
            out.add(token)
        else:
            unknown.append(token)
    return out, unknown
