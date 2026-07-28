"""tool_guidance.cam — curated options for the cam tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    # ------------------------------------------------------------------ job lifecycle
    "cam_new_job": {
        "prompt_note": (
            "Entry point of every CAM workflow — call first; all siblings "
            "act on the job it makes current. Then cam_add_tool/setup/op."
        ),
    },
    "cam_job_tree": {
        "prompt_note": (
            "Cheap state read — use it to recover setup_id / op_id / tool "
            "numbers after context loss instead of re-creating the job."
        ),
    },
    "cam_undo": {
        "prompt_note": (
            "Reverts the last job edit (ops, tools, setups, stock) — not "
            "toolpath generation. Prefer cam_update_operation to fix a bad "
            "param."
        ),
    },
    "cam_redo": {
        "prompt_note": (
            "Re-applies the last undone edit — only meaningful right after "
            "cam_undo."
        ),
    },
    # ------------------------------------------------------------------ tools / stock / setup
    "cam_add_tool": {
        "prompt_note": (
            "Define tools before ops — cam_add_operation references them by "
            "tool_number, and reusing a number replaces. cam_tool_types lists "
            "valid types."
        ),
    },
    "cam_add_setup": {
        "prompt_note": (
            "Needs an active job — cam_new_job first. Stock defaults to a bbox "
            "box-offset; pass cam_extract(kind='stock') output for tighter "
            "stock."
        ),
    },
    "cam_set_stock": {
        "prompt_note": (
            "from_sim chains setups: cam_simulate setup 1, then reuse its "
            "remaining stock as setup 2's stock for a flip/second-side op."
        ),
    },
    # ------------------------------------------------------------------ operations
    "cam_add_operation": {
        "prompt_note": (
            "Get geometry from cam_extract first; omit rpm/feed in params to "
            "auto-resolve from material+tool. Call cam_generate after adding "
            "ops."
        ),
    },
    "cam_update_operation": {
        "prompt_note": (
            "Merge-patch — only the keys you pass change. Edits do not "
            "regenerate toolpaths; call cam_generate afterwards."
        ),
    },
    "cam_remove_operation": {
        "prompt_note": (
            "Undoable via cam_undo. Check cam_job_tree for current op_ids "
            "before removing."
        ),
    },
    # ------------------------------------------------------------------ extraction
    "cam_extract": {
        "prompt_note": (
            "Reads the CURRENT part (build/open one first) unless path= is "
            "given. kind='holes' feeds drill geometry, 'stock' feeds "
            "cam_add_setup."
        ),
        "timeout_s": 180,
    },
    # ------------------------------------------------------------------ generate / sim / post
    "cam_generate": {
        "prompt_note": (
            "Run after all ops are added, before cam_simulate/cam_post — its "
            "warnings (e.g. tool too large for a pocket) beat simulation "
            "failures."
        ),
        "timeout_s": 300,
    },
    "cam_simulate": {
        "prompt_note": (
            "Needs generated toolpaths — cam_generate first. Smaller "
            "resolution = finer/slower; check_holder only flags when "
            "holder geometry is defined."
        ),
        "timeout_s": 600,
    },
    "cam_post": {
        "prompt_note": (
            "Final step once cam_simulate is clean; verify the output with "
            "cam_audit before shipping it to a machine."
        ),
        "timeout_s": 180,
    },
    "cam_audit": {
        "prompt_note": (
            "Deterministic G-code verify — run after cam_post before "
            "shipping; ok=false means errors, but review warnings too. Works "
            "job-free via gcode=."
        ),
        "timeout_s": 180,
    },
    "cam_setup_sheet": {
        "prompt_note": (
            "Operator-facing summary for the machine floor — needs generated "
            "toolpaths (cam_generate first) for cut lengths and times."
        ),
    },
    # ------------------------------------------------------------------ persistence
    "cam_save": {
        "prompt_note": (
            "Persists the job tree (not G-code) to .cam+; reopen with "
            "cam_open, review changes later with cam_job_diff."
        ),
    },
    "cam_open": {
        "prompt_note": (
            "Replaces the active job — cam_save any unsaved work first."
        ),
    },
    "cam_job_diff": {
        "prompt_note": (
            "Run before cam_save to review what changed since the last save, "
            "or to sanity-check a hand-edited .cam+ against the live job."
        ),
    },
    # ------------------------------------------------------------------ reference data
    "cam_post_dialects": {
        "prompt_note": (
            "Static list — one call suffices. Pick the dialect at "
            "cam_new_job(post=...) or override it at cam_post."
        ),
    },
    "cam_materials": {
        "prompt_note": (
            "Valid material ids for cam_add_setup and feeds_speeds; the "
            "CuttingDB group ids are what the feeds/speeds priors key on."
        ),
    },
    "cam_op_types": {
        "prompt_note": (
            "Valid type strings for cam_add_operation; the op type decides "
            "which geometry key (regions/contours/points/profile/mesh) it "
            "needs."
        ),
    },
    "cam_tool_types": {
        "prompt_note": (
            "Valid type strings for cam_add_tool, each with the cutting-data "
            "class that feeds/speeds auto-resolution uses."
        ),
    },
    # ------------------------------------------------------------------ feeds/speeds calc
    "cam_check_feeds_envelope": {
        "prompt_note": (
            "Range-checks caller-pinned rpm/feed/stepdown against the table "
            "envelope; use feeds_speeds when you want the envelope itself."
        ),
    },
    "feeds_speeds": {
        "prompt_note": (
            "Look up the envelope before pinning params in cam_add_operation; "
            "pass machine= to clamp to a real machine's rpm/feed limits."
        ),
    },
    "tap_drill": {
        "prompt_note": (
            "Quick drill size for a thread string; feeds_speeds(thread=...) "
            "returns the same value alongside cutting data."
        ),
    },
}
