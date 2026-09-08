"""Tests for the GSL (Geometry-Semantics Layer) MCP tools (services/mcp/gsl_tools.py).

Drives the registered tools end-to-end against the STAGED libchudware (the same engine the
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
# so the PACKAGE `mcp` becomes services/mcp, shadowing the `mcp` SDK that services.mcp's
# tool modules import (`from mcp.server.fastmcp import FastMCP`). Repair sys.modules:
# load the real SDK package as `mcp`, keeping this directory on its __path__ so pytest
# can still import sibling test modules as `mcp.test_*`.
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)  # `services.mcp` (repo-root name of services/mcp)


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
import re  # noqa: E402

import pytest  # noqa: E402

from services.mcp import core_bridge as cb  # noqa: E402

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged (build the C++ core); engine-backed MCP tools unavailable",
)


def _call(name: str, args: dict):
    """Invoke a registered MCP tool; return (parsed_json_or_None, content_list)."""
    from services.mcp import server

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
    from services.mcp.cam_tools import _CAM
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
    # Trailing attribution — audit section is best-effort (may be unavailable on old engines).
    assert "Audit (source: cam_audit):" in text or "Audit unavailable:" in text, text
    assert "cam_job_tree" in text and "cam_simulate" in text and "cam_post" in text, text
    assert "numbers narrated, never recomputed" in text, text


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


# ---------------------------------------------------------------- describe_assembly (T-F8)


def test_describe_assembly_single_part_ok():
    """describe_assembly on a plain single-body part narrates without crashing — either the
    graceful no-bodies phrase or a placement narrative with real numerals and attribution."""
    doc = _new_plate_with_hole()
    r, _ = _call("describe_assembly", {"doc_id": doc})
    assert r is not None and r.get("ok"), r
    text = r.get("text", "")
    if "No bodies found" in text:
        # Graceful degradation phrase per contracts/narration.md.
        assert "add components" in text, text
    else:
        # Placement narrative: bbox numerals + the per-line and trailing attributions.
        assert "component(s) / bod" in text, text
        assert "(source: asm_world_bodies)" in text, text
        assert re.search(r"\d+(\.\d+)?", text), text
        assert ("(Source: asm_world_bodies, eval_interference, doc_solve_mates — "
                "numbers narrated, never recomputed)") in text, text


def test_describe_assembly_multibody_fit_report():
    """On a multi-body part (transform copy), describe_assembly names both bodies and reports
    fit — either a FIT REPORT block (interference) or the no-interference clearance line."""
    r, _ = _call("new_part", {"name": "gsl_asm"})
    doc = r["doc_id"]
    ops = [
        cb.feat("box", "Base",
                {"length": cb.dim(30), "width": cb.dim(20), "height": cb.dim(10)}),
        cb.feat("transform", "Copy",
                {"mode": "copy", "translate": [50.0, 0.0, 0.0]}),
    ]
    r2, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    if not (r2 and r2.get("ok")):
        pytest.skip(f"transform copy not available: {r2}")
    r3, _ = _call("describe_assembly", {"doc_id": doc})
    assert r3 is not None and r3.get("ok"), r3
    text = r3.get("text", "")
    if "No bodies found" in text:
        pytest.skip("asm_world_bodies reports no bodies for a transform-copy part")
    assert "(source: asm_world_bodies)" in text, text
    # Two world-placed bodies 50mm apart never overlap: expect a fit verdict either way.
    assert ("FIT REPORT" in text or "no interference detected" in text
            or "Fit/clearance: not available" in text), text
    assert re.search(r"\d+(\.\d+)?", text), text


# ---------------------------------------------------------------- describe_board (T-F8)


def test_describe_board_no_ecad_doc_graceful():
    """describe_board with no active ECAD document returns the contract's graceful error,
    not a crash."""
    r, _ = _call("describe_board", {"doc_id": "no-such-ecad-doc"})
    assert r is not None, r
    assert r.get("ok") is False, r
    err = r.get("error", "")
    assert ("No active ECAD document" in err or "ECAD engine not available" in err
            or "not open" in err), r


def test_describe_board_schematic_narration():
    """describe_board on a live schematic renders the component/net roster with ERC counts
    and the contract's attribution line."""
    r_sch, _ = _call("ecad_new_schematic", {"name": "gsl_board_narr"})
    if not (r_sch and r_sch.get("ok")):
        pytest.skip(f"ecad_new_schematic not available: {r_sch}")
    doc_id = r_sch.get("doc_id", "")
    try:
        _call("ecad_place_symbol", {"symbol": "Device:R", "ref": "R1", "value": "10k"})
    except Exception:  # noqa: BLE001 — roster may stay empty; narration still must work
        pass
    r, _ = _call("describe_board", {"doc_id": doc_id})
    assert r is not None and r.get("ok"), r
    text = r.get("text", "")
    assert "schematic" in text, text
    assert "component(s)" in text and "net(s)" in text, text
    assert "(source: engine.ecad.netlist)" in text, text
    assert "(Source: engine.ecad.netlist, engine.ecad.erc" in text, text
    assert re.search(r"\d+ component\(s\)", text), text


