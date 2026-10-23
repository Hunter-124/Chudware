"""mission_template.py — turn a validated deliverable manifest into an
``aicad.dag/1`` mission graph whose nodes carry role assignments.

The mission DAG is the executable form of the phase sequence
(docs/COPILOT_MISSION_PIPELINE.md §1).  Each phase becomes one or more DAG nodes;
each node names the ROLE that runs it.  Role system prompts are loaded VERBATIM
from ``services/agent/roles/*.yaml`` via the role-card loader — never inlined in code
(contract rule; docs §5).  The node's ``inputs.role`` + ``inputs.role_prompt``
carry the card to the node runner, which applies the role's tool allowlist.

Phase → node mapping:
  * INTAKE   → one ``plan`` node, role ``intake_analyst`` (produces the manifest).
  * CLARIFY  → one ``plan`` node, role ``clarifier`` (only if blocking ambiguity).
  * SPECIFY  → one ``design`` node per deliverable, role ``spec_writer``.
  * ORGANIZE → one ``plan`` node, role ``department_manager`` (feature-flagged).
  * PLAN     → one ``plan`` node, role ``orchestrator`` (build graphs) — for
               assemblies a preceding ``design`` node, role ``layout_engineer``.
  * BUILD    → one ``design`` node per buildable deliverable, role by kind
               (part → ``feature_modeler``; assembly → ``assembly_fit_engineer``;
               drawing → ``drawing_detailer``; controlled documents →
               ``general_agent`` with the docs pack).
               Independent builds fan out; declared ``depends_on`` relationships
               add peer edges so drawings/documents wait for source artifacts.
  * VERIFY   → one ``verify`` node per buildable deliverable, role ``reviewer``.
  * DELIVER  → one ``report`` node per deliverable by kind; controlled-document
               kinds retain ``general_agent`` rather than a tool-less reviewer.

The graph is validated against ``aicad.dag/1`` before return (raises on invalid).
Fan-out at Phase 4 is expressed structurally: BUILD nodes for independent parts
share no forward edge, so the executor's ready-set schedules them together.
"""
from __future__ import annotations

from typing import Any, Optional

from .graph_loader import validate_graph

# aicad.dag/1 node types (graph_loader.VALID_NODE_TYPES).  We map mission roles onto
# the closest structural type.
_TYPE_INTAKE = "plan"
_TYPE_CLARIFY = "plan"
_TYPE_SPEC = "design"
_TYPE_ORGANIZE = "plan"
_TYPE_LAYOUT = "design"
_TYPE_PLAN = "plan"
_TYPE_BUILD = "design"
_TYPE_VERIFY = "verify"
_TYPE_DELIVER = "report"

# Role assignment by deliverable kind (BUILD + DELIVER phases).
# Analysis deliverables split into producing ECAD/firmware roles
# (name-keyed) so they leave sim_engineer (FEA-only) and stop dying as one-shot
# ollama DELIVER leaves with zero schematic/board/fw artifacts.
_BUILD_ROLE_BY_KIND = {
    "part": "feature_modeler",
    "assembly": "assembly_fit_engineer",
    "drawing": "drawing_detailer",
    "cam_program": "feature_modeler",
    "analysis": "sim_engineer",         # default; overridden by name heuristics
    "report": "general_agent",
    "work_instruction": "general_agent",
    "weldment_traveler": "general_agent",
    "modification": "feature_modeler",
}
_DELIVER_ROLE_BY_KIND = {
    "cam_program": "cam_programmer",
    "drawing": "reviewer",
    "analysis": "reviewer",
    "report": "general_agent",
    "work_instruction": "general_agent",
    "weldment_traveler": "general_agent",
}

# Map deliverable kind → role_models TaskClass so depth-0 leaf nodes
# (VERIFY/DELIVER/INTAKE) can auto_pick cheap models. Without this stamp every
# node defaults to unknown→min_rank=3→frontier and cheap-share stays 0%.
_KIND_TASK_CLASS = {
    "part": "multi_feature_part",
    "assembly": "assembly",
    "cam_program": "cam_program",
    "analysis": "analysis",
    "modification": "modification",
    "enclosure": "enclosure",
    "drawing": "simple_part",
    "report": "multi_feature_part",
    "work_instruction": "multi_feature_part",
    "weldment_traveler": "assembly",
}
# Phase/role leaf classes (low difficulty) — conservation routing targets.
_LEAF_TASK_CLASS = "leaf"
_REPORT_TASK_CLASS = "simple_part"

# Kinds that get a real BUILD + VERIFY node (not only SPECIFY → DELIVER leaf).
_BUILDABLE_KINDS = frozenset({
    "part", "assembly", "modification", "analysis", "drawing", "report",
    "work_instruction", "weldment_traveler",
})


def _role_prompt(role_id: str) -> str:
    """Load a role card's system prompt VERBATIM.  Best-effort: a
    missing card leaves the prompt empty so the node runner falls back to the
    default — the mission still runs (a role prompt is an enhancement, not a hard
    dependency of the DAG shape)."""
    try:
        from services.agent.roles import load_role
        return load_role(role_id).system_prompt
    except Exception:  # noqa: BLE001
        return ""


