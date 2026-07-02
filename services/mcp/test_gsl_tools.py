"""Tests for the GSL (Geometry-Semantics Layer) MCP tools (services/mcp/gsl_tools.py).

Drives the registered tools end-to-end against the STAGED libaicad (the same engine the
app loads): describe_part's placement narrative, hover's progressive disclosure,
query_adjacency's face graph, describe_sketch's entity/DOF text, and describe_scene's
per-view visibility text. Skipped cleanly when the core library isn't built/staged.

Run:  python3 -m pytest services/mcp/test_gsl_tools.py -q
      python3 services/mcp/test_gsl_tools.py          # also works (prints PASS/FAIL)
"""
from __future__ import annotations

# --- pytest import-shadow repair --------------------------------------------------------
# Under `python3 -m pytest services/mcp/test_gsl_tools.py` pytest's default (prepend)
# import mode names this module `mcp.test_gsl_tools` — this directory has an __init__.py,
# so the PACKAGE `mcp` becomes services/mcp, shadowing the `mcp` SDK that mcp_server's
# tool modules import (`from mcp.server.fastmcp import FastMCP`). Repair sys.modules:
# load the real SDK package as `mcp`, keeping this directory on its __path__ so pytest
# can still import sibling test modules as `mcp.test_*`.
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)  # `mcp_server` (repo-root name of services/mcp)


def _unshadow_mcp_sdk() -> None:
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile or os.path.dirname(os.path.realpath(sfile)) != _HERE:
        return  # `mcp` is absent or already the real SDK — nothing to repair
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _HERE:
            spec = importlib.util.spec_from_file_location(
                "mcp", init, submodule_search_locations=[os.path.dirname(init), _HERE])
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module  # must be visible to the SDK's own imports
            spec.loader.exec_module(module)
            return
    raise ImportError("could not locate the real `mcp` SDK to un-shadow services/mcp")


_unshadow_mcp_sdk()

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

from mcp_server import core_bridge as cb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libaicad not staged (build the C++ core); engine-backed MCP tools unavailable",
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
    """60x40x10 plate with one d8 through-hole from the top face (2 features, 1 bore)."""
    r, _ = _call("new_part", {"name": "gsl_plate"})
    doc = r["doc_id"]
    ops = [
        cb.feat("box", "Plate",
                {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)}),
        cb.feat("hole", "Bore",
                {"style": "simple", "end": "through_all", "diameter": cb.dim(8)},
                {"face": {"of": "face", "side": "top"}}),
    ]
    r, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert r["ok"], r
    return doc


# ---------------------------------------------------------------- describe_part (A3)


def test_describe_part_placement_narrative():
    """describe_part returns the outline with ONE placement line per feature: the reference
    side, the bbox offsets on the perpendicular axes, and what the feature touches."""
    doc = _new_plate_with_hole()
    r, _ = _call("describe_part", {"doc_id": doc})
    assert r["ok"], r
    outline = r["outline"]
    assert "Features (placement narrative, one line each):" in outline, outline
    # The box's placement line: "f1 Plate [box] (N face(s)): on <side> face of f2,
    # centered X mm from -A edge / Y mm from -B edge".
    assert "Plate [box]" in outline and "Bore [hole]" in outline, outline
    assert "face(s)): on " in outline, outline
    assert "centered" in outline and "mm from -" in outline, outline
    # The hole cut touches the box it cut into (adjacency-bridged "of f1").
    assert "of f1" in outline, outline
    # Digest carries the structured placement for each feature group.
    fgs = r["digest"]["feature_groups"]
    assert len(fgs) == 2, fgs
    for fg in fgs:
        assert fg.get("placement"), fg
        assert fg["placement"].get("offsets"), fg


