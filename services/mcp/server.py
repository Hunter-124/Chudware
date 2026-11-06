"""Chudware MCP server.

Exposes the Chudware modeling engine — the SAME C++ core (`libchudware.dll`) the desktop app
runs — as MCP tools, so an MCP client (Claude in cowork/chat or Claude Code) can drive
real parametric CAD and simulation: build an editable feature tree with the plain-English
geometry vocabulary, render an annotated image to self-review, run FEA (static/modal) and
wind-tunnel CFD, make 2D drawings, pull standard hardware, and export STEP/STL/3MF/etc.

This is parity with the in-app AI agent — except YOU (the model calling these tools) are the
brain. `cad_guide()` lists focused topics; request only the needed topic, then model → render →
iterate → simulate exactly like the app does.

Mechanical CAD + simulation tools live in the `cad_*_tools` family modules (driven through
`core_bridge`, the ctypes bridge to the staged DLL). ECAD (schematic/PCB) tools live in the
`ecad_*_tools` family modules.
Runs over stdio; the desktop host registers it into the machine's MCP client configs.
"""
from __future__ import annotations

import atexit
import os
import signal
import sys
import tempfile
import threading
import time

from mcp.server.fastmcp import FastMCP

# Profile selects how much scaffolding the server exposes + how prescriptive its instructions
# are. `full` (default) is the supervised experience tuned for small local models: the long
# how-to instructions plus the DAG/mission/buildgraph/advisory/ledger/workspace orchestration
# families. `lite` is for capable models (e.g. mythos) that drive themselves: it keeps EVERY
# capability + calculator + sourcing helper but drops the orchestration/supervision families
# and swaps in a short, non-prescriptive instruction block, so the model runs freer and we
# don't spend input tokens on a workflow it won't follow.  Switch with CHUDWARE_MCP_PROFILE=lite.
# WI-16: capability letter also influences the default — C (frontier) prefers lite; A/B full.
# Explicit CHUDWARE_MCP_PROFILE always wins. CHUDWARE_CAPABILITY=A|B|C or CHUDWARE_TIER can select.
_PROFILE_ENV = os.environ.get("CHUDWARE_MCP_PROFILE", "").strip().lower()


def _capability_letter_from_env() -> str:
    return (os.environ.get("CHUDWARE_CAPABILITY") or "").strip().upper()


def _resolve_mcp_profile() -> str:
    """full | lite — explicit env wins; else capability letter C → lite (WI-16)."""
    if _PROFILE_ENV in ("full", "lite"):
        return _PROFILE_ENV
    letter = _capability_letter_from_env()
    if letter not in ("A", "B", "C"):
        # Best-effort: map active tier → capability letter without hard-importing
        # the full agent package at MCP import time when unavailable.
        try:
            from services.agent.capability import resolve_capability  # noqa: PLC0415
            tier = (os.environ.get("CHUDWARE_TIER") or "").strip()
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
# full + suggestive block when CHUDWARE_CAPABILITY=B. The buildgraph/plan-template
# sentence is appended only in the full profile: lite does not register buildgraph_*.
_SOFT_A = (
    "\n[CAPABILITY_SOFT] A — MANDATORY DISCIPLINE (local scaffold)\n"
    "calculate-before-model: never invent load-bearing numbers — run fit_calc / "
    "research_* / the matching calculator before dimensioned features.\n"
    "datum-first; fillet-last within a region; read-back after every mutation "
    "(describe_part / check_sketch) before the next mutate; "
)
_SOFT_A_FULL_TAIL = "use plan/buildgraph templates rather than free-forming the tree.\n"
_SOFT_A_LITE_TAIL = "do not free-form the tree without a feature plan.\n"
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
    letter = _capability_letter_from_env()
    if letter not in ("A", "B", "C"):
        letter = "C" if _LITE else "A"
    if letter == "A":
        return _SOFT_A + (_SOFT_A_LITE_TAIL if _LITE else _SOFT_A_FULL_TAIL)
    if letter == "B":
        return _SOFT_B
    return _SOFT_C


_TOOL_CALL_GUIDANCE = (
    "\nTOOL CALL CONTRACTS: discover with list_tool_categories. Reading or writing an "
    "inactive CAD xd:// device enables it for the conversation, subject to tool policy. "
    "When explicitly calling enable_tools, wait for it to succeed before using that device "
    "in another call; do not batch them together. "
    "For wiring harnesses use cad_guide('harness') and the harness tool category: "
    "define connectivity and placements, route, inspect checks and electrical assumptions, "
    "then produce geometry and formboard as needed. Formboard drawing-sheet integration "
    "is not supported. "
    "Use cad_skill('edit-part') for the "
    "awaited helper: details.xdev.inner is already decoded; recover _truncated.full_result "
    "JSON before using fields, never repeat a mutation to recover its reply. "
    "Face/edge SIDs are revision-scoped: requery after geometry edits; $N is a feature/op "
    "reference, not a newly generated edge. Primitive modes are new|add|boss|cut; "
    "new_body is normalized to new (cad_guide('add_features')). "
    "Fillet params.type supports constant or variable (variable also needs radius2); "
    "explicit modes use refs.edges, including structured direction queries. Variable "
    "multi-edge contours require consistent shared-vertex radii; unequal radii on a "
    "closed edge, contradictory vertex radii and variable contact joins are unsupported; "
    "individually resolvable positive unequal endpoints within kernel radius-difference "
    "precision are refused instead of averaged. Unresolvable endpoints may enter lower "
    "correction under local B-rep tolerances. Where needed, finite radii may be "
    "bounded-corrected only to a valid result covering every "
    "selected edge, not a partial blend. Inspect features[].data.fillet requested/effective "
    "radii and selected_edges/covered_edges in the completed add_features result. "
    "Correction is bounded, not guaranteed or globally optimal. A timeout stops the "
    "search without becoming a radius bound; an earlier fully validated all-edge "
    "candidate can still be returned. Invalid selectors, nonfinite inputs, unsupported "
    "topology, crashes and failure to find a valid all-edge result are errors. "
    "See cad_guide('fillet'). "
    "Thread cut/raised failures and insufficient turns caps reject the operation; "
    "cosmetic is an explicit no-geometry choice and preview is provisional, not a "
    "finished thread. See cad_guide('thread'). "
    "For surface extrusion, depth is required for blind/mid_plane, not through_all. "
    "Native surface sweep/loft do not support guide/centerline refs; see cad_guide('surfacing'). "
    "render_views accepts render_part's world-point target with radius; points are "
    "crosshairs, not face queries (cad_guide('gsl')). "
    "render_part targets may be assembly component IDs; view.distance 0 or null auto-fits. "
    "A plain render_part is read-risk; save_path or display_quality makes that call "
    "write-risk. inspect_native_files is write-risk because it can write output_path. "
    "Errors expose error and code, with optional hint/detail; a reply marked "
    "engine='private' comes from an engine not shared with the app. "
    "render_part target and emphasize are aliases (pass one); a view name that is not a view "
    "is an error listing the valid ones; section=\"top\"|{plane,offset}|{origin,normal} on "
    "render_part/inspect_part/contact_sheet cuts on that exact plane (datum planes pass "
    "through the world origin), look_around n_views is 1-8, and failed tiles are reported "
    "in the caption (cad_guide('gsl')). "
    "check_interference members or components accept member names or component IDs. "
    "inspect_section target_center is a visual hint: properties cover the whole plane. "
    "close_part(save=False) means discard=True. describe_assembly ok describes narration "
    "success, not fit completeness; inspect fit and warnings (cad_guide('assembly')). "
    "slice_part.printability and .gcode_check are objects with issues lists; "
    "verify_gcode.checks is a list. Use stats.print_time_s and stats.filament_g. "
    "Relative print inputs use the selected workspace, not the server cwd; preserve "
    "gcode_path from the slice reply. Completed validation failures remain failures: "
    "inspect findings and print_ready, not ok alone (cad_guide('printing')).\n"
)


