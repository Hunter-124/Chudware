"""services.plm.test_watch — keywords-of-interest watch overlay."""
from __future__ import annotations

import json
from pathlib import Path

from services.plm import watch


def test_add_list_remove_roundtrip(tmp_path: Path):
    r = watch.watch_add(tmp_path, keyword="Thermal", note="hot parts", origin="ai")
    assert r["ok"] and r["entry"]["id"].startswith("w")
    assert r["entry"]["keyword"] == "thermal"  # normalized strip/lower
    assert "node_id" not in r["entry"]
    assert r["entry"]["note"] == "hot parts" and r["entry"]["origin"] == "ai"

    entries = watch.list_watch(tmp_path)
    assert len(entries) == 1

    rm = watch.watch_remove(tmp_path, r["entry"]["id"])
    assert rm["ok"] and rm["removed"] == r["entry"]["id"]
    assert watch.list_watch(tmp_path) == []


def test_node_watch_entry_shape(tmp_path: Path):
    r = watch.watch_add(tmp_path, node_id="n1")
    assert r["ok"] and r["entry"]["node_id"] == "n1"
    assert "keyword" not in r["entry"]
    assert r["entry"]["note"] == "" and r["entry"]["origin"] == "manual"


def test_missing_file_is_empty(tmp_path: Path):
    assert watch.list_watch(tmp_path) == []


def test_overlay_roundtrip_on_disk(tmp_path: Path):
    watch.watch_add(tmp_path, keyword="rf")
    data = json.loads((tmp_path / ".plm" / "graph_watch.json").read_text(encoding="utf-8"))
    assert data["schema"] == watch.SCHEMA
    assert [e["keyword"] for e in data["watch"]] == ["rf"]


def test_invalid_watch_rejected(tmp_path: Path):
    r = watch.watch_add(tmp_path)
    assert r["ok"] is False and r["reason"] == "invalid_watch"
    r = watch.watch_add(tmp_path, keyword="rf", node_id="n1")
    assert r["ok"] is False and r["reason"] == "invalid_watch"
    assert watch.list_watch(tmp_path) == []


def test_dedupe_returns_existing(tmp_path: Path):
    r1 = watch.watch_add(tmp_path, keyword="RF")
    r2 = watch.watch_add(tmp_path, keyword="rf")
    assert r2["ok"] and r2["deduped"] is True
    assert r2["entry"]["id"] == r1["entry"]["id"]
    n1 = watch.watch_add(tmp_path, node_id="n1")
    n2 = watch.watch_add(tmp_path, node_id="n1")
    assert n2["deduped"] is True and n2["entry"]["id"] == n1["entry"]["id"]
    assert len(watch.list_watch(tmp_path)) == 2


def test_remove_unknown(tmp_path: Path):
    r = watch.watch_remove(tmp_path, "wdeadbeef")
    assert r["ok"] is False and r["reason"] == "not_found"


def test_watched_matches():
    graph = {"nodes": [
        {"id": "n1"},
        {"id": "kw:thermal"},
        {"id": "n3", "meta": {"keywords": ["thermal", "rf"]}},
        {"id": "n4", "meta": {}},
        {"id": "n5", "meta": None},
        {"id": "n6", "meta": {"keywords": ["other"]}},
    ]}
    entries = [{"keyword": "thermal"}, {"node_id": "n1"}]
    assert watch.watched_matches(graph, entries) == {"n1", "kw:thermal", "n3"}
    assert watch.watched_matches(graph, []) == set()
    assert watch.watched_matches({}, entries) == set()
