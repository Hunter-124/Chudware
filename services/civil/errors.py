"""Shared refusal type for the civil package (contracts/civil_schema.md §2).

Every civil module raises :class:`CivilError` for invalid input or an unsupported case;
the MCP layer turns it into the house ``reason``-keyed refusal
(``services.mcp._envelopes.reason_err``). Reasons are stable snake_case tokens.
"""

from __future__ import annotations

from typing import Any, Optional


class CivilError(ValueError):
    """A named, actionable refusal: ``reason`` token + optional ``hint`` + extra fields."""

    def __init__(self, reason: str, hint: Optional[str] = None, **extra: Any) -> None:
        super().__init__(f"{reason}: {hint}" if hint else reason)
        self.reason = reason
        self.hint = hint
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "reason": self.reason}
        if self.hint:
            out["hint"] = self.hint
        out.update(self.extra)
        return out
