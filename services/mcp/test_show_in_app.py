"""show_in_app reports what the desktop app can actually see, per engine transport.

c247: in daemon mode the GUI mirrors every hostd document live and ignores handoff
files, so _publish_current() returns None by design — and show_in_app turned that into
{"ok": false, "error": "could not publish (no geometry yet?)"} for a 26k-face part the user
was looking at. The legacy in-process path still publishes through the handoff folder.

Run: python3 -m pytest services/mcp/test_show_in_app.py -q
"""
from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp import FastMCP

from services.mcp import cad_common, cad_doc_tools


@pytest.fixture
def show_in_app(monkeypatch):
    mcp = FastMCP("show-in-app-test")
    cad_doc_tools.register_cad_doc_tools(mcp)
    fn = mcp._tool_manager._tools["show_in_app"].fn
    docs = [{"doc_id": "doc-cpp00000002", "name": "surface", "revision": 7}]
    calls: list[str] = []

    def _call(method, args=None):
        calls.append(method)
        assert method == "doc_list", f"unexpected engine call {method}"
        return {"docs": list(docs)}

    monkeypatch.setattr(cad_common.cb, "call", _call)
    monkeypatch.setitem(cad_common._CUR, "doc_id", None)
    monkeypatch.setattr(cad_common.cb, "consume_foreign_edits", lambda: [])
    return lambda **kw: json.loads(fn(**kw)), calls


def test_daemon_mode_reports_the_live_mirror_instead_of_a_failed_publish(
        show_in_app, monkeypatch):
    call, calls = show_in_app
    monkeypatch.setattr(cad_common.cb, "active_daemon_mode", lambda: True)
    published: list[str] = []
    monkeypatch.setattr(cad_doc_tools, "_publish_current",
                        lambda d, _deferred=False: published.append(d))

    out = call(doc_id="doc-cpp00000002")

    assert out["ok"] is True and out["live"] is True
    assert (out["doc_id"], out["name"], out["revision"]) == (
        "doc-cpp00000002", "surface", 7)
    assert published == [], "daemon mode never writes handoff files"
    assert set(calls) == {"doc_list"}


def test_legacy_mode_still_reports_a_publish_that_produced_nothing(
        show_in_app, monkeypatch):
    call, _ = show_in_app
    monkeypatch.setattr(cad_common.cb, "active_daemon_mode", lambda: False)
    monkeypatch.setattr(cad_doc_tools, "_publish_current",
                        lambda d, _deferred=False: None)

    out = call(doc_id="doc-cpp00000002")

    assert out["ok"] is False and "could not publish" in out["error"]


def test_legacy_mode_returns_the_publish_record(show_in_app, monkeypatch):
    call, _ = show_in_app
    monkeypatch.setattr(cad_common.cb, "active_daemon_mode", lambda: False)
    monkeypatch.setattr(cad_doc_tools, "_publish_current",
                        lambda d, _deferred=False: {"path": "/tmp/live.step+", "rev": 3})

    out = call(doc_id="doc-cpp00000002")

    assert out == {"ok": True, "doc_id": "doc-cpp00000002",
                   "path": "/tmp/live.step+", "rev": 3}
