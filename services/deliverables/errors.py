"""Contract-shaped failures (contract §6).

Every tool returns ``{"ok": false, "reason": "<snake_case>", "hint": "<actionable>"}`` on
failure — never a traceback, never a partial file with a hole. Internally the module raises
:class:`DeliverableError`; the API layer catches it and emits the envelope. The frozen minimum
``reason`` set every caller can rely on is enumerated in :data:`REASONS`.
"""
from __future__ import annotations

# Frozen minimum reason codes (contract §6). Implementations MAY add more in a later version.
REASONS = frozenset({
    "invalid_spec",
    "provenance_violation",
    "slot_unresolved",
    "unsupported_format",
    "converter_missing",
    "artifact_not_found",
})


class DeliverableError(Exception):
    """A contract-shaped failure. ``reason`` is a §6 code; ``hint`` is actionable prose."""

    def __init__(self, reason: str, hint: str) -> None:
        super().__init__(f"{reason}: {hint}")
        self.reason = reason
        self.hint = hint

    def envelope(self) -> dict:
        return {"ok": False, "reason": self.reason, "hint": self.hint}


def fail(reason: str, hint: str) -> dict:
    """Build a failure envelope directly (for call sites that don't raise)."""
    return {"ok": False, "reason": reason, "hint": hint}
