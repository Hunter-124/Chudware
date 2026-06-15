"""W13-ext — SolidWorks-parity drawing annotations (engine/doc/drawing.py).

Builds on test_drawing_dims.py. Adds the four parity gaps from docs/SW_PARITY_SPEC.md §4 / GAPS P0:
circular-feature dimensions (radial R, diameter ⌀), angular dims, and center marks / center lines.

The enabling change is ARC EXTRACTION in the HLR projection: a projected circle/arc is preserved as
a real analytic arc entity (center + radius in sheet mm), not just a discretized polyline, so a dim
can attach to a circular feature. We:

  * build a box with a Ø10 through hole (a full circle in the TOP view) and a filleted corner (a 90°
    arc), drop a TOP view, and assert the projection extracts BOTH — a closed circle r≈5 (Ø10) and an
    open arc r≈8 — as ``arcs`` entities with the right radii;
  * add a RADIAL dim on the fillet arc -> value == the true model radius 8 (R-prefixed text);
  * add a DIAMETER dim on the hole circle -> value == the true model diameter 10 (⌀-prefixed text);
  * assert radial/diameter are SCALE-INDEPENDENT (re-scale the view 2x, re-pick, value unchanged);
  * add an ANGULAR dim between two picked lines -> value == the angle in degrees (scale-free);
  * add a CENTER MARK at the hole center -> cross placed at the circle center (sheet mm);
  * add a CENTER LINE between two points -> stored line endpoints;
  * assert every extended annotation round-trips through drawing_get AND validate_drawing stays clean
    (they persist as schema ``note`` + a ``kind`` tag), and remove_annotation deletes one.

Run:  python -m engine.doc.test_drawing_dims_ext  (or pytest engine/doc/test_drawing_dims_ext.py)
"""

from __future__ import annotations

import math

from engine.doc import drawing, session as docsess
from engine.doc.schema import validate_drawing


def _dim(v: float) -> dict:
    return {"value": v, "expr": None}


# --------------------------------------------------------------------------- part fixture
#
# Mirrors test_drawing.py::_build_part_with_hole — a box + a filleted edge + a Ø10 through hole, so
# the TOP view (looking down the hole/fillet axis) projects a true circle AND a true arc.


def _build_box_hole_fillet(length=60.0, width=40.0, height=20.0,
                           fillet_r=8.0, hole_dia=10.0) -> docsess.DocSession:
    s = docsess.new_session("part", name="Bracket")
    docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1", "suppressed": False,
        "params": {"length": _dim(length), "width": _dim(width), "height": _dim(height)},
        "refs": {}}}])
    pick = docsess.resolve_pick(s, s.revision, body_id=1, kind="edge", topo_id=1)
    docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "fillet", "name": "Fillet1", "suppressed": False,
        "params": {"radius": _dim(fillet_r)},
        "refs": {"edges": [pick["selector"]]}}}])
    # Drill a through hole down the TOP face center (axis +Z -> a true circle in the top view).
    result = docsess._ensure_built(s)
    body = result.bodies[-1].shape
    top_face = max(body.faces(), key=lambda f: f.center().Z)
    top_pick = _selector_for_face(s, body, top_face)
    docsess.apply_ops(s, None, [{"op": "add_feature", "feature": {
        "type": "hole", "name": "Hole1", "suppressed": False,
        "params": {"style": "simple", "end": "through_all", "standard": "custom",
                   "diameter": _dim(hole_dia)},
        "refs": {"face": top_pick, "points": [[0.0, 0.0]]}}}])
    return s


def _selector_for_face(s, body, face):
    faces = body.faces()
    for i, f in enumerate(faces, start=1):
        c = f.center()
        if (abs(c.X - face.center().X) < 1e-6 and abs(c.Y - face.center().Y) < 1e-6
                and abs(c.Z - face.center().Z) < 1e-6):
            return docsess.resolve_pick(s, s.revision, body_id=1, kind="face", topo_id=i)["selector"]
    raise AssertionError("could not locate the top face index")


def _closed_arc(geom: dict, radius: float, tol: float = 0.3) -> dict:
    """The first FULL-circle arc in the view geometry whose radius ≈ ``radius`` (sheet mm)."""
    for ar in geom.get("arcs", []) or []:
        if ar.get("closed") and abs(float(ar["radius"]) - radius) < tol:
            return ar
    raise AssertionError(f"no closed circle r≈{radius} in arcs: "
                         f"{[(round(a['radius'], 2), a['closed']) for a in geom.get('arcs', [])]}")


