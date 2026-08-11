"""Regression tests for services/mcp/office_tools.py (op-vocabulary sweep).

Pure-Python — no libchudware DLL required. Registers the tools on a throwaway
FastMCP and drives them against a tmp PDF.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from mcp.server.fastmcp import FastMCP  # noqa: E402

pypdf = pytest.importorskip("pypdf")

from services.mcp.office_tools import register_office_tools  # noqa: E402


def _mcp() -> FastMCP:
    m = FastMCP("test-office")
    register_office_tools(m)
    return m


def _call(mcp: FastMCP, name: str, args: dict):
    _content, structured = asyncio.run(mcp.call_tool(name, args))
    out = structured.get("result") if isinstance(structured, dict) else structured
    return json.loads(out) if isinstance(out, str) else out


def _tiny_pdf(tmp_path: Path) -> str:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    path = tmp_path / "in.pdf"
    with open(path, "wb") as f:
        writer.write(f)
    return str(path)


def test_office_edit_pdf_unknown_op_names_the_closed_vocabulary(tmp_path):
    """Class-3/5: the old message was ``unknown op '<x>'`` and nothing else — it
    named neither the accepted vocabulary nor a hint to recover. The op space
    here is closed and tiny (rotate/delete/merge/set_metadata), so the error
    must say so."""
    path = _tiny_pdf(tmp_path)
    out = _call(_mcp(), "office_edit_pdf",
                {"path": path, "ops": [{"op": "resize"}]})
    assert out["ok"] is False
    assert out["reason"] == "invalid_op"
    assert "resize" in out["hint"]
    for word in ("rotate", "delete", "merge", "set_metadata"):
        assert word in out["hint"]


def test_office_edit_pdf_still_rejects_reflow_as_unsupported(tmp_path):
    """Recognized-but-unsupported ops (edit_text/replace_text/reflow) must keep
    their distinct ``unsupported_op`` reason, not fall into the generic
    unknown-op branch."""
    path = _tiny_pdf(tmp_path)
    out = _call(_mcp(), "office_edit_pdf",
                {"path": path, "ops": [{"op": "edit_text"}]})
    assert out["ok"] is False
    assert out["reason"] == "unsupported_op"