def _mission_context(manifest: dict[str, Any]) -> str:
    """The mission request VERBATIM — the context every node request embeds.

    A real C1 run proved the synthesized node requests referenced 'the
    project brief' without CONTAINING it, so the model (correctly) stalled asking
    for it. Every node request now carries the manifest's request_verbatim —
    nothing invented, just the §16 content the mission was compiled from."""
    req = (manifest.get("request_verbatim") or "").strip()
    return f"Mission request (verbatim):\n{req}" if req else ""


def _deliverable_request(manifest: dict[str, Any], d: dict[str, Any],
                         job: str) -> str:
    """A node request built from the deliverable's OWN manifest entry: name,
    kind, description and acceptance criteria (with each criterion's named check
    tool + pass rule), plus the verbatim mission request. *job* states this
    node's task in one sentence."""
    lines = [j for j in (_mission_context(manifest),) if j]
    name = d.get("name") or d.get("id") or "?"
    desc = (d.get("description") or "").strip()
    lines.append(f"Deliverable {d.get('id')}: {name} (kind: {d.get('kind')})"
                 + (f" — {desc}" if desc else ""))
    acs = d.get("acceptance_criteria") or []
    if acs:
        lines.append("Acceptance criteria:")
        for ac in acs:
            check = ac.get("check") or {}
            lines.append(f"- {ac.get('id')}: {ac.get('text')}"
                         f" [check tool: {check.get('tool', '?')};"
                         f" pass rule: {check.get('pass_rule', '?')}]")
    lines.append(f"Your job on this node: {job}")
    return "\n".join(lines)


def _deliverable_blob(d: dict[str, Any]) -> str:
    """Lowercased name+description for leg-routing heuristics."""
    parts = [
        str(d.get("name") or ""),
        str(d.get("description") or ""),
        str(d.get("id") or ""),
    ]
    return " ".join(parts).lower()


_MANUFACTURING_PLAN_PROCESS_IDS = frozenset({
    "cnc",
    "cnc_mill",
    "cnc_milling",
    "fabricated",
    "fabrication",
    "laser",
    "laser_cut",
    "lathe",
    "machined",
    "machining",
    "milled",
    "sheet_metal",
    "turned",
    "turning",
    "waterjet",
    "welded",
    "weldment",
})


def _needs_manufacturing_plan(d: dict[str, Any]) -> bool:
    """Whether this part/assembly needs the shop-planning BUILD pre-node."""
    if d.get("kind") not in {"part", "assembly"}:
        return False
    raw_process = (
        d.get("process")
        or d.get("manufacturing_process")
        or d.get("process_id")
        or ""
    )
    if isinstance(raw_process, dict):
        raw_process = (
            raw_process.get("id")
            or raw_process.get("process")
            or raw_process.get("profile_id")
            or raw_process.get("name")
            or ""
        )
    process = str(raw_process).strip().lower().replace("-", "_").replace(" ", "_")
    if process in _MANUFACTURING_PLAN_PROCESS_IDS:
        return True
    blob = _deliverable_blob(d)
    return any(cue in blob for cue in (
        "cnc", "fabricate", "fabricated", "fabrication", "laser cut",
        "laser-cut", "machined", "machining", "milled", "milling",
        "sheet metal", "turning", "turned", "waterjet", "welded", "weldment",
    ))


def _ledger_context_for(d: dict[str, Any]) -> list[str] | None:
    if d.get("kind") in {"drawing", "work_instruction"}:
        return ["mfg_plans"]
    return None


_PACKAGE_PHRASES = (
    "manufacturing package",
    "manufacturing-package",
    "shop package",
    "production drawing package",
)


def _coverage_criterion(cid: str, text: str, tool: str,
                        pass_rule: str) -> dict[str, Any]:
    return {
        "id": cid,
        "text": text,
        "check": {"tool": tool, "params_hint": {}, "pass_rule": pass_rule},
    }


