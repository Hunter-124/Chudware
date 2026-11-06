"""Drawing-tool dialect tests (services/mcp/cad_drawing_tools.py).

Engine-backed, end-to-end through the registered MCP tools, in the same style as
test_cad_tools.py. These defend the two live pass-2 general-lane failures of 2026-07-29:

  1. add_dimension(dtype="diameter")  -> "annotation type 'diameter' not recognized"
     even though the engine implements diameter_dim (core/doc/drawing_session.cpp:1034-1060).
  2. make_drawing(title="90x50x12 Plate") -> "title must be a title-block fields dict".

Every assertion here is on a VALUE (the measured dimension, the stored annotation type, the
title-block field, the refusal text) — never on a bare ok:true. In particular a diameter must
read 2*r and a radius must read r: those two words must never collapse onto one type.

Run:  .venv-mcp/bin/python -m pytest services/mcp/test_cad_drawing_tools.py -q
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from services.mcp import core_bridge as cb

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged (run build.sh); engine-backed MCP tools unavailable",
)

HOLE_D_MM = 8.0  # the bore drilled by _plate_with_hole below


def _call(name: str, args: dict):
    """Invoke a registered MCP tool; return the parsed JSON reply."""
    from services.mcp import server

    _content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    assert isinstance(text, str), (name, structured)
    return json.loads(text)

@pytest.fixture(autouse=True)
def discard_minted_documents():
    """Keep engine-backed drawing tests isolated from later agent terminal-save sweeps."""
    before = {
        row.get("doc_id")
        for row in (cb.call("doc_list", {}).get("docs") or [])
        if isinstance(row, dict) and row.get("doc_id")
    }
    yield
    after = [
        row for row in (cb.call("doc_list", {}).get("docs") or [])
        if isinstance(row, dict) and row.get("doc_id") not in before
    ]
    drawings = [row for row in after if str(row.get("doc_id")).startswith("dwg-")
                or str(row.get("kind") or row.get("type") or "").lower() == "drawing"]
    parts = [row for row in after if row not in drawings]
    for row in reversed(drawings):
        doc_id = row["doc_id"]
        result = cb.call("drawing_close", {"doc_id": doc_id})
        closed = result.get("result", result.get("ok")) if isinstance(result, dict) else result
        assert closed, f"failed to close scratch drawing {doc_id}: {result}"
    for row in reversed(parts):
        doc_id = row["doc_id"]
        result = _call("close_part", {"doc_id": doc_id, "discard": True})
        assert result.get("ok"), (
            f"failed to discard drawing-tool scratch document {doc_id}: "
            f"{result.get('error')}")


def _plate_with_hole() -> str:
    """A 60x40x10 plate with a single ⌀8 through bore in the top face."""
    doc = _call("new_part", {"name": "plate"})["doc_id"]
    ops = [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40), "height": cb.dim(10)}),
        cb.feat("hole", "Bore", {"style": "simple", "end": "through_all",
                                 "diameter": cb.dim(HOLE_D_MM)},
                {"face": {"of": "face", "side": "top"}}),
    ]
    r = _call("add_features", {"ops": ops, "doc_id": doc})
    assert r["ok"], r
    return doc


def _drawing_with_circle(doc: str) -> tuple[str, str, list]:
    """(drawing_doc_id, view_id, [cx,cy]) of the view where the ⌀8 bore projects as a circle.

    Auto annotations are turned OFF so the only dimension on the sheet is the one the test
    adds — no auto hole callout can be mistaken for it.
    """
    mk = _call("make_drawing", {"doc_id": doc, "views": ["front", "top"],
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    dwg = mk["drawing_doc_id"]
    full = cb.call("drawing_get", {"doc_id": dwg})
    for view in full["sheets"][0]["views"]:
        scale = view.get("scale") or 1.0
        for arc in view.get("arcs", []):
            if not arc.get("closed"):
                continue
            if abs(arc.get("radius", 0.0) / scale - HOLE_D_MM / 2.0) > 1e-4:
                continue
            return dwg, view["view_id"], list(arc["center"])
    raise AssertionError("no projected ⌀8 circle found in the drawing views")


def _stored_annotations(dwg: str, view_id: str) -> list:
    full = cb.call("drawing_get", {"doc_id": dwg})
    for view in full["sheets"][0]["views"]:
        if view["view_id"] == view_id:
            return view.get("annotations", [])
    raise AssertionError(f"view {view_id} vanished from {dwg}")


def _the_annotation(dwg: str, view_id: str, ann_id) -> dict:
    anns = [a for a in _stored_annotations(dwg, view_id) if a.get("id") == ann_id]
    assert len(anns) == 1, (ann_id, _stored_annotations(dwg, view_id))
    return anns[0]


# --------------------------------------------------------------- Problem 1: dtype vocabulary


def test_add_dimension_diameter_word_measures_the_diameter():
    """The verbatim live call `dtype="diameter"` must succeed, be STORED as the engine's
    diameter_dim, and read the DIAMETER (8.0) of the known ⌀8 bore — not its radius."""
    dwg, vid, center = _drawing_with_circle(_plate_with_hole())
    r = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": "diameter",
                                "a": center, "b": center})
    assert r["ok"], r
    assert r["dtype"] == "diameter_dim", r
    assert abs(r["value"] - HOLE_D_MM) < 1e-6, r
    ann = _the_annotation(dwg, vid, r["annotation"])
    # The engine files radial/diameter dims as type "note" with the real kind in `kind`.
    assert ann.get("kind") == "diameter_dim", ann
    assert abs(ann["value"] - HOLE_D_MM) < 1e-6, ann
    assert abs(ann["model_radius"] - HOLE_D_MM / 2.0) < 1e-6, ann
    assert "\u2300" in ann["text"], ann  # ⌀8 — a diameter callout, not R4


@pytest.mark.parametrize("word", ["diameter", "diam", "dia", "\u2300", "DIAMETER", "Diameter"])
def test_diameter_spellings_all_land_on_diameter_dim(word):
    dwg, vid, center = _drawing_with_circle(_plate_with_hole())
    r = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": word,
                                "a": center, "b": center})
    assert r["ok"] and r["dtype"] == "diameter_dim", (word, r)
    assert abs(r["value"] - HOLE_D_MM) < 1e-6, (word, r)


def test_add_dimension_radius_word_measures_the_radius_not_the_diameter():
    """`radius` must map to radial_dim and report 4.0 on the same ⌀8 bore. If the two words
    were ever collapsed onto one type this reads 8.0 and the sheet lies by a factor of two."""
    dwg, vid, center = _drawing_with_circle(_plate_with_hole())
    r = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": "radius",
                                "a": center, "b": center})
    assert r["ok"], r
    assert r["dtype"] == "radial_dim", r
    assert abs(r["value"] - HOLE_D_MM / 2.0) < 1e-6, r
    ann = _the_annotation(dwg, vid, r["annotation"])
    assert ann.get("kind") == "radial_dim", ann
    assert abs(ann["value"] - HOLE_D_MM / 2.0) < 1e-6, ann
    assert ann["text"].startswith("R"), ann  # R4 — a radius callout


def test_radius_and_diameter_on_the_same_circle_differ_by_exactly_two():
    """The single assertion that proves the two words are not aliases of each other."""
    dwg, vid, center = _drawing_with_circle(_plate_with_hole())
    rad = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": "rad",
                                  "a": center, "b": center})
    dia = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": "dia",
                                  "a": center, "b": center})
    assert rad["ok"] and dia["ok"], (rad, dia)
    assert abs(dia["value"] - 2.0 * rad["value"]) < 1e-6, (rad, dia)


def test_linear_and_angular_words_map_to_their_engine_types():
    """Length and angle drafting words reach their corresponding dimension operations."""
    dwg, vid, center = _drawing_with_circle(_plate_with_hole())
    lin = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": "length",
                                  "a": [center[0], center[1]], "b": [center[0] + 20.0, center[1]]})
    assert lin["ok"] and lin["dtype"] == "linear_dim", lin
    ann = _the_annotation(dwg, vid, lin["annotation"])
    assert ann["type"] == "linear_dim", ann
    assert ann["value"] > 0.0, ann

    ang = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": "angle",
                                  "a": center, "b": [center[0] + 10, center[1]],
                                  "c": center, "d": [center[0], center[1] + 10]})
    assert ang["ok"] and ang["dtype"] == "angular_dim", ang
    assert ang["value"] == pytest.approx(90.0)
    assert _the_annotation(dwg, vid, ang["annotation"])["value"] == pytest.approx(90.0)


def test_unknown_dtype_still_fails_and_lists_the_real_vocabulary():
    dwg, vid, center = _drawing_with_circle(_plate_with_hole())
    r = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": "ordinate_baseline",
                                "a": center, "b": center})
    assert r["ok"] is False, r
    assert "ordinate_baseline" in r["error"], r
    for real in ("linear_dim", "aligned_dim", "radial_dim", "diameter_dim", "angular_dim",
                 "center_mark", "center_line"):
        assert real in r["error"], (real, r)
    assert r["valid_dtype"][0] == "linear_dim", r
    # Nothing was written to the sheet by the refused call.
    assert _stored_annotations(dwg, vid) == [], _stored_annotations(dwg, vid)


def test_default_dtype_and_engine_spellings_are_unchanged():
    """The pre-existing suffixed vocabulary must pass through byte-identically."""
    dwg, vid, center = _drawing_with_circle(_plate_with_hole())
    for spelling in ("linear_dim", "aligned_dim"):
        r = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": spelling,
                                    "a": center, "b": [center[0] + 15.0, center[1] + 15.0]})
        assert r["ok"] and r["dtype"] == spelling, r
        assert _the_annotation(dwg, vid, r["annotation"])["type"] == spelling, r
    r = _call("add_dimension", {"doc_id": dwg, "view_id": vid,
                                "a": center, "b": [center[0] + 10.0, center[1]]})
    assert r["ok"] and r["dtype"] == "linear_dim", r


# --------------------------------------------------------------- Problem 2: make_drawing title


def _title_block(dwg: str) -> dict:
    full = cb.call("drawing_get", {"doc_id": dwg})
    return full["sheets"][0].get("title_block") or {}


def test_make_drawing_accepts_a_bare_string_title():
    """The verbatim live call: title as a plain string must land in the title block's `title`
    field — read back off the drawing, not merely accepted."""
    doc = _plate_with_hole()
    mk = _call("make_drawing", {"doc_id": doc, "sheet": "A4", "title": "90x50x12 Plate",
                                "views": ["front"], "model_items": False,
                                "hole_callouts": False})
    assert mk["ok"], mk
    tb = _title_block(mk["drawing_doc_id"])
    assert tb.get("title") == "90x50x12 Plate", tb
    # ...and it survives a later partial set_titleblock, like a dict-set title does.
    st = _call("set_titleblock", {"doc_id": mk["drawing_doc_id"], "fields": {"rev": "C"}})
    assert st["ok"] and st["title_block"]["title"] == "90x50x12 Plate", st


def test_make_drawing_string_title_beats_the_part_name():
    """The promoted string must WIN over the auto "use the part name" fallback, otherwise the
    call would look successful while the sheet carried the wrong title."""
    doc = _plate_with_hole()
    mk = _call("make_drawing", {"doc_id": doc, "title": "Bracket Rev C", "views": ["front"],
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    assert _title_block(mk["drawing_doc_id"]).get("title") == "Bracket Rev C", mk


def test_make_drawing_dict_title_unchanged():
    """Dict form keeps working exactly as before, extra fields included."""
    doc = _plate_with_hole()
    mk = _call("make_drawing", {"doc_id": doc, "views": ["front"], "model_items": False,
                                "hole_callouts": False,
                                "title": {"title": "PLATE", "material": "AL 6061",
                                          "rev": "A"}})
    assert mk["ok"], mk
    tb = _title_block(mk["drawing_doc_id"])
    assert tb.get("title") == "PLATE" and tb.get("material") == "AL 6061", tb
    assert tb.get("rev") == "A", tb


def test_make_drawing_json_string_title_still_decodes_to_fields():
    """A stringified dict must still be decoded as FIELDS, not swallowed as a literal title."""
    doc = _plate_with_hole()
    mk = _call("make_drawing", {"doc_id": doc, "views": ["front"], "model_items": False,
                                "hole_callouts": False,
                                "title": '{"title": "PLATE", "rev": "B"}'})
    assert mk["ok"], mk
    tb = _title_block(mk["drawing_doc_id"])
    assert tb.get("title") == "PLATE" and tb.get("rev") == "B", tb


@pytest.mark.parametrize("bad", [["PLATE"], 42, 3.5, True])
def test_make_drawing_refuses_a_malformed_title(bad):
    """A list/number/bool is not a title and not a field dict — still refused, with the
    teaching shape that names the argument."""
    doc = _plate_with_hole()
    mk = _call("make_drawing", {"doc_id": doc, "views": ["front"], "model_items": False,
                                "hole_callouts": False, "title": bad})
    assert mk["ok"] is False, (bad, mk)
    assert "title" in mk["error"], (bad, mk)

# ------------------------------------------------- Problem 3: argument NAMES (pass-2 sweep)
#
# Six live pass-2 failures (2026-07-29, claude-sonnet-4-5, general lane) died on the argument
# NAME before the engine was reached. Every test below reads the RESULT back off the drawing
# (drawing_get) rather than trusting the call's own echo, and every refusal test also proves
# the document was not touched.


_PLATE_N = itertools.count(1)


def _plain_plate() -> str:
    """A fresh 90x50x12 plate. new_part is idempotent per NAME (the same name hands back the
    same doc), so every plate gets its own — several tests need two distinct documents."""
    n = next(_PLATE_N)
    doc = _call("new_part", {"name": f"Plate{n}"})["doc_id"]
    r = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", f"Plate{n}", {"length": cb.dim(90), "width": cb.dim(50),
                                     "height": cb.dim(12)})]})
    assert r["ok"], r
    return doc


def _a_drawing(doc: str, **extra) -> dict:
    args = {"doc_id": doc, "views": ["front"], "model_items": False, "hole_callouts": False}
    args.update(extra)
    return _call("make_drawing", args)


def _sheet_size(dwg: str) -> str:
    return cb.call("drawing_get", {"doc_id": dwg})["sheets"][0]["size"]


def _drawn_source(dwg: str) -> str:
    """The doc the drawing was actually projected FROM, straight off the engine."""
    return cb.call("drawing_get", {"doc_id": dwg})["source_doc_id"]


def _auto_dims(dwg: str) -> int:
    """How many auto (model-items) annotations the engine placed."""
    full = cb.call("drawing_get", {"doc_id": dwg})
    return sum(1 for sh in full["sheets"] for v in sh["views"]
               for a in v.get("annotations", []) if a.get("auto"))


# --- 1. the source-document spellings ------------------------------------------------------


@pytest.mark.parametrize("name", ["source_doc", "source_doc_id", "source", "from_doc",
                                  "model_doc", "part_doc"])
def test_source_document_spellings_draw_the_right_document(name):
    """The verbatim live call used source_doc=. source_doc / source_doc_id are declared
    parameters of make_drawing; source / from_doc / model_doc / part_doc reach doc_id through
    the shared reconciler (TOOL_ARG_SYNONYMS + the affix rule). Both mechanisms only work
    because the tool deliberately does NOT declare **kwargs — that would switch the reconciler
    off and, on this MCP path, silently DROP the argument instead of refusing it."""
    doc = _plain_plate()
    other = _plain_plate()  # a second, newer doc: a dropped alias would silently draw THIS one
    assert other != doc
    mk = _call("make_drawing", {name: doc, "views": ["front"], "model_items": False,
                                "hole_callouts": False})
    assert mk["ok"], (name, mk)
    assert _drawn_source(mk["drawing_doc_id"]) == doc, (name, mk)
    assert any("doc_id" in n for n in mk.get("interpreted", [])) or name == "doc_id", mk


def test_two_disagreeing_document_spellings_are_refused_not_silently_picked():
    """doc_id and source_doc name the SAME slot. Choosing a winner would draw one document
    while the caller believes it asked for the other — invisible in the reply."""
    doc, other = _plain_plate(), _plain_plate()
    mk = _call("make_drawing", {"doc_id": doc, "source_doc": other, "views": ["front"],
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"] is False, mk
    assert "doc_id" in mk["error"] and "source_doc" in mk["error"], mk
    assert "drawing_doc_id" not in mk, mk


def test_the_same_document_under_two_spellings_is_harmless():
    doc = _plain_plate()
    mk = _call("make_drawing", {"doc_id": doc, "source_doc": doc, "views": ["front"],
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    assert _drawn_source(mk["drawing_doc_id"]) == doc, mk


def test_make_drawing_still_refuses_a_genuinely_unknown_argument():
    doc = _plain_plate()
    mk = _call("make_drawing", {"doc_id": doc, "views": ["front"], "frobnicate": 7})
    assert mk["ok"] is False and mk["status"] == "unknown_argument", mk
    assert mk["unknown_params"] == ["frobnicate"], mk


# --- 2. template / sheet-size ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["template", "sheet_size", "sheet"])
def test_paper_size_spellings_land_on_the_sheet(name):
    """template="A4" IS the paper size under another name — proved by reading the sheet back."""
    mk = _call("make_drawing", {"doc_id": _plain_plate(), name: "A4", "views": ["front"],
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"], (name, mk)
    assert _sheet_size(mk["drawing_doc_id"]) == "A4", (name, mk)
    assert mk["sheet"] == "A4", (name, mk)


def test_a_decorated_paper_size_is_still_understood():
    mk = _a_drawing(_plain_plate(), sheet="ISO A2 landscape")
    assert mk["ok"] and _sheet_size(mk["drawing_doc_id"]) == "A2", mk


def test_a_template_that_is_not_a_paper_size_is_refused_naming_both_concepts():
    """Contract rule 1: a titleblock/border template FILE is not the paper size. This codebase
    has no template concept at all, so the honest answer is a refusal that names the two things
    a sheet really has — and no drawing may be created."""
    doc = _plain_plate()
    mk = _call("make_drawing", {"doc_id": doc, "template": "iso_titleblock.dwt",
                                "views": ["front"]})
    assert mk["ok"] is False, mk
    assert "drawing_doc_id" not in mk, mk
    err = mk["error"]
    assert "sheet=" in err and "title" in err.lower(), err
    assert "A4" in err and "template" in err.lower(), err


def test_an_unknown_paper_size_no_longer_falls_back_to_A3_silently():
    """drawing_session.cpp:1797 swallows a bad size and keeps A3, so `sheet="A6"` used to
    produce a normal-looking A3 sheet. It must be refused instead."""
    mk = _a_drawing(_plain_plate(), sheet="A6")
    assert mk["ok"] is False, mk
    assert "A6" in mk["error"] and "A3" in mk["error"], mk
    assert mk["valid_sheet"][0] == "A4", mk


def test_the_default_sheet_is_unchanged_when_no_size_is_given():
    mk = _a_drawing(_plain_plate())
    assert mk["ok"] and _sheet_size(mk["drawing_doc_id"]) == "A3", mk


# --- 3. model_items -------------------------------------------------------------------------


def test_model_items_basic_dims_runs_the_real_auto_dimension_pass():
    """The verbatim live value. model_items IS implemented (drawing_session.cpp:1948-1952 ->
    drawing_model_items), so the drafting phrasing is honoured — proved by counting the auto
    annotations the engine placed, not by ok:true."""
    mk = _call("make_drawing", {"doc_id": _plain_plate(), "views": ["front"],
                                "model_items": "basic_dims", "hole_callouts": False})
    assert mk["ok"] and mk["auto_dimensions_added"] > 0, mk
    assert _auto_dims(mk["drawing_doc_id"]) > 0, mk


def test_a_negative_model_items_word_is_not_truthy():
    """bool("none") is True. If the word reached the flag unfiltered, the engine would run the
    auto-dimension pass the caller just switched OFF."""
    mk = _call("make_drawing", {"doc_id": _plain_plate(), "views": ["front"],
                                "model_items": "none", "hole_callouts": False})
    assert mk["ok"] and mk["auto_dimensions_added"] == 0, mk
    assert _auto_dims(mk["drawing_doc_id"]) == 0, mk


def test_an_unrecognised_model_items_value_is_refused_and_points_at_add_dimension():
    """Never silently dropped and never silently truthy: a caller who asked for dimensions must
    not ship an undimensioned drawing believing otherwise."""
    mk = _call("make_drawing", {"doc_id": _plain_plate(), "views": ["front"],
                                "model_items": "only the hole depths"})
    assert mk["ok"] is False, mk
    assert "add_dimension" in mk["error"] and "SWITCH" in mk["error"], mk
    assert "drawing_doc_id" not in mk, mk


def test_model_items_booleans_are_unchanged():
    on = _call("make_drawing", {"doc_id": _plain_plate(), "views": ["front"],
                                "model_items": True, "hole_callouts": False})
    off = _call("make_drawing", {"doc_id": _plain_plate(), "views": ["front"],
                                 "model_items": False, "hole_callouts": False})
    assert _auto_dims(on["drawing_doc_id"]) > 0, on
    assert _auto_dims(off["drawing_doc_id"]) == 0, off


# --- 4. export_drawing filename -------------------------------------------------------------


def _exported(tmp_path, **extra) -> dict:
    mk = _a_drawing(_plain_plate())
    assert mk["ok"], mk
    args = {"doc_id": mk["drawing_doc_id"]}
    args.update(extra)
    return _call("export_drawing", args)


def test_filename_without_an_extension_gets_exactly_one(tmp_path):
    """The verbatim live call: format="pdf", filename="90x50x12_plate_drawing"."""
    stem = str(tmp_path / "90x50x12_plate_drawing")
    r = _exported(tmp_path, format="pdf", filename=stem)
    assert r["ok"], r
    assert r["path"] == stem + ".pdf", r
    assert os.path.exists(r["path"]), r


def test_a_filename_that_already_has_the_extension_is_not_doubled(tmp_path):
    """The save_part "x.pdf.pdf" bug must not be reintroduced here."""
    want = str(tmp_path / "plate.pdf")
    r = _exported(tmp_path, fmt="pdf", filename=want)
    assert r["ok"], r
    assert r["path"] == want, r
    assert not r["path"].endswith(".pdf.pdf"), r


@pytest.mark.parametrize("name", ["filename", "out_path", "file_path", "path"])
def test_output_name_spellings_all_reach_the_path_slot(tmp_path, name):
    """`filename` is a declared parameter; out_path / file_path reach `path` through the shared
    affix rule, which only survives because export_drawing does NOT declare **kwargs."""
    want = str(tmp_path / f"via_{name}")
    r = _exported(tmp_path, fmt="dxf", **{name: want})
    assert r["ok"], (name, r)
    assert r["path"] == want + ".dxf" and os.path.exists(r["path"]), (name, r)


def test_filename_disagreeing_with_path_is_refused(tmp_path):
    a, b = str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")
    r = _exported(tmp_path, fmt="pdf", path=a, filename=b)
    assert r["ok"] is False, r
    assert "path" in r["error"] and "filename" in r["error"], r
    assert not os.path.exists(a) and not os.path.exists(b), r


def test_a_contradicting_extension_is_refused_rather_than_written(tmp_path):
    """fmt="pdf" into a .dxf name would write PDF bytes to a file nothing can open."""
    want = str(tmp_path / "plate.dxf")
    r = _exported(tmp_path, format="pdf", filename=want)
    assert r["ok"] is False, r
    assert "pdf" in r["error"] and "dxf" in r["error"], r
    assert not os.path.exists(want), r


def test_the_extension_chooses_the_format_when_none_was_given(tmp_path):
    want = str(tmp_path / "plate.dxf")
    r = _exported(tmp_path, filename=want)
    assert r["ok"] and r["format"] == "dxf", r
    assert r["path"] == want and os.path.exists(want), r


def test_export_drawing_default_is_still_pdf_and_svg_is_a_supported_final(tmp_path):
    r = _exported(tmp_path)
    assert r["ok"] and r["format"] == "pdf" and r["path"].endswith(".pdf"), r
    svg = _exported(tmp_path, fmt="svg", filename=str(tmp_path / "editable"))
    assert svg["ok"] and svg["format"] == "svg", svg
    assert svg["path"] == str(tmp_path / "editable.svg"), svg
    assert svg["paths"] == [svg["path"]], svg
    assert Path(svg["path"]).stat().st_size > 0
    bad = _exported(tmp_path, fmt="png", filename=str(tmp_path / "not-a-drawing"))
    assert bad["ok"] is False and "pdf" in bad["error"] and "svg" in bad["error"], bad


def test_pdf_companions_write_editable_siblings_and_list_every_path(tmp_path):
    stem = tmp_path / "released"
    result = _exported(
        tmp_path,
        fmt="pdf",
        filename=str(stem),
        companions=["dxf", "svg"],
    )
    expected = [str(stem.with_suffix(ext)) for ext in (".pdf", ".dxf", ".svg")]
    assert result["ok"] and result["path"] == expected[0], result
    assert result["paths"] == expected, result
    assert all(Path(path).stat().st_size > 0 for path in expected), result


def test_svg_rejection_from_an_older_engine_degrades_honestly(monkeypatch, tmp_path):
    made = _a_drawing(_plain_plate())
    real_call = cb.call

    def engine_without_svg(method, params=None):
        if method in {"export_drawing", "drawing_export"} and params.get("fmt") == "svg":
            raise cb.CoreError("format must be 'dxf' or 'pdf'")
        return real_call(method, params)

    monkeypatch.setattr(cb, "call", engine_without_svg)
    result = _call("export_drawing", {
        "doc_id": made["drawing_doc_id"],
        "fmt": "svg",
        "path": str(tmp_path / "legacy.svg"),
    })
    assert result["ok"] is False, result
    assert result["reason"] == "engine_unsupported", result
    assert result["tool"] == "export_drawing", result
    assert result["rpc"] == "drawing_export", result


def test_export_drawing_still_refuses_a_genuinely_unknown_argument(tmp_path):
    r = _exported(tmp_path, colour_depth=8)
    assert r["ok"] is False and r["status"] == "unknown_argument", r
    assert r["unknown_params"] == ["colour_depth"], r


# --- 5. set_titleblock field names ----------------------------------------------------------

#
# set_titleblock takes its field names as FLATTENED **kwargs (the 2026-07-27 fix). A varkw tool
# is only reachable that way on the built-in-agent path: FastMCP's generated arg model turns
# `**kwargs` into one ordinary field called "kwargs" and DROPS every unrecognised top-level key,
# so server.mcp.call_tool would silently swallow revision=/part_name=/units= before the tool
# body ran. The desktop copilot dispatches through agent_turn._dispatch_tool, which is where the
# live failure came from and what `_agent_call` below reproduces — the same seam
# services/agent/test_arg_reconciliation.py uses for this tool.


def _agent_call(name: str, args: dict):
    """Invoke a tool exactly as the built-in agent does (agent_turn._dispatch_tool)."""
    from services.agent import agent_turn as at

    raw = at._dispatch_tool(name, args)
    return raw if isinstance(raw, dict) else json.loads(raw)


def _tb_drawing() -> str:
    mk = _a_drawing(_plain_plate(), title={"title": ""})
    assert mk["ok"], mk
    return mk["drawing_doc_id"]


def test_revision_lands_in_rev_and_survives_the_export(tmp_path):
    """Revision aliases resolve to one canonical cell and export in the framed grid."""
    dwg = _tb_drawing()
    st = _agent_call("set_titleblock", {"doc_id": dwg, "revision": "A", "material": "6061-T6"})
    assert st["ok"], st
    tb = _title_block(dwg)
    assert tb.get("rev") == "A", tb
    assert tb.get("revision") == "A", tb
    out = str(tmp_path / "rev.dxf")
    ex = _call("export_drawing", {"doc_id": dwg, "fmt": "dxf", "path": out})
    assert ex["ok"], ex
    text = Path(out).read_text(encoding="utf8", errors="replace")
    assert "\n1\nREV\n" in text and "\n1\nA\n" in text, text[-1200:]
    assert "\n1\nMATERIAL\n" in text and "\n1\n6061-T6\n" in text, text[-1200:]


@pytest.mark.parametrize("name,field", [("part_no", "part_number"), ("drawn", "drawn_by")])
def test_the_engines_own_cell_aliases_are_accepted(name, field):
    dwg = _tb_drawing()
    st = _agent_call("set_titleblock", {"doc_id": dwg, name: "X-1"})
    assert st["ok"], (name, st)
    assert _title_block(dwg).get(field) == "X-1", (name, _title_block(dwg))


def test_part_name_lands_in_title_and_never_in_part_number():
    """The verbatim live call. A part's NAME is what a title block's TITLE carries; PART NO is
    the identifier a shop floor orders from. Contract rule 1: these are not the same field."""
    dwg = _tb_drawing()
    st = _agent_call("set_titleblock", {"doc_id": dwg, "part_name": "40mm Cube"})
    assert st["ok"], st
    tb = _title_block(dwg)
    assert tb.get("title") == "40mm Cube", tb
    assert tb.get("part_number", "") == "", tb
    assert "part_name" not in tb, tb
    assert any("title" in n for n in st.get("interpreted", [])), st


def test_part_name_beside_an_explicit_title_is_refused_not_filed_as_a_number():
    dwg = _tb_drawing()
    before = _title_block(dwg)
    st = _agent_call("set_titleblock", {"doc_id": dwg, "title": "Plate",
                                       "part_name": "40mm Cube"})
    assert st["ok"] is False, st
    assert "part_number" in st["error"] and "identifier" in st["error"], st
    assert _title_block(dwg) == before, (before, _title_block(dwg))


def test_units_is_refused_and_says_where_units_actually_live():
    """There is no units cell (session.cpp:6946-6957). Units are a DOCUMENT property —
    units.system, MMGS by default — so the refusal must send the caller there, and must not
    write a key nothing renders."""
    dwg = _tb_drawing()
    before = _title_block(dwg)
    st = _agent_call("set_titleblock", {"doc_id": dwg, "units": "mm"})
    assert st["ok"] is False, st
    err = st["error"]
    assert "units.system" in err and "MMGS" in err and "set_units" in err, err
    assert _title_block(dwg) == before, (before, _title_block(dwg))
    assert "units" not in _title_block(dwg), _title_block(dwg)


def test_the_whole_live_titleblock_call_now_lands_except_units():
    """The live call verbatim. `scale` was already accepted and must still be; part_name and
    revision now have homes; units is the one deliberate refusal, so the call is refused as a
    whole and NOTHING is written — a half-applied title block is worse than a refused one."""
    dwg = _tb_drawing()
    before = _title_block(dwg)
    st = _agent_call("set_titleblock", {"doc_id": dwg, "part_name": "40mm Cube",
                                       "revision": "A", "units": "mm", "scale": "1:1"})
    assert st["ok"] is False and "units" in st["error"], st
    assert _title_block(dwg) == before, (before, _title_block(dwg))
    # Drop the one field that has no home and the rest lands, every value read back.
    ok = _agent_call("set_titleblock", {"doc_id": dwg, "part_name": "40mm Cube",
                                       "revision": "A", "scale": "1:1"})
    assert ok["ok"], ok
    tb = _title_block(dwg)
    assert tb.get("title") == "40mm Cube" and tb.get("rev") == "A", tb
    assert tb.get("scale") == "1:1", tb


def test_set_titleblock_still_refuses_a_genuinely_unknown_field():
    dwg = _tb_drawing()
    before = _title_block(dwg)
    st = _agent_call("set_titleblock", {"doc_id": dwg, "colour": "blue"})
    assert st["ok"] is False and "colour" in st["error"], st
    assert "part_number" in st["error"], st  # still teaches the real field list
    assert _title_block(dwg) == before, st


def test_nested_fields_dict_and_flattened_kwargs_still_merge():
    """The 2026-07-27 flattened-kwarg fix must be untouched by the alias layer."""
    dwg = _tb_drawing()
    st = _agent_call("set_titleblock", {"doc_id": dwg, "fields": {"material": "6061"},
                                       "drawn_by": "HK", "rev": "B"})
    assert st["ok"], st
    tb = _title_block(dwg)
    assert tb.get("material") == "6061" and tb.get("drawn_by") == "HK", tb
    assert tb.get("rev") == "B", tb

# ---------------------------------------- Problem 4: dimensioning a FEATURE (pass-2b sweep)
#
# Three live failures in ONE run (2026-07-29, claude-sonnet-4-5) were the same idea: the model
# held the bore's stable sid and tried to dimension it directly.
#   a=["bore","F6356a7"], b=["bore","F6356a7"]  -> "dimension point a must be [x,y] sheet mm"
#   a=["bore","F6356a7"], b omitted             -> missing_argument: b
#   a=["F6356a7","center"], b=["F6356a7","edge"]
# Every projected circle in drawing_get names the model edge (and faces) it was drawn from —
# HLR provenance, core/doc/drawing.cpp — so a sid lands on exactly its own circle and the tool
# hands the engine a point ON that ring. The ring point matters for concentric circles: picking
# their common centre makes the engine choose the smallest circle. Every test below asserts the
# measured VALUE or the anchor — a ⌀6 callout reading 10.0 is a wrong drawing that looks normal.

BIG_D_MM, SMALL_D_MM = 10.0, 6.0


def _plate_with_two_bores() -> str:
    """An 80x50x10 plate with a ⌀10 bore at x=-20 and a ⌀6 bore at x=+20."""
    n = next(_PLATE_N)
    doc = _call("new_part", {"name": f"TwoBore{n}"})["doc_id"]
    r = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(80), "width": cb.dim(50),
                                 "height": cb.dim(10)}),
        cb.feat("hole", "BigBore", {"style": "simple", "end": "through_all",
                                    "diameter": cb.dim(BIG_D_MM)},
                {"face": {"of": "face", "side": "top"}, "points": [[-20.0, 0.0]]}),
        cb.feat("hole", "SmallBore", {"style": "simple", "end": "through_all",
                                      "diameter": cb.dim(SMALL_D_MM)},
                {"face": {"of": "face", "side": "top"}, "points": [[20.0, 0.0]]})]})
    assert r["ok"], r
    return doc

def _concentric_punch() -> str:
    """The live press failure: ⌀9.8 shank and ⌀20 head share one projected centre."""
    n = next(_PLATE_N)
    doc = _call("new_part", {"name": f"ConcentricPunch{n}"})["doc_id"]
    r = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("cylinder", "Shank", {
            "radius": cb.dim(4.9), "height": cb.dim(80)}),
        cb.feat("cylinder", "StrikeHead", {
            "radius": cb.dim(10.0), "height": cb.dim(15),
            "at": [0, 0, 80], "mode": "boss"}),
    ]})
    assert r["ok"], r
    return doc


def _plate_with_twin_bores() -> str:
    """An 80x50x10 plate with TWO identical ⌀6 bores from ONE hole feature."""
    n = next(_PLATE_N)
    doc = _call("new_part", {"name": f"TwinBore{n}"})["doc_id"]
    r = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(80), "width": cb.dim(50),
                                 "height": cb.dim(10)}),
        cb.feat("hole", "Bores", {"style": "simple", "end": "through_all",
                                  "diameter": cb.dim(SMALL_D_MM)},
                {"face": {"of": "face", "side": "top"},
                 "points": [[-20.0, 0.0], [20.0, 0.0]]})]})
    assert r["ok"], r
    return doc


CROSS_HOLE_XS = (-36.0, -12.0, 12.0, 36.0)


def _plate_with_four_cross_holes(blind_third: bool = False) -> str:
    """A 100x10x40 upright plate (thin in Y) with four identical ⌀6 holes drilled along Y at
    x = -36/-12/12/36, so the FRONT and BACK views look straight down every hole. With
    `blind_third`, hole #3 (x=12) is only 5 mm deep, open on the -Y face."""
    n = next(_PLATE_N)
    doc = _call("new_part", {"name": f"CrossHoles{n}"})["doc_id"]
    ops = [cb.feat("box", "Plate", {"length": cb.dim(100), "width": cb.dim(10),
                                    "height": cb.dim(40)})]
    for i, x in enumerate(CROSS_HOLE_XS, start=1):
        blind = blind_third and i == 3
        ops.append(cb.feat("cylinder", f"Hole{i}", {
            "radius": cb.dim(SMALL_D_MM / 2.0), "height": cb.dim(5.0 if blind else 20.0),
            "at": [x, -2.5 if blind else 0.0, 0.0], "axis": [0, 1, 0], "mode": "cut"}))
    r = _call("add_features", {"doc_id": doc, "ops": ops})
    assert r["ok"], r
    return doc


