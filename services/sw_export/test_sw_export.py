"""SolidWorks export: plan building and the macro writer (no SolidWorks needed), plus a
round trip through SolidWorks itself when it is installed."""

import os

import pytest

from services.sw_export.macro import write_body_macro, write_macro
from services.sw_export.plan import build_plan, to_sw


def _rect(fid, plane, x0, y0, x1, y1, **extra):
    lines = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)),
             ((x0, y1), (x0, y0))]
    return {"id": fid, "type": "sketch", "name": f"Sketch{fid}",
            "params": {"entities": [{"id": f"e{i}", "type": "line", "construction": False,
                                     "p1": list(a), "p2": list(b)}
                                    for i, (a, b) in enumerate(lines)],
                       "constraints": [], **extra},
            "refs": {"plane": plane}}


def _extrude(fid, sketch, depth, **params):
    return {"id": fid, "type": "extrude", "name": f"Extrude{fid}",
            "params": {"mode": "boss", "end": "blind", "depth": {"value": depth, "expr": None},
                       **params},
            "refs": {"profile": [{"kind": "region", "sketch": sketch, "centroid": [0, 0]}]}}


def test_point_mapping_is_the_importers_inverse():
    # sw_translate._T maps SolidWorks (x, y, z) -> engine (x, -z, y).
    sw = [1.5, 2.5, -3.5]
    engine = [sw[0], -sw[2], sw[1]]
    assert to_sw(engine) == sw


def test_sketch_on_principal_plane_keeps_its_2d_coordinates():
    plan = build_plan([_rect("f1", "plane.front", 0, 0, 40, 20), _extrude("f2", "f1", 10)],
                      name="asym")
    assert plan.unsupported == []
    sk, ex = plan.steps
    assert sk["plane"] == {"kind": "principal", "plane": "Front Plane"}
    assert sk["entities"][0]["p1"] == [0, 0] and sk["entities"][0]["p2"] == [40, 0]
    assert ex["sketch"] == "Sketchf1" and ex["depth"] == 10 and ex["mode"] == "boss"
    assert ex["end"] == "blind" and ex["end2"] == "none" and ex["flip"] is False


def test_offset_datum_plane_folds_into_the_sketch():
    datum = {"id": "f1", "type": "plane", "name": "Plane1",
             "params": {"method": "offset", "offset": {"value": 25, "expr": None}},
             "refs": {"base": "plane.top"}}
    plan = build_plan([datum, _rect("f2", "f1", -5, -5, 5, 5)], name="p")
    assert plan.unsupported == [] and plan.degraded == []
    assert plan.steps[0]["plane"] == {"kind": "offset", "base": "Top Plane", "offset": 25.0,
                                      "name": "Plane1"}


def test_datum_object_refs_name_the_default_planes():
    sk = _rect("f1", {"kind": "datum", "feature": "plane.right"}, 0, 0, 1, 1)
    datum = {"id": "f2", "type": "plane", "name": "P2",
             "params": {"offset": {"value": 4, "expr": None}, "flip": True},
             "refs": {"base": {"kind": "datum", "feature": "plane.front"}}}
    sk2 = _rect("f3", {"kind": "datum", "feature": "f2"}, 0, 0, 1, 1)
    plan = build_plan([sk, datum, sk2], name="p")
    assert plan.unsupported == []
    assert plan.steps[0]["plane"] == {"kind": "principal", "plane": "Right Plane"}
    assert plan.steps[1]["plane"]["base"] == "Front Plane"
    assert plan.steps[1]["plane"]["offset"] == -4.0


def test_cut_and_two_direction_extrudes():
    cut = _extrude("f3", "f1", 5, mode="cut", end="through_all")
    two = _extrude("f4", "f1", 5, end2="blind", depth2={"value": 7, "expr": None})
    plan = build_plan([_rect("f1", "plane.top", -10, -10, 10, 10), cut, two], name="p")
    assert plan.unsupported == []
    assert plan.steps[1]["mode"] == "cut" and plan.steps[1]["end"] == "through_all"
    assert plan.steps[2]["end2"] == "blind" and plan.steps[2]["depth2"] == 7


