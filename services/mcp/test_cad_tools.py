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


# ---- task 06: sketch relations + check_sketch -----------------------------------------------
# Sketch entity/constraint shape mirrors the proven C++ DSL (core/tests/test_document.cpp::
# rect_entities): 4 lines closed by coincident + h/v relations + a fixed corner + 2 driving
# distance dims == a FULLY DEFINED rectangle (dof 0). Drop the dims -> under-defined.
def _line(eid, x1, y1, x2, y2):
    return {"id": eid, "type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}


def _rel(cid, ctype, refs):
    return {"id": cid, "type": ctype, "refs": refs}


def _dim(cid, ctype, refs, value):
    return {"id": cid, "type": ctype, "refs": refs, "value": {"value": value, "expr": None},
            "driven": False}


def _rect_sketch(fid, w, h, with_dims=True):
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
    ]
    if with_dims:
        cons += [
            _dim("c10", "distance", ["e1.p1", "e1.p2"], w),
            _dim("c11", "distance", ["e2.p1", "e2.p2"], h),
        ]
    return {"op": "add_feature", "feature": {
        "id": fid, "type": "sketch", "plane": "top",
        "entities": [
            _line("e1", 0, 0, w, 0), _line("e2", w, 0, w, h),
            _line("e3", w, h, 0, h), _line("e4", 0, h, 0, 0),
        ],
        "constraints": cons,
    }}


def test_check_sketch_fully_defined():
    r, _ = _call("new_part", {"name": "sk"})
    doc = r["doc_id"]
    _call("add_features", {"ops": [_rect_sketch("s1", 40, 24, with_dims=True)], "doc_id": doc})
    r, _ = _call("check_sketch", {"sketch": "s1", "doc_id": doc})
    assert r["ok"], r
    assert r["status"] == "full" and r["dof"] == 0, r
    assert r["fully_defined"] is True, r
    assert not r["conflicting"], r
    assert "Fully defined" in r["summary"], r


def test_check_sketch_under_defined():
    r, _ = _call("new_part", {"name": "sk2"})
    doc = r["doc_id"]
    _call("add_features", {"ops": [_rect_sketch("s1", 40, 24, with_dims=False)], "doc_id": doc})
    r, _ = _call("check_sketch", {"sketch": "s1", "doc_id": doc})
    assert r["ok"], r
    assert r["status"] == "under" and r["dof"] >= 1, r
    assert r["fully_defined"] is False, r
    assert "Under-defined" in r["summary"], r


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


def _find_image(x):
    if getattr(x, "type", "") == "image":
        return x
    if isinstance(x, (list, tuple)):
        for e in x:
            found = _find_image(e)
            if found is not None:
                return found
    return None


def test_render_part_custom_angle_returns_image():
    # Task 03: render_part now takes an on-request view (named or {azimuth,elevation}). Each yields
    # a real PNG, and the core echoes the resolved camera, proving the angle actually applied.
    doc = _new_plate_with_hole()
    from mcp_server import server

    for view in ("right", {"azimuth": 30, "elevation": 20}):
        result = asyncio.run(
            server.mcp.call_tool("render_part", {"doc_id": doc, "view": view}))
        item = _find_image(result)
        assert item is not None, f"no ImageContent for view={view!r} in {result!r}"
        assert getattr(item, "mimeType", "").startswith("image/"), item
        assert item.data, "empty image payload"

    # azimuth 90 / elevation 0 resolves to the same camera as the named "right" view.
    import os
    png = _tmp_png()
    r = cb.call("doc_render_view",
                {"doc_id": doc, "out_path": png, "view": {"azimuth": 90, "elevation": 0}})
    assert r.get("ok"), r
    assert os.path.exists(png)
    assert abs(r["dir"][0] - 1.0) < 1e-6 and abs(r["dir"][1]) < 1e-6, r  # looks down +X


def test_contact_sheet_returns_image():
    # Task 03: contact_sheet tiles several labelled views into one PNG.
    doc = _new_plate_with_hole()
    from mcp_server import server

    result = asyncio.run(server.mcp.call_tool("contact_sheet", {"doc_id": doc}))
    item = _find_image(result)
    assert item is not None, f"no ImageContent in {result!r}"
    assert getattr(item, "mimeType", "").startswith("image/"), item
    assert item.data, "empty image payload"

    # Explicit view list is honoured; the core reports the tiled view names.
    import os
    png = _tmp_png()
    r = cb.call("doc_render_contact_sheet",
                {"doc_id": doc, "out_path": png, "views": ["front", "top"]})
    assert r.get("ok") and os.path.exists(png), r
    assert [v.upper() for v in r["views"]] == ["FRONT", "TOP"], r


def _tmp_png():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    return path


def test_measure_part():
    doc = _new_plate_with_hole()
    r, _ = _call("measure_part", {"doc_id": doc})
    assert r["ok"]
    assert r["mass"]["volume_mm3"] > 0


def test_set_material_preset_and_explicit():
    # Task 04: the copilot/MCP can set a body's appearance. A preset name resolves to a colour +
    # finish; an explicit dict is honoured; the appearance is written to the doc and baked onto the
    # tessellated body (so render_part reflects it).
    doc = _new_plate_with_hole()

    r, _ = _call("set_material", {"material": "brass", "doc_id": doc})
    assert r["ok"], r
    assert r["bodies"] == ["b1"], r
    assert r["appearance"]["metallic"] > 0.5  # brass is metallic

    # The op landed on the doc and the tessellated body carries the brass colour/material.
    tree = cb.call("doc_tree", {"doc_id": doc})
    ap = (tree.get("tree") or {}).get("appearances", {})
    assert "b1" in ap, ap
    # The baked body colour/material is reported in the apply-ops tess metadata.
    info = cb.call("doc_apply_ops", {"doc_id": doc, "ops": []})
    b0 = info["tess"]["bodies"][0]
    assert b0["metallic"] > 0.5 and b0["color"][0] > 0.5, b0  # warm metallic

    # Explicit dict {color, roughness} also works.
    r2, _ = _call("set_material",
                  {"material": {"color": [0.1, 0.2, 0.3], "roughness": 0.2}, "doc_id": doc})
    assert r2["ok"], r2
    info2 = cb.call("doc_apply_ops", {"doc_id": doc, "ops": []})
    assert info2["tess"]["bodies"][0]["color"][2] > 0.25  # ~0.3 blue channel


