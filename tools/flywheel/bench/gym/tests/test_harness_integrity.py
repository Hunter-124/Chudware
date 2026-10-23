"""Pin harness discovery, category freshness, skip accounting, and tool bindings."""
from __future__ import annotations

import io
import json

import pytest

from services.agent.agent_turn import _bind_tool
from tools.flywheel.bench import categories
from tools.flywheel.bench.gym import challenges


def _declared_tools() -> set[str]:
    names = {
        tool
        for spec in categories.CATEGORY_SPECS
        for tool in spec.tools_covered
    }
    for challenge in challenges.load_challenges():
        for deliverable in challenge.manifest.get("deliverables", []):
            for criterion in deliverable.get("acceptance_criteria", []):
                check = criterion.get("check") or {}
                if check.get("tool"):
                    names.add(str(check["tool"]))
        for scripts_by_deliverable in challenge.scripts.values():
            for turns in scripts_by_deliverable.values():
                names.update(
                    str(turn["tool"])
                    for turn in turns
                    if turn.get("tool")
                )
    return names


def test_every_declared_bench_and_challenge_tool_resolves():
    dead = sorted(name for name in _declared_tools() if _bind_tool(name) is None)
    assert dead == [], f"declared tools do not resolve through agent_turn._bind_tool: {dead}"


def test_roundtrip_ignores_seeded_old_pass_after_current_crash(tmp_path, monkeypatch):
    out_dir = tmp_path / "category"
    out_dir.mkdir()
    (out_dir / "summary.json").write_text(json.dumps({
        "category": "roundtrip",
        "pass_rate": 1.0,
        "exit_code": 0,
        "executed": 9,
        "stage": "done",
        "run_id": "old-run",
    }))
    monkeypatch.setattr(categories, "_run_cmd", lambda *args, **kwargs: 17)

    result = categories.run_roundtrip(tmp_path, out_dir, io.StringIO())

    assert result["exit_code"] == 17
    assert result["pass_rate"] is None
    assert result["executed"] == 0
    assert result["stage"] == "error"
    assert result["run_id"] != "old-run"
    assert json.loads((out_dir / "summary.json").read_text())["run_id"] == result["run_id"]


def test_run_category_returns_failure_not_seeded_roundtrip_success(tmp_path, monkeypatch):
    out_dir = categories.category_report_dir("roundtrip", tmp_path)
    out_dir.mkdir(parents=True)
    (out_dir / "summary.json").write_text(json.dumps({
        "category": "roundtrip",
        "pass_rate": 1.0,
        "exit_code": 0,
        "executed": 9,
        "stage": "done",
        "run_id": "old-run",
    }))
    monkeypatch.setattr(categories, "_run_cmd", lambda *args, **kwargs: 23)

    assert categories.run_category("roundtrip", repo=tmp_path) == 23
    status = json.loads(categories.category_status_path("roundtrip", tmp_path).read_text())
    assert status["exit_code"] == 23
    assert status["pass_rate"] is None
    assert status["executed"] == 0
    assert status["stage"] == "error"


def test_all_skipped_verdict_and_pytest_bundles_are_not_passes(tmp_path, monkeypatch):
    assert categories._parse_verdict_pass_rate({"counts": {"skip": 7}}) is None
    assert categories._verdict_stats({"counts": {"skip": 7}}) == (None, 0)

    monkeypatch.setattr(
        categories,
        "_run_cmd_capture",
        lambda *args, **kwargs: (0, "7 skipped in 0.01s\n"),
    )
    summary = categories._run_pytest_bundle(
        tmp_path,
        tmp_path / "pytest-category",
        io.StringIO(),
        category="fixture",
        targets=["fixture.py"],
    )
    assert summary["pass_rate"] is None
    assert summary["executed"] == 0
    assert summary["stage"] == "skipped"
    assert summary["exit_code"] == 0


def test_missing_category_targets_report_skipped_stage(tmp_path):
    summary = categories.run_reveng(tmp_path, tmp_path / "reveng", io.StringIO())
    assert summary["pass_rate"] is None
    assert summary["executed"] == 0
    assert summary["stage"] == "skipped"


def test_challenge_discovery_names_import_failure(monkeypatch):
    real_import = challenges.importlib.import_module

    def broken_import(name: str):
        if name.endswith(".c13_sketch_repair"):
            raise SyntaxError("planted syntax failure")
        return real_import(name)

    monkeypatch.setattr(challenges.importlib, "import_module", broken_import)
    with pytest.raises(ImportError, match=r"c13_sketch_repair.*SyntaxError"):
        challenges.load_challenges()
