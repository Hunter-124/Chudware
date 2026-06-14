"""W14 — 2D EXPORT (DXF + PDF) for drawing sheets and sheet-metal flat patterns.

Covered:
1.  A part -> drawing (front/top/right/iso) -> export_drawing to a temp .dxf: the file exists, is
    non-empty, carries SECTION/ENTITIES + the VISIBLE & HIDDEN layer names, and has exactly as many
    polyline entities as the drawing's visible+hidden polylines across all views.
2.  The same drawing -> export_drawing to a temp .pdf: a valid %PDF header + non-trivial size.
3.  A sheet-metal base+edge flange -> Flatten -> export_flat_dxf to a temp .dxf: the DXF has an
    OUTLINE layer and a BEND layer carrying the bend line(s).
4.  export_flat_dxf to a temp .pdf: valid %PDF + non-trivial size.
5.  Contained errors: a non-sheet-metal part errors cleanly; a bad format errors cleanly.

Run:  & $py -m pytest engine/doc/test_export2d.py -q   (from repo root)
"""

from __future__ import annotations

import os
import tempfile

import pytest

from engine.doc import drawing, export2d, session as docsess
from engine.doc.ops import OpError


# --------------------------------------------------------------------------- part / drawing fixture


def _build_part_with_hole() -> docsess.DocSession:
    """A 60×40×20 box with a Ø10 through hole — gives both visible and hidden edges in views."""
    s = docsess.new_session("part", name="Bracket")
    box = {"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": {"value": 60, "expr": None},
                   "width": {"value": 40, "expr": None},
                   "height": {"value": 20, "expr": None}}}}
    docsess.apply_ops(s, None, [box])

    result = docsess._ensure_built(s)
    body = result.bodies[-1].shape
    top_face = max(body.faces(), key=lambda f: f.center().Z)
    faces = body.faces()
    idx = None
    for i, f in enumerate(faces, start=1):
        c = f.center()
        if (abs(c.X - top_face.center().X) < 1e-6 and abs(c.Y - top_face.center().Y) < 1e-6
                and abs(c.Z - top_face.center().Z) < 1e-6):
            idx = i
            break
    top_pick = docsess.resolve_pick(s, s.revision, body_id=1, kind="face", topo_id=idx)["selector"]
    hole = {"op": "add_feature", "feature": {
        "type": "hole", "name": "Hole1",
        "params": {"style": "simple", "end": "through_all", "standard": "custom",
                   "diameter": {"value": 10.0, "expr": None}},
        "refs": {"face": top_pick, "points": [[0.0, 0.0]]}}}
    docsess.apply_ops(s, None, [hole])
    return s


def _make_drawing(part: docsess.DocSession) -> str:
    res = drawing.new_drawing(part.doc_id, name="Bracket Drawing")
    draw_id = res["doc_id"]
    for typ, orient, pos in [("base", "front", [100, 100]), ("projected", "top", [100, 40]),
                             ("projected", "right", [180, 100]), ("iso", "iso", [200, 40])]:
        drawing.add_view(draw_id, type=typ, orientation=orient, scale=1.0,
                         position=pos, show_hidden=True)
    return draw_id


def _expected_polyline_count(draw_id: str) -> int:
    """Count visible+hidden polylines across all views (>= 2 points) in the drawing_get result."""
    full = drawing.get_drawing_full(draw_id)
    n = 0
    for sheet in full.get("sheets", []):
        for view in sheet.get("views", []):
            n += sum(1 for pl in view.get("visible", []) if len(pl) >= 2)
            n += sum(1 for pl in view.get("hidden", []) if len(pl) >= 2)
    return n


# --------------------------------------------------------------------------- sheet-metal fixture


def _dim(value: float):
    return {"value": value, "expr": None}