def test_set_material_unknown_name_errors():
    doc = _new_plate_with_hole()
    r, _ = _call("set_material", {"material": "unobtanium", "doc_id": doc})
    assert not r.get("ok"), r  # _err envelope on an unknown preset


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


def test_save_part_native_stepplus_roundtrips():
    """save_part writes the NATIVE .step+ and it reopens with the feature tree intact."""
    import os
    import tempfile

    doc = _new_plate_with_hole()
    out = os.path.join(tempfile.mkdtemp(), "delivered")  # no extension -> tool adds .step+
    r, _ = _call("save_part", {"path": out, "doc_id": doc})
    assert r["ok"] and r["native"] and r["format"] == "step+", r
    assert r["path"].lower().endswith(".step+") and r["bytes"] > 0, r

    # Round-trip: reopening the native file restores an EDITABLE feature tree (box + hole),
    # which a flat STEP export would not carry. Drives the engine directly (no MCP open tool).
    opened = cb.call("doc_open", {"path": r["path"]})
    tree = cb.call("doc_tree", {"doc_id": opened["doc_id"]})
    feats = (tree.get("tree") or {}).get("features", [])
    types = {f.get("type") for f in feats}
    assert {"box", "hole"} <= types, (types, feats)
    os.unlink(r["path"])


def test_export_step_plus_alias_is_native():
    """export_part('step+') is accepted as an alias and routed to the native saver."""
    import os
    doc = _new_plate_with_hole()
    r, _ = _call("export_part", {"fmt": "step+", "doc_id": doc})
    assert r["ok"] and r.get("native") and r["format"] == "step+", r
    assert r["path"].lower().endswith(".step+") and r["exists"], r
    os.unlink(r["path"])


def test_drawing_overhaul_gdt_section_export():
    """The drawing overhaul end-to-end via the MCP tools: make a drawing with an auto half
    section, add a GD&T frame + datum + surface finish, render it, and export DXF (the GD&T
    strokes must be pooled into the export, not just the projected geometry)."""
    import os
    doc = _new_plate_with_hole()
    mk, _ = _call("make_drawing", {
        "doc_id": doc,
        "views": ["front", "top", "right", "iso"],
        "sections": [{"parent": "front", "axis": "vertical", "mode": "half"}],
        "title": {"title": "PLATE", "material": "AL 6061"},
    })
    assert mk["ok"], mk
    dwg = mk["drawing_doc_id"]
    assert dwg

    # The drawing has a section view with hatch (engine-computed).
    full = cb.call("drawing_get", {"doc_id": dwg})
    views = full["sheets"][0]["views"]
    base = next(v for v in views if v.get("type") == "base")
    sec = next((v for v in views if v.get("type") == "section"), None)
    assert sec is not None, "auto section not created"
    assert len(sec.get("section_faces", [])) > 0
    assert len(sec.get("hatch", [])) > 0
    bb = base["bbox"]
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2

    g, _ = _call("add_gdt", {
        "doc_id": dwg, "view_id": base["view_id"], "symbol": "position",
        "tolerance": "0.1", "diameter": True, "modifier": "mmc",
        "datums": ["A", "B"], "a": [cx, cy], "pos": [bb[2] + 15, bb[3]],
    })
    assert g["ok"], g
    d2, _ = _call("add_datum", {"doc_id": dwg, "view_id": base["view_id"], "label": "A",
                                "a": [bb[0], bb[1]], "pos": [bb[0], bb[1] - 12]})
    assert d2["ok"], d2
    sf, _ = _call("add_surface_finish", {"doc_id": dwg, "view_id": base["view_id"], "ra": "1.6",
                                         "a": [bb[2], cy], "pos": [bb[2] + 10, cy + 8]})
    assert sf["ok"], sf

    ex, _ = _call("export_drawing", {"fmt": "dxf", "doc_id": dwg})
    assert ex["ok"], ex
    assert ex["entities"] > 0
    assert os.path.exists(ex["path"])
    os.unlink(ex["path"])


def test_transform_moves_a_body():
    """transform translates a non-primitive-addressable body (COM shifts by the vector)."""
    r, _ = _call("new_part", {"name": "mv"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)})]})
    m0, _ = _call("measure_part", {"doc_id": doc})
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Move", {"translate": [200, 0, 0]})]})
    m1, _ = _call("measure_part", {"doc_id": doc})
    assert abs(m1["mass"]["com"][0] - m0["mass"]["com"][0] - 200) < 1e-6


def test_transform_copy_makes_two_bodies():
    """transform copy:true keeps the source and adds a moved duplicate (assembly instancing)."""
    r, _ = _call("new_part", {"name": "cp"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "C", {"radius": cb.dim(10), "height": cb.dim(30)})]})
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [50, 0, 0], "copy": True})]})
    assert r2["ok"] and r2["body_count"] == 2, r2


