"""Hermetic tests for reference-first reverse engineering."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from services.mcp import reveng_common as common
from services.mcp import reveng_session_tools as sessions
from services.mcp.reveng_tools import register_reveng_tools
from services.parts import model_matcher


class Collector:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "sessions"))
    collector = Collector()
    register_reveng_tools(collector)
    return collector.tools


def call(tools, method, **kwargs):
    return json.loads(tools[method](**kwargs))


def test_project_scoped_session_listing(tools, tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    first = call(tools, "reveng_start_session", name="A", project_dir=str(a))
    second = call(tools, "reveng_start_session", name="B", project_dir=str(b))
    assert first["ok"] and second["ok"]
    unscoped = call(tools, "reveng_start_session", name="Unscoped")
    all_sessions = call(tools, "list_reveng_sessions")
    assert {s["session_id"] for s in all_sessions["sessions"]} == {
        first["session_id"], second["session_id"], unscoped["session_id"]}
    for project, included, excluded in ((a, first, second), (b, second, first)):
        result = call(tools, "list_reveng_sessions", project_dir=str(project))
        ids = {s["session_id"] for s in result["sessions"]}
        assert included["session_id"] in ids
        assert excluded["session_id"] not in ids
        assert unscoped["session_id"] not in ids
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(a))
    assert [s["session_id"] for s in call(tools, "list_reveng_sessions")["sessions"]] == [first["session_id"]]


def test_find_models_maps_matcher_results_and_persists(tools, monkeypatch):
    matcher = Mock(return_value={"ok": True, "matches": [
        {"part_id": "catalog:grabcad::https://grabcad.com/library/widget",
         "title": "Widget", "site": "grabcad", "confidence": "strong", "attach": False},
        {"part_id": "standard:bolt", "title": "M8 bolt", "site": "standard_hardware",
         "artifact_plan": {"family": "hex_bolt", "size": "M8"}},
        {"part_id": "catalog:broken", "site": "other"},
    ], "coverage": {"sites_searched": 2, "raw_hits": 3}})
    monkeypatch.setattr(model_matcher, "match_model", matcher)
    sid = call(tools, "reveng_start_session", name="Widget")["session_id"]
    result = call(tools, "reveng_find_models", session_id=sid, query="Widget 123", limit=4)
    assert result["ok"]
    assert matcher.call_args.args[0] == {"title": "Widget 123", "mpn": ""}
    assert result["candidates"] == [{"title": "Widget", "site": "grabcad",
                                     "url": "https://grabcad.com/library/widget",
                                     "confidence": "strong", "attach": False}]
    assert result["standard_part"]["family"] == "hex_bolt"
    saved = call(tools, "get_reveng_session", session_id=sid)["session"]["reference"]
    assert saved["candidates"] == result["candidates"]
    assert saved["coverage"] == result["coverage"]
    assert saved["searched_at"]


def test_find_models_url_does_not_search(tools, monkeypatch):
    matcher = Mock(side_effect=AssertionError("URL must not search"))
    monkeypatch.setattr(model_matcher, "match_model", matcher)
    sid = call(tools, "reveng_start_session")["session_id"]
    url = "https://example.org/models/a%20part.step"
    result = call(tools, "reveng_find_models", session_id=sid, query=url)
    assert result["ok"] and result["candidates"][0]["url"] == url
    assert result["candidates"][0]["attach"] is False
    matcher.assert_not_called()


def test_find_models_ranks_focused_titles_first_and_honours_limit(tools, monkeypatch):
    def hit(slug, title):
        return {"part_id": f"catalog:grabcad::https://grabcad.com/library/{slug}",
                "title": title, "site": "grabcad", "confidence": "weak"}
    monkeypatch.setattr(model_matcher, "match_model", Mock(return_value={"ok": True, "matches": [
        hit("a-gripper", "Robotic gripper for MG996R servo with bracket"),
        hit("b-bracket", "MG996R bracket"),
        hit("c-servo", "Servo MG996R"),
    ], "coverage": {"sites_searched": 1, "raw_hits": 3}}))
    sid = call(tools, "reveng_start_session")["session_id"]
    result = call(tools, "reveng_find_models", session_id=sid, query="MG996R servo", limit=2)
    assert [c["title"] for c in result["candidates"]] == ["Servo MG996R", "MG996R bracket"]


def test_background_find_models_stores_result_and_drops_superseded(tools, monkeypatch):
    import threading
    release = {"old": threading.Event(), "new": threading.Event()}

    def search(query, **kw):
        release["old" if query == "old part" else "new"].wait(5)
        return {"ok": True, "query": query, "candidates": [
            {"title": query, "site": "grabcad", "url": f"https://grabcad.com/{query[:3]}",
             "confidence": "weak", "attach": False}],
            "standard_part": None, "coverage": {"sites_searched": 1, "raw_hits": 1}, "hint": "h"}
    monkeypatch.setattr(common, "_find_reference_models", search)
    sid = call(tools, "reveng_start_session")["session_id"]

    def reference():
        return call(tools, "get_reveng_session", session_id=sid)["session"]["reference"]

    assert call(tools, "reveng_find_models", session_id=sid, query="old part",
                wait=False) == {"ok": True, "session_id": sid, "query": "old part", "pending": True}
    assert reference()["status"] == "searching"
    call(tools, "reveng_find_models", session_id=sid, query="new part", wait=False)
    release["new"].set()
    for _ in range(200):
        if reference()["status"] != "searching":
            break
        threading.Event().wait(0.02)
    release["old"].set()
    threading.Event().wait(0.2)   # the superseded search finishes after the new one
    saved = reference()
    assert saved["status"] == "done" and saved["query"] == "new part"
    assert [c["title"] for c in saved["candidates"]] == ["new part"]


def test_start_research_skips_ingest_when_title_covers_query(tools, monkeypatch):
    # Community sites carry no part-number field: a title holding every query word still
    # means the model exists, so research must not run.
    monkeypatch.setattr(sessions, "_find_reference_models", lambda query, **kw: {
        "ok": True, "query": query, "candidates": [{"title": "Servo MG996R (metal gear)",
                                                     "url": "https://grabcad.com/x",
                                                     "confidence": "weak"}],
        "standard_part": None, "coverage": {}})
    ingest = Mock(side_effect=AssertionError("an existing model should skip research"))
    monkeypatch.setattr(sessions, "_research_ingest", ingest)
    assert call(tools, "reveng_start_session", research="mg996r servo")["ok"]
    ingest.assert_not_called()


@pytest.mark.parametrize("seed,key", [("6204-2RS", "part_number"),
                                       ("SKF bearing", "query"),
                                       ("https://example.org/spec.pdf", "url")])
def test_start_research_ingests_when_no_match(tools, monkeypatch, seed, key):
    monkeypatch.setattr(sessions, "_find_reference_models", lambda query, **kw: {
        "ok": True, "query": query, "candidates": [], "standard_part": None,
        "coverage": {"sites_searched": 1, "raw_hits": 0}})
    ingest = Mock(return_value={"ok": True, "candidate_dims": []})
    monkeypatch.setattr(sessions, "_research_ingest", ingest)
    result = call(tools, "reveng_start_session", research=seed)
    assert result["ok"] and result["research_result"]["ok"]
    ingest.assert_called_once_with(session_id=result["session_id"], **{key: seed})


def test_start_research_skips_ingest_for_match(tools, monkeypatch):
    monkeypatch.setattr(sessions, "_find_reference_models", lambda query, **kw: {
        "ok": True, "query": query, "candidates": [{"url": "https://example.org/a.step",
                                                       "confidence": "strong"}],
        "standard_part": None, "coverage": {}})
    ingest = Mock(side_effect=AssertionError("matching reference should skip ingest"))
    monkeypatch.setattr(sessions, "_research_ingest", ingest)
    result = call(tools, "reveng_start_session", research="bearing")
    assert result["ok"]
    ingest.assert_not_called()
    saved = call(tools, "get_reveng_session", session_id=result["session_id"])["session"]
    assert saved["reference"]["candidates"][0]["url"] == "https://example.org/a.step"


def test_use_model_local_and_failed_replacement(tools, tmp_path):
    sid = call(tools, "reveng_start_session", name="Reference")["session_id"]
    path = tmp_path / "reference.step"
    path.write_bytes(b"ISO-10303-21;\nEND-ISO-10303-21;\n")
    result = call(tools, "reveng_use_model", session_id=sid, path=str(path))
    assert result["ok"] and result["path"] == str(path)
    saved = call(tools, "get_reveng_session", session_id=sid)["session"]
    assert saved["reference"]["model"]["path"] == str(path)
    assert saved["reference"]["model"]["sha256"]
    assert saved["artifacts"]["reference_model_path"] == str(path)
    assert saved["stages"]["capture"] == saved["stages"]["model"] == "done"
    assert saved["lane"] == "reference" and saved["status"] == "done"
    assert saved["provenance"][-1]["step"] == "use_reference_model"
    failed = call(tools, "reveng_use_model", session_id=sid, path=str(tmp_path / "missing.step"))
    assert failed["ok"] is False
    after = call(tools, "get_reveng_session", session_id=sid)["session"]
    assert after["reference"]["model"] == saved["reference"]["model"]
    assert call(tools, "reveng_use_model", session_id=sid)["ok"] is False
    assert call(tools, "reveng_use_model", session_id=sid,
                path=str(path), url="https://example.org/model.step")["ok"] is False


def test_use_model_url_acquisition_and_failure(tools, tmp_path, monkeypatch):
    from services.mcp import parts_tools

    project = tmp_path / "project"
    project.mkdir()
    sid = call(tools, "reveng_start_session", name="Widget",
               project_dir=str(project))["session_id"]
    url = "https://example.org/widget.step"
    artifact = str(tmp_path / "widget.step")
    fetch = Mock(return_value={"ok": True, "artifact_path": artifact, "format": "step",
                               "source_url": url,
                               "provenance": {"site": "example.org", "sha256": "abc"}})
    monkeypatch.setattr(parts_tools, "fetch_cad_model_impl", fetch)
    result = call(tools, "reveng_use_model", session_id=sid, url=url)
    assert result["ok"] and result["path"] == artifact
    # Downloads land in the session's own working folder, never an outer marked ancestor.
    fetch.assert_called_once_with(url, name="Widget", import_part=False,
                                  project_root=Path(os.path.realpath(project)))
    saved = call(tools, "get_reveng_session", session_id=sid)["session"]
    assert saved["reference"]["model"]["source_url"] == url
    fetch.return_value = {"ok": False, "reason": "login_required", "hint": "Sign in"}
    failed = call(tools, "reveng_use_model", session_id=sid, url=url)
    assert failed["ok"] is False and failed["reason"] == "login_required"
    after = call(tools, "get_reveng_session", session_id=sid)["session"]
    assert after["reference"]["model"] == saved["reference"]["model"]


def test_library_search_returns_existing_model_and_force_build_bypasses(tools, monkeypatch):
    from services.mcp import reveng_build_tools as builds

    candidate = {"title": "Bearing", "url": "https://example.org/bearing.step",
                 "site": "example.org", "confidence": "exact", "attach": False}
    search = Mock(return_value={"ok": True, "query": "6204-2RS",
                                "candidates": [candidate], "standard_part": None,
                                "coverage": {"sites_searched": 1, "raw_hits": 1}})
    ingest = Mock(return_value={"ok": False, "reason": "offline", "hint": "Offline"})
    monkeypatch.setattr(builds, "_find_reference_models", search)
    monkeypatch.setattr(builds, "_research_ingest", ingest)
    result = call(tools, "reveng_make_part_from_library", part_ref="bearing",
                  part_number="6204-2RS")
    assert result["ok"] and result["model_present"] is False
    assert result["existing_models"] == [candidate]
    search.assert_called_once_with("6204-2RS")
    ingest.assert_not_called()
    saved = call(tools, "get_reveng_session", session_id=result["session_id"])["session"]
    assert saved["reference"]["candidates"] == [candidate]
    search.reset_mock()
    forced = call(tools, "reveng_make_part_from_library", part_ref="bearing",
                  part_number="6204-2RS", force_build=True)
    assert forced["ok"] is False and forced["reason"] == "offline"
    search.assert_not_called()
    ingest.assert_called_once()