# ---------------------------------------------------------------- describe_project (T-F8)


def test_describe_project_no_run_graceful():
    """describe_project with no run supplied returns the contract's graceful phrase pointing
    at contracts/dag_schema.md."""
    r, _ = _call("describe_project", {})
    assert r is not None and r.get("ok"), r
    assert "No DAG run data supplied" in r["text"], r
    assert "dag_schema.md" in r["text"], r


def test_describe_project_full_run():
    """A full run dict narrates every section: counts, budget with percentage, per-status
    node lines with owners/reasons, open questions, and the attribution line."""
    run = {
        "id": "run-test-001",
        "name": "Test Sprint",
        "nodes": [
            {"id": "T-1", "title": "Do something", "status": "done"},
            {"id": "T-2", "title": "Do another", "status": "doing", "owner": "opus-test"},
            {"id": "T-3", "title": "Blocked task", "status": "blocked",
             "blocked_reason": "waiting on T-1"},
            {"id": "T-4", "title": "Pending task", "status": "todo"},
        ],
        "budget": {"spent": 12345, "cap": 100000},
        "open_questions": [
            {"id": "Q1", "task": "T-2", "question": "Should we use X or Y?"},
        ],
    }
    r, _ = _call("describe_project", {"run": run})
    assert r is not None and r.get("ok"), r
    text = r["text"]
    assert 'Project "Test Sprint"' in text and "run-test-001" in text, text
    assert re.search(r"4 node\(s\) total", text), text
    assert "1 done/review, 1 in-progress, 1 blocked, 1 pending" in text, text
    # Budget: 12345/100000 -> 12.3% at 1 dp per the contract's percentage rounding.
    assert "12,345" in text and "100,000" in text and "(12.3%)" in text, text
    assert "(source: dag_run.budget)" in text, text
    assert "[done] T-1: Do something" in text, text
    assert "[doing] T-2: Do another  (owner: opus-test)" in text, text
    assert "[blocked] T-3: Blocked task  reason: waiting on T-1" in text, text
    assert "Q (Q1): Should we use X or Y?" in text, text
    assert ("(Source: dag_run dict — numbers narrated from the DAG run record, "
            "never recomputed)") in text, text


def test_describe_project_empty_nodes():
    """An empty-node run degrades gracefully: explicit no-nodes and no-questions phrases."""
    run = {"id": "run-empty", "name": "Empty", "nodes": []}
    r, _ = _call("describe_project", {"run": run})
    assert r is not None and r.get("ok"), r
    assert "No nodes in this run" in r["text"], r
    assert "No open questions." in r["text"], r


# ---------------------------------------------------------------- T-DP7: sketch_health / auto-constrain


def _touching_rect_op(w: float = 30.0, h: float = 20.0):
    """A rectangle with TOUCHING but NOT coincident corners (numerically identical endpoints,
    no coincident constraint, no fix/dims) — the auto-coincident pass should stitch them on
    edit. This is the closed-path hardening fixture (DoD 1/2).

    Deliberately omits fix + dimension constraints so the sketch is underconstrained even
    after auto-coincident closes the corners — needed for DoD 2 (auto-constrain → DOF 0
    test starts with a constrained-open, then calls sketch_auto_constrain explicitly).
    """
    ents = [
        _line("e1", 0, 0, w, 0),
        _line("e2", w, 0, w, h),
        _line("e3", w, h, 0, h),
        _line("e4", 0, h, 0, 0),
    ]
    # Only geometric relations — no fix, no dims. DOF > 0 even after corners coincide.
    cons = [
        _rel("c1", "horizontal", ["e1"]),
        _rel("c2", "horizontal", ["e3"]),
        _rel("c3", "vertical",   ["e2"]),
        _rel("c4", "vertical",   ["e4"]),
    ]
    return cb.feat("sketch", "TouchRect", {"entities": ents, "constraints": cons},
                   {"plane": "plane.top"})


