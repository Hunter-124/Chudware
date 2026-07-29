"""Tests for services.plm.visual_diff (the 2D half of the PLM graphical diff).

These pin observable contracts rather than plumbing:

* the multiset stroke diff (identical revisions -> nothing changed; an appended
  stroke -> exactly one add; a *moved* stroke -> one add AND one remove, which is
  what proves position is part of a stroke's identity);
* a changed dimension value surfacing as removed text + added text;
* the SHARED frame — unchanged geometry must occupy the identical pixels whichever
  revision is passed as A, which is the property that lets a reviewer flip between
  two renders. Asserted by rendering (A, B) and (B, A) and comparing the exact set
  of unchanged-grey pixel coordinates;
* the registered pixel diff's counts and its red/green placement at known pixels;
* graceful degradation to ``None`` (never an exception) when Pillow is absent.
"""
from __future__ import annotations

import sys

from PIL import Image, ImageDraw

from services.plm import visual_diff


# ---------------------------------------------------------------------------
# fixtures — drawing_get-shaped payloads (core/doc/session.cpp drawing_render_png)
# ---------------------------------------------------------------------------

def _drawing(visible: list, texts: list | None = None) -> dict:
    view: dict = {"visible": visible, "hidden": [], "hatch": []}
    if texts is not None:
        view["annotations"] = [{"draw": {"texts": texts}}]
    return {"sheets": [{"title_block": {"title": "BRACKET"}, "views": [view]}]}


def _drawing_a() -> dict:
    return _drawing(
        [[[0.0, 0.0], [50.0, 0.0], [50.0, 30.0], [0.0, 30.0], [0.0, 0.0]],
         [[10.0, 10.0], [20.0, 10.0]]],
        texts=[{"pos": [25.0, -8.0], "text": "50.00", "h": 3.5}],
    )


# ---------------------------------------------------------------------------
# fixtures — canonical ECAD documents
# ---------------------------------------------------------------------------

def _pcb_a() -> dict:
    return {
        "schema": "aicad.pcb/1",
        "outline": {"shapes": [
            {"type": "line", "p1": [0, 0], "p2": [40, 0]},
            {"type": "line", "p1": [40, 0], "p2": [40, 30]},
            {"type": "line", "p1": [40, 30], "p2": [0, 30]},
            {"type": "line", "p1": [0, 30], "p2": [0, 0]},
        ], "closed": True},
        "footprints": [{"id": "fp1", "ref": "R1", "pos": [10, 10], "angle": 0,
                        "pads": [{"id": "p1", "pos": [-0.8, 0], "size": [1.0, 1.2],
                                  "shape": "rect", "type": "smd", "layers": ["F.Cu"]},
                                 {"id": "p2", "pos": [0.8, 0], "size": [1.0, 1.2],
                                  "shape": "rect", "type": "smd", "layers": ["F.Cu"]}]}],
        "tracks": [{"id": "t1", "p1": [10.8, 10], "p2": [25, 10],
                    "width": 0.25, "layer": "F.Cu", "net": "n1"}],
        "vias": [], "zones": [], "graphics": [],
    }


# ---------------------------------------------------------------------------
# drawing_diff_image
# ---------------------------------------------------------------------------

def test_drawing_identical_revisions_report_no_change(tmp_path):
    out = tmp_path / "sub" / "same.png"
    res = visual_diff.drawing_diff_image(_drawing_a(), _drawing_a(), str(out))
    assert res is not None
    assert res["added"] == 0 and res["removed"] == 0
    assert res["unchanged"] > 0
    assert out.exists()
    assert Image.open(out).size == (900, 640)


def test_drawing_appended_polyline_is_one_add(tmp_path):
    b = _drawing_a()
    b["sheets"][0]["views"][0]["visible"].append([[5.0, 20.0], [15.0, 25.0]])
    res = visual_diff.drawing_diff_image(_drawing_a(), b, str(tmp_path / "add.png"))
    assert res is not None
    assert (res["added"], res["removed"]) == (1, 0)