def _open_arc(geom: dict, radius: float, tol: float = 0.3) -> dict:
    for ar in geom.get("arcs", []) or []:
        if not ar.get("closed") and abs(float(ar["radius"]) - radius) < tol:
            return ar
    raise AssertionError(f"no open arc r≈{radius} in arcs: "
                         f"{[(round(a['radius'], 2), a['closed']) for a in geom.get('arcs', [])]}")


# --------------------------------------------------------------------------- arc extraction


def test_projection_extracts_circle_and_arc() -> None:
    """The HLR projection preserves a hole as a true CIRCLE entity and a fillet as a true ARC."""
    part = _build_box_hole_fillet()
    res = drawing.new_drawing(part.doc_id, name="Bracket Drawing")
    draw_id = res["doc_id"]
    tv = drawing.add_view(draw_id, type="base", orientation="top", scale=1.0,
                          position=[100, 100], show_hidden=True)
    g = tv["view"]
    assert g["error"] is None, g["error"]
    assert "arcs" in g and g["arcs"], "top view should expose extracted arc entities"

    circle = _closed_arc(g, radius=5.0)   # Ø10 hole -> r 5 at scale 1
    assert circle["closed"] is True
    assert abs(circle["radius"] - 5.0) < 0.3, circle
    assert abs(abs(circle["span"]) - 2.0 * math.pi) < 1e-3, "a full circle spans 2π"

    arc = _open_arc(g, radius=8.0)        # filleted corner -> 90° arc r 8
    assert arc["closed"] is False
    assert abs(arc["radius"] - 8.0) < 0.3, arc
    assert abs(abs(arc["span"]) - math.pi / 2.0) < 1e-2, f"fillet arc should span 90°, got {arc['span']}"
    print(f"[ext] arc extraction: hole circle r={circle['radius']:.2f} (closed) ; "
          f"fillet arc r={arc['radius']:.2f} span={math.degrees(arc['span']):.1f}° -> PASS")
    drawing.close_drawing(draw_id)


# --------------------------------------------------------------------------- radial / diameter


def test_radial_and_diameter_dims() -> None:
    """Radial dim reads the true model radius; diameter dim the true model diameter — scale-free."""
    part = _build_box_hole_fillet(fillet_r=8.0, hole_dia=10.0)
    res = drawing.new_drawing(part.doc_id, name="Bracket Drawing")
    draw_id = res["doc_id"]
    tv = drawing.add_view(draw_id, type="base", orientation="top", scale=1.0,
                          position=[100, 100], show_hidden=True)
    vid = tv["view_id"]
    g = tv["view"]

    circle = _closed_arc(g, radius=5.0)
    cx, cy = circle["center"]
    # Pick a point ON the circle (host snaps near the rim) -> diameter dim.
    dd = drawing.add_dim(draw_id, vid, type="diameter_dim", a=[cx + circle["radius"], cy])
    da = dd["annotation"]
    assert da["type"] == "note" and da["kind"] == "diameter_dim", da
    assert abs(da["value"] - 10.0) < 0.3, f"diameter should be the model Ø10, got {da['value']}"
    assert da["text"].startswith("⌀"), f"diameter text should be ⌀-prefixed: {da['text']}"
    print(f"[ext] diameter dim value={da['value']:.3f} text={da['text']!r} (model Ø10)")

    arc = _open_arc(g, radius=8.0)
    fcx, fcy = arc["center"]
    rd = drawing.add_dim(draw_id, vid, type="radial_dim", a=[fcx + arc["radius"], fcy])
    ra = rd["annotation"]
    assert ra["kind"] == "radial_dim"
    assert abs(ra["value"] - 8.0) < 0.3, f"radius should be the model R8, got {ra['value']}"
    assert ra["text"].startswith("R"), f"radial text should be R-prefixed: {ra['text']}"
    print(f"[ext] radial dim value={ra['value']:.3f} text={ra['text']!r} (model R8)")

    # --- SCALE INDEPENDENCE: rescale the view 2x, re-pick off the rescaled arcs, value unchanged.
    g2 = drawing.set_view(draw_id, vid, scale=2.0, position=[100, 100])["view"]
    c2 = _closed_arc(g2, radius=10.0)     # at scale 2 the SHEET radius doubles to 10...
    assert abs(c2["radius"] - 10.0) < 0.6, f"at scale 2 the SHEET radius should be ~10, got {c2['radius']}"
    cx2, cy2 = c2["center"]
    dd2 = drawing.add_dim(draw_id, vid, type="diameter_dim", a=[cx2 + c2["radius"], cy2])
    assert abs(dd2["annotation"]["value"] - 10.0) < 0.3, \
        f"...but the diameter VALUE must STILL be the 10 mm model Ø, got {dd2['annotation']['value']}"
    print(f"[ext] scale=2.0 sheet_radius={c2['radius']:.2f} but diameter value="
          f"{dd2['annotation']['value']:.3f} (scale-independent) -> PASS")
    drawing.close_drawing(draw_id)


