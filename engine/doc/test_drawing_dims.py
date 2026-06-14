"""W13 — drawing dimensions + BOM (engine/doc/drawing.py add_dim / remove_annotation / drawing_bom).

The headline guarantee: a linear dimension's VALUE is the TRUE model length, independent of the
view scale. We build a 60x40x20 box, drop a front view, pick two opposite corners of the projected
outline, add a linear dimension, and assert:

  * at scale 1.0 the value == the real model span (60 mm wide, 20 mm tall);
  * at scale 2.0 the SHEET distance doubles but the dimension VALUE stays the model span (scale
    independence — value = sheet_distance / scale);
  * an aligned dimension across the diagonal == the true 3D-projected diagonal length;
  * the annotation persists in drawing_get and round-trips through the schema validator;
  * remove_annotation deletes it.

BOM: a two-component assembly (two pegs of the SAME part + one plate) drawing -> drawing_bom groups
by source path, so the repeated peg gets quantity 2. A part drawing -> empty BOM.

Run:  python -m engine.doc.test_drawing_dims   (or pytest engine/doc/test_drawing_dims.py)
"""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

from engine.doc import drawing, session as docsess
from engine.doc.schema import validate_drawing


def _dim(v: float) -> dict:
    return {"value": v, "expr": None}


def _build_box(length: float, width: float, height: float) -> docsess.DocSession:
    """A simple length x width x height box at the origin (corner at origin, +X/+Y/+Z extents)."""
    s = docsess.new_session("part", name="Block")
    box = {"op": "add_feature", "feature": {
        "type": "box", "name": "Box1", "suppressed": False,
        "params": {"length": _dim(length), "width": _dim(width), "height": _dim(height)},
        "refs": {}}}
    docsess.apply_ops(s, None, [box])
    return s


def _outline_corners(view_geom: dict) -> tuple[list, list, list, list]:
    """The four bbox corners of a projected view (sheet mm): (min, x-max, y-max, max-diagonal)."""
    bb = view_geom["bbox"]
    assert bb is not None, "view has no bbox"
    minx, miny, maxx, maxy = bb
    bl = [minx, miny]
    br = [maxx, miny]
    tl = [minx, maxy]
    tr = [maxx, maxy]
    return bl, br, tl, tr


# --------------------------------------------------------------------------- dimension tests


def test_linear_dim_value_is_model_length() -> None:
    """A linear dim reads the TRUE model length at scale 1.0 AND a non-1.0 scale."""
    part = _build_box(60.0, 40.0, 20.0)
    res = drawing.new_drawing(part.doc_id, name="Block Drawing")
    draw_id = res["doc_id"]

    # FRONT view (looks +Y): screen +x = world +X (60 wide), screen +y = world +Z (20 tall).
    fr = drawing.add_view(draw_id, type="base", orientation="front", scale=1.0,
                          position=[100, 100], show_hidden=True)
    vid = fr["view_id"]
    bb = fr["view"]["bbox"]
    sheet_w = bb[2] - bb[0]
    sheet_h = bb[3] - bb[1]
    assert abs(sheet_w - 60.0) < 0.5, f"front sheet width should be ~60 at scale 1, got {sheet_w}"
    assert abs(sheet_h - 20.0) < 0.5, f"front sheet height should be ~20 at scale 1, got {sheet_h}"

    bl, br, tl, tr = _outline_corners(fr["view"])

    # Horizontal linear dim across the bottom edge -> model width 60.
    out = drawing.add_dim(draw_id, vid, type="linear_dim", a=bl, b=br)
    ann = out["annotation"]
    assert abs(ann["value"] - 60.0) < 0.5, f"horizontal dim value should be ~60 (model), got {ann['value']}"
    assert ann["id"] == "a1" and ann["type"] == "linear_dim"
    print(f"[dims] scale=1.0 horizontal linear dim value={ann['value']:.3f} mm (model width 60)")

    # The full drawing is returned for one-round-trip re-render.
    assert "drawing" in out and out["drawing"]["doc_id"] == draw_id

    # Vertical linear dim up the left edge -> model height 20.
    out2 = drawing.add_dim(draw_id, vid, type="linear_dim", a=bl, b=tl)
    assert abs(out2["annotation"]["value"] - 20.0) < 0.5, \
        f"vertical dim value should be ~20 (model), got {out2['annotation']['value']}"
    print(f"[dims] scale=1.0 vertical linear dim value={out2['annotation']['value']:.3f} mm (model height 20)")

    # --- NON-1.0 SCALE: re-scale the view to 2.0; the sheet doubles but the VALUE stays the model
    # length. We must re-pick corners off the rescaled geometry.
    g2 = drawing.set_view(draw_id, vid, scale=2.0, position=[100, 100])["view"]
    bb2 = g2["bbox"]
    sheet_w2 = bb2[2] - bb2[0]
    assert abs(sheet_w2 - 120.0) < 1.0, f"at scale 2 the SHEET width should be ~120, got {sheet_w2}"
    bl2, br2, tl2, tr2 = _outline_corners(g2)
    out3 = drawing.add_dim(draw_id, vid, type="linear_dim", a=bl2, b=br2)
    assert abs(out3["annotation"]["value"] - 60.0) < 0.5, \
        f"at scale 2 the horizontal dim VALUE must STILL be the 60 mm model width, got {out3['annotation']['value']}"
    print(f"[dims] scale=2.0 sheet_width={sheet_w2:.1f}mm but dim value={out3['annotation']['value']:.3f} mm "
          f"(scale-independent model length)")

    drawing.close_drawing(draw_id)
    print("[dims] linear dim value is the model length at scale 1.0 AND 2.0 -> PASS")