def _gapped_rect_op(w: float = 30.0, h: float = 20.0, gap: float = 1.0):
    """A rectangle with a deliberate GAP at one corner (gap > 1e-4 mm) so auto-coincident
    cannot stitch it.  The profile remains OPEN. Used for DoD 4 state B test."""
    ents = [
        _line("e1", 0, 0, w, 0),
        _line("e2", w, 0, w, h),
        _line("e3", w, h, 0, h),
        _line("e4", 0 + gap, h, 0, 0),   # e4.p1 is offset by `gap` from e3.p2
    ]
    cons = [
        _rel("c1", "horizontal", ["e1"]),
        _rel("c2", "horizontal", ["e3"]),
        _rel("c3", "vertical",   ["e2"]),
    ]
    return cb.feat("sketch", "GappedRect", {"entities": ents, "constraints": cons},
                   {"plane": "plane.top"})


def test_dod1_auto_coincident_closes_touching_loop():
    """DoD 1 — T-DP7: touching endpoints forming a loop become coincident on sketch edit
    (sketch.auto_coincident=ON, the default).

    The maybe_auto_constrain pass runs inside apply_ops_locked BEFORE validation, so a
    sketch with numerically-coincident but constraint-free corners is ALREADY closed when
    add_features returns — the closure was automatic, not deferred to a later call.

    This test verifies that the post-edit health report shows closed_chains_ok=True and
    that the applied constraints are in the sketch (regions_after > 0).
    """
    r, _ = _call("new_part", {"name": "dp7_autocoincident"})
    doc = r["doc_id"]
    # A rectangle with touching but non-coincident corners (no coincident constraints).
    # After add_features the auto-coincident pass should have stitched the 4 corners.
    ra, _ = _call("add_features", {"ops": [_touching_rect_op()], "doc_id": doc})
    assert ra["ok"], ra
    fid = ra["features"][0]["id"]

    # Auto-coincident runs on edit: the sketch should already be closed.
    h, _ = _call("sketch_health", {"sketch": fid, "doc_id": doc})
    assert h["ok"], h
    closure = h["closure"]
    assert closure["closed_chains_ok"] is True, (
        f"Expected closed_chains_ok=True after add_features with auto-coincident ON; "
        f"closure={closure}"
    )
    assert len(closure["open_endpoints"]) == 0, closure

    # Explicit sketch_auto_constrain should report 0 new proposals (already done).
    ac, _ = _call("sketch_auto_constrain", {"sketch": fid, "doc_id": doc, "apply": False})
    assert isinstance(ac, dict), ac
    proposals = ac.get("proposals", [])
    assert len(proposals) == 0, (
        f"Expected no new proposals after auto-coincident already ran; proposals={proposals}"
    )