def _flat_sheet_metal_session() -> docsess.DocSession:
    """A 60×40×2 base flange + a 90° edge flange, then a Flatten feature, applied through the session
    pipeline so the worker's CompileResult.sheet_metal_flat side-channel is exercised exactly."""
    from engine.doc.compiler import compile_document
    from engine.doc.selectors import describe_edges, make_selector

    s = docsess.new_session("part", name="SMFlat")
    # Base sketch (rectangle on top plane) + base flange. Explicit feature ids so the region selector
    # can reference the sketch by id ("f1"), exactly like the sm_flat_pattern unit tests.
    w, h, t = 60.0, 40.0, 2.0
    sketch = {"op": "add_feature", "feature": {
        "id": "f1", "type": "sketch", "name": "Sketch1",
        "params": {"entities": [
            {"id": "e1", "type": "line", "construction": False, "p1": [0.0, 0.0], "p2": [w, 0.0]},
            {"id": "e2", "type": "line", "construction": False, "p1": [w, 0.0], "p2": [w, h]},
            {"id": "e3", "type": "line", "construction": False, "p1": [w, h], "p2": [0.0, h]},
            {"id": "e4", "type": "line", "construction": False, "p1": [0.0, h], "p2": [0.0, 0.0]}],
            "constraints": []},
        "refs": {"plane": "plane.top"}}}
    docsess.apply_ops(s, None, [sketch])
    region = {"kind": "region", "sketch": "f1", "centroid": [w / 2.0, h / 2.0],
              "area_or_len": w * h, "hint_index": 0}
    base = {"op": "add_feature", "feature": {
        "id": "f2", "type": "sm_base_flange", "name": "Base-Flange",
        "params": {"thickness": _dim(t)}, "refs": {"profile": [region]}}}
    docsess.apply_ops(s, None, [base])

    # Pick the long y=0 edge of the base broad face for the edge flange.
    result = docsess._ensure_built(s)
    body = result.bodies[0]
    infos = describe_edges(body.shape.wrapped)
    cands = [i for i in infos if i.surf == "line" and abs((i.measure or 0.0) - w) < 1e-3]
    target = (w / 2.0, 0.0, 0.0)
    best = min(cands, key=lambda i: sum((i.centroid[k] - target[k]) ** 2 for k in range(3)))
    sel = make_selector(best)
    sel["owner_feature"] = "f2"
    edge = {"op": "add_feature", "feature": {
        "id": "f3", "type": "sm_edge_flange", "name": "Edge-Flange",
        "params": {"angle": _dim(90.0), "length": _dim(20.0), "radius": _dim(2.0),
                   "position": "material_inside", "flip": False},
        "refs": {"edge": sel}}}
    docsess.apply_ops(s, None, [edge])

    flat = {"op": "add_feature", "feature": {
        "id": "f4", "type": "sm_flat_pattern", "name": "Flat-Pattern", "params": {}}}
    docsess.apply_ops(s, None, [flat])
    # The interleaved _ensure_built (to pick the edge) leaves the live compile cache with a base-flange
    # hit that doesn't repopulate the _sheet_metal side-channel; force a FRESH full rebuild (clearing
    # the per-feature cache) so every sm_* feature reruns in order, exactly as the export RPC does.
    from engine.doc.compiler import CompileCache
    s.compile_cache = CompileCache()
    s.last_result = None
    res = docsess._ensure_built(s)
    statuses = {fs["id"]: fs for fs in res.feature_status}
    assert statuses["f3"]["ok"], f"edge flange failed: {statuses['f3'].get('error')}"
    assert statuses["f4"]["ok"], f"flatten failed: {statuses['f4'].get('error')}"
    return s


def _read(path: str) -> str:
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        return fh.read()


def _ezdxf_roundtrip(path: str, expect_layers: set) -> None:
    """If ezdxf is installed, open the DXF and assert the expected layers + entities load.

    This proves the file is valid in a real third-party DXF reader (the wave's open-it-elsewhere
    acceptance check). Skipped silently when ezdxf is absent (it is not a frozen-runtime dependency).
    """
    try:
        import ezdxf
    except ImportError:
        return
    doc = ezdxf.readfile(path)
    layers = {layer.dxf.name for layer in doc.layers}
    assert expect_layers <= layers, f"ezdxf saw layers {layers}, expected {expect_layers}"
    ents = list(doc.modelspace())
    assert len(ents) > 0, "ezdxf parsed zero entities"


# --------------------------------------------------------------------------- 1: drawing -> DXF


def test_export_drawing_dxf() -> None:
    part = _build_part_with_hole()
    draw_id = _make_drawing(part)
    expected = _expected_polyline_count(draw_id)
    assert expected > 0, "drawing should have view geometry"

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "bracket_drawing.dxf")
        res = export2d.export_drawing(draw_id, path, "dxf")
        assert res["path"] == path
        assert os.path.exists(path) and os.path.getsize(path) > 0
        text = _read(path)
        assert "SECTION" in text and "ENTITIES" in text and "EOF" in text
        assert "VISIBLE" in text and "HIDDEN" in text, "both layers must be declared"
        # One POLYLINE entity per visible+hidden polyline (each is a POLYLINE/VERTEX*/SEQEND chain;
        # the bare "POLYLINE" token appears once per polyline). Count whole-line "POLYLINE" values.
        ent_lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
        n_entities = sum(1 for ln in ent_lines if ln == "POLYLINE")
        assert n_entities == expected, f"DXF has {n_entities} polylines, expected {expected}"
        assert res["entities"] == expected
        # If ezdxf is installed, prove the file actually opens in a real DXF reader.
        _ezdxf_roundtrip(path, expect_layers={"VISIBLE", "HIDDEN"})
        print(f"[export-drawing-dxf] {n_entities} POLYLINE entities "
              f"(visible={res['visible']} hidden={res['hidden']})")