def test_transform_targets_named_non_last_body():
    """transform with a body SELECTOR OBJECT moves THAT named body, not the current/last one (T-1).

    Regression for the assembly bug: a canonical body selector object
    {"kind":"body","owner_feature":"fN"} was not resolved by body_ref (it only honored a
    bare string), so transform silently fell back to the last-imported body — every
    batched per-body move stacked onto one body. Here we lift the FIRST (non-last) body
    and confirm *it* moved while the later copy stayed put.
    """
    r, _ = _call("new_part", {"name": "tgt"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B1", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    first_id = r1["features"][0]["id"]
    # A moved copy off in +X becomes the current/last body (the wrong target if the bug bites).
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [100, 0, 0], "copy": True})]})
    # Target the FIRST body by a canonical selector OBJECT (top-level body field) and lift it +1000 Z.
    r3, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "LiftFirst", {"translate": [0, 0, 1000]},
                body={"kind": "body", "owner_feature": first_id})]})
    assert r3["ok"], r3
    m, _ = _call("measure_part", {"doc_id": doc})
    bodies = m["mass"]["bodies"]
    assert len(bodies) == 2, m
    near0 = next(b for b in bodies if abs(b["com"][0]) < 50)     # the original first body
    near100 = next(b for b in bodies if b["com"][0] > 50)        # the moved copy
    assert near0["com"][2] > 900, ("the named first body should have lifted", bodies)
    assert near100["com"][2] < 100, ("the copy must NOT have moved", bodies)


def test_transform_targets_named_body_string():
    """Same T-1 guard via a plain top-level body id STRING (the documented cheatsheet form)."""
    r, _ = _call("new_part", {"name": "tgt2"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B1", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    first_id = r1["features"][0]["id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [100, 0, 0], "copy": True})]})
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "LiftFirst", {"translate": [0, 0, 1000]}, body=first_id)]})
    m, _ = _call("measure_part", {"doc_id": doc})
    bodies = m["mass"]["bodies"]
    near0 = next(b for b in bodies if abs(b["com"][0]) < 50)
    near100 = next(b for b in bodies if b["com"][0] > 50)
    assert near0["com"][2] > 900 and near100["com"][2] < 100, bodies


def test_second_transform_composes_not_duplicates():
    """Issue 4: a second copy:false transform that targets a body a PRIOR transform already moved
    must compose on the placed body — not re-read the original and spawn a duplicate."""
    r, _ = _call("new_part", {"name": "compose"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    fid = r1["features"][0]["id"]
    m0, _ = _call("measure_part", {"doc_id": doc})
    vol0 = m0["mass"]["volume_mm3"]
    # First in-place move (consumes fid, produces a placed body).
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Place", {"translate": [100, 0, 0]}, body=fid)]})
    # Second transform STILL targeting the ORIGINAL fid, copy:false -> must compose on the placed
    # body. body_count stays 1, volume unchanged (no duplicate), and both moves accumulate.
    r3, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Nudge", {"translate": [0, 0, 50]}, body=fid)]})
    assert r3["ok"] and r3["body_count"] == 1, r3
    m1, _ = _call("measure_part", {"doc_id": doc})
    assert abs(m1["mass"]["volume_mm3"] - vol0) < 1e-6, (m1["mass"]["volume_mm3"], vol0)
    com = m1["mass"]["com"]
    assert com[0] > 90 and com[2] > 40, com  # composed both translations onto one body


def test_config_override_accepts_a_vector():
    """Issue 5: set_config_override accepts a LIST value (a transform translate) so one file can
    hold configs differing only in a placement vector."""
    r, _ = _call("new_part", {"name": "cfgvec"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(10), "width": cb.dim(10), "height": cb.dim(10)})]})
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Place", {"translate": [0, 0, 0]})]})
    tid = r2["features"][-1]["id"]
    _call("add_configuration", {"name": "Stowed", "doc_id": doc})
    ov, _ = _call("set_config_override", {"config": "Stowed", "feature_id": tid,
                                          "param": "translate", "value": [0, 0, 50], "doc_id": doc})
    assert ov["ok"] and ov["value"] == [0.0, 0.0, 50.0], ov
    act, _ = _call("activate_configuration", {"name": "Stowed", "doc_id": doc})
    assert act["ok"], act
    m, _ = _call("measure_part", {"doc_id": doc})
    assert m["mass"]["com"][2] > 40, m  # the body lifted in the Stowed config


def test_get_part_summary_is_lightweight():
    """Issue 8: get_part(summary=True) lists feature id/name/type/params with no tessellation."""
    doc = _new_plate_with_hole()
    summ, _ = _call("get_part", {"doc_id": doc, "summary": True})
    assert summ["ok"] and summ["summary"] is True, summ
    assert summ["feature_count"] >= 1 and summ["features"], summ
    f0 = summ["features"][0]
    assert set(("id", "name", "type", "params")).issubset(f0), f0


def test_measure_multibody_per_body_and_total():
    """A multi-body doc reports per-body breakdown + the combined total (not just one body)."""
    r, _ = _call("new_part", {"name": "asm"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "C", {"radius": cb.dim(10), "height": cb.dim(30)})]})
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [50, 0, 0], "copy": True})]})
    m, _ = _call("measure_part", {"doc_id": doc})
    mass = m["mass"]
    assert mass["body_count"] == 2 and len(mass.get("bodies", [])) == 2, mass
    per = sum(b["volume_mm3"] for b in mass["bodies"])
    assert abs(mass["volume_mm3"] - per) < 1e-3  # total == sum of bodies


def test_export_step_round_trip_validated():
    """export_part returns real metadata and only ok:True when the file actually re-reads."""
    doc = _new_plate_with_hole()
    r, _ = _call("export_part", {"fmt": "step", "doc_id": doc})
    assert r["ok"] and r["validated"] is True, r
    assert r["solid_count"] == r["expected_solids"] == 1 and r["bytes"] > 0, r
    os.unlink(r["path"])