def test_describe_part_lists_bores_and_caps():
    """The outline names the bore (with radius) and the flat faces by side, each with a
    stable sid handle."""
    doc = _new_plate_with_hole()
    r, _ = _call("describe_part", {"doc_id": doc})
    assert r["ok"], r
    bores = r["digest"]["bores"]
    assert len(bores) == 1 and bores[0]["radius"] == 4.0, bores
    assert bores[0]["sid"], bores
    assert f'{bores[0]["sid"]}(r4.0)' in r["outline"], r["outline"]
    sides = {c["side"] for c in r["digest"]["planar_caps"]}
    assert {"top", "bottom", "left", "right", "front", "back"} <= sides, sides


# ---------------------------------------------------------------- hover


def test_hover_expands_bore_by_sid():
    """hover(sid) returns the full semantic card for ONE face — progressive disclosure."""
    doc = _new_plate_with_hole()
    r, _ = _call("describe_part", {"doc_id": doc})
    sid = r["digest"]["bores"][0]["sid"]
    h, _ = _call("hover", {"sid": sid, "doc_id": doc})
    assert h["ok"], h
    m = h["match"]
    assert m["sid"] == sid, m
    assert m["surf"] == "cylinder" and m["concavity"] == "bore", m
    assert m["radius"] == 4.0, m
    assert isinstance(m.get("normal"), list) and len(m["normal"]) == 3, m


def test_hover_unknown_sid_reports_not_found():
    doc = _new_plate_with_hole()
    h, _ = _call("hover", {"sid": "Fdeadbeef", "doc_id": doc})
    assert h["ok"] is False, h
    assert "no face/edge with sid" in h["error"], h


# ---------------------------------------------------------------- query_adjacency (W3)


def test_query_adjacency_face_graph():
    """The adjacency graph reports every face with neighbors, and each shared edge with a
    convex/concave verdict — the relationships the flat face dump never carried."""
    doc = _new_plate_with_hole()
    r, _ = _call("query_adjacency", {"doc_id": doc})
    faces = r["faces"]
    edges = r["edges"]
    assert len(faces) == 7, faces  # 6 box sides + 1 bore wall
    for f in faces:
        assert "ord" in f and "surf" in f, f
        assert isinstance(f.get("neighbors"), list) and f["neighbors"], f
    n = len(faces)
    for e in edges:
        a, b = e["between"]
        # NOTE a == b is legitimate: the bore wall's cylindrical SEAM edge borders the
        # same face on both sides.
        assert 0 <= a < n and 0 <= b < n, e
        assert isinstance(e.get("convex"), bool), e
        assert e.get("length", 0) > 0, e
    # A closed cylinder wall carries its seam as a self-adjacent edge.
    assert any(e["between"][0] == e["between"][1] for e in edges), edges
    # The plate's outer box edges include convex outside corners between DIFFERENT faces.
    assert any(e["convex"] and e["between"][0] != e["between"][1] for e in edges), edges


# ---------------------------------------------------------------- describe_sketch (A1)