# --------------------------------------------------------------------------- 2: drawing -> PDF


def test_export_drawing_pdf() -> None:
    part = _build_part_with_hole()
    draw_id = _make_drawing(part)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "bracket_drawing.pdf")
        res = export2d.export_drawing(draw_id, path, "pdf")
        assert os.path.exists(path)
        with open(path, "rb") as fh:
            head = fh.read(8)
        assert head.startswith(b"%PDF-1."), f"not a PDF: {head!r}"
        assert os.path.getsize(path) > 400, "PDF should be non-trivial"
        print(f"[export-drawing-pdf] size={os.path.getsize(path)} bytes")


# --------------------------------------------------------------------------- 3: flat pattern -> DXF


def test_export_flat_dxf() -> None:
    s = _flat_sheet_metal_session()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "smflat.dxf")
        res = export2d.export_flat_dxf(s.doc_id, path, "dxf")
        assert os.path.exists(path) and os.path.getsize(path) > 0
        text = _read(path)
        assert "SECTION" in text and "ENTITIES" in text
        # OUTLINE + BEND must be declared as separate layers (the key SM requirement).
        assert "OUTLINE" in text and "BEND" in text, "outline + bend layers required"
        assert res["bend_lines"] >= 1, "single bend -> at least one bend segment on BEND"
        # The BEND layer must carry entities: a group-code 8 (layer name) value "BEND" inside the
        # ENTITIES section. Parse the tag stream (code line / value line pairs) and confirm a "BEND"
        # value appears after an LWPOLYLINE (i.e. as an entity's layer, not just the LAYER table).
        lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip() != ""]
        ent_start = lines.index("ENTITIES")
        bend_entities = sum(1 for i in range(ent_start, len(lines) - 1)
                            if lines[i] == "8" and lines[i + 1] == "BEND")
        outline_entities = sum(1 for i in range(ent_start, len(lines) - 1)
                               if lines[i] == "8" and lines[i + 1] == "OUTLINE")
        assert bend_entities >= 1, "at least one entity must be on the BEND layer"
        assert outline_entities >= 1, "at least one entity must be on the OUTLINE layer"
        _ezdxf_roundtrip(path, expect_layers={"OUTLINE", "BEND"})
        assert res["outline_points"] >= 4, "outline should bound the flat blank"
        print(f"[export-flat-dxf] bend_lines={res['bend_lines']} "
              f"outline_pts={res['outline_points']} area={res['flat_area']:.1f}")


# --------------------------------------------------------------------------- 4: flat pattern -> PDF


def test_export_flat_pdf() -> None:
    s = _flat_sheet_metal_session()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "smflat.pdf")
        res = export2d.export_flat_dxf(s.doc_id, path, "pdf")
        with open(path, "rb") as fh:
            head = fh.read(8)
        assert head.startswith(b"%PDF-1."), f"not a PDF: {head!r}"
        assert os.path.getsize(path) > 400
        print(f"[export-flat-pdf] size={os.path.getsize(path)} bytes "
              f"bend_lines={res['bend_lines']}")


# --------------------------------------------------------------------------- 5: contained errors


def test_export_flat_no_sheet_metal_errors_cleanly() -> None:
    s = docsess.new_session("part", name="Plain")
    box = {"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": _dim(20), "width": _dim(20), "height": _dim(20)}}}
    docsess.apply_ops(s, None, [box])
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.dxf")
        with pytest.raises(OpError) as ei:
            export2d.export_flat_dxf(s.doc_id, path, "dxf")
        assert "flat pattern" in str(ei.value).lower()
        assert not os.path.exists(path), "no file should be written on error"
        print(f"[export-flat-error] -> {str(ei.value)[:70]}")


def test_export_bad_format_errors_cleanly() -> None:
    part = _build_part_with_hole()
    draw_id = _make_drawing(part)
    # Bad explicit format.
    with pytest.raises(OpError) as ei:
        export2d._handler_export_drawing(
            {"doc_id": draw_id, "path": "/tmp/x.dxf", "format": "svg"}, lambda _e: None)
    assert "format" in str(ei.value).lower()
    # No extension, no format param.
    with pytest.raises(OpError):
        export2d._handler_export_drawing(
            {"doc_id": draw_id, "path": "/tmp/noext"}, lambda _e: None)
    print(f"[export-badfmt] -> {str(ei.value)[:70]}")
