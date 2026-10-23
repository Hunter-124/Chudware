"""Smoke test for the CAM MCP surface — drives the cam_* RPCs through the C++ core
(libchudware) the same way the MCP tools do, and checks the cad_guide('cam') text.

House style: a plain main() runner. Skips gracefully if libchudware can't be located/
loaded (e.g. the C++ core hasn't been built) so it never blocks a Python-only run.

    & $py -m services.mcp.test_cam_tools
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import sys
import tempfile

import pytest

from services.mcp import core_bridge as cb

_HERE = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))


def _unshadow_mcp_sdk() -> None:
    """Repair a pytest package-import collision: this directory has no ``mcp`` subpackage,
    but under some collection orders ``services.mcp`` still ends up bound at ``sys.modules
    ["mcp"]`` (same repair as test_cam_audit.py / test_cad_tools.py), which would otherwise
    break ``from mcp.server.fastmcp import FastMCP`` below."""
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile or os.path.dirname(os.path.realpath(sfile)) != _HERE:
        return
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _HERE:
            spec = importlib.util.spec_from_file_location(
                "mcp", init, submodule_search_locations=[os.path.dirname(init), _HERE])
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module
            spec.loader.exec_module(module)
            return
    raise ImportError("could not locate the real `mcp` SDK to un-shadow services/mcp")


_unshadow_mcp_sdk()


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
    path = os.path.join(tempfile.gettempdir(), "chudware_camtest.step")
    cb.call("export_file", {"doc_id": doc, "path": path, "format": "step"})
    return path


def _check_registry_foldin(check) -> None:
    """CuttingDB::builtin() loads the materials registry at RUNTIME (in-code fallback).

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
    env = dict(os.environ, CHUDWARE_MATERIALS_REGISTRY=p)
    code = (
        "import sys; sys.path[:0]=['services/mcp','.']\n"
        "from services.mcp import core_bridge as cb\n"
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
        env2.pop("CHUDWARE_MATERIALS_REGISTRY", None)
        out2 = subprocess.run([sys.executable, "-c", code], env=env2, capture_output=True,
                             text=True, timeout=120, cwd=os.getcwd())
        val2 = float((out2.stdout.strip().splitlines() or ["0"])[-1])
        check(val2 == 900.0, "unset registry -> CuttingDB in-code table stands (headless fallback)")
    except Exception as e:  # noqa: BLE001
        check(False, f"registry fold-in runtime check: {e}")


def _check_add_setup_material_resolution(check) -> None:
    """cam_add_setup accepts any registry material ID (translated to a cutting group)."""
    try:
        from services.mcp.cam_tools import _resolve_cam_material
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
    # engine.materials.registry was deleted with the oracle; the bundled registry
    # the core loads lives at core/data/materials/registry.json.
    from pathlib import Path
    return Path(__file__).resolve().parents[2] / "core" / "data" / "materials" / "registry.json"


def _check_hole_extraction(cb, check) -> None:  # noqa: ANN001 - cb module + check() closure
    """Hole-extraction upgrade: real-geometry checks for counterbore/countersink/
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

        path = os.path.join(tempfile.gettempdir(), "chudware_cam_holeextract.step")
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
    # Upgrade: the extractor now accepts holes with |axis.Z()| >= 0.95 (tilt up
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
                path2 = os.path.join(tempfile.gettempdir(), "chudware_cam_tilted.step")
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
        print("SKIP: libchudware not available (build the C++ core to run this test).")
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
        from services.mcp.cad_tools import cad_guide_text
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

    # The CuttingDB folds the engine materials registry's `cam` sub-sections at runtime.
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

    # check_holder is accepted + forwarded to the core (holder/shank collision sim).
    # Mirrors the plain cam_simulate call above -- param-plumbing check (DoD), not a claim
    # about tripping a real collision (needs geometry deep/narrow enough to snag the tool
    # body, out of scope here).
    sim_h = cb.call("cam_simulate", {"doc_id": doc, "options": {"check_holder": True}})
    res0_h = sim_h["results"][0]
    check("holder_collisions" in res0_h and isinstance(res0_h["holder_collisions"], int),
          "check_holder=True accepted; holder_collisions surfaced as an int "
          f"(got {res0_h.get('holder_collisions')!r})")

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

    # Hole-extraction upgrade (counterbore/countersink/tilted holes) +
    # kind="sections"/"profile" extract modes, on real geometry.
    _check_hole_extraction(cb, check)

    cb.call("cam_close", {"doc_id": doc})

    if failures:
        print(f"\nFAILED: {len(failures)} check(s).")
        return 1
    print("\nALL CAM SMOKE CHECKS PASSED.")
    return 0


# --------------------------------------------------------------------------------------- #
# Semantic write-confirmation tests (fake bridge, no live engine) — cover the mutating
# cam_* MCP tools whose responses were upgraded from a bare id/tree echo to a dense
# "changes" summary (+ "job_stats" counts), and cam_save's output-path resolution fix.
# --------------------------------------------------------------------------------------- #
def _call_tool(mcp, name, args):
    async def _run():
        _c, structured = await mcp.call_tool(name, args)
        text = structured.get("result") if isinstance(structured, dict) else None
        return json.loads(text) if isinstance(text, str) else text
    return asyncio.run(_run())


def test_cam_add_tool_changes_field(monkeypatch):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        params = dict(params or {})
        if method == "cam_apply_ops":
            tool = params["ops"][0]["tool"]
            return {"revision": 1, "id_map": {}, "tree": {"tools": [tool], "setups": []}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")

    mcp = FastMCP("test-cam-add-tool")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_add_tool",
                      {"number": 3, "type": "drill", "diameter": 5.0, "flutes": 2})
    assert "tree" not in out, out
    assert out.get("job_stats") == {"tools": 1, "setups": 0, "operations": 0}, out
    changes = out.get("changes", "")
    assert "#3" in changes and "drill" in changes and "5" in changes, changes


# --------------------------------------------------------------------------------------- #
# Discriminator VALUE guards (silent-fall-through regressions).
#
# core/cam/model.cpp:100  tool_type_from_string -> lookup_rev(kTool, s, EndmillFlat)
# core/cam/extract.cpp:361 the kind if/else chain's final `else` is the "all" payload
# Both substitute a default for an unrecognised value WITHOUT erroring. Measured on this
# engine before the fix: cam_add_tool(type="ball"|"DRILL"|"driller") each stored
# type="endmill_flat" (so feeds/speeds resolved a flat endmill's cutting class), and
# cam_extract(kind="regions") answered with the "all" payload, which has no `regions` key.
# --------------------------------------------------------------------------------------- #
def test_resolve_tool_type_vocabulary_and_aliases():
    """Pure-Python: the accepted set, the mapped short forms, and the rejections."""
    from services.mcp import cam_tools as ct

    # Every id in the kTool mirror is accepted and returned unchanged.
    for tid in ct._TOOL_TYPES_KTOOL:
        assert ct._resolve_tool_type(tid) == tid, tid
    # Plain equivalents map to the tool they obviously mean.
    for word, want in [("ball", "endmill_ball"), ("ballnose", "endmill_ball"),
                       ("flat", "endmill_flat"), ("endmill", "endmill_flat"),
                       ("chamfer", "chamfer_mill"), ("facemill", "face_mill"),
                       ("twist_drill", "drill"), ("driller", "drill"),
                       ("v_bit", "vbit"), ("bullnose", "endmill_bull"),
                       ("parting_blade", "parting_tool"), ("laser", "laser_head")]:
        assert ct._resolve_tool_type(word) == want, (word, want)
    # Case and padding are noise, not a different tool.
    assert ct._resolve_tool_type("DRILL") == "drill"
    assert ct._resolve_tool_type("  Endmill_Ball  ") == "endmill_ball"
    # A word naming no specific tool stays rejected — guessing one is the very silent
    # substitution this guard exists to stop.
    for bad in ("totally_bogus", "mill", "cutter", "", "   ", None, 7):
        with pytest.raises(ValueError):
            ct._resolve_tool_type(bad)


def test_resolve_tool_type_error_names_the_valid_set():
    from services.mcp import cam_tools as ct

    with pytest.raises(ValueError) as ei:
        ct._resolve_tool_type("wobble_mill")
    msg = str(ei.value)
    assert "wobble_mill" in msg
    for tid in ("endmill_flat", "endmill_ball", "drill", "tap", "face_mill",
                "boring_bar_id", "probe"):
        assert tid in msg, (tid, msg)
    assert "cam_tool_types" in msg, msg


def test_resolve_tool_type_folds_in_the_live_engine_enum(monkeypatch):
    """The vocabulary is the ENGINE's cam_tool_types unioned with the mirror, so a type
    the engine gains is never rejected by a stale Python list — and an unreachable
    engine must not pin the mirror-only floor for the rest of the process."""
    from services.mcp import cam_tools as ct

    monkeypatch.setitem(ct._TOOL_TYPES_CACHE, "ids", None)
    monkeypatch.setattr(ct.cb, "call", lambda m, p=None: {
        "tool_types": [{"id": "endmill_flat"}, {"id": "brand_new_cutter"}]})
    assert ct._resolve_tool_type("brand_new_cutter") == "brand_new_cutter"
    assert ct._TOOL_TYPES_CACHE["ids"] is not None      # a real answer IS cached

    def boom(method, params=None):
        raise RuntimeError("engine down")

    monkeypatch.setitem(ct._TOOL_TYPES_CACHE, "ids", None)
    monkeypatch.setattr(ct.cb, "call", boom)
    assert ct._resolve_tool_type("drill") == "drill"    # the mirror still validates
    assert ct._TOOL_TYPES_CACHE["ids"] is None          # ... and is NOT pinned


def test_cam_add_tool_rejects_unknown_type_without_touching_the_library(monkeypatch):
    """The negative case: no add_tool op reaches the engine, and the response names the set."""
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    applied: list = []

    def fake_call(method, params=None):
        if method == "cam_tool_types":
            return {"tool_types": [{"id": t} for t in ct._TOOL_TYPES_KTOOL]}
        if method == "cam_apply_ops":
            applied.append(dict(params or {}))
            tool = params["ops"][0]["tool"]
            return {"revision": 1, "id_map": {}, "tree": {"tools": [tool], "setups": []}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setitem(ct._TOOL_TYPES_CACHE, "ids", None)
    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    mcp = FastMCP("test-cam-add-tool-badtype")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_add_tool", {"number": 1, "type": "driller_9000"})
    assert out.get("ok") is False, out
    assert "driller_9000" in out["error"] and "endmill_flat" in out["error"], out
    assert applied == [], applied     # the tool table was never written to


def test_cam_add_tool_stores_the_canonical_type_and_matching_name(monkeypatch):
    """A valid type still lands, and an alias lands as the tool it means — record AND
    label. Before the fix, type="ball" stored endmill_flat under the name "T3 ball"."""
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    stored: list = []

    def fake_call(method, params=None):
        if method == "cam_tool_types":
            return {"tool_types": [{"id": t} for t in ct._TOOL_TYPES_KTOOL]}
        if method == "cam_apply_ops":
            tool = params["ops"][0]["tool"]
            stored.append(tool)
            return {"revision": 1, "id_map": {}, "tree": {"tools": [tool], "setups": []}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setitem(ct._TOOL_TYPES_CACHE, "ids", None)
    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    mcp = FastMCP("test-cam-add-tool-canon")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_add_tool", {"number": 2, "type": "drill", "diameter": 5.0})
    assert stored[-1]["type"] == "drill" and stored[-1]["name"] == "T2 drill", stored[-1]
    assert "drill" in out.get("changes", ""), out

    _call_tool(mcp, "cam_add_tool", {"number": 3, "type": "ballnose", "diameter": 4.0})
    assert stored[-1]["type"] == "endmill_ball", stored[-1]
    assert stored[-1]["name"] == "T3 endmill_ball", stored[-1]

    # An explicit name is still the caller's to choose.
    _call_tool(mcp, "cam_add_tool", {"number": 4, "type": "tap", "name": "M6 tapper"})
    assert stored[-1]["type"] == "tap" and stored[-1]["name"] == "M6 tapper", stored[-1]


def test_resolve_extract_kind_vocabulary_and_aliases():
    from services.mcp import cam_tools as ct

    for kind in ct._EXTRACT_KINDS:
        assert ct._resolve_extract_kind(kind) == kind, kind
    for word, want in [("regions", "section"), ("slice", "section"),
                       ("levels", "sections"), ("waterline", "sections"),
                       ("drill_points", "holes"), ("points", "holes"),
                       ("silhouette", "profile"), ("bounds", "stock"),
                       ("triangles", "mesh"), ("STOCK", "stock"), ("  Mesh  ", "mesh")]:
        assert ct._resolve_extract_kind(word) == want, (word, want)
    with pytest.raises(ValueError) as ei:
        ct._resolve_extract_kind("bogus_kind")
    msg = str(ei.value)
    assert "bogus_kind" in msg and "section" in msg and "profile" in msg, msg


def test_cam_extract_rejects_unknown_kind_before_exporting_anything(monkeypatch):
    """A bad kind must cost nothing: no STEP export, no cam_extract RPC."""
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    seen: list = []

    def fake_call(method, params=None):
        seen.append(method)
        if method == "export_file":
            return {"path": params["path"]}
        if method == "cam_extract":
            return {"bbox": {}, "kind_seen": params["kind"]}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CUR, "doc_id", "part-1")
    mcp = FastMCP("test-cam-extract-kind")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_extract", {"kind": "bogus_kind"})
    assert out.get("ok") is False and "bogus_kind" in out["error"], out
    assert seen == [], seen

    # A mapped equivalent reaches the engine as the kind it means.
    out = _call_tool(mcp, "cam_extract", {"kind": "regions"})
    assert out.get("kind_seen") == "section", out
    assert seen == ["export_file", "cam_extract"], seen


def test_cam_add_setup_changes_field(monkeypatch):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        params = dict(params or {})
        if method == "material_cam_id":
            return {"result": "aluminum"}
        if method == "cam_apply_ops":
            setup = dict(params["ops"][0]["setup"])
            setup["id"] = "su1"
            setup["operations"] = []
            return {"revision": 1, "id_map": {"_s": "su1"}, "tree": {"tools": [], "setups": [setup]}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")

    mcp = FastMCP("test-cam-add-setup-changes")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_add_setup", {"name": "S1", "machine": "mill3", "material": "aluminum"})
    assert out.get("setup_id") == "su1", out
    assert "tree" not in out, out
    assert out.get("job_stats") == {"tools": 0, "setups": 1, "operations": 0}, out
    changes = out.get("changes", "")
    assert "S1" in changes and "aluminum" in changes, changes


def test_cam_set_stock_changes_field(monkeypatch):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        params = dict(params or {})
        if method == "cam_apply_ops":
            stock = params["ops"][0]["stock"]
            return {"revision": 2, "id_map": {},
                    "tree": {"tools": [], "setups": [{"id": "su1", "stock": stock, "operations": []}]}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")

    mcp = FastMCP("test-cam-set-stock")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_set_stock",
                      {"setup_id": "su1", "stock": {"type": "box", "min": [0, 0, 0], "max": [10, 10, 10]}})
    changes = out.get("changes", "")
    assert "su1" in changes and "box" in changes, changes


def test_cam_add_operation_changes_field(monkeypatch):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        params = dict(params or {})
        if method == "cam_apply_ops":
            op = params["ops"][0]["operation"]
            return {"revision": 3, "id_map": {"_o": "op1"},
                    "tree": {"tools": [], "setups": [{"id": "su1", "operations": [op]}]}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")

    mcp = FastMCP("test-cam-add-operation")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_add_operation",
                      {"type": "pocket2d", "tool_number": 1, "setup_id": "su1",
                       "geometry": {"regions": [{"outer": [[0, 0], [10, 0], [10, 10], [0, 10]]}]}})
    assert out.get("op_id") == "op1", out
    assert out.get("job_stats") == {"tools": 0, "setups": 1, "operations": 1}, out
    changes = out.get("changes", "")
    assert "pocket2d" in changes and "#1" in changes and "regions" in changes, changes


def test_cam_update_operation_changes_field(monkeypatch):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        if method == "cam_apply_ops":
            return {"revision": 4, "id_map": {},
                    "tree": {"tools": [], "setups": [{"id": "su1", "operations": [{"id": "op1"}]}]}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")

    mcp = FastMCP("test-cam-update-operation")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_update_operation", {"op_id": "op1", "params": {"stepover": 2, "feed": 900}})
    assert out.get("changes") == "operation op1 updated: feed, stepover", out


def test_cam_remove_operation_changes_field(monkeypatch):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        if method == "cam_apply_ops":
            return {"revision": 5, "id_map": {}, "tree": {"tools": [], "setups": [{"id": "su1", "operations": []}]}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")

    mcp = FastMCP("test-cam-remove-operation")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_remove_operation", {"op_id": "op1"})
    assert out.get("job_stats") == {"tools": 0, "setups": 1, "operations": 0}, out
    changes = out.get("changes", "")
    assert "op1" in changes and "removed" in changes and "0 operation" in changes, changes


def test_cam_undo_redo_changes_field(monkeypatch):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    state = {"undo_calls": 0}

    def fake_call(method, params=None):
        if method == "cam_undo":
            state["undo_calls"] += 1
            if state["undo_calls"] == 1:
                return {"revision": 5, "tree": {"tools": [], "setups": []}}
            return {"revision": 5, "tree": {"tools": [], "setups": []}, "noop": True}
        if method == "cam_redo":
            return {"revision": 6, "tree": {"tools": [], "setups": []}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")

    mcp = FastMCP("test-cam-undo-redo")
    ct.register_cam_tools(mcp)

    out1 = _call_tool(mcp, "cam_undo", {})
    assert "tree" not in out1 and out1.get("changes") == "undid last CAM edit -> revision 5", out1

    out2 = _call_tool(mcp, "cam_undo", {})
    assert out2.get("changes") == "nothing to undo", out2

    out3 = _call_tool(mcp, "cam_redo", {})
    assert out3.get("changes") == "redid last undone CAM edit -> revision 6", out3


def test_cam_save_resolves_path_and_confirms_write(tmp_path, monkeypatch):
    """cam_save resolves a relative path against the selected workspace, not either
    process's cwd, and confirms that the file actually landed there."""
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        if method == "cam_save":
            p = params["path"]
            assert os.path.isabs(p), f"cam_save must receive an already-resolved abs path, got {p!r}"
            with open(p, "wb") as f:
                f.write(b"CAMDATA")
            return {"path": p}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    worker = tmp_path / "worker"
    worker.mkdir()
    monkeypatch.chdir(worker)

    mcp = FastMCP("test-cam-save")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_save", {"path": "job.cam+"})
    expected = str(tmp_path / "job.cam+")
    assert out.get("path") == expected, out
    assert out.get("exists") is True, out
    assert out.get("size_bytes") == len(b"CAMDATA"), out
    assert expected in out.get("changes", "") and "7 bytes" in out.get("changes", ""), out
    assert list(worker.iterdir()) == []


def test_cam_open_changes_field(monkeypatch, tmp_path):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        if method == "cam_open":
            assert os.path.isabs(params["path"]), params["path"]
            return {"doc_id": "doc-2", "revision": 0,
                    "tree": {"tools": [{"number": 1}],
                             "setups": [{"id": "su1", "operations": [{"id": "op1"}]}]}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))

    mcp = FastMCP("test-cam-open")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_open", {"path": "job.cam+"})
    assert ct._CAM["doc_id"] == "doc-2", out
    assert out.get("job_stats") == {"tools": 1, "setups": 1, "operations": 1}, out
    changes = out.get("changes", "")
    assert "1 setup" in changes and "1 tool" in changes and "1 operation" in changes, changes