_LITE_INSTRUCTIONS = (
    "Real parametric CAD + simulation on the Chudware C++ engine (same kernel as the desktop "
    "app). You are the design brain and you drive yourself — these tools are helpers, not a "
    "script; call whatever you need, in whatever order the task wants, and skip what you don't.\n"
    "DISCOVERY: cad_guide() returns a compact topic index; request cad_guide(feature_type) "
    "for parameters or cad_guide('add_features') for batch shapes and worked examples. "
    "Use cad_guide('sketch') for the 2D sketch workflow, "
    "'scad' for OpenSCAD and 'parts' for portable libraries; "
    "explicit 'ops' returns the full vocabulary.\n"
    "ECAD: KiCad project/schematic/board files are the documents of record; humans "
    "edit in KiCad and agent ecad_* edits save to bound files. Open with ecad_open(path), "
    "inspect KiCad with ecad_kicad_status(), and search footprints with "
    "ecad_find_footprint(query='', limit=30). KiCad CLI supplies ERC/DRC and "
    "fabrication/STEP outputs; Chudware supplies simulation, bus simulation, placement, "
    "autorouting and AI design. Respect file locks and external changes "
    "(cad_guide('ecad')).\n"
    "ecad_simulate(netlist_source='kicad') uses KiCad-exported SPICE models "
    "with ngspice and never falls back to MNA; inspect the reported backend.\n"
    "SCAN REFERENCES: cad_guide('scan_reference') covers embedded non-solid scans, "
    "placement, visibility and sampled deviation against the editable part.\n"
    "CONVENTIONS THAT MATTER: prefer STRUCTURED geometry queries such as "
    "{\"of\":\"face\",\"side\":\"top\"} or {\"of\":\"edge\",\"parallel\":\"z\"}; use "
    "query_geometry/describe_part to inspect matches before editing. Simple side words "
    "are supported, but prose is not a precise selector. "
    "query_geometry(owner=feature_id) filters raw engine owner_map before geometric predicates; "
    "page with offset/page_size and heed ambiguous_sids. query_adjacency pages the last body's "
    "graph with face SIDs; doc_face_edges(body_id, topo_id) returns a face's bordering edge IDs. "
    "measure_part density is positive g/cm³ up to 25; heed default-density warnings; body_id "
    "selects a body and inertia is about its centre of mass. measure_between requires one "
    "match per target; axis_distance is axis-to-axis, not clearance. check_symmetry supports "
    "custom planes and body selection. inspect_section rejects target_center; draft_analysis "
    "reports threshold-only results. "
    "Build with the feature a person "
    "would use (sketch+revolve for round bodies, loft for blended shapes). "
    "Touching sketch endpoints auto-coincide on every edit, so profiles close on their own. "
    "render_part() SEES the annotated result; describe_part()/describe_sketch()/describe_scene() "
    "are the text-only read when you have no vision.\n"
    "MODEL INTENT: edit original features, define critical dimensions and relations, and "
    "place structure before dress-ups; prefer meaningful features over minimizing count. "
    "check_model_history(doc_id) reports native history findings; simplify_part(doc_id, "
    "apply=False, refine=False) previews conservative cleanup, and explicit apply=True "
    "commits it. Refine is opt-in, editable and never flattens history "
    "(cad_guide('model_history')).\n"
    "PARALLEL WORK: agents can work asynchronously on the same or different documents. "
    "Explicit delegation settings govern the depth cap; absent a setting, an explicit "
    "request to use subagents can enable delegation for the turn. "
    "Pass the doc_id returned by new_part/open explicitly for shared/delegated work. "
    "Each embedded agent has its own current part; a bare call never selects a peer's "
    "AI part. Use list_parts() and an explicit doc_id to join existing work. "
    "list_parts and get_part page results with limit and cursor; follow next_cursor "
    "(get_part: feature_page.next_cursor), restarting if its revision changes. "
    "Oversized replies include result_path for the complete JSON. "
    "No exclusive custody or claim/release handshake is required. The engine applies "
    "edit batches atomically in arrival order; a later valid edit to the same feature "
    "wins, not a semantic merge. Read latest state before dependent edits and verify "
    "the combined result after writers finish. Keep document names in narration.\n"
    "PRESENTATION: working documents stay out of the viewport. Agent edits persist "
    "automatically unless human edits are pending; never ask the user to save AI work. "
    "AI recovery is automatic when the backing file is unchanged; conflicts are retained "
    "for agent recovery. Call show_in_app(doc_id) only for chosen artifacts. "
    "Live turns retain their opened or attached documents between tool calls. A doc_not_found "
    "may give an exact import_part(path=..., resume=True) for a verified saved file; otherwise "
    "check recover_parts(all=True) before reopening a possibly older native file. "
    "User-opened documents receive live edits and refresh after external file rewrites "
    "unless pending human edits require conflict resolution.\n"
    "REFERENCE ACQUISITION ORDER: (1) part_search / make_standard_part for catalog and "
    "standard parts (servos, steppers, bearings, fasteners, extrusions); part_get confirms "
    "dimensions. The part_search preview sheet labels match JSON row indices; use the images "
    "to select candidates, then confirm identity and dimensions with part_get before acquiring. "
    "(2) match_model / part_source for a discrete product's CAD. "
    "(3) fetch_cad_model(url) for a specific model page or STEP/IGES file found by web "
    "search; import_artifact if not auto-imported. "
    "For ambiguous models, present the returned ask_user choice through ask_user; do not "
    "choose silently. Retry fetch_cad_model with format and/or member for an archive, "
    "or selection_field and selection for an adapter choice; retain prior selectors "
    "for multi-stage choices. For queue errors, retry scrape_enqueue with job_id and "
    "member (optionally format), or use the original hit as preset with member/format "
    "and any source-specific selectors. "
    "(4) Web search only for specs/datasheets absent from the library. Then COMMIT to cited dimensions with clearance and build; "
    "never loop on research. For an MG90S arm, search MG90S before dimensioning mating "
    "brackets. Calculators (feeds_speeds, tap_drill, fit_calc, stackup_analyze, "
    "gear/bearing checks, struct_*), verification (verify_battery, design_review, "
    "check_*), and deliverables (make_report/drawing/presentation) are available.\n"
    "PRINTED PARTS: while modelling, run wall_thickness() for the process-minimum RED heat map "
    "AND dfm_check. Fix every red region at its named face/feature or explicitly justify it "
    "before export/slicing; re-run after edits. min_wall_mm defaults to the bound process/FDM "
    "profile; resolution is optional mm spacing; image=True by default, image=False for JSON. "
    "Read inscribed-sphere basis/spacing; null/grey is unmeasured, not safe. "
    "For FDM thin walls use dfm_reinforce for shadow-verified candidate edits and inspect "
    "volume and other-region side effects; it never applies changes. "
    "verify_battery does not run thickness or DFM by default (cad_guide('printing'), "
    "cad_guide('evaluate'), cad_guide('verify')).\n"
    "mfg_plan_check reports DfM not_evaluated when no helper input was compared against "
    "a process minimum; resolved facts alone are not a pass (cad_guide('manufacturing')).\n"
    "PRINTER SAFETY: print delivery is text G-code only, never firmware .bin/.hex/.uf2 "
    "or binary G-code. Firmware/EEPROM/media-write commands and slicer post_process hooks "
    "are refused; user_override cannot authorize them. Root firmware images block media "
    "publication and must be reviewed off the printer. Never flash or reset a printer "
    "to prepare a print (cad_guide('printing')).\n"
    "Print artifacts reject symlink/hardlink aliases; temporary exports use .part, never "
    "firmware filenames. Root firmware checks include FAT trailing-dot/space aliases.\n"
    "PROJECT WEB: the PLM graph links parts, assemblies, office docs and files — tag things "
    "with plm_graph_keyword, navigate with plm_graph_search/plm_graph_neighbors, watch keywords "
    "of interest with plm_graph_watch + plm_graph_changes (cad_guide('plm')).\n"
    "BE ECONOMICAL: model, check what actually needs checking, and deliver. Don't over-verify, "
    "don't narrate every step, and keep outputs tight — no busywork, no redundant re-renders."
    + _TOOL_CALL_GUIDANCE
    + _soft_tail_for_profile()
)

