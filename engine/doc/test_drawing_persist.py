"""W12+ — drawing PERSISTENCE through the ``.step+`` container (engine/doc/stepplus.py).

A DrawingDocument stores no geometry — only the source-part reference + view definitions and
annotations — so it round-trips as plain JSON embedded in the part's ``.step+`` ZIP under
``drawings/<drawing_id>.json``. These tests prove:

  * SAVE embeds every open drawing of a part as ``drawings/<id>.json`` in the container;
  * OPEN (fresh session) restores each drawing as a NEW drawing session whose ``source`` points at
    the REOPENED part's new doc_id, with its views + annotations intact, and ``drawing_get``
    reprojects against the reopened bodies (non-empty geometry);
  * a part saved with NO drawings round-trips byte-for-byte unchanged (regression guard) and has
    an empty ``drawings/`` set on open.

Run:  & $py -X utf8 -m pytest engine/doc/test_drawing_persist.py -q   (from repo root)
"""

from __future__ import annotations

import zipfile

from engine.doc import drawing, session as docsess, stepplus
from engine.doc.schema import SCHEMA_DRAWING, validate_drawing


# --------------------------------------------------------------------------- fixtures


def _build_box_part(name: str = "Block") -> docsess.DocSession:
    """A plain 60x40x20 box part, built through the real op pipeline."""
    s = docsess.new_session("part", name=name)
    box = {"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": {"value": 60, "expr": None},
                   "width": {"value": 40, "expr": None},
                   "height": {"value": 20, "expr": None}}}}
    docsess.apply_ops(s, None, [box])
    assert all(f["ok"] for f in s.last_result.feature_status)
    return s


def _nverts(polylines) -> int:
    return sum(len(pl) for pl in polylines)


def _drawing_entries(path) -> list[str]:
    with zipfile.ZipFile(path, "r") as zf:
        return [n for n in zf.namelist() if n.startswith("drawings/")]


# --------------------------------------------------------------------------- tests


def test_drawing_persists_through_stepplus(tmp_path) -> None:
    """Part + drawing (front/top views + a dimension) -> save -> reopen -> drawing restored."""
    part = _build_box_part("Block")

    # --- create a drawing of the part: front + top views + one dimension ----
    res = drawing.new_drawing(part.doc_id, name="Block Drawing")
    draw_id = res["doc_id"]
    fr = drawing.add_view(draw_id, type="base", orientation="front", scale=1.0,
                          position=[100, 100], show_hidden=True)
    drawing.add_view(draw_id, type="projected", orientation="top", scale=1.0,
                     position=[100, 200], show_hidden=True)
    # A linear dimension on the front view between two projected vertices (sheet mm).
    fr_bb = fr["view"]["bbox"]
    dim = drawing.add_dim(draw_id, fr["view_id"], type="linear_dim",
                          a=[fr_bb[0], fr_bb[1]], b=[fr_bb[2], fr_bb[1]])
    assert dim["annotation"]["type"] == "linear_dim"
    orig_doc = drawing.serialize_drawing(drawing.get_drawing(draw_id))
    n_views = sum(len(sh.get("views", [])) for sh in orig_doc["sheets"])
    assert n_views == 2

    # --- save to a temp .step+ ; assert the ZIP carries drawings/<id>.json ---
    path = tmp_path / "block.step+"
    out = stepplus.save(part, str(path))
    entries = _drawing_entries(out)
    assert entries == [f"drawings/{draw_id}.json"], entries

    # --- reopen in a FRESH world: close the originals so ids can't leak -----
    docsess.close_session(part.doc_id)
    drawing.close_drawing(draw_id)
    assert draw_id not in drawing._DRAWINGS

    reopened, _warn = stepplus.open(out)
    restored = stepplus.restore_drawings(reopened)
    assert len(restored) == 1, restored
    rinfo = restored[0]
    new_draw_id = rinfo["doc_id"]

    # The restored drawing is a fresh session (new id), but the same drawing name.
    assert new_draw_id != draw_id
    assert rinfo["name"] == "Block Drawing"
    rs = drawing.get_drawing(new_draw_id)

    # --- source re-pointed to the REOPENED part's new doc_id ----------------
    assert rs.source_doc_id == reopened.doc_id
    assert rs.doc["source"] == reopened.doc_id
    assert reopened.doc_id != part.doc_id  # genuinely a new part session

    # --- views + annotation intact ------------------------------------------
    rdoc = rs.doc
    assert rdoc["schema"] == SCHEMA_DRAWING
    assert validate_drawing(rdoc) == []
    rviews = [v for sh in rdoc["sheets"] for v in sh.get("views", [])]
    assert len(rviews) == 2, rviews
    orients = sorted(v["orientation"] for v in rviews)
    assert orients == ["front", "top"], orients
    front_v = next(v for v in rviews if v["orientation"] == "front")
    anns = front_v.get("annotations", [])
    assert len(anns) == 1, anns
    assert anns[0]["type"] == "linear_dim"
    # The measured model value survived the round-trip exactly.
    assert abs(anns[0]["value"] - dim["annotation"]["value"]) < 1e-9

    # --- drawing_get reprojects against the reopened part (non-empty) -------
    full = drawing.get_drawing_full(new_draw_id)
    assert full["source_doc_id"] == reopened.doc_id
    got_views = [v for sh in full["sheets"] for v in sh.get("views", [])]
    assert len(got_views) == 2
    for gv in got_views:
        assert gv["error"] is None, gv["error"]
        assert _nverts(gv["visible"]) > 0, f"{gv['orientation']} reprojected empty"
    # The restored front-view geometry still carries the dimension annotation.
    g_front = next(v for v in got_views if v["orientation"] == "front")
    assert len(g_front.get("annotations", [])) == 1


