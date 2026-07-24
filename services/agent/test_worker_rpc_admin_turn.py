"""test_worker_rpc_admin_turn.py — services.agent.worker_rpc._agent_admin_turn (harness overhaul).

Proves the ``agent_admin_turn`` worker RPC (the seam core/agent/run.cpp's admin
pre-flight branch calls out to instead of refusing a project/file/PLM
administration request):

  * empty ``request`` is rejected without touching a provider at all.
  * offline mode (``config.get_provider`` raising ``OfflineModeError``) surfaces
    as a clean ``ok: False`` envelope — never an exception — so the C++ caller's
    "any failure -> fall back to the blocked response" contract holds.
  * a reachable provider runs ONE ``project_organizer`` turn via
    ``mission_dispatch.run_linear_role_turn`` and streams events through the
    ``_emit`` callback (worker_main.py's per-request event sink).

Scripted provider only (T-mega24-style), no model/network/port.
Run: pytest services/agent/test_worker_rpc_admin_turn.py -q
"""
from __future__ import annotations

from services.agent import config as agent_config
from services.agent import worker_rpc


def test_agent_admin_turn_rejects_empty_request(monkeypatch):
    called = {"get_provider": False}
    monkeypatch.setattr(
        agent_config, "get_provider",
        lambda *a, **k: called.__setitem__("get_provider", True) or (_ for _ in ()).throw(
            AssertionError("get_provider should not be called for an empty request")),
    )
    out = worker_rpc._agent_admin_turn({"request": "   "}, lambda data: None)
    assert out == {"ok": False, "reason": "agent_admin_turn: empty request"}
    assert called["get_provider"] is False


def test_agent_admin_turn_offline_falls_back_cleanly(monkeypatch):
    def _raise_offline(*_a, **_k):
        raise agent_config.OfflineModeError(
            "offline mode is enabled: the resolved provider is not local-only")

    monkeypatch.setattr(agent_config, "get_provider", _raise_offline)

    events: list[dict] = []
    out = worker_rpc._agent_admin_turn(
        {"request": "clean up my project files and archive deprecated parts"},
        events.append,
    )
    assert out["ok"] is False
    assert "offline" in out["reason"].lower()
    assert events == []  # never got far enough to run a turn


def test_agent_admin_turn_no_provider_falls_back_cleanly(monkeypatch):
    """Worker-unreachable-equivalent: any other provider-construction failure also
    degrades to a clean ok:false, never an exception across the RPC boundary."""
    def _raise(*_a, **_k):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(agent_config, "get_provider", _raise)

    out = worker_rpc._agent_admin_turn({"request": "organize my files"}, lambda data: None)
    assert out["ok"] is False
    assert "no provider" in out["reason"]


class _OneToolProvider:
    """Emits ONE tool-call reply then a plain-text finish — enough to exercise
    the tool_call event-emission path without a full multi-turn scripted flow."""
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        return self._replies.pop(0) if self._replies else "done, nothing else to do."


def test_agent_admin_turn_runs_project_organizer_turn_and_streams_events(monkeypatch):
    """A tool-calling turn (not just a bare no-code finish) exercises the SAME
    ``agent.tool_call`` event emission a real fs_edit/fs_list/run_shell step
    would — proving events flow through ``_emit`` for the C++ caller to relay,
    without needing the full MCP tool registry wired up in a unit test (the
    scripted tool need not actually resolve)."""
    provider = _OneToolProvider([
        '```json\n{"tool": "fs_list", "args": {"path": "."}}\n```',
        "Listed the project root; nothing needs reorganizing.",
    ])
    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: provider)

    events: list[dict] = []
    out = worker_rpc._agent_admin_turn(
        {"request": "clean up the folders and track it in PLM", "run_id": "r_admin_1"},
        events.append,
    )
    assert out["ok"] is True
    turn = out["turn"]
    assert turn["role"] == "project_organizer"
    assert "fs_edit" in turn["allowed_tools"] and "fs_list" in turn["allowed_tools"]
    assert provider.calls == 2
    # The scripted fs_list call streamed as an agent.tool_call event through _emit
    # (worker_main.py tags/relays these; run.cpp's admin branch relays them onward).
    assert events, "expected at least one streamed event for the tool call"
    assert all("kind" in e for e in events)
    assert any(e["kind"] == "agent.tool_call" and e.get("tool_name") == "fs_list"
               for e in events)