_FULL_INSTRUCTIONS = (
        "Real parametric CAD + simulation on the Chudware C++ engine (same kernel as the "
        "desktop app). You are the design brain.\n"
        "DISCOVERY: cad_guide() lists focused topics. Request cad_guide(feature_type) for "
        "parameters or cad_guide('add_features') for batch shapes; 'simulation' and 'review' "
        "cover FEA/CFD and annotated renders; 'sketch' is the "
        "2D sketch workflow; 'scad' and 'parts' cover OpenSCAD and portable libraries. "
        "Explicit 'ops' returns the full vocabulary.\n"
        "SCAN REFERENCES: cad_guide('scan_reference') covers embedded non-solid scans, "
        "placement, visibility and sampled deviation against the editable part.\n"
        "MODEL INTENT: edit original features, define critical dimensions and relations, "
        "and place structure before dress-ups; prefer meaningful features over minimizing "
        "count. check_model_history(doc_id) reports native findings; simplify_part(doc_id, "
        "apply=False, refine=False) previews conservative cleanup, and explicit apply=True "
        "commits it. Refine is opt-in, editable and never flattens history "
        "(cad_guide('model_history')).\n"
        "PARALLEL WORK: agents can work asynchronously on the same or different documents. "
        "Explicit delegation settings govern the depth cap; absent a setting, an explicit "
        "request to use subagents can enable delegation for the turn. "
        "Pass the doc_id returned by new_part/new_assembly/import explicitly for "
        "shared/delegated work. Each embedded agent has its own current part; a bare "
        "call never selects a peer's AI part. Use list_parts() and an explicit doc_id "
        "to join existing work, never switch the user's viewport. No exclusive custody "
        "or claim/release handshake is required. Edit batches apply atomically in arrival "
        "order; a later valid edit to the same feature wins, not a semantic merge. "
        "Read latest state before dependent edits, then verify the combined result after "
        "writers finish. Keep document names in narration.\n"
        "PRESENTATION: working documents stay out of the viewport. Agent edits persist "
        "automatically unless human edits are pending; never ask the user to save AI work. "
        "AI recovery is automatic when the backing file is unchanged; conflicts are retained "
        "for agent recovery. Call show_in_app(doc_id) only for chosen artifacts. "
        "Live turns retain their opened or attached documents between tool calls. A doc_not_found "
        "may give an exact import_part(path=..., resume=True) for a verified saved file; otherwise "
        "check recover_parts(all=True) before reopening a possibly older native file. "
        "User-opened documents receive live edits and refresh after external file rewrites "
        "unless pending human edits require conflict resolution.\n"
        "TYPICAL LOOP: new_part(name) -> add_features([...ops...]) to build with standard "
        "editable features (boxes/sketch+extrude/revolve/loft/sweep, fillet/chamfer/shell, "
        "holes, bolt_circle, patterns/mirror) -> render_part() to SEE the colour-coded, "
        "face-labelled result and self-review against the request -> iterate add_features to "
        "fix anything -> measure_part()/query_geometry() to check -> run_fea_static()/"
        "run_fea_modal()/run_cfd_flow() to prove it -> export_part() or make_drawing() to "
        "deliver.\n"
        "Prefer STRUCTURED geometry queries: {\"of\":\"face\",\"side\":\"top\"}, "
        "{\"of\":\"edge\",\"parallel\":\"z\"}. Inspect matches with query_geometry/"
        "describe_part before editing; simple side words work but prose is not a precise "
        "selector. Build with the feature a person would use (sketch+revolve for round "
        "bodies, loft for blended shapes), not stacks of boxes. "
        "The OpenCASCADE B-rep kernel gives precise solids.\n"
        "NO VISION? Use describe_part()/describe_sketch()/describe_scene() (cad_guide('gsl')) "
        "for a text-only read of the geometry instead of render_part's image. Before calling a "
        "design done, run check_feature_overlap() for raw pre-boolean feature-tool "
        "intersections and, for multi-body/assembly docs, check_interference() "
        "for clashes/clearance between bodies or components. Pairs marked "
        "method=fallback_fuzzy_* are approximate; read fuzzy_tolerance_mm. A "
        "fallback_extrema_disjoint flag identifies constituent separation evidence. "
        "Tool overlaps and legacy boss_swallowed do not prove material removal; inspect geometry and history.\n"
        "SKETCHES: author geometry + relations + critical dims in the SAME add_features call "
        "as the sketch; relations first (never `fix` your own profile), then only critical "
        "dimensions. Prefer symmetric about axes/centerlines, equal, midpoint and point_on axis "
        "over coordinates; do not dimension what a relation implies; use eq_set "
        "expressions/globals across features. sketch_probe dry-runs one add_constraint/set_dim/"
        "delete_constraint (would_over_define, dof_delta, conflicts_with) before you add a driving "
        "dimension. add_features auto-defines NEW sketches and sketch_add_entity batches in the "
        "same revision unless auto_define:false: it adds relations first, then dimensions with "
        "measured values rounded to clean increments (round:false opts out), all tagged auto:true "
        "(soft: driving but superseded by later explicit intent). Read authored_dimensions vs "
        "auto_dimensions and the reply's sketch_definitions[] / op_reports[]. Parametric ops "
        "(sketch_offset/pattern_entities/fillet/chamfer/move/rotate) register their own "
        "dimensions and relations. Finish check_sketch (dof 0, not pinned_only); open "
        "sweep/rib paths are valid. Details: cad_guide('sketch').\n"
        "BATCH DIALECTS: sketch_fully_define accepts id as an alias for sketch; edit_feature "
        "accepts a boolean suppressed alongside params and applies both edits. Assembly "
        "component transforms accept 16 row-major numbers or four rows of four numbers.\n"
        "For holes specify refs.face and params.end (blind or through_all); do not guess a face "
        "or end. Signed and offset axes use explicit refs {axis:[x,y,z],point:[x,y,z]}. "
        "Booleans combine two operands; chain additional operands. Inspect add_features "
        "assumptions and use cad_guide('ops') for the engine's live per-feature parameter keys.\n"
        "ASSEMBLIES: new_assembly(name) creates an assembly doc; asm_add_component(path) places "
        "saved .step+ parts; asm_mate(type, a, b) constrains them with plain-word face/edge "
        "queries per component (concentric+coincident = the bolt-in-hole seat) — then "
        "describe_assembly()/check_interference()/interface_check validate the fit. "
        "interface_check separates bore diameter differences from positional alignment; pass "
        "a fastener for nominal clearance. Contact is unverified without planar mate references "
        "or explicit contact_faces (cad_guide('assembly')).\n"
        "ASSEMBLIES MOVE: simulate_motion drives dimensional mates on a snapshot; "
        "persist_study=true records a Motion study; the default is read-only. Large trajectories may return a digest; "
        "detail='full' requests all poses subject to the reply cap. Dynamics uses component material densities or explicit "
        "density/mass inputs. Sweep envelopes are conservative sampled AABBs, not continuous "
        "clearance proofs (cad_guide('motion')).\n"
        "DONE MEANS VERIFIED: before reporting a design as finished, call verify_battery(doc_id=doc_id) "
        "(cad_guide('verify')); require verdict pass and nonempty coverage.ran. Measurements "
        "are informational, not pass/fail. Thickness and DFM are not run by default; "
        "request them explicitly.\n"
        "PRINTED PARTS: while modelling each part, run wall_thickness() for the "
        "process-minimum RED heat map AND dfm_check(printer=<model>). Fix every red region "
        "at its named face/feature or explicitly justify it before export/slicing, then "
        "re-run. min_wall_mm defaults to the bound process/FDM profile (currently 1.0 mm); "
        "resolution is optional mm spacing; image=True by default, image=False for JSON. "
        "Read inscribed-sphere basis/spacing; null/grey is unmeasured, not safe "
        "(cad_guide('printing'), cad_guide('evaluate')). "
        "Use dfm_reinforce for shadow-verified FDM wall edits; review side effects before applying. "
        "dfm_check(verify_fixes=False) skips clone verification and omits proposed operations.\n"
        "PRINTER SAFETY: print delivery is text G-code only, never firmware .bin/.hex/.uf2 "
        "or binary G-code. Firmware/EEPROM/media-write commands and slicer post_process hooks "
        "are refused; user_override cannot authorize them. Root firmware images block media "
        "publication and must be reviewed off the printer. Never flash or reset a printer "
        "to prepare a print (cad_guide('printing')).\n"
        "Print artifacts reject symlink/hardlink aliases; temporary exports use .part, never "
        "firmware filenames. Root firmware checks include FAT trailing-dot/space aliases.\n"
        "REFERENCE ACQUISITION ORDER: (1) part_search / make_standard_part for catalog "
        "and standard parts (servos, steppers, bearings, fasteners, extrusions); "
        "part_get confirms dimensions, part_place places catalog records. "
        "The part_search preview sheet labels match JSON row indices; use the images to "
        "select candidates, then confirm identity and dimensions with part_get before acquiring. "
        "For ECAD, pass doc_id to part_search/part_get for KiCad project library "
        "context; part_place with doc_id or doc=<doc_id> edits and saves the bound "
        "KiCad schematic or board. "
        "(2) match_model / part_source for a discrete product's CAD; follow its "
        "artifact_plan and confirm uncertain identities. (3) fetch_cad_model(url) "
        "for a specific model page or STEP/IGES file found by web search; "
        "import_artifact if not auto-imported. "
        "For ambiguous models, present the returned ask_user choice via ask_user rather "
        "than choosing silently. Retry fetch_cad_model with format and/or member for an "
        "archive, or selection_field and selection for an adapter choice; retain prior "
        "selectors for multi-stage choices. For queue errors retry scrape_enqueue with "
        "job_id and member (optionally format), or the original hit as preset with "
        "member/format and source-specific selectors. "
        "(4) Web search only for specs/datasheets absent from the library. Then COMMIT to cited dimensions "
        "with clearance and build; never loop on research. For an MG90S six-axis "
        "arm, search MG90S first and build mating brackets around its envelope "
        "and mount dimensions (cad_guide('parts')). vendors_health checks vendor "
        "adapters (needs_human_login → ask the human to vendor_login, never "
        "fabricate); cam_tool_import handles catalog cutting tools.\n"
        "REQUIREMENTS: capture design intent first with req_create(title=title, doc_id=doc_id) "
        "or req_path, inspect with req_report, and check individual requirements with "
        "req_verify_by_id (cad_guide('requirements')).\n"
        "MULTI-STEP MISSIONS: for 3+ sequential tool calls that can fail independently, use "
        "dag_plan → dag_run with checkpointing (cad_guide('dag')).\n"
        "BUILD-ORDER PLANNING: for complex parts/assemblies, call buildgraph_plan(plan_json) "
        "to convert a BUILD PLAN or dag/1 mission into an aicad.buildgraph/1 prerequisite graph "
        "(datum→sketch, boss→pierce, parts→mates order); validator findings are advisory items. "
        "buildgraph_status(run_id) reports node statuses. "
        "Use before executing multi-feature parts to catch missing prerequisites.\n"
        "UNKNOWNS: batch all user questions into ONE ask_user([{id, prompt, type, default}, ...]) "
        "call with defaults for every field (cad_guide('interaction')). Prefer a native questions "
        "array; JSON-encoded arrays/objects are decoded, while ordinary text is one text question.\n"
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
        "piping_* (pipe networks: piping_route obstacle-avoiding routing -> piping_solve -> "
        "piping_spec_check ASME B31.3 -> piping_bom spools, run_pipe_flow internal-flow CFD, "
        "persisted per doc and reloaded with piping_load, piping_pump_drive sizes a network "
        "pump's electric motor — cad_guide('piping')), vessel_size "
        "(ASME VIII-1 pressure vessels) and duct_size (HVAC/process ducts) — "
        "cad_guide('industrial'), hydraulic_* (ISO 1219 fluid power: hydraulic_new -> "
        "add_component/add_hose -> size_cylinder/size_pump/size_motor -> pump_drive_size "
        "(electric motor) -> hydraulic_simulate transient under a valve schedule -> bom, "
        "persisted per doc and reloaded with hydraulic_load — "
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
        "REVERSE ENGINEERING: after reveng_start_session, search for an existing model "
        "with reveng_find_models(session_id, query) before researching or building. "
        "Inspect candidates and use reveng_use_model(session_id, url=... or path=...) "
        "when a suitable model is available; otherwise proceed with research or reconstruction. "
        "VIDEO TO PART: reveng_start_session -> reveng_add_video (async; poll session.run). "
        "For Gaussian capture, reveng_start_splat(live=True) trains snapshots without CAD "
        "scale/mesh gates; inspect reveng_splat_status then reveng_finish_splat for a "
        "full-quality pass. reveng_reconstruct -> confirm scale (reveng_resolve_gate, or "
        "reveng_confirm_scale when no mission gate exists). known_dim must compare "
        "identifiable endpoints of the same feature, not a guessed PCA span. "
        "reveng_align_axes(method='planes'|'pca'|'manual') gives suggested, not verified, "
        "datums; refit after alignment via reveng_fit_primitives -> reveng_build_parametric. "
        "Inspect actual features and bodies: ok:false/no_features_rebuilt and null "
        "match_pct mean no scored editable rebuild. recognition.unclaimed has at most "
        "100 examples; unclaimed_count and unclaimed_path identify the full evidence. "
        "Use get_reveng_deviation and attach/import the reference. "
        "Sparse cameras, Gaussian splat and mesh from the same reconstruction share its SfM "
        "gauge and metric transform. Brush outputs visual Gaussians, never a mesh. "
        "reveng_install_tools fetches a missing Brush / OpenMVS in the background (poll "
        "reveng_status().install); never ask the user to run a provisioning script. "
        "reveng_splat_to_mesh prunes the splat to its subject (returns points_path, "
        "pruned_splat_path, prune report; min_opacity is adaptive unless given) and defaults to approximate Gaussian-depth TSDF when proven "
        "matching undistorted training poses exist (the full splat's median depth from up to 120 views, "
        "support plane removed, photo-colored; minutes), otherwise reports Poisson fallback; "
        "method='poisson' selects it explicitly. Neither reconstructs unseen surfaces; "
        "independently measure critical features and confirm scale. Never pair an imported "
        "splat with unrelated cameras. reveng_import_splat / reveng_convert_splat support "
        "external Gaussian files; get_reveng_session's sfm_dataset pairs the undistorted "
        "sparse_dir/images_dir with SfM quality stats; reveng_sparse_preview returns up to 5000 points in "
        "the confirmed metric frame, otherwise native, plus frusta and an artifact path. "
        "See cad_guide('video_to_part') and cad_guide('splats').\n"
        "NARRATION: use describe_toolpaths/describe_results/describe_assembly/describe_board/"
        "describe_project for compact text summaries instead of raw JSON dumps (cad_guide('narration')).\n"
        "REPAIR: inspect any regen_diagnosis on a failed batch; regen_diagnose(doc_id=...) "
        "can inspect an open document (cad_guide('regen')).\n"
        "DIFF + REVIEW: doc_diff_tool(rev_a, rev_b) for semantic change records (cad_guide('doc_diff')); "
        "design_review(doc_id) for a one-call geometry battery (cad_guide('design_review')); "
        "stackup_analyze(dims) for 1D worst-case + RSS tolerance stack-up over a dimension chain "
        "(cad_guide('tolerance_stackup')).\n"
        "PROJECT WEB: the PLM graph links parts, assemblies, office docs and files into one "
        "navigable web — plm_graph_keyword to tag, plm_graph_search/plm_graph_neighbors to "
        "navigate, plm_graph_watch + plm_graph_changes for keywords of interest, "
        "plm_graph_configure for office-content extraction + cross-project linked graphs "
        "(cad_guide('plm')).\n"
        "DELIVERABLES: make_report/make_presentation/make_workbook build real office files "
        "(cad_guide('deliverables')). RESEARCH: research_web_search/research_fetch_page "
        "for missing specs/datasheets, never recall a load-bearing number from memory.\n"
        "DESIGN LEDGER: ledger_get(run_id, budget_tokens) / ledger_note(run_id, kind, "
        "payload) persist mission state (brief, decisions, dimensions, requirements, questions, "
        "stage_log, artifacts) across role handoffs — the anti-context-loss mechanism for "
        "24k-context local models. Every role should cold-start from ledger_get, not chat history.\n"
        "Common cad_guide topics: overview, edit_part, simulation, review, intake, gsl, cam, narration, parts, "
        "reverse, reveng, video_to_part, splats, deliverables, requirements, dag, verify, interaction, sourcing, cost, "
        "spec, piping, industrial, hydraulic, firmware, structural, regen, code_compliance, "
        "design_review, doc_diff, plm, tolerance_stackup, evaluate, thickness, printing, sketch, sketch3d.\n"
        "REBUILD FAILURE? A failed add_features batch may carry a `regen_diagnosis` fix-preview "
        "card (diagnosis + editable fix_ops + verifier verdict). Record your verdict with "
        "regen_resolve(card, 'accept'|'edit'|'reject') — accept/edit returns the fixed document; "
        "regen_diagnose(doc_id=...) or regen_diagnose(document_json=...) diagnoses directly; "
        "a rebuild without reference geometry is unverified.\n"
        "ECAD: KiCad project/schematic/board files are the documents of record; humans "
        "edit in KiCad and agent ecad_* edits save to bound files. ecad_open(path) opens "
        "a project or file, ecad_kicad_status() checks the installation, and "
        "ecad_find_footprint(query='', limit=30) searches footprints. KiCad CLI is "
        "authoritative for ERC/DRC and fabrication/STEP outputs; Chudware retains "
        "simulation, bus simulation, autorouting, placement, AI design and analyses "
        "(cad_guide('ecad')). Respect locks and external changes. Pre-fab checks: "
        "ecad_run_hostcheck / ecad_run_sicheck / ecad_fpga_check / ecad_run_pdn; "
        "cross-sim handoffs via cad_guide('simulation').\n"
        "SENSOR-NODE DEMO: scripts/demo_sensor_node.py is the end-to-end worked "
        "example (sensor-node board → verify ladder → firmware → thermal → heatsink → budget → "
        "make_report); see docs/reference/SENSOR_NODE_DEMO.md and "
        "cad_guide('simulation').\n"
        + _TOOL_CALL_GUIDANCE
        + _soft_tail_for_profile()
)

