"""Sketch Text geometry tests (SolidWorks-parity sketch-text feature).

A ``text`` sketch entity produces real filled glyph FACE geometry on the sketch plane, scaled so
the rendered cap height matches the requested ``height`` (mm). The faces flow into the same
region/profile collection extrude consumes, so a text profile extrudes into 3-D solids — one per
glyph, with the inner hole of letters like O/A/D preserved (extrudes as a ring, not a filled prism).

Run (from repo root, dev venv):
    set PYTHONUTF8=1 && <devpython> -X utf8 -m pytest engine/doc/test_sketch_text.py -q
"""

from __future__ import annotations

import math

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
from engine.doc.schema import validate_document


def _text_sketch(fid: str, text: str, height: float = 10.0,
                 p=(0.0, 0.0), font=None) -> dict:
    ent = {"id": "e1", "type": "text", "construction": False,
           "p": list(p), "text": text, "height": height}
    if font is not None:
        ent["font"] = font
    return T._feat(fid, "sketch",
                   {"entities": [ent], "constraints": []},
                   refs={"plane": "plane.top"})


def _extrude(fid: str, profile: list, depth: float = 5.0, mode: str = "boss") -> dict:
    return T._feat(fid, "extrude",
                   {"mode": mode, "end": "blind", "depth": {"value": depth, "expr": None}},
                   refs={"profile": profile})


# --------------------------------------------------------------------------- validation


def test_text_entity_validates() -> None:
    doc = T._doc([_text_sketch("f1", "AB", height=8.0)])
    assert validate_document(doc) == [], "text sketch must validate"


def test_text_with_optional_font_validates() -> None:
    doc = T._doc([_text_sketch("f1", "Hi", height=8.0, font="Arial")])
    assert validate_document(doc) == [], "text sketch with font must validate"


def test_text_missing_required_fields_rejected() -> None:
    bad = T._feat("f1", "sketch", {"entities": [
        {"id": "e1", "type": "text", "construction": False, "p": [0, 0]}  # no text/height
    ], "constraints": []}, refs={"plane": "plane.top"})
    errs = validate_document(T._doc([bad]))
    assert errs, "text entity missing text/height must fail validation"


# --------------------------------------------------------------------------- geometry


def test_text_extrudes_to_solids() -> None:
    """A text profile extrudes (boss, blind 5) into >= 1 solid with positive volume."""
    doc = T._doc([_text_sketch("f1", "AB", height=10.0),
                  _extrude("f2", [{"kind": "region", "sketch": "f1"}], depth=5.0)])
    assert validate_document(doc) == []
    res = compile_document(doc)
    s = T._status(res, "f2")
    assert s["ok"], f"text extrude failed: {s.get('error')}"
    body = res.bodies[-1].shape
    solids = body.solids()
    assert len(solids) >= 1, f"expected >=1 text solid, got {len(solids)}"
    assert body.volume > 0.0, "text solid must have positive volume"


def test_letter_with_hole_preserves_ring() -> None:
    """A letter with an interior hole ('O') extrudes as a ring: the solid volume is noticeably
    less than the bounding-box prism (height x width x depth) because the hole is preserved."""
    depth = 5.0
    doc = T._doc([_text_sketch("f1", "O", height=10.0),
                  _extrude("f2", [{"kind": "region", "sketch": "f1"}], depth=depth)])
    assert validate_document(doc) == []
    res = compile_document(doc)
    s = T._status(res, "f2")
    assert s["ok"], f"'O' extrude failed: {s.get('error')}"
    body = res.bodies[-1].shape
    vol = body.volume
    bb = body.bounding_box()
    prism_vol = (bb.max.X - bb.min.X) * (bb.max.Y - bb.min.Y) * (bb.max.Z - bb.min.Z)
    assert vol > 0.0
    # The ring of an 'O' occupies well under half its bounding-box prism.
    assert vol < 0.7 * prism_vol, \
        f"'O' volume {vol:.1f} is not noticeably less than its bbox prism {prism_vol:.1f} (hole lost?)"


def test_text_height_matches_request() -> None:
    """The rendered sketch-text bounding-box Y-extent equals the requested height within ~5%."""
    height = 12.0
    doc = T._doc([_text_sketch("f1", "AO", height=height)])
    assert validate_document(doc) == []
    res = compile_document(doc)
    s = T._status(res, "f1")
    assert s["ok"], f"text sketch failed: {s.get('error')}"
    # The sketch pseudo-body is the compound of glyph faces.
    tb = None
    for tname, shape in res.tool_bodies.items():
        if tname == "f1":
            tb = shape
            break
    assert tb is not None, "sketch f1 must expose a tool body (glyph compound)"
    bb = tb.bounding_box()
    rendered_h = bb.max.Y - bb.min.Y
    assert abs(rendered_h - height) <= 0.05 * height, \
        f"rendered text height {rendered_h:.3f} != requested {height} (>5%)"


def test_empty_text_raises_clean_error() -> None:
    """Empty text geometry fails as a contained feature error (status not ok), never a crash."""
    sk = T._feat("f1", "sketch", {"entities": [
        {"id": "e1", "type": "text", "construction": False,
         "p": [0.0, 0.0], "text": "   ", "height": 10.0}
    ], "constraints": []}, refs={"plane": "plane.top"})
    res = compile_document(T._doc([sk]))
    s = T._status(res, "f1")
    assert not s["ok"], "empty/whitespace text should produce a contained feature error"