def test_batch_atomicity_rolls_back_on_failure():
    """A batch with a feature that fails to COMPILE is rejected whole — nothing persists."""
    r, _ = _call("new_part", {"name": "atom"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    before, _ = _call("get_part", {"doc_id": doc})
    n_before = len((before.get("tree") or {}).get("features", []))
    bad, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B2", {"length": cb.dim(5), "width": cb.dim(5), "height": cb.dim(5)}),
        cb.feat("fillet", "F", {"radius": cb.dim(9999)}, {"edges": {"of": "edge"}})]})
    after, _ = _call("get_part", {"doc_id": doc})
    n_after = len((after.get("tree") or {}).get("features", []))
    assert bad["ok"] is False and bad.get("rolled_back") is True, bad
    assert n_after == n_before, (n_before, n_after)  # the whole batch was rolled back


def test_cfd_diagnostics_and_v2_scaling():
    """CFD reports resolution + convergence diagnostics, and drag now scales ~V^2 (not V^1)."""
    r, _ = _call("new_part", {"name": "sph"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("sphere", "S", {"radius": cb.dim(50)})]})
    r1, _ = _call("run_cfd_flow", {"doc_id": doc, "velocity": 10.0, "cells": 64, "n_steps": 1200})
    r2, _ = _call("run_cfd_flow", {"doc_id": doc, "velocity": 20.0, "cells": 64, "n_steps": 1200})
    for rr in (r1, r2):
        assert rr["ok"] and "voxel_mm" in rr and "converged" in rr and "residual" in rr, rr
        assert "ref_length_mm" in rr
    # Doubling velocity ~quadruples drag (V^2), and Cd is ~velocity-independent (was Cd∝1/V).
    ratio = r2["drag_n"] / r1["drag_n"]
    assert 3.2 < ratio < 4.8, (ratio, r1["drag_n"], r2["drag_n"])
    assert abs(r1["cd"] - r2["cd"]) / max(abs(r1["cd"]), 1e-9) < 0.05, (r1["cd"], r2["cd"])


def test_background_job_and_get_job():
    """A background CFD returns a job_id immediately; get_job polls it to completion."""
    import time
    r, _ = _call("new_part", {"name": "bg"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("sphere", "S", {"radius": cb.dim(40)})]})
    j, _ = _call("run_cfd_flow", {"doc_id": doc, "velocity": 12.0, "cells": 48,
                                  "n_steps": 800, "background": True})
    assert j["ok"] and j.get("job_id") and j["status"] == "running", j
    jid = j["job_id"]
    for _ in range(120):
        g, _ = _call("get_job", {"job_id": jid})
        if g.get("status") in ("done", "error"):
            break
        time.sleep(1)
    assert g["status"] == "done" and g["result"]["ok"], g


def test_run_cfd_accurate_dispatches():
    """run_cfd_accurate either starts an OpenFOAM job (returns a job_id) or reports it's
    unavailable — both are valid; we don't wait for the multi-minute solve here."""
    r, _ = _call("new_part", {"name": "sph"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [cb.feat("sphere", "S", {"radius": cb.dim(40)})]})
    out, _ = _call("run_cfd_accurate", {"doc_id": doc, "velocity": 15.0, "preset": "coarse"})
    if not out["ok"]:
        assert "OpenFOAM" in out.get("error", ""), out  # graceful "not available"
        return
    assert out.get("job_id") and out["status"] == "running", out
    g, _ = _call("get_job", {"job_id": out["job_id"]})
    assert g["status"] in ("running", "done", "error"), g


def test_fea_static_material_scaling_f4():
    """F4: aluminum must deflect ~2.86x MORE than steel under the same load (E_steel/E_Al ≈ 200/70).

    Bug F4: non-steel materials were 3.6x too stiff — aluminum deflected LESS than steel because
    the material name was not being correctly resolved to a properties dict before the solve.
    Fix: _resolve_material always returns a fresh copy of the _MATERIALS dict and validates that
    E_mpa > 0 so a bad entry is caught in Python rather than as a cryptic C++ error.

    This test catches any regression where the wrong E (e.g. steel's 200 GPa) is used for
    aluminum instead of the correct 69 GPa.  The cantilever is the same geometry and load for
    both solves; the deflection ratio must equal E_steel / E_Al to within 15% (the tolerance
    accommodates the coarse mesh and boundary-layer effects, but 3.6x too stiff collapses the
    ratio to ~1 and would fail instantly).
    """
    r, _ = _call("new_part", {"name": "mat_scale"})
    doc = r["doc_id"]
    # A simple 80×20×10 mm cantilever beam.
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Beam",
                {"length": cb.dim(80), "width": cb.dim(20), "height": cb.dim(10)})]})

    common = {
        "doc_id": doc,
        "fixtures": [{"of": "face", "side": "left"}],
        "loads": [{"face": {"of": "face", "side": "right"}, "force": [0, 0, -300]}],
    }
    rs, _ = _call("run_fea_static", {**common, "material": "steel"})
    ra, _ = _call("run_fea_static", {**common, "material": "aluminum"})
    assert rs["ok"], f"steel solve failed: {rs}"
    assert ra["ok"], f"aluminum solve failed: {ra}"

    delta_steel = rs["summary"]["max_disp_mm"]
    delta_al = ra["summary"]["max_disp_mm"]
    assert delta_steel > 0, f"steel deflection is zero: {rs}"
    assert delta_al > 0, f"aluminum deflection is zero: {ra}"

    # Aluminum (E≈69 GPa) must deflect MORE than steel (E≈200 GPa) — the bug caused it to
    # deflect LESS (backwards), which maps to a ratio < 1.
    assert delta_al > delta_steel, (
        f"F4 regression: aluminum deflects LESS than steel (delta_al={delta_al:.4f} mm < "
        f"delta_steel={delta_steel:.4f} mm). Material resolution is broken — check _MATERIALS "
        f"and _resolve_material in cad_tools.py."
    )

    # The ratio must be close to E_steel / E_Al = 200000 / 69000 ≈ 2.90.
    E_steel, E_al = 200000.0, 69000.0
    expected_ratio = E_steel / E_al  # ≈ 2.90
    actual_ratio = delta_al / delta_steel
    assert abs(actual_ratio - expected_ratio) / expected_ratio < 0.15, (
        f"F4: deflection ratio {actual_ratio:.3f} differs from expected {expected_ratio:.3f} "
        f"by more than 15%  (delta_steel={delta_steel:.4f}, delta_al={delta_al:.4f}). "
        f"A ratio ~1 means both materials are being solved with the SAME (steel) E."
    )


