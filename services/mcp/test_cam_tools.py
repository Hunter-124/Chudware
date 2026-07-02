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


def _check_registry_foldin(check) -> None:
    """DoD (T-M4): CuttingDB::builtin() loads the materials registry at RUNTIME (in-code fallback).

    Discriminating check: point the core at a registry copy whose aluminum cam.sfm_mill is a value
    NOT in the in-code table, in a FRESH subprocess (builtin() is a cached singleton), and confirm
    cam_materials reflects it — proving a real file load, not a build-time snapshot."""
    import json
    import subprocess
    try:
        reg = json.loads((_registry_path()).read_text())
        reg["materials"]["aluminum_6061_t6"]["cam"]["sfm_mill"] = 4321.0
        d = tempfile.mkdtemp()
        p = os.path.join(d, "registry.json")
        with open(p, "w") as fh:
            json.dump(reg, fh)
    except Exception as e:  # noqa: BLE001
        print(f"  skip: registry fold-in setup ({e})")
        return
    env = dict(os.environ, AICAD_MATERIALS_REGISTRY=p)
    code = (
        "import sys; sys.path[:0]=['services/mcp','.']\n"
        "from mcp_server import core_bridge as cb\n"
        "m={x['id']:x for x in cb.call('cam_materials',{})['materials']}\n"
        "print(m['aluminum']['sfm_mill'])\n"
    )
    try:
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, timeout=120, cwd=os.getcwd())
        val = float((out.stdout.strip().splitlines() or ["0"])[-1])
        check(val == 4321.0, "cam_materials reflects the registry at runtime (in-code fallback)")
        # And unset -> in-code stands (aluminum's builtin sfm_mill is 900). The raw RPC has no
        # cam_tools wiring, so with no env it falls back to the in-code table.
        env2 = dict(os.environ)
        env2.pop("AICAD_MATERIALS_REGISTRY", None)
        out2 = subprocess.run([sys.executable, "-c", code], env=env2, capture_output=True,
                             text=True, timeout=120, cwd=os.getcwd())
        val2 = float((out2.stdout.strip().splitlines() or ["0"])[-1])
        check(val2 == 900.0, "unset registry -> CuttingDB in-code table stands (headless fallback)")
    except Exception as e:  # noqa: BLE001
        check(False, f"registry fold-in runtime check: {e}")


def _check_add_setup_material_resolution(check) -> None:
    """DoD (T-M4): cam_add_setup accepts any registry material ID (translated to a cutting group)."""
    try:
        from mcp_server.cam_tools import _resolve_cam_material
    except Exception as e:  # noqa: BLE001
        print(f"  skip: cam_add_setup material resolution ({e})")
        return
    rid, note = _resolve_cam_material("aluminum_6061_t6")
    check(rid == "aluminum" and note is None, "registry id 'aluminum_6061_t6' -> group 'aluminum'")
    ralias, _ = _resolve_cam_material("6061")
    check(ralias == "aluminum", "registry alias '6061' -> group 'aluminum'")
    rgroup, _ = _resolve_cam_material("steel_mild")
    check(rgroup == "steel_mild", "bare CuttingDB id 'steel_mild' passes through")
    rnone, note2 = _resolve_cam_material("peek")   # a registry material with no cam fold-in
    check(rnone == "peek" and bool(note2), "registry material w/o cam data accepted + noted")


def _registry_path():
    from engine.materials import registry as _reg
    return _reg.registry_path()


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

    # T-M4: the CuttingDB folds the engine materials registry's `cam` sub-sections at runtime.
    _check_registry_foldin(check)
    _check_add_setup_material_resolution(check)

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