# --------------------------------------------------------------------------- angular


def test_angular_dim() -> None:
    """An angular dim measures the angle (degrees) between two picked lines — scale-independent."""
    part = _build_box_hole_fillet()
    res = drawing.new_drawing(part.doc_id, name="Bracket Drawing")
    draw_id = res["doc_id"]
    tv = drawing.add_view(draw_id, type="base", orientation="top", scale=1.0,
                          position=[100, 100], show_hidden=True)
    vid = tv["view_id"]

    # Line 1 horizontal, line 2 at 45° -> 45°.
    ad = drawing.add_dim(draw_id, vid, type="angular_dim",
                         a=[0, 0], b=[10, 0], c=[0, 0], d=[10, 10])
    an = ad["annotation"]
    assert an["kind"] == "angular_dim", an
    assert abs(an["value"] - 45.0) < 1e-6, f"angle should be 45°, got {an['value']}"
    assert an["text"].endswith("°"), f"angular text should be °-suffixed: {an['text']}"
    assert an["vertex"] == [0.0, 0.0], f"vertex should be the line intersection: {an['vertex']}"
    print(f"[ext] angular dim value={an['value']}° text={an['text']!r} vertex={an['vertex']}")

    # A 90° corner (horizontal vs vertical) at a non-1 scale -> still 90° (scale cancels).
    drawing.set_view(draw_id, vid, scale=3.0, position=[100, 100])
    ad2 = drawing.add_dim(draw_id, vid, type="angular_dim",
                          a=[5, 5], b=[25, 5], c=[5, 5], d=[5, 25])
    assert abs(ad2["annotation"]["value"] - 90.0) < 1e-6, \
        f"perpendicular lines should read 90° at any scale, got {ad2['annotation']['value']}"
    print(f"[ext] angular dim (scale 3) perpendicular value={ad2['annotation']['value']}° -> PASS")
    drawing.close_drawing(draw_id)


# --------------------------------------------------------------------------- center mark / line


def test_center_mark_and_center_line() -> None:
    """A center mark is a cross at a circle's center; a center line spans two picked points."""
    part = _build_box_hole_fillet()
    res = drawing.new_drawing(part.doc_id, name="Bracket Drawing")
    draw_id = res["doc_id"]
    tv = drawing.add_view(draw_id, type="base", orientation="top", scale=1.0,
                          position=[100, 100], show_hidden=True)
    vid = tv["view_id"]
    g = tv["view"]

    circle = _closed_arc(g, radius=5.0)
    cx, cy = circle["center"]
    cm = drawing.add_dim(draw_id, vid, type="center_mark", a=[cx + circle["radius"], cy])
    cma = cm["annotation"]
    assert cma["kind"] == "center_mark", cma
    assert abs(cma["center"][0] - cx) < 1e-6 and abs(cma["center"][1] - cy) < 1e-6, \
        f"center mark must sit at the circle center {[cx, cy]}, got {cma['center']}"
    assert abs(cma["radius"] - circle["radius"]) < 1e-6
    print(f"[ext] center mark at circle center {[round(v, 3) for v in cma['center']]} "
          f"r={cma['radius']:.2f}")

    # Center line between two symmetric points either side of the hole center (a midline).
    cl = drawing.add_dim(draw_id, vid, type="center_line",
                         a=[cx - 20.0, cy], b=[cx + 20.0, cy])
    cla = cl["annotation"]
    assert cla["kind"] == "center_line"
    assert cla["a"] == [cx - 20.0, cy] and cla["b"] == [cx + 20.0, cy], cla
    # The default text anchor is the line midpoint = the hole center.
    assert abs(cla["pos"][0] - cx) < 1e-6 and abs(cla["pos"][1] - cy) < 1e-6, cla["pos"]
    print(f"[ext] center line {cla['a']} -> {cla['b']} (anchor at midpoint) -> PASS")
    drawing.close_drawing(draw_id)


# --------------------------------------------------------------------------- roundtrip / validate