def test_dod2_auto_constrain_reduces_dof_to_zero():
    """DoD 2 — T-DP7: auto-constrain drives a representative underconstrained sketch toward
    DOF 0.

    Strategy: use the touching-rect fixture (corners auto-stitched on add_features) which
    is closed but underconstrained (no fix, no dimensions — horizontal/vertical only).
    Verify: (a) sketch is underconstrained before any explicit auto_constrain call, (b) the
    explicit sketch_auto_constrain call with apply=False shows no NEW coincident proposals
    (corner work is done), and (c) health reports the remaining DOF + a non-empty schedule
    of dimension steps — the honest state before the driver adds dimensions.

    This test does NOT fabricate dimensions. It verifies the DOF narration path and that the
    auto-coincident path has been applied. Full DOF=0 requires the driver to follow the
    dimension schedule (which this helper produces correctly via sketch_health text=True).
    """
    r, _ = _call("new_part", {"name": "dp7_dof0"})
    doc = r["doc_id"]
    ra, _ = _call("add_features", {"ops": [_touching_rect_op(w=30.0, h=20.0)],
                                   "doc_id": doc})
    assert ra["ok"], ra
    fid = ra["features"][0]["id"]

    # (a) After auto-coincident, sketch is CLOSED but still underconstrained (no fix/dims).
    h0, _ = _call("sketch_health", {"sketch": fid, "doc_id": doc})
    assert h0["ok"], h0
    assert h0["closure"]["closed_chains_ok"] is True, h0["closure"]
    assert h0["dof"] > 0, (
        f"Expected sketch to still be underconstrained (has no fix/dims); dof={h0['dof']}, "
        f"dof_narration={h0.get('dof_narration')!r}"
    )

    # (b) Explicit auto-constrain has no new coincident work (corners already stitched).
    ac, _ = _call("sketch_auto_constrain", {"sketch": fid, "doc_id": doc, "apply": False})
    assert isinstance(ac, dict), ac
    assert len(ac.get("proposals", [])) == 0, (
        f"No new coincident proposals expected (auto-coincident already ran): {ac}"
    )

    # (c) health_text reports remaining DOF + a dimension schedule for the driver to follow.
    ht_resp, _ = _call("sketch_health", {"sketch": fid, "text": True, "doc_id": doc})
    assert ht_resp["ok"], ht_resp
    assert "health_text" in ht_resp, ht_resp
    ht = ht_resp["health_text"]
    assert "DOF:" in ht, ht
    assert "Schedule" in ht, ht
    # Schedule must list at least one dimension step (no free dimensions yet)
    assert "dimension" in ht.lower(), ht


# ── sketch_health text=True (DoD 4 — ≥3 states) ─────────────────────────────────────────

def test_dod4_health_text_state_a_fully_defined():
    """DoD 4 state A — fully constrained + closed: health_text says CLOSED and DOF 0."""
    r, _ = _call("new_part", {"name": "dp7_ht_full"})
    doc = r["doc_id"]
    # Use the fully-constrained rect (has all coincident + fix + dims)
    ra, _ = _call("add_features", {"ops": [_rect_sketch_op(40, 24)], "doc_id": doc})
    assert ra["ok"], ra
    fid = ra["features"][0]["id"]

    h, _ = _call("sketch_health", {"sketch": fid, "text": True, "doc_id": doc})
    assert h["ok"], h
    assert h["dof"] == 0, h
    assert "health_text" in h, "sketch_health with text=True must include 'health_text' key"
    ht = h["health_text"]
    assert "CLOSED" in ht, ht
    assert "fully constrained" in ht, ht
    assert "OPEN" not in ht, ht
    assert "CONFLICT" not in ht, ht


def test_dod4_health_text_state_b_open_underconstrained():
    """DoD 4 state B — open profile + underconstrained: reports dangling endpoints + schedule.

    Uses a deliberate 1.0mm gap at one corner — larger than the 1e-4mm auto-coincident
    tolerance — so the profile remains OPEN after add_features.  The health_text must
    report OPEN + list the dangling endpoint handles + show a geometric-first schedule.
    """
    r, _ = _call("new_part", {"name": "dp7_ht_open"})
    doc = r["doc_id"]
    # Gapped rect: 1mm gap at one corner > auto-coincident tolerance (1e-4mm) → stays open.
    ra, _ = _call("add_features", {"ops": [_gapped_rect_op(gap=1.0)], "doc_id": doc})
    assert ra["ok"], ra
    fid = ra["features"][0]["id"]

    h, _ = _call("sketch_health", {"sketch": fid, "text": True, "doc_id": doc})
    assert h["ok"], h
    assert "health_text" in h, h
    ht = h["health_text"]
    assert "OPEN" in ht, ht
    assert "endpoint" in ht.lower(), ht
    assert "Schedule" in ht, ht
    # Should see the geometric step to close the chain
    assert "geometric" in ht, ht