def test_cam_post_refuses_a_program_with_no_motion(monkeypatch):
    """THE classic CAM false green. Measured live 2026-07-30: a pocket2d whose geometry
    produced no regions generated 0 toolpaths (cam_generate warned "pocket: no regions"),
    and cam_post still answered ok:true / line_count:9 / warnings:[] with
    "G21 G90 G17 G40 M5 M9 M30" — a syntactically valid NC file that machines nothing."""
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    header_only = "( J - S1)\n( post: GRBL )\nG21\nG90\nG17\nG40\nM5\nM9\nM30\n"

    def fake_call(method, params=None):
        if method == "cam_post":
            return {"ok": True, "gcode": header_only, "line_count": 9,
                    "tool_changes": 0, "est_time_s": 0.0, "warnings": [], "error": ""}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    mcp = FastMCP("test-cam-post-empty")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_post", {"dialect": "grbl"})
    assert out.get("ok") is False, out
    assert out.get("motion_line_count") == 0, out
    assert "motion" in out.get("error", "") and "cam_generate" in out.get("error", ""), out


def test_cam_post_reports_motion_on_a_real_program(monkeypatch):
    """The gate must not fire on a program that DOES cut: modal continuation lines
    (`X2.659 Y-25.781 Z9.994`) carry most of the motion in every dialect here."""
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    gcode = ("( J - S1)\nG21\nG90\n( T1 6mm )\nS14553 M3\n"
             "G0 X0.000 Y0.000 Z15.000\nX2.700 Y-26.250\nG1 Z10.000 F785.9\n"
             "X2.659 Y-25.781 Z9.994 F1309.8\nX2.537 Y-25.327 Z9.989\nM5\nM30\n")

    def fake_call(method, params=None):
        if method == "cam_post":
            return {"ok": True, "gcode": gcode, "line_count": 12, "tool_changes": 1,
                    "est_time_s": 250.6, "warnings": [], "error": ""}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    mcp = FastMCP("test-cam-post-real")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_post", {"dialect": "grbl"})
    assert out.get("ok") is True, out
    assert out.get("motion_line_count") == 5, out
    assert "5 motion line(s)" in out.get("changes", ""), out


