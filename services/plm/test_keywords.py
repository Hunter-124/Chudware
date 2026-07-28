"""services.plm.test_keywords — keyword tags on CAD graph targets."""
from __future__ import annotations

import json
from pathlib import Path

from services.plm import keywords


def test_add_list_remove_roundtrip(tmp_path: Path):
    r = keywords.add_keyword(tmp_path, "part.step", "Thermal",
                             origin="ai", author={"name": "copilot"})
    assert r["ok"] and r["entry"]["id"].startswith("k")
    entries = keywords.list_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["target"] == "part.step"
    assert entries[0]["keyword"] == "thermal"  # normalized strip/lower
    assert entries[0]["origin"] == "ai"
    assert entries[0]["author"] == {"name": "copilot"}
    assert entries[0]["created_at"].endswith("Z")

    rm = keywords.remove_keyword(tmp_path, r["entry"]["id"])
    assert rm["ok"] and rm["removed"] == r["entry"]["id"]
    assert keywords.list_entries(tmp_path) == []


def test_missing_file_is_empty(tmp_path: Path):
    assert keywords.list_entries(tmp_path) == []


def test_overlay_roundtrip_on_disk(tmp_path: Path):
    keywords.add_keyword(tmp_path, "n1", "rf")
    data = json.loads((tmp_path / ".plm" / "keywords.json").read_text(encoding="utf-8"))
    assert data["schema"] == keywords.SCHEMA
    assert [e["keyword"] for e in data["keywords"]] == ["rf"]


def test_invalid_keyword_rejected(tmp_path: Path):
    r = keywords.add_keyword(tmp_path, "n1", "   ")
    assert r["ok"] is False and r["reason"] == "invalid_keyword"
    r = keywords.add_keyword(tmp_path, "n1", "x" * 65)
    assert r["ok"] is False and r["reason"] == "invalid_keyword"
    assert keywords.list_entries(tmp_path) == []


def test_invalid_target_rejected(tmp_path: Path):
    r = keywords.add_keyword(tmp_path, "  ", "rf")
    assert r["ok"] is False and r["reason"] == "invalid_target"


def test_origin_coerced_to_manual(tmp_path: Path):
    r = keywords.add_keyword(tmp_path, "n1", "rf", origin="bogus")
    assert r["entry"]["origin"] == "manual"
    assert r["entry"]["author"] == {}


def test_dedupe_returns_existing(tmp_path: Path):
    r1 = keywords.add_keyword(tmp_path, "n1", "RF")
    r2 = keywords.add_keyword(tmp_path, "n1", "rf")
    assert r2["ok"] and r2["deduped"] is True
    assert r2["entry"]["id"] == r1["entry"]["id"]
    assert len(keywords.list_entries(tmp_path)) == 1


def test_remove_unknown(tmp_path: Path):
    r = keywords.remove_keyword(tmp_path, "kdeadbeef")
    assert r["ok"] is False and r["reason"] == "not_found"


def test_remove_pair(tmp_path: Path):
    keywords.add_keyword(tmp_path, "n1", "rf")
    keywords.add_keyword(tmp_path, "n1", "thermal")
    keywords.add_keyword(tmp_path, "n2", "rf")
    r = keywords.remove_pair(tmp_path, "n1", "RF")
    assert r["ok"] and r["removed"] == 1
    remaining = [(e["target"], e["keyword"]) for e in keywords.list_entries(tmp_path)]
    assert remaining == [("n1", "thermal"), ("n2", "rf")]


def test_remove_pair_not_found(tmp_path: Path):
    keywords.add_keyword(tmp_path, "n1", "rf")
    r = keywords.remove_pair(tmp_path, "n1", "nope")
    assert r["ok"] is False and r["reason"] == "not_found"
    assert len(keywords.list_entries(tmp_path)) == 1


def test_keywords_for(tmp_path: Path):
    keywords.add_keyword(tmp_path, "n1", "thermal")
    keywords.add_keyword(tmp_path, "n1", "rf")
    keywords.add_keyword(tmp_path, "n2", "rf")
    got = keywords.keywords_for(tmp_path, ["n1", "n2", "n3"])
    assert got == {"n1": ["rf", "thermal"], "n2": ["rf"], "n3": []}