def _cross_hole_sids(doc: str) -> list:
    """The four hole walls' sids, ordered hole #1..#4 by model x."""
    matches = cb.call("doc_query", {"doc_id": doc,
                                    "query": {"of": "face", "surf": "cylinder"}})["matches"]
    assert len(matches) == 4, matches
    return [m["sid"] for m in sorted(matches, key=lambda m: m["centroid"][0])]


def _circle_centres(dwg: str, view_id: str, diameter: float) -> list:
    """Every distinct sheet-mm centre of a projected full circle of *diameter*, sorted by x."""
    full = cb.call("drawing_get", {"doc_id": dwg})
    out: set = set()
    for view in full["sheets"][0]["views"]:
        if view["view_id"] != view_id:
            continue
        scale = view.get("scale") or 1.0
        for arc in view.get("arcs", []):
            if arc.get("closed") and abs(2.0 * arc["radius"] / scale - diameter) < 1e-4:
                out.add(tuple(round(x, 6) for x in arc["center"]))
    return [list(c) for c in sorted(out)]


def _round_sids(doc: str) -> dict:
    """{model radius: sid} for every round FACE of the part, as query_geometry hands them out."""
    matches = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}})["matches"]
    return {round(float(m["radius"]), 6): m["sid"] for m in matches
            if isinstance(m.get("radius"), (int, float)) and m["radius"]}


