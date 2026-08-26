"""tool_guidance.workspace — curated options for workspace tools + pseudo-tools.

The pseudo-tools (enable_tools / list_tool_categories / design_recipe /
delegate) have no live registry callable outside a mission dispatcher; their
guidance lives here with the workspace family.
"""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "workspace_status": {
        "prompt_note": (
            "One-call orient snapshot: open docs, owners, node statuses. Poll "
            "this before touching docs you did not create."
        ),
    },
    "workspace_doc_events": {
        "prompt_note": (
            "Incremental per-doc event stream — pass the count of events you "
            "already have as since_seq."
        ),
    },
    "submit_bug_report": {
        "prompt_note": (
            "Only for OUR bugs (a schema-conformant call still fails, a crash, "
            "an unknown method) — never for ordinary retry-and-fix errors."
        ),
    },
    "enable_tools": {
        "prompt_note": (
            "Enable only the packs the current step needs — every enabled "
            "tool costs prompt tokens for the rest of the turn."
        ),
    },
    "list_tool_categories": {
        "prompt_note": (
            "List the requestable category packs, then unlock with "
            "enable_tools(categories=[...])."
        ),
    },
    "design_recipe": {
        "prompt_note": (
            "Empty query lists the recipe catalog; a natural-language query "
            "returns ordered steps with params and verify hints."
        ),
    },
    "delegate": {
        "prompt_note": (
            "The brief must be fully self-contained — the child sees no chat "
            "history. Give role + concrete task + acceptance criteria."
        ),
    },
}
