"""test_linear_turn_fs_dispatch.py — advertised ⇒ dispatchable (harness overhaul).

Regression for the live failure where the project_organizer turn advertised
fs_list/fs_glob/run_shell but every call FAILED: reviewer_exec._resolve_fn (the
process-wide dispatch resolver agent_turn._dispatch_tool uses) had no branch
for the files/shell/plm MCP modules. Also pins the meta-tool coverage:
ask_user on the organizer card resolves through the run-scoped MissionDispatch.

Discipline: scripted provider only (no model/network); real dispatch — NO
tool_dispatch override, so the calls go through the live registry resolver.
Run: pytest services/agent/test_linear_turn_fs_dispatch.py -q
"""
from __future__ import annotations

import json
import os

from services.agent.copilot_mode_tools import (
    AGENT_DEFAULT_TOOLS,
    SUBAGENT_DEFAULT_TOOLS,
)
from services.agent.mission_dispatch import MissionDispatch, run_linear_role_turn
from services.agent.reviewer_exec import _resolve_fn
from services.agent.roles import load_role


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, system, messages, max_tokens=8000):
        return self._replies.pop(0) if self._replies else "no more"


# Tools the turn/mission layer handles instead of the process-wide resolver:
# enable_tools/list_tool_categories are intercepted inside run_turn; ask_user
# (and the other coordination tools) resolve via MissionDispatch.resolve.
_TURN_OR_MISSION_HANDLED = {"ask_user", "advise", "enable_tools", "list_tool_categories"}


def test_every_advertised_organizer_tool_is_dispatchable():
    card = load_role("project_organizer")
    advertised = set(card.concrete_tools) | set(AGENT_DEFAULT_TOOLS) | set(SUBAGENT_DEFAULT_TOOLS)
    unresolved = sorted(
        t for t in advertised - _TURN_OR_MISSION_HANDLED if _resolve_fn(t) is None
    )
    assert not unresolved, (
        "advertised tools that the dispatch resolver cannot serve (the exact "
        f"live 'fs_list failed' bug): {unresolved}"
    )
    # Meta-tool coverage: ask_user must be served by the run-scoped dispatcher.
    assert "ask_user" in MissionDispatch.TOOLS


def test_linear_turn_executes_real_fs_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    (tmp_path / "old_part.step").write_text("solid x\n")
    out = tmp_path / "manifest.md"
    provider = _ScriptedProvider([
        '```json\n{"tool": "fs_list", "args": {"path": "%s"}}\n```' % tmp_path,
        '```json\n{"tool": "fs_write", "args": {"path": "%s", "content": "# inventory\\n"}}\n```'
        % out,
        '```json\n{"done": true, "handoff": {"status": "done", '
        '"reasoning_summary": "listed and wrote manifest"}}\n```',
    ])
    res = run_linear_role_turn(
        "clean up my project files", "project_organizer", provider)
    assert res["success"] is True, json.dumps(res["result"])[:800]
    calls = res["result"]["calls"]
    assert [c["tool"] for c in calls[:2]] == ["fs_list", "fs_write"]
    assert calls[0]["ok"] is True and not calls[0].get("error")
    assert calls[1]["ok"] is True and not calls[1].get("error")
    # The write REALLY happened on disk — dispatch reached the live fs tool.
    assert out.read_text() == "# inventory\n"
    assert os.path.exists(tmp_path / "old_part.step")