mcp = FastMCP(
    "Chudware",
    instructions=(_LITE_INSTRUCTIONS if _LITE else _FULL_INSTRUCTIONS) + (
        "\nTOOL CALLS: await embedded eval calls and check ok:false before indexing results. "
        "Python forwarding helpers must use a positional-only tool selector, "
        "e.g. async def cad(tool_name, /, **args), so name= remains a CAD argument. "
        "Read each tool's actual reply shape instead of assuming shared success keys. "
        "Embedded Eval's details.xdev.inner is already decoded; do not JSON-decode returned "
        "strings. cad_guide and cad_skill return plain text through the same checked helper. "
        "For existing native parts, see cad_guide('edit_part'): import_part(path) "
        "attaches to the live document; copy=True opens an independent copy. get_part features are in tree.features; "
        "feature_ids selects complete records without dumping the whole tree. "
        "cad_guide('feature_history') covers insertion, reorder and rollback. "
        "Compact reads retain tree.rollback_after; mutation features[].rolled_back marks "
        "uncompiled suffix features, not successful builds. "
        "When a valid feature edit breaks existing downstream history, add_features keeps "
        "the edit and returns auto_rollback with the bar, broken features and hidden suffix. "
        "Repair the listed features and set_rollback after:null to rebuild; repeat if needed. "
        "A batch-authored feature that fails or was not compiled in the attempted rebuild "
        "still rejects the batch atomically. Already-broken histories can retain a partial "
        "repair with no new failures, even behind an earlier unresolved rollback. "
        "auto_rollback.after='start' means the bar is before every feature. "
        "Do not silently deliver a rolled-back model: save_part warns, and an intentional "
        "incomplete handoff must disclose the rollback and all hidden feature IDs. "
        "A raw add_features running reply is a handoff, not final geometry. The edit-part "
        "checked helper joins auto_backgrounded jobs; otherwise wait_job(job_id) and inspect "
        "the result before accessing body_count, features or id_map. For get_job/wait_job, "
        "status=done means finished, not successful: a failed operation sets top-level "
        "ok:false and preserves result. Recover recorded results or current state after "
        "reporting errors; never replay successful mutations. "
        "For costly Eval edits, request add_features(background=True), record job_id and "
        "wait outside Eval. Aborting a CAD call abandons its hostd connection; queued work "
        "is abandoned, but an already-running native operation may still finish. Inspect "
        "document state before retrying. A Python kernel reset does not reset CAD documents. "
        "preflight_ops shares add_features normalization and auto_define policy; pass the same "
        "auto_define setting to prediction and apply. "
        "MODELING TRUTH: rib and sweep paths may be open; revolutions may close to the "
        "axis. Rib considers both drop directions against support and rejects ambiguous "
        "or detached webs. sketch_fillet radius is driving and clamps to the valid corner "
        "range (op_reports requested_radius/applied_radius). Clamped expressions retain the "
        "original inside min/max bounds snapshotted from the corner; later geometry changes "
        "can invalidate the frozen upper bound. Conflicts reject atomically. "
        "Do not delete a partially dependent relation wholesale. Blind cuts start along "
        "the sketch normal; params.flip reverses it. Only a simple blind cut without an "
        "explicit direction or flip can retry the opposite side when it removes no material; "
        "a wrong-side cut that intersects material never auto-corrects. "
        "Mutation volumes/deltas are approximate display statistics; without a "
        "revision-correct baseline no delta is reported. bore_faces_delta counts faces, "
        "not holes; body_count counts physical solids and logical_member_count compiled "
        "members. measure_part returns volume_mm3 both at top level and under result['mass'], "
        "which also contains mass_g. Expression literals are not evaluated dimensions. "
        "check_feature_overlap pairs are pre-boolean tool intersections, not material "
        "loss; dead_features means no surviving owned faces. Read completeness and inspect "
        "geometry. STL validation_stages separates facet checks from the skipped B-rep "
        "round-trip. For interference read clearance_status/clearance_tolerance_mm: "
        "within_tolerance is uncertain, not a definite pass or violation. "
        "Use cad_guide('rib'), cad_guide('eq_set'), cad_guide('split'), "
        "cad_guide('assembly') and cad_guide('sm_relief'). "
        "Planar coincident aligned uses opposite outward normals; parallel aligned uses "
        "the same direction. Planar distance satisfies normal · (point_b − point_a) = "
        "sign × value (aligned +1, anti −1); normal is the first planar reference's, "
        "else the second's. Source-frame candidates are suggestions, never remaps. "
        "A standard SHCS has an editable nominal-key-width socket to minimum full-hex "
        "depth, not modeled threads/broach point/corner radii or certified properties; "
        "inspect part_place.detail.geometry_quality and omissions. Catalog-backed assembly "
        "BOM components are bought with saved catalog part_id as part_ref; vendor, MPN "
        "and price remain unknown unless separately available. "
        "For flat sheet metal add sm_flat_pattern before export_flat_dxf (DXF/DWG layers "
        "OUTLINE, BEND, CUTS). For static/contact FEA fill-guard failures use smaller "
        "mesh_size_mm and, if needed, min_size_mm, then check mesh quality/convergence; "
        "CLI -h is metres, MCP sizes are millimetres. "
        "STRENGTH: use find_stress_risers and wall_thickness to inspect load paths, "
        "reinforce thin regions and smooth re-entrant corners when feasible. A dry-run "
        "only proves a feature builds, not strength; check effective radius and rerun "
        "the load cases. Compare local mesh convergence and do not treat a sharp-corner "
        "singularity as a converged stress. Kt depends on the chart's geometry, loading "
        "and nominal stress; do not apply a notch factor twice to a resolved FEA field. "
        "DRAWINGS: review a requested draft before export; a non-release draft does not "
        "require a release audit and must not be presented as certified. For release, "
        "audit_drawing against real manufacturing requirements; empty specs are refused. "
        "Pair view_id with annotation_id, use a=[x,y] sheet mm at/near the projected "
        "circle for add_hole_callout; thread/depth and non-simple hole details require a "
        "matching feature_ref. Chamfer dimensions use a model edge SID or sheet anchor, "
        "with an optional feature_ref; symmetric and distance-angle sizes come from the model. "
        "For angular_dim supply a,b,c,d sheet points or two straight edge SIDs in a,b. "
        "Linear/aligned dimensions accept one line edge SID or two parallel planar face SIDs "
        "in a view that exposes their true separation. "
        "Check make_drawing auto_dimensions_added, sections_added and warnings; model "
        "dimensions whose anchors cannot resolve are stale. Audit all sheets and inspect "
        "model-derived missing dimensions, hole callouts, and mismatched text overrides. "
        "Unevaluated assembly component schemes and equation-driven feature nominals remain "
        "missing critical requirements, not release evidence. "
        "suggest_dimension_scheme requires process and live sketch health; review and apply "
        "its geometric relation rows first, then re-plan before adding dimensions. "
        "Use positions={'x': [...], 'y': [...]} for ordinate axis='both'; set_titleblock "
        "uses sheet_id or the active sheet. Set geometric scale rather than only title text. "
        "Save a standalone drawing with export_part(fmt='draw+', doc_id=<drawing_id>, "
        "path='sheet.draw+'); save_part accepts its open source part/assembly instead. "
        "Relative import_part/save_part/export_part/asm_add_component paths use the selected "
        "session workspace, not the worker cwd or assembly folder; without a workspace, "
        "use absolute paths. Vault-backed plm_* tools default path to that exact workspace "
        "and resolve explicit relative paths beneath it, never an ancestor project. "
        "Assembly edit/delete ops require id as a nonempty string, not a component/mate object. "
        "Assembly pose ownership uses the final batch state: delete/suppress all live mates "
        "and transform the freed component in one batch, or drive its remaining mates. "
        "Mate residual_dof uses the accepted constraint rank, not a separate scale-sensitive "
        "rank estimate; mechanism dof still includes explicit driven coordinates. "
        "Open path-backed source parts update direct and nested assemblies immediately, "
        "including unsaved edits and undo/redo; saving is for persistence, not refresh. "
        "Collision checks, measurements, rendering and activation also refresh changed "
        "disk-only descendants without a manual solve.\n"
    ),
)

# Tool registry (contracts/providers.md §6): tag every tool with {family, risk} and enforce
# a per-context allow-list at dispatch. `view(mcp, family)` stamps each register_*_tools()
# call site with its family and wraps its tools in the single shared dispatch guard. Default
# policy = everything allowed, so this is behavior-neutral until a DAG node / env sets a list.
from services.mcp import tool_registry as _reg

# Mechanical CAD + simulation (C++ feature engine via libchudware.dll).
# Per-family modules (split out of the former cad_tools.py); all register
# under the stable registry family "cad" EXCEPT cad_sim_tools (FEA/CFD solves), which
# is genuinely "sim" so the Settings -> AI -> Tools toggle matches real ownership.
from services.mcp.cad_doc_tools import register_cad_doc_tools
from services.mcp.cad_reference_tools import register_cad_reference_tools
from services.mcp.cad_eval_tools import register_cad_eval_tools
from services.mcp.cad_render_tools import register_cad_render_tools
from services.mcp.cad_exchange_tools import register_cad_exchange_tools
from services.mcp.cad_sim_tools import register_cad_sim_tools
from services.mcp.cad_asm_tools import register_cad_asm_tools
from services.mcp.cad_motion_tools import register_cad_motion_tools
from services.mcp.cad_drawing_tools import register_cad_drawing_tools
register_cad_doc_tools(_reg.view(mcp, "cad"))
register_cad_reference_tools(_reg.view(mcp, "cad"))
register_cad_eval_tools(_reg.view(mcp, "cad"))
from services.mcp.stress_tools import register_stress_tools
register_stress_tools(_reg.view(mcp, "cad"))
register_cad_render_tools(_reg.view(mcp, "cad"))
# render_animation (turntable/explode/motion/fea films) — the render family's moving half;
# same engine render helpers, same save-path containment, so it registers beside it.
from services.mcp.cad_animation_tools import register_cad_animation_tools; register_cad_animation_tools(_reg.view(mcp, "cad"))
register_cad_exchange_tools(_reg.view(mcp, "cad"))
register_cad_sim_tools(_reg.view(mcp, "sim"))
from services.mcp.fea_probe import register_fea_probe
register_fea_probe(_reg.view(mcp, "sim"))
# Release-evidence packets (sim_evidence_packet) + the independent Polyhedral-FEA CLI
# cross-check lane (polymesh_cli). Same "sim" family as the solves they document.
from services.mcp.sim_evidence_tools import register_sim_evidence_tools
register_sim_evidence_tools(_reg.view(mcp, "sim"))
# Topology optimization: native capability matrix + the durable sim_run_topology study run
# (persisted type "topology" study the GUI opens; collector registered at import).
from services.mcp.topology_tools import register_topology_tools
register_topology_tools(_reg.view(mcp, "sim"))
register_cad_asm_tools(_reg.view(mcp, "cad"))
# Kinematic evidence (swept envelope/solid, Ackermann turning radius, batched motion->FEA).
# Composes the shared motion implementations directly; registered in the "sim" family.
register_cad_motion_tools(_reg.view(mcp, "sim"))
# cad_asm mixes assembly authoring (cad) with kinematics (sim): retag the two motion
# tools post-registration so the Settings 'sim' family toggle governs them.
for _n in ("simulate_motion", "motion_fea_handoff"):
    if _n in _reg.catalog():
        _reg.catalog()[_n]["family"] = "sim"
register_cad_drawing_tools(_reg.view(mcp, "cad"))

# Nested-tree profiler (mission master): engine_profile_tree drills a call-tree / phase-tree
# by path so an agent reads root + hot children first (context-frugal). Over prof/tree.{h,cpp}.
from services.mcp.profile_tools import register_profile_tools
register_profile_tools(_reg.view(mcp, "cad"))

# GSL — Geometry-Semantics Layer: compact named part outline (describe_part), face-adjacency
# graph (query_adjacency), and stable-id hover. The progressive-pull view of the geometry.
from services.mcp.gsl_tools import register_gsl_tools
register_gsl_tools(_reg.view(mcp, "gsl"))

# Recon — reverse-engineering: import a STEP/mesh → deterministic feature recognition → an
# EDITABLE parametric tree, verified against the source solid by boolean comparison
# (reverse_engineer / compare_parts / convert_face_to_sketch / project_into_sketch;
# guide: cad_guide("reverse")).
from services.mcp.recon_tools import register_recon_tools
register_recon_tools(_reg.view(mcp, "recon"))

# Intake — photo-measurement + manufacturing-clarification + first-try-fit. resolve/set process,
# request/submit photos, confirm critical dims, dfm_check, fit_report, make_fit_coupon.
from services.mcp.intake_tools import register_intake_tools
register_intake_tools(_reg.view(mcp, "intake"))

# Interaction — the ONE ask_user seam (contracts/interaction.md): typed questions
# with suggested defaults surface as MCP elicitations; answers persist + replay via the shared
# interaction core (services/agent/interaction.py) that intake clarify and DAG gates share.
from services.mcp.interaction_tools import register_interaction_tools
register_interaction_tools(_reg.view(mcp, "intake"))

