"""tool_guidance.mission — curated options for the mission/DAG/buildgraph tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "buildgraph_plan": {
        "prompt_note": (
            "Prefer this over dag_plan when the build steps are already "
            "known — deterministic, LLM-free; check advisories + interpreted "
            "first."
        ),
    },
    "buildgraph_status": {
        "prompt_note": (
            "Poll with the run_id from buildgraph_plan; an unknown id returns "
            "known_run_ids — pick from that list, never invent one."
        ),
    },
    "dag_abort": {
        "prompt_note": (
            "Cancels a gate-paused run (decision=reject); omit run_id to hit "
            "this session's latest run. autonomy_default must match dag_run."
        ),
    },
    "dag_approve": {
        "prompt_note": (
            "The continue-button after dag_run returns interrupted — resumes "
            "execution to the next gate; autonomy_default must match dag_run."
        ),
        "timeout_s": 600,
    },
    "dag_plan": {
        "prompt_note": (
            "LLM planner — verify the returned graph before dag_run; for a "
            "known mechanical sequence prefer deterministic buildgraph_plan "
            "instead."
        ),
        "timeout_s": 300,
    },
    "dag_replan": {
        "prompt_note": (
            "Mid-run surgery: spliced nodes need end_condition + a back-edge "
            "with retry_budget or validation fails; route via replan_request "
            "first."
        ),
        "timeout_s": 600,
    },
    "dag_run": {
        "prompt_note": (
            "Plan with dag_plan first (omit graph_json to reuse the last "
            "plan); interrupted in the result → paused at a gate: "
            "dag_approve/dag_abort."
        ),
        "timeout_s": 600,
    },
    "dag_status": {
        "prompt_note": (
            "Restart-safe topology + gate_pending check; per-node verdicts "
            "exist only in the run's own session — use mission_status for "
            "the phase narrative."
        ),
    },
    "mission_start": {
        "prompt_note": (
            "Compiles but does not execute — hand the graph to dag_run. "
            "Without an intake_analyst manifest you get the intake "
            "instruction back, not a plan."
        ),
    },
    "mission_status": {
        "prompt_note": (
            "Phase-level view (stage_log, open questions) from the run's "
            "ledger; use dag_status for raw topology. Fails if the run has "
            "no ledger."
        ),
    },
    "replan_request": {
        "prompt_note": (
            "Routing dry-run only — reports required approvers, changes "
            "nothing; apply with dag_replan once ruled on."
        ),
    },
}
