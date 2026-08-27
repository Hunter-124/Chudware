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
    """`length` is a distance between two picks; `angle` is the four-point angular dim, which
    the engine rejects for want of c/d — proving the WORD resolved rather than being refused
    as unknown vocabulary."""
    dwg, vid, center = _drawing_with_circle(_plate_with_hole())
    lin = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": "length",
                                  "a": [center[0], center[1]], "b": [center[0] + 20.0, center[1]]})
    assert lin["ok"] and lin["dtype"] == "linear_dim", lin
    ann = _the_annotation(dwg, vid, lin["annotation"])
    assert ann["type"] == "linear_dim", ann
    assert ann["value"] > 0.0, ann

    ang = _call("add_dimension", {"doc_id": dwg, "view_id": vid, "dtype": "angle",
                                  "a": center, "b": center})
    assert ang["ok"] is False, ang
    # The engine's own four-point complaint, i.e. the type resolved to angular_dim.
    assert "angular_dim" in ang["error"] and "four" in ang["error"], ang


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
    assert mk["ok"] and mk["model_items"] is True, mk
    assert _auto_dims(mk["drawing_doc_id"]) > 0, mk


def test_a_negative_model_items_word_is_not_truthy():
    """bool("none") is True. If the word reached the flag unfiltered, the engine would run the
    auto-dimension pass the caller just switched OFF."""
    mk = _call("make_drawing", {"doc_id": _plain_plate(), "views": ["front"],
                                "model_items": "none", "hole_callouts": False})
    assert mk["ok"] and mk["model_items"] is False, mk
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
    assert result["rpc"] == "export_drawing", result


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
# The projected arcs carry no sid (HLR drops topological identity — core/doc/drawing.cpp:149-186),
# so the resolution joins the sid's MODEL radius to this view's projected circle of the same
# radius/scale and hands the engine a point ON that ring. The ring point matters for concentric
# circles: picking their common centre makes the engine choose the smallest circle. Every test
# below asserts the measured VALUE — a ⌀6 callout reading 10.0 is a wrong drawing that looks normal.

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
    """An 80x50x10 plate with TWO identical ⌀6 bores — the ambiguous case."""
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

def test_feature_dimensions_pick_the_requested_concentric_ring():
    doc = _concentric_punch()
    mk = _call("make_drawing", {
        "doc_id": doc, "views": ["top"], "model_items": False,
        "hole_callouts": False})
    assert mk["ok"], mk
    dwg = mk["drawing_doc_id"]
    top = next(iter(mk["views"]))
    shank = _call("add_dimension", {
        "doc_id": dwg, "view_id": top, "dtype": "diameter_dim", "a": "Shank"})
    head = _call("add_dimension", {
        "doc_id": dwg, "view_id": top, "dtype": "diameter_dim", "a": "StrikeHead"})
    assert shank["value"] == pytest.approx(9.8), shank
    assert head["value"] == pytest.approx(20.0), head
    assert shank["text"] != head["text"]


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
    the hole feature's own diameter parameter is the model radius."""
    doc = _plate_with_two_bores()
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter",
                                "a": "SmallBore"})
    assert r["ok"], r
    assert abs(r["value"] - SMALL_D_MM) < 1e-6, r
    assert "SmallBore" in " ".join(r["interpreted"]), r


def test_identical_holes_report_which_one_was_dimensioned():
    """Two ⌀6 bores share a radius, so the sid cannot tell them apart. The VALUE is the same
    either way, but the position is not — so the reply must say how many matched and where the
    others are instead of quietly picking one."""
    doc = _plate_with_twin_bores()
    sid = next(iter(_round_sids(doc).values()))
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "diameter", "a": sid})
    assert r["ok"], r
    assert abs(r["value"] - SMALL_D_MM) < 1e-6, r
    note = " ".join(r["interpreted"])
    assert "2 circles" in note, r
    assert "the others are at" in note, r


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


def test_a_between_two_picks_dimension_still_needs_b():
    doc = _plate_with_hole()
    sid = _round_sids(doc)[HOLE_D_MM / 2.0]
    dwg, top, _front = _plate_drawing(doc)
    r = _call("add_dimension", {"doc_id": dwg, "view_id": top, "dtype": "linear", "a": sid})
    assert r["ok"] is False, r
    assert "needs both a and b" in r["error"], r
    assert "diameter_dim" in r["error"], r
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


def test_rescaling_keeps_each_view_where_the_layout_put_it():
    """Geometry is laid at model*scale + position (drawing_session.cpp:1917-1920), so applying a
    scale without recomputing the position slides the view across the sheet by
    centre_model*(new-old). Built off-origin precisely so that drift is non-zero: same part, two
    scales, same sheet centres."""
    doc = _offset_block()
    centers = {}
    for scale in (1.0, 2.0):
        mk = _call("make_drawing", {"doc_id": doc, "views": ["front", "top"],
                                    "model_items": False, "hole_callouts": False,
                                    "scale": scale})
        assert mk["ok"], (scale, mk)
        assert not mk.get("warnings"), (scale, mk)
        centers[scale] = {v["orientation"]: [(v["bbox"][0] + v["bbox"][2]) / 2.0,
                                             (v["bbox"][1] + v["bbox"][3]) / 2.0]
                          for v in _views(mk["drawing_doc_id"])}
    for orientation, c1 in centers[1.0].items():
        c2 = centers[2.0][orientation]
        assert abs(c1[0] - c2[0]) < 0.5 and abs(c1[1] - c2[1]) < 0.5, (orientation, c1, c2)


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