def test_aligned_dim_diagonal() -> None:
    """An aligned dim measures the true point-to-point distance, divided by scale."""
    part = _build_box(60.0, 40.0, 20.0)
    res = drawing.new_drawing(part.doc_id, name="Block Drawing")
    draw_id = res["doc_id"]
    fr = drawing.add_view(draw_id, type="base", orientation="front", scale=1.0,
                          position=[0, 0], show_hidden=False)
    vid = fr["view_id"]
    bl, br, tl, tr = _outline_corners(fr["view"])
    out = drawing.add_dim(draw_id, vid, type="aligned_dim", a=bl, b=tr)
    expect = math.hypot(60.0, 20.0)
    assert abs(out["annotation"]["value"] - expect) < 1.0, \
        f"aligned diagonal should be ~{expect:.2f}, got {out['annotation']['value']}"
    print(f"[dims] aligned diagonal dim value={out['annotation']['value']:.3f} mm (~hypot(60,20)={expect:.3f})")
    drawing.close_drawing(draw_id)


def test_annotation_roundtrip_and_remove() -> None:
    """A dim persists in drawing_get, validates, and remove_annotation deletes it."""
    part = _build_box(50.0, 30.0, 10.0)
    res = drawing.new_drawing(part.doc_id, name="Block Drawing")
    draw_id = res["doc_id"]
    fr = drawing.add_view(draw_id, type="base", orientation="front", scale=1.0,
                          position=[100, 100], show_hidden=True)
    vid = fr["view_id"]
    bl, br, tl, tr = _outline_corners(fr["view"])
    out = drawing.add_dim(draw_id, vid, type="linear_dim", a=bl, b=br, pos=[120, 80])
    ann_id = out["annotation"]["id"]
    assert out["annotation"]["pos"] == [120.0, 80.0], "explicit pos must be honored"

    # drawing_get round-trips the annotation onto the view geometry.
    full = drawing.get_drawing_full(draw_id)
    view = full["sheets"][0]["views"][0]
    anns = view.get("annotations") or []
    assert any(a["id"] == ann_id for a in anns), f"annotation {ann_id} missing from drawing_get"
    print(f"[dims] drawing_get round-trips annotation {ann_id}: {anns[0]}")

    # Schema validation accepts the annotated drawing.
    errs = validate_drawing(full["tree"])
    assert errs == [], f"annotated drawing failed schema validation: {errs}"
    print(f"[dims] validate_drawing(annotated) -> clean")

    # remove_annotation deletes it.
    rm = drawing.remove_annotation(draw_id, vid, ann_id)
    assert rm["removed"]
    full2 = drawing.get_drawing_full(draw_id)
    anns2 = full2["sheets"][0]["views"][0].get("annotations") or []
    assert not any(a["id"] == ann_id for a in anns2), "annotation should be gone after remove"
    print(f"[dims] remove_annotation({ann_id}) -> annotations now {len(anns2)}")
    drawing.close_drawing(draw_id)


