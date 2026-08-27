"""Focused tests for additive operator details in ``cam_setup_sheet``."""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from mcp.server.fastmcp import FastMCP  # noqa: E402
from services.mcp import cam_tools as ct  # noqa: E402


def _call_tool(mcp, name: str, args: dict) -> dict:
    async def _run():
        _content, structured = await mcp.call_tool(name, args)
        text = structured.get("result") if isinstance(structured, dict) else None
        return json.loads(text) if isinstance(text, str) else text

    return asyncio.run(_run())


def test_cam_setup_sheet_adds_stock_wcs_fixture_and_tool_geometry(monkeypatch):
    base = {
        "html": "<html><body><h1>Setup Sheet</h1><p>Estimated total machining time: 1m</p></body></html>",
        "text": "SETUP SHEET\nEstimated total machining time: 1m\n",
    }
    tree = {
        "tools": [
            {"number": 7, "name": "Finisher", "type": "endmill_flat", "diameter": 8,
             "holder_diameter": 32, "holder_length": 100, "stickout": 31.5},
        ],
        "setups": [
            {"name": "OP10", "stock": {"type": "box", "material": "aluminum_6061_t6",
                                           "min": [0, 0, 0], "max": [52, 42, 15]},
             "wcs": {"offset": 54, "origin": [1, 2, 15]},
             "operations": [
                 {"id": "op1", "name": "Finish pocket", "type": "pocket2d",
                  "tool_number": 7, "wcs": {"offset": 55, "origin": [4, 5, 6]},
                  "extra": {"datum": "Pocket center",
                            "fixture_notes": "Toe clamps outside tool envelope"}},
             ]},
        ],
    }

    def fake_call(method, params=None):
        if method == "cam_setup_sheet":
            return dict(base)
        if method == "cam_tree":
            return {"tree": tree}
        raise AssertionError(f"unexpected engine method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "cam-1")
    mcp = FastMCP("setup-sheet-enrichment")
    ct.register_cam_tools(mcp)

    result = _call_tool(mcp, "cam_setup_sheet", {})
    assert set(base).issubset(result), result
    for expected in (
        "G54", "[1, 2, 15]", "52 x 42 x 15 mm",
        "G55", "Pocket center", "Toe clamps outside tool envelope",
        "diameter 32 mm, length 100 mm", "31.5 mm",
    ):
        assert expected in result["text"], result["text"]
        assert expected in result["html"], result["html"]


def test_cam_setup_sheet_keeps_base_output_when_tree_read_is_unavailable(monkeypatch):
    base = {"html": "<html><body>legacy</body></html>", "text": "legacy\n"}

    def fake_call(method, params=None):
        if method == "cam_setup_sheet":
            return dict(base)
        if method == "cam_tree":
            raise RuntimeError("old engine")
        raise AssertionError(method)

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "cam-1")
    mcp = FastMCP("setup-sheet-backcompat")
    ct.register_cam_tools(mcp)

    assert _call_tool(mcp, "cam_setup_sheet", {}) == base