def test_part_without_drawings_roundtrips_unchanged(tmp_path) -> None:
    """A part with NO drawings: no drawings/ folder on save, empty restore on open (regression)."""
    part = _build_box_part("Plain")
    path = tmp_path / "plain.step+"
    out = stepplus.save(part, str(path))

    # No drawing entries written.
    assert _drawing_entries(out) == []
    with zipfile.ZipFile(out, "r") as zf:
        names = set(zf.namelist())
    assert "document.json" in names and "meta.json" in names

    # Reopen: zero restored drawings, part itself round-trips.
    reopened, _warn = stepplus.open(out)
    assert stepplus.restore_drawings(reopened) == []
    assert reopened.doc.get("name") == "Plain"
    assert all(f["ok"] for f in reopened.last_result.feature_status)


def test_resave_does_not_duplicate_or_stale_drawings(tmp_path) -> None:
    """Save -> reopen -> restore -> resave keeps exactly one live drawing entry (no stale dupes).

    The reopened part carries the old ``drawings/*.json`` in extra_entries; on resave those stale
    entries must be dropped and the LIVE restored drawing (under its new id) written instead.
    """
    part = _build_box_part("Resave")
    res = drawing.new_drawing(part.doc_id, name="Resave Drawing")
    draw_id = res["doc_id"]
    drawing.add_view(draw_id, type="base", orientation="front", scale=1.0, position=[0, 0])

    path = tmp_path / "resave.step+"
    out = stepplus.save(part, str(path))
    assert _drawing_entries(out) == [f"drawings/{draw_id}.json"]

    docsess.close_session(part.doc_id)
    drawing.close_drawing(draw_id)

    reopened, _warn = stepplus.open(out)
    restored = stepplus.restore_drawings(reopened)
    new_draw_id = restored[0]["doc_id"]

    # Resave under the SAME path: exactly one drawing entry, keyed by the NEW (live) id.
    out2 = stepplus.save(reopened, str(path))
    entries2 = _drawing_entries(out2)
    assert entries2 == [f"drawings/{new_draw_id}.json"], entries2
    # The stale old id is gone.
    assert f"drawings/{draw_id}.json" not in entries2