def test_landscape_orientation_is_refused_and_names_the_real_sheet_argument():
    """The verbatim live call. Every paper size in the engine is landscape (A3 = 420x297) and a
    sheet carries only a SIZE, so accepting this would report a rotation that never happened."""
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, "orientation": "landscape"})
    assert mk["ok"] is False, mk
    assert "sheet=" in mk["error"] and "landscape" in mk["error"], mk
    assert mk["valid_sheet"][0] == "A4", mk
    assert "drawing_doc_id" not in mk, mk


def test_portrait_is_refused_too_rather_than_rotating_nothing():
    mk = _call("make_drawing", {"doc_id": _cube(40.0), "views": ["front"],
                                "model_items": False, "orientation": "portrait"})
    assert mk["ok"] is False and "no orientation to set" in mk["error"], mk


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
    assert mk["model_items"] is True and _auto_dims(mk["drawing_doc_id"]) > 0, mk
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
                                          "model_items": False, "orientation": "landscape"})
    assert orient["ok"] is False and "no orientation to set" in orient["error"], orient


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
            "scale": "1:1", "rev": "A", "finish": "AS SPECIFIED",
        },
    })
    assert result["ok"], result


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


def test_hole_callout_emits_finished_thread_and_depth_text():
    dwg, view_id, center = _drawing_with_circle(_plate_with_hole())
    result = _call("add_hole_callout", {
        "doc_id": dwg, "view_id": view_id, "a": center,
        "hole_type": "tapped", "thread": "M6x1.0", "depth": 14,
    })
    assert result["ok"], result
    assert "M6x1.0" in result["text"] and "14" in result["text"], result
    assert "\u21a7" in result["text"], result
    stored = _the_annotation(dwg, view_id, result["annotation"])
    assert stored["thread"] == "M6x1.0" and stored["depth"] == 14


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
    assert summary["layout_clean"] is True, summary
    assert summary["manufacturing_ready"] is False, summary
    assert "audit_drawing" in summary["manufacturing_ready_reason"], summary


def test_audit_flags_missing_row_and_passes_when_exact_annotation_is_covered():
    dwg, made = _sheet_drawing()
    _complete_titleblock(dwg)
    bbox = _view(dwg, "v2")["bbox"]
    dimension = _call("add_dimension", {
        "doc_id": dwg, "view_id": "v2",
        "a": [bbox[0], bbox[1]], "b": [bbox[2], bbox[1]],
        "tol_type": "symmetric", "tol_upper": 0.1, "precision": 1,
    })
    assert dimension["ok"], dimension
    missing = _call("audit_drawing", {
        "doc_id": made["source_doc_id"], "drawing_id": dwg,
        "spec": {"requirements": [{
            "requirement_id": "D1", "type": "dimension_tolerance",
            "annotation_id": "missing-ann", "critical": True,
        }]},
    })
    assert missing["verdict"] == "not_release_ready", missing
    assert missing["coverage_matrix"][0]["status"] == "missing", missing
    covered = _call("audit_drawing", {
        "doc_id": made["source_doc_id"], "drawing_id": dwg,
        "spec": {"requirements": [{
            "requirement_id": "D1", "type": "dimension_tolerance",
            "annotation_id": dimension["annotation"], "critical": True,
        }]},
    })
    assert covered["verdict"] == "release_ready", covered
    assert covered["coverage_matrix"][0]["annotation_id"] == dimension["annotation"]
    review = _review_summary(dwg, covered)
    assert review["manufacturing_ready"] is True, review


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
        "pos": [215, 165], "length": 1.0, "angle": 45,
    })
    assert result == {
        "ok": False,
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


def test_hole_feature_ref_auto_populates_thread_depth_and_count(monkeypatch):
    source = _plate_with_hole()
    dwg, view_id, center = _drawing_with_circle(source)
    real_call = cb.call

    def with_tapped_semantics(method, params=None):
        if method == "doc_tree" and params.get("doc_id") == source:
            return {"tree": {"features": [{
                "id": "f-taps", "name": "Clamp Threads", "type": "hole",
                "params": {
                    "style": "tap", "thread": "M6x1.0", "depth": 14, "end": "blind",
                    "diameter": 5.0,
                    "positions": [[-10, -10], [10, -10], [10, 10], [-10, 10]],
                },
            }]}}
        return real_call(method, params)

    monkeypatch.setattr(cb, "call", with_tapped_semantics)
    result = _call("add_hole_callout", {
        "doc_id": dwg, "view_id": view_id, "a": center, "feature_ref": "f-taps",
    })
    assert result["ok"], result
    assert result["text"].startswith("4\u00d7 M6x1.0"), result
    assert "\u21a7 14" in result["text"], result


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


def test_native_chamfer_ordinate_balloon_datum_and_extended_weld_tools():
    dwg, _mk = _sheet_drawing()
    bbox = _view(dwg, "v2")["bbox"]
    chamfer = _call("add_chamfer_dimension", {
        "doc_id": dwg, "view_id": "top", "a": [bbox[0], bbox[1]],
        "pos": [bbox[0] - 15, bbox[1] - 15], "length": 1.5, "angle": 45,
    })
    assert chamfer["ok"] and chamfer["kind"] == "chamfer", chamfer
    ordinate = _call("add_ordinate_dimensions", {
        "doc_id": dwg, "view_id": "top", "origin": [bbox[0], bbox[1]],
        "points": [[bbox[2], bbox[1]], [bbox[0], bbox[3]]], "axis": "both",
    })
    assert ordinate["ok"] and ordinate["count"] >= 2, ordinate
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
