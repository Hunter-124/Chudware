"""W12 — DrawingDocument schema + HLR projection + drawing_* RPC (engine/doc/drawing.py).

Builds a part (box + fillet + through hole, so there are visible AND hidden edges), creates a
drawing referencing it, adds front/top/right/iso views, and asserts:

  * the DrawingDocument validates (schema.py validate_drawing);
  * each view returns non-empty VISIBLE polylines;
  * the FRONT view (show_hidden) has HIDDEN edges from the through hole;
  * the bbox scales with the view ``scale``;
  * orientations differ (front projection != top projection);
  * editing the source (change the box length) → re-fetch → projection updates (associativity).

Run:  & $py -m engine.doc.test_drawing   (from repo root)
"""

from __future__ import annotations

from engine.doc import drawing, session as docsess
from engine.doc.schema import validate_drawing, validate_document, SCHEMA_DRAWING


# --------------------------------------------------------------------------- part fixture


def _build_part_with_hole() -> docsess.DocSession:
    """A 60×40×20 box, one filleted vertical edge, and a Ø10 through hole down the middle.

    The hole gives HIDDEN edges in the front/right views (the bore walls behind the front face);
    the fillet gives a smooth/outline curve. Built through the real op pipeline so the compiler runs
    exactly as the worker would.
    """
    s = docsess.new_session("part", name="Bracket")
    box = {"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": {"value": 60, "expr": None},
                   "width": {"value": 40, "expr": None},
                   "height": {"value": 20, "expr": None}}}}
    docsess.apply_ops(s, None, [box])

    pick = docsess.resolve_pick(s, s.revision, body_id=1, kind="edge", topo_id=1)
    fillet = {"op": "add_feature", "feature": {
        "type": "fillet", "name": "Fillet1",
        "params": {"radius": {"value": 4.0, "expr": None}},
        "refs": {"edges": [pick["selector"]]}}}
    docsess.apply_ops(s, None, [fillet])

    # Find the TOP face (centroid highest Z) to drill the hole through.
    result = docsess._ensure_built(s)
    body = result.bodies[-1].shape
    top_face = max(body.faces(), key=lambda f: f.center().Z)
    top_pick = _selector_for_face(s, body, top_face)
    hole = {"op": "add_feature", "feature": {
        "type": "hole", "name": "Hole1",
        "params": {"style": "simple", "end": "through_all", "standard": "custom",
                   "diameter": {"value": 10.0, "expr": None}},
        "refs": {"face": top_pick},
        }}
    # `points` is a UV_LIST ref (face-local offsets); a single [0,0] drills the face-frame center.
    hole["feature"]["refs"]["points"] = [[0.0, 0.0]]
    docsess.apply_ops(s, None, [hole])
    return s


def _selector_for_face(s, body, face):
    """Resolve a face object to a persistent selector via resolve_pick (match by TopExp index)."""
    faces = body.faces()
    idx = None
    for i, f in enumerate(faces, start=1):
        c = f.center()
        if abs(c.X - face.center().X) < 1e-6 and abs(c.Y - face.center().Y) < 1e-6 \
                and abs(c.Z - face.center().Z) < 1e-6:
            idx = i
            break
    assert idx is not None, "could not locate the top face index"
    return docsess.resolve_pick(s, s.revision, body_id=1, kind="face", topo_id=idx)["selector"]


# --------------------------------------------------------------------------- helpers


def _nverts(polylines):
    return sum(len(pl) for pl in polylines)


# --------------------------------------------------------------------------- the test