def test_fea_modal_free_free_f6():
    """F6: run_fea_modal(fixtures=[]) must NOT return 'no fixture faces resolved' — it should
    call the C++ solver with no constraints and return rigid-body + elastic modes.

    Bug F6: the Python guard `if not fixture_sets: return error` fired for empty-fixture calls
    even though the C++ fea_modal_solve handles unconstrained (free-free) problems correctly.
    Fix: only reject when fixtures were PROVIDED but none resolved; skip the guard when
    fixtures=[] (free-free is intentional).

    Verified behavior from the live C++ solver on a 60×20×10 mm box:
    - ok:True (not an error)
    - Returns the requested n_modes frequencies
    - At least the first few modes are near 0 Hz (rigid-body; the shift-invert solver may
      collapse all 6 near-zero modes into fewer numerical zeros depending on spectral shift)
    - All remaining (elastic) modes are strictly positive
    """
    r, _ = _call("new_part", {"name": "free_free"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block",
                {"length": cb.dim(60), "width": cb.dim(20), "height": cb.dim(10)})]})

    # Free-free: no fixture faces.  Before the F6 fix this returned {"ok":False,
    # "error":"no fixture faces resolved"} without ever calling the C++ solver.
    rf, _ = _call("run_fea_modal", {"doc_id": doc, "fixtures": [], "n_modes": 12,
                                    "material": "steel"})
    assert rf["ok"], (
        f"F6 regression: free-free modal returned an error — the Python guard is still "
        f"blocking unconstrained solves.  Error: {rf.get('error', rf)}"
    )
    freqs = rf.get("freqs_hz", [])
    assert len(freqs) >= 6, (
        f"expected at least 6 modes from a free-free solve, got {len(freqs)}: {freqs}"
    )

    # At least the first mode must be near 0 Hz (a rigid-body mode).  We use a generous
    # 1 Hz threshold to accommodate numerical noise; the exact count of near-zero modes
    # depends on the spectral shift the solver uses.
    assert abs(freqs[0]) < 1.0, (
        f"F6: mode 0 has frequency {freqs[0]:.4f} Hz — expected a rigid-body mode near 0 Hz. "
        f"All modes: {freqs}"
    )

    # Count near-zero (rigid-body) modes and confirm the rest are positive elastic modes.
    near_zero = [f for f in freqs if abs(f) < 1.0]
    elastic = [f for f in freqs if abs(f) >= 1.0]
    assert len(near_zero) >= 1, f"no near-zero rigid-body modes found: {freqs}"
    for f in elastic:
        assert f > 0, (
            f"elastic mode has non-positive frequency {f} Hz — expected all elastic "
            f"modes to be > 0. Rigid-body modes: {near_zero}, elastic: {elastic}"
        )


def test_measure_between_single_face_area():
    """measure_between(one face query) returns that face's area."""
    doc = _new_plate_with_hole()
    r, _ = _call("measure_between", {"a_query": "top", "doc_id": doc})
    assert r["ok"], r
    assert r["kind"] == "area"
    # The plate top is 60x40 minus an 8mm hole's footprint; area is just under 2400.
    assert 2000 < r["area"] < 2400, r
    assert "mm^2" in r["summary"], r


def test_measure_between_two_faces_distance():
    """measure_between(top, bottom) returns the through-thickness distance (= plate height 10)."""
    doc = _new_plate_with_hole()
    r, _ = _call("measure_between", {"a_query": "top", "b_query": "bottom", "doc_id": doc})
    assert r["ok"], r
    assert abs(r["distance"] - 10.0) < 1e-6, r
    assert "distance" in r["summary"], r


def test_measure_between_two_faces_angle():
    """Adjacent orthogonal faces measure a 90-degree angle."""
    doc = _new_plate_with_hole()
    r, _ = _call("measure_between", {"a_query": "top", "b_query": "front", "doc_id": doc})
    assert r["ok"], r
    assert abs(r["angle"] - 90.0) < 1e-6, r


def test_measure_between_ambiguous_query_is_rejected():
    """A query matching several faces/edges is rejected with a helpful message (not a wrong number)."""
    doc = _new_plate_with_hole()
    # "all the vertical edges" matches 4 -> must not silently measure one.
    r, _ = _call("measure_between", {"a_query": {"of": "edge", "parallel": "z"}, "doc_id": doc})
    assert r["ok"] is False, r
    assert "matched" in r["error"].lower() or "candidate" in r["error"].lower(), r


def test_check_interference_single_part_clean():
    """A single-body part trivially reports no interference."""
    doc = _new_plate_with_hole()
    r, _ = _call("check_interference", {"doc_id": doc})
    assert r["ok"] is True, r
    assert r["interferences"] == [], r
    assert len(r["parts"]) == 1, r