# ECAD (schematic / PCB / native circuit sim) — per-family modules (split out of the
# former ecad_tools.py god file); all register under the stable registry family "ecad".
from services.mcp.ecad_sch_tools import register_ecad_sch_tools
from services.mcp.ecad_pcb_tools import register_ecad_pcb_tools
from services.mcp.ecad_sim_tools import register_ecad_sim_tools
register_ecad_sch_tools(_reg.view(mcp, "ecad"))
register_ecad_pcb_tools(_reg.view(mcp, "ecad"))
register_ecad_sim_tools(_reg.view(mcp, "ecad"))
# ECAD schematic DRAWING-quality self-check (geometric, pre-ERC): ecad_lint_schematic.
from services.mcp.ecad_qa_tools import register_ecad_qa_tools
register_ecad_qa_tools(_reg.view(mcp, "ecad"))

# Wiring harness design and its electrical analyses share the ECAD policy family.
from services.mcp.harness_tools import register_harness_tools
from services.mcp.harness_electrical_tools import register_harness_electrical_tools
register_harness_tools(_reg.view(mcp, "ecad"))
register_harness_electrical_tools(_reg.view(mcp, "ecad"))

# ECAD -> thermal FEA handoff (aicad.simhandoff/1 coupling ecad_to_thermal_fea):
# one call from a routed board to a solved board-level thermal study + Tj/limits verify.
from services.mcp.ecad_thermal import register_ecad_thermal_tools
register_ecad_thermal_tools(_reg.view(mcp, "ecad"))

# ECAD -> CHT airflow handoff (aicad.simhandoff/1 coupling ecad_to_cht):
# board + powered components as separate CHT solid regions -> OpenFOAM conjugate heat
# transfer, verifying the thermal handoff's preset-h film-coefficient assumption.
from services.mcp.ecad_thermal import register_ecad_airflow_tools
register_ecad_airflow_tools(_reg.view(mcp, "ecad"))
# ECAD -> vibration reliability screen (aicad.simhandoff/1 coupling
# ecad_to_structural_fea): mounted-board modal solve + excitation-band flagging +
# Steinberg deflection-limit solder-joint risk screen (cited heuristic).
from services.mcp.ecad_reliability import register_ecad_reliability_tools
register_ecad_reliability_tools(_reg.view(mcp, "ecad"))
# Heatsink selection loop: parts-library + parametric candidates re-solved
# to a Tj/case target on the cached ecad_thermal_handoff study (verify envelope
# coupling ecad_heatsink_to_thermal_fea).
from services.mcp.heatsink import register_heatsink_tools
register_heatsink_tools(_reg.view(mcp, "ecad"))

# CAM (computer-aided machining): toolpaths, feeds & speeds, material-removal sim, G-code post.
from services.mcp.cam_tools import register_cam_tools
register_cam_tools(_reg.view(mcp, "cam"))
from services.mcp.cam_audit import register_cam_audit_tools
register_cam_audit_tools(_reg.view(mcp, "cam"))
# Additive CAM: deterministic printability/orientation, stock-PrusaSlicer slicing (subprocess
# only, vendor presets read from the installed slicer) and G-code verification.
from services.mcp.print_tools import register_print_tools
register_print_tools(_reg.view(mcp, "cam"))

# Manufacture gateway: read/propose/request-only surface over chudware-hostd's
# single-owner lane. The family is known but default-off; no approval-create,
# resume, jog, override, unlock, or safety-class tool is registered.
from services.mcp.mfg_tools import register_mfg_tools
register_mfg_tools(_reg.view(mcp, "manufacture"))

# Doc diff — semantic feature-level diff between two document revisions.
from services.mcp.doc_diff_tools import register_doc_diff_tools
register_doc_diff_tools(_reg.view(mcp, "cad"))
# PLM vault (aicad.plm/1): hidden git-backed version history —
# Project/History/Checkpoint/Release/Restore. Schema: contracts/plm.md.
# Engine: services/plm/. Zero user-facing git vocabulary (contract §8).
from services.mcp.plm_tools import register_plm_tools
register_plm_tools(_reg.view(mcp, "plm"))
# Office documents (T-office, contracts/office.md): read+edit .pptx/.xlsx/.xlsm/.csv/.pdf/
# .doc/.docx over python-pptx/openpyxl/python-docx/pypdfium2/pypdf + the LibreOffice-headless
# bridge for legacy .doc. office_extract is the FROZEN contract also exposed as a worker RPC
# (services/agent/worker_rpc.py) for the host AI-tab docs_panel.
from services.mcp.office_tools import register_office_tools
register_office_tools(_reg.view(mcp, "office"))
# General-purpose coding-harness tools (fs_tools.py): the tool-calling agent (agent_turn
# DAG/mission/subagent + external MCP clients) can read/organize files on disk, search, and
# run commands. fs_read/list/glob/grep/write/edit under "files"; run_shell under "shell"
# (separate family so shell can be toggled off independently). fs_read is context-safe
# (line-numbered, offset/limit, byte/column caps, selectors) and routes documents through
# office_extract, so large files/docs never overflow the model context.
from services.mcp.fs_tools import register_fs_tools, register_shell_tools
register_fs_tools(_reg.view(mcp, "files"))
register_shell_tools(_reg.view(mcp, "shell"))
# CAD link graph (1D): cad_graph traversal + cad_graph_link (AI overlay) +
# assembly_plan_review. Over services/plm/cad_graph.py + graph_links.py.
from services.mcp.graph_tools import register_graph_tools
register_graph_tools(_reg.view(mcp, "cad"))
# Requirements (aicad.req/1): CRUD + markdown projection for design briefs.
# Schema: contracts/requirements.md  Brain: services/requirements/requirements.py
from services.mcp.req_tools import register_req_tools
register_req_tools(_reg.view(mcp, "cad"))

# Layout-first planning helper — capture envelope + critical dims + interfaces
# BEFORE modeling; coverage gate (layout_coverage) surfaces in design_review.
from services.mcp.layout_plan_tools import register_layout_plan_tools
register_layout_plan_tools(_reg.view(mcp, "cad"))

# Typed, run-ledger manufacturing plan (design-only; physical device tools remain
# isolated in the default-off manufacture family).
from services.mcp.mfg_plan_tools import register_mfg_plan_tools
register_mfg_plan_tools(_reg.view(mcp, "cad"))

# Verify battery — deterministic check dispatcher for RL reward + CI gate.
from services.mcp.verify_tools import register_verify_tools
register_verify_tools(_reg.view(mcp, "cad"))

# Research: look-it-up-instead-of-recall. web_search + fetch_page return the
# provenance-stamped answer envelope; per-user local cache; offline-safe degradation.
# Also registers fit_calc + recommend_fit (ISO 286, aicad.calc/1 envelope).
# Rule: call fit_calc BEFORE hole_sizing / bore decisions; recommend_fit to choose the class.
from services.mcp.research_tools import register_research_tools
register_research_tools(_reg.view(mcp, "research"))

# Dimension scheme planner: suggest_dimension_scheme reads a sketch
# entity roster and emits an ordered process-aware dimension plan (IT-grade tolerances,
# bolt-pattern datum rules, needs_user_confirm flags for function-critical dims).
from services.mcp.dimension_scheme_tools import register_dimension_scheme_tools
register_dimension_scheme_tools(_reg.view(mcp, "cad"))

# Deliverables — spec-driven office artifacts (contract deliverables/1): make_presentation /
# make_report / make_workbook build real .pptx/.docx/.pdf/.xlsx from a JSON spec with the
# provenance rule (the model never types a load-bearing number); render_document previews any
# supported file; list_artifacts reads the artifact registry. Guide: cad_guide('deliverables').
from services.deliverables import register_deliverables_tools
register_deliverables_tools(_reg.view(mcp, "cad"))

# Sourcing — unified BOM roll-up (made/bought) + approved-provider price resolution.
from services.mcp.sourcing_tools import register_sourcing_tools
register_sourcing_tools(_reg.view(mcp, "parts"))

# Cost-out — made-part cost model (material + machining) + approved quote-out APIs.
from services.mcp.cost_tools import register_cost_tools
register_cost_tools(_reg.view(mcp, "parts"))

# Spec-sheet matching — deterministic part-number → standard-part model (BOLTS/KiCad).
from services.mcp.spec_tools import register_spec_tools
register_spec_tools(_reg.view(mcp, "parts"))

# Piping (routed pipe networks): steady-state 1-D hydraulic solve + pressure-drop report.
from services.mcp.piping_tools import register_piping_tools
register_piping_tools(_reg.view(mcp, "sim"))

# Hydraulic (ISO 1219 circuit design + component sizing).
from services.mcp.hydraulic_tools import register_hydraulic_tools
register_hydraulic_tools(_reg.view(mcp, "sim"))

# Civil / site / utility infrastructure (contracts/civil_schema.md, aicad.civil/1): terrain,
# alignments, water + gravity networks, hydrology, structures, QTO and the piping tie-in.
from services.mcp.civil_tools import register_civil_tools
register_civil_tools(_reg.view(mcp, "sim"))
from services.mcp.civil_hydro_tools import register_civil_hydro_tools
register_civil_hydro_tools(_reg.view(mcp, "sim"))
from services.mcp.civil_link_tools import register_civil_link_tools
register_civil_link_tools(_reg.view(mcp, "sim"))

# Photo capture + photogrammetry entry path for reverse_engineer.
# DEFAULT OFF — start_capture_session MCP tool starts the local listener on demand.
from services.mcp.recon_tools import register_photo_recon_tools
register_photo_recon_tools(_reg.view(mcp, "recon"))

# Firmware (embedded compile loop): fw_new_project / fw_build / fw_sources / fw_sim_run — auto-selects
# the MCU target, generates a pinmap.h header from the linked ECAD board's netlist, and runs
# the ELF through Renode or QEMU with assertion checks.
from services.mcp.firmware_tools import register_firmware_tools
register_firmware_tools(_reg.view(mcp, "ecad"))

# Full-wave EM (eigenmode / driven / timedomain) — builtin-fdfd + analytic adapters;
# openEMS when installed. Compact summary-first envelopes (field arrays behind detail=).
from services.mcp.em_tools import register_em_tools
register_em_tools(_reg.view(mcp, "em"))

# PCB antenna design + EM bridge (mission master): ecad_antenna_design (patch/monopole/IFA/PIFA
# calculators + board copper geometry) + ecad_antenna_analyze (analytic resonance always, openEMS
# full-wave S11 when installed). Cross-checks the IPC-2141 feed impedance.
from services.mcp.antenna_tools import register_antenna_tools
register_antenna_tools(_reg.view(mcp, "em"))


from services.mcp.power_budget import register_power_budget_tools
register_power_budget_tools(_reg.view(mcp, "ecad"))

# Structural: PyNite member forces & deflections for aicad.structure/1 docs.
from services.mcp.structural_tools import register_structural_tools
register_structural_tools(_reg.view(mcp, "sim"))

# Preflight: dry-run plan validation on shadow doc clones.
# preflight_ops is read-only; never mutates the live doc.
from services.mcp.preflight_tools import register_preflight_tools
register_preflight_tools(_reg.view(mcp, "cad"))