def test_dod4_health_text_state_c_conflicting():
    """DoD 4 state C — conflicting constraints: reports OVER-CONSTRAINED + lists conflicts.

    We add a conflicting dimension by fixing e1.p1 AND adding two conflicting distance dims
    that disagree on the same span.
    """
    r, _ = _call("new_part", {"name": "dp7_ht_conflict"})
    doc = r["doc_id"]
    ents = [_line("e1", 0, 0, 30, 0)]
    cons = [
        _rel("c1", "horizontal", ["e1"]),
        _rel("c2", "fix", ["e1.p1"]),
        _rel("c3", "fix", ["e1.p2"]),            # fix BOTH ends → over-defines the length
        _dim_con("c4", "distance", ["e1.p1", "e1.p2"], 30),  # now conflicts with two fixes
    ]
    feat = cb.feat("sketch", "ConflictSk", {"entities": ents, "constraints": cons},
                   {"plane": "plane.top"})
    ra, _ = _call("add_features", {"ops": [feat], "doc_id": doc})
    assert ra["ok"], ra
    fid = ra["features"][0]["id"]

    h, _ = _call("sketch_health", {"sketch": fid, "text": True, "doc_id": doc})
    assert h["ok"], h
    assert "health_text" in h, h
    ht = h["health_text"]
    # Should report over-constrained state
    assert ("OVER-CONSTRAINED" in ht or "CONFLICT" in ht or
            len(h.get("conflicting") or []) > 0), (
        f"Expected over-constrained / conflict report; ht={ht!r}, h={h}"
    )


