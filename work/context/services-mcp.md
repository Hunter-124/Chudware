# services/mcp — MCP server (AI-CAD engine exposed to external agents)

Scope: services/mcp/**, root `mcp_server` symlink (the stdio MCP server)
Contracts: contracts/doc_ops.md, contracts/doc_schema.md, contracts/worker_rpc.md,
           contracts/feature_params.md, contracts/ecad/ecad_rpc.md,
           contracts/narration.md, contracts/interaction.md, contracts/requirements.md,
           contracts/dag_schema.md, contracts/parts.md, contracts/sourcing.md,
           deliverables/1 (deliverables package)

## Entry points

- services/mcp/server.py:26 `mcp` FastMCP instance — root MCP server; registers all 26
  tool families via `register_*` functions (L60–185). Instructions string (L29–51) is the
  agent's first-contact map of the full capability surface.
- services/mcp/server.py:267 `main()` — entrypoint; spawns libaicad.so warmup thread,
  redirects native stdout (OCCT prints) to a log file to protect the JSON-RPC channel.
- services/mcp/cad_tools.py:1158 `register_cad_tools(mcp)` — mechanical CAD + simulation
  (new_part, add_features, render_part, run_fea_static/modal, run_cfd_flow/accurate,
  export_part, make_drawing, measure_part, check_feature_overlap, check_interference,
  wall_thickness, draft_analysis, list_parts, undo/redo, close_part, recover_parts,
  import_part, make_standard_part, set_material/view/process, configurations, drawings).
- services/mcp/cad_tools.py:1009 `cad_guide_text(topic)` — the knowledge-dispatch
  function behind the `cad_guide` tool; topics: overview, simulation, review, intake, gsl,
  cam, narration, parts, reverse, deliverables, requirements, dag, verify, interaction,
  sourcing, cost, spec, piping, hydraulic, firmware, structural, regen, code_compliance,
  serving, design_review, doc_diff, plm (stub), + per-feature cheatsheets.
- services/mcp/gsl_tools.py:1221 `register_gsl_tools(mcp)` — compact geometry read:
  describe_part, query_adjacency, hover, describe_sketch, describe_scene,
  describe_toolpaths, describe_results, describe_assembly, describe_board, describe_project.
- services/mcp/recon_tools.py `register_recon_tools(mcp)` — reverse-engineering + intake:
  reverse_engineer, compare_parts, convert_face_to_sketch, resolve_process, dfm_check,
  start/stop_capture_session, request/submit_part_photos, measure_from_photo.
- services/mcp/intake_tools.py `register_intake_tools(mcp)` — manufacturing clarification:
  intake_status, confirm_dimensions, dfm_check, fit_report, make_fit_coupon.
- services/mcp/interaction_tools.py:66 `register_interaction_tools(mcp)` — elicitation
  seam (Pillar 9, contracts/interaction.md): ask_user (typed questions with defaults,
  persist + replay within scope).
- services/mcp/ecad_tools.py `register_ecad_tools(mcp)` — schematic/PCB: ecad_new_*,
  ecad_place_symbol, ecad_wire, ecad_label, ecad_add_power/zone, ecad_autoroute,
  ecad_fill_zones, ecad_run_erc/drc/dfm, ecad_simulate, ecad_to_3d, ecad_export,
  ecad_status, ecad_find_part, ecad_design, ecad_sweep, ecad_tf, ecad_update_from_sch,
  ecad_check_impedance/diff_impedance.
- services/mcp/cam_tools.py:49 `register_cam_tools(mcp)` — CAM/toolpaths: cam_new_job,
  cam_add_setup/tool/operation, cam_update/remove_operation, cam_generate, cam_simulate,
  cam_post, cam_job_tree, cam_open, cam_save, cam_undo/redo, cam_extract, cam_set_stock,
  cam_setup_sheet, cam_materials, cam_op_types, cam_tool_types, cam_post_dialects.
- services/mcp/doc_diff_tools.py:185 `register_doc_diff_tools(mcp)` — semantic
  feature-level diff between two document revisions: doc_diff_tool(rev_a, rev_b).
- services/mcp/req_tools.py:102 `register_req_tools(mcp)` — requirements CRUD
  (aicad.req/1): req_create, req_update, req_status, req_report.
- services/mcp/verify_tools.py:8 `register_verify_tools(mcp)` — done-check battery:
  verify_battery(doc_id, checks?), check_metadata().
- services/mcp/research_tools.py `register_research_tools(mcp)` — Pillar 8 look-it-up:
  research_web_search, research_fetch_page, research_material_lookup, research_unit_convert,
  research_standard_section, research_datasheet_fetch, research_thread_spec, research_eng_calc.
- deliverables/ `register_deliverables_tools(mcp)` — spec-driven office artifacts: make_report,
  make_presentation, make_workbook, render_document, list_artifacts. Guide: cad_guide('deliverables').
- services/mcp/sourcing_tools.py:17 `register_sourcing_tools(mcp)` — BOM roll-up + pricing:
  bom_rollup(doc_path), bom_price(doc_path).
- services/mcp/cost_tools.py:62 `register_cost_tools(mcp)` — made-part cost model:
  cam_cost_estimate(doc_path, material?, process?), request_quote(doc_path, vendor, qty).
- services/mcp/spec_tools.py:19 `register_spec_tools(mcp)` — MPN resolution + datasheet
  extraction: match_part_number(mpn), spec_sheet_match(text_or_url, mpn, template),
  list_spec_templates().
- services/mcp/piping_tools.py:77 `register_piping_tools(mcp)` — routed pipe networks:
  piping_solve, piping_report, piping_operating_point, piping_fluid, piping_internal_volume,
  piping_cfd_handoff, piping_verify_cfd.
- services/mcp/hydraulic_tools.py:76 `register_hydraulic_tools(mcp)` — ISO 1219 circuits:
  hydraulic_new, hydraulic_add_component, hydraulic_add_hose, hydraulic_size_cylinder,
  hydraulic_size_pump, hydraulic_bom.
- services/mcp/firmware_tools.py:25 `register_firmware_tools(mcp)` — embedded compile loop:
  fw_new_project, fw_build, fw_sources, fw_sim_run, fw_flash, fw_debug_start/halt/read_mem.
- services/mcp/structural_tools.py:37 `register_structural_tools(mcp)` — PyNite member
  analysis: struct_analyze, struct_check (AISC 360/NDS code check), struct_estimate.
- services/mcp/parts_tools.py:51 `register_parts_tools(mcp)` — federated standard-parts
  library (aicad.parts/1): part_search, part_get, part_place.
- services/mcp/serving_tools.py:28 `register_serving_tools(mcp)` — LoRA adapter routing:
  serving_status, serving_set_canary, serving_rollback, serving_evaluate, serving_promote.
- services/mcp/regen_tools.py:89 `register_regen_tools(mcp)` — rebuild-failure diagnosis:
  regen_diagnose(document_json, reference_json?).
- services/mcp/code_compliance_tools.py:23 `register_code_compliance_tools(mcp)` —
  building-code checks (IBC/ASCE/NFPA + RAG): code_check(doc_path, codes?, jurisdiction?).
- services/dag/mcp_tools.py:100 `register_dag_tools(mcp)` + `register_dag_replan_tools(mcp)` —
  DAG orchestrator: dag_plan, dag_run, dag_status, dag_approve, dag_abort, dag_replan.
- services/mcp/design_review_tools.py:32 `register_design_review_tools(mcp)` — one-call
  geometry battery narrated via contracts/narration.md: design_review(doc_id, checks?).

## How it connects

The stdio MCP server (server.py) wraps the SAME C++ core (`libaicad.so`) that the desktop
app uses, exposing it to external MCP clients (Claude Code, Cursor, etc.) as a tool palette.
Each tool group registers its tools via FastMCP decorators (`@mcp.tool()`) and dispatches
into the C++ engine through `core_bridge.call()` — a ctypes wrapper over the native
`aicad_call(method, params_json)` C-ABI entry. The tool_registry `view()` wrapper stamps
each registration with a `{family, risk}` tag and enforces a per-context allow-list at
dispatch. Default policy = everything allowed; DAG nodes + env can restrict it.

Python engine_oracle modules (worker.py, kernel.py, formats.py) are called in-process by
MCP tools, NOT through the worker socket. ECAD is pure Python until 3D (board_to_step).

This is different from `work/tools/team_mcp.py` (dev-team coordination MCP server — separate).

## Debugging playbook

- Run: `python -m mcp_server.test_roundtrip` — full MCP roundtrip (new_part, add_features,
  render, FEA, export); prints each step. Requires libaicad.so.
- Run: `python -m pytest services/mcp/test_mcp_freshness.py -v` — freshness/consistency
  test: every tool family discoverable from server instructions + cad_guide. No libaicad needed.
- Run: `python -m pytest services/mcp/test_cad_tools.py -v` — mechanical CAD tool suite.
- Run: `python -m pytest services/mcp/test_gsl_tools.py -v` — GSL describe/query/hover tests.
- Run: `python -m pytest services/mcp/test_recon_tools.py -v` — reverse-engineering tests.
- Run: `python -m pytest services/mcp/test_ecad_tools.py -v` — ECAD tool tests.
- Run: `python -m pytest services/mcp/test_cam_tools.py -v` — CAM tool tests.
- Run: `python -m pytest services/mcp/test_req_tools.py -v` — requirements CRUD tests.
- Run: `python -m pytest services/mcp/test_autosave_recovery.py -v` — autosave recovery.
- Environment: `$AI_CAD_LIBAICAD=/path/to/libaicad.so` (Linux) or `$AI_CAD_DIST=dist/AI-CAD`
  (Windows); core_bridge.py L75–92 lists candidates.
- Gotcha: server.py `_install_clean_stdout()` redirects fd 1 to a log file — native C++ prints
  (OCCT, Netgen, gmsh) corrupt the JSON-RPC channel; set $TEMP to see logs.
- Gotcha: `_warm_core()` pre-loads libaicad.so + ECAD deps in a background thread to avoid
  transport timeouts on the first tool call; the thread also runs ECAD autosave recovery.

## MCP freshness rule (WORKFLOW.md §6 — enforced mechanically)

`test_mcp_freshness.py` catches drift: every registered tool family must have a signal string
in server.py instructions OR a cad_guide topic in cad_tools.py:cad_guide_text(). When adding
a new tool family: (1) add it to `_TOOL_FAMILIES` in the test, (2) add its signal to
`_FAMILY_SIGNALS`, (3) update either server.py instructions or cad_guide_text() with a topic.
The test runs in the pytest gate stage (no libaicad.so needed).

## Graphify starters

- graphify query "mcp_server call cad_guide new_part add_features render_part"
- graphify query "register_cad_tools register_gsl_tools register_cam_tools server.py"
- graphify explain "core_bridge call"
- graphify path "server.py" "core_bridge.py"

## Related tasks/docs

- docs/AI_TOOLING_MASTER_PLAN.md (pillar 12 — MCP-first distribution)
- contracts/doc_ops.md (feature op vocabulary), contracts/doc_schema.md
- contracts/worker_rpc.md (C++ core protocol), contracts/ecad/ecad_rpc.md
- contracts/narration.md (narration style guide for describe_* tools)
- contracts/interaction.md (ask_user contract — Pillar 9)
- contracts/requirements.md (aicad.req/1 schema)
- contracts/dag_schema.md (DAG run/node schema)
- contracts/parts.md (aicad.parts/1 federated catalog schema)
- work/tasks: T-BL26 (freshness audit this file documents), T-R2 (PLM plm_* tools)