def _flat_sid(doc: str) -> str:
    matches = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face"}})["matches"]
    return next(m["sid"] for m in matches if not m.get("radius"))


def _plate_drawing(doc: str) -> tuple[str, str, str]:
    """(drawing_doc_id, the TOP view id — where bores project as circles, the FRONT view id)."""
    mk = _call("make_drawing", {"doc_id": doc, "views": ["front", "top"],
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    top = next(v for v, ori in mk["views"].items() if ori == "top")
    front = next(v for v, ori in mk["views"].items() if ori == "front")
    return mk["drawing_doc_id"], top, front


def _circle_center(dwg: str, view_id: str, diameter: float) -> list:
    """The sheet-mm centre of the projected circle of *diameter*, read off the engine."""
    full = cb.call("drawing_get", {"doc_id": dwg})
    for view in full["sheets"][0]["views"]:
        if view["view_id"] != view_id:
            continue
        scale = view.get("scale") or 1.0
        for arc in view.get("arcs", []):
            if arc.get("closed") and abs(2.0 * arc["radius"] / scale - diameter) < 1e-4:
                return [round(x, 6) for x in arc["center"]]
    raise AssertionError(f"no ⌀{diameter} circle in view {view_id}")


def test_dimension_a_bore_by_sid_reads_its_real_diameter():
    """The point of the whole fix: dimension the hole BY SID and the callout must read the
    bore's true ⌀10 — the value comes off the projected circle the engine snapped to."""
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter", "a": sid})
    assert r["ok"], r
    assert abs(r["value"] - HOLE_D_MM) < 1e-6, r
    assert r["resolved"]["a"]["model_diameter"] == HOLE_D_MM, r
    ann = _the_annotation(dwg, top, r["annotation"])
    assert ann["kind"] == "diameter_dim", ann
    assert abs(ann["value"] - HOLE_D_MM) < 1e-6, ann
    # …and it is attached to that circle's own centre, not to some other sheet point.
    assert [round(x, 6) for x in ann["center"]] == _circle_center(dwg, top, HOLE_D_MM), ann


def _views_showing(dwg: str, sid: str) -> set:
    """Ids of the views in which the engine draws a VISIBLE full circle bounding face *sid*."""
    full = cb.call("drawing_get", {"doc_id": dwg})
    return {view["view_id"] for view in full["sheets"][0]["views"]
            for arc in view.get("arcs", [])
            if arc.get("closed") and arc.get("layer") == "visible"
            and sid in ((arc.get("src") or {}).get("face_sids") or [])}


def test_feature_dimensions_pick_the_requested_concentric_ring():
    """⌀9.8 shank (z -40..40) under a ⌀20 head (z 72.5..87.5): both rings share one
    projected centre. The BOTTOM view looks up +Z from below (contracts/doc_schema.md §8), so
    both rims are visible there and each feature name must land on its OWN ring. From the
    TOP the head hides the shank: the shank is refused as hidden and pointed at the bottom
    view."""
    doc = _concentric_punch()
    sids = _round_sids(doc)
    shank_sid, head_sid = sids[4.9], sids[10.0]
    mk = _call("make_drawing", {
        "doc_id": doc, "views": ["top", "bottom"], "model_items": False,
        "hole_callouts": False})
    assert mk["ok"], mk
    dwg = mk["drawing_doc_id"]
    open_end = next(v for v, ori in mk["views"].items() if ori == "bottom")
    head_end = next(v for v, ori in mk["views"].items() if ori == "top")
    assert _views_showing(dwg, shank_sid) == {open_end}
    assert _views_showing(dwg, head_sid) == {open_end, head_end}

    shank = _call("add_dimension", {
        "doc_id": dwg, "view_id": open_end, "dtype": "diameter_dim", "a": "Shank"})
    head = _call("add_dimension", {
        "doc_id": dwg, "view_id": open_end, "dtype": "diameter_dim", "a": "StrikeHead"})
    assert shank["ok"] and shank["value"] == pytest.approx(9.8), shank
    assert head["ok"] and head["value"] == pytest.approx(20.0), head
    assert shank["resolved"]["a"]["sids"] == [shank_sid], shank
    assert head["resolved"]["a"]["sids"] == [head_sid], head
    assert shank["text"] != head["text"]

    hidden = _call("add_dimension", {
        "doc_id": dwg, "view_id": head_end, "dtype": "diameter_dim", "a": "Shank"})
    assert not hidden["ok"], hidden
    assert "HIDDEN" in hidden["error"] and open_end in hidden["error"], hidden
    over = _call("add_dimension", {
        "doc_id": dwg, "view_id": head_end, "dtype": "diameter_dim", "a": "StrikeHead"})
    assert over["ok"] and over["value"] == pytest.approx(20.0), over


#: contracts/doc_schema.md §8: each orientation's (sheet +x, sheet +y) as model axes.
_CONTRACT_SHEET_AXES = {
    "front": ((1, 0, 0), (0, 0, 1)), "back": ((-1, 0, 0), (0, 0, 1)),
    "top": ((1, 0, 0), (0, 1, 0)), "bottom": ((1, 0, 0), (0, -1, 0)),
    "right": ((0, 1, 0), (0, 0, 1)), "left": ((0, -1, 0), (0, 0, 1)),
}


def test_every_view_is_drawn_from_its_contract_side_the_right_way_up():
    """A ⌀12 boss standing on a 100x60x10 plate at model (+30, +10, top). In every orthographic
    view the boss must sit where the contract frame puts it relative to the view's centre — a
    front view drawn from +Y came out upside down (boss BELOW the plate) and every top view
    was really seen from underneath — and the boss's top rim is visible only from the top."""
    n = next(_PLATE_N)
    doc = _call("new_part", {"name": f"OrientBoss{n}"})["doc_id"]
    r = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(100), "width": cb.dim(60),
                                 "height": cb.dim(10)}),
        cb.feat("cylinder", "Boss", {"radius": cb.dim(6), "height": cb.dim(20),
                                     "at": [30, 10, 15], "mode": "boss"})]})
    assert r["ok"], r
    boss_sid = _round_sids(doc)[6.0]
    mk = _call("make_drawing", {"doc_id": doc, "views": list(_CONTRACT_SHEET_AXES),
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    full = cb.call("drawing_get", {"doc_id": mk["drawing_doc_id"]})
    by_orient = {v["orientation"]: v for v in full["sheets"][0]["views"]}
    # The boss's offset from the part's bbox centre: +30 X, +10 Y, +10 Z (boss mid-height
    # z=15 vs the part's z-centre of 10).
    offset = (30.0, 10.0, 10.0)
    for orient, (sx, sy) in _CONTRACT_SHEET_AXES.items():
        view = by_orient[orient]
        pts = [p for layer in ("visible", "hidden")
               for pl, src in zip(view[layer], view[f"{layer}_src"])
               if boss_sid in ((src or {}).get("face_sids") or []) for p in pl]
        assert pts, orient
        x0, y0, x1, y1 = view["bbox"]
        dx = sum(p[0] for p in pts) / len(pts) - (x0 + x1) / 2.0
        dy = sum(p[1] for p in pts) / len(pts) - (y0 + y1) / 2.0
        want_x = sum(a * b for a, b in zip(sx, offset))
        want_y = sum(a * b for a, b in zip(sy, offset))
        assert dx * want_x > 0 and dy * want_y > 0, (orient, dx, dy, want_x, want_y)
        top_rim_visible = any(
            a.get("closed") and a.get("layer") == "visible"
            and boss_sid in ((a.get("src") or {}).get("face_sids") or [])
            for a in view.get("arcs", []))
        assert top_rim_visible == (orient == "top"), (orient, view.get("arcs"))


def test_the_verbatim_live_call_now_lands():
    """add_dimension(dtype="diameter", a=["bore","F…"], b=["bore","F…"], pos=[80,120]) — the
    exact call that answered "dimension point a must be [x,y] sheet mm"."""
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                "a": ["Bore", sid], "b": ["Bore", sid], "pos": [80, 120]})
    assert r["ok"], r
    assert abs(r["value"] - HOLE_D_MM) < 1e-6, r
    ann = _the_annotation(dwg, top, r["annotation"])
    assert [round(x, 3) for x in ann["pos"]] == [80.0, 120.0], ann
    assert any("b was not needed" in n for n in r["interpreted"]), r


