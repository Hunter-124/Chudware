"""In-process MCP roundtrip: list tools, build the sample, screenshot, read info."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp_server.server import mcp

SAMPLE = (Path(__file__).parent.parent / "engine" / "samples" / "mounting_plate.py").read_text(
    encoding="utf-8"
)


async def main() -> None:
    tools = await mcp.list_tools()
    names = sorted(t.name for t in tools)
    print("TOOLS:", names)
    assert {"run_model", "get_screenshot", "get_model_info", "export_model", "reset"} <= set(names)

    res = await mcp.call_tool("run_model", {"code": SAMPLE, "name": "mcp_demo"})
    text = _text(res)
    print("RUN_MODEL ->", text[:200].replace("\n", " "), "...")
    assert '"ok": true' in text

    shot = await mcp.call_tool("get_screenshot", {"view": "all"})
    kinds = _content_kinds(shot)
    print("GET_SCREENSHOT content kinds:", kinds)
    assert "image" in kinds

    info = _text(await mcp.call_tool("get_model_info", {}))
    print("MODEL_INFO ->", info[:160].replace("\n", " "), "...")
    assert '"volume_mm3"' in info

    # ---- multi-part / assembly tools ----
    assert {"run_assembly", "get_fit_report", "get_assembly_screenshot", "export_part"} <= set(names)

    asm_code = (
        "a = Box(20, 20, 20)\n"
        "b = Pos(10, 0, 0) * Box(20, 20, 20)\n"
        'parts = {"a": a, "b": b}\n'
    )
    asm = json.loads(_text(await mcp.call_tool("run_assembly", {"code": asm_code, "name": "mcp_asm"})))
    print("RUN_ASSEMBLY -> is_assembly:", asm["is_assembly"], "parts:",
          [p["name"] for p in asm["parts"]], "fit:", asm["fit"]["summary"])
    assert asm["ok"] and asm["is_assembly"]
    assert {p["name"] for p in asm["parts"]} == {"a", "b"}
    assert asm["fit"]["interferences"], "overlapping boxes should report interference"

    fit = json.loads(_text(await mcp.call_tool("get_fit_report", {})))
    assert fit["interferences"] and abs(fit["interferences"][0]["volume"] - 4000.0) < 1.0
    print("GET_FIT_REPORT -> overlap:", round(fit["interferences"][0]["volume"], 1), "mm^3")

    ashot = await mcp.call_tool("get_assembly_screenshot", {})
    print("GET_ASSEMBLY_SCREENSHOT content kinds:", _content_kinds(ashot))
    assert "image" in _content_kinds(ashot)

    ep = json.loads(_text(await mcp.call_tool("export_part", {"name": "a", "fmt": "step"})))
    print("EXPORT_PART a ->", ep.get("path", "")[-40:])
    assert ep.get("path", "").endswith(".step")
    ep_all = json.loads(_text(await mcp.call_tool("export_part", {"name": "assembly", "fmt": "stl"})))
    assert ep_all.get("path", "").endswith(".stl")

    print("\nALL MCP ROUNDTRIP CHECKS PASSED (single-part + assembly tools)")


def _unwrap(res):
    # FastMCP.call_tool may return (content_list, structured) or just content_list
    if isinstance(res, tuple):
        return res[0]
    return res


def _text(res) -> str:
    for c in _unwrap(res):
        if getattr(c, "type", None) == "text":
            return c.text
    return ""


def _content_kinds(res):
    return {getattr(c, "type", None) for c in _unwrap(res)}


if __name__ == "__main__":
    asyncio.run(main())
