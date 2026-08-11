"""Smoke tests for the CAD-graph MCP tools (services/mcp/graph_tools.py, 1D).

Pure-Python — no libchudware DLL required. Registers the tools on a throwaway
FastMCP and drives them against a tmp project.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mcp.server.fastmcp import FastMCP  # noqa: E402

from services.mcp.graph_tools import register_graph_tools  # noqa: E402


def _mcp() -> FastMCP:
    m = FastMCP("test-graph")
    register_graph_tools(m)
    return m


def _call(mcp: FastMCP, name: str, args: dict):
    _content, structured = asyncio.run(mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    return json.loads(text) if isinstance(text, str) else None


def _project(tmp_path: Path) -> Path:
    doc = {
        "schema": "aicad.part/1", "id": "uuid-p", "name": "P",
        "equations": {"globals": [{"name": "W", "expr": "6", "value": 6.0}]},
        "features": [{"id": "f1", "type": "box",
                      "params": {"w": {"value": 6, "expr": "W"}}}],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("document.json", json.dumps(doc))
    (tmp_path / "P.step+").write_bytes(buf.getvalue())
    return tmp_path


def test_cad_graph_get(tmp_path: Path):
    mcp = _mcp()
    out = _call(mcp, "cad_graph",
                {"query_kind": "get", "project_dir": str(_project(tmp_path))})
    assert out["ok"] is True
    assert out["nodes"] and out["edges"]
    assert "node_count" in out and "truncated" in out


def test_cad_graph_invalid_kind(tmp_path: Path):
    out = _call(_mcp(), "cad_graph",
                {"query_kind": "bogus", "project_dir": str(tmp_path)})
    assert out["ok"] is False and out["reason"] == "invalid_query_kind"


def test_cad_graph_link_ai(tmp_path: Path):
    proj = _project(tmp_path)
    g = _call(_mcp(), "cad_graph", {"query_kind": "get", "project_dir": str(proj)})
    a, b = g["nodes"][0]["id"], g["nodes"][1]["id"]
    out = _call(_mcp(), "cad_graph_link",
                {"src": a, "dst": b, "kind": "REFERENCES",
                 "note": "n", "project_dir": str(proj)})
    assert out["ok"] is True and out["link"]["origin"] == "ai"


def test_assembly_plan_review(tmp_path: Path):
    out = _call(_mcp(), "assembly_plan_review",
                {"project_dir": str(_project(tmp_path))})
    assert out["ok"] is True
    assert "suggestions" in out and "narrative" in out


# --- plm_graph_keyword ----------------------------------------------------

def test_plm_graph_keyword_add_list_remove(tmp_path: Path):
    proj = _project(tmp_path)
    mcp = _mcp()
    g = _call(mcp, "cad_graph", {"query_kind": "get", "project_dir": str(proj)})
    target = g["nodes"][0]["id"]
    out = _call(mcp, "plm_graph_keyword",
                {"path": str(proj), "target": target, "keyword": "Hot"})
    assert out["ok"] is True
    assert out["entry"]["origin"] == "ai"
    assert out["entry"]["keyword"] == "hot"  # normalized strip/lower
    listed = _call(mcp, "plm_graph_keyword",
                   {"path": str(proj), "op": "list"})
    assert listed["ok"] is True
    assert any(e["target"] == target and e["keyword"] == "hot"
               for e in listed["entries"])
    filtered = _call(mcp, "plm_graph_keyword",
                     {"path": str(proj), "op": "list", "target": "doc:nope"})
    assert filtered["ok"] is True and filtered["entries"] == []
    entry_id = out["entry"]["id"]
    rm = _call(mcp, "plm_graph_keyword",
               {"path": str(proj), "op": "remove", "entry_id": entry_id})
    assert rm["ok"] is True and rm["removed"] == entry_id


def test_plm_graph_keyword_remove_pair(tmp_path: Path):
    proj = _project(tmp_path)
    mcp = _mcp()
    _call(mcp, "plm_graph_keyword",
          {"path": str(proj), "target": "project:root", "keyword": "x"})
    rm = _call(mcp, "plm_graph_keyword",
               {"path": str(proj), "op": "remove",
                "target": "project:root", "keyword": "x"})
    assert rm["ok"] is True and rm["removed"] == 1


def test_plm_graph_keyword_errors(tmp_path: Path):
    mcp = _mcp()
    out = _call(mcp, "plm_graph_keyword",
                {"path": "", "target": "t", "keyword": "k"})
    assert out["ok"] is False and out["reason"] == "project_dir required"
    out = _call(mcp, "plm_graph_keyword",
                {"path": str(tmp_path), "op": "bogus"})
    assert out["ok"] is False and out["reason"] == "invalid_op"


# --- plm_graph_search -----------------------------------------------------

def test_plm_graph_search(tmp_path: Path):
    proj = _project(tmp_path)
    mcp = _mcp()
    out = _call(mcp, "plm_graph_search",
                {"project_dir": str(proj), "q": "P", "kinds": "part"})
    assert out["ok"] is True
    assert out["nodes"] and all(n["kind"] == "part" for n in out["nodes"])
    assert out["node_count"] == len(out["nodes"])
    assert out["truncated"] is False


def test_plm_graph_search_requires_project():
    out = _call(_mcp(), "plm_graph_search", {"project_dir": "", "q": "x"})
    assert out["ok"] is False and out["reason"] == "project_dir required"


def test_plm_graph_search_linked_cross_project(tmp_path: Path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    proj_a = _project(dir_a)
    proj_b = _project(dir_b)
    mcp = _mcp()
    ga = _call(mcp, "cad_graph", {"query_kind": "get", "project_dir": str(proj_a)})
    gb = _call(mcp, "cad_graph", {"query_kind": "get", "project_dir": str(proj_b)})
    for proj, g in ((proj_a, ga), (proj_b, gb)):
        out = _call(mcp, "plm_graph_keyword",
                    {"path": str(proj), "target": g["nodes"][0]["id"],
                     "keyword": "shared"})
        assert out["ok"] is True
    cfg = _call(mcp, "plm_graph_configure",
                {"path": str(proj_a), "op": "set",
                 "add_linked_project": str(proj_b)})
    assert cfg["ok"] is True
    res = _call(mcp, "plm_graph_search",
                {"project_dir": str(proj_a), "keyword": "shared",
                 "linked": True})
    assert res["ok"] is True
    kw_nodes = [n for n in res["nodes"] if n["id"] == "kw:shared"]
    assert len(kw_nodes) == 1  # one shared keyword web across both projects
    projects = {str((n.get("meta") or {}).get("project"))
                for n in res["nodes"] if n["id"] != "kw:shared"}
    assert str(proj_a.resolve()) in projects
    assert str(proj_b.resolve()) in projects
    assert len(res["edges"]) >= 2  # a TAGGED edge into each project


# --- plm_graph_neighbors --------------------------------------------------

def test_plm_graph_neighbors(tmp_path: Path):
    proj = _project(tmp_path)
    mcp = _mcp()
    out = _call(mcp, "plm_graph_neighbors",
                {"project_dir": str(proj), "node_id": "doc:P.step+",
                 "depth": 1})
    assert out["ok"] is True
    ids = {n["id"] for n in out["nodes"]}
    assert "doc:P.step+" in ids and "feature:doc:P.step+:f1" in ids
    assert out["node_count"] == len(out["nodes"])
    assert out["truncated"] is False


def test_plm_graph_neighbors_not_found(tmp_path: Path):
    proj = _project(tmp_path)
    out = _call(_mcp(), "plm_graph_neighbors",
                {"project_dir": str(proj), "node_id": "doc:ghost.step+"})
    assert out["ok"] is False and out["reason"] == "not_found"


# --- plm_graph_links ------------------------------------------------------

def test_plm_graph_links_list_remove(tmp_path: Path):
    proj = _project(tmp_path)
    mcp = _mcp()
    g = _call(mcp, "cad_graph", {"query_kind": "get", "project_dir": str(proj)})
    a, b = g["nodes"][0]["id"], g["nodes"][1]["id"]
    made = _call(mcp, "cad_graph_link",
                 {"src": a, "dst": b, "kind": "REFERENCES",
                  "project_dir": str(proj)})
    assert made["ok"] is True
    listed = _call(mcp, "plm_graph_links",
                   {"project_dir": str(proj), "op": "list"})
    assert listed["ok"] is True
    assert any(lk["id"] == made["link"]["id"] for lk in listed["links"])
    rm = _call(mcp, "plm_graph_links",
               {"project_dir": str(proj), "op": "remove",
                "link_id": made["link"]["id"]})
    assert rm["ok"] is True and rm["removed"] == made["link"]["id"]


def test_plm_graph_links_errors(tmp_path: Path):
    mcp = _mcp()
    out = _call(mcp, "plm_graph_links",
                {"project_dir": str(tmp_path), "op": "bogus"})
    assert out["ok"] is False and out["reason"] == "invalid_op"
    out = _call(mcp, "plm_graph_links",
                {"project_dir": str(tmp_path), "op": "remove",
                 "link_id": "l_missing"})
    assert out["ok"] is False and out["reason"] == "not_found"


# --- plm_graph_watch ------------------------------------------------------

def test_plm_graph_watch_add_list_remove(tmp_path: Path):
    proj = _project(tmp_path)
    mcp = _mcp()
    out = _call(mcp, "plm_graph_watch",
                {"path": str(proj), "op": "add", "keyword": "hot",
                 "note": "n"})
    assert out["ok"] is True and out["entry"]["origin"] == "ai"
    listed = _call(mcp, "plm_graph_watch", {"path": str(proj), "op": "list"})
    assert listed["ok"] is True
    assert any(e.get("keyword") == "hot" for e in listed["watch"])
    rm = _call(mcp, "plm_graph_watch",
               {"path": str(proj), "op": "remove",
                "entry_id": out["entry"]["id"]})
    assert rm["ok"] is True


def test_plm_graph_watch_errors(tmp_path: Path):
    mcp = _mcp()
    out = _call(mcp, "plm_graph_watch", {"path": "", "op": "list"})
    assert out["ok"] is False and out["reason"] == "project_dir required"
    out = _call(mcp, "plm_graph_watch", {"path": str(tmp_path), "op": "bogus"})
    assert out["ok"] is False and out["reason"] == "invalid_op"


# --- plm_graph_changes ----------------------------------------------------

def test_plm_graph_changes_detects_new_file(tmp_path: Path):
    proj = _project(tmp_path)
    mcp = _mcp()
    base = _call(mcp, "cad_graph", {"query_kind": "get",
                                    "project_dir": str(proj)})
    assert base["ok"] is True  # establishes the cached baseline
    (proj / "notes.txt").write_text("hello", encoding="utf-8")
    out = _call(mcp, "plm_graph_changes", {"project_dir": str(proj)})
    assert out["ok"] is True
    added_ids = {n["id"] for n in out["nodes_added"]}
    assert "file:notes.txt" in added_ids
    assert out["watched"] == []
    assert out["nodes_added_truncated"] is False
    assert out["edges_removed_truncated"] is False


def test_plm_graph_changes_marks_watched(tmp_path: Path):
    proj = _project(tmp_path)
    mcp = _mcp()
    _call(mcp, "cad_graph", {"query_kind": "get", "project_dir": str(proj)})
    w = _call(mcp, "plm_graph_watch",
              {"path": str(proj), "op": "add", "keyword": "hot"})
    assert w["ok"] is True
    kw = _call(mcp, "plm_graph_keyword",
               {"path": str(proj), "target": "project:root",
                "keyword": "hot"})
    assert kw["ok"] is True
    out = _call(mcp, "plm_graph_changes", {"project_dir": str(proj)})
    assert out["ok"] is True
    assert "kw:hot" in out["watched"]
    flagged = {n["id"] for n in out["nodes_added"] if n.get("watched")}
    assert "kw:hot" in flagged


def test_plm_graph_changes_requires_project():
    out = _call(_mcp(), "plm_graph_changes", {"project_dir": ""})
    assert out["ok"] is False and out["reason"] == "project_dir required"


# --- plm_graph_configure --------------------------------------------------

def test_plm_graph_configure_get_and_set(tmp_path: Path):
    proj = _project(tmp_path)
    other = tmp_path / "linked"
    other.mkdir()
    mcp = _mcp()
    got = _call(mcp, "plm_graph_configure", {"path": str(proj), "op": "get"})
    assert got["ok"] is True
    assert got["config"]["office_content"] is False
    assert got["project_key"]
    out = _call(mcp, "plm_graph_configure",
                {"path": str(proj), "op": "set", "office_content": True,
                 "add_linked_project": str(other)})
    assert out["ok"] is True
    assert out["config"]["office_content"] is True
    assert out["config"]["linked_projects"] == [str(other.resolve())]


def test_plm_graph_configure_errors(tmp_path: Path):
    mcp = _mcp()
    out = _call(mcp, "plm_graph_configure", {"path": "", "op": "get"})
    assert out["ok"] is False and out["reason"] == "project_dir required"
    out = _call(mcp, "plm_graph_configure",
                {"path": str(tmp_path), "op": "bogus"})
    assert out["ok"] is False and out["reason"] == "invalid_op"


# --- plm_graph_suggest ----------------------------------------------------

def test_plm_graph_suggest_name_match(tmp_path: Path):
    proj = _project(tmp_path)
    (proj / "P.txt").write_text("spec sheet for P", encoding="utf-8")
    mcp = _mcp()
    out = _call(mcp, "plm_graph_suggest", {"project_dir": str(proj)})
    assert out["ok"] is True
    sug = out["suggestions"]
    assert sug
    pair = [s for s in sug
            if {s["src"], s["dst"]} == {"file:P.txt", "doc:P.step+"}]
    # stem "p" appears in the doc's label/id → name_match outranks same_folder
    assert pair and pair[0]["reason"] == "name_match"
    assert pair[0]["confidence"] == 0.7
    assert pair[0]["kind"] == "REFERENCES"
    confs = [s["confidence"] for s in sug]
    assert confs == sorted(confs, reverse=True)


def test_plm_graph_suggest_shared_keyword(tmp_path: Path):
    proj = _project(tmp_path)
    (proj / "widget.txt").write_text("x", encoding="utf-8")
    mcp = _mcp()
    for target in ("doc:P.step+", "file:widget.txt"):
        out = _call(mcp, "plm_graph_keyword",
                    {"path": str(proj), "target": target,
                     "keyword": "common"})
        assert out["ok"] is True
    out = _call(mcp, "plm_graph_suggest", {"project_dir": str(proj)})
    assert out["ok"] is True
    sk = [s for s in out["suggestions"] if s["reason"] == "shared_keyword"]
    assert any({s["src"], s["dst"]} == {"doc:P.step+", "file:widget.txt"}
               for s in sk)


def test_plm_graph_suggest_node_filter_and_dedupe(tmp_path: Path):
    proj = _project(tmp_path)
    (proj / "P.txt").write_text("x", encoding="utf-8")
    mcp = _mcp()
    # an existing overlay link must suppress the suggestion for that pair
    made = _call(mcp, "cad_graph_link",
                 {"src": "file:P.txt", "dst": "doc:P.step+",
                  "kind": "REFERENCES", "project_dir": str(proj)})
    assert made["ok"] is True
    out = _call(mcp, "plm_graph_suggest",
                {"project_dir": str(proj), "node_id": "doc:P.step+"})
    assert out["ok"] is True
    assert all(s["src"] == "doc:P.step+" or s["dst"] == "doc:P.step+"
               for s in out["suggestions"])
    assert not any({s["src"], s["dst"]} == {"file:P.txt", "doc:P.step+"}
                   for s in out["suggestions"])


def test_plm_graph_suggest_errors(tmp_path: Path):
    mcp = _mcp()
    out = _call(mcp, "plm_graph_suggest", {"project_dir": ""})
    assert out["ok"] is False and out["reason"] == "project_dir required"
    out = _call(mcp, "plm_graph_suggest",
                {"project_dir": str(_project(tmp_path)),
                 "node_id": "doc:ghost.step+"})
    assert out["ok"] is False and out["reason"] == "not_found"


# --- plm_graph_brief ------------------------------------------------------

def test_plm_graph_brief(tmp_path: Path):
    proj = _project(tmp_path)
    mcp = _mcp()
    out = _call(mcp, "plm_graph_brief",
                {"project_dir": str(proj), "node_id": "doc:P.step+"})
    assert out["ok"] is True
    assert out["node"]["id"] == "doc:P.step+"
    assert out["node"]["kind"] == "part"
    assert out["degree"] == sum(out["edge_kinds"].values())
    assert out["degree"] > 0
    ids = {n["id"] for n in out["neighbors"]}
    assert "project:root" in ids and "feature:doc:P.step+:f1" in ids
    assert len(out["neighbors"]) <= 25
    assert out["overlay_links"] == [] and out["watched"] is False


def test_plm_graph_brief_not_found(tmp_path: Path):
    proj = _project(tmp_path)
    out = _call(_mcp(), "plm_graph_brief",
                {"project_dir": str(proj), "node_id": "doc:ghost.step+"})
    assert out["ok"] is False and out["reason"] == "not_found"