def test_cam_add_operation_rejects_empty_geometry(monkeypatch):
    """cam_extract returns `{"regions": []}` whenever the section Z misses the part, and
    the operation used to be accepted with a cheerful "pocket2d operation added" — the
    failure only surfaced two calls later as a header-only posted program."""
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        raise AssertionError(f"engine must not be touched, got {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    mcp = FastMCP("test-cam-empty-geo")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_add_operation",
                      {"type": "pocket2d", "tool_number": 1, "geometry": {"regions": []}})
    assert out.get("ok") is False, out
    assert "regions is empty" in out.get("error", ""), out
    assert "cam_extract" in out.get("error", ""), out


def test_cam_generate_digests_the_moves_and_names_an_empty_result(monkeypatch):
    """Measured live 2026-07-30: ONE pocket2d on a 100x60x20 block returned 894,116 chars
    of per-move JSON from cam_generate — bigger than most context windows, and nothing a
    caller can act on that the digest does not carry."""
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    moves = ([{"kind": "rapid", "to": [0, 0, 15]}]
             + [{"kind": "cut", "to": [i, 0, 2]} for i in range(500)])

    def fake_call(method, params=None):
        if method == "cam_generate":
            return {"stats": {"count": 1}, "warnings": [],
                    "toolpaths": [{"op": "op1", "cut_length": 9047.6, "moves": moves}]}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    mcp = FastMCP("test-cam-generate")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_generate", {})
    tp = out["toolpaths"][0]
    assert "moves" not in tp, tp
    assert tp["move_count"] == 501 and tp["moves_by_kind"] == {"cut": 500, "rapid": 1}, tp
    assert tp["cut_length"] == 9047.6, tp
    assert "501 move(s)" in out.get("changes", ""), out


