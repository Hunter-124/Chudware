"""Regression: cached-input billing must not undercount context occupancy."""

from __future__ import annotations

import pytest

from services.agent.providers import token_usage as tu


@pytest.fixture(autouse=True)
def _clean_usage():
    tu.begin_turn_usage()
    yield
    tu.begin_turn_usage()


# ---------------------------------------------------------------------------
# occupancy vs billing
# ---------------------------------------------------------------------------


def test_context_tokens_is_separate_from_billed_prompt_tokens():
    """Both numbers must survive: cost wants one, the meter wants the other."""
    row = tu.record_usage(
        provider="cached-fake",
        model="m",
        prompt_tokens=311,
        completion_tokens=38,
        context_tokens=12087,
    )
    assert row["prompt_tokens"] == 311  # billed, non-cached — cost math
    assert row["context_tokens"] == 12087  # occupancy — the meter
    assert tu.get_last_usage()["context_tokens"] == 12087


def test_context_tokens_defaults_to_prompt_tokens():
    """Providers that report no cache counts must not regress to 0."""
    row = tu.record_usage(provider="openai", model="gpt-4o", prompt_tokens=4321)
    assert row["context_tokens"] == 4321


def test_context_tokens_can_never_undercut_billed_input():
    """Occupancy CONTAINS the billed input, so it can never be smaller."""
    row = tu.record_usage(
        provider="x", model="y", prompt_tokens=900, context_tokens=100
    )
    assert row["context_tokens"] == 900


# ---------------------------------------------------------------------------
# the event the GUI consumes
# ---------------------------------------------------------------------------


def test_context_event_reports_occupancy_not_billed_input():
    """`agent.context` must carry the meter's number, whatever the cache did."""
    from services.agent import agent_turn as at

    class _P:
        name = "cached-fake"
        model = "m"
        context_window = 1048576
        context_window_source = "catalog"

        def __init__(self):
            self.i = 0

        def complete(self, system, messages, max_tokens=8000):
            self.i += 1
            tu.record_usage(
                provider="cached-fake",
                model="m",
                prompt_tokens=200 * self.i,
                completion_tokens=10,
                context_tokens=9000 + 500 * self.i,
            )
            if self.i == 1:
                return '```json\n{"tool": "new_part", "args": {"name": "p"}}\n```'
            return (
                '{"done": true, "handoff": {"status": "done", '
                '"summary": "made p", "outputs": {"doc_id": "d1"}}}'
            )

    seen: list[dict] = []

    def sink(kind, payload):
        if kind == "agent.context":
            seen.append(dict(payload))

    at.run_turn(
        "make a part",
        _P(),
        tool_dispatch=lambda t, a: {"ok": True},
        tools_text="new_part",
        on_event=sink,
        max_tool_calls=6,
    )

    assert seen, "a measured completion must emit agent.context"
    used = [e["used"] for e in seen]
    # Occupancy, never the billed 200/400/... series.
    assert all(u >= 9000 for u in used), used
    # Occupancy only grows while a conversation grows.
    assert used == sorted(used), used
    assert seen[-1]["window"] == 1048576
    assert seen[-1]["window_source"] == "catalog"
