"""Per-tool recovery hints appended to dispatch failures.

Settings/content are static: keep short, actionable, and name a next tool.
"""
from __future__ import annotations

ERROR_HINTS: dict[str, list[str]] = {
    "add_features": [
        "Check feature_params / op vocabulary via cad_guide(); describe_part first for face refs.",
    ],
    "solve_mates": [
        "Over/under-constrained mates — run interface_check and review the mate list.",
    ],
    "run_fea_static": [
        "Mesh/BC failure — simplify geometry, check fixtures/loads on named faces, coarse mesh first.",
    ],
    "cam_generate": [
        "Need stock and setup first — cam_set_stock then cam_add_setup before generate.",
    ],
    "autoroute": [
        "Unrouted nets — check net classes and placement density; run_erc before autoroute.",
    ],
    "make_drawing": [
        "Add views first (add_drawing_view) and describe_part for entity ids before dimensions.",
    ],
    "add_dimension": [
        "Missing view/entity — add_drawing_view first; pull sizes from measure_part/describe_part.",
    ],
    "reverse_engineer": [
        "Poor fit — add photos/views or tighten seeds, then compare_parts in a loop.",
    ],
    "part_place": [
        "Part not found — part_search / part_get first, then place by part_id.",
    ],
    "import_part": [
        "Format/units issue — confirm file path/extension and document units before import.",
    ],
    "plm_release": [
        "State conflict — plm_status and plm_where_used before release.",
    ],
}


def hint_for(tool_name: str) -> str | None:
    """Return the first recovery hint for *tool_name*, or None."""
    if not tool_name:
        return None
    hints = ERROR_HINTS.get(str(tool_name).strip())
    if not hints:
        return None
    return str(hints[0])