# Unified parts library: federated search across all standard-parts catalogs.
# part_search / part_get over the aicad.parts/1 FTS hub (contracts/parts.md).
from services.mcp.parts_tools import register_parts_tools
register_parts_tools(_reg.view(mcp, "parts"))
# Regen diagnosis (IP-18): on a rebuild failure, diagnose + propose editable fix-ops as
# an accept/edit/reject card, logged as an IP-18 correction trace. Live:
# add_features attaches the card on rebuild failure; regen_resolve records the accept/edit/
# reject action trace (journaled to CACHE/telemetry/events.jsonl).
from services.mcp.regen_tools import register_regen_tools
register_regen_tools(_reg.view(mcp, "cad"))
# Code compliance: deterministic rule engine + agentic RAG for building-code checks.
from services.mcp.code_compliance_tools import register_code_compliance_tools
register_code_compliance_tools(_reg.view(mcp, "cad"))
# Design ledger: persistent mission state (aicad.ledger/1).
# ledger_get / ledger_note let any agent role read or write the design ledger without
# holding the full chat history — the anti-context-loss mechanism for 24k local models.
# ledger_get is risk-class READ; ledger_note is WRITE (auto-classified by tool_registry).
#
# NOT lite-gated (2026-08-27). It used to live inside the orchestration block below, but
# `lite` is the profile services/installer/setup_mcp_clients.py RECOMMENDS for the capable
# external clients (Claude Code, opencode, Cowork) — which run the LONGEST multi-step
# sessions, i.e. exactly the ones that lose context and most need a durable place to put
# findings. The ledger is a two-tool note store, not orchestration scaffolding: it
# prescribes no workflow and costs a self-driving model nothing it does not ask for.
# The genuinely heavy orchestration families (dag / mission / buildgraph / advisor /
# workspace) stay gated below, where the "runs freer" argument does apply.
from services.mcp.ledger_tools import register_ledger_tools
register_ledger_tools(_reg.view(mcp, "ledger"))
# ---------------------------------------------------------------------------
# Orchestration / supervision families — the DAG supervisor, mission phase
# machine, build-order planner, advisory engine, and workspace lease registry.
# These are the "pre-designed plans / workflows" scaffolding tuned for small
# local models. The `lite` profile omits them so a capable self-driving model
# runs unconstrained (all real capabilities above stay registered).
# ---------------------------------------------------------------------------
if not _LITE:
    # DAG orchestrator MCP tools: dag_plan / dag_run / dag_status / dag_approve / dag_abort.
    # These five tools expose the services/dag executor to MCP clients so a model or UI can
    # start/resume/inspect/gate DAG missions without a bespoke integration layer.
    from services.dag.mcp_tools import register_dag_tools
    register_dag_tools(_reg.view(mcp, "mission"))
    # DAG replan: dag_replan splices new nodes into a running checkpointed DAG run.
    # Validates end_condition + retry_budget on every spliced node (DoD enforcement), merges
    # into the persisted graph sidecar, and resumes via executor.resume().
    from services.dag.mcp_tools import register_dag_replan_tools
    register_dag_replan_tools(_reg.view(mcp, "mission"))
    # Mission pipeline: mission_start / mission_status / replan_request.
    # The copilot's mission front door — INTAKE→DELIVER phase machine on top of the
    # ledger + roles + buildgraph + DAG.  mission_status renders phase state from the
    # ledger stage_log (what mission control + the GUI consume); replan_request routes
    # an aicad.replan/1 request per contract §17.
    from services.mcp.mission_tools import register_mission_tools
    register_mission_tools(_reg.view(mcp, "mission"))
    # Build-order DAG planner: buildgraph_plan / buildgraph_status.
    # LLM-free planner converts BUILD PLAN / dag/1 mission into aicad.buildgraph/1 graph
    # with prerequisite edges; validator findings are aicad.advisory/1 items.
    # ready_nodes() surface for multi-doc fan-out.
    from services.agent.buildgraph.mcp_tools import register_buildgraph_tools
    register_buildgraph_tools(_reg.view(mcp, "mission"))
    # Advisory engine: advise / stage_set / advisory_dispute.
    # advise runs a stage's read-only check battery over a doc on demand (aicad.advisory/1);
    # the dag node runner already runs the advisory engine automatically after each node. advise is
    # risk-class READ; stage_set / advisory_dispute are WRITE (ledger-logged, §9.3).
    from services.mcp.advisor_tools import register_advisor_tools
    register_advisor_tools(_reg.view(mcp, "advisor"))
    # Workspace: multi-doc lease registry + per-doc event streams.
    # workspace_status / workspace_doc_events are risk-class READ (named in _READ_EXACT);
    # no write-lease required to poll status.
    from services.mcp.workspace_tools import register_workspace_tools
    register_workspace_tools(_reg.view(mcp, "workspace"))
    # delegate is deliberately not registered here: it is a mission-scoped
    # pseudo-tool supplied by MissionDispatch, not a global registry callable.
    # Two tests pin that distinction:
    #   test_tool_binding_gaps.py::test_delegate_is_correctly_diagnosed_as_mission_scoped_not_dead
    #   test_prompt_preview_autolearn.py::test_tool_catalog_covers_registry_and_guidance
    # Both exist because of a live forensics incident (2026-07-29): when an unbindable tool
    # got a fabricated cause, it "sends an engineer to mission plumbing and away from the
    # actual break". Outside a mission, `delegate` MUST stay unbound so
    # _unresolved_tool_error can say precisely "mission-scoped", instead of binding to a
    # callable with no delegation tree behind it. MissionDispatch.resolve owns the
    # run-scoped handler and is consulted first (agent_turn.py:1802-1817), so a real
    # mission is unaffected either way -- registering it here would only change the
    # OUT-of-mission case, and only for the worse.
    #
    # So an external MCP client genuinely cannot spawn sub-agents today. Closing that for
    # real is not a registry line: it needs an external-facing mission entry point that
    # binds a run_id + ledger + delegation tree for a client that has none, which is a
    # design task, not a wiring one.

# Design review battery: one-call battery dispatching through DISPATCH_TABLE
# (verify.py) + battery._get_fn for feature_overlap/dfm.  Returns a narrated verdict
# card per contracts/narration.md — never a raw JSON dump.  On-demand helper (kept in lite).
from services.mcp.design_review_tools import register_design_review_tools
register_design_review_tools(_reg.view(mcp, "cad"))
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

# Bug reports (Settings → AI → Tools "Send bug reports"): submit_bug_report lets the
# agent file a local markdown report (Documents/Chudware/Bug-Reports/) when it hits a
# tool error that looks like our bug. Self-gates on agent.bug_reports at call time;
# failure context is auto-captured by agent_turn via services.agent.bugreport_context.
from services.mcp.bugreport_tools import register_bugreport_tools
register_bugreport_tools(_reg.view(mcp, "workspace"))
# task_list (contracts/agent_events.md §plan_state) — the LIVE plan the copilot's Task
# list panel renders. It too existed only in reviewer_exec's private catalog, so an
# external MCP client could never publish one and the panel stayed empty for the whole
# session however long the external agent worked. NOT lite-gated: it is a cheap always-on
# helper like design_recipe in the same family, it spawns nothing, and plan visibility
# matters MOST during the long unattended external sessions `lite` exists to serve.
from services.mcp.task_list_tools import register_task_list_tools
register_task_list_tools(_reg.view(mcp, "workspace"))

# Interface alignment checker: interface_check inspects mating component pairs for
# hole-pattern registration, bore coaxiality, contact-plane flushness, and fastener pass-through
# — the layout errors that pass check_interference (no clash) but are still wrong.  Reuses
# doc_query (per-body faces), asm_world_bodies, cam_extract holes, research_thread_spec.
from services.mcp.interface_check_tools import register_interface_check_tools
register_interface_check_tools(_reg.view(mcp, "cad"))
from services.mcp.assembly_check_tools import register_assembly_check_tools
register_assembly_check_tools(_reg.view(mcp, "cad"))
# Tolerance stack-up analysis: 1D worst-case + RSS over a chain of feature
# dimensions.  Pure-Python; no C++ engine required.  See cad_guide('tolerance_stackup').
from services.mcp.tolerance_tools import register_tolerance_tools
register_tolerance_tools(_reg.view(mcp, "cad"))
# Mechanical engineering checks: AGMA gear tooth stress + ISO 281 bearing life.
from services.mcp.mechanical_tools import register_mechanical_tools
register_mechanical_tools(_reg.view(mcp, "sim"))
# 3D sketch + sketch preview tools: sketch3d_new/add_curves/get/clear/as_sweep_guide,
# render_sketch, sketch_preview_text, doc_sketch_contact_sheet, sketch3d_preview.
# These give an AI driver the same 3D-sketch and 2D-sketch-preview capabilities the GUI
# provides in the Sketch3D workspace + sketch overlay (no C++ build required — Python only).
from services.mcp.sketch3d_tools import register_sketch3d_tools
register_sketch3d_tools(_reg.view(mcp, "cad"))

# ASCII / plaintext 2D representations for non-vision models (owner item 5):
# render_board/sketch/schematic/sim_ascii, apply_ascii_edits, experimental render_ascii.
from services.mcp.ascii2d import register_ascii2d_tools
register_ascii2d_tools(_reg.view(mcp, "gsl"))

# Reverse Engineering MCP family (contracts/reveng.md §8) — per-family modules (split out
# of the former reveng_tools.py); all register under "reveng".
from services.mcp.reveng_session_tools import register_reveng_session_tools
from services.mcp.reveng_build_tools import register_reveng_build_tools
from services.mcp.reveng_splat_tools import register_reveng_splat_tools
register_reveng_session_tools(_reg.view(mcp, "reveng"))
register_reveng_build_tools(_reg.view(mcp, "reveng"))
register_reveng_splat_tools(_reg.view(mcp, "reveng"))

# Freeze what a full server import contributes to the process-wide catalog, so
# tool_registry.warm_catalog() can genuinely RE-register after a clear_catalog()
# instead of relying on a one-shot import side effect. See tool_registry.warm_catalog.
_reg.mark_warm_baseline()

# ---------------------------------------------------------------------------
# Argument teaching (bench evidence: ~40 recorded run failures were "unexpected
# keyword argument" / opaque pydantic dumps, plus a silent-drop class that never
# failed at all). ONE wrapper at the FastMCP dispatch seam, so EVERY registered tool
# gets it without touching a single tool module (485 in the default `full` profile,
# 468 in `lite` — counted 2026-08-27 from mcp._tool_manager._tools).
#
# Seam: ``FastMCP._tool_manager.call_tool``. Both entry points funnel through it —
# the wire path (``Server.call_tool`` handler registered in FastMCP.__init__ with
# ``validate_input=False`` -> ``FastMCP.call_tool`` -> ``self._tool_manager.call_tool``,
# see mcp/server/fastmcp/server.py:308,343-346) and every in-process
# ``server.mcp.call_tool(...)`` caller. Wrapping ``Tool.fn`` instead would be too
# late: FastMCP's generated arg model has pydantic's default ``extra="ignore"``
# (mcp/server/fastmcp/utilities/func_metadata.py ArgModelBase), so an invented
# kwarg is stripped BEFORE fn ever runs — that is exactly bug (b) below.
#
# Two failures this fixes:
#  (a) opaque validation: fit_calc(shaft_diameter=10) used to raise
#      "2 validation errors ... nominal Field required" — it named the MISSING
#      field but never the invented one, and never listed what fit_calc accepts.
#  (b) silent drop: add_features(doc_id=..., ops=[], operation="union") returned
#      ok:True with `operation` discarded. The model's intent vanished silently.
#
#  (b') the same silent drop on a ``**kwargs``-WIDENED tool, which this wrapper
#      used to skip entirely: query_geometry(query="top", return_sparkles=True)
#      returned ok:True with one matching face while the built-in agent path
#      refused the identical call with a teaching error. Widened tools already
#      own that refusal in-body (cad_eval_tools._reject_unknown); they were just
#      never handed the key. ``_open_varkw_arg_model`` reopens their arg model
#      so extras are FORWARDED into the real ``**kwargs`` instead of dropped.
#
# A correct call is untouched: no unknown key + no validation error means we hand
# the exact same arguments to the exact same original with the same
# ``convert_result`` flag, and return its result object unmodified.
import json as _json
import inspect as _inspect

from pydantic import ValidationError as _ValidationError
from mcp.server.fastmcp.exceptions import ToolError as _ToolError