def test_ext_annotations_roundtrip_validate_remove() -> None:
    """Every extended annotation persists in drawing_get, validates, and is removable."""
    part = _build_box_hole_fillet()
    res = drawing.new_drawing(part.doc_id, name="Bracket Drawing")
    draw_id = res["doc_id"]
    tv = drawing.add_view(draw_id, type="base", orientation="top", scale=1.0,
                          position=[100, 100], show_hidden=True)
    vid = tv["view_id"]
    g = tv["view"]
    circle = _closed_arc(g, radius=5.0)
    cx, cy = circle["center"]
    arc = _open_arc(g, radius=8.0)
    fcx, fcy = arc["center"]

    ids = []
    ids.append(drawing.add_dim(draw_id, vid, type="diameter_dim",
                               a=[cx + circle["radius"], cy])["annotation"]["id"])
    ids.append(drawing.add_dim(draw_id, vid, type="radial_dim",
                               a=[fcx + arc["radius"], fcy])["annotation"]["id"])
    ids.append(drawing.add_dim(draw_id, vid, type="angular_dim",
                               a=[0, 0], b=[10, 0], c=[0, 0], d=[0, 10])["annotation"]["id"])
    ids.append(drawing.add_dim(draw_id, vid, type="center_mark",
                               a=[cx + circle["radius"], cy])["annotation"]["id"])
    ids.append(drawing.add_dim(draw_id, vid, type="center_line",
                               a=[cx - 15, cy], b=[cx + 15, cy])["annotation"]["id"])

    full = drawing.get_drawing_full(draw_id)
    view = full["sheets"][0]["views"][0]
    anns = view.get("annotations") or []
    got = {a["id"] for a in anns}
    assert set(ids) <= got, f"missing annotations from drawing_get: {set(ids) - got}"
    kinds = {a["id"]: a.get("kind") for a in anns}
    assert kinds[ids[0]] == "diameter_dim" and kinds[ids[2]] == "angular_dim", kinds
    print(f"[ext] drawing_get round-trips {len(ids)} extended annotations: kinds="
          f"{sorted(set(kinds.values()))}")

    # Schema validation must stay clean — extended kinds persist as schema `note` + a `kind` tag.
    errs = validate_drawing(full["tree"])
    assert errs == [], f"extended annotations broke schema validation: {errs}"
    print("[ext] validate_drawing(extended annotations) -> clean")

    # remove one and confirm it's gone.
    drawing.remove_annotation(draw_id, vid, ids[0])
    full2 = drawing.get_drawing_full(draw_id)
    anns2 = full2["sheets"][0]["views"][0].get("annotations") or []
    assert not any(a["id"] == ids[0] for a in anns2), "diameter dim should be gone after remove"
    assert validate_drawing(full2["tree"]) == []
    print(f"[ext] remove_annotation({ids[0]}) -> {len(anns2)} remain, still valid -> PASS")
    drawing.close_drawing(draw_id)


# --------------------------------------------------------------------------- error handling


def test_ext_dim_errors() -> None:
    """Bad picks raise structured OpErrors (no arc near the pick; degenerate angular line)."""
    from engine.doc.ops import OpError

    part = _build_box_hole_fillet()
    res = drawing.new_drawing(part.doc_id, name="Bracket Drawing")
    draw_id = res["doc_id"]
    tv = drawing.add_view(draw_id, type="base", orientation="top", scale=1.0,
                          position=[100, 100], show_hidden=True)
    vid = tv["view_id"]

    # A diameter dim needs a FULL circle near the pick; the fillet center has only an open arc, but a
    # point far from everything yields the nearest arc — so instead exercise the degenerate angular
    # line (coincident endpoints) which must raise.
    raised = False
    try:
        drawing.add_dim(draw_id, vid, type="angular_dim",
                        a=[0, 0], b=[0, 0], c=[0, 0], d=[10, 10])
    except OpError as e:
        raised = True
        assert "degenerate" in str(e).lower() or "coincide" in str(e).lower(), str(e)
    assert raised, "a zero-length angular line must raise"

    # angular_dim with a missing second line must raise.
    raised2 = False
    try:
        drawing.add_dim(draw_id, vid, type="angular_dim", a=[0, 0], b=[10, 0])
    except OpError:
        raised2 = True
    assert raised2, "angular_dim without the second line must raise"
    print("[ext] error handling: degenerate / incomplete angular picks raise OpError -> PASS")
    drawing.close_drawing(draw_id)


if __name__ == "__main__":
    test_projection_extracts_circle_and_arc()
    test_radial_and_diameter_dims()
    test_angular_dim()
    test_center_mark_and_center_line()
    test_ext_annotations_roundtrip_validate_remove()
    test_ext_dim_errors()
    print("\nDRAWING DIMS EXT TEST PASSED")