def test_check_interference_overlapping_bodies():
    """Two overlapping copies of a body are flagged as an interference with an overlap volume."""
    r, _ = _call("new_part", {"name": "clash"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)})]})
    # A copy shifted only 10mm in X overlaps the original.
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [10, 0, 0], "copy": True})]})
    r2, _ = _call("check_interference", {"doc_id": doc})
    assert r2["ok"] is False, r2  # they overlap -> not ok
    assert len(r2["interferences"]) == 1, r2
    assert r2["interferences"][0]["volume"] > 0, r2


def test_check_symmetry_centered_box():
    """A box centered on the origin is symmetric about all three principal planes."""
    r, _ = _call("new_part", {"name": "sym"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(30), "height": cb.dim(20)})]})
    for plane in ("top", "front", "right"):
        rr, _ = _call("check_symmetry", {"plane": plane, "doc_id": doc})
        assert rr["ok"] and rr["symmetric"] is True, (plane, rr)
        assert rr["deviation_mm3"] < 1e-3, (plane, rr)


def test_configurations_variant_changes_volume():
    """A configuration with a parameter override + a suppression yields a different built variant."""
    r, _ = _call("new_part", {"name": "variants"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(40), "width": cb.dim(30), "height": cb.dim(20)})]})
    fid = r1["features"][0]["id"]

    base, _ = _call("measure_part", {"doc_id": doc})
    base_vol = base["mass"]["volume_mm3"]
    assert abs(base_vol - 24000) < 1e-3, base

    # Default + a "Big" variant that doubles the length.
    lst, _ = _call("list_configurations", {"doc_id": doc})
    assert lst["configurations"] == ["Default"] and lst["active"] == "Default", lst

    a, _ = _call("add_configuration", {"name": "Big", "doc_id": doc})
    assert a["ok"] and "Big" in a["configurations"], a

    o, _ = _call("set_config_override", {"config": "Big", "feature_id": fid,
                                         "param": "length", "value": 80, "doc_id": doc})
    assert o["ok"], o

    act, _ = _call("activate_configuration", {"name": "Big", "doc_id": doc})
    assert act["ok"] and act["active"] == "Big" and act["body_count"] == 1, act

    big, _ = _call("measure_part", {"doc_id": doc})
    assert abs(big["mass"]["volume_mm3"] - 48000) < 1e-3, big  # length doubled -> volume doubled

    # Switch back to Default -> original volume restored.
    _call("activate_configuration", {"name": "Default", "doc_id": doc})
    back, _ = _call("measure_part", {"doc_id": doc})
    assert abs(back["mass"]["volume_mm3"] - base_vol) < 1e-3, back


def test_config_suppression_drops_a_feature():
    """Suppressing the only feature in a config removes the body in that variant."""
    r, _ = _call("new_part", {"name": "supp"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(20)})]})
    fid = r1["features"][0]["id"]
    _call("add_configuration", {"name": "Empty", "doc_id": doc})
    s, _ = _call("set_config_suppression", {"config": "Empty", "feature_id": fid,
                                            "suppressed": True, "doc_id": doc})
    assert s["ok"], s
    act, _ = _call("activate_configuration", {"name": "Empty", "doc_id": doc})
    assert act["ok"] and act["body_count"] == 0, act  # the box is suppressed -> no body


def test_solve_mates_rejects_a_part_but_world_bodies_lists_it():
    """solve_mates is assembly-only (clear error on a part); list_world_bodies now ALSO works on
    a multi-body part (Issue 9) — it reports each body's world bbox so an import_part-assembled
    part can be inspected without promoting it to a formal assembly doc."""
    doc = _new_plate_with_hole()
    sm, _ = _call("solve_mates", {"doc_id": doc})
    assert sm["ok"] is False and "assembl" in sm["error"].lower(), sm
    wb, _ = _call("list_world_bodies", {"doc_id": doc})
    assert wb["ok"] is True and wb.get("multibody_part") is True, wb
    assert wb["body_count"] >= 1 and wb["bodies"], wb
    assert wb["bodies"][0].get("bbox"), wb  # each body carries a world bbox


# ---------------------------------------------------------------- inspection suite
# These exercise the three INSPECTION RPCs that were filled in C++ (section area+moments,
# numeric wall thickness, moldability draft). They need the REBUILT DLL — the math was 0/empty
# before. After `build.bat` rebuilds + stages libaicad.dll they pass.

def test_inspect_section_real_area():
    """inspect_section cuts the part and returns the REAL cross-section area (not 0).

    A 60x40x20 box cut by the 'right' plane (YZ, through the origin -> mid-length) exposes a
    40(width) x 20(height) rectangle = 800 mm^2. Before the C++ fix this was hard-coded to 0.
    """
    r, _ = _call("new_part", {"name": "sec"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(20)})]})
    s, _ = _call("inspect_section", {"plane": "right", "doc_id": doc})
    assert s["ok"], s
    # Right plane (YZ) slices the 40x20 face: 800 mm^2 (the box is centred on the origin).
    assert abs(s["area_mm2"] - 800.0) < 1.0, s
    assert "mm^2" in s["summary"], s
    # Second moments of a 40(b) x 20(h) rect about its centroid in the plane frame are nonzero.
    assert s["Ixx"] > 0 and s["Iyy"] > 0, s


