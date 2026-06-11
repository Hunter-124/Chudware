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


class RoleScriptedProvider(Provider):
    """A mock that returns replies keyed by ORCHESTRATOR ROLE, in per-role order.

    The orchestrator never names the role to the provider, so we sniff it from the system prompt's
    opening line (each role's prompt is distinctive). Used by config "mock" when the scripted-reply
    file is a ``{role: [reply, ...]}`` map — drives a deterministic agent_run_v2 in the worker test.
    """

    name = "mock-roles"
    supports_vision = False  # stats-only review keeps the worker test free of a render dependency

    def __init__(self, by_role: dict):
        self._by_role = {k: list(v) for k, v in by_role.items()}

    @staticmethod
    def role_of(system: str) -> str:
        s = (system or "").lower()
        # Explicit marker first (run.py prepends "ROLE: <role>" — deterministic routing;
        # the prose sniffs below remain as fallback for hand-built contexts).
        if s.startswith("role:"):
            return s.split("\n", 1)[0].split(":", 1)[1].strip()
        if s.startswith("you are the intake"):
            return "intake"
        if "planning a cad build" in s:
            return "planner"
        if s.startswith("you are the modeler"):
            return "modeler"
        if s.startswith("you are the reviewer"):
            return "reviewer"
        if s.startswith("you are the fixer"):
            return "fixer"
        if s.startswith("you are the memory"):
            return "memory"
        return "?"

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        role = self.role_of(system)
        queue = self._by_role.get(role)
        if not queue:
            # Loud sentinel: a missing role is a test-authoring bug, not a model reply.
            return f"MOCK-MISSING-ROLE:{role}"
        return queue.pop(0) if len(queue) > 1 else queue[0]