def _line(eid, x1, y1, x2, y2):
    return {"id": eid, "type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}


def _rel(cid, ctype, refs):
    return {"id": cid, "type": ctype, "refs": refs}


def _dim_con(cid, ctype, refs, value):
    return {"id": cid, "type": ctype, "refs": refs,
            "value": {"value": value, "expr": None}, "driven": False}


def _rect_sketch_op(w, h):
    """A fully-defined w x h rectangle sketch (canonical params/refs shape) on plane.top."""
    ents = [
        _line("e1", 0, 0, w, 0), _line("e2", w, 0, w, h),
        _line("e3", w, h, 0, h), _line("e4", 0, h, 0, 0),
    ]
    cons = [
        _rel("c1", "coincident", ["e1.p2", "e2.p1"]),
        _rel("c2", "coincident", ["e2.p2", "e3.p1"]),
        _rel("c3", "coincident", ["e3.p2", "e4.p1"]),
        _rel("c4", "coincident", ["e4.p2", "e1.p1"]),
        _rel("c5", "horizontal", ["e1"]),
        _rel("c6", "horizontal", ["e3"]),
        _rel("c7", "vertical", ["e2"]),
        _rel("c8", "vertical", ["e4"]),
        _rel("c9", "fix", ["e1.p1"]),
        _dim_con("c10", "distance", ["e1.p1", "e1.p2"], w),
        _dim_con("c11", "distance", ["e2.p1", "e2.p2"], h),
    ]
    return cb.feat("sketch", "RectSk", {"entities": ents, "constraints": cons},
                   {"plane": "plane.top"})


def test_describe_sketch_entities_and_dof_verdict():
    """describe_sketch composes the plane, one line per entity (by id, with solved coords
    and status), the constraint roster and the DOF verdict as text."""
    r, _ = _call("new_part", {"name": "gsl_sketch"})
    doc = r["doc_id"]
    ra, _ = _call("add_features", {"ops": [_rect_sketch_op(40, 24)], "doc_id": doc})
    assert ra["ok"], ra
    fid = ra["features"][0]["id"]
    d, _ = _call("describe_sketch", {"sketch": fid, "doc_id": doc})
    assert d["ok"], d
    text = d["text"]
    # Entity ids each get a line with type + solved geometry + per-entity status.
    for eid in ("e1", "e2", "e3", "e4"):
        assert f"{eid} line" in text, text
    assert "Entities (4):" in text, text
    assert "[full]" in text, text
    # Constraint roster: grouped relations + individually-listed driving dims.
    assert "coincident x4" in text and "driving dims:" in text, text
    assert "c10 distance=40" in text and "c11 distance=24" in text, text
    # One closed region, and the check_sketch-style DOF verdict.
    assert "Regions: 1 closed loop(s)" in text, text
    assert "Fully defined (dof 0)" in text, text
    assert d["digest"]["fully_defined"] is True, d["digest"]
    assert d["digest"]["dof"] == 0, d["digest"]


def test_describe_sketch_unknown_id_reports_not_found():
    r, _ = _call("new_part", {"name": "gsl_sketch2"})
    doc = r["doc_id"]
    d, _ = _call("describe_sketch", {"sketch": "s999", "doc_id": doc})
    assert d["ok"] is False, d
    assert "no sketch" in d["error"], d


# ---------------------------------------------------------------- describe_scene (A2)


def test_describe_scene_visibility_and_occlusion_disclaimer():
    """describe_scene lists, per view, the faces whose SIGNED normal points at the camera,
    and states the occlusion limitation explicitly.

    The bore wall's signed normal on this plate points -Z (hover reports side "bottom"
    for the concave bore face), so the deterministic dot-product filter lists the bore
    under the BOTTOM view; the top view shows the top cap. Occlusion is not modeled and
    the text must say so.
    """
    doc = _new_plate_with_hole()
    dp, _ = _call("describe_part", {"doc_id": doc})
    bore_sid = dp["digest"]["bores"][0]["sid"]
    top_sid = next(c["sid"] for c in dp["digest"]["planar_caps"] if c["side"] == "top")

    r, _ = _call("describe_scene", {"views": ["top", "bottom"], "doc_id": doc})
    assert r["ok"], r
    text = r["text"]
    assert "occlusion not modeled" in text, text
    assert "top view" in text and "bottom view" in text, text
    views = r["views"]
    assert top_sid in views["top"]["visible"], views
    # The bore's signed normal faces the bottom camera -> listed under bottom, not top.
    assert bore_sid in views["bottom"]["visible"], views
    assert bore_sid not in views["top"]["visible"], views
    # Each described view carries its camera direction + the part narrative up top.
    assert views["top"]["dir"] == [0.0, 0.0, 1.0], views
    assert "Feature placement:" in text, text


def test_describe_scene_default_views():
    doc = _new_plate_with_hole()
    r, _ = _call("describe_scene", {"doc_id": doc})
    assert r["ok"], r
    assert set(r["views"]) == {"front", "right", "top", "iso"}, r["views"]


# ---------------------------------------------------------------- describe_toolpaths


def test_describe_toolpaths_no_job_graceful():
    """describe_toolpaths with no active CAM job returns a graceful error, not a crash."""
    from mcp_server.cam_tools import _CAM
    old = _CAM.get("doc_id")
    _CAM["doc_id"] = None
    try:
        r, _ = _call("describe_toolpaths", {})
        # Should return JSON with ok:False and a message, not raise
        assert r is not None
        assert r.get("ok") is False or "no cam job" in str(r).lower() or "No CAM job" in str(r)
    finally:
        _CAM["doc_id"] = old


def test_describe_toolpaths_narrates_job_sim_and_gcode():
    """describe_toolpaths narrates the job/setup/op hierarchy from cam_job_tree, the
    simulation numbers from cam_simulate, and the G-code digest from cam_post — every
    section present, every number attributed to its source tool."""
    rp, _ = _call("new_part", {"name": "cam_narr_plate"})
    doc = rp["doc_id"]
    ops = [cb.feat("box", "Plate",
                   {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)})]
    _call("add_features", {"ops": ops, "doc_id": doc})

    rj, _ = _call("cam_new_job", {"name": "TestJob", "post": "grbl"})
    assert rj.get("doc_id"), rj

    _call("cam_add_tool", {"number": 1, "type": "endmill_flat", "diameter": 6.0})
    rs, _ = _call("cam_add_setup", {"name": "Setup1", "machine": "mill3",
                                    "material": "aluminum"})
    ro, _ = _call("cam_add_operation", {"type": "face", "tool_number": 1,
                                        "setup_id": rs["setup_id"], "name": "Face1"})
    assert ro.get("op_id"), ro
    _call("cam_generate", {})

    r, _ = _call("describe_toolpaths", {})
    assert r["ok"], r
    text = r["text"]
    # Header from cam_job_tree (nested "tree" payload).
    assert 'CAM job "TestJob" — 1 setup(s), 1 operation(s) total' in text, text
    assert 'Setup "Setup1" [mill3] — 1 op(s)' in text, text
    # One op line: name [type]: Tn tool_type ødiam.
    assert 'Op "Face1" [face]: T1 endmill_flat ø6.0mm' in text, text
    # Simulation numbers attributed to cam_simulate (machined %, remaining stock, cycle).
    assert "Simulation: machined " in text and "(source: cam_simulate)" in text, text
    assert "remaining stock " in text and " mm3" in text, text
    assert "Gouges: " in text and "rapid-through-stock collisions: " in text, text
    # G-code digest attributed to cam_post: line count, bounds, feed range, tool changes.
    assert "G-code digest (source: cam_post): " in text, text
    assert " lines, bounds X[" in text and "] Y[" in text and "] Z[" in text, text
    assert " mm/min, " in text and " tool change(s)" in text, text
    # Trailing attribution line per contracts/narration.md.
    assert ("(Source: cam_job_tree, cam_simulate, cam_post — numbers narrated, "
            "never recomputed)") in text, text


