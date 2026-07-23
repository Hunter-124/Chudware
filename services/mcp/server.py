"""AI-CAD MCP server.

Exposes the AI-CAD modeling engine — the SAME C++ core (`libaicad.dll`) the desktop app
runs — as MCP tools, so an MCP client (Claude in cowork/chat or Claude Code) can drive
real parametric CAD and simulation: build an editable feature tree with the plain-English
geometry vocabulary, render an annotated image to self-review, run FEA (static/modal) and
wind-tunnel CFD, make 2D drawings, pull standard hardware, and export STEP/STL/3MF/etc.

This is parity with the in-app AI agent — except YOU (the model calling these tools) are the
brain, so call `cad_guide()` first to load the engine's op vocabulary, then model → render →
iterate → simulate exactly like the app does.

Mechanical CAD + simulation tools live in the `cad_*_tools` family modules (driven through
`core_bridge`, the ctypes bridge to the staged DLL). ECAD (schematic/PCB) tools live in the
`ecad_*_tools` family modules.
Runs over stdio; the desktop host registers it into the machine's MCP client configs.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

from mcp.server.fastmcp import FastMCP

# Profile selects how much scaffolding the server exposes + how prescriptive its instructions
# are. `full` (default) is the supervised experience tuned for small local models: the long
# how-to instructions plus the DAG/mission/buildgraph/foreman/ledger/workspace orchestration
# families. `lite` is for capable models (e.g. mythos) that drive themselves: it keeps EVERY
# capability + calculator + sourcing helper but drops the orchestration/supervision families
# and swaps in a short, non-prescriptive instruction block, so the model runs freer and we
# don't spend input tokens on a workflow it won't follow.  Switch with AICAD_MCP_PROFILE=lite.
# WI-16: capability letter also influences the default — C (frontier) prefers lite; A/B full.
# Explicit AICAD_MCP_PROFILE always wins. AICAD_CAPABILITY=A|B|C or AI_CAD_TIER can select.
_PROFILE_ENV = os.environ.get("AICAD_MCP_PROFILE", "").strip().lower()


def _resolve_mcp_profile() -> str:
    """full | lite — explicit env wins; else capability letter C → lite (WI-16)."""
    if _PROFILE_ENV in ("full", "lite"):
        return _PROFILE_ENV
    letter = (os.environ.get("AICAD_CAPABILITY")
              or os.environ.get("AI_CAD_CAPABILITY")
              or "").strip().upper()
    if letter not in ("A", "B", "C"):
        # Best-effort: map active tier → capability letter without hard-importing
        # the full agent package at MCP import time when unavailable.
        try:
            from services.agent.capability import resolve_capability  # noqa: PLC0415
            tier = (os.environ.get("AI_CAD_TIER") or "").strip()
            if tier:
                letter = resolve_capability(tier).letter
        except Exception:  # noqa: BLE001
            letter = ""
    if letter == "C":
        return "lite"
    if letter in ("A", "B"):
        return "full"
    return "full"  # historical default


_PROFILE = _resolve_mcp_profile()
_LITE = _PROFILE == "lite"

# WI-16 SOFT phrasing tails (capability dial). Lite carries C (minimal); full
# carries A mandatory discipline when the server is in supervised mode. B uses
# full + suggestive block when AICAD_CAPABILITY=B.
_SOFT_A = (
    "\n[CAPABILITY_SOFT] A — MANDATORY DISCIPLINE (local scaffold)\n"
    "calculate-before-model: never invent load-bearing numbers — run fit_calc / "
    "research_* / the matching calculator before dimensioned features.\n"
    "datum-first; fillet-last within a region; read-back after every mutation "
    "(describe_part / check_sketch) before the next mutate; use plan/buildgraph "
    "templates rather than free-forming the tree.\n"
)
_SOFT_B = (
    "\n[CAPABILITY_SOFT] B — RECOMMENDED DISCIPLINE (budget)\n"
    "Suggested build order: datums → solids → secondary → patterns → blends. "
    "Verify after feature groups (not necessarily every call). Name features. "
    "Preflight recommended on non-trivial ops. Calculate when uncertain.\n"
)
_SOFT_C = (
    "\n[CAPABILITY_SOFT] C — HARD FLOORS ONLY (frontier freedom)\n"
    "Prefer verify_battery before done; checkpoint before irreversible ops. "
    "No prescribed build order beyond those process floors.\n"
)


def _soft_tail_for_profile() -> str:
    letter = (os.environ.get("AICAD_CAPABILITY")
              or os.environ.get("AI_CAD_CAPABILITY")
              or "").strip().upper()
    if letter not in ("A", "B", "C"):
        letter = "C" if _LITE else "A"
    if letter == "A":
        return _SOFT_A
    if letter == "B":
        return _SOFT_B
    return _SOFT_C


_LITE_INSTRUCTIONS = (
    "Real parametric CAD + simulation on the AI-CAD C++ engine (same kernel as the desktop "
    "app). You are the design brain and you drive yourself — these tools are helpers, not a "
    "script; call whatever you need, in whatever order the task wants, and skip what you don't.\n"
    "ENTRY: call cad_guide() for the engine's op vocabulary + worked examples, and "
    "cad_guide(topic) for depth on a subsystem (simulation, review, gsl, cam, ecad, parts, "
    "sourcing, cost, piping, hydraulic, firmware, structural, sketch3d, reverse, deliverables, "
    "requirements, tolerance_stackup, …). Don't recall op syntax from memory — cad_guide has it.\n"
    "CONVENTIONS THAT MATTER: address geometry in PLAIN WORDS ('the top face', 'the vertical "
    "edges', 'the largest round face') — never face numbers or raw coordinates. Build with the "
    "feature a person would use (sketch+revolve for round bodies, loft for blended shapes). "
    "Touching sketch endpoints auto-coincide on every edit, so profiles close on their own. "
    "render_part() SEES the annotated result; describe_part()/describe_sketch()/describe_scene() "
    "are the text-only read when you have no vision.\n"
    "PARALLEL WORK: every tool takes a doc_id. When more than one part is open — e.g. several "
    "subagents each building a different part — ALWAYS pass the doc_id you got from new_part/open "
    "so you address your OWN part; omitting it errors (the 'current part' is process-wide and "
    "shared, so a bare call would clobber another agent's work).\n"
    "LOOK IT UP, DON'T RECALL: use research_* for any load-bearing number (materials, sections, "
    "threads, unit conversions) — never type one from memory.\n"
    "STANDARD PARTS FIRST for hardware: part_search/part_source → part_place before custom "
    "geometry. Calculators (feeds_speeds, tap_drill, fit_calc, stackup_analyze, gear/bearing "
    "checks, struct_*), verification (verify_battery, design_review, check_*), and deliverables "
    "(make_report/drawing/presentation) are all available — reach for them when they help.\n"
    "BE ECONOMICAL: model, check what actually needs checking, and deliver. Don't over-verify, "
    "don't narrate every step, and keep outputs tight — no busywork, no redundant re-renders."
    + _soft_tail_for_profile()
)

_FULL_INSTRUCTIONS = (
        "Real parametric CAD + simulation on the AI-CAD C++ engine (same kernel as the "
        "desktop app). You are the design brain.\n"
        "START by calling cad_guide() — it returns the engine's modeling op vocabulary and "
        "worked examples; cad_guide('simulation') and cad_guide('review') cover FEA/CFD and "
        "how to read the annotated render.\n"
        "PARALLEL WORK: every tool takes a doc_id. When more than one part is open — e.g. several "
        "subagents each building a different part in this shared engine — ALWAYS pass the doc_id "
        "returned by new_part/new_assembly/import so you address your OWN part; omitting it errors "
        "when >1 part is open (the 'current part' is process-wide, so a bare call would edit "
        "another agent's part). The engine serialises ops safely, so parallel agents just work.\n"
        "TYPICAL LOOP: new_part(name) -> add_features([...ops...]) to build with standard "
        "editable features (boxes/sketch+extrude/revolve/loft/sweep, fillet/chamfer/shell, "
        "holes, bolt_circle, patterns/mirror) -> render_part() to SEE the colour-coded, "
        "face-labelled result and self-review against the request -> iterate add_features to "
        "fix anything -> measure_part()/query_geometry() to check -> run_fea_static()/"
        "run_fea_modal()/run_cfd_flow() to prove it -> export_part() or make_drawing() to "
        "deliver.\n"
        "Address geometry in PLAIN WORDS — 'the top face', 'all edges', 'the vertical edges', "
        "'the largest round face' — never face numbers or raw coordinates. Build with the "
        "feature a person would use (sketch+revolve for round bodies, loft for blended shapes), "
        "not stacks of boxes. The OpenCASCADE B-rep kernel gives precise solids.\n"
        "NO VISION? Use describe_part()/describe_sketch()/describe_scene() (cad_guide('gsl')) "
        "for a text-only read of the geometry instead of render_part's image. Before calling a "
        "design done, run check_feature_overlap() (one part's own features colliding — a boss "
        "swallowed by a later cut, redundant cuts) and, for multi-body/assembly docs, "
        "check_interference() (clash/clearance between bodies or components).\n"
        "SKETCHES: touching endpoints are auto-coincident on every sketch edit (config "
        "sketch.auto_coincident, default on), so profiles close without a 'not closed' error. "
        "The sketch loop is edit -> sketch_health(id) for the narrated read (closure, which free "
        "variables remain in plain words, an ordered relations-then-dimensions schedule) -> add the "
        "scheduled relations / sketch_auto_constrain(id) to stitch near-touching endpoints -> "
        "repeat until check_sketch reports dof 0.\n"
        "ASSEMBLIES: new_assembly(name) creates an assembly doc; asm_add_component(path) places "
        "saved .step+ parts; asm_mate(type, a, b) constrains them with plain-word face/edge "
        "queries per component (concentric+coincident = the bolt-in-hole seat) — then "
        "describe_assembly()/check_interference()/interface_check validate the fit "
        "(cad_guide('assembly')).\n"
        "ASSEMBLIES MOVE: solve_mates() seats an assembly; simulate_motion(mate_id, start, stop) "
        "then DRIVES one mate through a range, re-solving each step, to animate the mechanism and "
        "sweep interference across the motion (kinematics — cad_guide('motion')).\n"
        "DONE MEANS VERIFIED: before reporting a design as finished, call verify_battery(doc_id) "
        "(cad_guide('verify')) — it runs the full check battery and returns a narrated PASS/WARN/"
        "FAIL verdict. Never declare done on a FAIL.\n"
        "STANDARD PARTS FIRST: before custom geometry for any hardware (fasteners, bearings, "
        "sections, fittings, ECAD symbols), call part_search(query) then part_place(part_id) — "
        "standard parts are editable, sourced, and BOM-ready (cad_guide('parts')). "
        "Browse categories with parts_taxonomy(node_id) and narrow with faceted "
        "part_search(query, category_id, facets); for a plain-language need use "
        "part_source(need) — it returns scored candidates each with an artifact_plan "
        "(part_place | scrape_enqueue+import_artifact | make_standard_part). "
        "vendors_health() checks vendor adapters (needs_human_login → ask the human to "
        "vendor_login, never fabricate); cam_tool_import(part_id) puts a catalog cutting "
        "tool into the CAM tool library (cad_guide('sourcing')).\n"
        "REQUIREMENTS: capture design intent first with req_create(title, body) so verify_battery "
        "can check fulfilment (cad_guide('requirements')).\n"
        "MULTI-STEP MISSIONS: for 3+ sequential tool calls that can fail independently, use "
        "dag_plan → dag_run with checkpointing (cad_guide('dag')).\n"
        "BUILD-ORDER PLANNING: for complex parts/assemblies, call buildgraph_plan(plan_json) "
        "to convert a BUILD PLAN or dag/1 mission into an aicad.buildgraph/1 prerequisite graph "
        "(datum→sketch, boss→pierce, parts→mates order); validator findings are advisory items. "
        "buildgraph_status(run_id) reports node statuses. "
        "Use before executing multi-feature parts to catch missing prerequisites.\n"
        "UNKNOWNS: batch all user questions into ONE ask_user([{id, prompt, type, default}, ...]) "
        "call with defaults for every field (cad_guide('interaction')).\n"
        "BOM + COST: after placing parts, bom_rollup() → bom_price() for a costed BOM; "
        "cam_cost_estimate() for made-part cost seeds (cad_guide('sourcing'), cad_guide('cost')).\n"
        "CAM FEEDS & SPEEDS: feeds_speeds(material, tool_type, tool_dia, operation, machine=?) returns a "
        "table-driven rpm/feed/stepdown/stepover envelope (aicad.calc/1); pass machine= (haas_vf2, …) to "
        "clamp to a machine profile; thread= folds in tap_drill_mm. cam_check_feeds_envelope() "
        "returns an aicad.advisory/1 hint when params are out of range; tap_drill(thread) gives "
        "the standard tap-drill size for metric and unified threads. Call these BEFORE cam_add_operation "
        "to pick sane defaults, and use cam_check_feeds_envelope() AFTER to flag risky params before "
        "simulation. Materials: same ids as cam_materials() / cam_add_setup() (alu_6061, steel_1018, "
        "ti_6al4v, abs, pla, nylon_pa12, cast_iron_gray, inconel_718, …). "
        "cam_audit — deterministic G-code verification; run after cam_post.\n"
        "DOMAIN TOOLS: cam_* (machining toolpaths — cad_guide('cam')), ecad_* (schematic/PCB), "
        "piping_* (pipe networks — cad_guide('piping')), hydraulic_* (ISO 1219 circuits — "
        "cad_guide('hydraulic')), fw_* (firmware build/sim/flash — cad_guide('firmware')), "
        "struct_* (beam/frame analysis — cad_guide('structural')), "
        "code_check() (building-code compliance — cad_guide('code_compliance')).\n"
        "3D SKETCH + SKETCH PREVIEW: sketch3d_path_from_points (one-call routing path) -> "
        "sketch3d_add_constraints -> check_sketch3d (narrated dof/health) -> sketch3d_pipe "
        "(one-call tube along the path) — real engine `sketch3d` doc features (persist with "
        "the part) that sweeps consume as path/guide; render_sketch/sketch_preview_text "
        "to SEE or TEXT-read a 2D sketch before extruding; doc_sketch_contact_sheet for a "
        "multi-view part image + per-sketch health table in one call. "
        "See cad_guide('sketch3d') for the full workflow.\n"
        "NARRATION: use describe_toolpaths/describe_results/describe_assembly/describe_board/"
        "describe_project for compact text summaries instead of raw JSON dumps (cad_guide('narration')).\n"
        "REPAIR: if add_features returns ok=false on any feature, call regen_diagnose() to get "
        "a fix-ops card (cad_guide('regen')).\n"
        "DIFF + REVIEW: doc_diff_tool(rev_a, rev_b) for semantic change records (cad_guide('doc_diff')); "
        "design_review(doc_id) for a one-call geometry battery (cad_guide('design_review')); "
        "stackup_analyze(dims) for 1D worst-case + RSS tolerance stack-up over a dimension chain "
        "(cad_guide('tolerance_stackup')).\n"
        "DELIVERABLES: make_report/make_presentation/make_workbook build real office files "
        "(cad_guide('deliverables')). RESEARCH: research_web_search/research_fetch_page for "
        "look-it-up, never recall — the model never types a load-bearing number from memory.\n"
        "DESIGN LEDGER (Pillar 20): ledger_get(run_id, budget_tokens) / ledger_note(run_id, kind, "
        "payload) persist mission state (brief, decisions, dimensions, requirements, questions, "
        "stage_log, artifacts) across role handoffs — the anti-context-loss mechanism for "
        "24k-context local models. Every role should cold-start from ledger_get, not chat history.\n"
        "All cad_guide topics: overview, simulation, review, intake, gsl, cam, narration, parts, "
        "reverse, reveng, deliverables, requirements, dag, verify, interaction, sourcing, cost, "
        "spec, piping, hydraulic, firmware, structural, regen, code_compliance, serving, "
        "design_review, doc_diff, plm, tolerance_stackup, sketch3d.\n"
        "REBUILD FAILURE? A failed add_features batch may carry a `regen_diagnosis` fix-preview "
        "card (diagnosis + editable fix_ops + verifier verdict). Record your verdict with "
        "regen_resolve(card, 'accept'|'edit'|'reject') — accept/edit returns the fixed document; "
        "regen_diagnose(document_json) runs the same diagnosis on any failed doc directly.\n"
        "ECAD (schematic/PCB) tools are also available (ecad_*). Pre-fab host-board verification: "
        "ecad_run_hostcheck / ecad_run_sicheck / ecad_fpga_check / ecad_run_pdn; cross-sim handoffs "
        "via cad_guide('simulation') (ecad_thermal_handoff, heatsink_select, power_budget).\n"
        "CONTENT GOLDEN DEMO (T-FIN25): scripts/demo_sensor_node.py is the living E2E worked "
        "example (sensor-node board → verify ladder → firmware → thermal → heatsink → budget → "
        "make_report); see docs/FIN25_SENSOR_NODE_GOLDEN_DEMO.md and cad_guide('simulation').\n"
        + _soft_tail_for_profile()
)

mcp = FastMCP(
    "Chudware",
    instructions=_LITE_INSTRUCTIONS if _LITE else _FULL_INSTRUCTIONS,
)

# Tool registry (contracts/providers.md §6): tag every tool with {family, risk} and enforce
# a per-context allow-list at dispatch. `view(mcp, family)` stamps each register_*_tools()
# call site with its family and wraps its tools in the single shared dispatch guard. Default
# policy = everything allowed, so this is behavior-neutral until a DAG node / env sets a list.
from mcp_server import tool_registry as _reg

# Mechanical CAD + simulation (C++ feature engine via libaicad.dll).
# Per-family modules (T-RF8 split of the former cad_tools.py god file); all register
# under the stable registry family "cad".
from mcp_server.cad_doc_tools import register_cad_doc_tools
from mcp_server.cad_eval_tools import register_cad_eval_tools
from mcp_server.cad_render_tools import register_cad_render_tools
from mcp_server.cad_exchange_tools import register_cad_exchange_tools
from mcp_server.cad_sim_tools import register_cad_sim_tools
from mcp_server.cad_asm_tools import register_cad_asm_tools
from mcp_server.cad_drawing_tools import register_cad_drawing_tools
register_cad_doc_tools(_reg.view(mcp, "cad"))
register_cad_eval_tools(_reg.view(mcp, "cad"))
register_cad_render_tools(_reg.view(mcp, "cad"))
register_cad_exchange_tools(_reg.view(mcp, "cad"))
register_cad_sim_tools(_reg.view(mcp, "cad"))
register_cad_asm_tools(_reg.view(mcp, "cad"))
register_cad_drawing_tools(_reg.view(mcp, "cad"))

# Nested-tree profiler (mission master): engine_profile_tree drills a call-tree / phase-tree
# by path so an agent reads root + hot children first (context-frugal). Over prof/tree.{h,cpp}.
from mcp_server.profile_tools import register_profile_tools
register_profile_tools(_reg.view(mcp, "cad"))

# GSL — Geometry-Semantics Layer: compact named part outline (describe_part), face-adjacency
# graph (query_adjacency), and stable-id hover. The progressive-pull view of the geometry.
from mcp_server.gsl_tools import register_gsl_tools
register_gsl_tools(_reg.view(mcp, "gsl"))

# Recon — reverse-engineering: import a STEP/mesh → deterministic feature recognition → an
# EDITABLE parametric tree, verified against the source solid by boolean comparison
# (reverse_engineer / compare_parts / convert_face_to_sketch / project_into_sketch;
# guide: cad_guide("reverse")).
from mcp_server.recon_tools import register_recon_tools
register_recon_tools(_reg.view(mcp, "recon"))

# Intake — photo-measurement + manufacturing-clarification + first-try-fit. resolve/set process,
# request/submit photos, confirm critical dims, dfm_check, fit_report, make_fit_coupon.
from mcp_server.intake_tools import register_intake_tools
register_intake_tools(_reg.view(mcp, "intake"))

# Interaction — the ONE ask_user seam (Pillar 9, contracts/interaction.md): typed questions
# with suggested defaults surface as MCP elicitations; answers persist + replay via the shared
# interaction core (services/agent/interaction.py). Callers (intake clarify, DAG gates) migrate
# onto this in T-F4.
from mcp_server.interaction_tools import register_interaction_tools
register_interaction_tools(mcp)

# ECAD (schematic / PCB / native circuit sim) — per-family modules (T-RF8 split of the
# former ecad_tools.py god file); all register under the stable registry family "ecad".
from mcp_server.ecad_sch_tools import register_ecad_sch_tools
from mcp_server.ecad_pcb_tools import register_ecad_pcb_tools
from mcp_server.ecad_sim_tools import register_ecad_sim_tools
register_ecad_sch_tools(_reg.view(mcp, "ecad"))
register_ecad_pcb_tools(_reg.view(mcp, "ecad"))
register_ecad_sim_tools(_reg.view(mcp, "ecad"))

# ECAD -> thermal FEA handoff (T-FIN16, aicad.simhandoff/1 coupling ecad_to_thermal_fea):
# one call from a routed board to a solved board-level thermal study + Tj/limits verify.
from mcp_server.ecad_thermal import register_ecad_thermal_tools
register_ecad_thermal_tools(_reg.view(mcp, "ecad"))

# ECAD -> CHT airflow handoff (T-FIN19, aicad.simhandoff/1 coupling ecad_to_cht):
# board + powered components as separate CHT solid regions -> OpenFOAM conjugate heat
# transfer, verifying the T-FIN16 preset-h film-coefficient assumption.
from mcp_server.ecad_thermal import register_ecad_airflow_tools
register_ecad_airflow_tools(_reg.view(mcp, "ecad"))
# ECAD -> vibration reliability screen (T-FIN21, aicad.simhandoff/1 coupling
# ecad_to_structural_fea): mounted-board modal solve + excitation-band flagging +
# Steinberg deflection-limit solder-joint risk screen (cited heuristic).
from mcp_server.ecad_reliability import register_ecad_reliability_tools
register_ecad_reliability_tools(_reg.view(mcp, "ecad"))
# Heatsink selection loop (T-FIN17): parts-library + parametric candidates re-solved
# to a Tj/case target on the cached ecad_thermal_handoff study (verify envelope
# coupling ecad_heatsink_to_thermal_fea).
from mcp_server.heatsink import register_heatsink_tools
register_heatsink_tools(_reg.view(mcp, "ecad"))

# CAM (computer-aided machining): toolpaths, feeds & speeds, material-removal sim, G-code post.
from mcp_server.cam_tools import register_cam_tools
register_cam_tools(_reg.view(mcp, "cam"))
from mcp_server.cam_audit import register_cam_audit_tools
register_cam_audit_tools(_reg.view(mcp, "cam"))

# Doc diff — semantic feature-level diff between two document revisions (T-H2).
from mcp_server.doc_diff_tools import register_doc_diff_tools
register_doc_diff_tools(mcp)
# PLM vault (Pillar 19, aicad.plm/1): hidden git-backed version history —
# Project/History/Checkpoint/Release/Restore. Schema: contracts/plm.md.
# Engine: services/plm/. Zero user-facing git vocabulary (contract §8).
from mcp_server.plm_tools import register_plm_tools
register_plm_tools(mcp)
# CAD link graph (1D): cad_graph traversal + cad_graph_link (AI overlay) +
# assembly_plan_review. Over services/plm/cad_graph.py + graph_links.py.
from mcp_server.graph_tools import register_graph_tools
register_graph_tools(_reg.view(mcp, "cad"))
# Requirements (aicad.req/1): CRUD + markdown projection for design briefs.
# Schema: contracts/requirements.md  Brain: services/requirements/requirements.py
from mcp_server.req_tools import register_req_tools
register_req_tools(mcp)

# Layout-first planning helper (T-BL50) — capture envelope + critical dims + interfaces
# BEFORE modeling; coverage gate (layout_coverage) surfaces in design_review.
from mcp_server.layout_plan_tools import register_layout_plan_tools
register_layout_plan_tools(mcp)

# Verify battery (T-H4) — deterministic check dispatcher for RL reward + CI gate.
from mcp_server.verify_tools import register_verify_tools
register_verify_tools(mcp)

# Research (Pillar 8): look-it-up-instead-of-recall. web_search + fetch_page return the
# provenance-stamped answer envelope; per-user local cache; offline-safe degradation.
# T-W3: also registers fit_calc + recommend_fit (ISO 286, aicad.calc/1 envelope).
# Rule: call fit_calc BEFORE hole_sizing / bore decisions; recommend_fit to choose the class.
from mcp_server.research_tools import register_research_tools
register_research_tools(mcp)

# Dimension scheme planner (T-W3 / Pillar 20): suggest_dimension_scheme reads a sketch
# entity roster and emits an ordered process-aware dimension plan (IT-grade tolerances,
# bolt-pattern datum rules, needs_user_confirm flags for function-critical dims).
from mcp_server.dimension_scheme_tools import register_dimension_scheme_tools
register_dimension_scheme_tools(mcp)

# Deliverables — spec-driven office artifacts (contract deliverables/1): make_presentation /
# make_report / make_workbook build real .pptx/.docx/.pdf/.xlsx from a JSON spec with the
# provenance rule (the model never types a load-bearing number); render_document previews any
# supported file; list_artifacts reads the artifact registry. Guide: cad_guide('deliverables').
from deliverables import register_deliverables_tools
register_deliverables_tools(mcp)

# Sourcing — unified BOM roll-up (made/bought) + approved-provider price resolution (workstream B).
from mcp_server.sourcing_tools import register_sourcing_tools
register_sourcing_tools(mcp)

# Cost-out — made-part cost model (material + machining) + approved quote-out APIs (workstream B).
from mcp_server.cost_tools import register_cost_tools
register_cost_tools(mcp)

# Spec-sheet matching — deterministic part-number → standard-part model (BOLTS/KiCad, workstream B).
from mcp_server.spec_tools import register_spec_tools
register_spec_tools(mcp)

# Piping (routed pipe networks): steady-state 1-D hydraulic solve + pressure-drop report.
from mcp_server.piping_tools import register_piping_tools
register_piping_tools(mcp)

# Hydraulic (ISO 1219 circuit design + component sizing).
from mcp_server.hydraulic_tools import register_hydraulic_tools
register_hydraulic_tools(mcp)

# Photo capture + photogrammetry entry path for reverse_engineer (Pillar 10 v2).
# DEFAULT OFF — start_capture_session MCP tool starts the local listener on demand.
from mcp_server.recon_tools import register_photo_recon_tools
register_photo_recon_tools(mcp)

# Firmware (embedded compile loop): fw_new_project / fw_build / fw_sources / fw_sim_run — auto-selects
# the MCU target, generates a pinmap.h header from the linked ECAD board's netlist (T-C2), and runs
# the ELF through Renode or QEMU with assertion checks (T-C3).
from mcp_server.firmware_tools import register_firmware_tools
register_firmware_tools(mcp)

# Full-wave EM (eigenmode / driven / timedomain) — builtin-fdfd + analytic adapters;
# openEMS when installed. Compact summary-first envelopes (field arrays behind detail=).
from mcp_server.em_tools import register_em_tools
register_em_tools(_reg.view(mcp, "em"))

# PCB antenna design + EM bridge (mission master): ecad_antenna_design (patch/monopole/IFA/PIFA
# calculators + board copper geometry) + ecad_antenna_analyze (analytic resonance always, openEMS
# full-wave S11 when installed). Cross-checks the IPC-2141 feed impedance.
from mcp_server.antenna_tools import register_antenna_tools
register_antenna_tools(_reg.view(mcp, "em"))


from mcp_server.power_budget import register_power_budget_tools
register_power_budget_tools(_reg.view(mcp, "ecad"))

# Structural (T-D2): PyNite member forces & deflections for aicad.structure/1 docs.
from mcp_server.structural_tools import register_structural_tools
register_structural_tools(mcp)

# Preflight (T-W12, Pillar 20 §2.10): dry-run plan validation on shadow doc clones.
# preflight_ops is read-only; never mutates the live doc.
from mcp_server.preflight_tools import register_preflight_tools
register_preflight_tools(mcp)

# Unified parts library (T-P1): federated search across all standard-parts catalogs.
# part_search / part_get over the aicad.parts/1 FTS hub (contracts/parts.md).
from mcp_server.parts_tools import register_parts_tools
register_parts_tools(mcp)
# Adapter serving (T-H9): per-insertion-point LoRA routing table — status ("which brain
# answered"), canary dial, always-safe rollback, and the Erik-ratified gated promotion
# (evidence + human sign-off; no auto-promote). Pure-Python, no core dependency.
from mcp_server.serving_tools import register_serving_tools
register_serving_tools(mcp)
# Regen diagnosis (T-H11 / IP-18): on a rebuild failure, diagnose + propose editable fix-ops as
# an accept/edit/reject card, logged as an IP-18 correction trace. LIVE since T-BL22:
# add_features attaches the card on rebuild failure; regen_resolve records the accept/edit/
# reject action trace (journaled to CACHE/telemetry/events.jsonl).
from mcp_server.regen_tools import register_regen_tools
register_regen_tools(mcp)
# Code compliance (T-D4): deterministic rule engine + agentic RAG for building-code checks.
from mcp_server.code_compliance_tools import register_code_compliance_tools
register_code_compliance_tools(mcp)
# ---------------------------------------------------------------------------
# Orchestration / supervision families — the DAG supervisor, mission phase
# machine, build-order planner, foreman, ledger, and workspace lease registry.
# These are the "pre-designed plans / workflows" scaffolding tuned for small
# local models. The `lite` profile omits them so a capable self-driving model
# runs unconstrained (all real capabilities above stay registered).
# ---------------------------------------------------------------------------
if not _LITE:
    # DAG orchestrator MCP tools (T-A4): dag_plan / dag_run / dag_status / dag_approve / dag_abort.
    # These five tools expose the services/dag executor to MCP clients so a model or UI can
    # start/resume/inspect/gate DAG missions without a bespoke integration layer.
    from services.dag.mcp_tools import register_dag_tools
    register_dag_tools(mcp)
    # DAG replan (T-F12): dag_replan splices new nodes into a running checkpointed DAG run.
    # Validates end_condition + retry_budget on every spliced node (DoD enforcement), merges
    # into the persisted graph sidecar, and resumes via executor.resume().
    from services.dag.mcp_tools import register_dag_replan_tools
    register_dag_replan_tools(mcp)
    # Mission pipeline (T-W19): mission_start / mission_status / replan_request.
    # The copilot's mission front door — INTAKE→DELIVER phase machine on top of the
    # ledger + roles + buildgraph + DAG.  mission_status renders phase state from the
    # ledger stage_log (what mission control + the GUI consume); replan_request routes
    # an aicad.replan/1 request per contract §17.  The in-app path is
    # services.agent.loop.run_mission.
    from services.mcp.mission_tools import register_mission_tools
    register_mission_tools(mcp)
    # Design ledger (T-W1, Pillar 20): persistent mission state (aicad.ledger/1).
    # ledger_get / ledger_note let any agent role read or write the design ledger without
    # holding the full chat history — the anti-context-loss mechanism for 24k local models.
    # ledger_get is risk-class READ; ledger_note is WRITE (auto-classified by tool_registry).
    from services.mcp.ledger_tools import register_ledger_tools
    register_ledger_tools(_reg.view(mcp, "ledger"))
    # Build-order DAG planner (T-W15): buildgraph_plan / buildgraph_status.
    # LLM-free planner converts BUILD PLAN / dag/1 mission into aicad.buildgraph/1 graph
    # with prerequisite edges; validator findings are aicad.advisory/1 items.
    # ready_nodes() surface for T-W16 multi-doc fan-out.
    from services.agent.buildgraph.mcp_tools import register_buildgraph_tools
    register_buildgraph_tools(mcp)
    # Advisory engine / foreman (T-W5, Pillar 20): advise / stage_set / advisory_dispute.
    # advise runs a stage's read-only check battery over a doc on demand (aicad.advisory/1);
    # the in-app loop.py + dag node runner already run the foreman automatically. advise is
    # risk-class READ; stage_set / advisory_dispute are WRITE (ledger-logged, §9.3).
    from services.mcp.advisor_tools import register_advisor_tools
    register_advisor_tools(_reg.view(mcp, "advisor"))
    # Workspace (T-W16, Pillar 20 §2.14): multi-doc lease registry + per-doc event streams.
    # workspace_status / workspace_doc_events are risk-class READ (named in _READ_EXACT);
    # no write-lease required to poll status.
    from services.mcp.workspace_tools import register_workspace_tools
    register_workspace_tools(_reg.view(mcp, "workspace"))

# Design review battery (T-F12): one-call battery dispatching through DISPATCH_TABLE
# (verify.py) + battery._get_fn for feature_overlap/dfm.  Returns a narrated verdict
# card per contracts/narration.md — never a raw JSON dump.  On-demand helper (kept in lite).
from services.mcp.design_review_tools import register_design_review_tools
register_design_review_tools(mcp)
# design_recipe — NL intent → ordered tool steps (always-on helper).
from services.agent.design_recipe import design_recipe as _design_recipe_fn
_ws = _reg.view(mcp, "workspace")

@_ws.tool()
def design_recipe(query: str = "", max_results: int = 3) -> dict:
    """Map a natural-language intent to an ordered tool recipe (or list the catalog).

    Empty query returns {ok, recipes:[{id,title,summary}...]}. Non-empty returns
    ranked full recipes with steps[{tool,params,note}] and verify[].
    """
    return _design_recipe_fn(query=query, max_results=max_results)

# Interface alignment checker (T-BL49): interface_check inspects mating component pairs for
# hole-pattern registration, bore coaxiality, contact-plane flushness, and fastener pass-through
# — the layout errors that pass check_interference (no clash) but are still wrong.  Reuses
# doc_query (per-body faces), asm_world_bodies, cam_extract holes, research_thread_spec.
from mcp_server.interface_check_tools import register_interface_check_tools
register_interface_check_tools(mcp)
# Tolerance stack-up analysis (T-BL08): 1D worst-case + RSS over a chain of feature
# dimensions.  Pure-Python; no C++ engine required.  See cad_guide('tolerance_stackup').
from services.mcp.tolerance_tools import register_tolerance_tools
register_tolerance_tools(mcp)
# Mechanical engineering checks (T-BL18): AGMA gear tooth stress + ISO 281 bearing life.
from mcp_server.mechanical_tools import register_mechanical_tools
register_mechanical_tools(mcp)
# 3D sketch + sketch preview tools: sketch3d_new/add_curves/get/clear/as_sweep_guide,
# render_sketch, sketch_preview_text, doc_sketch_contact_sheet, sketch3d_preview.
# These give an AI driver the same 3D-sketch and 2D-sketch-preview capabilities the GUI
# provides in the Sketch3D workspace + sketch overlay (no C++ build required — Python only).
from mcp_server.sketch3d_tools import register_sketch3d_tools
register_sketch3d_tools(_reg.view(mcp, "cad"))

# ASCII / plaintext 2D representations for non-vision models (owner item 5):
# render_board/sketch/schematic/sim_ascii, apply_ascii_edits, experimental render_ascii.
from mcp_server.ascii2d import register_ascii2d_tools
register_ascii2d_tools(_reg.view(mcp, "gsl"))

# Reverse Engineering MCP family (contracts/reveng.md §8) — per-family modules (T-RF8
# split of the former reveng_tools.py god file); both register under "reveng".
from services.mcp.reveng_session_tools import register_reveng_session_tools
from services.mcp.reveng_build_tools import register_reveng_build_tools
register_reveng_session_tools(_reg.view(mcp, "reveng"))
register_reveng_build_tools(_reg.view(mcp, "reveng"))

# Family retags so Settings → AI → Tools toggles match real tool ownership.
# FEA/CFD/motion ship inside register_cad_tools but belong under ``sim``;
# bare register_* calls (research/plm/dag/mission/buildgraph) get families here.
_FAMILY_REASSIGN: dict[str, tuple[str, ...]] = {
    "sim": (
        "run_fea_static", "run_fea_modal", "run_fea_fatigue", "run_fea_thermal",
        "run_fea_buckling", "run_fea_contact", "run_cfd_flow", "run_cfd_accurate",
        "simulate_motion",
    ),
    "plm": (
        "plm_init", "plm_open", "plm_save", "plm_checkpoint", "plm_release",
        "plm_history", "plm_restore", "plm_status", "plm_compare",
        "plm_set_ai_mode", "plm_item_set", "plm_item_add", "plm_where_used",
        "plm_timeline", "plm_rollback", "plm_add_remote", "plm_sync",
        "plm_lock", "plm_unlock", "plm_link_repo", "plm_plan",
    ),
    "research": (
        "research_web_search", "research_fetch_page", "research_material_lookup",
        "research_unit_convert", "research_standard_section",
        "research_datasheet_fetch", "research_thread_spec", "research_eng_calc",
        "fit_calc", "recommend_fit",
    ),
    "mission": (
        "dag_plan", "dag_run", "dag_status", "dag_approve", "dag_abort",
        "dag_replan", "buildgraph_plan", "buildgraph_status",
        "mission_start", "mission_status", "replan_request",
    ),
}
_cat = _reg.catalog()
for _fam, _names in _FAMILY_REASSIGN.items():
    for _n in _names:
        if _n in _cat:
            _cat[_n]["family"] = _fam


def _install_clean_stdout():
    """Point the process's native stdout (fd 1) at a log file and return a private handle to
    the REAL stdout pipe for the MCP transport.

    Why: the C++ core (OCCT) prints statistics straight to fd 1 — which is the MCP JSON-RPC
    channel. Those bytes corrupt the protocol, and on a slow-drained pipe a big native write
    can even BLOCK and hang the call ("request timed out"). Redirecting fd 1 to a file once,
    here at startup, makes that impossible without any per-call fd toggling (which would race
    the transport's off-thread response writer). The transport is handed an explicit stream
    bound to the preserved pipe. Returns an ``anyio`` async file, or None if the dup dance
    isn't supported (then we fall back to the default transport).
    """
    try:
        import anyio
        from io import FileIO, TextIOWrapper

        real_fd = os.dup(1)  # the genuine JSON-RPC pipe to the MCP client
        log_path = os.path.join(tempfile.gettempdir(), "ai-cad-mcp-native.log")
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(log_fd, 1)  # fd 1 -> log; sys.stdout follows, so stray prints are harmless too
        os.close(log_fd)
        real_stream = anyio.wrap_file(TextIOWrapper(FileIO(real_fd, mode="w"), encoding="utf-8"))
        return real_stream
    except Exception:
        return None


def _warm_core() -> None:
    """Load libaicad.dll up front (in the background) so the FIRST tool call doesn't eat the
    cold load (DLL + OCCT/gmsh deps can take many seconds) inside the client's call timeout."""
    # Arm ECAD autosave + recover any ECAD docs from a previous (possibly killed) session FIRST.
    # ECAD sessions live in the C++ core now (T-RF20), but recovery reads plain-JSON autosave files
    # and re-mints sessions via ecad_open, so it never blocks on the full DLL warm and an ECAD
    # recovery succeeds even if the heavy OCCT deps are slow. ECAD docs live only in memory, so a
    # transport-timeout kill of a heavy ecad_* tool would otherwise lose every open schematic/board.
    try:
        from mcp_server.ecad_common import enable_ecad_autosave, recover_ecad_autosaved
        enable_ecad_autosave()
        recover_ecad_autosaved()
    except Exception:
        pass
    # Pre-warm the heavy PURE-PYTHON import stacks the ECAD tools lazy-import on first use, so the
    # FIRST ecad_simulate / ecad_to_3d call doesn't pay a cold import INSIDE the client's transport
    # window. This matters more than usual here: a cold-import overrun trips -32001, which KILLS the
    # server, so the import never gets cached — every retry re-pays it and times out again (a vicious
    # loop). Importing them once in the background breaks that. Best-effort; never blocks the DLL warm.
    # T-RF20: the SPICE simulator is C++ now (sim_netlist_to_circuit / sim_spice RPCs) — no
    # Python warm needed here; the core_bridge warm below covers the DLL + dispatch.
    try:
        # OCP/build123d 3D kernel (ecad_to_3d) — can be many seconds cold.
        from services.ecad.threed import board_to_step  # noqa: F401  # STAYS (T-RF20): no C++ counterpart
    except Exception:
        pass
    try:
        from mcp_server import core_bridge
        core_bridge.available()
        # Recover any docs auto-saved before a previous restart so a client-timeout kill doesn't
        # permanently drop the user's open parts (they reload with fresh ids; list_parts shows them).
        try:
            from mcp_server.cad_common import (recover_autosaved, start_periodic_autosave,
                                               sweep_stale_handoff)
            # Prune dead-session handoff fragments FIRST: if the last publisher crashed
            # (atexit never ran), docs.json still advertises its docs and the GUI shows
            # ghost mirror tabs until someone re-merges — which is us, right now.
            sweep_stale_handoff()
            recover_autosaved()
            # Arm the periodic autosave timer AFTER recovery (so we don't re-save stale files).
            start_periodic_autosave()
        except Exception:
            pass
    except Exception:
        pass
    # Install crash-bundle handler AFTER recovery so a crash on first load still
    # writes a useful bundle with the autosave references from this session.
    try:
        from mcp_server import crash_bundle
        crash_bundle.install()
    except Exception:
        pass


def _reexec_with_fast_malloc() -> None:
    """Re-exec THIS process with a fast allocator preloaded (tcmalloc), then return in the child.

    OCCT's booleans/meshing are allocation-heavy; tcmalloc cuts modeling time ~5-10% vs glibc
    malloc (measured, tools/perf_regression.py) with no logic change and ctest still green. The
    engine is dlopened via ctypes, so linking an allocator into libaicad wouldn't override the
    already-resolved glibc malloc — LD_PRELOAD before the process allocates is the only reliable
    way, hence this early self-reexec. Opt out with AICAD_NO_FAST_MALLOC=1. Loop-guarded + fully
    best-effort: any problem just continues on the default allocator.
    """
    if os.name == "nt":
        return
    if os.environ.get("AICAD_NO_FAST_MALLOC") or os.environ.get("_AICAD_MALLOC_REEXEC"):
        return
    pre = os.environ.get("LD_PRELOAD", "")
    if "tcmalloc" in pre or "jemalloc" in pre:
        return  # an allocator is already preloaded (by a launcher) — respect it
    import ctypes.util
    lib = None
    for cand in ("/lib64/libtcmalloc_minimal.so.4", "/usr/lib64/libtcmalloc_minimal.so.4",
                 "/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4"):
        if os.path.exists(cand):
            lib = cand
            break
    if lib is None:
        found = ctypes.util.find_library("tcmalloc_minimal") or ctypes.util.find_library("jemalloc")
        lib = found
    if not lib:
        return
    try:
        env = dict(os.environ)
        env["LD_PRELOAD"] = (lib + (":" + pre if pre else ""))
        env["_AICAD_MALLOC_REEXEC"] = "1"  # sentinel: never re-exec twice
        os.execve(sys.executable, [sys.executable] + sys.argv, env)
    except Exception:  # noqa: BLE001
        pass  # exec failed -> fall through and run on the default allocator


def _start_orphan_watchdog() -> None:
    """Exit when the client that spawned us is gone.

    stdio MCP servers are children of their client (Claude Desktop/Code, Cursor,
    the GUI copilot). When that parent dies without closing our pipes cleanly —
    crash, SIGKILL, session teardown bugs — this process used to linger forever,
    holding a live connection into aicad-hostd: the daemon never idle-exited,
    its docs never GC'd, and orphaned bridges piled up one per dead session
    (observed: 5 on one box). The parent can't be relied on to clean us up, so
    we watch it ourselves: if our ppid changes (parent died; we were reparented
    to init/a subreaper), exit. The engine state lives in hostd, not here, so
    an abrupt exit loses nothing. Opt out with AICAD_ORPHAN_WATCHDOG=0."""
    if os.name == "nt" or os.environ.get("AICAD_ORPHAN_WATCHDOG") == "0":
        return  # Windows has no reparenting signal; Job Objects handle teardown
    parent = os.getppid()
    if parent <= 1:
        return  # already init-parented (launched by a supervisor) — no signal to watch

    def _watch() -> None:
        while True:
            time.sleep(15.0)
            if os.getppid() != parent:
                print("[mcp_server] parent client died — exiting orphan bridge",
                      file=sys.stderr)
                os._exit(0)

    threading.Thread(target=_watch, name="aicad-orphan-watchdog", daemon=True).start()


def main() -> None:
    _reexec_with_fast_malloc()  # fast-allocator preload (best-effort, before any core alloc)
    _start_orphan_watchdog()  # a dead client must never leave a zombie bridge pinning hostd
    real_stream = _install_clean_stdout()
    # Warm the core only AFTER fd 1 is redirected, so its load-time prints land in the log,
    # never on the protocol pipe.
    threading.Thread(target=_warm_core, name="aicad-warm", daemon=True).start()

    if real_stream is None:
        mcp.run()  # fallback (fd redirect unsupported) — stdio transport on sys.stdout
        return

    import anyio
    from mcp.server.stdio import stdio_server

    async def _serve() -> None:
        async with stdio_server(stdout=real_stream) as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream, write_stream,
                mcp._mcp_server.create_initialization_options(),
            )

    anyio.run(_serve)


if __name__ == "__main__":
    main()