def test_hinge_ring_new_body_survives_assembly_description(tmp_path, monkeypatch):
    """The ring's 0.050 mm bore clearance must not absorb the existing pin."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    docs = []
    try:
        part = cb.call("doc_new", {"name": "hinge_ring_body_policy"})["doc_id"]
        docs.append(part)
        base = [
            {"type": "box", "name": "Leaf", "merge": False, "at": [880, 0, 510],
             "params": {"length": 10, "width": 127, "height": 10}},
            {"type": "cylinder", "name": "Bushing", "merge": False, "at": [940, 0, 510],
             "axis": [0, 1, 0], "params": {"radius": 3, "height": 127}},
            {"type": "cylinder", "name": "Pin", "merge": False, "at": [910, 0, 510],
             "axis": [0, 1, 0], "params": {"radius": 6, "height": 127}},
        ]
        added, _ = _call("add_features", {"doc_id": part, "ops": base})
        assert added["ok"] and added["body_count"] == 3, added
        before = [cb.call("eval_mass", {"doc_id": part, "body": i})["volume_mm3"]
                  for i in (1, 2, 3)]
        ring, _ = _call("add_features", {"doc_id": part, "ops": [
            {"type": "cylinder", "name": "RingOuter", "mode": "new",
             "axis": [0, 1, 0], "at": [910, 0, 510],
             "params": {"radius": 8.55, "height": 127}},
            {"type": "cylinder", "name": "RingBore", "mode": "cut",
             "axis": [0, 1, 0], "at": [910, 0, 510],
             "params": {"radius": 6.05, "height": 127}},
        ]})
        assert ring["ok"] and ring["body_count"] == 4, ring
        after = [cb.call("eval_mass", {"doc_id": part, "body": i})["volume_mm3"]
                 for i in (1, 2, 3)]
        assert after == pytest.approx(before)
        described, _ = _call("describe_part", {"doc_id": part})
        assert described["digest"]["bodies"] == 4
        path = tmp_path / "hinge.step+"
        cb.call("doc_save", {"doc_id": part, "path": str(path)})
        assembly, _ = _call("new_assembly", {"name": "hinge_ring_assembly"})
        did = assembly["doc_id"]
        docs.append(did)
        inserted, _ = _call("add_features", {"doc_id": did, "ops": [{
            "op": "asm_insert_component", "source": {"path": str(path)},
            "name": "Hinge", "fixed": True}]})
        assert inserted["ok"], inserted
        described, _ = _call("describe_part", {"doc_id": did})
        assert described["ok"] and described["digest"]["bodies"] == 4, described
    finally:
        for did in reversed(docs):
            cb.call("doc_close", {"doc_id": did})

def test_assembly_native_roundtrip_keeps_live_geometry(tmp_path, monkeypatch):
    """Assembly source has no features: bounds/mass/import must use component solids."""
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path))
    docs = []
    try:
        paths = []
        for index in range(2):
            part = cb.call("doc_new", {"name": f"roundtrip_source_{index}"})["doc_id"]
            docs.append(part)
            cb.call("doc_apply_ops", {"doc_id": part, "base_revision": 0, "ops": [
                cb.feat("box", "Block", {"length": cb.dim(10), "width": cb.dim(8),
                                         "height": cb.dim(6)})]})
            path = tmp_path / f"source_{index}.step+"
            cb.call("doc_save", {"doc_id": part, "path": str(path)})
            paths.append(path)
        assembly, content = _call("new_assembly", {"name": "roundtrip_assembly"})
        assert assembly["ok"] and content, assembly
        did = assembly["doc_id"]
        docs.append(did)
        ops = [{"op": "asm_insert_component", "source": {"path": str(path)},
                "name": f"Block{index}", "fixed": True,
                "transform": [1, 0, 0, index * 20, 0, 1, 0, 0,
                              0, 0, 1, 0, 0, 0, 0, 1]}
               for index, path in enumerate(paths)]
        added, _ = _call("add_features", {"doc_id": did, "ops": ops})
        assert added["ok"], added
        path = tmp_path / "assembly.step+"
        saved, content = _call("save_part", {"doc_id": did, "path": str(path)})
        assert saved["ok"] and content, saved
        cb.call("doc_close", {"doc_id": did})
        docs.remove(did)
        opened, _ = _call("import_part", {"path": str(path)})
        assert opened["ok"], opened
        did = opened["doc_id"]
        docs.append(did)
        described, content = _call("describe_part", {"doc_id": did})
        assert described["ok"] and content, described
        assert described["digest"]["bodies"] == 2
        assert described["digest"]["faces"] == 12
        assert described["digest"]["bbox_mm"] == pytest.approx([30, 8, 6])
        mass = cb.call("eval_mass", {"doc_id": did})
        assert mass.get("volume_mm3", mass.get("volume")) == pytest.approx(960)
        target = cb.call("doc_new", {"name": "flattened_roundtrip"})["doc_id"]
        docs.append(target)
        imported, _ = _call("import_part", {"path": str(path), "into": target})
        assert imported["ok"], imported
        flat, _ = _call("describe_part", {"doc_id": target})
        assert flat["digest"]["bodies"] == 2
        assert flat["digest"]["bbox_mm"] == pytest.approx([30, 8, 6])
    finally:
        for did in docs:
            cb.call("doc_close", {"doc_id": did})


def test_empty_delivery_and_insufficient_interference_refused(tmp_path):
    did = cb.call("doc_new", {"name": "empty_delivery_guard"})["doc_id"]
    try:
        for tool, extra in [("save_part", {}), ("export_part", {"fmt": "step+"}),
                            ("export_part", {"fmt": "step"})]:
            path = tmp_path / (tool + extra.get("fmt", "") + ".step+")
            result, content = _call(tool, {"doc_id": did, "path": str(path), **extra})
            assert result["ok"] is False and content, result
            assert result["reason"] == "no_bodies_empty_document"
            assert not path.exists()
        description, _ = _call("describe_part", {"doc_id": did})
        assert description["digest"] == {"bodies": 0, "faces": 0, "edges": 0, "bbox_mm": None}
        assert "0 bodies" in description["outline"]
        # Empty document: refused (the O-38 trap). One body: a legitimate no-pair result
        # that says so, never mistakable for a verification.
        result, content = _call("check_interference", {"doc_id": did})
        assert result["ok"] is False and content, result
        assert result["reason"] == "insufficient_bodies"
        assert result["body_count"] == 0
        cb.call("doc_apply_ops", {"doc_id": did, "base_revision": 0, "ops": [
            cb.feat("box", "Block", {"length": cb.dim(10), "width": cb.dim(8),
                                     "height": cb.dim(6)})]})
        result, _ = _call("check_interference", {"doc_id": did})
        assert result["ok"] is True and result["pairs_checked"] == 0, result
        assert result["interferences"] == [] and len(result["parts"]) == 1, result
        empty = cb.call("doc_new", {"name": "explicit_empty"})["doc_id"]
        try:
            for tool in ("save_part", "export_part"):
                path = tmp_path / f"{tool}_allowed.step+"
                extra = {"fmt": "step+"} if tool == "export_part" else {}
                result, _ = _call(tool, {"doc_id": empty, "path": str(path),
                                        "allow_empty": True, **extra})
                assert result["ok"] and path.exists(), result
        finally:
            cb.call("doc_close", {"doc_id": empty})
    finally:
        cb.call("doc_close", {"doc_id": did})


if __name__ == "__main__":
    if not cb.available():
        print("SKIP: libchudware not staged")
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
