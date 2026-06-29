"""Smoke test for the CAM MCP surface — drives the cam_* RPCs through the C++ core
(libaicad) the same way the MCP tools do, and checks the cad_guide('cam') text.

House style: a plain main() runner. Skips gracefully if libaicad can't be located/
loaded (e.g. the C++ core hasn't been built) so it never blocks a Python-only run.

    & $py -m mcp_server.test_cam_tools
"""
from __future__ import annotations

import os
import sys
import tempfile

from mcp_server import core_bridge as cb


def _build_part_step() -> str:
    """Build a 50x50x10 box with a Ø6 through-hole via the doc C ABI and export STEP."""
    nd = cb.call("doc_new", {"kind": "part", "name": "CamTestPart"})
    doc = nd.get("doc_id") or nd.get("id")

    def dim(v):
        return {"value": v, "expr": None}

    ops = [
        {"op": "add_feature", "feature": {"type": "box", "name": "Box1",
            "params": {"length": dim(50), "width": dim(50), "height": dim(10), "centered": True}}},
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Cyl1",
            "params": {"radius": dim(3), "height": dim(30), "centered": True, "mode": "cut"}}},
    ]
    cb.call("doc_apply_ops", {"doc_id": doc, "base_revision": -1, "ops": ops})
    path = os.path.join(tempfile.gettempdir(), "aicad_camtest.step")
    cb.call("export_file", {"doc_id": doc, "path": path, "format": "step"})
    return path


def main() -> int:
    if not cb.available():
        print("SKIP: libaicad not available (build the C++ core to run this test).")
        return 0

    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)
            print("  FAIL:", msg)
        else:
            print("  ok:", msg)

    # cad_guide('cam') text is wired (needs the `mcp` package; skipped if absent).
    try:
        from mcp_server.cad_tools import cad_guide_text
        g = cad_guide_text("cam")
        check("CAM / MACHINING GUIDE" in g, "cad_guide('cam') returns the CAM guide")
        cond = ("cam_post" in g) and ("cam_extract" in g)
        check(cond, "guide mentions the core workflow tools")
    except ModuleNotFoundError as e:
        print(f"  skip: cad_guide check (missing dep: {e.name})")

    # reference data
    dl = cb.call("cam_post_dialects", {})
    ids = [d["id"] for d in dl["dialects"]]
    check(len(ids) == 8, f"8 post dialects ({ids})")
    check("fanuc" in ids and "grbl" in ids, "fanuc + grbl present")
    check(len(cb.call("cam_materials", {})["materials"]) >= 20, "cutting DB has >=20 materials")
    check(len(cb.call("cam_op_types", {})["op_types"]) == 28, "28 operation types")

    # job lifecycle: new -> tools -> setup -> ops -> generate -> post
    job = cb.call("cam_new", {"name": "SmokeJob", "post": "fanuc"})
    doc = job["doc_id"]
    rev = job["revision"]
    a1 = cb.call("cam_apply_ops", {"doc_id": doc, "base_revision": rev, "ops": [
        {"op": "add_tool", "tool": {"number": 1, "type": "endmill_flat", "diameter": 6, "flutes": 3}},
        {"op": "add_tool", "tool": {"number": 2, "type": "drill", "diameter": 5}},
        {"op": "add_setup", "ref": "S", "setup": {
            "name": "S1", "machine": {"kind": "mill3"},
            "stock": {"type": "box", "min": [-25, -25, -10], "max": [25, 25, 0], "material": "aluminum"}}},
    ]})
    sid = a1["id_map"]["S"]
    pocket_op = {"type": "pocket2d", "tool_number": 1, "stepdown": 2, "stepover": 3,
                 "heights": {"top": 0, "bottom": -4},
                 "geometry": {"regions": [{"outer": [[-15, -15], [15, -15], [15, 15], [-15, 15]]}]}}
    drill_op = {"type": "drill", "tool_number": 2, "cycle": "peck", "peck": 2,
                "heights": {"top": 0, "bottom": -10, "retract": 2},
                "geometry": {"points": [[0, 0, 0], [10, 0, 0]]}}
    a2 = cb.call("cam_apply_ops", {"doc_id": doc, "base_revision": a1["revision"], "ops": [
        {"op": "add_operation", "setup_id": sid, "operation": pocket_op},
        {"op": "add_operation", "setup_id": sid, "operation": drill_op},
    ]})
    gen = cb.call("cam_generate", {"doc_id": doc})
    check(len(gen["toolpaths"]) == 2, "generate -> 2 toolpaths")

    sim = cb.call("cam_simulate", {"doc_id": doc})
    res0 = sim["results"][0]
    check(res0["removed_volume_mm3"] > 0, "sim removed material")
    check(res0["cycle_time_s"] > 0, "sim cycle time > 0")

    post = cb.call("cam_post", {"doc_id": doc, "dialect": "fanuc"})
    check(post["ok"], "post ok")
    check("G83" in post["gcode"], "fanuc post emits a G83 peck cycle")
    check(post["gcode"].lstrip().startswith("%"), "fanuc post has % wrap")

    # extract from a real part
    try:
        step = _build_part_step()
        ex = cb.call("cam_extract", {"path": step, "kind": "all", "deflection": 0.2})
        check(abs(ex["bbox"]["max"][0] - 25) < 0.1, "extract bbox ~ +25")
        check(len(ex["holes"]) == 1 and abs(ex["holes"][0]["dia"] - 6) < 0.1, "extract found the Ø6 hole")
        check(len(ex["mesh"]["tris"]) > 0, "extract produced a mesh")
    except Exception as e:  # noqa: BLE001
        check(False, f"cam_extract on a real STEP: {e}")

    cb.call("cam_close", {"doc_id": doc})

    if failures:
        print(f"\nFAILED: {len(failures)} check(s).")
        return 1
    print("\nALL CAM SMOKE CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