def _expand_manufacturing_package(
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """Expand a generic multi-part manufacturing-package placeholder.

    Intake sometimes names one ``report`` called "manufacturing package". That
    is not an executable artifact: shops need controlled part drawings, an
    assembly drawing, a BOM/cut list and instructions. Expand that placeholder
    deterministically without mutating the validated caller-owned manifest.
    """
    original = [
        dict(d) for d in (manifest.get("deliverables") or [])
        if isinstance(d, dict)
    ]
    request_blob = str(manifest.get("request_verbatim") or "").lower()
    package_markers = [
        d for d in original
        if any(phrase in _deliverable_blob(d) for phrase in _PACKAGE_PHRASES)
    ]
    wants_package = (
        any(phrase in request_blob for phrase in _PACKAGE_PHRASES)
        or bool(package_markers)
    )
    parts = [d for d in original if d.get("kind") == "part"]
    assemblies = [d for d in original if d.get("kind") == "assembly"]
    if not wants_package or (len(parts) < 2 and not assemblies):
        return original

    marker_ids = {str(d.get("id")) for d in package_markers}
    marker_criteria = [
        dict(ac) for d in package_markers
        for ac in (d.get("acceptance_criteria") or [])
        if isinstance(ac, dict)
    ]
    expanded = [d for d in original if str(d.get("id")) not in marker_ids]
    used_ids = {str(d.get("id")) for d in expanded}

    def unique_id(base: str) -> str:
        candidate = base
        index = 2
        while candidate in used_ids:
            candidate = f"{base}_{index}"
            index += 1
        used_ids.add(candidate)
        return candidate

    def add_generated(base_id: str, *, name: str, kind: str,
                      description: str, depends_on: list[str],
                      criteria: list[dict[str, Any]]) -> str:
        did = unique_id(base_id)
        expanded.append({
            "id": did,
            "name": name,
            "kind": kind,
            "description": description,
            "acceptance_criteria": criteria,
            "depends_on": list(dict.fromkeys(depends_on)),
            "package_complexity": "multi_part",
        })
        return did

    drawing_ids: list[str] = []
    for part in parts:
        pid = str(part.get("id"))
        existing = next((
            d for d in expanded
            if d.get("kind") == "drawing"
            and list(d.get("depends_on") or []) == [pid]
        ), None)
        if existing:
            drawing_ids.append(str(existing.get("id")))
            continue
        drawing_ids.append(add_generated(
            f"{pid}_drawing",
            name=f"{part.get('name') or pid} manufacturing drawing",
            kind="drawing",
            description=(
                f"Controlled part drawing for {part.get('name') or pid}, including "
                "all ledger dimensions, tolerances, callouts and process notes."
            ),
            depends_on=[pid],
            criteria=[_coverage_criterion(
                f"{pid}_DRAWING_AUDIT",
                "Every critical specification row is covered on-sheet.",
                "audit_drawing",
                "verdict == release_ready",
            )],
        ))

    product_deps = [
        str(d.get("id")) for d in (assemblies or parts) if d.get("id")
    ]
    assembly_drawing = next((
        d for d in expanded
        if d.get("kind") == "drawing"
        and "assembly" in _deliverable_blob(d)
    ), None)
    if assembly_drawing:
        assembly_drawing_id = str(assembly_drawing.get("id"))
    else:
        assembly_drawing_id = add_generated(
            "manufacturing_assembly_drawing",
            name="Manufacturing assembly drawing",
            kind="drawing",
            description=(
                "Multi-part assembly/weldment drawing with balloons, interfaces, "
                "weld symbols, sections and controlled notes."
            ),
            depends_on=product_deps,
            criteria=[_coverage_criterion(
                "ASSEMBLY_DRAWING_AUDIT",
                "Assembly drawing covers interfaces, balloons and required welds.",
                "audit_drawing",
                "verdict == release_ready",
            )],
        )
    drawing_ids.append(assembly_drawing_id)

    bom = next((
        d for d in expanded
        if d.get("kind") == "report"
        and any(key in _deliverable_blob(d) for key in ("bom", "cut list"))
    ), None)
    if bom:
        bom_id = str(bom.get("id"))
    else:
        bom_id = add_generated(
            "manufacturing_bom",
            name="Manufacturing BOM / cut list",
            kind="report",
            description=(
                "Controlled bill of materials and weldment cut list with item "
                "numbers matching assembly-drawing balloons."
            ),
            depends_on=product_deps,
            criteria=[_coverage_criterion(
                "BOM_COMPLETE",
                "Every modeled part/component has a quantity and source row.",
                "manual",
                "controlled BOM/cut-list review passes",
            )],
        )

    instruction = next((
        d for d in expanded if d.get("kind") == "work_instruction"
    ), None)
    if instruction:
        instruction_id = str(instruction.get("id"))
    else:
        instruction_id = add_generated(
            "manufacturing_work_instructions",
            name="Manufacturing and assembly work instructions",
            kind="work_instruction",
            description=(
                "Ordered shop routing, setup, inspection hold points, finishing "
                "and assembly instructions with source provenance."
            ),
            depends_on=[*drawing_ids, bom_id],
            criteria=[
                *marker_criteria,
                _coverage_criterion(
                    "WORK_INSTRUCTIONS_COMPLETE",
                    "Routing, inspection, finish and assembly steps are controlled.",
                    "manual",
                    "work-instruction review passes",
                ),
            ],
        )

    weldment = any(
        "weld" in _deliverable_blob(d) for d in [*parts, *assemblies]
    )
    if weldment and not any(
        d.get("kind") == "weldment_traveler" for d in expanded
    ):
        add_generated(
            "manufacturing_weldment_traveler",
            name="Weldment traveler",
            kind="weldment_traveler",
            description=(
                "Weld sequence, procedure references, fit-up and inspection "
                "signoffs for the fabricated weldment."
            ),
            depends_on=[assembly_drawing_id, bom_id, instruction_id],
            criteria=[_coverage_criterion(
                "WELD_TRAVELER_COMPLETE",
                "Every weld has a sequence, procedure reference and inspection hold.",
                "manual",
                "weldment-traveler review passes",
            )],
        )
    return expanded


def _task_class_for(
    d: dict[str, Any],
    deliverables: list[dict[str, Any]],
) -> str:
    """Classify drawings by their real sheet/product complexity."""
    kind = str(d.get("kind") or "")
    if kind != "drawing":
        return _KIND_TASK_CLASS.get(kind, "unknown")
    blob = _deliverable_blob(d)
    deps = set(d.get("depends_on") or [])
    dep_kinds = {
        str(other.get("kind") or "")
        for other in deliverables if other.get("id") in deps
    }
    if (
        d.get("package_complexity") == "multi_part"
        or "assembly" in dep_kinds
        or len(deps) > 1
        or any(key in blob for key in (
            "assembly", "weldment", "welded", "multi-part", "multipart",
            "multi part", "bom", "cut list", "multi-sheet",
        ))
    ):
        return "assembly"
    if any(key in blob for key in (
        "thread", "tolerance", "gd&t", "datum", "surface finish",
        "chamfer", "ordinate", "section",
    )):
        return "multi_feature_part"
    return "simple_part"


def _resolve_build_role(d: dict[str, Any]) -> str:
    """Pick the BUILD worker role for a deliverable.

    Schematic/SPICE analysis → ``ecad_engineer``; PCB/board/antenna
    analysis → ``pcb_engineer``. A mixed schematic+board deliverable starts in
    ``ecad_engineer`` and uses its explicit handoff/delegate path to pcb_engineer.
    Firmware/sensor_loop → ``firmware_engineer``; generic analysis → sim_engineer.
    Reveng keyword scan (any kind) → ``re_modeler`` before the analysis-only gate.
    """
    kind = d.get("kind") or ""
    role = _BUILD_ROLE_BY_KIND.get(kind, "feature_modeler")
    blob = _deliverable_blob(d)
    reveng_keys = (
        "reverse", "reveng", "scan", "mesh", "point cloud",
        "photogrammetry", "reverse.engineer", "reverse engineer",
        "reverse-engineer",
    )
    if any(k in blob for k in reveng_keys):
        return "re_modeler"
    if kind != "analysis":
        return role
    fw_keys = (
        "firmware", "sensor_loop", "sensor-loop", "renode", "fw_build",
        "mcu firmware", "embedded",
    )
    if any(k in blob for k in fw_keys):
        return "firmware_engineer"
    schematic_keys = (
        "schematic", "spice", "erc", "netlist",
    )
    if any(k in blob for k in schematic_keys):
        return "ecad_engineer"
    pcb_keys = (
        "pcb", "board", "antenna", "rf", "drc", "gerber", "autoroute",
    )
    if any(k in blob for k in pcb_keys):
        return "pcb_engineer"
    ecad_keys = (
        "ecad", "flight_computer", "flight-computer", "avionics",
    )
    if any(k in blob for k in ecad_keys):
        return "ecad_engineer"
    return role


def _resolve_deliver_role(d: dict[str, Any]) -> str:
    """DELIVER role by kind. Producing happens on BUILD; DELIVER
    stays a cheap report/reviewer so one-shot leaves do not re-invent tools."""
    kind = d.get("kind") or ""
    return _DELIVER_ROLE_BY_KIND.get(kind, "reviewer")


def _build_job_for(d: dict[str, Any], role: str) -> str:
    """Concrete BUILD job text naming deliverable + entry tools."""
    if role == "ecad_engineer":
        return (
            "PRODUCE this deliverable's schematic artifacts with the engine "
            "tools — not prose. Entry path (forced order): ledger_get → "
            "ecad_new_schematic → ecad_symbols + ecad_place_symbol → ecad_wire "
            "+ ecad_add_power (nets REQUIRED; place-only yields an empty "
            "netlist) → ecad_run_erc + ecad_lint_schematic → "
            "enable_tools(categories=['ecad']) + ecad_simulate when SPICE is "
            "required → ecad_netlist + ecad_bom + ecad_plot. "
            "ledger_note the sch_doc_id, checks, and artifact refs. The physical "
            "board stage belongs to pcb_engineer: hand off that exact schematic "
            "package, and when this deliverable also requests a board, delegate "
            "pcb_engineer rather than claiming board/DRC work. Do not declare "
            "done without schematic tool evidence (or a blocked handoff naming "
            "the tool failure)."
        )
    if role == "pcb_engineer":
        return (
            "PRODUCE this deliverable's physical PCB and fabrication artifacts "
            "with the engine tools — not prose. Entry path: ledger_get the "
            "ecad_engineer schematic handoff; if no usable sch_doc_id exists, "
            "delegate that scoped capture stage to ecad_engineer → "
            "board_from_schematic → describe_board + ecad_status → "
            "ecad_update_from_sch when the source changed → ecad_autoroute → "
            "ecad_fill_zones → ecad_run_drc until errors==0 → "
            "ecad_export(fmt='gerber'). Preserve ecad_plot/ecad_bom artifacts. "
            "When antenna/RF is required, run ecad_antenna_design + "
            "ecad_antenna_analyze and report an honest backend SKIP/failure. "
            "ledger_note source sch_doc_id, pcb_doc_id, final DRC, and artifact "
            "refs. Do not declare done without a DRC-clean board and confirmed "
            "artifact tool results (or a blocked handoff naming the failure)."
        )
    if role == "firmware_engineer":
        return (
            "PRODUCE this deliverable's firmware with the engine tools — not "
            "prose. Entry path: fw_status → fw_new_project(mcu_family=stm32f4, "
            "board_doc_ref from ECAD when present) → fw_sources as needed → "
            "fw_build (returncode==0 + ELF). Sensor-loop when required: "
            "fw_sensor_scenario → fw_sim_run → fw_verify_board; honest SKIP "
            "when Renode/QEMU absent (ledger_note the provision absence). "
            "Do not declare done without fw_build tool evidence (or blocked "
            "with toolchain_not_found)."
        )
    if role == "sim_engineer":
        return (
            "run the named simulation studies that prove this deliverable's "
            "criteria (anchor first, then solve, then compare). Record "
            "verdicts with numbers — do not invent outcomes."
        )
    kind = str(d.get("kind") or "")
    if role == "drawing_detailer":
        return (
            "AUTHOR the controlled drawing artifact, not geometry or a prose "
            "summary. Start by calling mfg_plan_get(view=\"audit_spec\") when a "
            "manufacturing plan exists; that returned object is the canonical "
            "audit_drawing spec. Then make_drawing with model_items=false and place "
            "an explicit per-feature view/dimension/callout plan from the ledger. "
            "Cover threads, fits/tolerances, finishes, datums/GD&T, welds and "
            "notes as applicable. Run review_drawing for layout, then "
            "audit_drawing with the projected plan spec; export_drawing is allowed "
            "only when the current audit verdict is release_ready."
        )
    if kind == "report":
        return (
            "Call enable_tools(categories=[\"docs\"]), then BUILD the requested "
            "report artifact with make_report. Populate it from recorded tool "
            "evidence and return its path; a prose summary without the report "
            "file is not done."
        )
    if kind == "work_instruction":
        return (
            "Call enable_tools(categories=[\"docs\", \"cad\"]), read the governing "
            "mfg_plans route, then BUILD the controlled shop document with "
            "make_work_instruction. Include operation routing, stock/setup/fixturing, "
            "process parameters with provenance, inspection hold points, finishing "
            "and assembly steps. Return the artifact path; prose alone is not done."
        )
    if kind == "weldment_traveler":
        return (
            "Call enable_tools(categories=[\"docs\"]), then BUILD the controlled "
            "weld document with make_weldment_traveler. Cover every weld's "
            "procedure, fit-up, sequence, process variables and inspection/signoff "
            "hold points. Return the artifact path; prose alone is not done."
        )
    # Mechanical / default BUILD — concrete airframe geometry guidance
    name = str(d.get("name") or d.get("id") or "").lower()
    desc = str(d.get("description") or "").lower()
    blob = f"{name} {desc}"
    if any(k in blob for k in (
        "airframe", "rocket", "fuselage", "payload", "body_tube", "boost",
    )):
        return (
            "BUILD the boost-glide airframe as editable CAD features (not a "
            "hollow placeholder). Concrete geometry targets (use these dims "
            "unless ledger DIMs override): "
            "(1) body tube: revolve or extrude OD≈60 mm, wall≥1.5 mm, length≈520 mm "
            "overall with nose ogive ≈90 mm; "
            "(2) motor mount aft: tube ID≈29 mm OD≈32 mm length≈200 mm; "
            "(3) payload bay for FC: internal clear ≥56×86×20 mm (board envelope "
            "50×80×1.6 + component height; AC2 bay ≥ board); bay lid ≈2 mm; "
            "4× M2 mount bosses on 44×74 mm pattern raised ≈3 mm from bay floor; "
            "(4) wing/glide surface: span≈400 mm, root chord≈100 tip≈60, thickness≈3 mm; "
            "(5) 3 fins aft: root≈70 tip≈35 span≈55 thickness≈2.5 mm. "
            "Feature order: new_part → add_features solid body (revolve/extrude shell "
            "with real wall thickness — no zero-thickness sheets) → bay pocket/cut → "
            "fins/wing as pads or extrudes → measure_part volume. "
            "Keep walls ≥1.5 mm, manifold closed solid, volume large enough for bay "
            "(order 1e5 mm³ class). After solids exist run verify_battery(doc_id) and "
            "record honest PASS/WARN/FAIL — do not declare done without battery or "
            "check tool evidence."
        )
    return (
        "build this deliverable's geometry with the engine tools so every "
        "acceptance criterion can pass. Prefer real wall thicknesses (≥1.5 mm), "
        "closed manifold solids, and named features (box/revolve/extrude/cut) — "
        "not empty shells. After the solid exists, run verify_battery(doc_id) "
        "(or check_feature_overlap) and record the honest PASS/WARN/FAIL — do "
        "not declare done without a battery or check tool run."
    )




def _verify_job_for(d: dict[str, Any]) -> str:
    kind = str(d.get("kind") or "")
    if kind == "drawing":
        return (
            "run each acceptance criterion's NAMED check tool, then run "
            "review_drawing for current layout evidence and audit_drawing against "
            "the specification. PASS requires audit verdict release_ready; a clean "
            "layout or legacy manufacturing_ready flag alone is insufficient."
        )
    if kind in {"report", "work_instruction", "weldment_traveler"}:
        return (
            "run each acceptance criterion's NAMED check and inspect the produced "
            "document artifact for required sections and provenance. Report "
            "PASS/FAIL/NOT-CHECKED per criterion; do not substitute a prose "
            "summary for the file."
        )
    return (
        "run each acceptance criterion's NAMED check tool and report "
        "PASS/FAIL/NOT-CHECKED per criterion — never guess a result. ALWAYS also "
        "run verify_battery(doc_id) and log the per-check verdicts "
        "(PASS/WARN/FAIL honest) before handoff."
    )



def _node(
    node_id: str,
    node_type: str,
    name: str,
    role: str,
    *,
    inputs: Optional[dict[str, Any]] = None,
    on_fail: str = "pause",
    autonomy: str = "auto",
) -> dict[str, Any]:
    """Build one aicad.dag/1 node with its role assignment carried in ``inputs``.

    The role's system prompt is loaded verbatim and placed in ``inputs.role_prompt``
    so the node runner (services/dag/node_runner) can scope the turn to the role
    without re-reading the card.  ``inputs.role`` is the id (the runner applies the
    tool allowlist from it)."""
    node_inputs: dict[str, Any] = {k: v for k, v in (inputs or {}).items()
                                   if v is not None}
    node_inputs["role"] = role
    node_inputs["role_prompt"] = _role_prompt(role)
    return {
        "id": node_id,
        "type": node_type,
        "name": name,
        "inputs": node_inputs,
        "on_fail": on_fail,
        "autonomy": autonomy,
    }


def build_mission_graph(
    manifest: dict[str, Any],
    *,
    graph_id: str = "mission",
    is_assembly: Optional[bool] = None,
    org_layer: bool = False,
    default_autonomy: str = "auto",
    tier: Optional[str] = None,
) -> dict[str, Any]:
    """Compile a validated deliverable manifest into an ``aicad.dag/1`` mission graph.

    Args:
        manifest:        an ``aicad.deliverables/1`` manifest (already validated by
                         ``services.agent.pipeline.manifest.validate_manifest`` —
                         this function does NOT re-validate the manifest, it maps
                         it).
        graph_id:        the DAG id.
        is_assembly:     force the assembly path (layout_engineer + assembly build).
                         When ``None``, inferred from the presence of an
                         ``assembly``-kind deliverable.
        org_layer:       insert the ORGANIZE (department_manager) node (Phase 2.5,
                         feature-flagged).
        default_autonomy: node autonomy floor ("auto" | "approve" | "manual").
        tier:            optional model tier (``local-24k`` / ``local-48k`` / ``mid`` /
                         ``frontier``) recorded on EVERY node's ``inputs.tier``. This is
                         the plumbing a caller with no in-hand Provider object uses to
                         pin the mission to a tier (e.g. ``mission_start(tier=...)``
                         over MCP, followed by ``dag_run``): ``services.dag.node_runner``
                         reads ``inputs.tier`` and resolves it via
                         ``services.agent.tiers.provider_for_tier`` — the same seam the
                         gym bench uses — instead of silently falling through
                         to the ambient config.json / ``CHUDWARE_PROVIDER`` default.
                         ``None`` (the default) leaves every node's ``inputs`` exactly
                         as before — no behaviour change for callers that don't pass it.

    Returns:
        An ``aicad.dag/1`` graph dict (validated).  Raises ``GraphValidationError``
        if the assembled graph is malformed (a bug in this mapper, not the manifest).
    """
    deliverables = _expand_manufacturing_package(manifest)
    has_blocking = any(
        isinstance(a, dict) and a.get("blocking")
        for a in manifest.get("ambiguities", []) or []
    )
    if is_assembly is None:
        is_assembly = any(d.get("kind") == "assembly" for d in deliverables)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # Every node request EMBEDS the mission/deliverable content (verbatim
    # manifest data) — a synthesized request that merely references 'the project
    # brief' stalls a real model turn (proved by the C1 real run).
    mission_ctx = _mission_context(manifest)

    # --- Phase 0: INTAKE ---------------------------------------------------
    intake_id = "n_intake"
    nodes.append(_node(intake_id, _TYPE_INTAKE, "INTAKE: build manifest",
                       "intake_analyst", autonomy=default_autonomy,
                       inputs={"request": (
                           f"{mission_ctx}\n\nYour job on this node: confirm the "
                           "deliverable manifest for this mission is complete and "
                           "recorded." if mission_ctx else None),
                               "task_class": _LEAF_TASK_CLASS}))
    tail = intake_id

    # --- Phase 1: CLARIFY (only when a blocking ambiguity exists) ----------
    if has_blocking:
        clarify_id = "n_clarify"
        nodes.append(_node(clarify_id, _TYPE_CLARIFY, "CLARIFY: resolve ambiguities",
                           "clarifier", on_fail="pause", autonomy=default_autonomy,
                           inputs={"task_class": _LEAF_TASK_CLASS}))
        edges.append({"from": tail, "to": clarify_id})
        tail = clarify_id

    # --- Phase 2: SPECIFY (one node per deliverable) -----------------------
    spec_ids: dict[str, str] = {}
    for d in deliverables:
        did = d["id"]
        sid = f"n_spec_{did}"
        spec_ids[did] = sid
        tc = _task_class_for(d, deliverables)
        nodes.append(_node(sid, _TYPE_SPEC, f"SPECIFY {did}: {d.get('name', '')}",
                           "spec_writer",
                           inputs={"deliverable": did,
                                   "task_class": tc,
                                   "request": _deliverable_request(
                                       manifest, d,
                                       "write the engineering specification for this "
                                       "deliverable (dimensions, fits, materials) and "
                                       "record the key values.")},
                           autonomy=default_autonomy))
        edges.append({"from": tail, "to": sid})
    # A join point so the following phases depend on ALL specs.  It is a plan-type
    # node (role overseer) rather than a DAG 'gate' node: the SPECIFY exit gate is
    # enforced by the pipeline PhaseMachine (deterministic), and the overseer AUDIT
    # is a feature-flagged trigger — neither should hard-pause an
    # autonomous run.  The run-level autonomy_default still applies (raise the whole
    # run to approve/manual to gate every node, including this one).
    spec_join = "n_spec_done"
    nodes.append(_node(spec_join, "plan", "SPECIFY complete (overseer audit point)",
                       "overseer", on_fail="pause", autonomy=default_autonomy,
                       inputs={"request": (
                           f"{mission_ctx}\n\nYour job on this node: audit that every "
                           "deliverable's specification is complete; report any gap."
                           if mission_ctx else None),
                               "task_class": _LEAF_TASK_CLASS}))
    for sid in spec_ids.values():
        edges.append({"from": sid, "to": spec_join})
    tail = spec_join

    # --- Phase 2.5: ORGANIZE (feature-flagged) -----------------------------
    if org_layer:
        org_id = "n_organize"
        nodes.append(_node(org_id, _TYPE_ORGANIZE, "ORGANIZE: instantiate departments",
                           "department_manager", autonomy=default_autonomy,
                           inputs={"request": (
                               f"{mission_ctx}\n\nYour job on this node: group the "
                               "mission's deliverables into departments and confirm "
                               "each has an owner role." if mission_ctx else None),
                                   "task_class": _LEAF_TASK_CLASS}))
        edges.append({"from": tail, "to": org_id})
        tail = org_id

    # --- Phase 3: PLAN (layout first for assemblies, then orchestrator) ----
    if is_assembly:
        layout_id = "n_layout"
        nodes.append(_node(layout_id, _TYPE_LAYOUT, "PLAN: layout skeleton",
                           "layout_engineer", autonomy=default_autonomy,
                           inputs={"task_class": "assembly"}))
        edges.append({"from": tail, "to": layout_id})
        tail = layout_id
    plan_id = "n_plan"
    nodes.append(_node(plan_id, _TYPE_PLAN, "PLAN: mission DAG + build graphs",
                       "orchestrator", on_fail="pause", autonomy=default_autonomy,
                       inputs={"request": (
                           f"{mission_ctx}\n\nYour job on this node: produce the build "
                           "plan (ordered steps per deliverable) for this mission."
                           if mission_ctx else None),
                               "task_class": "multi_feature_part"}))
    edges.append({"from": tail, "to": plan_id})

    # --- Phase 4: BUILD (fan-out per buildable deliverable) ----------------
    # Analysis (ECAD/firmware) is buildable so legs get tool-capable
    # nodes instead of jumping SPECIFY → one-shot DELIVER with zero artifacts.
    build_ids: dict[str, str] = {}
    buildable = [d for d in deliverables if d.get("kind") in _BUILDABLE_KINDS]
    for d in buildable:
        did = d["id"]
        bid = f"n_build_{did}"
        build_ids[did] = bid
        role = _resolve_build_role(d)
        tc = _task_class_for(d, deliverables)
        # Domain tags help auto_pick / multi-domain gate classify producing legs.
        domain_tags: list[str] = []
        if role == "ecad_engineer":
            domain_tags = ["ecad", "analysis"]
        elif role == "pcb_engineer":
            domain_tags = ["ecad", "pcb", "analysis"]
        elif role == "firmware_engineer":
            domain_tags = ["firmware", "analysis"]
        elif role == "sim_engineer":
            domain_tags = ["analysis"]
        elif d.get("kind") == "assembly":
            domain_tags = ["assembly", "geometry"]
        elif d.get("kind") in ("part", "modification"):
            domain_tags = ["geometry"]
        # BUILD depends on PLAN (build graphs) + this deliverable's SPEC. Independent
        # parts share no edge → the executor ready-set runs them in parallel.
        build_inputs: dict[str, Any] = {
            "deliverable": did,
            "task_class": tc,
            "kind": d.get("kind"),
            "request": _deliverable_request(
                manifest, d, _build_job_for(d, role)),
        }
        ledger_context = _ledger_context_for(d)
        if ledger_context:
            build_inputs["ledger_sections"] = ledger_context
        if domain_tags:
            build_inputs["domain_tags"] = domain_tags
        needs_mfg_plan = _needs_manufacturing_plan(d)
        if needs_mfg_plan:
            mfg_plan_id = f"n_mfg_plan_{did}"
            nodes.append(_node(
                mfg_plan_id,
                _TYPE_PLAN,
                f"BUILD PLAN {did}: manufacturing route",
                "manufacturing_planner",
                inputs={
                    "deliverable": did,
                    "task_class": tc,
                    "kind": d.get("kind"),
                    "request": _deliverable_request(
                        manifest,
                        d,
                        "derive and persist the shop-ready manufacturing plan before "
                        "any modeling: process rationale, stock, datum/setup strategy, "
                        "operation sequence, function-derived critical dimensions, "
                        "inspection plan, and open risks.",
                    ),
                },
                on_fail="pause",
                autonomy=default_autonomy,
            ))
            edges.append({"from": plan_id, "to": mfg_plan_id})
        nodes.append(_node(bid, _TYPE_BUILD, f"BUILD {did}: {d.get('name', '')}",
                           role,
                           inputs=build_inputs,
                           on_fail="pause", autonomy=default_autonomy))
        edges.append({
            "from": mfg_plan_id if needs_mfg_plan else plan_id,
            "to": bid,
        })

    # Preserve declared artifact dependencies inside the BUILD fan-out. PLAN still
    # precedes every build, while independent parts retain no peer edge.
    for d in buildable:
        bid = build_ids[d["id"]]
        for dep in d.get("depends_on") or []:
            dep_bid = build_ids.get(dep)
            if dep_bid:
                edges.append({"from": dep_bid, "to": bid})

    # --- Phase 5: VERIFY (one per buildable deliverable, runs the ACs) -----
    verify_ids: dict[str, str] = {}
    for d in buildable:
        did = d["id"]
        vid = f"n_verify_{did}"
        verify_ids[did] = vid
        nodes.append(_node(vid, _TYPE_VERIFY, f"VERIFY {did}: run acceptance checks",
                           "reviewer",
                           inputs={"deliverable": did,
                                   "task_class": _LEAF_TASK_CLASS,
                                   "risk": "low",
                                   "ledger_sections": _ledger_context_for(d),
                                   "request": _deliverable_request(
                                       manifest, d, _verify_job_for(d))},
                           on_fail="pause", autonomy=default_autonomy))
        edges.append({"from": build_ids[did], "to": vid})

    # --- Phase 7: DELIVER (one per deliverable, role by kind) --------------
    for d in deliverables:
        did = d["id"]
        kind = d.get("kind")
        del_id = f"n_deliver_{did}"
        role = _resolve_deliver_role(d)
        # Producing analysis legs already ran tools on BUILD; DELIVER stays a
        # cheap report class so conservation routing does not re-inflate.
        nodes.append(_node(del_id, _TYPE_DELIVER, f"DELIVER {did}: {kind}",
                           role,
                           inputs={"deliverable": did,
                                   "task_class": _REPORT_TASK_CLASS,
                                   "risk": "low",
                                   "ledger_sections": _ledger_context_for(d),
                                   "request": _deliverable_request(
                                       manifest, d,
                                       "summarise what was built and verified for "
                                       "this deliverable, from the recorded results "
                                       "only — do not invent outcomes.")},
                           on_fail="skip", autonomy=default_autonomy))
        # Deliver depends on this deliverable's VERIFY when it was built; otherwise
        # on its SPEC (e.g. a report deliverable with no build node).
        upstream = verify_ids.get(did) or spec_ids.get(did) or plan_id
        edges.append({"from": upstream, "to": del_id})

    # Record the tier on EVERY node's inputs (best-effort, additive only) so
    # node_runner can resolve the matching provider per-node when no Provider
    # object is threaded in directly (the mission_start/dag_run MCP path — see
    # the ``tier`` arg docstring above). A caller that omits ``tier`` gets
    # exactly the graph this function has always produced.
    if tier:
        for n in nodes:
            n.setdefault("inputs", {})["tier"] = tier

    # Safety net: every node must carry task_class for auto_pick.
    for n in nodes:
        ni = n.setdefault("inputs", {})
        if not ni.get("task_class"):
            ni["task_class"] = _LEAF_TASK_CLASS
    graph = {
        "schema": "aicad.dag/1",
        "id": graph_id,
        "nodes": nodes,
        "edges": edges,
    }
    validate_graph(graph)
    return graph