def test_drawing_moved_polyline_is_one_add_and_one_remove(tmp_path):
    """Position is part of a stroke's key, so a move is red-then-green, not 'changed'."""
    b = _drawing_a()
    b["sheets"][0]["views"][0]["visible"][1] = [[10.0, 18.0], [20.0, 18.0]]
    res = visual_diff.drawing_diff_image(_drawing_a(), b, str(tmp_path / "move.png"))
    assert res is not None
    assert (res["added"], res["removed"]) == (1, 1)


def test_drawing_duplicate_stroke_collapsing_to_one_is_a_removal(tmp_path):
    a = _drawing_a()
    a["sheets"][0]["views"][0]["visible"].append([[10.0, 10.0], [20.0, 10.0]])
    res = visual_diff.drawing_diff_image(a, _drawing_a(), str(tmp_path / "dup.png"))
    assert res is not None
    assert (res["added"], res["removed"]) == (0, 1)


def test_drawing_changed_dimension_value_is_one_add_one_remove(tmp_path):
    b = _drawing_a()
    b["sheets"][0]["views"][0]["annotations"][0]["draw"]["texts"][0]["text"] = "52.00"
    res = visual_diff.drawing_diff_image(_drawing_a(), b, str(tmp_path / "dim.png"))
    assert res is not None
    assert (res["added"], res["removed"]) == (1, 1)


def test_drawing_arc_equals_its_polyline_tessellation(tmp_path):
    """An arc must tessellate exactly as drawing_render_png does, or every arc
    would read as a spurious add+remove pair."""
    import math
    center, radius, a0, a1 = (5.0, 5.0), 4.0, 0.0, math.pi / 2
    n = max(10, int(abs(a1 - a0) / 0.3))
    poly = [[center[0] + radius * math.cos(a0 + (a1 - a0) * i / n),
             center[1] + radius * math.sin(a0 + (a1 - a0) * i / n)] for i in range(n + 1)]
    a = _drawing([[[0, 0], [10, 0]]], texts=[])
    a["sheets"][0]["views"][0]["annotations"][0]["draw"]["arcs"] = [
        {"center": list(center), "radius": radius, "a0": a0, "a1": a1}]
    b = _drawing([[[0, 0], [10, 0]]], texts=[])
    b["sheets"][0]["views"][0]["annotations"][0]["draw"]["segments"] = [poly]
    res = visual_diff.drawing_diff_image(a, b, str(tmp_path / "arc.png"))
    assert res is not None
    assert (res["added"], res["removed"]) == (0, 0)


def test_drawing_title_block_change_counts_but_does_not_move_the_frame(tmp_path):
    b = _drawing_a()
    b["sheets"][0]["title_block"]["title"] = "BRACKET REV B"
    out_a = tmp_path / "tb_a.png"
    out_b = tmp_path / "tb_b.png"
    base = visual_diff.drawing_diff_image(_drawing_a(), _drawing_a(), str(out_a))
    res = visual_diff.drawing_diff_image(_drawing_a(), b, str(out_b))
    assert base is not None and res is not None
    assert (res["added"], res["removed"]) == (1, 1)
    # geometry framing is untouched by a title-block-only change
    assert res["unchanged"] == base["unchanged"] - 1


def test_drawing_empty_both_sides_returns_none(tmp_path):
    assert visual_diff.drawing_diff_image({}, {}, str(tmp_path / "none.png")) is None


def test_drawing_malformed_payload_does_not_raise(tmp_path):
    junk = {"sheets": [{"views": [{"visible": [None, [[1]], "x"], "annotations": ["x"]}]}]}
    # nothing drawable on either side -> None, but no exception
    assert visual_diff.drawing_diff_image(junk, junk, str(tmp_path / "junk.png")) is None
    res = visual_diff.drawing_diff_image(junk, _drawing_a(), str(tmp_path / "junk2.png"))
    assert res is not None and res["added"] > 0


# ---------------------------------------------------------------------------
# shared frame
# ---------------------------------------------------------------------------

def _pixels_of(path, color) -> set:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    px = img.load()
    return {(x, y) for y in range(h) for x in range(w) if px[x, y] == color}


