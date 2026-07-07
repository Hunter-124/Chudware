"""Smoke tests for services/mcp/sketch3d_tools.py.

Tests the Python-side logic (3D sketch CRUD, geometry, connectivity analysis) without
requiring the C++ core / libaicad.  Engine-backed tools (render_sketch, doc_sketch_contact_sheet,
sketch3d_as_sweep_guide) are exercised in the skip-guarded section at the bottom.

Run:
  cd /home/hunter/Repositories/CAD
  python3 -m pytest services/mcp/test_sketch3d_tools.py -q
  python3 services/mcp/test_sketch3d_tools.py            # also works
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import sys

# ── import-shadow repair (same as test_gsl_tools) ────────────────────────────
_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


def _unshadow_mcp_sdk() -> None:
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile:
        return
    if os.path.dirname(os.path.realpath(sfile)) != _HERE:
        return
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _HERE:
            spec = importlib.util.spec_from_file_location(
                "mcp", init,
                submodule_search_locations=[os.path.dirname(init), _HERE],
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module
            spec.loader.exec_module(module)
            return


_unshadow_mcp_sdk()

import pytest  # noqa: E402

from mcp_server import core_bridge as cb  # noqa: E402
from mcp_server.sketch3d_tools import _STORE, _S3Sketch, _seg_from_dict  # noqa: E402

_engine_available = cb.available()


# ─────────────────────────────────────────────── helpers ──────────────────────

def _call(name: str, args: dict):
    """Invoke a registered MCP tool; return (parsed_json_or_None, raw_content).

    Handles both the two-value (content, structured) return from FastMCP tools that
    return a string, and the single-value return from tools that return [Image, text]
    (image-bearing tools — those return a flat list from call_tool in some MCP versions).
    """
    from mcp_server import server
    result = asyncio.run(server.mcp.call_tool(name, args))
    # FastMCP may return (content_list, structured_dict) or just a content_list.
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        text = structured.get("result") if isinstance(structured, dict) else None
    else:
        # Image / list return — content is the result itself; no structured JSON.
        content = result
        text = None
    parsed = None
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    # For image returns, check if any content item is text we can parse.
    if parsed is None and isinstance(content, (list, tuple)):
        for item in content:
            raw = getattr(item, "text", None)
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                    break
                except (json.JSONDecodeError, Exception):
                    pass
    return parsed, content


# ─────────────────────────────────────────────── unit: _S3Sketch ──────────────


class TestS3SketchGeometry:
    """Pure-Python geometry: no MCP calls, no engine."""

    def test_new_sketch_empty(self):
        sk = _S3Sketch("test")
        assert sk.id.startswith("s3d_")
        assert sk.name == "test"
        assert sk.segs == []
        assert sk.bbox() is None
        assert sk.total_length() == 0.0

    def test_bbox_single_line(self):
        sk = _S3Sketch("line_test")
        from mcp_server.sketch3d_tools import _S3Seg
        sk.segs.append(_S3Seg("line", [[0, 0, 0], [10, 0, 0]]))
        bb = sk.bbox()
        assert bb is not None
        assert bb["min"] == [0.0, 0.0, 0.0]
        assert bb["max"] == [10.0, 0.0, 0.0]
        assert bb["size"] == [10.0, 0.0, 0.0]

    def test_length_line(self):
        sk = _S3Sketch("len_test")
        from mcp_server.sketch3d_tools import _S3Seg
        sk.segs.append(_S3Seg("line", [[0, 0, 0], [3, 4, 0]]))
        assert abs(sk.total_length() - 5.0) < 1e-9  # 3-4-5 triangle

    def test_length_polyline(self):
        sk = _S3Sketch("poly_test")
        from mcp_server.sketch3d_tools import _S3Seg
        # Two unit steps
        sk.segs.append(_S3Seg("polyline", [[0, 0, 0], [1, 0, 0], [1, 1, 0]]))
        assert abs(sk.total_length() - 2.0) < 1e-9


class TestSegFromDict:
    """_seg_from_dict validation."""

    def test_line_ok(self):
        seg = _seg_from_dict({"kind": "line", "pts": [[0, 0, 0], [1, 0, 0]]})
        assert seg.kind == "line"
        assert len(seg.pts) == 2

    def test_arc_ok(self):
        seg = _seg_from_dict(
            {"kind": "arc", "pts": [[0, 0, 0], [1, 1, 0], [2, 0, 0]]}
        )
        assert seg.kind == "arc"
        assert len(seg.pts) == 3

    def test_spline_ok(self):
        seg = _seg_from_dict(
            {"kind": "spline", "pts": [[0, 0, 0], [5, 5, 5], [10, 0, 10]]}
        )
        assert seg.kind == "spline"

    def test_line_wrong_pts_count(self):
        with pytest.raises(ValueError, match="exactly 2"):
            _seg_from_dict({"kind": "line", "pts": [[0, 0, 0]]})

    def test_arc_wrong_pts_count(self):
        with pytest.raises(ValueError, match="exactly 3"):
            _seg_from_dict({"kind": "arc", "pts": [[0, 0, 0], [1, 0, 0]]})

    def test_bad_kind(self):
        with pytest.raises(ValueError, match="Unknown curve kind"):
            _seg_from_dict({"kind": "bezier", "pts": [[0, 0, 0], [1, 0, 0]]})

    def test_bad_point_shape(self):
        with pytest.raises(ValueError, match=r"pts\[0\]"):
            _seg_from_dict({"kind": "line", "pts": [[0, 0], [1, 0, 0]]})


# ─────────────────────────────────────────────── MCP tools (pure Python) ─────


class TestSketch3dMcpTools:
    """Tests that run the full MCP tool path but need no engine."""

    def test_new_returns_id(self):
        r, _ = _call("sketch3d_new", {"name": "my_path"})
        assert r is not None and r.get("ok"), r
        assert r["name"] == "my_path"
        assert r["id"].startswith("s3d_")

    def test_add_curves_line(self):
        r0, _ = _call("sketch3d_new", {"name": "sweep_path"})
        sid = r0["id"]
        r, _ = _call("sketch3d_add_curves", {
            "sketch3d_id": sid,
            "curves": [
                {"kind": "line", "pts": [[0, 0, 0], [0, 0, 50]]},
            ],
        })
        assert r["ok"], r
        assert r["added"] == 1
        assert r["total_segs"] == 1
        assert abs(r["total_length_mm"] - 50.0) < 0.01

    def test_add_multiple_curves(self):
        r0, _ = _call("sketch3d_new", {"name": "multi_path"})
        sid = r0["id"]
        r, _ = _call("sketch3d_add_curves", {
            "sketch3d_id": sid,
            "curves": [
                {"kind": "line",     "pts": [[0, 0, 0], [10, 0, 0]]},
                {"kind": "polyline", "pts": [[10, 0, 0], [10, 10, 0], [10, 10, 20]]},
                {"kind": "arc",      "pts": [[10, 10, 20], [15, 15, 25], [20, 10, 20]]},
            ],
        })
        assert r["ok"], r
        assert r["added"] == 3
        assert r["total_segs"] == 3

    def test_get_returns_geometry(self):
        r0, _ = _call("sketch3d_new", {"name": "get_test"})
        sid = r0["id"]
        _call("sketch3d_add_curves", {
            "sketch3d_id": sid,
            "curves": [{"kind": "line", "pts": [[0, 0, 0], [30, 40, 0]]}],
        })
        r, _ = _call("sketch3d_get", {"sketch3d_id": sid})
        assert r["ok"], r
        assert r["seg_count"] == 1
        assert abs(r["total_length_mm"] - 50.0) < 0.01
        bb = r["bbox"]
        assert bb["max"][0] == pytest.approx(30.0)

    def test_clear(self):
        r0, _ = _call("sketch3d_new", {"name": "clear_test"})
        sid = r0["id"]
        _call("sketch3d_add_curves", {
            "sketch3d_id": sid,
            "curves": [{"kind": "line", "pts": [[0, 0, 0], [1, 0, 0]]}],
        })
        rc, _ = _call("sketch3d_clear", {"sketch3d_id": sid})
        assert rc["ok"], rc
        r, _ = _call("sketch3d_get", {"sketch3d_id": sid})
        assert r["seg_count"] == 0

    def test_list(self):
        # Ensure at least one sketch in store from earlier tests.
        r, _ = _call("sketch3d_list", {})
        assert r["ok"], r
        assert isinstance(r["sketches"], list)

    def test_preview_connectivity(self):
        r0, _ = _call("sketch3d_new", {"name": "chain_test"})
        sid = r0["id"]
        # Two connected line segments sharing a point at (10,0,0).
        _call("sketch3d_add_curves", {
            "sketch3d_id": sid,
            "curves": [
                {"kind": "line", "pts": [[0, 0, 0], [10, 0, 0]]},
                {"kind": "line", "pts": [[10, 0, 0], [20, 0, 0]]},
            ],
        })
        r, _ = _call("sketch3d_preview", {"sketch3d_id": sid})
        assert r["ok"], r
        assert r["connected_chains"] == 1, (
            f"Expected 1 chain for two connected lines, got {r['connected_chains']}"
        )

    def test_preview_disconnected(self):
        r0, _ = _call("sketch3d_new", {"name": "disco_test"})
        sid = r0["id"]
        # Two disconnected segments.
        _call("sketch3d_add_curves", {
            "sketch3d_id": sid,
            "curves": [
                {"kind": "line", "pts": [[0, 0, 0],    [5, 0, 0]]},
                {"kind": "line", "pts": [[10, 0, 0], [15, 0, 0]]},
            ],
        })
        r, _ = _call("sketch3d_preview", {"sketch3d_id": sid})
        assert r["ok"], r
        assert r["connected_chains"] == 2, (
            f"Expected 2 chains for disconnected lines, got {r['connected_chains']}"
        )

    def test_unknown_sketch_id_returns_error(self):
        r, _ = _call("sketch3d_get", {"sketch3d_id": "s3d_does_not_exist"})
        assert r is not None
        assert r.get("ok") is False
        assert "s3d_does_not_exist" in r.get("error", "")

    def test_add_curves_empty_list_returns_error(self):
        r0, _ = _call("sketch3d_new", {"name": "empty_curves"})
        r, _ = _call("sketch3d_add_curves", {"sketch3d_id": r0["id"], "curves": []})
        assert r is not None
        assert r.get("ok") is False

    def test_as_sweep_guide_empty_sketch_returns_error(self):
        r0, _ = _call("sketch3d_new", {"name": "empty_guide"})
        if not _engine_available:
            return  # skip without engine
        r, _ = _call("sketch3d_as_sweep_guide", {"sketch3d_id": r0["id"]})
        # Should fail because sketch is empty OR because no current doc.
        assert r is not None and r.get("ok") is False


# ─────────────────────────────────── engine-backed tests (skipped if no core) ─


@pytest.mark.skipif(
    not _engine_available,
    reason="libaicad not staged — C++ engine not available",
)
class TestSketch3dEngineIntegration:
    """End-to-end: create a part, inject a 3D sketch as a guide, verify it round-trips."""

    def _new_part(self, name: str) -> str:
        r, _ = _call("new_part", {"name": name})
        assert r and r.get("ok"), f"new_part failed: {r}"
        return r["doc_id"]

    def test_sketch3d_as_sweep_guide_basic(self):
        doc_id = self._new_part("guide_test_part")
        r0, _ = _call("sketch3d_new", {"name": "helix_guide"})
        sid = r0["id"]
        # A simple straight path up Z.
        _call("sketch3d_add_curves", {
            "sketch3d_id": sid,
            "curves": [{"kind": "line", "pts": [[0, 0, 0], [0, 0, 30]]}],
        })
        r, _ = _call("sketch3d_as_sweep_guide",
                     {"sketch3d_id": sid, "role": "guide", "doc_id": doc_id})
        assert r is not None and r.get("ok"), f"sketch3d_as_sweep_guide failed: {r}"
        assert "wire_id" in r
        # guide_support flag reflects whether the engine build has the feature.
        assert isinstance(r.get("guide_support"), bool)

    def test_doc_sketch_contact_sheet_runs(self):
        doc_id = self._new_part("contact_sheet_test")
        # Add a simple box so there's something to render.
        _call("add_features", {"ops": [{"op": "box", "dx": 20, "dy": 20, "dz": 20}],
                               "doc_id": doc_id})
        r, content = _call("doc_sketch_contact_sheet", {"doc_id": doc_id})
        # A list return means [Image, caption_text] — content will have ImageContent.
        assert content or r is not None  # Either structured or content returned


# ─────────────────────────────────────────────── __main__ runner ─────────────

if __name__ == "__main__":
    import traceback

    print("Running sketch3d_tools smoke tests...")
    passed = failed = 0

    suites = [
        TestS3SketchGeometry,
        TestSegFromDict,
        TestSketch3dMcpTools,
    ]
    if _engine_available:
        suites.append(TestSketch3dEngineIntegration)
    else:
        print("  [skip] Engine not available — TestSketch3dEngineIntegration skipped")

    for cls in suites:
        obj = cls()
        for name in [m for m in dir(cls) if m.startswith("test_")]:
            fn = getattr(obj, name)
            try:
                fn()
                print(f"  PASS  {cls.__name__}.{name}")
                passed += 1
            except Exception as exc:
                print(f"  FAIL  {cls.__name__}.{name}: {exc}")
                traceback.print_exc()
                failed += 1

    print(f"\n{'PASS' if failed == 0 else 'FAIL'}  {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