# --------------------------------------------------------------------------- BOM tests


def _save_part(name: str, ops: list, path: str) -> None:
    from engine.doc import stepplus
    s = docsess.new_session("part", name=name)
    docsess.apply_ops(s, None, ops)
    stepplus.save(s, path)
    docsess.close_session(s.doc_id)


def test_bom_assembly_and_part() -> None:
    """An assembly drawing's BOM groups identical parts (quantity); a part drawing's BOM is empty."""
    tmp = Path(tempfile.mkdtemp(prefix="aicad_bom_"))
    plate_p = str(tmp / "plate.step+")
    peg_p = str(tmp / "peg.step+")
    _save_part("plate", [
        {"op": "add_feature", "feature": {"type": "box", "name": "Plate", "suppressed": False,
            "params": {"length": _dim(40), "width": _dim(40), "height": _dim(10)}, "refs": {}}},
    ], plate_p)
    _save_part("peg", [
        {"op": "add_feature", "feature": {"type": "cylinder", "name": "Peg", "suppressed": False,
            "params": {"radius": _dim(4), "height": _dim(20)}, "refs": {}}},
    ], peg_p)

    # Assembly: one plate + TWO pegs (same source path -> grouped to quantity 2).
    asm = docsess.new_session("assembly", name="PegBoard")
    docsess.apply_ops(asm, None, [
        {"op": "asm_insert_component", "source": {"path": plate_p}},
        {"op": "asm_insert_component", "source": {"path": peg_p},
         "transform": [1, 0, 0, 10, 0, 1, 0, 0, 0, 0, 1, 12, 0, 0, 0, 1]},
        {"op": "asm_insert_component", "source": {"path": peg_p},
         "transform": [1, 0, 0, -10, 0, 1, 0, 0, 0, 0, 1, 12, 0, 0, 0, 1]},
    ])

    res = drawing.new_drawing(asm.doc_id, name="PegBoard Drawing")
    draw_id = res["doc_id"]
    bom = drawing.drawing_bom(draw_id)
    assert bom["is_assembly"], f"assembly drawing BOM must be is_assembly=True: {bom}"
    rows = bom["rows"]
    assert rows, f"assembly BOM should have rows, got {bom}"
    by_name = {r["name"]: r for r in rows}
    assert "peg" in by_name and by_name["peg"]["quantity"] == 2, \
        f"two identical pegs must group to quantity 2: {rows}"
    assert "plate" in by_name and by_name["plate"]["quantity"] == 1, f"one plate: {rows}"
    # item ordinals are 1..N, sources are the saved paths.
    assert sorted(r["item"] for r in rows) == list(range(1, len(rows) + 1)), rows
    print(f"[bom] assembly BOM rows: {rows}")
    drawing.close_drawing(draw_id)

    # A PART drawing -> empty BOM (clean, not an error).
    part = _build_box(20.0, 20.0, 20.0)
    pres = drawing.new_drawing(part.doc_id, name="Cube Drawing")
    pbom = drawing.drawing_bom(pres["doc_id"])
    assert pbom["rows"] == [] and not pbom["is_assembly"], f"part drawing BOM must be empty: {pbom}"
    print(f"[bom] part drawing BOM is empty (is_assembly={pbom['is_assembly']}) -> PASS")
    drawing.close_drawing(pres["doc_id"])

    docsess.close_session(asm.doc_id)


if __name__ == "__main__":
    test_linear_dim_value_is_model_length()
    test_aligned_dim_diagonal()
    test_annotation_roundtrip_and_remove()
    test_bom_assembly_and_part()
    print("\nDRAWING DIMS + BOM TEST PASSED")