def test_shared_frame_unchanged_pixels_identical_under_argument_swap(tmp_path):
    """The load-bearing framing property: both revisions are framed on the UNION
    bbox, so the geometry common to A and B lands on exactly the same pixels
    whether the call is (A, B) or (B, A) — only the red/green roles swap."""
    a = _drawing_a()
    b = _drawing_a()
    # a far-away extra stroke that materially enlarges the union bbox
    b["sheets"][0]["views"][0]["visible"].append([[400.0, 300.0], [420.0, 320.0]])

    fwd = tmp_path / "fwd.png"
    rev = tmp_path / "rev.png"
    r1 = visual_diff.drawing_diff_image(a, b, str(fwd))
    r2 = visual_diff.drawing_diff_image(b, a, str(rev))
    assert r1 == {"path": str(fwd), "added": 1, "removed": 0, "unchanged": r1["unchanged"]}
    assert (r2["added"], r2["removed"]) == (0, 1)

    grey_fwd = _pixels_of(fwd, visual_diff.COLOR_UNCHANGED)
    grey_rev = _pixels_of(rev, visual_diff.COLOR_UNCHANGED)
    assert grey_fwd, "unchanged geometry must actually be drawn"
    assert grey_fwd == grey_rev

    # and the differing stroke swaps colour rather than moving
    assert _pixels_of(fwd, visual_diff.COLOR_ADDED) == _pixels_of(rev, visual_diff.COLOR_REMOVED)


# ---------------------------------------------------------------------------
# pixel_diff_image
# ---------------------------------------------------------------------------

def _img_with_rects(path, size, rects) -> None:
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    for box in rects:
        d.rectangle(box, fill=(0, 0, 0))
    img.save(path)


def test_pixel_diff_counts_only_changed_ink_and_colours_it(tmp_path):
    a, b, out = tmp_path / "a.png", tmp_path / "b.png", tmp_path / "d.png"
    shared = [0, 0, 3, 3]                 # 4x4 = 16 px of ink in both
    _img_with_rects(a, (40, 40), [shared, [5, 5, 14, 14]])    # 10x10 only in A
    _img_with_rects(b, (40, 40), [shared, [20, 20, 29, 29]])  # 10x10 only in B

    res = visual_diff.pixel_diff_image(str(a), str(b), str(out))
    assert res is not None
    assert res["changed_px"] == 200        # 100 removed + 100 added
    assert res["total_px"] == 1600
    assert res["changed_pct"] == 12.5

    px = Image.open(out).convert("RGB").load()
    assert px[10, 10] == visual_diff.COLOR_REMOVED
    assert px[25, 25] == visual_diff.COLOR_ADDED
    assert px[2, 2] == visual_diff.COLOR_UNCHANGED
    assert px[38, 2] == (255, 255, 255)


def test_pixel_diff_identical_images_report_zero(tmp_path):
    a, out = tmp_path / "s.png", tmp_path / "so.png"
    _img_with_rects(a, (32, 32), [[4, 4, 9, 9]])
    res = visual_diff.pixel_diff_image(str(a), str(a), str(out))
    assert res is not None and res["changed_px"] == 0 and res["changed_pct"] == 0.0


def test_pixel_diff_pads_mismatched_sizes_without_resizing(tmp_path):
    """Padding, not resizing: the shared top-left ink stays registered, so only the
    extra ink counts as changed."""
    a, b, out = tmp_path / "pa.png", tmp_path / "pb.png", tmp_path / "po.png"
    _img_with_rects(a, (20, 20), [[0, 0, 4, 4]])            # 25 px
    _img_with_rects(b, (40, 30), [[0, 0, 4, 4], [10, 10, 14, 14]])  # 25 shared + 25 new
    res = visual_diff.pixel_diff_image(str(a), str(b), str(out))
    assert res is not None
    assert res["total_px"] == 40 * 30
    assert res["changed_px"] == 25
    px = Image.open(out).convert("RGB").load()
    assert px[2, 2] == visual_diff.COLOR_UNCHANGED
    assert px[12, 12] == visual_diff.COLOR_ADDED


