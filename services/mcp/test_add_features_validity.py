"""End-to-end: add_features reports an invalid result body and a fillet radius the geometry
could not take, through the real engine (skipped when libchudware is not staged)."""
from __future__ import annotations

import pytest

from services.mcp import core_bridge as cb
from services.mcp.test_cad_tools import _call

pytestmark = pytest.mark.skipif(not cb.available(), reason="libchudware not staged")


def _new_part(name: str) -> str:
    r, _ = _call("new_part", {"name": name})
    return r["doc_id"]


def test_fillet_radius_auto_reduced_is_reported(tmp_path):
    doc = _new_part("fillet_adjust")
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Strip", {"length": cb.dim(40), "width": cb.dim(0.7), "height": cb.dim(20)}),
    ]})
    assert r["ok"], r
    assert "adjusted" not in r
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("fillet", "Round", {"radius": cb.dim(3)},
                {"edges": [{"kind": "edge", "surf": "line", "axis": [1, 0, 0],
                            "centroid": [20, 0, 20]}]}),
    ]})
    assert r["ok"], r
    assert r["adjusted"] is True
    (row,) = r["adjustments"]
    assert row["requested_radius"] == pytest.approx(3.0)
    assert 0 < row["applied_radius"] < 0.7
    assert row["change_fraction"] > 0.5
    assert "RADIUS CHANGED" in r["warning"]
    assert "body_valid" not in r  # the reduced fillet is still a sound body


def test_invalid_result_body_is_reported(tmp_path):
    ocp = pytest.importorskip("OCP")  # noqa: F841
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt, gp_Vec

    poly = BRepBuilderAPI_MakePolygon()
    for x, y in ((0, 0), (40, 20), (40, 0), (0, 5)):  # figure-eight cap
        poly.Add(gp_Pnt(x, y, 0))
    poly.Close()
    folded = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(),
                                   gp_Vec(0, 0, 10)).Shape()
    brep = tmp_path / "folded.brep"
    assert BRepTools.Write_s(folded, str(brep))

    doc = _new_part("invalid_body")
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("box", "Block", {"length": cb.dim(10), "width": cb.dim(10), "height": cb.dim(10)}),
    ]})
    assert r["ok"], r
    assert "body_valid" not in r
    r, _ = _call("add_features", {"doc_id": doc, "ops": [
        cb.feat("import", "Folded", {"source_path": str(brep), "format": "brep"}),
    ]})
    assert r.get("body_valid") is False, r
    assert "result body invalid" in r["warning"]
