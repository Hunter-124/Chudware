"""AI-CAD MCP server.

Exposes the AI-CAD modeling engine — the SAME C++ core (`libaicad.dll`) the desktop app
runs — as MCP tools, so an MCP client (Claude in cowork/chat or Claude Code) can drive
real parametric CAD and simulation: build an editable feature tree with the plain-English
geometry vocabulary, render an annotated image to self-review, run FEA (static/modal) and
wind-tunnel CFD, make 2D drawings, pull standard hardware, and export STEP/STL/3MF/etc.

This is parity with the in-app AI agent — except YOU (the model calling these tools) are the
brain, so call `cad_guide()` first to load the engine's op vocabulary, then model → render →
iterate → simulate exactly like the app does.

Mechanical CAD + simulation tools live in `cad_tools` (driven through `core_bridge`, the
ctypes bridge to the staged DLL). ECAD (schematic/PCB) tools live in `ecad_tools`.
Runs over stdio; the desktop host registers it into the machine's MCP client configs.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "AI-CAD",
    instructions=(
        "Real parametric CAD + simulation on the AI-CAD C++ engine (same kernel as the "
        "desktop app). You are the design brain.\n"
        "START by calling cad_guide() — it returns the engine's modeling op vocabulary and "
        "worked examples; cad_guide('simulation') and cad_guide('review') cover FEA/CFD and "
        "how to read the annotated render.\n"
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
        "DONE MEANS VERIFIED: before reporting a design as finished, call verify_battery(doc_id) "
        "(cad_guide('verify')) — it runs the full check battery and returns a narrated PASS/WARN/"
        "FAIL verdict. Never declare done on a FAIL.\n"
        "STANDARD PARTS FIRST: before custom geometry for any hardware (fasteners, bearings, "
        "sections, fittings, ECAD symbols), call part_search(query) then part_place(part_id) — "
        "standard parts are editable, sourced, and BOM-ready (cad_guide('parts')).\n"
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
        "CAM FEEDS & SPEEDS: feeds_speeds(material, tool_type, tool_dia, operation) returns a "
        "table-driven rpm/feed/stepdown/stepover envelope (aicad.calc/1); cam_check_feeds_envelope() "
        "returns an aicad.advisory/1 hint when params are out of range; tap_drill(thread) gives "
        "the standard tap-drill size for metric and unified threads. Call these BEFORE cam_add_operation "
        "to pick sane defaults, and use cam_check_feeds_envelope() AFTER to flag risky params before "
        "simulation. Materials: same ids as cam_materials() / cam_add_setup() (alu_6061, steel_1018, "
        "ti_6al4v, abs, pla, nylon_pa12, cast_iron_gray, inconel_718, …).\n"
        "DOMAIN TOOLS: cam_* (machining toolpaths — cad_guide('cam')), ecad_* (schematic/PCB), "
        "piping_* (pipe networks — cad_guide('piping')), hydraulic_* (ISO 1219 circuits — "
        "cad_guide('hydraulic')), fw_* (firmware build/sim/flash — cad_guide('firmware')), "
        "struct_* (beam/frame analysis — cad_guide('structural')), "
        "code_check() (building-code compliance — cad_guide('code_compliance')).\n"
        "NARRATION: use describe_toolpaths/describe_results/describe_assembly/describe_board/"
        "describe_project for compact text summaries instead of raw JSON dumps (cad_guide('narration')).\n"
        "REPAIR: if add_features returns ok=false on any feature, call regen_diagnose() to get "
        "a fix-ops card (cad_guide('regen')).\n"
        "DIFF + REVIEW: doc_diff_tool(rev_a, rev_b) for semantic change records (cad_guide('doc_diff')); "
        "design_review(doc_id) for a one-call geometry battery (cad_guide('design_review')).\n"
        "DELIVERABLES: make_report/make_presentation/make_workbook build real office files "
        "(cad_guide('deliverables')). RESEARCH: research_web_search/research_fetch_page for "
        "look-it-up, never recall — the model never types a load-bearing number from memory.\n"
        "DESIGN LEDGER (Pillar 20): ledger_get(run_id, budget_tokens) / ledger_note(run_id, kind, "
        "payload) persist mission state (brief, decisions, dimensions, requirements, questions, "
        "stage_log, artifacts) across role handoffs — the anti-context-loss mechanism for "
        "24k-context local models. Every role should cold-start from ledger_get, not chat history.\n"
        "All cad_guide topics: overview, simulation, review, intake, gsl, cam, narration, parts, "
        "reverse, deliverables, requirements, dag, verify, interaction, sourcing, cost, spec, "
        "piping, hydraulic, firmware, structural, regen, code_compliance, serving, design_review, "
        "doc_diff, plm."
        "REBUILD FAILURE? A failed add_features batch may carry a `regen_diagnosis` fix-preview "
        "card (diagnosis + editable fix_ops + verifier verdict). Record your verdict with "
        "regen_resolve(card, 'accept'|'edit'|'reject') — accept/edit returns the fixed document; "
        "regen_diagnose(document_json) runs the same diagnosis on any failed doc directly.\n"
        "ECAD (schematic/PCB) tools are also available (ecad_*)."
    ),
)

# Tool registry (contracts/providers.md §6): tag every tool with {family, risk} and enforce
# a per-context allow-list at dispatch. `view(mcp, family)` stamps each register_*_tools()
# call site with its family and wraps its tools in the single shared dispatch guard. Default
# policy = everything allowed, so this is behavior-neutral until a DAG node / env sets a list.
from mcp_server import tool_registry as _reg

# Mechanical CAD + simulation (C++ feature engine via libaicad.dll).
from mcp_server.cad_tools import register_cad_tools
register_cad_tools(_reg.view(mcp, "cad"))

# GSL — Geometry-Semantics Layer: compact named part outline (describe_part), face-adjacency
# graph (query_adjacency), and stable-id hover. The progressive-pull view of the geometry.
from mcp_server.gsl_tools import register_gsl_tools
register_gsl_tools(_reg.view(mcp, "gsl"))

# Recon — reverse-engineering: import a STEP/mesh → deterministic feature recognition → an
# EDITABLE parametric tree, verified against the source solid by boolean comparison
# (reverse_engineer / compare_parts / convert_face_to_sketch; guide: cad_guide("reverse")).
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

# ECAD (schematic / PCB / native circuit sim).
from mcp_server.ecad_tools import register_ecad_tools
register_ecad_tools(_reg.view(mcp, "ecad"))

# CAM (computer-aided machining): toolpaths, feeds & speeds, material-removal sim, G-code post.
from mcp_server.cam_tools import register_cam_tools
register_cam_tools(_reg.view(mcp, "cam"))

# Doc diff — semantic feature-level diff between two document revisions (T-H2).
from mcp_server.doc_diff_tools import register_doc_diff_tools
register_doc_diff_tools(mcp)
# Requirements (aicad.req/1): CRUD + markdown projection for design briefs.
# Schema: contracts/requirements.md  Brain: engine_oracle/requirements/requirements.py
from mcp_server.req_tools import register_req_tools
register_req_tools(mcp)

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
# Design review battery (T-F12): one-call battery dispatching through DISPATCH_TABLE
# (verify.py) + battery._get_fn for feature_overlap/dfm.  Returns a narrated verdict
# card per contracts/narration.md — never a raw JSON dump.
from services.mcp.design_review_tools import register_design_review_tools
register_design_review_tools(mcp)
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
    # ECAD is pure Python with no DLL dependency, so this never blocks on the C++ load and an ECAD
    # recovery succeeds even if libaicad.dll is unavailable. ECAD docs live only in memory, so a
    # transport-timeout kill of a heavy ecad_* tool would otherwise lose every open schematic/board.
    try:
        from mcp_server.ecad_tools import enable_ecad_autosave, recover_ecad_autosaved
        enable_ecad_autosave()
        recover_ecad_autosaved()
    except Exception:
        pass
    # Pre-warm the heavy PURE-PYTHON import stacks the ECAD tools lazy-import on first use, so the
    # FIRST ecad_simulate / ecad_to_3d call doesn't pay a cold import INSIDE the client's transport
    # window. This matters more than usual here: a cold-import overrun trips -32001, which KILLS the
    # server, so the import never gets cached — every retry re-pays it and times out again (a vicious
    # loop). Importing them once in the background breaks that. Best-effort; never blocks the DLL warm.
    try:
        # scipy-backed SPICE simulator (ecad_simulate) — ~0.4 s cold.
        from engine.sim.spice import netlist_to_spice_circuit  # noqa: F401
        from engine.sim.circuit import simulate as _sim_warm  # noqa: F401
    except Exception:
        pass
    try:
        # OCP/build123d 3D kernel (ecad_to_3d) — can be many seconds cold.
        from engine.ecad.threed import board_to_step  # noqa: F401
    except Exception:
        pass
    try:
        from mcp_server import core_bridge
        core_bridge.available()
        # Recover any docs auto-saved before a previous restart so a client-timeout kill doesn't
        # permanently drop the user's open parts (they reload with fresh ids; list_parts shows them).
        try:
            from mcp_server.cad_tools import recover_autosaved, start_periodic_autosave
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


def main() -> None:
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
