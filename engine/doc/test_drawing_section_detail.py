"""W15 — SECTION + DETAIL drawing views (engine/doc/drawing.py).

Builds a box with a through hole, makes a drawing with a front base view, then:

  * drawing_add_section with a vertical world plane through the middle -> the section view returns
    VISIBLE polylines AND non-empty section_faces (the hatched cut area), and the bore is revealed
    (the cut face is split into two pieces by the hole);
  * a section defined by a 2D cut_line dropped across the parent view -> same (real cut geometry);
  * drawing_add_detail on a corner region -> a magnified crop whose geometry is the parent's
    polylines clipped to the circle, scaled (bbox within the magnified circle, fewer polylines);
  * drawing_get attaches the detail_circle / section_line markers to the parent view;
  * error cases (unknown parent, no plane, bad radius, cut that misses the model);
  * the DrawingDocument still validates with the new view types.

Run:  & $py -X utf8 -m pytest engine/doc/test_drawing_section_detail.py -q
"""

from __future__ import annotations

import math

import pytest

from engine.doc import drawing, session as docsess
from engine.doc.ops import OpError
from engine.doc.schema import validate_drawing, SCHEMA_DRAWING


# --------------------------------------------------------------------------- part fixture


def _selector_for_face(s, body, face):
    faces = body.faces()
    idx = None
    for i, f in enumerate(faces, start=1):
        c = f.center()
        if (abs(c.X - face.center().X) < 1e-6 and abs(c.Y - face.center().Y) < 1e-6
                and abs(c.Z - face.center().Z) < 1e-6):
            idx = i
            break
    assert idx is not None, "could not locate the face index"
    return docsess.resolve_pick(s, s.revision, body_id=1, kind="face", topo_id=idx)["selector"]


def _build_box_with_hole() -> docsess.DocSession:
    """A 60x40x20 box centered at the origin with a vertical Ø10 through hole down the middle."""
    s = docsess.new_session("part", name="Block")
    box = {"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": {"value": 60, "expr": None},
                   "width": {"value": 40, "expr": None},
                   "height": {"value": 20, "expr": None}}}}
    docsess.apply_ops(s, None, [box])

    result = docsess._ensure_built(s)
    body = result.bodies[-1].shape
    top_face = max(body.faces(), key=lambda f: f.center().Z)
    top_pick = _selector_for_face(s, body, top_face)
    hole = {"op": "add_feature", "feature": {
        "type": "hole", "name": "Hole1",
        "params": {"style": "simple", "end": "through_all", "standard": "custom",
                   "diameter": {"value": 10.0, "expr": None}},
        "refs": {"face": top_pick, "points": [[0.0, 0.0]]}}}
    docsess.apply_ops(s, None, [hole])
    return s


def _nverts(polylines):
    return sum(len(pl) for pl in polylines)


# --------------------------------------------------------------------------- the test


