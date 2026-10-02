"""tool_guidance.intake — curated options for the intake tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "ask_user": {
        "prompt_note": (
            "No-human path: each question is answered with its `suggested` "
            "default (source:'default'), never fabricated; pass run_id so "
            "gates replay."
        ),
    },
    "confirm_dimensions": {
        "prompt_note": (
            "The only way to close fit-critical demands: typed caliper "
            "values, not photo reads. Pass run_id so DAG gates never re-ask."
        ),
    },
    "dfm_check": {
        "prompt_note": (
            "Run before export or slicing; uses the process bound by set_process "
            "unless you pass process=. For FDM it measures walls, overhangs, "
            "bridges, support (incl. trapped), holes, internal voids, bed adhesion "
            "and bed fit (printer=); rows name faces/features to fix."
        ),
    },
    "fit_report": {
        "prompt_note": (
            "No args = document report (needs measurements recorded first); "
            "explicit bore/shaft args = ad-hoc one-off, same engine as "
            "fit_calc."
        ),
    },
    "intake_status": {
        "prompt_note": (
            "Cheap orient call before any intake work: bound process, open "
            "critical-dim demands, and any pending photo request to resume."
        ),
    },
    "make_fit_coupon": {
        "prompt_note": (
            "Needs measured dims on the interface first; returns a NEW "
            "doc_id to export with export_part for a real test-fit before "
            "the full print."
        ),
    },
    "measure_from_photo": {
        "prompt_note": (
            "Needs a photo_id from submit_part_photos first; px_a/px_b are "
            "FULL-RES pixel coords; low-tier reads still demand "
            "confirm_dimensions."
        ),
    },
    "request_part_photos": {
        "prompt_note": (
            "Only after standard-parts lookup fails to resolve the part; "
            "the user's files come back next turn via submit_part_photos."
        ),
    },
    "resolve_process": {
        "prompt_note": (
            "Decide once, then bind the choice with set_process. Pass run_id "
            "so the clarification is ledgered and a DAG gate never re-asks."
        ),
    },
    "set_process": {
        "prompt_note": (
            "Call once the process is known (FDM default) — its clearance/"
            "wall/thread numbers drive later fit-budgeted dims and dfm_check."
        ),
    },
    "submit_part_photos": {
        "prompt_note": (
            "photos= registers files (then measure_from_photo); claims= "
            "submits dims you read. Close returned open_demands via "
            "confirm_dimensions."
        ),
    },
}
