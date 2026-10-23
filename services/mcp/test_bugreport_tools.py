"""test_bugreport_tools.py — submit_bug_report (Settings → AI → Tools).

Offline discipline: no live model, no real config.json — config comes from a
monkeypatched ``services.paths.CONFIG`` and reports land in tmp_path via
``CHUDWARE_BUG_REPORTS_DIR``.
"""
from __future__ import annotations

import json

import pytest

from services import paths
from services.agent import bugreport_context as brc
from services.mcp import tool_registry as reg
from services.mcp.bugreport_tools import register_bugreport_tools


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated config + report dir; yields (config_path, report_dir)."""
    brc.clear()
    report_dir = tmp_path / "Bug-Reports"
    monkeypatch.setenv("CHUDWARE_BUG_REPORTS_DIR", str(report_dir))
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(paths, "CONFIG", cfg)
    yield cfg, report_dir
    brc.clear()


def _write_cfg(cfg_path, enabled):
    cfg_path.write_text(json.dumps({"agent": {"bug_reports": enabled}}),
                        encoding="utf-8")


def _tool():
    m = _FakeMcp()
    register_bugreport_tools(reg.view(m, "workspace"))
    return m.tools["submit_bug_report"]


def test_risk_is_write_not_external():
    """The submit_ prefix auto-classifies EXTERNAL (network egress) — the
    report is a LOCAL file, so the registry override must hold, else
    offline/local-only policies refuse the tool."""
    assert reg.classify_risk("submit_bug_report") == reg.RISK_WRITE


def test_files_report_with_context_when_enabled(env):
    cfg, report_dir = env
    _write_cfg(cfg, True)
    brc.record_failure(
        "add_features", {"ops": [{"op": "add_feature", "feature": {"type": "sketch"}}]},
        {"ok": False, "error": "features[0]{f1}.params.entities[0].type: "
                               "not a sketch entity type"},
        run_id="r_test", actor="general_agent")

    out = _tool()(title="add_features rejects sketch entities",
                  summary="First-call validation failure on a documented shape.")
    assert out["ok"] is True, out
    path = report_dir / (out["path"].rsplit("/", 1)[-1])
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "# Bug report: add_features rejects sketch entities" in text
    assert "First-call validation failure" in text
    # auto-captured failure context embedded
    assert "add_features" in text
    assert "not a sketch entity type" in text
    assert "r_test" in text
    assert out["failures_included"] == 1


def test_refuses_when_disabled_and_writes_nothing(env):
    cfg, report_dir = env
    _write_cfg(cfg, False)
    out = _tool()(title="x", summary="y")
    assert out["ok"] is False and out["reason"] == "disabled"
    assert not report_dir.exists()


def test_default_is_enabled_without_config(env):
    _cfg, report_dir = env  # no config.json written at all
    out = _tool()(title="default on", summary="no config file present")
    assert out["ok"] is True, out
    assert list(report_dir.glob("*.md"))


def test_duplicate_title_dedupes_within_session(env):
    cfg, _report_dir = env
    _write_cfg(cfg, True)
    tool = _tool()
    first = tool(title="same bug", summary="first")
    second = tool(title="same bug", summary="again")
    assert first["ok"] is True and second["ok"] is True
    assert second.get("duplicate") is True
    assert second["path"] == first["path"]


def test_missing_title_or_summary_is_a_teaching_error(env):
    cfg, report_dir = env
    _write_cfg(cfg, True)
    tool = _tool()
    out = tool(title="", summary="y")
    assert out["ok"] is False and out["reason"] == "missing_title"
    out = tool(title="x", summary="  ")
    assert out["ok"] is False and out["reason"] == "missing_summary"
    assert not report_dir.exists()


def test_turn_failure_lands_in_the_report_context(tmp_path, monkeypatch):
    """A failing tool call inside run_turn is captured into the ring
    submit_bug_report embeds — this is the wiring that makes reports useful
    without the model pasting payloads."""
    from services.agent import agent_turn as at

    brc.clear()
    monkeypatch.setenv("CHUDWARE_BUG_REPORTS_DIR", str(tmp_path / "Bug-Reports"))
    monkeypatch.setattr(paths, "CONFIG", tmp_path / "config.json")

    class _ScriptedProvider:
        name = "scripted"

        def __init__(self, replies):
            self._replies = list(replies)

        def complete(self, system, messages, max_tokens=8000):
            return self._replies.pop(0) if self._replies else "no more"

    def _dispatch(tool, args):
        return {"ok": False, "error": "boom: document failed validation"}

    p = _ScriptedProvider([
        '```json\n{"tool":"flaky_tool","args":{"a":1}}\n```',
        '```json\n{"status":"done","summary":"gave up","outputs":{}}\n```',
    ])
    at.run_turn(
        "do the thing",
        p,
        allowed_tools=["flaky_tool"],
        max_tool_calls=4,
        tool_dispatch=_dispatch,
        actor="general_agent",
        run_id="r_brc",
    )
    failures = [f for f in brc.recent_failures() if f["tool"] == "flaky_tool"]
    assert failures, "failed call was not captured for the bug-report context"
    assert "boom" in failures[0]["error"]
    assert failures[0].get("run_id") == "r_brc"
    assert failures[0].get("actor") == "general_agent"
    brc.clear()


@pytest.mark.parametrize("lane", ["offline", "chudcode"])
def test_linear_turn_advertises_the_tool_only_when_enabled(tmp_path, monkeypatch, lane):
    """Headless role turns retain mutation tools and honor the report toggle,
    regardless of the interactive approval-first default."""
    from services.agent import agent_turn as at
    from services.agent import mission_dispatch as md

    cfg = tmp_path / "config.json"
    monkeypatch.setattr(paths, "CONFIG", cfg)
    captured = {}

    class _Res:
        success = True
        stop_reason = "confirmed"
        result = {"calls": []}

    def _fake_run_turn(*a, **k):
        captured.update(k)
        return _Res()

    monkeypatch.setattr(at, "run_turn", _fake_run_turn)

    def advertised_tools():
        if lane == "offline":
            md.run_linear_role_turn("build me a bracket", "general_agent", provider=object())
            return captured["allowed_tools"]
        from services.agent.chudcode_roles import _assemble_role_runtime
        from services.agent.roles import load_role

        _, allowed, _, _, _ = _assemble_role_runtime(
            load_role("general_agent"), "general_agent", "bug-report-turn", "",
            {}, None, None, None,
        )
        return allowed

    _write_cfg(cfg, True)
    allowed = advertised_tools()
    assert "submit_bug_report" in allowed
    assert "new_part" in allowed

    _write_cfg(cfg, False)
    allowed = advertised_tools()
    assert "submit_bug_report" not in allowed
    assert "new_part" in allowed