def test_pixel_diff_unreadable_file_returns_none(tmp_path):
    a = tmp_path / "ok.png"
    _img_with_rects(a, (8, 8), [[1, 1, 2, 2]])
    assert visual_diff.pixel_diff_image(str(a), str(tmp_path / "nope.png"),
                                        str(tmp_path / "o.png")) is None


# ---------------------------------------------------------------------------
# ecad_diff_image
# ---------------------------------------------------------------------------

def test_ecad_identical_docs_report_no_change(tmp_path):
    out = tmp_path / "pcb_same.png"
    res = visual_diff.ecad_diff_image(_pcb_a(), _pcb_a(), str(out))
    assert res is not None
    assert (res["added"], res["removed"]) == (0, 0)
    assert res["unchanged"] > 0 and out.exists()


def test_ecad_added_track_is_one_add(tmp_path):
    b = _pcb_a()
    b["tracks"].append({"id": "t2", "p1": [10, 10.6], "p2": [10, 25],
                        "width": 0.25, "layer": "F.Cu", "net": "n2"})
    res = visual_diff.ecad_diff_image(_pcb_a(), b, str(tmp_path / "pcb_add.png"))
    assert res is not None
    assert (res["added"], res["removed"]) == (1, 0)


def test_ecad_moved_track_is_one_add_and_one_remove(tmp_path):
    b = _pcb_a()
    b["tracks"][0]["p2"] = [28.0, 10.0]
    res = visual_diff.ecad_diff_image(_pcb_a(), b, str(tmp_path / "pcb_move.png"))
    assert res is not None
    assert (res["added"], res["removed"]) == (1, 1)


def test_ecad_track_width_and_layer_are_part_of_identity(tmp_path):
    b = _pcb_a()
    b["tracks"][0]["width"] = 0.5
    res = visual_diff.ecad_diff_image(_pcb_a(), b, str(tmp_path / "pcb_w.png"))
    assert res is not None and (res["added"], res["removed"]) == (1, 1)

    c = _pcb_a()
    c["tracks"][0]["layer"] = "B.Cu"
    res = visual_diff.ecad_diff_image(_pcb_a(), c, str(tmp_path / "pcb_l.png"))
    assert res is not None and (res["added"], res["removed"]) == (1, 1)


def test_ecad_moved_footprint_moves_its_pads_too(tmp_path):
    """A footprint placement change must move its marker, its ref text and both of
    its pads: 4 strokes gone, 4 arrived."""
    b = _pcb_a()
    b["footprints"][0]["pos"] = [10, 20]
    res = visual_diff.ecad_diff_image(_pcb_a(), b, str(tmp_path / "pcb_fp.png"))
    assert res is not None
    assert (res["added"], res["removed"]) == (4, 4)


def test_ecad_missing_sections_contribute_no_strokes(tmp_path):
    """A schematic-shaped doc with no board geometry has nothing to draw; against a
    board it is a pure removal."""
    sch = {"schema": "aicad.sch/1", "nets": [], "symbols": []}
    assert visual_diff.ecad_diff_image(sch, sch, str(tmp_path / "sch.png")) is None
    res = visual_diff.ecad_diff_image(_pcb_a(), sch, str(tmp_path / "sch2.png"))
    assert res is not None
    assert res["added"] == 0 and res["removed"] > 0 and res["unchanged"] == 0


# ---------------------------------------------------------------------------
# Pillow-absent degradation
# ---------------------------------------------------------------------------

def test_every_function_degrades_to_none_without_pillow(tmp_path, monkeypatch):
    a = tmp_path / "x.png"
    _img_with_rects(a, (8, 8), [[1, 1, 2, 2]])
    for name in [m for m in list(sys.modules) if m == "PIL" or m.startswith("PIL.")]:
        monkeypatch.setitem(sys.modules, name, None)

    assert visual_diff._pil() is None
    assert visual_diff.drawing_diff_image(_drawing_a(), _drawing_a(),
                                          str(tmp_path / "p1.png")) is None
    assert visual_diff.ecad_diff_image(_pcb_a(), _pcb_a(), str(tmp_path / "p2.png")) is None
    assert visual_diff.pixel_diff_image(str(a), str(a), str(tmp_path / "p3.png")) is None