def test_b_is_optional_for_an_arc_determined_dimension():
    """The second live call omitted b and died on this tool's SIGNATURE — the engine never
    looks at b for diameter/radial/center_mark."""
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    dwg, top, _front = _plate_drawing(doc)
    for dtype, expect in (("diameter", HOLE_D_MM), ("radius", HOLE_D_MM / 2.0),
                          ("center_mark", 0.0)):
        r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": dtype, "a": sid})
        assert r["ok"], (dtype, r)
        if dtype != "center_mark":
            assert abs(r["value"] - expect) < 1e-6, (dtype, r)


def test_a_sid_with_a_positional_qualifier_resolves_and_says_so():
    """The earlier pass wrote a=[sid,"center"], b=[sid,"edge"]. For a radius the arc determines
    both points, so the qualifier carries nothing — it is dropped WITH a note, not silently."""
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "radius",
                                "a": [sid, "center"], "b": [sid, "edge"]})
    assert r["ok"], r
    assert abs(r["value"] - HOLE_D_MM / 2.0) < 1e-6, r
    assert any("qualifier 'center'" in n for n in r["interpreted"]), r


def test_the_sid_picks_the_RIGHT_circle_when_the_part_has_two():
    """The anti-guess test. Two bores of DIFFERENT diameter project into the same view; each
    sid must dimension its own circle — proved by the value AND by the anchor point."""
    doc = _plate_with_two_bores()
    sids = _round_sids(doc)
    dwg, top, _front = _plate_drawing(doc)
    for diameter in (BIG_D_MM, SMALL_D_MM):
        r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                    "a": sids[diameter / 2.0]})
        assert r["ok"], (diameter, r)
        assert abs(r["value"] - diameter) < 1e-6, (diameter, r)
        ann = _the_annotation(dwg, top, r["annotation"])
        assert [round(x, 6) for x in ann["center"]] == _circle_center(dwg, top, diameter), \
            (diameter, ann)


def test_a_feature_name_resolves_like_a_sid():
    """The live call passed the feature NAME beside the sid ("bore"), so a bare name must work:
    it resolves to the round face that hole feature created."""
    doc = _plate_with_two_bores()
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                "a": "SmallBore"})
    assert r["ok"], r
    assert abs(r["value"] - SMALL_D_MM) < 1e-6, r
    assert "SmallBore" in " ".join(r["interpreted"]), r


def test_identical_holes_each_land_on_their_own_circle():
    """Two ⌀6 bores from one feature measure the same, but each wall has its own sid and the
    projected circles carry it — so each sid dimensions its own circle, and the shared feature
    NAME (two circles) is refused with both sids rather than guessed."""
    doc = _plate_with_twin_bores()
    matches = cb.call("doc_query", {"doc_id": doc,
                                    "query": {"of": "face", "surf": "cylinder"}})["matches"]
    sids = [m["sid"] for m in sorted(matches, key=lambda m: m["centroid"][0])]
    assert len(sids) == 2, matches
    dwg, top, _front = _plate_drawing(doc)
    centres = _circle_centres(dwg, top, SMALL_D_MM)
    assert len(centres) == 2, centres
    anchors = []
    for sid in sids:
        r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                    "a": sid})
        assert r["ok"], (sid, r)
        assert abs(r["value"] - SMALL_D_MM) < 1e-6, (sid, r)
        assert r["resolved"]["a"]["sids"] == [sid], r
        anchors.append([round(x, 6) for x in _the_annotation(dwg, top, r["annotation"])["center"]])
    assert sorted(anchors) == centres, (anchors, centres)
    both = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                   "a": "Bores"})
    assert both["ok"] is False, both
    assert "2 separate" in both["error"], both
    assert all(sid in both["error"] for sid in sids), both


def test_hole_three_of_four_identical_holes_lands_on_hole_three_in_the_front_view():
    """Four identical ⌀6 cross holes: dimensioning hole #3's sid must attach to hole #3's
    projected centre — the third of four equal circles along the front view's +X — not to
    whichever equal circle a size match would find first."""
    doc = _plate_with_four_cross_holes()
    hole3 = _cross_hole_sids(doc)[2]
    mk = _call("make_drawing", {"doc_id": doc, "views": ["front", "top"],
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    dwg = mk["drawing_doc_id"]
    front = next(v for v, ori in mk["views"].items() if ori == "front")
    centres = _circle_centres(dwg, front, SMALL_D_MM)
    assert len(centres) == 4, centres
    r = _call("add_dimension", {"doc_id": dwg, "view_id": front, "dtype": "diameter",
                                "a": hole3})
    assert r["ok"], r
    assert abs(r["value"] - SMALL_D_MM) < 1e-6, r
    assert r["resolved"]["a"]["sids"] == [hole3], r
    ann = _the_annotation(dwg, front, r["annotation"])
    assert [round(x, 6) for x in ann["center"]] == centres[2], (ann, centres)
    spacing = _call("add_dimension", {"doc_id": dwg, "view_id": front, "dtype": "linear",
                                      "a": _cross_hole_sids(doc)[1], "b": hole3})
    assert spacing["ok"], spacing
    assert abs(spacing["value"] - (CROSS_HOLE_XS[2] - CROSS_HOLE_XS[1])) < 1e-4, spacing


def test_a_hidden_sid_is_refused_naming_the_views_where_it_is_visible():
    """Blind hole #3 opens on one face only: looking at the other face it exists solely as
    hidden lines. There the sid is refused BY NAME and the view where it is visible is named;
    from the open side the same sid dimensions normally."""
    doc = _plate_with_four_cross_holes(blind_third=True)
    hole3 = _cross_hole_sids(doc)[2]
    mk = _call("make_drawing", {"doc_id": doc, "views": ["front", "back"],
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    dwg = mk["drawing_doc_id"]
    replies = {vid: _call("add_dimension", {"doc_id": dwg, "view_id": vid,
                                            "dtype": "diameter", "a": hole3})
               for vid in mk["views"]}
    shown = [vid for vid, r in replies.items() if r["ok"]]
    hidden = [vid for vid, r in replies.items() if not r["ok"]]
    assert len(shown) == 1 and len(hidden) == 1, replies
    assert abs(replies[shown[0]]["value"] - SMALL_D_MM) < 1e-6, replies
    error = replies[hidden[0]]["error"]
    assert "HIDDEN" in error and hole3 in error, error
    assert shown[0] in error, error
    assert _stored_annotations(dwg, hidden[0]) == [], _stored_annotations(dwg, hidden[0])


def test_a_view_that_does_not_show_the_circle_is_refused_and_names_one_that_does():
    """The bore projects as hidden lines in the FRONT view, so there is nothing to attach a
    diameter to. Refuse, and name the view where it IS a circle."""
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    dwg, top, front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": front, "dtype": "diameter", "a": sid})
    assert r["ok"] is False, r
    assert top in r["error"], r
    assert "8" in r["error"], r          # ⌀8 — the diameter it resolved to
    assert _stored_annotations(dwg, front) == [], _stored_annotations(dwg, front)


def test_a_planar_face_sid_is_refused_with_the_query_that_finds_a_round_one():
    doc = _plate_with_hole()
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                "a": _flat_sid(doc)})
    assert r["ok"] is False, r
    assert "no radius" in r["error"] and "cylinder" in r["error"], r
    assert _stored_annotations(dwg, top) == [], _stored_annotations(dwg, top)


def test_an_unresolvable_target_is_refused_naming_where_current_sids_come_from():
    doc = _plate_with_hole()
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                "a": "Fdeadbee"})
    assert r["ok"] is False, r
    assert "describe_part" in r["error"], r
    r2 = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                 "a": "the big hole"})
    assert r2["ok"] is False, r2
    assert "Bore" in r2["error"], r2     # the real feature roster, so the model can retry
    assert _stored_annotations(dwg, top) == [], _stored_annotations(dwg, top)


def test_a_single_linear_dimension_rejects_a_round_model_target():
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "linear", "a": sid})
    assert r["ok"] is False, r
    assert "not a straight model edge SID" in r["error"], r
    assert _stored_annotations(dwg, top) == [], _stored_annotations(dwg, top)


def test_a_linear_dimension_between_two_bores_by_sid_measures_their_spacing():
    """Model targets are not diameter-only: the two bores sit 40 mm apart in the model, and the
    engine divides the sheet distance by the view scale, so the dimension must read 40."""
    doc = _plate_with_two_bores()
    sids = _round_sids(doc)
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "linear",
                                "a": sids[BIG_D_MM / 2.0], "b": sids[SMALL_D_MM / 2.0]})
    assert r["ok"], r
    assert abs(r["value"] - 40.0) < 1e-4, r
    assert r["resolved"]["b"]["model_diameter"] == SMALL_D_MM, r


def test_two_targets_on_the_same_circle_are_refused_not_measured_as_zero():
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "linear",
                                "a": sid, "b": sid})
    assert r["ok"] is False, r
    assert "SAME circle" in r["error"], r
    assert _stored_annotations(dwg, top) == [], _stored_annotations(dwg, top)


def test_coordinates_mixed_with_a_name_are_refused_rather_than_guessed():
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                "a": [80, sid]})
    assert r["ok"] is False, r
    assert "mixes sheet coordinates with names" in r["error"], r


def test_sheet_point_dimensions_are_untouched_by_the_target_layer():
    """The pre-existing dialect must still work byte-for-byte, including a stringified point."""
    doc = _plate_with_hole()
    dwg, top, center = _drawing_with_circle(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                "a": center, "b": center})
    assert r["ok"] and abs(r["value"] - HOLE_D_MM) < 1e-6, r
    assert "resolved" not in r, r
    lin = _call("add_dimension", {"doc_id": dwg, "view_id": top,
                                  "a": json.dumps(center),
                                  "b": json.dumps([center[0] + 12.0, center[1]])})
    assert lin["ok"] and lin["dtype"] == "linear_dim", lin
    assert lin["value"] > 0.0, lin


# ------------------------------------- Problem 5: make_drawing scale / orientation / name
#
# Four more pass-2b unknown_argument failures. Two of the three names denote something the
# engine really has (a geometric view scale; the drawing document's name) and are wired; the
# third (sheet orientation) does not exist at all and is refused rather than ignored.


def _cube(mm: float = 40.0) -> str:
    n = next(_PLATE_N)
    doc = _call("new_part", {"name": f"Cube{n}"})["doc_id"]
    r = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Cube", {"length": cb.dim(mm), "width": cb.dim(mm),
                                "height": cb.dim(mm)})]})
    assert r["ok"], r
    return doc


def _views(dwg: str) -> list:
    return cb.call("drawing_get", {"doc_id": dwg})["sheets"][0]["views"]


def _drawing_name(dwg: str) -> str:
    return cb.call("drawing_get", {"doc_id": dwg})["tree"]["name"]


def test_scale_is_a_real_geometric_view_scale():
    """scale=1.0 must draw the 40 mm cube 40 mm wide on the sheet (the engine otherwise
    auto-scales to fill the paper), on EVERY view, with the title block's text to match."""
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front", "top"],
                                "model_items": False, "hole_callouts": False, "scale": 1.0})
    assert mk["ok"], mk
    assert mk["scale"] == 1.0 and mk["scale_text"] == "1:1", mk
    for view in _views(mk["drawing_doc_id"]):
        assert view["scale"] == 1.0, view
        bbox = view["bbox"]
        assert abs((bbox[2] - bbox[0]) - 40.0) < 1e-3, view
    assert _title_block(mk["drawing_doc_id"])["scale"] == "1:1", mk


def test_a_ratio_scale_halves_the_drawn_geometry():
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, "hole_callouts": False, "scale": "1:2"})
    assert mk["ok"], mk
    assert mk["scale"] == 0.5 and mk["scale_text"] == "1:2", mk
    view = _views(mk["drawing_doc_id"])[0]
    assert view["scale"] == 0.5, view
    assert abs((view["bbox"][2] - view["bbox"][0]) - 20.0) < 1e-3, view
    assert _title_block(mk["drawing_doc_id"])["scale"] == "1:2", mk


def _offset_block() -> str:
    """A 40x30x20 block built AWAY from the origin (`at`), so its projected bbox centre is not
    the model origin — the only shape that can tell a correct reposition from a missing one."""
    n = next(_PLATE_N)
    doc = _call("new_part", {"name": f"Offset{n}"})["doc_id"]
    r = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(40), "width": cb.dim(30),
                                 "height": cb.dim(20), "at": [60.0, 20.0, 30.0]})]})
    assert r["ok"], r
    return doc


def test_an_explicit_scale_keeps_the_projection_alignment():
    """An explicit scale is laid out by the engine BEFORE dimensioning: at 1:1 and 2:1 the top
    view stays on the front view's vertical projection line and every view stays on the sheet.
    Built off-origin so a missing reposition would show as drift."""
    doc = _offset_block()
    for scale in (1.0, 2.0):
        mk = _call("make_drawing", {"doc_id": doc, "views": ["front", "top"],
                                    "model_items": False, "hole_callouts": False,
                                    "scale": scale})
        assert mk["ok"], (scale, mk)
        assert not mk.get("warnings"), (scale, mk)
        by = {v["orientation"]: v["bbox"] for v in _views(mk["drawing_doc_id"])}
        fx = (by["front"][0] + by["front"][2]) / 2.0
        tx = (by["top"][0] + by["top"][2]) / 2.0
        assert abs(fx - tx) < 1e-3, (scale, by)
        assert abs((by["front"][2] - by["front"][0]) - 40.0 * scale) < 1e-3, (scale, by)
        assert by["top"][1] > by["front"][3], (scale, by)   # third angle: top above front