def test_section_and_detail():
    passed = 0
    part = _build_box_with_hole()
    fstatus = part.last_result.feature_status
    assert all(f["ok"] for f in fstatus), [f for f in fstatus if not f["ok"]]
    passed += 1

    res = drawing.new_drawing(part.doc_id, name="Block Drawing")
    draw_id = res["doc_id"]
    front = drawing.add_view(draw_id, type="base", orientation="front", scale=1.0,
                             position=[100, 100], show_hidden=True)
    fid = front["view_id"]
    assert front["view"]["error"] is None
    passed += 1

    # --- SECTION via an explicit world plane (x=0, vertical, normal +X) through the middle ----
    sec = drawing.add_section(draw_id, fid,
                              plane={"normal": [1.0, 0.0, 0.0], "point": [0.0, 0.0, 0.0]},
                              scale=1.0, position=[260, 100])
    sg = sec["view"]
    assert sg["error"] is None, f"section error: {sg['error']}"
    assert _nverts(sg["visible"]) > 0, "section view has no visible polylines"
    assert len(sg["section_faces"]) > 0, "section view has no hatched cut faces"
    assert sg["type"] == "section" and sg["label"]
    # The bore splits the cut face into TWO pieces (the section reveals the hole as a gap).
    assert len(sg["section_faces"]) >= 2, \
        f"the through hole should split the cut face into >=2 pieces, got {len(sg['section_faces'])}"
    # Each section face is a closed polygon (>=3 pts).
    assert all(len(p) >= 3 for p in sg["section_faces"])
    print(f"[sd] section(plane): visible={len(sg['visible'])} "
          f"section_faces={len(sg['section_faces'])} (bore revealed) label={sg['label']}")
    passed += 1

    # The section faces should sit inside the part's height extent (Z in ~[-10,10] mapped to sheet y
    # around the section position 100). Sanity: their bbox height is ~20 mm (the box height).
    ys = [p[1] for poly in sg["section_faces"] for p in poly]
    assert (max(ys) - min(ys)) > 15.0, "section face height should reflect the 20mm box height"
    passed += 1

    # --- SECTION via a 2D cut_line dropped vertically across the parent front view ----
    # The front view projects the box (X->sheet x, Z->sheet y) centered at (100,100). A vertical
    # cutting line at sheet x=100 spanning the height cuts the part down the middle.
    sec2 = drawing.add_section(draw_id, fid,
                               cut_line=[[100.0, 80.0], [100.0, 120.0]],
                               scale=1.0, position=[260, 200])
    sg2 = sec2["view"]
    assert sg2["error"] is None, f"cut_line section error: {sg2['error']}"
    assert _nverts(sg2["visible"]) > 0
    assert len(sg2["section_faces"]) > 0, "cut_line section produced no hatched faces"
    print(f"[sd] section(cut_line): visible={len(sg2['visible'])} "
          f"section_faces={len(sg2['section_faces'])}")
    passed += 1

    # --- DETAIL on a corner region of the front view ----
    # The front view spans roughly x in [70,130], y in [90,110] (box 60x20 at center 100,100). A
    # circle around a corner (70,110) radius 18 crops the top-left corner.
    det = drawing.add_detail(draw_id, fid, center=[70.0, 110.0], radius=18.0, scale=2.0,
                             position=[100, 240])
    dg = det["view"]
    assert dg["error"] is None, f"detail error: {dg['error']}"
    assert _nverts(dg["visible"]) > 0, "detail view clipped away all geometry"
    assert dg["type"] == "detail" and dg["radius"] == 18.0
    # Magnified: at 2x the parent's 1x scale, a kept ~corner segment grows. The detail bbox width
    # should be no larger than 2 * (2*radius) = the circle diameter * mag.
    bb = dg["bbox"]
    assert bb is not None
    detail_w = bb[2] - bb[0]
    assert detail_w <= 2.0 * (2.0 * 18.0) + 1e-3, "detail content must fit the magnified circle"
    print(f"[sd] detail: visible={len(dg['visible'])} bbox_w={detail_w:.1f} "
          f"(<= magnified circle dia {2.0 * 2.0 * 18.0:.0f}) label={dg['label']}")
    passed += 1

    # detail content is clipped: every point lies within the magnified circle about the detail pos.
    cx, cy = dg["position"]
    for pl in dg["visible"]:
        for x, y in pl:
            r = math.hypot(x - cx, y - cy)
            assert r <= 2.0 * 18.0 + 1e-2, f"detail point {r:.2f} outside magnified circle"
    passed += 1

    # --- drawing_get attaches parent markers (detail_circle + section_line) ----
    full = drawing.get_drawing_full(draw_id)
    views = {v["view_id"]: v for sh in full["sheets"] for v in sh["views"]}
    parent_geom = views[fid]
    assert "detail_circles" in parent_geom and len(parent_geom["detail_circles"]) == 1, \
        "front view should carry a detail-circle marker"
    assert parent_geom["detail_circles"][0]["radius"] == 18.0
    assert "section_lines" in parent_geom and len(parent_geom["section_lines"]) == 1, \
        "front view should carry the cut_line section marker"
    print(f"[sd] parent markers: detail_circles={len(parent_geom['detail_circles'])} "
          f"section_lines={len(parent_geom['section_lines'])}")
    passed += 1

    # --- schema validation with the new view types ----
    errs = validate_drawing(full["tree"])
    assert errs == [], f"drawing failed schema validation: {errs}"
    passed += 1

    # --- error cases ----
    with pytest.raises(OpError):
        drawing.add_section(draw_id, "v999", plane={"normal": [1, 0, 0], "point": [0, 0, 0]})
    with pytest.raises(OpError):
        drawing.add_section(draw_id, fid)  # no plane and no cut_line
    with pytest.raises(OpError):
        drawing.add_detail(draw_id, fid, center=[0, 0], radius=-5.0)  # bad radius
    with pytest.raises(OpError):
        drawing.add_detail(draw_id, "v999", center=[0, 0], radius=5.0)  # unknown parent
    # A cut plane that misses the model entirely: contained error on the view, not a raise.
    miss = drawing.add_section(draw_id, fid,
                               plane={"normal": [1, 0, 0], "point": [10000, 0, 0]},
                               position=[400, 100])
    assert miss["view"]["error"] is not None and not miss["view"]["section_faces"], \
        "a cut that misses the model should set the view error + empty section faces"
    print("[sd] error cases: unknown parent / no plane / bad radius / missing cut all handled")
    passed += 1

    drawing.close_drawing(draw_id)
    print(f"\nSECTION+DETAIL TEST PASSED ({passed} checks)")


if __name__ == "__main__":
    test_section_and_detail()
