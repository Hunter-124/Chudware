"""tool_guidance.gsl — curated options for the GSL (Geometry-Semantics Layer) tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "describe_part": {
        "prompt_note": (
            "Make this the first read on any part — the returned sids are "
            "the stable addresses every later hover() and edit call uses."
        ),
    },
    "query_adjacency": {
        "prompt_note": (
            "Reach for it to reason about a shared edge — convex "
            "outside-corner vs concave pocket edge — which the flat per-face "
            "read-back never carries."
        ),
    },
    "hover": {
        "prompt_note": (
            "Feed it a sid from describe_part / query_geometry; hover only "
            "the 1-2 entities an edit touches instead of re-reading the "
            "whole outline."
        ),
    },
    "describe_sketch": {
        "prompt_note": (
            "Takes the sketch feature id ('s1'); once the shape is known, "
            "check_sketch returns the same DOF verdict in a much smaller "
            "payload."
        ),
    },
    "sketch_health": {
        "prompt_note": (
            "Verify step after every sketch edit: apply the schedule in "
            "order (relations first, then driving dimensions) and re-run "
            "until status is 'full'."
        ),
    },
    "sketch_auto_constrain": {
        "prompt_note": (
            "Auto-runs after sketch edits (sketch.auto_coincident, default "
            "ON) — call explicitly only to preview (apply=False) or "
            "re-stitch with a looser tol_mm."
        ),
    },
    "describe_scene": {
        "prompt_note": (
            "Occlusion is not modeled — a hidden face still lists if it "
            "faces the camera; confirm true visibility with render_part "
            "when vision is available."
        ),
    },
    "describe_assembly": {
        "prompt_note": (
            "Run solve_mates first — placements and gaps are narrated as "
            "stored, so stale mate state means stale numbers. For a single "
            "solid part use describe_part."
        ),
    },
    "describe_results": {
        "prompt_note": (
            "Run a study first (run_fea_static / run_fea_modal / "
            "run_cfd_flow) — it narrates persisted results only and never "
            "recomputes."
        ),
    },
    "describe_toolpaths": {
        "prompt_note": (
            "Needs an active CAM job with results — cam_generate + "
            "cam_simulate first; audit=False skips the cam_audit summary "
            "for a shorter read."
        ),
    },
    "describe_board": {
        "prompt_note": (
            "Needs an open ECAD doc (ecad_new_schematic / ecad_new_pcb) "
            "first; narrates stored ERC/DRC numbers only — it never "
            "re-runs the checks."
        ),
    },
    "describe_project": {
        "prompt_note": (
            "Pass dag_status()['run'] for live status; a bare brief string "
            "is only narrated back — describe_project reports runs, "
            "dag_plan creates them."
        ),
    },
    "render_ascii": {
        "prompt_note": (
            "Experimental last resort for no-vision models — prefer "
            "describe_part / render_part; only its own p-handle callouts "
            "are addressable here, not sids."
        ),
    },
    "render_sketch_ascii": {
        "prompt_note": (
            "add_features batches already append a compact auto-readback — "
            "read that first; pair this with sketch_health for the final "
            "verify loop."
        ),
    },
    "render_schematic_ascii": {
        "prompt_note": (
            "Keep its grid+index output — that is the cell contract "
            "apply_ascii_edits(kind='schematic') resolves edits against."
        ),
    },
    "render_board_ascii": {
        "prompt_note": (
            "Board mutations already append a compact auto-readback — read "
            "that first; use this uncapped full render as the final review "
            "before declaring a board done."
        ),
    },
    "render_sim_ascii": {
        "prompt_note": (
            "Topological, not spatial — the read after ecad_simulate (or "
            "pass offline JSON); for cell-addressed sheet edits use "
            "render_schematic_ascii."
        ),
    },
    "apply_ascii_edits": {
        "prompt_note": (
            "Preview with dry_run=True; pass grid_json + index_json from "
            "the last render_*_ascii reply so cell coords resolve against "
            "that raster, not a re-render."
        ),
    },
}