def test_a_scale_contradicting_the_printed_scale_cell_is_refused():
    """The two "scale"s must not be reconciled by picking one: a sheet whose SCALE cell says
    1:1 while its views are 2:1 is a wrong drawing that looks right."""
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, "scale": 2.0,
                                "title": {"scale": "1:1"}})
    assert mk["ok"] is False, mk
    assert "2:1" in mk["error"] and "1:1" in mk["error"], mk
    assert "drawing_doc_id" not in mk, mk


def test_a_matching_printed_scale_cell_is_accepted():
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, "scale": 0.5,
                                "title": {"scale": "1:2", "title": "CUBE"}})
    assert mk["ok"], mk
    tb = _title_block(mk["drawing_doc_id"])
    assert tb["scale"] == "1:2" and tb["title"] == "CUBE", tb
    assert _views(mk["drawing_doc_id"])[0]["scale"] == 0.5, mk


def test_a_value_that_is_not_a_scale_factor_is_refused_naming_both_concepts():
    for bad in ("big", 0, -1):
        mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                    "model_items": False, "scale": bad})
        assert mk["ok"] is False, (bad, mk)
        assert "set_titleblock" in mk["error"], (bad, mk)   # names the TEXT cell too
        assert "drawing_doc_id" not in mk, (bad, mk)


def test_a_scale_that_overflows_the_sheet_says_so():
    """Accepted (the caller asked for it) but never silently: the reply carries the warning."""
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, "scale": 20})
    assert mk["ok"], mk
    assert any("past the" in w for w in mk.get("warnings", [])), mk


def test_landscape_orientation_keeps_the_landscape_sheet():
    """orientation="landscape" is the SHEET's orientation and the default one."""
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, "orientation": "landscape"})
    assert mk["ok"], mk
    assert _sheet_size(mk["drawing_doc_id"]) == "A3", mk
    assert any("sheet" in n for n in mk["interpreted"]), mk


def test_portrait_orientation_rotates_the_paper():
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"], "sheet": "A4",
                                "model_items": False, "orientation": "portrait"})
    assert mk["ok"], mk
    assert _sheet_size(mk["drawing_doc_id"]) == "A4P", mk
    full = cb.call("drawing_get", {"doc_id": mk["drawing_doc_id"]})
    assert full["sheets"][0]["size_mm"] == [210.0, 297.0], full["sheets"][0]["size_mm"]


def test_first_angle_projection_and_arrange_drawing():
    """projection="first" mirrors the lattice; arrange_drawing re-lays it third-angle at an
    explicit standard scale, and the title block echoes scale + projection."""
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front", "top", "right"],
                                "model_items": False, "projection": "first"})
    assert mk["ok"], mk
    by = {v["orientation"]: v["bbox"] for v in _views(mk["drawing_doc_id"])}
    assert by["top"][3] < by["front"][1] and by["right"][2] < by["front"][0], by
    assert _title_block(mk["drawing_doc_id"])["projection"] == "first"
    ar = _call("arrange_drawing", {"doc_id": mk["drawing_doc_id"], "projection": "third",
                                   "scale": "1:2"})
    assert ar["ok"] and ar["scale"] == 0.5 and ar["projection"] == "third", ar
    by = {v["orientation"]: v["bbox"] for v in _views(mk["drawing_doc_id"])}
    assert by["top"][1] > by["front"][3] and by["right"][0] > by["front"][2], by
    assert abs((by["front"][2] - by["front"][0]) - 20.0) < 1e-3, by
    tb = _title_block(mk["drawing_doc_id"])
    assert tb["scale"] == "1:2" and tb["projection"] == "third", tb


def test_extended_title_block_fields_are_accepted():
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"], "model_items": False,
                                "title": {"title": "CUBE", "checked": "QA",
                                          "general_tolerance": "ISO 2768-m", "mass": "0.17 kg"}})
    assert mk["ok"], mk
    tb = _title_block(mk["drawing_doc_id"])
    assert tb["checked_by"] == "QA" and tb["general_tolerance"] == "ISO 2768-m", tb
    frame_text = {t["text"] for t in cb.call("drawing_get", {"doc_id": mk["drawing_doc_id"]})
                  ["sheets"][0]["frame"]["texts"]}
    assert {"QA", "0.17 kg", "CUBE"} <= frame_text, frame_text


def test_orientation_naming_views_is_read_as_views():
    """"front"/["front","top"] IS a view orientation — the one real meaning of the word here."""
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "model_items": False,
                                "hole_callouts": False, "orientation": ["front", "top"]})
    assert mk["ok"], mk
    assert sorted(mk["views"].values()) == ["front", "top"], mk
    assert any("views=" in n for n in mk["interpreted"]), mk


def test_orientation_disagreeing_with_views_is_refused():
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "model_items": False,
                                "views": ["front"], "orientation": "top"})
    assert mk["ok"] is False, mk
    assert "both name which views" in mk["error"], mk
    assert "drawing_doc_id" not in mk, mk


def test_name_names_the_drawing_document_and_not_the_title_block():
    """name= is the drawing DOC's name (drawing_make already reads spec["name"]); the printed
    title stays the part's, because those are two different fields."""
    doc = _cube(40.0)
    mk = _call("make_drawing", {"doc_id": doc, "views": ["front"], "model_items": False,
                                "hole_callouts": False, "name": "40mm_cube_drawing"})
    assert mk["ok"], mk
    assert _drawing_name(mk["drawing_doc_id"]) == "40mm_cube_drawing", mk
    assert mk["name"] == "40mm_cube_drawing", mk
    assert mk["part_name"].startswith("Cube"), mk
    assert _title_block(mk["drawing_doc_id"])["title"] == mk["part_name"], mk


def test_name_and_title_land_in_their_own_slots():
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, "name": "DWG-001",
                                "title": "40mm Cube"})
    assert mk["ok"], mk
    assert _drawing_name(mk["drawing_doc_id"]) == "DWG-001", mk
    assert _title_block(mk["drawing_doc_id"])["title"] == "40mm Cube", mk


def test_without_a_name_the_engine_default_still_applies():
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    assert _drawing_name(mk["drawing_doc_id"]).endswith(" - Drawing"), mk
    assert "scale" not in mk, mk           # no scale asked for -> no scale reported


# --- the shadowing re-test (contract rule 2) ------------------------------------------------
# Declaring a parameter can SHADOW a spelling that already reconciled (declaring `source_doc`
# broke `source_doc_id` until it too was declared). scale / orientation / name are three new
# declarations, so the neighbouring spellings are re-measured here.


@pytest.mark.parametrize("name", ["view_scale", "drawing_scale", "scale"])
def test_scale_neighbour_spellings_still_reach_the_scale_slot(name):
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, name: 1.0})
    assert mk["ok"], (name, mk)
    assert _views(mk["drawing_doc_id"])[0]["scale"] == 1.0, (name, mk)


@pytest.mark.parametrize("name", ["drawing_name", "name"])
def test_name_neighbour_spellings_still_reach_the_name_slot(name):
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, name: "DWG-7"})
    assert mk["ok"], (name, mk)
    assert _drawing_name(mk["drawing_doc_id"]) == "DWG-7", (name, mk)


def test_the_earlier_fixes_still_work_beside_the_new_parameters():
    """source_doc / template / title / model_items all resolved BEFORE scale, orientation and
    name were declared — re-measured together, in one call, after."""
    doc = _plain_plate()
    mk = _call("make_drawing", {"source_doc": doc, "template": "A4", "title": "90x50x12 Plate",
                                "model_items": "basic_dims", "views": ["front"],
                                "scale": 1.0, "name": "PLATE-DWG"})
    assert mk["ok"], mk
    assert _drawn_source(mk["drawing_doc_id"]) == doc, mk
    assert _sheet_size(mk["drawing_doc_id"]) == "A4", mk
    assert _title_block(mk["drawing_doc_id"])["title"] == "90x50x12 Plate", mk
    assert _drawing_name(mk["drawing_doc_id"]) == "PLATE-DWG", mk
    assert mk["auto_dimensions_added"] > 0 and _auto_dims(mk["drawing_doc_id"]) > 0, mk
    assert _views(mk["drawing_doc_id"])[0]["scale"] == 1.0, mk


def test_both_dispatch_paths_accept_a_model_target_and_the_new_parameters():
    """The desktop copilot dispatches through agent_turn._dispatch_tool, not FastMCP, and the
    two paths read signatures separately — a parameter that works on one can be dropped on the
    other. Both are measured, on the same values, reading the result off the drawing."""
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    mk = _agent_call("make_drawing", {"doc_id": doc, "views": ["front", "top"],
                                      "model_items": False, "hole_callouts": False,
                                      "scale": 1.0, "name": "AGENT-DWG"})
    assert mk["ok"], mk
    dwg = mk["drawing_doc_id"]
    assert _drawing_name(dwg) == "AGENT-DWG", mk
    assert all(v["scale"] == 1.0 for v in _views(dwg)), mk
    top = next(v for v, ori in mk["views"].items() if ori == "top")
    r = _agent_call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                      "a": ["Bore", sid], "pos": [80, 120]})
    assert r["ok"], r
    assert abs(r["value"] - HOLE_D_MM) < 1e-6, r
    assert _the_annotation(dwg, top, r["annotation"])["kind"] == "diameter_dim", r
    orient = _agent_call("make_drawing", {"doc_id": doc, "views": ["front"],
                                          "model_items": False, "orientation": "portrait"})
    assert orient["ok"] and orient["sheet"] == "A3P", orient


# ------------------------------- Problem 6: the sheet is a place, and things can miss it
#
# Measured 2026-07-30 driving the whole "make a dimensioned 2D drawing" flow on a 50x30x4
# plate. Five separate ways a drawing tool reported success while producing nothing, the wrong
# thing, or something that had left the paper. Every assertion below is on a NUMBER read back
# off the engine's own projection (bbox, entity count, measured value, exported bytes) — never
# on ok:true, which is exactly what each of these bugs already returned.


def _sheet_drawing() -> tuple[str, dict]:
    """(drawing_doc_id, the make_drawing reply) for a 60x40x10 plate, front + top, A3."""
    mk = _call("make_drawing", {"doc_id": _plate_with_hole(), "views": ["front", "top"],
                                "sheet": "A3", "model_items": False, "hole_callouts": False})
    assert mk["ok"], mk
    return mk["drawing_doc_id"], mk


def _view(dwg: str, view_id: str) -> dict:
    return next(v for v in _views(dwg) if v.get("view_id") == view_id)


def test_make_drawing_reports_how_much_geometry_each_view_actually_got():
    """"A view that renders nothing" is the top drawing defect class and used to be invisible
    in the reply. Every view of a solid plate must count real projected entities."""
    dwg, mk = _sheet_drawing()
    assert set(mk["view_entities"]) == set(mk["views"]), mk
    for vid, n in mk["view_entities"].items():
        assert n > 0, (vid, mk)
        got = _view(dwg, vid)
        assert n == len(got.get("visible") or []) + len(got.get("arcs") or []), (vid, mk)
    assert not mk.get("warnings"), mk


def test_a_single_axis_move_actually_moves_the_view():
    """SILENT DROP: `if x is not None and y is not None` threw away set_view(x=…) and still
    answered ok:true with the view's unchanged position."""
    dwg, _mk = _sheet_drawing()
    was = _view(dwg, "v1")["position"]
    r = _call("set_view", {"doc_id": dwg, "view_id": "v1", "x": was[0] + 25.0})
    assert r["ok"], r
    assert abs(r["position"][0] - (was[0] + 25.0)) < 1e-6, (was, r)
    assert abs(r["position"][1] - was[1]) < 1e-6, (was, r)   # the untouched axis is HELD
    assert abs(_view(dwg, "v1")["position"][0] - (was[0] + 25.0)) < 1e-6, _view(dwg, "v1")


def test_a_move_off_the_sheet_is_reported_with_the_numbers():
    """set_view(x=900,y=900) on a 420x297 sheet answered a bare ok:true and the view was gone
    from the render and the export with nothing saying so."""
    dwg, _mk = _sheet_drawing()
    r = _call("set_view", {"doc_id": dwg, "view_id": "v1", "x": 900.0, "y": 900.0})
    assert r["ok"], r
    assert r["sheet_mm"] == [420.0, 297.0], r
    assert r["on_sheet"] is False, r
    assert r["off_sheet_by_mm"]["right"] > 400.0, r
    assert any("hangs off" in w for w in r["warnings"]), r
    # …and a move back onto the paper clears it, so the flag tracks the state, not the history.
    back = _call("set_view", {"doc_id": dwg, "view_id": "v1", "x": 60.0, "y": 60.0})
    assert back["on_sheet"] is True and "off_sheet_by_mm" not in back, back


def test_two_views_stacked_on_each_other_report_the_overlap_area():
    dwg, _mk = _sheet_drawing()
    at = _view(dwg, "v2")["position"]
    r = _call("set_view", {"doc_id": dwg, "view_id": "v1", "x": at[0], "y": at[1]})
    assert r["ok"], r
    lap = next(o for o in r["overlaps"] if o["view_id"] == "v2")
    assert lap["overlap_mm"][0] > 0.0 and lap["overlap_mm"][1] > 0.0, r
    assert any("overlaps" in w for w in r["warnings"]), r


def test_a_detail_circle_enclosing_nothing_is_refused_and_leaves_no_empty_view():
    """The engine already filed "detail circle encloses no parent geometry" ON the view; the
    tool answered ok:true and left a blank magnified circle on the sheet."""
    dwg, _mk = _sheet_drawing()
    before = [v["view_id"] for v in _views(dwg)]
    r = _call("add_detail_view", {"doc_id": dwg, "parent_view_id": "v2",
                                  "cx": 5.0, "cy": 5.0, "radius": 4.0})
    assert r["ok"] is False, r
    assert "encloses none" in r["error"] and "sheet mm" in r["error"], r
    assert [v["view_id"] for v in _views(dwg)] == before, _views(dwg)


def test_a_detail_view_takes_an_orientation_word_like_its_siblings_and_has_geometry():
    """parent_view_id="top" was rejected outright ("detail parent view 'top' not found")
    although add_dimension / add_gdt / add_datum / add_section_view all take the word."""
    dwg, _mk = _sheet_drawing()
    top = _view(dwg, "v2")
    bb = top["bbox"]
    r = _call("add_detail_view", {"doc_id": dwg, "parent_view_id": "top",
                                  "cx": (bb[0] + bb[2]) / 2.0, "cy": (bb[1] + bb[3]) / 2.0,
                                  "radius": max(bb[2] - bb[0], bb[3] - bb[1]) / 4.0})
    assert r["ok"], r
    assert r["parent_view_id"] == "v2", r
    assert r["entities"] > 0, r