def test_fillet_edges_come_from_resolved_edge_points_in_solidworks_frame():
    fillet = {"id": "f3", "type": "fillet", "name": "Fillet1",
              "params": {"radius": {"value": 3, "expr": None}},
              "refs": {"edges": [{"of": "edge", "parallel": "z"}]}}
    plan = build_plan([_rect("f1", "plane.top", -20, -15, 20, 15), _extrude("f2", "f1", 20),
                       fillet], name="p",
                      edge_points=lambda fid: [[20, 15, 10], [-20, 15, 10]] if fid == "f3" else [])
    step = plan.steps[-1]
    assert step["op"] == "fillet" and step["size"] == 3
    assert step["edge_points"] == [[20.0, 10.0, -15.0], [-20.0, 10.0, -15.0]]


def test_unresolvable_edges_and_unknown_features_are_refused_not_guessed():
    fillet = {"id": "f3", "type": "fillet", "params": {"radius": 2}, "refs": {"edges": []}}
    hole = {"id": "f4", "type": "hole", "params": {}, "refs": {}}
    plan = build_plan([_rect("f1", "plane.top", 0, 0, 1, 1), _extrude("f2", "f1", 1),
                       fillet, hole], name="p")
    assert {u["id"] for u in plan.unsupported} == {"f3", "f4"}
    assert all(u["reason"] for u in plan.unsupported)


def test_sketch_relations_are_reported_as_degraded():
    sk = _rect("f1", "plane.top", 0, 0, 1, 1)
    sk["params"]["constraints"] = [{"id": "c1", "type": "horizontal", "refs": ["e0"]}]
    plan = build_plan([sk], name="p")
    assert plan.unsupported == [] and [d["id"] for d in plan.degraded] == ["f1"]


def test_pattern_directions_map_to_default_plane_normals():
    seed = _extrude("f2", "f1", 5, mode="cut")
    lin = {"id": "f3", "type": "linear_pattern", "name": "LPattern1",
           "params": {"count1": 4, "spacing1": {"value": 20, "expr": None},
                      "count2": 2, "spacing2": {"value": -10, "expr": None}},
           "refs": {"dir1": {"kind": "axis", "axis": [1, 0, 0]},
                    "dir2": {"kind": "axis", "axis": [0, 1, 0]}, "features": ["f2"]}}
    circ = {"id": "f4", "type": "circular_pattern", "name": "CirPattern1",
            "params": {"count": 6, "angle": {"value": 360, "expr": None}},
            "refs": {"axis": {"kind": "axis", "axis": [0, 0, 1], "origin": [0, 0, 0]},
                     "features": ["f2"]}}
    mirror = {"id": "f5", "type": "mirror", "name": "Mirror1", "params": {},
              "refs": {"plane": "plane.right", "features": ["f2"]}}
    plan = build_plan([_rect("f1", "plane.top", 0, 0, 1, 1), seed, lin, circ, mirror],
                      name="p")
    assert plan.unsupported == []
    lp, cp, mi = plan.steps[2:]
    assert lp["dirs"][0] == {"plane": "Right Plane", "flip": False, "count": 4, "spacing": 20.0}
    # engine +Y is SolidWorks -Z (Front Plane normal reversed); a negative spacing flips again.
    assert lp["dirs"][1] == {"plane": "Front Plane", "flip": False, "count": 2, "spacing": 10.0}
    assert lp["seeds"] == ["Extrudef2"]
    assert cp["axis_planes"] == ["Front Plane", "Right Plane"] and cp["count"] == 6
    assert mi["plane"] == "Right Plane" and mi["seeds"] == ["Extrudef2"]


def test_pattern_of_an_unexported_seed_is_refused():
    lin = {"id": "f3", "type": "linear_pattern", "params": {"count1": 2, "spacing1": 5},
           "refs": {"dir1": {"kind": "axis", "axis": [1, 0, 0]}, "features": ["f9"]}}
    plan = build_plan([lin], name="p")
    assert [u["id"] for u in plan.unsupported] == ["f3"]


def test_revolve_needs_one_centerline():
    sk = _rect("f1", "plane.front", 10, -30, 30, 30)
    sk["params"]["entities"].append({"id": "ax", "type": "line", "construction": True,
                                     "p1": [0, -40], "p2": [0, 40]})
    rev = {"id": "f2", "type": "revolve", "name": "Revolve1", "params": {"mode": "boss"},
           "refs": {"profile": [{"kind": "region", "sketch": "f1"}],
                    "axis": {"kind": "axis", "axis": [0, 0, 1], "origin": [0, 0, -40]}}}
    plan = build_plan([sk, rev], name="ring")
    assert plan.unsupported == [] and plan.steps[1]["angle"] == 360.0


