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
    "stage_set": {
        "prompt_note": (
            "Only when the inferred stage is actually wrong; needs an "
            "existing run ledger (run_id) and a replay-visible real reason."
        ),
    },
}
