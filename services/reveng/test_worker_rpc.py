"""Unit tests for the Reverse Engineering worker-RPC bridge (services.reveng.worker_rpc).

The bridge is what lets the GUI (RE tab + Parts Library "Reverse-engineer this
part" action) reach the reveng backend over the worker seam. These tests exercise
it directly (no MCP transport), with the session store redirected to a temp dir.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.reveng import worker_rpc as reveng_rpc


@pytest.fixture()
def reveng_dir(tmp_path, monkeypatch):
    d = tmp_path / "reveng"
    d.mkdir()
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(d))
    return d


def _emit(*_a, **_k):  # the bridge never emits; a no-op sink
    return None


def test_methods_registered_on_worker():
    """The bridge's methods land in the aggregated worker table (GUI reachability)."""
    from services import worker_main as w  # the post-oracle stdio worker (T-RF20)
    for name in ("list_reveng_sessions", "get_reveng_session", "reveng_start_session",
                 "reveng_add_images", "reveng_ingest_datasheet",
                 "reveng_start_from_product"):
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


def test_start_from_product_threads_media(reveng_dir, monkeypatch):
    """§2b hand-off: harvested images+documents are downloaded into the session
    and threaded through add_images / ingest_datasheet."""
    from services.parts import product_media as pm
    from services.browser import session as bsession

    monkeypatch.setattr(pm, "harvest_product_media", lambda ref: {
        "ok": True, "source_url": "https://shop.example/widget",
        "images": ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
        "documents": ["https://cdn.example/ds.pdf"],
    })

    def _download(url, site, dest, **_k):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4\n" if str(dest).endswith(".pdf")
                         else b"\xff\xd8\xff\xe0" + b"\x00" * 8)
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(bsession, "browser_download", _download)
    # datasheet ingest backend may be absent in CI — make it deterministic.
    monkeypatch.setattr("services.mcp.reveng_tools._ingest_pdf",
                        lambda path, out_dir: {"ok": True, "tables": [], "crops": []})

    r = reveng_rpc._reveng_start_from_product(
        {"url": "https://shop.example/widget", "title": "Widget", "mpn": "W-1"}, _emit)
    assert r["ok"] and r["session_id"]
    assert r["images_added"] == 2
    assert r["documents_ingested"] == 1
    assert r["source_url"] == "https://shop.example/widget"
    assert r["activeWorkspace"] == "ReverseEng"


def test_start_from_product_empty_harvest_still_starts_session(reveng_dir, monkeypatch):
    from services.parts import product_media as pm
    monkeypatch.setattr(pm, "harvest_product_media",
                        lambda ref: {"ok": False, "reason": "captcha_or_2fa_detected"})

    r = reveng_rpc._reveng_start_from_product(
        {"url": "https://shop.example/widget"}, _emit)
    assert r["ok"] and r["session_id"]
    assert r["images_added"] == 0 and r["documents_ingested"] == 0
    # session really exists on disk
    got = reveng_rpc._reveng_get_session({"session_id": r["session_id"]}, _emit)
    assert got["ok"]


def test_missing_session_is_structured_failure(reveng_dir):
    r = reveng_rpc._reveng_add_images({"session_id": "nope", "paths": []}, _emit)
    assert r["ok"] is False and r["reason"] == "no_such_session"

    g = reveng_rpc._reveng_get_session({"session_id": "nope"}, _emit)
    assert g["ok"] is False and g["reason"] == "no_such_session"
