"""Smoke test for the CAM MCP surface — drives the cam_* RPCs through the C++ core
(libaicad) the same way the MCP tools do, and checks the cad_guide('cam') text.

House style: a plain main() runner. Skips gracefully if libaicad can't be located/
loaded (e.g. the C++ core hasn't been built) so it never blocks a Python-only run.

    & $py -m mcp_server.test_cam_tools
"""
from __future__ import annotations

import math
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


def _check_hole_extraction(cb, check) -> None:  # noqa: ANN001 - cb module + check() closure
    """T-M5 hole-extraction upgrade: real-geometry checks for counterbore/countersink/
    tilted-hole records and the kind="sections"/"profile" extract modes.

    All ops go through the doc C ABI (doc_new/add_feature/doc_apply_ops/export_file),
    same as _build_part_step, then cam_extract reads the exported STEP.
    """

    def dim(v):
        return {"value": v, "expr": None}

    # --- counterbore / countersink / simple holes on an 80x80x20 box -----------------
    doc = None
    try:
        nd = cb.call("doc_new", {"kind": "part", "name": "HoleExtractTest"})
        doc = nd.get("doc_id") or nd.get("id")
        ops = [
            {"op": "add_feature", "feature": {"type": "box", "name": "B1",
                "params": {"length": dim(80), "width": dim(80), "height": dim(20),
                           "centered": True}}},
            {"op": "add_feature", "feature": {"type": "hole", "name": "Hcb",
                "params": {"face": {"of": "face", "side": "top"}, "style": "cbore",
                           "diameter": 6, "cbore_d": 12, "cbore_depth": 5,
                           "end": "through_all", "positions": [[-25, 0]]}}},
            {"op": "add_feature", "feature": {"type": "hole", "name": "Hcs",
                "params": {"face": {"of": "face", "side": "top"}, "style": "csink",
                           "diameter": 6, "csink_d": 12, "csink_angle": 90,
                           "end": "through_all", "positions": [[0, 0]]}}},
            {"op": "add_feature", "feature": {"type": "hole", "name": "Hs",
                "params": {"face": {"of": "face", "side": "top"}, "style": "simple",
                           "diameter": 5, "end": "through_all", "positions": [[25, 0]]}}},
        ]
        r = cb.call("doc_apply_ops", {"doc_id": doc, "base_revision": -1, "ops": ops})
        fs = r["rebuild"]["feature_status"]
        check(all(s.get("ok") for s in fs),
              "cbore/csink/simple hole features built ok "
              f"({[s.get('error') for s in fs if not s.get('ok')]})")

        path = os.path.join(tempfile.gettempdir(), "aicad_cam_holeextract.step")
        cb.call("export_file", {"doc_id": doc, "path": path, "format": "step"})
        ex = cb.call("cam_extract", {"path": path, "kind": "holes"})
        holes = ex.get("holes", [])
        check(len(holes) == 3, f"3 holes extracted from the cbore/csink/simple part (got {len(holes)})")

        by_kind = {h.get("kind"): h for h in holes}

        cbore = by_kind.get("counterbore")
        check(cbore is not None, "a 'counterbore' record is present in holes[]")
        if cbore:
            check(abs(cbore.get("dia", 0) - 6) < 0.5, f"counterbore dia ~6 (got {cbore.get('dia')})")
            check(abs(cbore.get("dia_major", 0) - 12) < 0.5,
                  f"counterbore dia_major ~12 (got {cbore.get('dia_major')})")
            check(abs(cbore.get("dia_minor", 0) - 6) < 0.5,
                  f"counterbore dia_minor ~6 (got {cbore.get('dia_minor')})")
            check(abs(cbore.get("depth_major", 0) - 5) < 1.0,
                  f"counterbore depth_major ~5 (got {cbore.get('depth_major')})")
            check(cbore.get("depth_minor", 0) > 10,
                  f"counterbore depth_minor > 10 (got {cbore.get('depth_minor')})")
            check(abs(cbore.get("tilt_deg", 99) - 0) < 0.5,
                  f"counterbore tilt_deg ~0 (got {cbore.get('tilt_deg')})")

        csink = by_kind.get("countersink")
        check(csink is not None, "a 'countersink' record is present in holes[]")
        if csink:
            check(abs(csink.get("dia", 0) - 6) < 0.5, f"countersink dia ~6 (got {csink.get('dia')})")
            check(abs(csink.get("csink_angle_deg", 0) - 90) < 2.0,
                  f"countersink csink_angle_deg ~90 (got {csink.get('csink_angle_deg')})")
            check(abs(csink.get("csink_outer_dia", 0) - 12) < 0.5,
                  f"countersink csink_outer_dia ~12 (got {csink.get('csink_outer_dia')})")
            check(abs(csink.get("tilt_deg", 99) - 0) < 0.5,
                  f"countersink tilt_deg ~0 (got {csink.get('tilt_deg')})")

        simple = by_kind.get("simple")
        check(simple is not None, "a 'simple' hole record is present in holes[]")
        if simple:
            check(abs(simple.get("dia", 0) - 5) < 0.5, f"simple hole dia ~5 (got {simple.get('dia')})")
            check(abs(simple.get("tilt_deg", 99) - 0) < 0.5,
                  f"simple hole tilt_deg ~0 (got {simple.get('tilt_deg')})")

        # --- kind="sections" -----------------------------------------------------
        sec = cb.call("cam_extract", {"path": path, "kind": "sections", "z_levels": [-5, 0, 5]})
        check("bbox" in sec and "levels" in sec, "sections extract returns bbox+levels")
        levels = sec.get("levels", [])
        check(len(levels) == 3, f"sections extract has 3 z-levels (got {len(levels)})")
        check(all("regions" in lv for lv in levels), "every section level carries a 'regions' key")
        check(all(len(lv.get("regions", [])) > 0 for lv in levels),
              "every section level has at least one region")

        # --- kind="profile" --------------------------------------------------------
        prof = cb.call("cam_extract", {"path": path, "kind": "profile"})
        check("bbox" in prof and "profile" in prof, "profile extract returns bbox+profile")
        check(len(prof.get("profile", [])) > 0, "profile extract returns a non-empty [z,x] list")
    except Exception as e:  # noqa: BLE001
        check(False, f"counterbore/countersink/simple/sections/profile extraction: {e}")
    finally:
        if doc is not None:
            try:
                cb.call("cam_close", {"doc_id": doc})
            except Exception:  # noqa: BLE001
                pass

    # --- tilted hole -------------------------------------------------------------
    # T-M5 upgrade: the extractor now accepts holes with |axis.Z()| >= 0.95 (tilt up
    # to ~18deg) instead of the old >= 0.99 threshold. Build a hole tilted ~10deg
    # (cos(10deg) ~= 0.985 -- rejected by the OLD 0.99 threshold, accepted by the
    # NEW 0.95 one) by drilling normal to a sloped face made from a box's top face
    # via an asymmetric two-distance chamfer that consumes (almost) the whole face.
    #
    # Empirically determined (core/geom/dress.cpp chamfer_edges_two_distance, via
    # BRepFilletAPI_MakeChamfer::Add(d1, d2, edge, face) where `face` is the edge's
    # FIRST incident face per TopExp::MapShapesAndAncestors):
    #   - params.distance (leg 1) lands on the box's FRONT face -> bounded by the
    #     box HEIGHT (>= height fails "too large").
    #   - params.length2  (leg 2) lands on the box's TOP face   -> bounded by the
    #     box WIDTH (>= width fails "too large"; must stay strictly under it).
    # A leg2 just under the box width turns (almost) the entire top face into one
    # sloped plane, tilted by atan(leg1/leg2) from horizontal.
    tilt_doc = None
    try:
        box_w = 60.0
        top_leg = 59.99  # just under box_w -> consumes ~the whole top face
        tilt_target_deg = 10.0
        front_leg = top_leg * math.tan(math.radians(tilt_target_deg))

        nd2 = cb.call("doc_new", {"kind": "part", "name": "TiltedHoleTest"})
        tilt_doc = nd2.get("doc_id") or nd2.get("id")
        ops2 = [
            {"op": "add_feature", "feature": {"type": "box", "name": "B1",
                "params": {"length": dim(box_w), "width": dim(box_w), "height": dim(20),
                           "centered": True}}},
            {"op": "add_feature", "feature": {"type": "chamfer", "name": "Ch1",
                "params": {"distance": front_leg, "length2": top_leg},
                "refs": {"edges": {"of": "edge", "near": [0, -box_w / 2, 10], "count": 1}}}},
        ]
        r1 = cb.call("doc_apply_ops", {"doc_id": tilt_doc, "base_revision": -1, "ops": ops2})
        fs1 = r1["rebuild"]["feature_status"]
        chamfer_ok = all(s.get("ok") for s in fs1)
        if not chamfer_ok:
            print("  skip: tilted-hole chamfer build failed: "
                  f"{[s.get('error') for s in fs1 if not s.get('ok')]}")
        else:
            # Drill normal to the sloped face, targeting a point that sits ON that
            # face (NOT the {"side":"top"} query, which would instead pick the thin
            # sliver of ORIGINAL flat top face left over from top_leg < box_w).
            zmid = 10 - front_leg * 0.5
            ops3 = [
                {"op": "add_feature", "feature": {"type": "hole", "name": "Ht",
                    "params": {"face": {"of": "face", "near": [0, 0, zmid], "count": 1},
                               "style": "simple", "diameter": 5, "end": "through_all",
                               "positions": [[0, 0]]}}},
            ]
            r2 = cb.call("doc_apply_ops",
                         {"doc_id": tilt_doc, "base_revision": r1["revision"], "ops": ops3})
            fs2 = r2["rebuild"]["feature_status"]
            hole_ok = all(s.get("ok") for s in fs2)
            if not hole_ok:
                print("  skip: tilted-hole drill failed: "
                      f"{[s.get('error') for s in fs2 if not s.get('ok')]}")
            else:
                path2 = os.path.join(tempfile.gettempdir(), "aicad_cam_tilted.step")
                cb.call("export_file", {"doc_id": tilt_doc, "path": path2, "format": "step"})
                ext = cb.call("cam_extract", {"path": path2, "kind": "holes"})
                tholes = ext.get("holes", [])
                tilted = [h for h in tholes if h.get("tilt_deg", 0) > 5]
                check(len(tilted) >= 1,
                      "a tilted hole (tilt_deg > 5) appears in holes[] "
                      f"(got tilt_deg values {[h.get('tilt_deg') for h in tholes]})")
                if tilted:
                    check(tilted[0]["tilt_deg"] < 18,
                          "tilted hole's tilt_deg is within the new-threshold-accepted range "
                          f"(<18deg, got {tilted[0]['tilt_deg']})")
                    # Pin dia too, so the tilt>5 match is provably the Ø5 bore we drilled
                    # (not some other artifact) -- both fields are exact/stable here.
                    check(abs(tilted[0].get("dia", 0) - 5) < 0.5,
                          f"tilted hole dia ~5 (got {tilted[0].get('dia')})")
    except Exception as e:  # noqa: BLE001
        check(False, f"tilted-hole extraction: {e}")
    finally:
        if tilt_doc is not None:
            try:
                cb.call("cam_close", {"doc_id": tilt_doc})
            except Exception:  # noqa: BLE001
                pass


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

    # T-M5: hole-extraction upgrade (counterbore/countersink/tilted holes) +
    # kind="sections"/"profile" extract modes, on real geometry.
    _check_hole_extraction(cb, check)

    cb.call("cam_close", {"doc_id": doc})

    if failures:
        print(f"\nFAILED: {len(failures)} check(s).")
        return 1
    print("\nALL CAM SMOKE CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