def test_a_surface_finish_takes_an_orientation_word_like_its_siblings():
    dwg, _mk = _sheet_drawing()
    bb = _view(dwg, "v2")["bbox"]
    r = _call("add_surface_finish", {"doc_id": dwg, "view_id": "top", "ra": "1.6",
                                     "a": [bb[0], bb[1]], "pos": [bb[0], bb[1] - 10.0]})
    assert r["ok"] and r["annotation"], r

def test_free_text_note_takes_one_anchor_and_preserves_text():
    dwg, _mk = _sheet_drawing()
    bb = _view(dwg, "v2")["bbox"]
    r = _call("add_dimension", {
        "doc_id": dwg, "view_id": "top", "dtype": "note",
        "a": [bb[0], bb[1]], "text": "BREAK SHARP EDGES 0.5"})
    assert r["ok"], r
    ann = _the_annotation(dwg, "v2", r["annotation"])
    assert ann["text"] == "BREAK SHARP EDGES 0.5"


def test_annotation_can_be_patched_and_deleted_without_rebuilding_sheet():
    dwg, _mk = _sheet_drawing()
    bb = _view(dwg, "v2")["bbox"]
    made = _call("add_dimension", {
        "doc_id": dwg, "view_id": "v2",
        "a": [bb[0], bb[1]], "b": [bb[2], bb[1]]})
    ann_id = made["annotation"]
    patched = _call("edit_drawing_annotation", {
        "doc_id": dwg, "view_id": "top", "annotation_id": ann_id,
        "fields": {"suffix": " TYP", "precision": 1}})
    assert patched["ok"], patched
    assert _the_annotation(dwg, "v2", ann_id)["text"].endswith(" TYP")
    removed = _call("edit_drawing_annotation", {
        "doc_id": dwg, "view_id": "v2", "annotation_id": ann_id,
        "delete": True})
    assert removed["ok"] and removed["deleted"] == ann_id, removed
    assert ann_id not in [ann.get("id") for ann in _stored_annotations(dwg, "v2")]

def test_review_pairs_pixels_with_layout_and_material_warnings():
    from services.mcp import server

    dwg, _mk = _sheet_drawing()
    tb = _call("set_titleblock", {
        "doc_id": dwg,
        "fields": {
            "title": "STEEL PLATE", "part_number": "P-1", "material": "A36 STEEL",
            "scale": "1:1", "rev": "A",
        },
    })
    assert tb["ok"], tb
    raw = asyncio.run(server.mcp.call_tool("review_drawing", {"doc_id": dwg}))
    content = raw[0] if isinstance(raw, tuple) else raw
    assert any(getattr(item, "type", "") == "image" for item in content), content
    text_items = [item for item in content if getattr(item, "type", "") == "text"]
    assert text_items, content
    summary = json.loads(text_items[-1].text)
    assert summary["views"] and summary["sheet_mm"] == [420.0, 297.0], summary
    assert summary["source_material"] == "alu_6061", summary
    assert any("material mismatch" in warning for warning in summary["warnings"]), summary
    assert summary["manufacturing_ready"] is False


def test_a_dimension_between_two_picks_that_touch_nothing_is_refused():
    """A confident "5" measured at the sheet origin, on a drawing that otherwise looks
    finished, is the silently-wrong shape this whole file exists to prevent. The view's bbox
    is the exact extent of its projected geometry, so BOTH picks outside it cannot be
    measuring the part under any pick tolerance."""
    dwg, _mk = _sheet_drawing()
    before = len(_stored_annotations(dwg, "v2"))
    r = _call("add_dimension", {"doc_id": dwg, "view_id": "v2", "a": [0.0, 0.0],
                                "b": [10.0, 0.0]})
    assert r["ok"] is False, r
    assert "neither pick is on view v2" in r["error"], r
    assert "sheet mm" in r["error"], r
    assert len(_stored_annotations(dwg, "v2")) == before, _stored_annotations(dwg, "v2")


def test_a_dimension_across_the_real_view_measures_the_real_width():
    """The other side of the guard: picks ON the projection are untouched, and the number the
    engine reports is the plate's true 60 mm width whatever the auto-fit scale is."""
    dwg, _mk = _sheet_drawing()
    bb = _view(dwg, "v2")["bbox"]
    r = _call("add_dimension", {"doc_id": dwg, "view_id": "v2",
                                "a": [bb[0], bb[1]], "b": [bb[2], bb[1]]})
    assert r["ok"], r
    assert abs(r["value"] - 60.0) < 1e-6, (r, _view(dwg, "v2")["scale"])
    assert "interpreted" not in r, r


def test_one_pick_off_the_projection_is_reported_but_not_refused():
    """A witness line extended past the silhouette is legitimate, so one stray pick earns a
    note naming the view's real bounds rather than a refusal."""
    dwg, _mk = _sheet_drawing()
    bb = _view(dwg, "v2")["bbox"]
    r = _call("add_dimension", {"doc_id": dwg, "view_id": "v2",
                                "a": [bb[0], bb[1]], "b": [bb[2] + 30.0, bb[1]]})
    assert r["ok"], r
    assert any("outside view v2" in n for n in r["interpreted"]), r


def test_make_drawing_title_dict_goes_through_the_titleblock_vocabulary():
    """SILENT DROP: title={"part_name": …} reached drawing_make untouched and was merged into
    the sheet raw, so the part name sat in a key neither the renderer nor the exporter reads
    and never appeared anywhere. set_titleblock has refused that spelling since 2026-07-29;
    make_drawing must fold it onto TITLE the same way."""
    mk = _call("make_drawing", {"doc_id": _plain_plate(), "views": ["front"],
                                "model_items": False,
                                "title": {"part_name": "Plate 50x30x4", "material": "6061"}})
    assert mk["ok"], mk
    tb = _title_block(mk["drawing_doc_id"])
    assert tb.get("title") == "Plate 50x30x4", tb
    assert "part_name" not in tb, tb
    assert tb.get("material") == "6061", tb
    assert any("title" in n for n in mk["interpreted"]), mk


def test_make_drawing_refuses_a_title_field_nothing_renders():
    mk = _call("make_drawing", {"doc_id": _plain_plate(), "views": ["front"],
                                "model_items": False, "title": {"colour": "blue"}})
    assert mk["ok"] is False, mk
    assert "colour" in mk["error"] and "part_number" in mk["error"], mk


def test_set_titleblock_guards_the_nested_fields_door_too():
    """Only the flattened-kwarg door was guarded; fields={"part_name": …} stored a dead key."""
    dwg = _tb_drawing()
    st = _agent_call("set_titleblock", {"doc_id": dwg, "fields": {"part_name": "40mm Cube"}})
    assert st["ok"], st
    tb = _title_block(dwg)
    assert tb.get("title") == "40mm Cube" and "part_name" not in tb, tb
    bad = _agent_call("set_titleblock", {"doc_id": dwg, "fields": {"colour": "blue"}})
    assert bad["ok"] is False and "colour" in bad["error"], bad


# ------------------------------------------------ manufacturing drawing release contract

def _complete_titleblock(dwg: str) -> None:
    result = _call("set_titleblock", {
        "doc_id": dwg,
        "fields": {
            "title": "TEST PLATE", "part_number": "TP-1", "material": "6061-T6 ALUMINUM",
            "rev": "A", "finish": "RA 3.2 MAX",
            "drawn_by": "TEST", "date": "2026-08-26",
        },
    })
    assert result["ok"], result


def _standards_full(dwg: str) -> dict:
    """One readback enriched with the extended controlled-title fields audit consumes."""
    full = cb.call("drawing_get", {"doc_id": dwg})
    full["sheets"][0]["title_block"].update({
        "projection": "THIRD ANGLE",
        "drawing_standard": "ASME Y14.5-2018",
        "general_tolerance": "UNLESS OTHERWISE SPECIFIED: DIMENSIONS ARE IN MM; ISO 2768-m",
    })
    return full


def _review_summary(dwg: str, audit_result=None) -> dict:
    from services.mcp import server
    args = {"doc_id": dwg}
    if audit_result is not None:
        args["audit_result"] = audit_result
    raw = asyncio.run(server.mcp.call_tool("review_drawing", args))
    content = raw[0] if isinstance(raw, tuple) else raw
    text_items = [item for item in content if getattr(item, "type", "") == "text"]
    assert text_items, content
    return json.loads(text_items[-1].text)


def test_hole_callout_rejects_unverified_thread_and_depth():
    dwg, view_id, center = _drawing_with_circle(_plate_with_hole())
    result = _call("add_hole_callout", {
        "doc_id": dwg, "view_id": view_id, "a": center,
        "hole_type": "tapped", "thread": "M6x1.0", "depth": 14,
    })
    assert result["ok"] is False, result
    assert not any(a.get("kind") == "hole_callout"
                   for a in _stored_annotations(dwg, view_id))


def test_dimension_tolerance_is_applied_atomically_at_creation():
    dwg, _mk = _sheet_drawing()
    bbox = _view(dwg, "v2")["bbox"]
    result = _call("add_dimension", {
        "doc_id": dwg, "view_id": "v2",
        "a": [bbox[0], bbox[1]], "b": [bbox[2], bbox[1]],
        "tol_type": "deviation", "tol_upper": 0.02, "tol_lower": 0.05,
        "precision": 2,
    })
    assert result["ok"], result
    stored = _the_annotation(dwg, "v2", result["annotation"])
    assert stored["tol_type"] == "deviation"
    assert stored["tol_upper"] == 0.02 and stored["tol_lower"] == 0.05
    assert "+0.02" in stored["text"] and "-0.05" in stored["text"]


def test_review_never_releases_a_bare_nominal_sheet_without_audit():
    dwg, _mk = _sheet_drawing()
    _complete_titleblock(dwg)
    summary = _review_summary(dwg)
    assert summary["layout_clean"] is True, summary["warnings"]
    assert summary["manufacturing_ready"] is False, summary
    assert "audit_drawing" in summary["manufacturing_ready_reason"], summary


def test_audit_covers_explicit_row_but_refuses_missing_model_requirements(monkeypatch):
    dwg, made = _sheet_drawing()
    _complete_titleblock(dwg)
    bbox = _view(dwg, "v2")["bbox"]
    dimension = _call("add_dimension", {
        "doc_id": dwg, "view_id": "v2",
        "a": [bbox[0], bbox[1]], "b": [bbox[2], bbox[1]],
        "tol_type": "symmetric", "tol_upper": 0.1, "precision": 1,
    })
    assert dimension["ok"], dimension
    full = _standards_full(dwg)
    missing = _audit_with_full(
        monkeypatch, dwg, made["source_doc_id"], full,
        {"requirements": [{
            "requirement_id": "D1", "type": "dimension_tolerance",
            "annotation_id": "missing-ann", "critical": True,
        }]},
    )
    assert missing["verdict"] == "not_release_ready", missing
    assert missing["coverage_matrix"][0]["status"] == "missing", missing
    covered = _audit_with_full(
        monkeypatch, dwg, made["source_doc_id"], full,
        {"requirements": [{
            "requirement_id": "D1", "type": "dimension_tolerance",
            "annotation_id": dimension["annotation"], "critical": True,
        }]},
    )
    assert covered["coverage_matrix"][0]["annotation_id"] == dimension["annotation"]
    assert covered["coverage_matrix"][0]["status"] == "covered", covered
    assert covered["verdict"] == "not_release_ready", covered
    assert any(row["requirement_id"].startswith("model:") and row["status"] == "missing"
               for row in covered["coverage_matrix"]), covered
    review = _review_summary(dwg, covered)
    assert review["manufacturing_ready"] is False, review


def _audit_with_full(monkeypatch, dwg: str, source: str, full: dict, spec: dict) -> dict:
    real_call = cb.call

    def drawing_payload(method, params=None):
        if method == "drawing_get" and params.get("doc_id") == dwg:
            return full
        return real_call(method, params)

    monkeypatch.setattr(cb, "call", drawing_payload)
    return _call("audit_drawing", {
        "doc_id": source, "drawing_id": dwg, "spec": spec,
    })


def test_audit_standards_requires_projection_and_general_tolerance(monkeypatch):
    dwg, made = _sheet_drawing()
    _complete_titleblock(dwg)
    full = _standards_full(dwg)
    full["sheets"][0]["title_block"]["projection"] = ""
    full["sheets"][0]["title_block"].pop("general_tolerance")

    result = _audit_with_full(
        monkeypatch, dwg, made["source_doc_id"], full,
        {"requirements": [{"type": "title_field", "field": "title"}]},
    )

    assert result["verdict"] == "not_release_ready", result
    codes = {finding["code"] for finding in result["standards"]["failures"]}
    assert {"title_field_missing", "units_statement_missing"} <= codes, result
    fields = {finding.get("field") for finding in result["standards"]["failures"]}
    assert {"projection", "general_tolerance"} <= fields, result
    assert all("set_titleblock" in finding["remedy"]
               for finding in result["standards"]["failures"]
               if finding["check"] in {"title_block", "units_statement"}), result


def test_audit_standards_rejects_gdt_reference_without_existing_datum(monkeypatch):
    dwg, made = _sheet_drawing()
    _complete_titleblock(dwg)
    full = _standards_full(dwg)
    full["sheets"][0]["views"][0]["annotations"] = [{
        "id": "gdt-b", "kind": "gdt_frame", "symbol": "position",
        "tol_value": "0.1", "tol_modifier": "none",
        "datums": [{"ref": "B", "modifier": "none"}],
        "a": [10, 10], "pos": [20, 20],
    }]

    result = _audit_with_full(
        monkeypatch, dwg, made["source_doc_id"], full,
        {"requirements": [{"type": "title_field", "field": "title"}]},
    )

    failures = result["standards"]["failures"]
    assert any(finding["code"] == "gdt_datum_unresolved"
               and finding["datum"] == "B" for finding in failures), result
    assert "add_datum" in next(finding["remedy"] for finding in failures
                              if finding["code"] == "gdt_datum_unresolved")
    assert result["verdict"] == "not_release_ready", result


def test_audit_standards_rejects_bare_diameter_for_thread_requirement(monkeypatch):
    dwg, made = _sheet_drawing()
    _complete_titleblock(dwg)
    full = _standards_full(dwg)
    full["sheets"][0]["views"][0]["annotations"] = [{
        "id": "bare-dia", "kind": "hole_callout", "hole_type": "tapped",
        "text": "DIA 5 THRU", "diameter": 5.0, "thru": True,
        "a": [10, 10], "pos": [20, 20],
    }]

    result = _audit_with_full(
        monkeypatch, dwg, made["source_doc_id"], full,
        {"requirements": [{
            "requirement_id": "THREAD-1", "type": "thread", "thread": "M6x1.0",
            "thru": True, "annotation_id": "bare-dia",
        }]},
    )

    failures = result["standards"]["failures"]
    assert any(finding["code"] == "thread_designation_invalid"
               and finding["annotation_id"] == "bare-dia" for finding in failures), result
    remedy = next(finding["remedy"] for finding in failures
                  if finding["code"] == "thread_designation_invalid")
    assert "edit_drawing_annotation" in remedy and "add_hole_callout" in remedy
    assert result["coverage_matrix"][0]["status"] == "missing", result
    assert result["verdict"] == "not_release_ready", result


