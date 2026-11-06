"""Explicit presentation and legacy handoff behavior."""
from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp import FastMCP

from services.mcp import cad_common, cad_doc_tools


@pytest.fixture
def presentation(monkeypatch, tmp_path):
    mcp = FastMCP("show-in-app-test")
    cad_doc_tools.register_cad_doc_tools(mcp)
    show = mcp._tool_manager._tools["show_in_app"].fn
    docs = {
        "doc-cpp00000002": {"doc_id": "doc-cpp00000002", "name": "surface",
                            "revision": 7, "geometry": True, "path": str(tmp_path / "source.step+")},
        "doc-cpp00000003": {"doc_id": "doc-cpp00000003", "name": "empty",
                            "revision": 0, "geometry": False, "path": ""},
    }
    calls = []
    mode = {"daemon": False}
    monkeypatch.setattr(cad_common, "_PUB", {"rev": None, "docs": {}, "active": None})
    monkeypatch.setattr(cad_common, "_PUB_THROTTLE", {})
    monkeypatch.setattr(cad_common, "_PUBLISH_MIN_INTERVAL_S", 0)
    monkeypatch.setenv("CHUDWARE_HANDOFF_DIR", str(tmp_path / "handoff"))
    monkeypatch.setattr(cad_common.cb, "active_daemon_mode", lambda: mode["daemon"])
    monkeypatch.setattr(cad_common.cb, "consume_foreign_edits", lambda: [])
    monkeypatch.setitem(cad_common._CUR, "doc_id", None)

    def call(method, args=None):
        args = args or {}
        calls.append((method, dict(args)))
        if method == "doc_list":
            return {"docs": list(docs.values())}
        doc = docs[args["doc_id"]]
        if method == "doc_present":
            return {"ok": True, "doc_id": doc["doc_id"], "name": doc["name"],
                    "revision": doc["revision"], "presented": True}
        if method == "doc_bbox":
            if not doc["geometry"]:
                raise RuntimeError("no bodies")
            return {"size": [1, 1, 1]}
        if method == "doc_save":
            from pathlib import Path
            Path(args["path"]).write_text(str(doc["revision"]))
            assert args["preserve_path"] is True
            return {"ok": True}
        raise AssertionError(method)

    monkeypatch.setattr(cad_common.cb, "call", call)
    return lambda **kw: json.loads(show(**kw)), docs, calls, mode, tmp_path


def test_daemon_requests_presentation_without_handoff(presentation):
    show, docs, calls, mode, tmp = presentation
    mode["daemon"] = True
    doc = "doc-cpp00000002"
    for _ in range(2):
        assert show(doc_id=doc) == {"ok": True, "doc_id": doc, "name": "surface",
                                    "revision": 7, "presented": True}
    assert [method for method, _ in calls].count("doc_present") == 2
    assert not (tmp / "handoff").exists()
    assert not show(doc_id="doc-cppffffffff")["ok"]


def test_legacy_only_publishes_explicit_documents_and_keeps_them_live(presentation):
    show, docs, calls, _, tmp = presentation
    doc, empty = "doc-cpp00000002", "doc-cpp00000003"
    source = tmp / "source.step+"
    source.write_text("user file")
    assert cad_common._publish_current(doc) is None
    assert not (tmp / "handoff").exists()
    assert not show(doc_id=empty)["ok"]
    assert cad_common._publish_current(empty) is None
    docs[empty]["geometry"] = True
    docs[empty]["revision"] = 1
    assert cad_common._publish_current(empty) is None
    assert empty not in cad_common._PUB["docs"]
    assert show(doc_id=doc)["presentation_requested"] is True
    manifest = json.loads((tmp / "handoff" / "docs.json").read_text())
    assert len(manifest["docs"]) == 1
    assert manifest["docs"][0]["doc_id"].endswith(doc)
    mirror = tmp / "handoff" / ("live-" + cad_common._SESSION_ID + "-" + doc + ".step+")
    assert mirror.read_text() == "7"
    docs[doc]["revision"] = 8
    assert manifest["docs"][0]["presented"] is True
    assert cad_common._publish_current(doc)["presentation_requested"] is True
    assert mirror.read_text() == "8"
    assert show(doc_id=doc)["ok"] is True
    assert source.read_text() == "user file"
    assert docs[doc]["path"] == str(source)
    assert all(args.get("preserve_path") for method, args in calls if method == "doc_save")


def test_failed_initial_handoff_does_not_opt_in(presentation, monkeypatch):
    show, docs, _, _, _ = presentation
    doc = "doc-cpp00000002"
    monkeypatch.setattr(cad_common, "_merge_handoff_fragments", lambda _path: False)
    assert show(doc_id=doc)["ok"] is False
    assert doc not in cad_common._PUB["docs"]
    docs[doc]["revision"] += 1
    assert cad_common._publish_current(doc) is None
