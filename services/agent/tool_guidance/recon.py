"""tool_guidance.recon — curated options for the recon tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "compare_parts": {
        "prompt_note": (
            "Re-prove any manual rebuild/edit against the reference (doc_a). No "
            "alignment is done — a moved copy scores low; degraded:true means "
            "the boolean itself failed."
        ),
    },
    "convert_face_to_sketch": {
        "prompt_note": (
            "Creates a NEW sketch on one planar face; to trace into an open "
            "sketch use project_into_sketch. Get face sids from describe_part, "
            "then extrude/cut/revolve the regions."
        ),
    },
    "project_into_sketch": {
        "prompt_note": (
            "Appends to an EXISTING sketch (convert_face_to_sketch creates "
            "one). 'front'/'top'/'right' mean datum planes, not faces — use a "
            "sid or side word for solid faces."
        ),
    },
    "reverse_engineer": {
        "prompt_note": (
            "Long rebuild+verify pipeline. Afterwards read rejected/candidates/"
            "unexplained, fix by hand, re-prove with compare_parts; hints come "
            "from reveng_fit_primitives."
        ),
        "timeout_s": 600,
    },
    "start_capture_session": {
        "prompt_note": (
            "One session per port — a duplicate call fails and returns the "
            "live upload_url. scale_marker_mm is only recorded; real scale "
            "comes via reveng_set_scale."
        ),
    },
    "stop_capture_session": {
        "prompt_note": (
            "Needs MESHROOM_BIN or COLMAP_BIN set; takes minutes. Mesh is in "
            "reconstruction units, not mm — reveng_set_scale first, then "
            "reverse_engineer(source=mesh_path)."
        ),
        "timeout_s": 600,
    },
}
