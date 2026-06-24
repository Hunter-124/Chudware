"""Smoke tests for the C++ feature-engine MCP tools (mcp_server/cad_tools.py).

These drive the registered MCP tools end-to-end against the STAGED libaicad.dll (the same
engine the app loads) — build a part, render it, run FEA + CFD. They are skipped cleanly
when the DLL isn't built/staged, so a checkout without a build still passes.

Run:  python -m pytest mcp_server/test_cad_tools.py -v
      python mcp_server/test_cad_tools.py        # also works (prints PASS/FAIL)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# Allow `python mcp_server/test_cad_tools.py` (script dir, not repo root, lands on path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from mcp_server import core_bridge as cb

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libaicad.dll not staged (run build.bat); engine-backed MCP tools unavailable",
)


def _call(name: str, args: dict):
    """Invoke a registered MCP tool; return (parsed_json_or_None, content_list)."""
    from mcp_server import server

    content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    parsed = None
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    return parsed, content


def _new_plate_with_hole() -> str:
    r, _ = _call("new_part", {"name": "plate"})
    doc = r["doc_id"]
    ops = [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)}),
        cb.feat("hole", "Bore", {"style": "simple", "end": "through_all", "diameter": cb.dim(8)},
                {"face": {"of": "face", "side": "top"}}),
    ]
    r, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert r["ok"], r
    assert r["body_count"] == 1, r
    # Drilled: volume strictly below the solid 60*40*10 = 24000.
    assert 0 < r["volume_mm3"] < 24000, r
    return doc


def test_cad_guide_returns_vocabulary():
    r, content = _call("cad_guide", {"topic": "overview"})
    text = content[0].text
    assert "add_feature" in text and "revolve" in text.lower(), text[:200]


def test_build_plate_with_hole():
    _new_plate_with_hole()


def test_query_geometry_top_face():
    doc = _new_plate_with_hole()
    r, _ = _call("query_geometry", {"query": "top", "doc_id": doc})
    assert r["count"] >= 1
    assert any(m.get("kind") == "face" for m in r["matches"])


def test_render_part_returns_image():
    doc = _new_plate_with_hole()
    from mcp_server import server

    # call_tool's return shape varies for image vs text tools; just hunt for the ImageContent.
    def _find_image(x):
        if getattr(x, "type", "") == "image":
            return x
        if isinstance(x, (list, tuple)):
            for e in x:
                found = _find_image(e)
                if found is not None:
                    return found
        return None

    result = asyncio.run(server.mcp.call_tool("render_part", {"doc_id": doc}))
    item = _find_image(result)
    assert item is not None, f"no ImageContent in {result!r}"
    assert getattr(item, "mimeType", "").startswith("image/"), item
    assert item.data, "empty image payload"


def test_measure_part():
    doc = _new_plate_with_hole()
    r, _ = _call("measure_part", {"doc_id": doc})
    assert r["ok"]
    assert r["mass"]["volume_mm3"] > 0


def test_fea_static_cantilever():
    doc = _new_plate_with_hole()
    r, _ = _call("run_fea_static", {
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "right"}, "force": [0, 0, -200]}],
        "material": "aluminum",
        "doc_id": doc,
    })
    assert r["ok"], r
    s = r["summary"]
    assert s["max_von_mises_mpa"] > 0
    assert "min_fos" in s and s["min_fos"] > 0


def test_cfd_flow_box():
    doc = _new_plate_with_hole()
    r, _ = _call("run_cfd_flow", {"velocity": 20.0, "doc_id": doc})
    assert r["ok"], r
    assert r["drag_n"] is not None
    assert r["reynolds"] > 0


def test_export_step():
    import os
    doc = _new_plate_with_hole()
    r, _ = _call("export_part", {"fmt": "step", "doc_id": doc})
    assert r["ok"] and r["exists"], r
    os.unlink(r["path"])


if __name__ == "__main__":
    import sys

    if not cb.available():
        print("SKIP: libaicad.dll not staged")
        sys.exit(0)
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