def test_expected_mass_is_carried_in_solidworks_coordinates():
    plan = build_plan([], name="p", expected={"volume_mm3": 8000.0, "com": [20, 5, 10]})
    assert plan.expected == {"volume_mm3": 8000.0, "com_sw_mm": [20.0, 10.0, -5.0]}


def test_macro_replays_every_step_and_checks_the_result(tmp_path):
    seed = _extrude("f3", "f1", 5, mode="cut")
    fillet = {"id": "f4", "type": "fillet", "name": "Fillet1", "params": {"radius": 1},
              "refs": {"edges": []}}
    plan = build_plan([_rect("f1", "plane.top", -20, -15, 20, 15), _extrude("f2", "f1", 20),
                       seed, fillet], name="bracket",
                      edge_points=lambda fid: [[20, 15, 10]],
                      expected={"volume_mm3": 24000.0, "com": [0, 0, 10]}).as_dict()
    path = write_macro(plan, str(tmp_path / "bracket.swb"))
    text = open(path, encoding="cp1252").read()
    assert "Sub main()" in text and text.count("SM.CreateLine(") == 4
    assert "FM.FeatureExtrusion3(" in text and "FM.FeatureCut4(" in text
    assert "FM.FeatureFillet3(195, 0.001," in text
    assert 'SelAt("EDGE", 0.02, 0.01, -0.015, True, 0)' in text
    assert "24000" in text and "bracket.SLDPRT" in text
    assert "\r\n" in open(path, "rb").read().decode("cp1252")


def test_body_macro_opens_the_step_beside_it(tmp_path):
    path = write_body_macro("plate", str(tmp_path / "plate.step"), str(tmp_path / "plate.swb"))
    text = open(path, encoding="cp1252").read()
    assert 'LoadFile4(folder & "\\plate.step"' in text and "plate.SLDPRT" in text


# --------------------------------------------------------------------------- live round trip
_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "corpora", "sw_fixtures")


def _live():
    from services.formats.formats_solidworks import solidworks_available
    from services.mcp import core_bridge
    return solidworks_available() and core_bridge.available()


@pytest.mark.skipif(not _live(), reason="needs SOLIDWORKS (COM) + a staged libchudware")
@pytest.mark.parametrize("fname", ["asym_front.SLDPRT", "extrude_front_40x30x20.SLDPRT",
                                   "fillet_box.SLDPRT", "chamfer_box.SLDPRT",
                                   "shell_box.SLDPRT", "linpat_holes.SLDPRT",
                                   "cirpat_holes.SLDPRT", "mirror_holes.SLDPRT",
                                   "revolve_ring.SLDPRT"])
def test_solidworks_round_trip_rebuilds_the_same_part(fname, tmp_path):
    """SolidWorks part -> Chudware feature tree -> SolidWorks features: same volume and
    centre of mass as the part SolidWorks authored."""
    from services.mcp import core_bridge as cb
    from services.mcp.cad_exchange_tools import _solidworks_export
    from services.sw_export.com_build import _app, mass_props
    from services.sw_import.sw_feature_import import _cast
    from services.sw_import.sw_translate import resolve

    src = os.path.abspath(os.path.join(_FIXTURES, fname))
    plan = resolve(src)
    assert not plan["blocking"], plan["blocking"]
    d = cb.call("doc_new", {"name": os.path.splitext(fname)[0]})["doc_id"]
    cb.call("doc_apply_ops", {"doc_id": d, "ops": plan["ops"]})
    out = _solidworks_export(d, str(tmp_path / fname), "com", False)
    assert out["ok"] and out["verified"], out

    app = _app()
    ret = app.OpenDoc6(src, 1, 1, "", 0, 0)
    model = _cast(ret[0] if isinstance(ret, tuple) else ret, "IModelDoc2")
    try:
        vol, com = mass_props(model)
    finally:
        app.CloseDoc(model.GetTitle())
    assert abs(out["volume_mm3"] - vol) <= 1e-5 * vol
    assert max(abs(out["com_sw_mm"][i] - com[i]) for i in range(3)) <= 1e-2
