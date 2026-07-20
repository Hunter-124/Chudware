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
        "edit_feature",
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
        "check_sketch_d",
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
        "interference_check",
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
        "describe_toolpaths",
    ],
    "ecad": [
        "family:ecad",
        "ecad_new_schematic",
        "ecad_new_pcb",
        "ecad_place_symbol",
        "ecad_wire",
        "ecad_find_part",
        "ecad_status",
        "ecad_run_erc",
        "ecad_run_drc",
        "ecad_simulate",
        "board_from_schematic",
        "describe_board",
    ],
    "assembly": [
        "new_assembly",
        "asm_add_component",
        "asm_mate",
        "solve_mates",
        "describe_assembly",
        "check_interference",
        "interference_check",
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
        "list_parts",
        "get_part",
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
        "add_surface_finish",
        "set_view",
        "set_titleblock",
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
        "ledger_get",
        "ledger_note",
        "buildgraph_plan",
        "buildgraph_status",
        "corporate_channel_read",
        "corporate_chart",
        "coordinate_board",
        "coordinate_progress",
    ],
    "sheetmetal": [
        "add_features",  # sm_base_flange / sm_edge_flange / sm_flat_pattern via ops
        "edit_feature",
        "set_process",
        "research_eng_calc",  # bend_allowance
        "dfm_check",
        "wall_thickness",
        "measure_part",
        "export_part",
    ],
}

_CATEGORY_DESCS: dict[str, str] = {
    "modeling": "Part/assembly geometry: add_features, edit, new/save/export docs",
    "sketch": "2D sketch health, constraints, project/convert faces",
    "measure": "Mass props, distances, geometry queries, hover",
    "inspect": "Visual inspect/render, DFM, interference, design review",
    "research": "Web search, materials, standards, eng calcs, datasheets",
    "sim": "FEA/CFD/motion and EM analysis jobs",
    "cam": "CAM jobs, ops, generate/post, feeds & speeds",
    "ecad": "Schematic/PCB place-wire-board and ERC/DRC/sim",
    "assembly": "Components, mates, stackup and fit calcs",
    "sourcing": "Parts library search, BOM, vendors, scrape import",
    "drawing": "2D drawings, GD&T, export PDF/DXF",
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
    """
    limit = CATEGORY_EXPAND_CAP if cap is None else max(0, int(cap))
    out: set[str] = set()
    for raw in names or []:
        key = str(raw or "").strip().lower()
        if not key:
            continue
        pack = CATEGORY_PACKS.get(key)
        if not pack:
            continue
        for token in pack:
            out |= _expand_token(token)
            if len(out) >= limit:
                # Deterministic trim when over cap.
                return set(sorted(out)[:limit])
    if len(out) > limit:
        return set(sorted(out)[:limit])
    return out