def test_drawing_module() -> None:
    passed = 0

    # --- build the part ------------------------------------------------------
    part = _build_part_with_hole()
    fstatus = part.last_result.feature_status
    assert all(f["ok"] for f in fstatus), [f for f in fstatus if not f["ok"]]
    print(f"[drawing] part built: features={[f['id'] for f in fstatus]} "
          f"bodies={len(part.last_result.bodies)}")
    passed += 1

    # --- drawing_new ---------------------------------------------------------
    res = drawing.new_drawing(part.doc_id, name="Bracket Drawing")
    draw_id = res["doc_id"]
    assert draw_id in drawing._DRAWINGS, "drawing not registered"
    assert res["drawing"]["schema"] == SCHEMA_DRAWING, res["drawing"]["schema"]
    assert res["drawing"]["source"] == part.doc_id
    print(f"[drawing] drawing_new -> doc_id={draw_id} schema={res['drawing']['schema']}")
    passed += 1

    # --- add front/top/right/iso views --------------------------------------
    fr = drawing.add_view(draw_id, type="base", orientation="front", scale=1.0,
                          position=[100, 100], show_hidden=True)
    tp = drawing.add_view(draw_id, type="projected", orientation="top", scale=1.0,
                          position=[100, 200], show_hidden=True)
    rt = drawing.add_view(draw_id, type="projected", orientation="right", scale=1.0,
                          position=[250, 100], show_hidden=True)
    iso = drawing.add_view(draw_id, type="iso", orientation="iso", scale=1.0,
                           position=[250, 200], show_hidden=False)
    for tag, v in (("front", fr), ("top", tp), ("right", rt), ("iso", iso)):
        g = v["view"]
        assert g["error"] is None, f"{tag} projection error: {g['error']}"
        assert _nverts(g["visible"]) > 0, f"{tag} view has no visible polylines"
        print(f"[drawing] add_view {tag}: vis_polylines={len(g['visible'])} "
              f"hid_polylines={len(g['hidden'])} bbox={[round(c,1) for c in g['bbox']] if g['bbox'] else None} "
              f"hlr_ms={g['ms']}")
    passed += 1

    # --- front view has HIDDEN edges from the through hole -------------------
    assert _nverts(fr["view"]["hidden"]) > 0, \
        "front view (show_hidden) should expose the through-hole's hidden bore edges"
    assert _nverts(iso["view"]["hidden"]) == 0, "iso view was added show_hidden=False"
    print(f"[drawing] front hidden polylines={len(fr['view']['hidden'])} "
          f"(through-hole bore) ; iso hidden={len(iso['view']['hidden'])} (suppressed)")
    passed += 1

    # --- orientations differ: front projection != top projection ------------
    fr_bb = fr["view"]["bbox"]
    tp_bb = tp["view"]["bbox"]
    fr_h = fr_bb[3] - fr_bb[1]   # front height ~ box height Z = 20
    tp_h = tp_bb[3] - tp_bb[1]   # top height ~ box depth Y = 40
    assert abs(fr_h - tp_h) > 1.0, f"front/top projections should differ (got {fr_h:.1f} vs {tp_h:.1f})"
    print(f"[drawing] front height={fr_h:.1f}mm (~Z=20) ; top height={tp_h:.1f}mm (~Y=40) -> differ")
    passed += 1

    # --- bbox scales with `scale` -------------------------------------------
    g1 = drawing.set_view(draw_id, fr["view_id"], scale=1.0, position=[0, 0])["view"]
    g2 = drawing.set_view(draw_id, fr["view_id"], scale=2.0, position=[0, 0])["view"]
    w1 = g1["bbox"][2] - g1["bbox"][0]
    w2 = g2["bbox"][2] - g2["bbox"][0]
    assert abs(w2 - 2.0 * w1) < 1e-3, f"bbox should scale 2x: {w1:.3f} -> {w2:.3f}"
    print(f"[drawing] bbox width scale=1 -> {w1:.2f}mm ; scale=2 -> {w2:.2f}mm (exactly 2x)")
    passed += 1
    # restore the front view position
    drawing.set_view(draw_id, fr["view_id"], scale=1.0, position=[100, 100])

    # --- drawing_get: full doc + every view's geometry ----------------------
    full = drawing.get_drawing_full(draw_id)
    assert full["source_doc_id"] == part.doc_id
    all_views = [v for sh in full["sheets"] for v in sh["views"]]
    assert len(all_views) == 4, f"expected 4 views, got {len(all_views)}"
    assert all(v["error"] is None and _nverts(v["visible"]) > 0 for v in all_views)
    # the default A3 sheet exposes its mm extent for the host border
    assert full["sheets"][0]["size_mm"] == [420.0, 297.0], full["sheets"][0]["size_mm"]
    print(f"[drawing] drawing_get -> {len(all_views)} views, sheet={full['sheets'][0]['size']} "
          f"{full['sheets'][0]['size_mm']}mm total_hlr_ms={full['total_hlr_ms']}")
    passed += 1

    # --- schema validation ---------------------------------------------------
    errs = validate_drawing(full["tree"])
    assert errs == [], f"drawing doc failed schema validation: {errs}"
    assert validate_document(full["tree"]) == []
    print(f"[drawing] validate_drawing -> clean ({len(all_views)} views across "
          f"{len(full['sheets'])} sheet)")
    passed += 1

    # --- associativity: edit the source, re-fetch, projection updates --------
    fr_width_before = full["sheets"][0]["views"][0]["bbox"][2] - full["sheets"][0]["views"][0]["bbox"][0]
    # Box1 is f1 — lengthen it from 60 to 90 mm.
    edit = {"op": "edit_feature", "id": "f1",
            "params": {"length": {"value": 90, "expr": None}}}
    docsess.apply_ops(part, None, [edit])
    full2 = drawing.get_drawing_full(draw_id)
    fr2 = full2["sheets"][0]["views"][0]
    fr_width_after = fr2["bbox"][2] - fr2["bbox"][0]
    assert full2["source_revision"] > full["source_revision"], "source revision did not advance"
    assert abs(fr_width_after - fr_width_before) > 1.0, \
        f"front view width should grow with the box (before {fr_width_before:.1f}, after {fr_width_after:.1f})"
    print(f"[drawing] associativity: box length 60->90 ; front view width "
          f"{fr_width_before:.1f} -> {fr_width_after:.1f}mm (re-projected)")
    passed += 1

    # --- remove_view ---------------------------------------------------------
    rm = drawing.remove_view(draw_id, iso["view_id"])
    assert rm["removed"]
    full3 = drawing.get_drawing_full(draw_id)
    remaining = [v["view_id"] for sh in full3["sheets"] for v in sh["views"]]
    assert iso["view_id"] not in remaining and len(remaining) == 3, remaining
    print(f"[drawing] remove_view({iso['view_id']}) -> remaining {remaining}")
    passed += 1

    # --- invalid-schema rejection (negative) --------------------------------
    bad = {"schema": SCHEMA_DRAWING, "id": "x", "name": "bad",
           "units": {"system": "MMGS"}, "source": "",
           "sheets": [{"id": "s1", "size": "A9",
                       "views": [{"id": "v1", "type": "iso", "orientation": "front",
                                  "scale": -1, "position": [0], "show_hidden": "no"}]}]}
    berrs = validate_drawing(bad)
    assert berrs, "malformed drawing should not validate"
    print(f"[drawing] negative schema check -> {len(berrs)} errors flagged (as expected)")
    passed += 1

    drawing.close_drawing(draw_id)
    print(f"\nDRAWING TEST PASSED ({passed} checks)")


if __name__ == "__main__":
    test_drawing_module()
