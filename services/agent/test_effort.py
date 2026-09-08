"""Observable policy decisions from prompt and document evidence."""
from __future__ import annotations

import os
import stat

import pytest

from services.agent.effort import TurnInputs


@pytest.fixture(autouse=True)
def no_local_classifier(monkeypatch):
    from services.agent import model_manager

    monkeypatch.setattr(model_manager, "classify_bundled_task", lambda *a, **kw: None)


def test_multiple_documents_enable_mission_review_but_respect_double_check():
    single = TurnInputs("build a bracket", feature_count=5).resolve(cfg={})
    mission = TurnInputs("build a bracket", feature_count=5, doc_count=2)
    reviewed = mission.resolve(cfg={})
    unchecked = mission.resolve(cfg={"copilot": {"double_check": False}})

    assert single.delegation_depth == 0
    assert single.council is False
    assert reviewed.delegation_depth == 2
    assert reviewed.council is True
    assert unchecked.council is False
    assert unchecked.verify_battery is True


def test_context_pressure_schedules_compaction_at_half_window():
    below = TurnInputs("build a bracket", context_tokens=499, context_window=1000)
    crowded = TurnInputs("build a bracket", context_tokens=500, context_window=1000)

    assert below.resolve(cfg={}).schedule_compaction is False
    policy = crowded.resolve(cfg={})
    assert policy.schedule_compaction is True
    assert policy.trim_tool_schemas is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_save_config_uses_owner_only_directory_and_file_modes(tmp_path, monkeypatch):
    from services import paths as engine_paths
    from services.agent.config import save_config

    config_dir = tmp_path / "nested"
    monkeypatch.setattr(engine_paths, "CONFIG", config_dir / "config.json")
    save_config({"keys": {"provider": "secret"}})
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((config_dir / "config.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((config_dir / "config.json.lock").stat().st_mode) == 0o600