def test_cam_generate_says_so_when_nothing_was_generated(monkeypatch):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    def fake_call(method, params=None):
        if method == "cam_generate":
            return {"stats": {"count": 0}, "toolpaths": [],
                    "warnings": [{"op": "op1", "error": "pocket: no regions"}]}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    mcp = FastMCP("test-cam-generate-empty")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_generate", {})
    changes = out.get("changes", "")
    assert "NO toolpaths" in changes and "pocket: no regions" in changes, changes


def test_cam_add_setup_resolves_the_default_offset_stock_against_the_part(monkeypatch):
    """core/cam/stock_sim.cpp 181-190 resolves box_offset from min/max, which a standalone
    CAM job never has. Measured live 2026-07-30: a setup built with this tool's OWN default
    stock simulated as {ok:false, error:"stock has zero extent", stock_volume_mm3:0}, while
    the same job with an explicit box gave stock 120000.0 / removed 41936.0."""
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    seen: dict = {}

    def fake_call(method, params=None):
        if method == "export_file":
            return {"ok": True}
        if method == "cam_extract":
            return {"stock": {"box": {"min": [-50.0, -30.0, -10.0],
                                      "max": [50.0, 30.0, 10.0]}}}
        if method == "cam_apply_ops":
            seen["stock"] = params["ops"][0]["setup"]["stock"]
            return {"revision": 2, "id_map": {"_s": "su1"},
                    "tree": {"tools": [], "setups": [{"id": "su1", "operations": []}]}}
        if method in ("material_cam_id", "material_resolve_key"):
            return {"result": "aluminum"}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    monkeypatch.setitem(ct._CUR, "doc_id", "part-1")
    mcp = FastMCP("test-cam-setup-stock")
    ct.register_cam_tools(mcp)

    out = _call_tool(mcp, "cam_add_setup", {"name": "S1"})
    assert out.get("setup_id") == "su1", out
    # off_ztop defaults to 1.0, so the blank sits 1 mm proud of the part's top face.
    assert seen["stock"]["type"] == "box", seen
    assert seen["stock"]["min"] == [-50.0, -30.0, -10.0], seen
    assert seen["stock"]["max"] == [50.0, 30.0, 11.0], seen
    assert "bbox" in out.get("stock_note", ""), out


def test_cam_add_setup_keeps_an_explicit_box_stock_untouched(monkeypatch):
    from services.mcp import cam_tools as ct
    from mcp.server.fastmcp import FastMCP

    seen: dict = {}

    def fake_call(method, params=None):
        if method == "cam_apply_ops":
            seen["stock"] = params["ops"][0]["setup"]["stock"]
            return {"revision": 2, "id_map": {"_s": "su1"},
                    "tree": {"tools": [], "setups": [{"id": "su1", "operations": []}]}}
        if method in ("material_cam_id", "material_resolve_key"):
            return {"result": "aluminum"}
        raise AssertionError(f"engine must not be measured, got {method}")

    monkeypatch.setattr(ct.cb, "call", fake_call)
    monkeypatch.setitem(ct._CAM, "doc_id", "doc-1")
    mcp = FastMCP("test-cam-setup-explicit-stock")
    ct.register_cam_tools(mcp)

    _call_tool(mcp, "cam_add_setup",
               {"name": "S1", "stock": {"type": "box", "min": [0, 0, 0], "max": [10, 10, 5]}})
    assert seen["stock"]["min"] == [0, 0, 0] and seen["stock"]["max"] == [10, 10, 5], seen


if __name__ == "__main__":
    sys.exit(main())
