"""Regression tests for the model-dialect guards added to sketch3d_tools.py,
office_tools.py and preflight_tools.py (sweep continuation — see
test_dialect_guards.py for the shared cad_common helpers and the original
asm/cam/drawing sweep).

Three defect shapes, all found live in the already-swept modules and hunted
here by direct read-across:
  1. CRASH        — a model-supplied dict/list arrives stringified and a bare
                     `.get()`/`.items()`/subscript blows up with a raw
                     AttributeError.
  2. SILENT WRONG  — a stringified batch is iterated CHARACTER BY CHARACTER
                     (or defaulted away) instead of failing, producing
                     garbage output while reporting success.
  3. CONTENTLESS   — a rejection that names neither the offending value nor
                     the accepted shape.

sketch3d_tools.py is engine-backed (doc feature `sketch3d`); the live-dispatch
tests below create a real part via the same `_bind_tool` seam a run uses.
office_tools.py / preflight_tools.py need no engine.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from services.mcp import core_bridge as cb  # noqa: E402
from services.mcp.cad_common import coerce_json_or_repr  # noqa: E402
from services.mcp.sketch3d_tools import (  # noqa: E402
    _coerce_point3,
    _curves_to_entities,
    _validate_pts,
)

_engine_available = cb.available()


def _bind():
    from services.agent.agent_turn import _bind_tool  # noqa: PLC0415
    return _bind_tool


# ============================================================== pure helpers

class TestCoercePoint3:
    def test_accepts_a_3_list(self):
        assert _coerce_point3([1, 2, 3], "p") == [1.0, 2.0, 3.0]

    def test_accepts_an_xyz_object_the_exact_2d_sketch_live_failure_shape(self):
        # Proven live on the 2D sketch path: points sent as {"x":..,"y":..} objects
        # instead of arrays. Here all three keys are present so the reshape is
        # unambiguous.
        assert _coerce_point3({"x": 1, "y": 2, "z": 3}, "p") == [1.0, 2.0, 3.0]

    def test_rejects_a_2_key_xy_object_as_ambiguous_for_3d(self):
        with pytest.raises(ValueError) as ei:
            _coerce_point3({"x": 1, "y": 2}, "p1")
        msg = str(ei.value)
        assert "p1" in msg and "z" in msg  # names the arg and the missing key

    def test_rejects_garbage_naming_arg_and_value(self):
        with pytest.raises(ValueError) as ei:
            _coerce_point3("nope", "curves[0].p1")
        msg = str(ei.value)
        assert "curves[0].p1" in msg and "nope" in msg


class TestValidatePts:
    def test_coerces_stringified_points_list(self):
        out = _validate_pts('[[0,0,0],[1,1,1]]', 2, name="points")
        assert out == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]

    def test_teaches_the_calling_arg_name_not_a_hardcoded_pts(self):
        # LIVE CLASS-3 shape: sketch3d_path_from_points passed a bare string and the
        # error used to say "pts must be..." regardless of the caller's own arg name.
        with pytest.raises(ValueError) as ei:
            _validate_pts("not a list", 2, name="points")
        msg = str(ei.value)
        assert msg.startswith("points must be")
        assert "str" in msg

    def test_coerces_xyz_object_points_inside_the_list(self):
        out = _validate_pts([{"x": 0, "y": 0, "z": 0}, [1, 1, 1]], 2)
        assert out == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]


class TestCurvesToEntities:
    def test_stringified_pts_inside_a_curve_spec_is_still_rejected_precisely(self):
        with pytest.raises(ValueError) as ei:
            _curves_to_entities([{"kind": "line", "pts": "garbage"}])
        assert "pts" in str(ei.value)

    def test_raw_entity_naming_its_shape_kind_instead_of_type_is_reshaped(self):
        """LIVE-CLASS read-across: the 2D sketch path saw entities naming their shape
        'kind' instead of 'type'. Unambiguous here because raw point fields
        (p1/p2), not 'pts', are present — sketch3d used to mis-route this into the
        legacy branch and fail on a phantom missing 'pts'."""
        out = _curves_to_entities([{"kind": "line", "p1": [0, 0, 0], "p2": [1, 0, 0]}])
        assert out == [{"type": "line", "construction": False,
                        "id": "e1", "p1": [0.0, 0.0, 0.0], "p2": [1.0, 0.0, 0.0]}]

    def test_kind_type_fallback_does_not_hijack_the_legitimate_legacy_form(self):
        """A real legacy curve spec {"kind":"line","pts":[...]} must NOT be treated
        as a mislabeled raw entity — the fallback only fires when raw point fields
        (p1/p2/pm/p/points) are present and 'pts' is absent."""
        out = _curves_to_entities([{"kind": "line", "pts": [[0, 0, 0], [1, 0, 0]]}])
        assert out == [{"id": "e1", "type": "line", "construction": False,
                        "p1": [0.0, 0.0, 0.0], "p2": [1.0, 0.0, 0.0]}]

    def test_raw_entity_point_as_xyz_object_is_coerced_not_silently_wrong(self):
        """LIVE-CLASS read-across: points as {"x":..,"y":..} objects instead of
        [x,y] arrays. Before this fix a raw entity's p1/p2/pm/p/points were never
        validated at all — the malformed dict sailed through to doc_apply_ops and
        the engine rejected it deep inside a CoreError round-trip."""
        out = _curves_to_entities([{"type": "line",
                                    "p1": {"x": 1, "y": 2, "z": 3},
                                    "p2": {"x": 4, "y": 5, "z": 6}}])
        assert out == [{"type": "line", "construction": False, "id": "e1",
                        "p1": [1.0, 2.0, 3.0], "p2": [4.0, 5.0, 6.0]}]

    def test_raw_entity_bad_point_still_teaches_not_silently_wrong(self):
        with pytest.raises(ValueError) as ei:
            _curves_to_entities([{"type": "line", "p1": "nope", "p2": [1, 0, 0]}])
        msg = str(ei.value)
        assert "curves[0].p1" in msg and "nope" in msg

    def test_raw_spline_points_list_is_coerced_including_xyz_objects(self):
        out = _curves_to_entities([{"type": "spline",
                                    "points": [[0, 0, 0], {"x": 1, "y": 1, "z": 1}]}])
        assert out[0]["points"] == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]


# ==================================================== live tool-level tests
# Drive the SAME dispatch path a real run uses (agent_turn._bind_tool), proving
# the fix end to end, not just at the helper unit level.

@pytest.mark.skipif(not _engine_available, reason="engine bridge unavailable")
class TestSketch3dLiveDispatch:
    @pytest.fixture(scope="class")
    def doc_id(self):
        new_part = _bind()("new_part")
        r = json.loads(new_part(name="dialect_guard_test_part"))
        assert r["ok"], r
        return r["doc_id"]

    def _new_sketch(self, doc_id, name):
        sketch3d_new = _bind()("sketch3d_new")
        r = json.loads(sketch3d_new(name=name, doc_id=doc_id))
        assert r["ok"], r
        return r["id"]

    def test_add_curves_accepts_a_stringified_but_well_formed_batch(self, doc_id):
        """CLASS-3 fix: `isinstance(curves, list)` used to reject a JSON-stringified
        batch with a contentless "curves must be a non-empty list", never naming
        the type it actually received."""
        fid = self._new_sketch(doc_id, "s1")
        add_curves = _bind()("sketch3d_add_curves")
        curves_str = '[{"kind":"line","pts":[[0,0,0],[10,0,0]]}]'
        out = json.loads(add_curves(sketch3d_id=fid, curves=curves_str, doc_id=doc_id))
        assert out["ok"] is True, out
        assert out["added"] == 1

    def test_add_curves_rejects_real_garbage_with_a_teaching_error(self, doc_id):
        fid = self._new_sketch(doc_id, "s2")
        add_curves = _bind()("sketch3d_add_curves")
        out = json.loads(add_curves(sketch3d_id=fid, curves="not curves at all",
                                    doc_id=doc_id))
        assert out["ok"] is False
        assert "curves" in out["error"] and "str" in out["error"]

    def test_add_curves_kind_instead_of_type_raw_entity_is_reshaped_not_rejected(self, doc_id):
        fid = self._new_sketch(doc_id, "s3")
        add_curves = _bind()("sketch3d_add_curves")
        out = json.loads(add_curves(sketch3d_id=fid,
                                    curves=[{"kind": "line", "p1": [0, 0, 0],
                                            "p2": [5, 5, 5]}],
                                    doc_id=doc_id))
        assert out["ok"] is True, out
        get = _bind()("sketch3d_get")
        g = json.loads(get(sketch3d_id=fid, doc_id=doc_id))
        assert g["entities"][0]["type"] == "line"

    def test_add_curves_xyz_object_points_solve_at_the_authored_coords_not_the_origin(self, doc_id):
        fid = self._new_sketch(doc_id, "s4")
        add_curves = _bind()("sketch3d_add_curves")
        out = json.loads(add_curves(
            sketch3d_id=fid,
            curves=[{"type": "line", "p1": {"x": 1, "y": 2, "z": 3},
                    "p2": {"x": 4, "y": 5, "z": 6}}],
            doc_id=doc_id))
        assert out["ok"] is True, out
        get = _bind()("sketch3d_get")
        g = json.loads(get(sketch3d_id=fid, doc_id=doc_id))
        ent = g["entities"][0]
        assert ent["p1"] == [1.0, 2.0, 3.0]
        assert ent["p2"] == [4.0, 5.0, 6.0]

    def test_add_constraints_accepts_a_stringified_batch(self, doc_id):
        fid = self._new_sketch(doc_id, "s5")
        add_curves = _bind()("sketch3d_add_curves")
        json.loads(add_curves(sketch3d_id=fid,
                              curves=[{"kind": "line", "pts": [[0, 0, 0], [10, 0, 0]]}],
                              doc_id=doc_id))
        add_constraints = _bind()("sketch3d_add_constraints")
        cons_str = '[{"type":"fix","refs":["e1.p1"]}]'
        out = json.loads(add_constraints(sketch3d_id=fid, constraints=cons_str,
                                         doc_id=doc_id))
        assert out["ok"] is True, out
        assert out["added"] == 1

    def test_path_from_points_names_points_not_a_hardcoded_pts_on_bad_input(self, doc_id):
        path_from_points = _bind()("sketch3d_path_from_points")
        out = json.loads(path_from_points(name="bad_path", points="not points",
                                          doc_id=doc_id))
        assert out["ok"] is False
        assert "points" in out["error"]

    def test_ref_vertex_rejects_stringified_selector_with_teaching_error(self, doc_id):
        fid = self._new_sketch(doc_id, "s6")
        ref_vertex = _bind()("sketch3d_ref_vertex")
        out = json.loads(ref_vertex(sketch3d_id=fid,
                                    selector="{'sid': 'v1', 'kind': 'vertex'}garbage",
                                    doc_id=doc_id))
        assert out["ok"] is False
        assert "selector" in out["error"]


# --------------------------------------------------------------------------- office

def _office():
    from services.mcp import office_tools  # noqa: PLC0415
    return office_tools


class TestOfficeApplyDocxOps:
    def test_stringified_ops_batch_no_longer_crashes(self, tmp_path):
        docx = pytest.importorskip("docx")
        doc = docx.Document()
        doc.add_paragraph("hello")
        changed = _office()._apply_docx_ops(doc, '[{"op":"append_paragraph","text":"world"}]')
        assert changed == 1
        assert [p.text for p in doc.paragraphs] == ["hello", "world"]

    def test_a_stray_string_element_in_an_otherwise_real_list_teaches_not_crashes(self):
        pytest.importorskip("docx")
        import docx
        doc = docx.Document()
        out = _office()._apply_docx_ops(doc, ["not a dict"])
        assert isinstance(out, dict) and out["ok"] is False
        assert "ops[0]" in out["hint"]

    def test_genuine_garbage_ops_teaches_type_and_shape(self):
        pytest.importorskip("docx")
        import docx
        doc = docx.Document()
        out = _office()._apply_docx_ops(doc, "not ops at all")
        assert isinstance(out, dict) and out["ok"] is False
        assert "ops" in out["hint"]


class TestOfficeEditPptx:
    def test_stringified_ops_batch_no_longer_crashes(self, tmp_path):
        pptx = pytest.importorskip("pptx")
        prs = pptx.Presentation()
        layout = prs.slide_layouts[6]
        slide = prs.slides.add_slide(layout)
        box = slide.shapes.add_textbox(0, 0, 100, 100)
        box.text_frame.text = "before"
        path = str(tmp_path / "deck.pptx")
        prs.save(path)

        from mcp.server.fastmcp import FastMCP
        m = FastMCP("t")
        _office().register_office_tools(m)
        edit_pptx = _bind_from_mcp(m, "office_edit_pptx")
        out = json.loads(edit_pptx(path=path,
                                   ops='[{"slide":0,"shape_index":0,"text":"after"}]'))
        assert out["ok"] is True, out


def _bind_from_mcp(m, name):
    tool = m._tool_manager._tools[name]
    return tool.fn


class TestOfficeEditXlsx:
    def test_stringified_cells_dict_no_longer_crashes(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.Workbook()
        wb.active.title = "Sheet1"
        path = str(tmp_path / "book.xlsx")
        wb.save(path)

        from mcp.server.fastmcp import FastMCP
        m = FastMCP("t")
        _office().register_office_tools(m)
        edit_xlsx = _bind_from_mcp(m, "office_edit_xlsx")
        out = json.loads(edit_xlsx(path=path, sheet="Sheet1",
                                   cells='{"B2": 214.8}'))
        assert out["ok"] is True, out
        assert out["cells_changed"] == 1


class TestOfficeEditCsv:
    def test_stringified_rows_no_longer_writes_one_column_per_character(self, tmp_path):
        """CLASS-2 fix: `for row in rows or []` on a stringified rows batch used to
        iterate the JSON text CHARACTER BY CHARACTER, writing a syntactically valid
        but garbage CSV while reporting success — worse than a crash."""
        from mcp.server.fastmcp import FastMCP
        m = FastMCP("t")
        _office().register_office_tools(m)
        edit_csv = _bind_from_mcp(m, "office_edit_csv")
        path = str(tmp_path / "out.csv")
        out = json.loads(edit_csv(path=path, rows='[["a","1"],["b","2"]]'))
        assert out["ok"] is True, out
        assert out["row_count"] == 2
        with open(path) as f:
            lines = f.read().splitlines()
        assert lines == ["a,1", "b,2"]

    def test_a_row_that_is_itself_a_bare_string_teaches_instead_of_exploding_per_char(self, tmp_path):
        from mcp.server.fastmcp import FastMCP
        m = FastMCP("t")
        _office().register_office_tools(m)
        edit_csv = _bind_from_mcp(m, "office_edit_csv")
        path = str(tmp_path / "out2.csv")
        out = json.loads(edit_csv(path=path, rows=["not a row"]))
        assert out["ok"] is False
        assert "rows[0]" in out["hint"]


class TestOfficeEditPdf:
    def test_stringified_ops_batch_no_longer_crashes(self, tmp_path):
        pypdf = pytest.importorskip("pypdf")
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        path = str(tmp_path / "in.pdf")
        with open(path, "wb") as f:
            writer.write(f)

        from mcp.server.fastmcp import FastMCP
        m = FastMCP("t")
        _office().register_office_tools(m)
        edit_pdf = _bind_from_mcp(m, "office_edit_pdf")
        out = json.loads(edit_pdf(path=path, ops='[{"op":"rotate","page":0,"degrees":90}]'))
        assert out["ok"] is True, out


# ------------------------------------------------------------------ preflight

@pytest.mark.skipif(not _engine_available, reason="engine bridge unavailable")
class TestPreflightOpsDialect:
    @pytest.fixture(scope="class")
    def doc_id(self):
        new_part = _bind()("new_part")
        r = json.loads(new_part(name="dialect_guard_preflight_part"))
        assert r["ok"], r
        return r["doc_id"]

    def test_a_bare_single_op_dict_is_wrapped_as_a_one_item_batch_and_actually_validated(self, doc_id):
        """Before this fix, a bare dict (not a list) skipped the local complexity gate
        (unknown shape) and was handed straight to run_preflight, which reported a
        misleading 'fail' with the generic detail "ops must be a list" — the SAME
        message genuine garbage produced, so the model could not tell a tolerable
        shape from a real error."""
        preflight_ops = _bind()("preflight_ops")
        op = {"op": "add_feature", "feature": {"type": "cylinder",
                                                "params": {"radius": 5, "height": 10}}}
        out = json.loads(preflight_ops(ops=op, doc_id=doc_id))
        assert "error" not in out
        assert out["verdict"] in ("pass", "warn")

    def test_genuine_garbage_ops_teaches_type_and_shape_distinctly_from_the_dict_case(self, doc_id):
        preflight_ops = _bind()("preflight_ops")
        out = json.loads(preflight_ops(ops="not ops at all, just prose", doc_id=doc_id))
        assert "error" in out
        assert "ops" in out["error"] and "str" in out["error"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
