"""Regression: the context meter must measure OCCUPANCY, not billed input.

Live 2026-07-29 (case bench, kimi-code/k3, run-30513252767546): the desktop context
meter read "1k/1.0M" on a conversation holding well over 10k tokens. Two separate
defects, both in the numbers fed to the GUI:

1. ``agent.context``'s ``used`` came from ``prompt_tokens``, which for a
   prompt-cache-aware provider is the NON-CACHED input only. Across one turn the
   four completions reported 8121 -> 719 -> 197 -> 1229: that drop is the cache
   engaging, not the context shrinking. Occupancy is
   ``input + cacheRead + cacheWrite`` (pi-ai's own convention — see
   vendor/omp/packages/ai CHANGELOG 0.12.10 and its error/flags.ts).
2. ``window`` came from ``model_catalog``, which has no kimi-code rows and so
   returned the LOCAL default: context 8192 for a model whose real window is
   1,048,576. The meter was measuring a 1M window against an 8192 ruler.
"""
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
    row = tu.record_usage(provider="kimi-code", model="k3",
                          prompt_tokens=311, completion_tokens=38,
                          context_tokens=12087)
    assert row["prompt_tokens"] == 311      # billed, non-cached — cost math
    assert row["context_tokens"] == 12087   # occupancy — the meter
    assert tu.get_last_usage()["context_tokens"] == 12087


def test_context_tokens_defaults_to_prompt_tokens():
    """Providers that report no cache counts must not regress to 0."""
    row = tu.record_usage(provider="openai", model="gpt-4o", prompt_tokens=4321)
    assert row["context_tokens"] == 4321


def test_context_tokens_can_never_undercut_billed_input():
    """Occupancy CONTAINS the billed input, so it can never be smaller."""
    row = tu.record_usage(provider="x", model="y", prompt_tokens=900,
                          context_tokens=100)
    assert row["context_tokens"] == 900


def test_bridge_usage_sums_cache_into_occupancy():
    """The exact sidecar Usage shape, straight through the bridge provider."""
    from services.agent.providers.omp_bridge_provider import OmpBridgeProvider

    p = OmpBridgeProvider.__new__(OmpBridgeProvider)
    p.name = "kimi-code"     # type: ignore[attr-defined]
    p.model = "k3"           # type: ignore[attr-defined]
    p._record_usage({"input": 311, "output": 38, "cacheRead": 11700,
                     "cacheWrite": 76, "totalTokens": 12125})
    last = tu.get_last_usage()
    assert last["prompt_tokens"] == 311
    # 311 + 11700 + 76 — the number the meter must show, not 311.
    assert last["context_tokens"] == 12087


def test_missing_cache_fields_leave_occupancy_at_input():
    from services.agent.providers.omp_bridge_provider import OmpBridgeProvider

    p = OmpBridgeProvider.__new__(OmpBridgeProvider)
    p.name = "kimi-code"     # type: ignore[attr-defined]
    p.model = "k3"           # type: ignore[attr-defined]
    p._record_usage({"input": 5000, "output": 12, "totalTokens": 5012})
    assert tu.get_last_usage()["context_tokens"] == 5000


# ---------------------------------------------------------------------------
# the event the GUI consumes
# ---------------------------------------------------------------------------

def test_context_event_reports_occupancy_not_billed_input():
    """`agent.context` must carry the meter's number, whatever the cache did."""
    from services.agent import agent_turn as at

    class _P:
        name = "kimi-code"
        model = "k3"
        context_window = 1048576
        context_window_source = "catalog"

        def __init__(self):
            self.i = 0

        def complete(self, system, messages, max_tokens=8000):
            self.i += 1
            tu.record_usage(provider="kimi-code", model="k3",
                            prompt_tokens=200 * self.i,
                            completion_tokens=10,
                            context_tokens=9000 + 500 * self.i)
            if self.i == 1:
                return '```json\n{"tool": "new_part", "args": {"name": "p"}}\n```'
            return ('{"done": true, "handoff": {"status": "done", '
                    '"summary": "made p", "outputs": {"doc_id": "d1"}}}')

    seen: list[dict] = []

    def sink(kind, payload):
        if kind == "agent.context":
            seen.append(dict(payload))

    at.run_turn("make a part", _P(), tool_dispatch=lambda t, a: {"ok": True},
                tools_text="new_part", on_event=sink, max_tool_calls=6)

    assert seen, "a measured completion must emit agent.context"
    used = [e["used"] for e in seen]
    # Occupancy, never the billed 200/400/... series.
    assert all(u >= 9000 for u in used), used
    # Occupancy only grows while a conversation grows.
    assert used == sorted(used), used
    assert seen[-1]["window"] == 1048576
    assert seen[-1]["window_source"] == "catalog"


# ---------------------------------------------------------------------------
# the window itself
# ---------------------------------------------------------------------------

def test_sidecar_catalog_beats_a_heuristic_guess():
    """A guess is the one thing worth replacing — and only a guess."""
    from services.agent import config as ac
    from services.agent.providers import omp_bridge_provider as obp

    real = {"context": 1048576, "max_output": 131072, "vision": True, "tools": True}
    p = obp.OmpBridgeProvider.__new__(obp.OmpBridgeProvider)
    p.name = "kimi-code"     # type: ignore[attr-defined]
    p.model = "k3"           # type: ignore[attr-defined]

    obp._CATALOG_CAPS[("kimi-code", "k3")] = real
    try:
        # model_catalog's local default, admitted as a heuristic -> replaced.
        ac._attach_catalog_meta(p, {"context": 8192, "max_output": 0,
                                    "vision": False, "reasoning_efforts": [],
                                    "auto_source": "heuristic"})
        assert p.context_window == 1048576
        assert p.context_window_source == "catalog"
        assert p.max_output_tokens == 131072
        assert p.vision is True

        # A USER override must stay an override, even against a real catalog row.
        ac._attach_catalog_meta(p, {"context": 64000, "max_output": 4096,
                                    "vision": False, "reasoning_efforts": [],
                                    "auto_source": "user"})
        assert p.context_window == 64000
        assert p.context_window_source == "user"
    finally:
        obp._CATALOG_CAPS.pop(("kimi-code", "k3"), None)


def test_unknown_model_keeps_whatever_was_resolved():
    """No sidecar row = keep the existing number; never fabricate a capability."""
    from services.agent import config as ac
    from services.agent.providers import omp_bridge_provider as obp

    p = obp.OmpBridgeProvider.__new__(obp.OmpBridgeProvider)
    p.name = "kimi-code"     # type: ignore[attr-defined]
    p.model = "made-up"      # type: ignore[attr-defined]
    obp._CATALOG_CAPS[("kimi-code", "made-up")] = None
    try:
        ac._attach_catalog_meta(p, {"context": 8192, "max_output": 0,
                                    "vision": False, "reasoning_efforts": [],
                                    "auto_source": "heuristic"})
        assert p.context_window == 8192
        assert p.context_window_source == "heuristic"
    finally:
        obp._CATALOG_CAPS.pop(("kimi-code", "made-up"), None)
