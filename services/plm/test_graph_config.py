"""services.plm.test_graph_config — per-project CAD graph configuration."""
from __future__ import annotations

import json
from pathlib import Path

from services.plm import graph_config


def test_defaults_when_missing(tmp_path: Path):
    cfg = graph_config.get_config(tmp_path)
    assert cfg == graph_config.DEFAULTS
    assert cfg["office_content"] is False
    assert cfg["linked_projects"] == []
    assert "project_key" not in cfg
    cfg["office_kinds"].append("nope")
    assert "nope" not in graph_config.DEFAULTS["office_kinds"]


def test_set_get_roundtrip(tmp_path: Path):
    r = graph_config.set_config(tmp_path, office_content=True, office_max_chars=8000,
                                office_kinds=["pdf", "csv"], project_key="brkt01")
    assert r["ok"] and r["config"]["office_content"] is True
    assert r["config"]["office_max_chars"] == 8000
    assert r["config"]["office_kinds"] == ["pdf", "csv"]
    assert r["config"]["project_key"] == "brkt01"
    cfg = graph_config.get_config(tmp_path)
    assert cfg["office_content"] is True and cfg["project_key"] == "brkt01"
    data = json.loads((tmp_path / ".plm" / "graph_config.json").read_text(encoding="utf-8"))
    assert data["schema"] == graph_config.SCHEMA


def test_set_merges_with_existing(tmp_path: Path):
    graph_config.set_config(tmp_path, office_content=True)
    r = graph_config.set_config(tmp_path, office_max_chars=500)
    assert r["ok"]
    assert r["config"]["office_content"] is True
    assert r["config"]["office_max_chars"] == 500


def test_unknown_field_rejected(tmp_path: Path):
    r = graph_config.set_config(tmp_path, nope=1)
    assert r["ok"] is False and r["reason"] == "invalid_field"
    assert graph_config.get_config(tmp_path) == graph_config.DEFAULTS


def test_invalid_values_rejected(tmp_path: Path):
    assert graph_config.set_config(tmp_path, office_content=1)["reason"] == "invalid_field"
    assert graph_config.set_config(tmp_path, office_max_chars=199)["reason"] == "invalid_field"
    assert graph_config.set_config(tmp_path, office_max_chars=200001)["reason"] == "invalid_field"
    assert graph_config.set_config(tmp_path, office_max_chars=True)["reason"] == "invalid_field"
    assert graph_config.set_config(tmp_path, office_kinds=["PDF"])["reason"] == "invalid_field"
    assert graph_config.set_config(tmp_path, office_kinds=["pdf", 1])["reason"] == "invalid_field"
    assert graph_config.set_config(tmp_path, office_kinds="pdf")["reason"] == "invalid_field"
    assert graph_config.set_config(tmp_path, project_key="x")["reason"] == "invalid_field"
    assert graph_config.set_config(tmp_path, project_key="has space")["reason"] == "invalid_field"
    assert graph_config.set_config(tmp_path, project_key="k" * 33)["reason"] == "invalid_field"


def test_linked_projects_set_config(tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    r = graph_config.set_config(tmp_path, linked_projects=["other"])
    assert r["ok"] and r["config"]["linked_projects"] == [str(other.resolve())]
    r = graph_config.set_config(tmp_path, linked_projects=["missing"])
    assert r["ok"] is False and r["reason"] == "invalid_project"


def test_add_remove_linked_project(tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    r = graph_config.add_linked_project(tmp_path, str(other))
    assert r["ok"] and r["config"]["linked_projects"] == [str(other.resolve())]
    # dedupe by resolved absolute path: no second entry
    r = graph_config.add_linked_project(tmp_path, "other")
    assert r["ok"] and r["config"]["linked_projects"] == [str(other.resolve())]
    r = graph_config.remove_linked_project(tmp_path, str(other))
    assert r["ok"] and r["config"]["linked_projects"] == []
    r = graph_config.remove_linked_project(tmp_path, str(other))
    assert r["ok"] is False and r["reason"] == "not_found"


def test_add_linked_project_must_exist(tmp_path: Path):
    r = graph_config.add_linked_project(tmp_path, "missing")
    assert r["ok"] is False and r["reason"] == "invalid_project"


def test_unknown_on_disk_keys_ignored(tmp_path: Path):
    (tmp_path / ".plm").mkdir()
    (tmp_path / ".plm" / "graph_config.json").write_text(
        json.dumps({"schema": graph_config.SCHEMA, "office_content": True,
                    "surprise": 42}), encoding="utf-8")
    cfg = graph_config.get_config(tmp_path)
    assert cfg["office_content"] is True
    assert "surprise" not in cfg


def test_config_mtime(tmp_path: Path):
    assert graph_config.config_mtime(tmp_path) == 0.0
    graph_config.set_config(tmp_path, office_content=True)
    assert graph_config.config_mtime(tmp_path) > 0.0


def test_project_key(tmp_path: Path):
    derived = graph_config.project_key(tmp_path)
    assert derived.startswith("p") and len(derived) == 9
    assert graph_config.project_key(tmp_path) == derived  # stable
    graph_config.set_config(tmp_path, project_key="brkt01")
    assert graph_config.project_key(tmp_path) == "brkt01"