def test_audit_standards_finds_malformed_composite_dangling_anchor_and_finish_note(monkeypatch):
    dwg, made = _sheet_drawing()
    _complete_titleblock(dwg)
    full = _standards_full(dwg)
    full["sheets"][0]["title_block"]["finish"] = ""
    full["sheets"][0]["views"][0]["annotations"] = [
        {
            "id": "bad-composite", "kind": "gdt_frame", "symbol": "position",
            "tol_value": "0.1", "datums": [], "row2": {"datums": []},
            "a": [10, 10], "pos": [20, 20],
        },
        {
            "id": "dangling", "kind": "note", "text": "INSPECT",
            "view_id": "deleted-view", "a": [10, 10], "pos": [20, 20],
        },
        {
            "id": "finish-symbol", "kind": "surface_finish", "value": "1.6",
            "a": [10, 10], "pos": [20, 20],
        },
    ]

    result = _audit_with_full(
        monkeypatch, dwg, made["source_doc_id"], full,
        {"requirements": [{"type": "title_field", "field": "title"}]},
    )

    codes = {finding["code"] for finding in result["standards"]["failures"]}
    assert {"gdt_composite_malformed", "annotation_dangling",
            "finish_note_missing"} <= codes, result
    assert all(finding["remedy"] for finding in result["standards"]["failures"]), result


def test_missing_new_annotation_rpc_returns_engine_unsupported(monkeypatch):
    dwg, _mk = _sheet_drawing()
    real_call = cb.call

    def missing_rpc(method, params=None):
        if method == "drawing_add_chamfer_dimension":
            raise cb.CoreError("unknown method drawing_add_chamfer_dimension")
        return real_call(method, params)

    monkeypatch.setattr(cb, "call", missing_rpc)
    result = _call("add_chamfer_dimension", {
        "doc_id": dwg, "view_id": "front", "a": [200, 150],
        "pos": [215, 165], "feature_ref": "f1", "length": 1.0, "angle": 45,
    })
    assert result == {
        "ok": False,
        "code": "error",
        "reason": "engine_unsupported",
        "tool": "add_chamfer_dimension",
        "rpc": "drawing_add_chamfer_dimension",
        "error": "unknown method drawing_add_chamfer_dimension",
    }


def test_composite_gdt_preserves_second_row_and_full_modifiers():
    dwg, _mk = _sheet_drawing()
    result = _call("add_gdt", {
        "doc_id": dwg, "view_id": "top", "symbol": "position", "tolerance": "0.2",
        "a": [200, 210], "pos": [240, 230], "modifier": "free",
        "datums": [{"ref": "A", "modifier": "tangent"}],
        "row2": {"tolerance": "0.05", "diameter": True, "modifier": "mmc",
                 "datums": [{"ref": "A", "modifier": "mmc"}]},
    })
    assert result["ok"], result
    stored = _the_annotation(dwg, "v2", result["annotation"])
    assert stored["tol_modifier"] == "free"
    assert stored["datums"][0]["modifier"] == "tangent"
    assert stored["row2"]["tol_value"] == "0.05"


def test_hole_callout_rejects_diameter_and_depth_inconsistent_with_model():
    source = _plate_with_hole()
    dwg, view_id, center = _drawing_with_circle(source)
    features = cb.call("doc_tree", {"doc_id": source})["tree"]["features"]
    hole = next(f for f in features if f.get("type") == "hole")
    base = {"doc_id": dwg, "view_id": view_id, "a": center}
    wrong_diameter = _call("add_hole_callout", {**base, "diameter": 6.0})
    assert wrong_diameter["ok"] is False, wrong_diameter
    wrong_depth = _call("add_hole_callout", {**base, "feature_ref": hole["id"], "depth": 7})
    assert wrong_depth["ok"] is False, wrong_depth
    correct = _call("add_hole_callout", {**base, "feature_ref": hole["id"]})
    assert correct["ok"], correct
    assert "8" in correct["text"] and "THRU" in correct["text"], correct


def test_sheet_table_and_multi_sheet_export_tools_round_trip(tmp_path):
    made = _a_drawing(_plain_plate())
    dwg = made["drawing_doc_id"]
    added = _call("add_drawing_sheet", {
        "doc_id": dwg, "size": "A4", "name": "DETAILS", "title": "DETAILS",
    })
    assert added["ok"] and added["sheet_id"], added
    listed = _call("list_drawing_sheets", {"doc_id": dwg})
    assert listed["ok"] and len(listed["sheets"]) >= 2, listed
    active = _call("set_active_drawing_sheet", {
        "doc_id": dwg, "sheet_id": added["sheet_id"],
    })
    assert active["ok"] and active["sheet_id"] == added["sheet_id"], active
    table = _call("add_drawing_table", {
        "doc_id": dwg, "kind": "notes", "position": [20, 30],
        "title": "GENERAL NOTES", "rows": [{"note": "BREAK SHARP EDGES"}],
    })
    assert table["ok"] and table["table_id"], table
    exported = _call("export_drawing", {
        "doc_id": dwg, "fmt": "pdf", "sheet": "all",
        "path": str(tmp_path / "multi-sheet.pdf"),
    })
    assert exported["ok"] and os.path.exists(exported["path"]), exported


def test_chamfer_callout_uses_the_model_feature():
    source = _plain_plate()
    added = _call("add_features", {"doc_id": source, "ops": [
        cb.feat("chamfer", "Edge break", {"distance": cb.dim(1.5)},
                {"edges": {"of": "edge"}})]})
    assert added["ok"], added
    tree = cb.call("doc_tree", {"doc_id": source})
    feature = next(f["id"] for f in tree["tree"]["features"] if f["name"] == "Edge break")
    made = _call("make_drawing", {"doc_id": source, "views": ["front", "top"],
                                  "model_items": False, "hole_callouts": False})
    assert made["ok"], made
    dwg = made["drawing_doc_id"]
    owners = set(map(tuple, tree["owner_map"][feature]["edges"]))
    for view in _views(dwg):
        for line, provenance in zip(view.get("visible", []), view.get("visible_src", [])):
            if (isinstance(provenance, dict)
                    and (provenance.get("body"), provenance.get("edge_id")) in owners
                    and len(line) == 2):
                a = [(line[0][i] + line[1][i]) / 2 for i in (0, 1)]
                args = {"doc_id": dwg, "view_id": view["view_id"], "a": provenance["sid"],
                        "pos": [a[0] + 10, a[1] + 10], "feature_ref": feature}
                bad = _call("add_chamfer_dimension", {**args, "length": 3.0})
                assert not bad["ok"], bad
                result = _call("add_chamfer_dimension", args)
                assert result["ok"] and result["length"] == pytest.approx(1.5), result
                assert result["angle"] == pytest.approx(45), result
                edge_only = _call("add_chamfer_dimension", {
                    key: value for key, value in args.items() if key != "feature_ref"})
                assert edge_only["ok"] and edge_only["length"] == pytest.approx(1.5), edge_only
                return
    pytest.fail("no projected edge owned by the chamfer feature")


def test_native_chamfer_ordinate_balloon_datum_and_extended_weld_tools():
    dwg, _mk = _sheet_drawing()
    bbox = _view(dwg, "v2")["bbox"]
    positions = {"x": [[30, 50], [40, 50]], "y": [[50, 30], [50, 40]]}
    ordinate = _call("add_ordinate_dimensions", {
        "doc_id": dwg, "view_id": "top", "origin": [bbox[0], bbox[1]],
        "points": [[bbox[2], bbox[1]], [bbox[0], bbox[3]]], "axis": "both",
        "positions": positions,
    })
    assert ordinate["ok"] and ordinate["count"] >= 2, ordinate
    stored = [_the_annotation(dwg, "v2", ann_id) for ann_id in ordinate["annotations"]]
    assert [ann["pos"] for ann in stored] == positions["x"] + positions["y"], stored
    balloon = _call("add_balloon", {
        "doc_id": dwg, "view_id": "top", "a": [bbox[0], bbox[1]], "item": 1,
    })
    assert balloon["ok"] and balloon["text"] == "1", balloon
    bom = _call("drawing_bom", {"doc_id": dwg})
    assert bom["ok"] and bom["rows"] == [] and bom["is_assembly"] is False, bom
    target = _call("add_datum_target", {
        "doc_id": dwg, "view_id": "top", "label": "A1", "target_type": "point",
        "a": [bbox[0], bbox[1]], "pos": [bbox[0] - 10, bbox[1] - 10],
    })
    assert target["ok"] and target["label"] == "A1", target
    weld = _call("add_weld_symbol", {
        "doc_id": dwg, "view_id": "top", "weld_type": "fillet", "size": "6",
        "a": [bbox[0], bbox[1]], "pos": [bbox[0] - 20, bbox[1] - 20],
        "intermittent_length": 40, "pitch": 100, "contour": "flush",
        "finish": "grinding", "field_weld": True, "tail_text": "E7018",
        "full_penetration": True,
    })
    assert weld["ok"], weld


def test_extended_view_and_section_controls_reach_the_engine():
    dwg, _mk = _sheet_drawing()
    changed = _call("set_view", {
        "doc_id": dwg, "view_id": "top", "orientation": "bottom", "show_hidden": True,
    })
    assert changed["ok"], changed
    assert changed["orientation"] == "bottom" and changed["show_hidden"] is True, changed
    bbox = _view(dwg, "v1")["bbox"]
    cut_y = (bbox[1] + bbox[3]) / 2
    cut = [
        [bbox[0], cut_y],
        [(bbox[0] + bbox[2]) / 2, cut_y],
        [bbox[2], cut_y],
    ]
    section = _call("add_section_view", {
        "doc_id": dwg, "parent": "front", "axis": "horizontal",
        "multipoint": cut, "offset": 2, "depth": 5,
        "reverse": True, "show_hidden": True, "label": "B",
    })
    assert section["ok"], section
    assert len(section["cut_line"]) == 3
    assert section["depth"] == 5 and section["reverse"] is True
    plane = _call("add_section_view", {
        "doc_id": dwg, "parent": "front",
        "plane": {"normal": [0, 0, 1], "point": [0, 0, 0]},
        "offset": 3, "mode": "full", "label": "C",
    })
    assert plane["ok"], plane
    assert plane["plane"]["point"] == [0.0, 0.0, 3.0]


def test_dogfood_audit_identity_and_callout(monkeypatch):
    source = _call("new_part", {"name": "custom_bore_audit"})["doc_id"]
    added = _call("add_features", {"doc_id": source, "ops": [
        cb.feat("box", "Plate", {"length": cb.dim(60), "width": cb.dim(40),
                                   "height": cb.dim(10)}),
        cb.feat("hole", "Custom Bore", {"style": "simple", "standard": "custom",
                                          "diameter": cb.dim(5), "end": "through_all"},
                {"face": {"of": "face", "side": "top"}}),
    ]})
    assert added["ok"], added
    bore = next(f for f in cb.call("doc_tree", {"doc_id": source})["tree"]["features"]
                if f["name"] == "Custom Bore")
    made = _call("make_drawing", {"doc_id": source, "views": ["front", "top"],
                                   "model_items": False, "hole_callouts": False})
    assert made["ok"], made
    dwg = made["drawing_doc_id"]
    front = next(vid for vid, orientation in made["views"].items() if orientation == "front")
    top = next(vid for vid, orientation in made["views"].items() if orientation == "top")

    for vid, tolerance in ((front, 0.1), (top, 0.2)):
        bb = _view(dwg, vid)["bbox"]
        dim = _call("add_dimension", {
            "doc_id": dwg, "view_id": vid, "a": [bb[0], bb[1]], "b": [bb[2], bb[1]],
            "tol_type": "symmetric", "tol_upper": tolerance,
        })
        assert dim["ok"] and dim["annotation"] == "a1", dim

    circle = next(arc for arc in _view(dwg, top)["arcs"]
                  if arc.get("closed") and
                  abs(arc["radius"] / _view(dwg, top)["scale"] - 2.5) < 1e-4)
    callout = _call("add_hole_callout", {
        "doc_id": dwg, "view_id": top, "a": circle["center"], "feature_ref": bore["id"],
    })
    assert callout["ok"], callout
    stored = _the_annotation(dwg, top, callout["annotation"])
    assert stored["hole_type"] == "simple" and abs(stored["value"] - 5) < 1e-6, stored
    assert not stored.get("thread"), stored

    spec = {"requirements": [
        {"requirement_id": "front", "type": "dimension_tolerance",
         "view_id": front, "annotation_id": "a1", "tol_upper": 0.1},
        {"requirement_id": "top", "type": "dimension_tolerance",
         "view_id": top, "annotation_id": "a1", "tol_upper": 0.2},
        {"requirement_id": "wrong-view", "type": "dimension_tolerance",
         "view_id": front, "annotation_id": "a1", "tol_upper": 0.2},
        {"requirement_id": "legacy", "type": "dimension_tolerance",
         "annotation_id": "a1", "tol_upper": 0.1},
    ]}
    audit = _call("audit_drawing", {"doc_id": source, "drawing_id": dwg, "spec": spec})
    by_req = {row["requirement_id"]: row for row in audit["coverage_matrix"]}
    assert by_req["front"]["status"] == "covered" and by_req["front"]["view_id"] == front
    assert by_req["top"]["status"] == "covered" and by_req["top"]["view_id"] == top
    assert by_req["wrong-view"]["status"] == "missing", audit
    assert by_req["legacy"]["status"] == "ambiguous", audit
    assert set(by_req["legacy"]["candidate_view_ids"]) == {front, top}
    assert audit["covered"] == sum(row["status"] == "covered" for row in audit["coverage_matrix"]), audit
    assert audit["total"] == len(audit["coverage_matrix"]), audit
    assert audit["verdict"] == "not_release_ready", audit
    assert not [f for f in audit["standards_failures"] if f["check"] == "thread_callout"], audit

    # A standards finding on one view must not suppress the same local id on another.
    full = cb.call("drawing_get", {"doc_id": dwg})
    for view in full["sheets"][0]["views"]:
        if view.get("view_id") in {front, top}:
            view["annotations"] = [{"id": "a1", "kind": "hole_callout",
                                     "hole_type": "tapped", "text": "DIA 5"}]
    findings = _audit_with_full(
        monkeypatch, dwg, source, full,
        {"requirements": [{"type": "title_field", "field": "title"}]})
    invalid = [f for f in findings["standards_failures"]
               if f["code"] == "thread_designation_invalid" and f["source"] == "annotation"]
    assert {f["view_id"] for f in invalid} == {front, top}, findings


