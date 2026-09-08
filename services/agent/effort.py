"""Task evidence passed to the authoritative Copilot preference resolver.

Effort is derived per turn, never persisted as a second settings surface.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnInputs:
    """Prompt and document measurements used to choose this turn's policy."""

    prompt: str
    feature_count: int = 0
    doc_count: int = 1
    context_tokens: int = 0
    context_window: int = 0

    def resolve(self, *, cfg: dict | None = None):
        from services.agent.preferences import resolve_turn_preferences

        return resolve_turn_preferences(
            self.prompt,
            cfg=cfg,
            feature_count=self.feature_count,
            doc_count=self.doc_count,
            context_tokens=self.context_tokens,
            context_window=self.context_window,
        )