from services.agent.arg_reconciliation import (
    annotation_admits as _annotation_admits,
    attach_warnings as _attach_warnings,
    bind_value as _bind_value,
    drop_descriptive as _drop_descriptive,
    dropped_note as _dropped_note,
    norm_arg as _norm_arg,
    resolve_unknown as _resolve_unknown,
    synonyms_for as _synonyms_for,
    unknown_arg_payload as _unknown_arg_payload,
)

_ARG_TEACHING = os.environ.get("CHUDWARE_MCP_ARG_TEACHING", "1").strip() != "0"
# tool name -> (tool object identity, metadata dict). Re-derived if the registry
# swaps the Tool out from under us; otherwise this is one dict hit per call.
_ARG_META_CACHE: dict[str, tuple] = {}


def _param_type(schema: dict, wire: str) -> str:
    prop = (schema.get("properties") or {}).get(wire) or {}
    t = prop.get("type")
    if isinstance(t, str):
        return t
    for variant in prop.get("anyOf") or ():
        vt = variant.get("type")
        if isinstance(vt, str) and vt != "null":
            return vt
    return "any"


def _arg_meta(tool) -> dict:
    """Per-tool parameter metadata, cached (hot path must stay a dict lookup)."""
    cached = _ARG_META_CACHE.get(tool.name)
    if cached is not None and cached[0] is tool:
        return cached[1]

    fields = tool.fn_metadata.arg_model.model_fields
    names: list[str] = []
    required: list[str] = []
    ann: dict[str, str] = {}
    listing: list[str] = []
    for fname, finfo in fields.items():
        # func_metadata aliases params that shadow a BaseModel attribute
        # (field_json/alias="json"); the alias is the on-the-wire name.
        wire = finfo.alias or fname
        names.append(wire)
        req = finfo.is_required()
        if req:
            required.append(wire)
        ptype = _param_type(tool.parameters, wire)
        ann[wire] = ptype
        listing.append(f"{wire} ({'required' if req else 'optional'}, {ptype})")

    # A tool taking **kwargs owns its own input dialect — never second-guess it
    # here. But the extras it exists to receive must actually REACH it; see
    # _open_varkw_arg_model.
    varkw = False
    try:
        for p in _inspect.signature(_inspect.unwrap(tool.fn)).parameters.values():
            if p.kind is _inspect.Parameter.VAR_KEYWORD:
                varkw = True
                break
    except (TypeError, ValueError):  # builtins / unintrospectable callables
        pass

    meta = {
        "names": names,
        "accepted": frozenset(names),
        "required": required,
        "ann": ann,
        "listing": listing,
        "varkw": varkw,
    }
    _ARG_META_CACHE[tool.name] = (tool, meta)
    return meta


def _open_varkw_arg_model(tool) -> None:
    """Let a ``**kwargs`` tool's arg model FORWARD unrecognised keys instead of
    eating them.

    Measured asymmetry (2026-07-29, this repo, doc-cpp0000000d):

        query_geometry(query="top", doc_id=..., return_sparkles=True)
          agent path -> ok:False "query_geometry() got unknown argument(s)
                        return_sparkles=True — it takes query=, doc_id=, …"
          MCP path   -> ok:True, one matching face, `return_sparkles` GONE

    Cause: FastMCP turns a var-keyword parameter into ONE ordinary pydantic
    field named after it ("kwargs"/"extra") and the generated model keeps
    pydantic's default ``extra="ignore"``, so every key the tool's own dialect
    normaliser was supposed to see is stripped by ``arg_model.model_validate``
    before ``fn`` runs (func_metadata.py ArgModelBase / call_fn_with_arg_validation).
    A loud ``unknown_argument`` refusal became a silently WRONG call.

    The fix is not a second guard: these tools already refuse unknown names
    correctly (cad_eval_tools._reject_unknown and friends) — they were simply
    never handed the evidence. So flip the model to ``extra="allow"`` and make
    ``model_dump_one_level`` (the only thing FastMCP feeds to ``fn(**…)``)
    include the extras, which lands them in the tool's own ``**kwargs`` FLAT —
    byte-identical to what the built-in agent path passes. Every existing
    in-body synonym, note and refusal therefore applies unchanged on both paths.

    Deliberately NOT ``extra="forbid"``: for a widened tool the extras ARE the
    dialect (``query_geometry(of="face", side="top")``,
    ``measure_part(density=2.7)``), so forbidding them would refuse the very
    calls the family was widened to accept.

    Idempotent, once per tool. The advertised JSON schema (``tool.parameters``)
    was generated at registration and is not touched, so nothing the client
    sees changes.
    """
    model = tool.fn_metadata.arg_model
    if model.__dict__.get("_chudware_varkw_open"):
        return
    base_dump = model.model_dump_one_level

    def model_dump_one_level(self) -> dict:
        out = base_dump(self)
        for key, value in (self.__pydantic_extra__ or {}).items():
            # setdefault: an alias collision must never become a duplicate
            # keyword argument to fn().
            out.setdefault(key, value)
        return out

    try:
        model.model_config["extra"] = "allow"
        model.model_rebuild(force=True)
    except Exception:  # noqa: BLE001 - a model we cannot reopen keeps old behaviour
        model.model_config.pop("extra", None)
        return
    model.model_dump_one_level = model_dump_one_level
    model._chudware_varkw_open = True


def _short_val(v) -> str:
    r = repr(v)
    return r if len(r) <= 40 else r[:37] + "..."


def _corrected_call(tool_name: str, meta: dict, known: dict, dropped=()) -> str:
    """The call the model should have written."""
    parts = []
    for wire in meta["names"]:
        if wire in known:
            parts.append(f"{wire}={_short_val(known[wire])}")
        elif wire in meta["required"]:
            parts.append(f"{wire}=...")
    call = f"{tool_name}({', '.join(parts)})"
    if dropped:
        call += "   # drop " + ", ".join(repr(d) for d in dropped)
    return call


def _validation_payload(tool, meta, arguments, exc) -> dict | None:
    """Rewrite a pydantic arg-model failure into the teaching shape.

    Returns None when *exc* is not an argument-validation failure (a tool body
    that raises must keep propagating untouched).
    """
    cause = exc.__cause__
    if not isinstance(cause, _ValidationError):
        return None
    if getattr(cause, "title", None) != tool.fn_metadata.arg_model.__name__:
        return None  # some pydantic model INSIDE the tool blew up — not our business
    try:
        errs = cause.errors(include_url=False)
    except TypeError:  # pragma: no cover - older pydantic
        errs = cause.errors()

    missing, detail = [], []
    for e in errs:
        arg = ".".join(str(p) for p in e.get("loc") or ()) or "<root>"
        if e.get("type") == "missing":
            missing.append(arg)
        detail.append({"argument": arg, "problem": e.get("msg"), "type": e.get("type")})

    if missing:
        why = "missing required argument(s): " + ", ".join(missing)
    else:
        why = "; ".join(f"{d['argument']}: {d['problem']}" for d in detail)
    return {
        "ok": False,
        "error": f"{tool.name}: invalid arguments — {why}",
        "supplied": list(arguments),
        "missing_required": missing,
        "parameters": meta["listing"],
        "required": list(meta["required"]),
        "hint": _corrected_call(tool.name, meta, arguments),
        "detail": detail,
    }


def _emit_payload(tool, payload: dict, convert_result: bool):
    """Return *payload* in whatever shape this tool's own results take.

    423 of the 485 tools are ``-> str`` (wrap_output, {"result": string}); the other 62
    return dict/list/Any (images/content blocks, no wrapper). Feeding the wrong one to
    ``convert_result`` would trip the output model, so try the tool's native shape
    first and fall back.
    """
    fm = tool.fn_metadata
    as_text = _json.dumps(payload, indent=2)
    if not convert_result:
        return as_text if fm.wrap_output else payload
    for candidate in ((as_text, payload) if fm.wrap_output else (payload, as_text)):
        try:
            return fm.convert_result(candidate)
        except Exception:  # noqa: BLE001 - try the other shape
            continue
    raise _ToolError(as_text)  # last resort: the teaching text still reaches the model


def _annotate_interpreted(result, notes: list[str]):
    """Echo the canonical spelling back in the result so the model learns it."""
    line = "; ".join(notes)
    if isinstance(result, str):
        stripped = result.lstrip()
        if stripped.startswith("{"):
            try:
                obj = _json.loads(stripped)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                obj["interpreted"] = notes
                return _json.dumps(obj, indent=2)
        return f"{result}\n\n[interpreted] {line}"
    if isinstance(result, dict):
        return {**result, "interpreted": notes}
    if isinstance(result, (list, tuple)):
        return [*result, f"[interpreted] {line}"]
    return [result, f"[interpreted] {line}"]


def _install_arg_teaching(server) -> None:
    """Wrap ``ToolManager.call_tool`` — the external-MCP dispatch path's own seam.

    NOT "the one seam both dispatch paths cross": the built-in agent
    (``services/agent/agent_turn.py::_bind_tool``) calls tool functions
    directly and never goes through ``ToolManager`` at all. What IS shared is
    the reconciliation logic itself — the synonym tables, name-matching, and
    teaching-payload shape live once in ``services.agent.arg_reconciliation``
    and both this wrapper and ``agent_turn._reconcile_kwargs`` call into it,
    so a fix here (or there) reaches both paths without being reapplied.
    """
    # A stubbed FastMCP (test_mcp_freshness patches the module) has no manager —
    # teaching is a bonus layer, never a hard import dependency.
    manager = getattr(server, "_tool_manager", None)
    if manager is None or getattr(manager, "_chudware_arg_teaching", False):
        return
    original = manager.call_tool

    async def call_tool(name, arguments, context=None, convert_result=False):
        tool = manager.get_tool(name)
        if tool is None:
            return await original(name, arguments, context=context,
                                  convert_result=convert_result)
        meta = _arg_meta(tool)
        notes: list[str] = []
        dropped: list[str] = []
        if meta["varkw"]:
            # The tool owns its dialect — but only if the keys reach it.
            _open_varkw_arg_model(tool)
        elif arguments and not meta["accepted"].issuperset(arguments):
            known, unresolved = {}, []
            for k, v in arguments.items():
                if k in meta["accepted"]:
                    known[k] = v
                else:
                    unresolved.append(k)
            taken = set(known)
            synonyms = _synonyms_for(name)
            still_unknown = []
            for k in unresolved:
                target = _resolve_unknown(k, arguments[k], meta["names"], meta["ann"],
                                          taken, synonyms)
                if target is None:
                    if _drop_descriptive(k, arguments[k], meta["names"], taken, synonyms):
                        # Purely-cosmetic label with no free slot: ignore it
                        # instead of refusing an otherwise-correct call. It goes
                        # in `warnings` here, while the built-in agent path
                        # reports the same drop through `arg_interpretation`;
                        # that asymmetry is deliberate (agent_turn's
                        # _reconcile_kwargs has only its notes channel and a
                        # 3-tuple a dozen callers unpack). Never silent, either way.
                        dropped.append(_dropped_note(k, name))
                        continue
                    still_unknown.append(k)
                else:
                    known[target] = _bind_value(target, meta["ann"].get(target, ""),
                                                arguments[k], tool=name, supplied=k)
                    taken.add(target)
                    notes.append(f"interpreted {k!r} as {target!r}")
            if still_unknown:
                return _emit_payload(
                    tool, _unknown_arg_payload(name, meta["names"], meta["required"],
                                               arguments, still_unknown),
                    convert_result)
            arguments = known

        try:
            result = await original(name, arguments, context=context,
                                    convert_result=convert_result
                                    and not notes and not dropped)
        except _ToolError as exc:
            payload = _validation_payload(tool, meta, arguments, exc)
            if payload is None:
                raise
            return _emit_payload(tool, payload, convert_result)

        if dropped:
            result = _attach_warnings(result, dropped)
        if notes:
            result = _annotate_interpreted(result, notes)
        if (notes or dropped) and convert_result:
            try:
                return tool.fn_metadata.convert_result(result)
            except Exception:  # noqa: BLE001 - never lose a good result over a note
                return tool.fn_metadata.convert_result(await original(
                    name, arguments, context=context, convert_result=False))
        return result

    manager.call_tool = call_tool
    manager._chudware_arg_teaching = True