# ---------------------------------------------------------------- describe_results


def test_describe_results_no_study_graceful():
    """describe_results with no simulation attached returns the contract's exact
    graceful-degradation phrase, not a crash or a silent omission."""
    doc = _new_plate_with_hole()
    r, _ = _call("describe_results", {"doc_id": doc})
    assert r["ok"], r
    assert "No simulation results attached — run run_fea_static or run_fea_modal first." \
        in r["text"], r


def _seed_static_study(doc: str) -> None:
    """Persist a static study through sim_study_add — the SAME RPC _persist_study uses
    after a real run_fea_static solve — so the narration is tested against a
    deterministic fixture with known numbers (no solver / mesh nondeterminism)."""
    cb.call("sim_study_add", {"doc_id": doc, "study": {
        "type": "fea_static", "name": "Static stress (steel)", "engine": "cpp",
        "material": {"name": "steel", "yield_mpa": 250.0, "E_mpa": 200000.0},
        "fixtures": ["bottom"], "loads": [{"face": "top", "force": [0, 0, -100]}],
        "mesh_fineness": 0.3,
        "results": {"max_von_mises_mpa": 12.34567, "max_disp_mm": 0.004321,
                    "min_fos": 15.0},
        "n_dof": 3456,
    }})


def test_describe_results_static_study_exact_slots():
    """Every static-study slot renders with the contract's fixed rounding and source
    attribution: stress 0.1 MPa, displacement 3 dp, FoS 2 dp, margin, DOF, and the
    fixture/load sid join against the live face layer."""
    doc = _new_plate_with_hole()
    _seed_static_study(doc)

    r, _ = _call("describe_results", {"doc_id": doc})
    assert r["ok"], r
    text = r["text"]
    assert 'FEA results — "Static stress (steel)" (fea_static)' in text, text
    assert "Material: steel (yield 250.0 MPa, source: run_fea_static material record)" \
        in text, text
    # Fixed rounding per contracts/narration.md: 12.34567 -> 12.3 MPa (0.1 MPa),
    # 0.004321 -> 0.004 mm (3 dp), FoS 2 dp, margin = 250.0 - 12.34567 -> 237.7 (0.1 MPa).
    assert "Max von Mises stress: 12.3 MPa  (source: run_fea_static)" in text, text
    assert "Max displacement: 0.004 mm  (source: run_fea_static)" in text, text
    assert "Min factor of safety: 15.00" in text and "holds" in text, text
    assert "Margin: 237.7 MPa above yield" in text, text
    assert "Mesh: 3456 DOF  (mesh_fineness 0.30, tet10 elements)" in text, text
    # Sid join: the stored fixture/load queries resolve to live face sids (F...).
    assert "Fixed at: face F" in text and "on bottom" in text, text
    assert "Load applied on: face F" in text and "on top" in text, text
    # The per-node field is not persisted — the output says so instead of guessing.
    assert "Max-stress node location: not stored in the study record" in text, text
    assert "Convergence note: tet10" in text, text
    assert ("(Source: sim_study_list — numbers narrated from the stored study, "
            "never recomputed)") in text, text


