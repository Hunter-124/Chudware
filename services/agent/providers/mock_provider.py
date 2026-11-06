"""Deterministic mock provider — drives the loop in tests with no API key."""
from __future__ import annotations

from .base import Provider


class MockProvider(Provider):
    name = "mock"

    def __init__(self, scripted_replies: list[str]):
        self._replies = scripted_replies
        self._i = 0

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        reply = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return reply