def test_wall_thickness_numeric_on_shell():
    """wall_thickness returns a numeric min/mean/max — the thinnest wall of a shelled box.

    A 40x40x40 box shelled to 2 mm walls should report a min wall thickness ~2 mm. The inward
    ray-cast (Moller-Trumbore, two-sided) stops at the NEAREST opposite wall, so the thinnest
    wall is found regardless of triangle winding. Before the C++ fix only an aicm_path came
    back (no numbers).

    NOTE: the body must actually HAVE walls — i.e. an open shell with a removed face. A
    "closed" shell with NO removed face is, in the current engine, a solid shrunk block (the
    shell op needs at least one open face to leave a cavity), and a solid block's "thickness"
    is correctly its full span (~36 mm), not 2. So we remove the top face to make a real
    2 mm-walled bucket and assert the algorithm finds the 2 mm wall."""
    r, _ = _call("new_part", {"name": "wt"})
    doc = r["doc_id"]
    # Open hollow shell, 2 mm walls: remove the top face so there is a genuine cavity with
    # real inner + outer walls. thickness+outward are the documented params (see
    # cheatsheets/shell.md); outward:false hollows inward.
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)}),
        cb.feat("shell", "Hollow", {"thickness": cb.dim(2), "outward": False},
                {"remove": {"of": "face", "side": "top"}})]})
    w, _ = _call("wall_thickness", {"doc_id": doc})
    assert w["ok"], w
    assert w["min_thickness_mm"] is not None, w
    # The thinnest wall is the 2 mm shell (raycast on a coarse tessellation -> tolerance band).
    assert 1.0 < w["min_thickness_mm"] < 3.5, w
    assert w["mean_thickness_mm"] >= w["min_thickness_mm"], w
    assert w["max_thickness_mm"] >= w["mean_thickness_mm"], w
    assert "wall" in w["summary"].lower(), w


def test_draft_analysis_flags_vertical_walls():
    """draft_analysis flags the 4 vertical walls of a box pulled along +Z (0deg draft < 3deg).

    A plain box's side walls are parallel to a +Z pull -> 0deg draft -> below the 3deg minimum.
    The top/bottom faces are perpendicular (90deg) -> fine. So exactly the 4 sides are flagged.
    """
    r, _ = _call("new_part", {"name": "dr"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(30), "height": cb.dim(20)})]})
    d, _ = _call("draft_analysis", {"pull_dir": [0, 0, 1], "min_draft_deg": 3.0, "doc_id": doc})
    assert d["ok"], d
    # The four vertical side walls have ~0deg draft -> below threshold; top+bottom are fine.
    assert d["n_faces_below_threshold"] == 4, d
    assert len(d["faces_below_threshold"]) == 4, d
    for f in d["faces_below_threshold"]:
        assert f["area_mm2"] > 0 and len(f["centroid"]) == 3, f
        assert f["min_draft_deg"] < 3.0, f
    assert "draft" in d["summary"].lower(), d


def test_draft_analysis_sphere_is_moldable():
    """A sphere has draft everywhere except a single silhouette ring; pulled along Z almost
    all faces clear a small threshold. We just assert the summary + fields are well-formed and
    that min/max draft span roughly -90..90 (a full range of surface orientations)."""
    r, _ = _call("new_part", {"name": "sph"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [cb.feat("sphere", "S", {"radius": cb.dim(20)})]})
    d, _ = _call("draft_analysis", {"pull_dir": [0, 0, 1], "min_draft_deg": 1.0, "doc_id": doc})
    assert d["ok"], d
    assert d["min_draft_deg"] is not None and d["max_draft_deg"] is not None, d
    assert d["min_draft_deg"] < -45.0 and d["max_draft_deg"] > 45.0, d  # full orientation span


def test_new_part_seeds_volume_baseline_for_write_diff():
    """new_part seeds _LAST_VOLUME so the FIRST add_features reports a real volume delta
    (volume 0.0 -> X) instead of degrading to the no-baseline 'volume now X' branch."""
    from mcp_server import cad_tools
    r, _ = _call("new_part", {"name": "vbase"})
    doc = r["doc_id"]
    # the empty part's volume baseline is seeded (zero) so the write-diff has a before-value
    assert doc in cad_tools._LAST_VOLUME, "new_part must seed _LAST_VOLUME for the write-diff"
    assert cad_tools._LAST_VOLUME[doc] == 0.0, cad_tools._LAST_VOLUME[doc]
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(30), "width": cb.dim(20), "height": cb.dim(10)})]})
    assert r2["ok"], r2
    ch = r2.get("changes") or {}
    # a baseline was available -> the delta is reported as a number (not the no-baseline branch)
    assert "volume_delta_mm3" in ch, ch
    assert ch["volume_delta_mm3"] > 0.0, ch  # material added from an empty part
    assert cad_tools._LAST_VOLUME[doc] == r2["volume_mm3"], "baseline updated to the new volume"


# ================================================================ AI-experience lane
# Appended tests (HANDOFF_AI_EXPERIENCE §TODO-2 TESTS-MCP): feature-overlap detection,
# add_features overlap warnings, coincident-contact interference, and the render caption.

# --- pytest import-shadow repair --------------------------------------------------------
# Under `python3 -m pytest services/mcp/test_cad_tools.py` pytest's default (prepend)
# import mode names this module `mcp.test_cad_tools` — this directory has an __init__.py,
# so the PACKAGE `mcp` becomes services/mcp, shadowing the `mcp` SDK that mcp_server's
# tool modules import (`from mcp.server.fastmcp import FastMCP`). Repair sys.modules:
# load the real SDK package as `mcp`, keeping this directory on its __path__ so pytest
# can still import sibling test modules as `mcp.test_*`. (Runs at import time, before
# any test body executes; a no-op when `mcp` already resolves to the real SDK — e.g.
# when run by path as `mcp_server/test_cad_tools.py`.)
import importlib.util


