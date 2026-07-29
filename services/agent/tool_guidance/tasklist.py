"""tool_guidance.tasklist — curated options for the live task-list tool.

``task_list`` is a floor tool on the general lane (roles/general_agent.yaml), not
a category-pack member, so its guidance lives in its own family module.
"""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "task_list": {
        "prompt_note": (
            "Publish the FULL list every call (omitted items disappear from "
            "the panel) — same order, updated statuses, after each step."
        ),
        # Re-publishing after every step is the point, so no tight per-turn cap
        # — but a model that loops on it burns the turn; 40 matches the item cap.
        "max_calls_per_turn": 40,
    },
}
