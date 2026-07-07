"""Unit tests for the Reverse Engineering worker-RPC bridge (engine_oracle.reveng_rpc).

The bridge is what lets the GUI (RE tab + Parts Library "Reverse-engineer this
part" action) reach the reveng backend over the worker seam. These tests exercise
it directly (no MCP transport), with the session store redirected to a temp dir.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from engine_oracle import reveng_rpc


@pytest.fixture()
def reveng_dir(tmp_path, monkeypatch):
    d = tmp_path / "reveng"
    d.mkdir()
    monkeypatch.setenv("AICAD_REVENG_DIR", str(d))
    return d


def _emit(*_a, **_k):  # the bridge never emits; a no-op sink
    return None


def test_methods_registered_on_worker():
    """The bridge's methods land in the aggregated worker table (GUI reachability)."""
    import engine_oracle.worker as w
    for name in ("list_reveng_sessions", "get_reveng_session", "reveng_start_session",
                 "reveng_add_images", "reveng_ingest_datasheet"):
        assert name in w._METHODS, f"{name} not registered on the worker"


def test_start_session_then_list_and_get(reveng_dir):
    r = reveng_rpc._reveng_start_session({"name": "bridge test"}, _emit)
    assert r["ok"] and r["session_id"]
    sid = r["session_id"]

    lst = reveng_rpc._reveng_list_sessions({}, _emit)
    assert lst["ok"]
    assert any(s["session_id"] == sid for s in lst["sessions"])

    got = reveng_rpc._reveng_get_session({"session_id": sid}, _emit)
    assert got["ok"] and got["session"]["session_id"] == sid


def test_start_session_with_research_seed_populates_lane(reveng_dir):
    # A part number seed routes through the research/datasheet lane (RE-29).
    r = reveng_rpc._reveng_start_session({"research": "SKF 6205"}, _emit)
    assert r["ok"]
    assert "research_result" in r  # the datasheet/web lane was invoked


def test_add_images_delivers_photos(reveng_dir, tmp_path):
    sid = reveng_rpc._reveng_start_session({"name": "photos"}, _emit)["session_id"]
    photo = tmp_path / "front.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)  # minimal JFIF

    r = reveng_rpc._reveng_add_images({"session_id": sid, "paths": [str(photo)]}, _emit)
    assert r["ok"] and r["added"] == 1
    assert r["session"]["artifacts"]["images"] == 1
    assert r["session"]["stages"]["capture"] == "done"


def test_missing_session_is_structured_failure(reveng_dir):
    r = reveng_rpc._reveng_add_images({"session_id": "nope", "paths": []}, _emit)
    assert r["ok"] is False and r["reason"] == "no_such_session"

    g = reveng_rpc._reveng_get_session({"session_id": "nope"}, _emit)
    assert g["ok"] is False and g["reason"] == "no_such_session"