def _unshadow_mcp_sdk() -> None:
    _here = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile or os.path.dirname(os.path.realpath(sfile)) != _here:
        return  # `mcp` is absent or already the real SDK — nothing to repair
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _here:
            spec = importlib.util.spec_from_file_location(
                "mcp", init, submodule_search_locations=[os.path.dirname(init), _here])
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module  # must be visible to the SDK's own imports
            spec.loader.exec_module(module)
            return
    raise ImportError("could not locate the real `mcp` SDK to un-shadow services/mcp")


_unshadow_mcp_sdk()


def test_check_feature_overlap_clean_part():
    """A part whose features don't collide reports no pairs and no dead features."""
    r, _ = _call("new_part", {"name": "ov_clean"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(30), "height": cb.dim(20)})]})
    assert r1["ok"], r1
    ov, _ = _call("check_feature_overlap", {"doc_id": doc})
    assert ov["ok"] is True, ov
    assert ov["pairs"] == [] and ov["dead_features"] == [], ov
    assert "No feature overlap" in ov["summary"], ov


def test_check_feature_overlap_boss_swallowed():
    """A cut that erases an EARLIER boss entirely is flagged kind=boss_swallowed — the only
    overlap kind that sets ok:false — and the boss lands in dead_features (it owns no
    surviving faces)."""
    r, _ = _call("new_part", {"name": "ov_swallow"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Base", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)})]})
    assert r1["ok"], r1
    # A boss pin through the plate (r4, taller than the plate)...
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "Boss", {"radius": cb.dim(4), "height": cb.dim(30)})]})
    assert r2["ok"], r2
    boss_id = r2["features"][-1]["id"]  # `features` lists the WHOLE tree's rebuild status
    # ...then a WIDER, taller coaxial cut removes the whole boss (plus a hole in the base).
    r3, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "Kill",
                {"radius": cb.dim(6), "height": cb.dim(40), "mode": "cut"})]})
    assert r3["ok"], r3
    kill_id = r3["features"][-1]["id"]

    ov, _ = _call("check_feature_overlap", {"doc_id": doc})
    assert ov["ok"] is False, ov  # boss_swallowed is the one kind that fails the check
    swallowed = [p for p in ov["pairs"] if p["kind"] == "boss_swallowed"]
    assert len(swallowed) == 1, ov["pairs"]
    assert swallowed[0]["a"] == boss_id and swallowed[0]["b"] == kill_id, swallowed
    assert swallowed[0]["volume_mm3"] > 0, swallowed
    assert boss_id in ov["dead_features"], ov
    assert "boss_swallowed" in ov["summary"], ov["summary"]


def test_add_features_attaches_overlap_warnings():
    """add_features runs the batch-scoped overlap check automatically and attaches
    `overlap_warnings` when the new batch collides with the existing tree (B3)."""
    r, _ = _call("new_part", {"name": "ov_warn"})
    doc = r["doc_id"]
    r1, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Base", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)})]})
    assert r1["ok"], r1
    assert not r1.get("overlap_warnings"), r1  # first feature alone: nothing to collide with
    # A boss cylinder overlapping the base volume -> the batch return warns boss_overlap.
    r2, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "Boss", {"radius": cb.dim(4), "height": cb.dim(30)})]})
    assert r2["ok"], r2
    warn = r2.get("overlap_warnings")
    assert warn, r2
    assert warn["pairs"] and warn["pairs"][0]["kind"] == "boss_overlap", warn
    assert warn["summary"], warn
    assert warn.get("dead_features") == [], warn


def test_check_interference_include_coincident_contacts():
    """include_coincident classifies exactly-touching bodies as `contacts` (the SolidWorks
    'coincident faces' case) instead of burying them in clearances; without the flag the
    same pair reports as a 0-distance clearance."""
    r, _ = _call("new_part", {"name": "touch"})
    doc = r["doc_id"]
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "B", {"length": cb.dim(40), "width": cb.dim(40), "height": cb.dim(40)})]})
    # A copy shifted by EXACTLY the box length -> faces touch at x=20, zero overlap.
    _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("transform", "Dup", {"translate": [40, 0, 0], "copy": True})]})

    ci, _ = _call("check_interference", {"doc_id": doc, "include_coincident": True})
    assert ci["ok"] is True, ci  # touching is not interfering
    assert ci["interferences"] == [], ci
    assert len(ci["contacts"]) == 1, ci
    assert ci["contacts"][0]["distance"] <= 1e-6, ci
    assert ci["clearances"] == [], ci  # the pair moved out of clearances

    ci2, _ = _call("check_interference", {"doc_id": doc})
    assert ci2["ok"] is True, ci2
    assert ci2["contacts"] == [], ci2
    assert len(ci2["clearances"]) == 1 and ci2["clearances"][0]["distance"] <= 1e-6, ci2


def test_render_part_caption_pairs_image_with_text():
    """render_part returns [ImageContent, TextContent]: the PNG plus the deterministic
    scene caption (mcp's _convert_to_content flattens the [Image, str] return), so a
    non-vision model reading the transcript still gets signal from the render."""
    doc = _new_plate_with_hole()
    from mcp_server import server

    # call_tool returns the bare flattened content list for image tools (no structured
    # result) — same shape test_render_part_returns_image hunts through.
    result = asyncio.run(server.mcp.call_tool("render_part", {"doc_id": doc}))
    content = result[0] if isinstance(result, tuple) else result
    image = _find_image(content)
    assert image is not None, f"no ImageContent in {content!r}"
    assert getattr(image, "mimeType", "").startswith("image/") and image.data, image
    texts = [c for c in content if getattr(c, "type", "") == "text"]
    assert texts, f"no TextContent caption in {content!r}"
    caption = texts[0].text
    assert "iso view of a" in caption, caption
    assert "60x40x10 mm" in caption, caption
    assert "occlusion not modeled" in caption, caption


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
