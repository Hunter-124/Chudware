"""tool_guidance.ledger — curated options for the ledger tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "ledger_get": {
        "prompt_note": (
            "Section names are fuzzy-matched — pass one bare name (decisions, "
            "dimensions, …) or omit for the full render; raise budget_tokens "
            "for large ledgers."
        ),
    },
    "ledger_note": {
        "prompt_note": (
            "The run's append-only memory: record every decision/dimension "
            "with provenance so later turns never re-derive a number."
        ),
    },
    "ledger_events": {
        "prompt_note": (
            "Raw event stream for the GUI transparency feed — agents almost "
            "always want ledger_get instead."
        ),
    },
}
