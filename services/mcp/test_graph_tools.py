"""Smoke tests for the CAD-graph MCP tools (services/mcp/graph_tools.py, 1D).

Pure-Python — no libaicad DLL required. Registers the tools on a throwaway
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

from mcp_server.graph_tools import register_graph_tools  # noqa: E402


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