if _ARG_TEACHING:
    _install_arg_teaching(mcp)


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
        log_path = os.path.join(tempfile.gettempdir(), "chudware-mcp-native.log")
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(log_fd, 1)  # fd 1 -> log; sys.stdout follows, so stray prints are harmless too
        os.close(log_fd)
        real_stream = anyio.wrap_file(TextIOWrapper(FileIO(real_fd, mode="w"), encoding="utf-8"))
        return real_stream
    except Exception:
        return None


def _warm_core() -> None:
    """Load libchudware.dll up front (in the background) so the FIRST tool call doesn't eat the
    cold load (DLL + OCCT/gmsh deps can take many seconds) inside the client's call timeout."""
    # Recover ECAD sessions from autosave before warming the remaining services.
    # Bound KiCad files are the persistent documents of record.
    try:
        from services.mcp.ecad_common import enable_ecad_autosave, recover_ecad_autosaved
        enable_ecad_autosave()
        recover_ecad_autosaved()
    except Exception:
        pass
    try:
        from services.mcp import core_bridge
        core_bridge.available()
        # Recover any docs auto-saved before a previous restart so a client-timeout kill doesn't
        # permanently drop the user's open parts (they reload with fresh ids; list_parts shows them).
        try:
            from services.mcp.cad_common import (recover_autosaved, start_periodic_autosave,
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
        from services.mcp import crash_bundle
        crash_bundle.install()
    except Exception:
        pass


def _reexec_with_fast_malloc() -> None:
    """Re-exec THIS process with a fast allocator preloaded (tcmalloc), then return in the child.

    OCCT's booleans/meshing are allocation-heavy; tcmalloc cuts modeling time ~5-10% vs glibc
    malloc (measured, tools/perf_regression.py) with no logic change and ctest still green. The
    engine is dlopened via ctypes, so linking an allocator into libchudware wouldn't override the
    already-resolved glibc malloc — LD_PRELOAD before the process allocates is the only reliable
    way, hence this early self-reexec. Opt out with CHUDWARE_NO_FAST_MALLOC=1. Loop-guarded + fully
    best-effort: any problem just continues on the default allocator.
    """
    if os.name == "nt":
        return
    if os.environ.get("CHUDWARE_NO_FAST_MALLOC") or os.environ.get("_CHUDWARE_MALLOC_REEXEC"):
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
        env["_CHUDWARE_MALLOC_REEXEC"] = "1"  # sentinel: never re-exec twice
        os.execve(sys.executable, [sys.executable] + sys.argv, env)
    except Exception:  # noqa: BLE001
        pass  # exec failed -> fall through and run on the default allocator


# ---------------------------------------------------------------------------
# Terminal persistence sweep — an external client's disconnect IS our turn boundary
# ---------------------------------------------------------------------------
# The in-app agent promotes AI-authored documents to real project files at its turn
# boundary: mission_dispatch.run_linear_role_turn and worker_rpc._agent_admin_turn both
# call cad_common.save_touched_docs(), the ONLY code that writes a doc into
# CHUDWARE_PROJECT_SAVE_DIR rather than a crash-recovery autosave.
#
# This entry point — the standalone external MCP server — had no such boundary, and so
# never called it. An external client (Claude Code / Cowork / Cursor) opened a part, kept
# building, and disconnected; the project file (if any) still held whatever the doc looked
# like at its LAST explicit save, and everything after that existed only in hostd memory
# and in the periodic crash-recovery autosave — a temp directory reachable solely through
# the `recover_parts` tool the now-disconnected client will never call. That is the same
# silent-loss class the in-app agent was explicitly fixed for.
#
# A connection-scoped server's turn boundary is its shutdown, so that is where the sweep
# goes. Every realistic disconnect path is covered:
#   * stdio EOF / main() returning normally   -> the `finally` in main()
#   * SIGTERM / SIGINT / SIGHUP               -> _install_shutdown_savers
#   * anything else that unwinds the process  -> atexit (same function)
#   * parent died without closing our pipes   -> _start_orphan_watchdog, below
# The sweep runs at most once per process: a signal followed by EOF must not save twice.
#
# It is BOUNDED on purpose. save_touched_docs() talks to hostd and may drain a Box mirror
# with a 90 s budget of its own; a shutdown that waits for that is, from the client's side,
# a server that will not die. So the work runs on a worker thread and the caller waits at
# most CHUDWARE_MCP_SHUTDOWN_SAVE_S (default 20 s) before letting the process go. A slow
# save beats lost work; a hung server beats neither.
_SHUTDOWN_SAVE_BUDGET_S: float = float(
    os.environ.get("CHUDWARE_MCP_SHUTDOWN_SAVE_S", "20") or 20)

_SAVE_LOCK = threading.Lock()
_SAVE_STARTED = False
_SAVE_FINISHED = threading.Event()
_SAVE_STATUS: dict = {}
# Set once a caller's budget expires. Later callers on the same shutdown (a signal
# handler that chains into KeyboardInterrupt, then main()'s finally, then atexit) must
# NOT each spend a fresh budget waiting on the same wedged sweep — that is how a 20 s
# bound turns into a minute of "the server will not die".
_SAVE_TIMED_OUT = False


def _run_terminal_save(reason: str) -> None:
    """Worker body: sweep once, publish the status, always set the done event."""
    try:
        from services.mcp.cad_common import save_touched_docs
        status = save_touched_docs() or {}
    except BaseException as exc:  # noqa: BLE001 — a shutdown sweep never raises
        status = {"saved": [], "errors": [f"{type(exc).__name__}: {exc}"]}
    status["reason"] = reason
    _SAVE_STATUS.clear()
    _SAVE_STATUS.update(status)
    _SAVE_FINISHED.set()
    # stderr ONLY: fd 1 is the JSON-RPC channel (see _install_clean_stdout).
    saved = status.get("saved") or []
    failure = status.get("persistence_failure")
    if saved or failure or status.get("errors") or status.get("warnings"):
        print(f"[services.mcp] terminal save ({reason}): "
              f"saved={[s.get('path') for s in saved]} "
              f"errors={status.get('errors') or []} warnings={status.get('warnings') or []} "
              f"failure={failure}",
              file=sys.stderr, flush=True)


def terminal_save(reason: str, budget_s: float | None = None) -> dict:
    """Persist this connection's unsaved AI-authored documents. Runs at most once.

    Returns the ``save_touched_docs()`` status, or ``{"reason": ..., "timeout": True}``
    when the sweep did not finish inside the budget. Never raises and never blocks
    longer than *budget_s* — every caller is a shutdown path, including a signal
    handler and the orphan watchdog's last act before ``os._exit``.

    The sweep considers only documents touched by the standalone connection's scope;
    crash recovery is handled independently by hostd.
    """
    global _SAVE_STARTED, _SAVE_TIMED_OUT
    with _SAVE_LOCK:
        first = not _SAVE_STARTED
        _SAVE_STARTED = True
    if first:
        threading.Thread(target=_run_terminal_save, args=(reason,),
                         name="chudware-terminal-save", daemon=True).start()
    if _SAVE_TIMED_OUT:
        return {"reason": reason, "timeout": True}
    if not _SAVE_FINISHED.wait(
            _SHUTDOWN_SAVE_BUDGET_S if budget_s is None else budget_s):
        _SAVE_TIMED_OUT = True
        print(f"[services.mcp] terminal save ({reason}) exceeded its "
              "CHUDWARE_MCP_SHUTDOWN_SAVE_S budget — exiting anyway",
              file=sys.stderr, flush=True)
        return {"reason": reason, "timeout": True}
    return dict(_SAVE_STATUS)


def _install_shutdown_savers() -> None:
    """Arm the atexit + signal terminal sweeps. Called from main(), on the main thread.

    Signal handlers CHAIN: the previous handler still runs (SIGINT's default is
    ``signal.default_int_handler``, so Ctrl-C still raises KeyboardInterrupt and anyio
    still unwinds normally), and a non-callable previous handler (SIG_DFL/SIG_IGN) is
    restored and re-raised so the process still dies with the right disposition. The
    save itself happens on the worker thread, so the handler only waits.
    """
    atexit.register(terminal_save, "atexit")
    if threading.current_thread() is not threading.main_thread():
        return  # only the main thread may install signal handlers
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue  # not on this platform (SIGHUP on Windows)
        try:
            previous = signal.getsignal(sig)
        except (OSError, ValueError):
            continue

        def _saver(signum, frame, _sig=sig, _prev=previous, _name=name):
            terminal_save(f"signal:{_name}")
            if callable(_prev):
                _prev(signum, frame)
                return
            signal.signal(_sig, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        try:
            signal.signal(sig, _saver)
        except (OSError, ValueError):
            continue  # handler install refused (embedded/non-main interpreter)


def _start_orphan_watchdog() -> None:
    """Save, then exit, when the client that spawned us is gone.

    stdio MCP servers are children of their client (Claude Desktop/Code, Cursor,
    the GUI copilot). When that parent dies without closing our pipes cleanly —
    crash, SIGKILL, session teardown bugs — this process used to linger forever,
    holding a live connection into chudware-hostd: the daemon never idle-exited,
    its docs never GC'd, and orphaned bridges piled up one per dead session
    (observed: 5 on one box). The parent can't be relied on to clean us up, so
    we watch it ourselves: if our ppid changes (parent died; we were reparented
    to init/a subreaper), exit. Opt out with CHUDWARE_ORPHAN_WATCHDOG=0.

    ``os._exit(0)`` is kept — it is the whole point of the watchdog that nothing,
    including a wedged transport or a stuck atexit handler, can stop us from going
    away. But it bypasses atexit entirely, which is why it used to be the one exit
    path that guaranteed the client's work was never persisted. So it is no longer
    the FIRST thing we do: we run the bounded terminal sweep, then hard-exit
    regardless of whether the sweep finished. A save that hangs still exits."""
    if os.name == "nt" or os.environ.get("CHUDWARE_ORPHAN_WATCHDOG") == "0":
        return  # Windows has no reparenting signal; Job Objects handle teardown
    parent = os.getppid()
    if parent <= 1:
        return  # already init-parented (launched by a supervisor) — no signal to watch

    def _watch() -> None:
        while True:
            time.sleep(15.0)
            if os.getppid() != parent:
                print("[services.mcp] parent client died — saving, then exiting "
                      "orphan bridge", file=sys.stderr, flush=True)
                terminal_save("orphan_watchdog")
                os._exit(0)

    threading.Thread(target=_watch, name="chudware-orphan-watchdog", daemon=True).start()


def main() -> None:
    _reexec_with_fast_malloc()  # fast-allocator preload (best-effort, before any core alloc)
    # Arm the sweeps AFTER the possible re-exec (execve does not run atexit handlers)
    # and BEFORE anything can build geometry.
    _install_shutdown_savers()
    _start_orphan_watchdog()  # a dead client must never leave a zombie bridge pinning hostd
    real_stream = _install_clean_stdout()
    # Warm the core only AFTER fd 1 is redirected, so its load-time prints land in the log,
    # never on the protocol pipe.
    threading.Thread(target=_warm_core, name="chudware-warm", daemon=True).start()

    try:
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
    finally:
        # The client closing our pipes (stdio EOF) lands here, as does any exception
        # that unwinds the transport. atexit would catch it too, but sweeping on the
        # normal path keeps persistence inside the server's own shutdown instead of
        # depending on interpreter-teardown ordering.
        terminal_save("stdio_eof")


if __name__ == "__main__":
    main()
