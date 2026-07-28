"""tool_guidance.advisor — curated options for the advisor tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "advise": {
        "prompt_note": (
            "Match stage to your workflow phase — the default 'assembly' "
            "only runs interference; pass run_id to log for dispute/replay."
        ),
    },
    "advisory_dispute": {
        "prompt_note": (
            "Contest only with concrete evidence; kernel-sourced disputes "
            "return waiver_required=true — a human must confirm the waiver."
        ),
    },
    "serving_evaluate": {
        "prompt_note": (
            "Evidence only — promotes nothing. Pass report_out to write the "
            "sign-off artifact serving_promote then needs as report_ref."
        ),
    },
    "serving_promote": {
        "prompt_note": (
            "No auto-promote path: report_ref must exist and evidence must "
            "recompute eligible or promotion_refused; confirm approver first."
        ),
    },
    "serving_rollback": {
        "prompt_note": (
            "Always-safe routing flip after a bad promote or canary "
            "regression; omit to_version to restore the previous adapter "
            "from history."
        ),
    },
    "serving_set_canary": {
        "prompt_note": (
            "Middle rung between evaluate and promote; opt_in replaces "
            "(not extends) the early-adopter list — pass the full list."
        ),
    },
    "serving_status": {
        "prompt_note": (
            "Read-only routing snapshot — call first for exact "
            "insertion_point names and versions before canary/promote/"
            "rollback."
        ),
    },
    "stage_set": {
        "prompt_note": (
            "Only when the inferred stage is actually wrong; needs an "
            "existing run ledger (run_id) and a replay-visible real reason."
        ),
    },
}