def test_describe_results_modal_study_and_named_lookup():
    """Modal studies narrate one 0.1 Hz line per mode; study_name selects a specific
    study when several are attached (default is the latest)."""
    doc = _new_plate_with_hole()
    _seed_static_study(doc)
    cb.call("sim_study_add", {"doc_id": doc, "study": {
        "type": "fea_modal", "name": "Modal (steel)", "engine": "cpp",
        "material": {"name": "steel", "yield_mpa": 250.0},
        "fixtures": ["bottom"], "n_modes": 3, "mesh_fineness": 0.5,
        "results": {"freqs_hz": [101.5, 202.25, 350.0], "n_modes": 3},
    }})

    # Default: the LATEST study (the modal one).
    r, _ = _call("describe_results", {"doc_id": doc})
    assert r["ok"], r
    text = r["text"]
    assert 'FEA results — "Modal (steel)" (fea_modal)' in text, text
    assert "Material: steel (yield 250.0 MPa, source: run_fea_modal material record)" \
        in text, text
    assert "Mode 1: 101.5 Hz  (source: run_fea_modal)" in text, text
    assert "Mode 2: 202.2 Hz  (source: run_fea_modal)" in text, text
    assert "Mode 3: 350.0 Hz  (source: run_fea_modal)" in text, text
    assert "rigid-body modes near 0 Hz" in text, text

    # Named lookup pulls the static study back.
    r2, _ = _call("describe_results", {"doc_id": doc, "study_name": "Static stress (steel)"})
    assert r2["ok"], r2
    assert "Max von Mises stress: 12.3 MPa" in r2["text"], r2["text"]

    # Unknown name: graceful error listing what IS attached.
    r3, _ = _call("describe_results", {"doc_id": doc, "study_name": "nope"})
    assert r3["ok"] is False and "No study named" in r3["error"], r3


if __name__ == "__main__":
    if not cb.available():
        print("SKIP: libaicad not staged")
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
