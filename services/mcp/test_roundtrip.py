"""In-process MCP roundtrip over the C++ feature engine: list tools, learn the vocabulary,
build a part, render it, measure it, run FEA, export. A runnable narrative companion to the
pytest smoke suite (test_cad_tools.py); prints each step so you can eyeball the engine.

Run:  python -m services.mcp.test_roundtrip
"""
from __future__ import annotations

import asyncio
import json

from services.mcp import core_bridge as cb
from services.mcp.server import mcp


async def main() -> None:
    tools = await mcp.list_tools()
    names = sorted(t.name for t in tools)
    print("TOOLS:", names)
    assert {"cad_guide", "new_part", "add_features", "render_part", "run_fea_static",
            "run_cfd_flow", "export_part"} <= set(names)

    guide = _text(await mcp.call_tool("cad_guide", {"topic": "overview"}))
    print("CAD_GUIDE ->", len(guide), "chars; mentions revolve:", "revolve" in guide.lower())
    assert "add_feature" in guide

    doc = json.loads(_text(await mcp.call_tool("new_part", {"name": "bracket"})))["doc_id"]
    print("NEW_PART ->", doc)

    ops = [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)}),
        cb.feat("hole", "Bore", {"style": "simple", "end": "through_all", "diameter": cb.dim(8)},
                {"face": {"of": "face", "side": "top"}}),
        cb.feat("fillet", "Round", {"radius": cb.dim(3)},
                {"edges": {"of": "edge", "parallel": "z"}}),
    ]
    built = json.loads(_text(await mcp.call_tool("add_features", {"ops": ops, "doc_id": doc})))
    print("ADD_FEATURES -> bodies:", built["body_count"], "vol:", round(built["volume_mm3"], 1),
          "feature ok:", [f["ok"] for f in built["features"]])
    assert built["ok"] and built["body_count"] == 1

    shot = await mcp.call_tool("render_part", {"doc_id": doc})
    print("RENDER_PART content kinds:", _content_kinds(shot))
    assert "image" in _content_kinds(shot)

    info = json.loads(_text(await mcp.call_tool("measure_part", {"doc_id": doc})))
    print("MEASURE_PART -> vol mm^3:", round(info["mass"]["volume_mm3"], 1),
          "mass g:", round(info["mass"]["mass_g"], 2))
    assert info["mass"]["volume_mm3"] > 0

    fea = json.loads(_text(await mcp.call_tool("run_fea_static", {
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "right"}, "force": [0, 0, -200]}],
        "material": "aluminum", "doc_id": doc,
    })))
    print("RUN_FEA_STATIC ->", fea["summary"])
    assert fea["ok"] and fea["summary"]["min_fos"] > 0

    ep = json.loads(_text(await mcp.call_tool("export_part", {"fmt": "step", "doc_id": doc})))
    print("EXPORT_PART ->", ep["path"][-44:])
    assert ep["ok"] and ep["exists"]

    print("\nALL MCP ROUNDTRIP CHECKS PASSED (C++ feature engine + FEA)")


def _unwrap(res):
    # FastMCP.call_tool may return (content_list, structured) or just a content_list / item.
    if isinstance(res, tuple):
        res = res[0]
    if not isinstance(res, (list, tuple)):
        res = [res]
    return res


def _text(res) -> str:
    for c in _unwrap(res):
        if getattr(c, "type", None) == "text":
            return c.text
    return ""


def _content_kinds(res):
    return {getattr(c, "type", None) for c in _unwrap(res)}


if __name__ == "__main__":
    if not cb.available():
        print("SKIP: libchudware.dll not staged (run build.bat)")
    else:
        asyncio.run(main())
