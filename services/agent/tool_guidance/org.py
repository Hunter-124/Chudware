"""tool_guidance.org — curated options for the org-layer tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "channel_send": {
        "prompt_note": (
            "Cross-department channel post (org layer must be active); "
            "interface_change auto-acks and blocks your stage exit until "
            "acked — keep the body under 120 words."
        ),
    },
    "request_raise": {
        "prompt_note": (
            "Raise a worker request to manager/orchestrator/overseer instead "
            "of stalling on missing context, resources, or an escalation."
        ),
    },
    "request_resolve": {
        "prompt_note": (
            "Managers: every raised request MUST be explicitly resolved "
            "(grant|deny|escalate) — silent expiry is impossible."
        ),
    },
    "council_convene": {
        "prompt_note": (
            "Convenes a 2-round multi-seat deliberation (build_strategy, "
            "spec_review, failure_review, …); stage/tier default from mission "
            "context — slow, reserve for genuinely contested calls."
        ),
        "timeout_s": 600,
    },
}