def test_dogfood_patch_and_scale_truth():
    source = _cube(40)
    made = _call("make_drawing", {"doc_id": source, "views": ["front", "top"],
                                   "model_items": False, "hole_callouts": False, "scale": 2})
    assert made["ok"], made
    dwg = made["drawing_doc_id"]
    front = next(vid for vid, orientation in made["views"].items() if orientation == "front")
    bb = _view(dwg, front)["bbox"]
    dim = _call("add_dimension", {
        "doc_id": dwg, "view_id": front, "a": [bb[0], bb[1]], "b": [bb[2], bb[1]],
        "tol_type": "deviation", "tol_upper": 0.02, "tol_lower": 0.05,
    })
    assert dim["ok"], dim
    ann_id = dim["annotation"]
    before = cb.call("drawing_get", {"doc_id": dwg})
    annotation_before = _the_annotation(dwg, front, ann_id)
    refused = _call("edit_drawing_annotation", {
        "doc_id": dwg, "view_id": front, "annotation_id": ann_id,
        "fields": {"tol_upper": None, "suffix": " MUST NOT APPLY"},
    })
    assert not refused["ok"] and "tol_upper" in refused["error"], refused
    assert _the_annotation(dwg, front, ann_id) == annotation_before
    assert cb.call("drawing_get", {"doc_id": dwg})["revision"] == before["revision"]

    with pytest.raises(cb.CoreError):
        cb.call("drawing_set_annotation", {
            "doc_id": dwg, "view_id": front, "ann_id": ann_id,
            "patch": {"tol_lower": None, "suffix": " MUST NOT APPLY"},
        })
    assert _the_annotation(dwg, front, ann_id) == annotation_before
    assert cb.call("drawing_get", {"doc_id": dwg})["revision"] == before["revision"]
    changed = _call("edit_drawing_annotation", {
        "doc_id": dwg, "view_id": front, "annotation_id": ann_id,
        "fields": {"tol_type": "none", "suffix": " CHECKED"},
    })
    assert changed["ok"] and changed["fields_set"] == ["suffix", "tol_type"], changed
    assert changed["annotation"]["tol_type"] == "none"
    assert changed["annotation"]["text"].endswith(" CHECKED"), changed

    title = _call("set_titleblock", {"doc_id": dwg, "fields": {"scale": "1:1"}})
    assert any("scale" in warning.lower() for warning in title.get("warnings", [])), title
    audit = _call("audit_drawing", {
        "doc_id": source, "drawing_id": dwg,
        "spec": {"requirements": [{"type": "title_field", "field": "scale"}]},
    })
    findings = [f for f in audit["standards_failures"] if f["code"] == "view_scale_mismatch"]
    assert {f["view_id"] for f in findings} == set(made["views"]), audit
    assert all(f["view_scale"] == 2 and f["printed_scale"] == "1:1" for f in findings)
    assert audit["verdict"] == "not_release_ready", audit

    auto = _call("make_drawing", {"doc_id": source, "views": ["front"], "model_items": False,
                                   "hole_callouts": False, "title": {"scale": "37:1"}})
    assert auto["ok"] and any("scale" in w.lower() for w in auto.get("warnings", [])), auto


def test_scale_audit_respects_separately_labelled_views():
    from services.mcp.cad_drawing_tools import _drawing_scale_findings

    full = {"sheets": [{"id": "s1", "title_block": {"scale": "1:1"}, "views": [
        {"view_id": "v1", "type": "base", "orientation": "front", "scale": 1},
        {"view_id": "v2", "type": "detail", "orientation": "detail", "scale": 4},
        {"view_id": "v3", "type": "iso", "orientation": "iso", "scale": 0.5,
         "caption": "SCALE 1:2"},
        {"view_id": "v4", "type": "section", "orientation": "section", "scale": 2},
    ]}]}
    findings = _drawing_scale_findings(full)
    assert [row["view_id"] for row in findings] == ["v4"], findings
    full["sheets"][0]["views"][2]["caption"] = "SCALE 1:5"
    findings = _drawing_scale_findings(full)
    assert {row["view_id"] for row in findings} == {"v3", "v4"}, findings


def test_drawing_export_resolves_project_relative_and_absolute_paths(tmp_path, monkeypatch):
    from services.agent.request_context import bind_roots

    dwg, _ = _sheet_drawing()
    project, worker = tmp_path / "project", tmp_path / "worker"
    project.mkdir()
    worker.mkdir()
    monkeypatch.chdir(worker)
    # Request-scoped root wins even if the worker environment names a different root.
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(worker))
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(worker))
    with bind_roots(str(project)):
        result = _call("export_drawing", {
            "doc_id": dwg, "path": "parts/enclosure-drawing.pdf", "companions": ["dxf"],
        })
    expected = [project / "parts/enclosure-drawing.pdf", project / "parts/enclosure-drawing.dxf"]
    assert result["ok"] and result["paths"] == [str(p) for p in expected], result
    assert all(p.is_file() and p.stat().st_size > 0 for p in expected)
    assert not (worker / "parts").exists()

    monkeypatch.delenv("CHUDWARE_PROJECT_DIR")
    monkeypatch.delenv("CHUDWARE_FS_ROOT")
    absolute = tmp_path / "external" / "sheet.pdf"
    result = _call("export_drawing", {
        "doc_id": dwg, "path": str(absolute), "companions": ["dxf"],
    })
    assert result["ok"], result
    assert result["paths"] == [str(absolute), str(absolute.with_suffix(".dxf"))], result
    assert absolute.is_file() and absolute.with_suffix(".dxf").is_file()

    refused = _call("export_drawing", {
        "doc_id": dwg, "path": "missing-root.pdf", "companions": ["dxf"],
    })
    assert not refused["ok"] and "ABSOLUTE" in refused["error"], refused
    assert not (worker / "missing-root.pdf").exists()
    assert not (worker / "missing-root.dxf").exists()


def test_drawing_export_refuses_sensitive_main_and_companion_before_writing(tmp_path, monkeypatch):
    project = tmp_path / "project"
    parts = project / "parts"
    secrets = tmp_path / ".ssh"
    parts.mkdir(parents=True)
    secrets.mkdir()
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(project))
    dwg, _ = _sheet_drawing()

    direct = _call("export_drawing", {
        "doc_id": dwg, "path": str(secrets / "drawing.pdf"), "companions": ["dxf"],
    })
    assert not direct["ok"] and "credential/key location" in direct["error"], direct
    (parts / "drawing.dxf").symlink_to(secrets / "drawing.dxf")
    companion = _call("export_drawing", {
        "doc_id": dwg, "path": "parts/drawing.pdf", "companions": ["dxf"],
    })
    assert not companion["ok"] and "credential/key location" in companion["error"], companion
    assert not (parts / "drawing.pdf").exists()
    assert not list(secrets.iterdir())

    # The engine derives *_s1.svg and *_s2.svg for a multi-sheet SVG companion.
    sheet = _call("add_drawing_sheet", {"doc_id": dwg, "size": "A4", "name": "EXTRA"})
    assert sheet["ok"], sheet
    (parts / "multi_s2.svg").symlink_to(secrets / "multi_s2.svg")
    multi = _call("export_drawing", {
        "doc_id": dwg, "path": "parts/multi.pdf", "sheet": "all", "companions": ["svg"],
    })
    assert not multi["ok"] and "credential/key location" in multi["error"], multi
    assert not (parts / "multi.pdf").exists()
    assert not list(secrets.iterdir())
    (parts / "multi_s2.svg").unlink()
    exported = _call("export_drawing", {
        "doc_id": dwg, "path": "parts/multi.pdf", "companions": ["svg"],
    })
    expected = [parts / "multi.pdf", parts / "multi_s1.svg", parts / "multi_s2.svg"]
    assert exported["ok"] and exported["paths"] == [str(p) for p in expected], exported
    assert all(p.is_file() and p.stat().st_size > 0 for p in expected)
    svg = _call("export_drawing", {"doc_id": dwg, "path": "parts/views.svg"})
    assert svg["ok"] and svg["page_count"] == 2, svg
    assert svg["exported_sheets"] == ["s1", "s2"] and len(svg["sheet_mm_all"]) == 2, svg
    assert svg["paths"] == [str(parts / "views_s1.svg"), str(parts / "views_s2.svg")], svg


def test_drawing_export_simultaneous_request_roots_are_isolated(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from services.agent.request_context import bind_roots
    from services.mcp import cad_drawing_tools as drawing

    class Registry:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def register(fn):
                self.tools[fn.__name__] = fn
                return fn
            return register

    roots = [tmp_path / "first", tmp_path / "second"]
    for root in roots:
        root.mkdir()
    worker = tmp_path / "worker"
    worker.mkdir()
    monkeypatch.chdir(worker)
    registry = Registry()
    drawing.register_cad_drawing_tools(registry)
    barrier = threading.Barrier(2)

    def export(root):
        with bind_roots(str(root)):
            barrier.wait(timeout=10)
            return json.loads(registry.tools["export_drawing"](
                doc_id="dwg", path="parts/sheet.pdf", companions=["dxf"]))

    with monkeypatch.context() as patch:
        patch.setattr(drawing, "_doc", lambda doc_id: doc_id)
        patch.setattr(drawing.cb, "call", lambda method, params: {"path": params["path"]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(export, roots))
    for root, result in zip(roots, results):
        assert result["ok"], result
        assert result["paths"] == [str(root / "parts/sheet.pdf"),
                                    str(root / "parts/sheet.dxf")], result


def test_audit_derives_missing_holes_from_the_model():
    source = _plate_with_hole()
    dwg, view_id, center = _drawing_with_circle(source)
    spec = {"requirements": [{"type": "title_field", "field": "title"}]}
    before = _call("audit_drawing", {"doc_id": source, "drawing_id": dwg, "spec": spec})
    bore_rows = [row for row in before["coverage_matrix"]
                 if row["requirement_id"].startswith("model:bore:")]
    assert bore_rows and all(row["status"] == "missing" for row in bore_rows), before
    callout = _call("add_hole_callout", {"doc_id": dwg, "view_id": view_id, "a": center})
    assert callout["ok"], callout
    after = _call("audit_drawing", {"doc_id": source, "drawing_id": dwg, "spec": spec})
    bore_rows = [row for row in after["coverage_matrix"]
                 if row["requirement_id"].startswith("model:bore:")]
    assert bore_rows and all(row["status"] == "covered" for row in bore_rows), after


def test_audit_checks_title_block_on_every_sheet():
    source = _plain_plate()
    made = _a_drawing(source)
    dwg = made["drawing_doc_id"]
    added = _call("add_drawing_sheet", {"doc_id": dwg, "name": "Sheet 2"})
    assert added["ok"], added
    sheet_id = added["sheet_id"]
    title = _call("set_titleblock", {"doc_id": dwg, "sheet_id": sheet_id,
                                      "fields": {"title": "SECOND SHEET"}})
    assert title["ok"] and title["title_block"]["title"] == "SECOND SHEET", title
    audit = _call("audit_drawing", {"doc_id": source, "drawing_id": dwg,
                                    "spec": [{"type": "title_field", "field": "title"}]})
    assert any(f.get("sheet_id") == sheet_id and f.get("code") == "title_field_missing"
               for f in audit["standards"]["failures"]), audit


def test_angular_dimension_accepts_two_model_line_targets():
    source = _plain_plate()
    made = _a_drawing(source)
    dwg = made["drawing_doc_id"]
    view = cb.call("drawing_get", {"doc_id": dwg})["sheets"][0]["views"][0]
    lines = [(line, src["sid"]) for line, src in
             zip(view["visible"], view["visible_src"])
             if len(line) == 2 and src.get("sid")]
    for line1, sid1 in lines:
        dx1, dy1 = line1[1][0] - line1[0][0], line1[1][1] - line1[0][1]
        for line2, sid2 in lines:
            dx2, dy2 = line2[1][0] - line2[0][0], line2[1][1] - line2[0][1]
            if sid1 != sid2 and abs(dx1 * dx2 + dy1 * dy2) < 1e-7:
                result = _call("add_dimension", {"doc_id": dwg, "view_id": view["view_id"],
                               "dtype": "angular_dim", "a": sid1, "b": sid2})
                assert result["ok"] and result["value"] == pytest.approx(90), result
                assert result["stale"] is False, result
                return
    pytest.fail("fixture has no visible perpendicular model edges")


def test_linear_dimension_between_parallel_model_faces():
    source = _plain_plate()
    made = _a_drawing(source)
    dwg = made["drawing_doc_id"]
    faces = cb.call("doc_query", {"doc_id": source, "query": {"of": "face"}})["matches"]
    left = next(f for f in faces if f.get("side") == "left")
    right = next(f for f in faces if f.get("side") == "right")
    result = _call("add_dimension", {"doc_id": dwg, "view_id": "front",
                   "dtype": "linear_dim", "a": left["sid"], "b": right["sid"]})
    assert result["ok"] and result["value"] == pytest.approx(90), result
    assert result["stale"] is False, result


def test_datum_and_surface_finish_reject_invalid_values_without_adding_symbols():
    dwg, _ = _sheet_drawing()
    before = len(_stored_annotations(dwg, "v2"))
    datum = _call("add_datum", {"doc_id": dwg, "view_id": "top",
                                "label": "A1", "a": [100, 100], "pos": [110, 110]})
    finish = _call("add_surface_finish", {"doc_id": dwg, "view_id": "top",
                   "ra": "1.6", "a": [100, 100], "pos": [110, 110],
                   "machining": "sometimes"})
    assert datum["ok"] is False, datum
    assert finish["ok"] is False, finish
    assert len(_stored_annotations(dwg, "v2")) == before


def test_review_removes_rendered_temporary_png(tmp_path, monkeypatch):
    from services.mcp import cad_drawing_tools
    dwg, _ = _sheet_drawing()
    path = tmp_path / "review.png"
    monkeypatch.setattr(cad_drawing_tools, "_tmp", lambda suffix: str(path))
    summary = _review_summary(dwg)
    assert isinstance(summary["layout_clean"], bool), summary
    assert not path.exists()


def test_make_drawing_counts_and_reports_a_missing_requested_section():
    source = _plain_plate()
    made = _call("make_drawing", {"doc_id": source, "views": ["front"],
                                  "model_items": True, "hole_callouts": False,
                                  "sections": [{"parent": "missing-view", "axis": "vertical"}]})
    assert made["ok"], made
    assert made["auto_dimensions_added"] > 0, made
    assert made["sections_added"] == 0, made
    assert made.get("warnings"), made


def test_audit_flags_numeric_override_that_disagrees_with_model():
    source = _plate_with_hole()
    dwg, view_id, center = _drawing_with_circle(source)
    dim = _call("add_dimension", {"doc_id": dwg, "view_id": view_id, "a": center,
                                   "dtype": "diameter_dim", "text": "999"})
    assert dim["ok"] and dim["overridden"], dim
    audit = _call("audit_drawing", {"doc_id": source, "drawing_id": dwg,
                                    "spec": [{"type": "title_field", "field": "title"}]})
    assert any(f.get("code") == "dimension_override_mismatch"
               and f.get("annotation_id") == dim["annotation"]
               for f in audit["standards"]["failures"]), audit
