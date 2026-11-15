"""SolidWorks export: plan building and the macro writer (no SolidWorks needed), plus a
round trip through SolidWorks itself when it is installed."""

import math
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


def test_sketch_on_principal_plane_places_its_entities_in_model_space():
    plan = build_plan([_rect("f1", "plane.front", 0, 0, 40, 20), _extrude("f2", "f1", 10)],
                      name="asym")
    assert plan.unsupported == []
    sk, ex = plan.steps
    # plane.front: u = +X, v = +Z (engine) -> SolidWorks (x, z, -y); normal -Y -> +Z.
    assert sk["op"] == "sketch3"
    assert sk["plane"]["origin"] == pytest.approx([0, 0, 0])
    assert sk["plane"]["normal"] == pytest.approx([0, 0, 1])
    assert sk["entities"][0]["p1"] == pytest.approx([0, 0, 0])
    assert sk["entities"][0]["p2"] == pytest.approx([40, 0, 0])
    assert sk["entities"][1]["p2"] == pytest.approx([40, 20, 0])
    assert ex["sketch"] == "Sketchf1" and ex["depth"] == 10 and ex["mode"] == "boss"
    assert ex["body"] == {"mode": "merge", "target": None}
    assert ex["end"] == "blind" and ex["end2"] == "none" and ex["flip"] is False


def test_offset_datum_plane_folds_into_the_sketch():
    datum = {"id": "f1", "type": "plane", "name": "Plane1",
             "params": {"method": "offset", "offset": {"value": 25, "expr": None}},
             "refs": {"base": "plane.top"}}
    plan = build_plan([datum, _rect("f2", "f1", -5, -5, 5, 5)], name="p")
    assert plan.unsupported == [] and plan.degraded == []
    # plane.top normal +Z (engine) = +Y (SolidWorks): the plane sits 25 mm up.
    plane = plan.steps[0]["plane"]
    assert plane["origin"] == pytest.approx([0, 25, 0])
    assert plane["normal"] == pytest.approx([0, 1, 0]) and plane["name"] == "Plane1"
    assert plan.steps[0]["entities"][0]["p1"] == pytest.approx([-5, 25, 5])


def test_datum_object_refs_name_the_default_planes():
    sk = _rect("f1", {"kind": "datum", "feature": "plane.right"}, 0, 0, 1, 1)
    datum = {"id": "f2", "type": "plane", "name": "P2",
             "params": {"offset": {"value": 4, "expr": None}, "flip": True},
             "refs": {"base": {"kind": "datum", "feature": "plane.front"}}}
    sk2 = _rect("f3", {"kind": "datum", "feature": "f2"}, 0, 0, 1, 1)
    plan = build_plan([sk, datum, sk2], name="p")
    assert plan.unsupported == []
    assert plan.steps[0]["plane"]["normal"] == pytest.approx([1, 0, 0])
    # flipped: 4 mm along -(front normal) = engine +Y = SolidWorks -Z.
    assert plan.steps[1]["plane"]["origin"] == pytest.approx([0, 0, -4])


def test_endpoints_closer_than_a_micron_are_joined():
    sk = {"id": "f1", "type": "sketch", "name": "S",
          "params": {"entities": [
              {"type": "line", "p1": [0, 0], "p2": [10, 0]},
              {"type": "line", "p1": [10.0000004, 0.0000003], "p2": [10, 10]},
              {"type": "line", "p1": [10, 10], "p2": [0, 0]}]},
          "refs": {"plane": "plane.top"}}
    ents = build_plan([sk], name="p").steps[0]["entities"]
    assert ents[0]["p2"] == ents[1]["p1"]


def test_cut_and_two_direction_extrudes():
    cut = _extrude("f3", "f1", 5, mode="cut", end="through_all")
    two = _extrude("f4", "f1", 5, end2="blind", depth2={"value": 7, "expr": None})
    plan = build_plan([_rect("f1", "plane.top", -10, -10, 10, 10), cut, two], name="p")
    assert plan.unsupported == []
    assert plan.steps[1]["mode"] == "cut" and plan.steps[1]["end"] == "through_all"
    assert plan.steps[1]["body"] == {"mode": "cut", "target": None}
    assert plan.steps[2]["end2"] == "blind" and plan.steps[2]["depth2"] == 7


def test_cut_target_and_new_bodies_follow_the_engine_body_policy():
    new = _extrude("f2", "f1", 5, merge=False)
    cut = _extrude("f3", "f1", 5, mode="cut", body="f2")
    plan = build_plan([_rect("f1", "plane.top", 0, 0, 1, 1), new, cut], name="p")
    assert plan.steps[1]["body"] == {"mode": "new", "target": None}
    assert plan.steps[2]["body"] == {"mode": "cut", "target": "f2"}


def test_primitives_become_a_sketch_on_their_centre_plane_and_a_symmetric_feature():
    feats = [
        {"id": "f1", "type": "cylinder", "name": "Shaft",
         "params": {"at": [0, 0, 0], "axis": "+x", "height": 100, "radius": 5, "mode": "new"}},
        {"id": "f2", "type": "box", "name": "Key",
         "params": {"at": [10, 0, 0], "length": 20, "width": 4, "height": 2, "centered": False,
                    "mode": "cut"}},
        {"id": "f3", "type": "sphere", "name": "Ball", "params": {"at": [0, 0, 50], "radius": 3}},
        {"id": "f4", "type": "torus", "name": "Ring",
         "params": {"at": [0, 0, 0], "axis": [0, 1, 0], "major_r": 10, "minor_r": 1}},
    ]
    plan = build_plan(feats, name="p")
    assert plan.unsupported == []
    ops = [(s["op"], s.get("end") or s.get("angle")) for s in plan.steps]
    assert ops == [("sketch3", None), ("extrude", "mid_plane"), ("sketch3", None),
                   ("extrude", "mid_plane"), ("sketch3", None), ("revolve", 360.0),
                   ("sketch3", None), ("revolve", 360.0)]
    cyl, box = plan.steps[0], plan.steps[2]
    assert cyl["plane"]["normal"] == pytest.approx([1, 0, 0])
    assert cyl["entities"][0]["type"] == "circle" and cyl["entities"][0]["radius"] == 5.0
    assert plan.steps[1]["depth"] == 100 and plan.steps[1]["body"]["mode"] == "new"
    # centered:false box: corner at `at` -> centre (20, 2, 1) engine = (20, 1, -2) SolidWorks.
    assert box["plane"]["origin"] == pytest.approx([20, 1, -2])
    assert plan.steps[3]["body"] == {"mode": "cut", "target": None}
    torus = plan.steps[6]["entities"]
    assert torus[-1]["construction"] and torus[0]["radius"] == 1.0


def test_relational_primitive_placement_is_refused():
    plan = build_plan([{"id": "f1", "type": "cylinder",
                        "params": {"at": {"of": "face", "side": "top"}}}], name="p")
    assert [u["id"] for u in plan.unsupported] == ["f1"]


def test_transform_angles_reproduce_the_engine_rotation():
    from services.sw_export.plan import axis_angle, engine_rotation_to_sw, sw_euler
    f = {"id": "f2", "type": "transform", "name": "Place",
         "params": {"body": "f1", "angle": 25, "rotate": [1, 0.3, -0.2], "pivot": [1, 2, 3],
                    "translate": [5, 6, 7], "copy": True}}
    plan = build_plan([{"id": "f1", "type": "box", "params": {}}, f], name="p")
    mv = plan.steps[-1]
    assert mv["op"] == "move_body" and mv["target"] == "f1" and mv["copy"] is True
    rot, tr = mv["moves"]
    assert tr["translate"] == pytest.approx([5, 7, -6])
    assert rot["pivot"] == pytest.approx([1, 3, -2])
    a, b, c = (math.radians(x) for x in rot["rotate"])

    def rx(t):
        return [[1, 0, 0], [0, math.cos(t), -math.sin(t)], [0, math.sin(t), math.cos(t)]]

    def ry(t):
        return [[math.cos(t), 0, math.sin(t)], [0, 1, 0], [-math.sin(t), 0, math.cos(t)]]

    def rz(t):
        return [[math.cos(t), -math.sin(t), 0], [math.sin(t), math.cos(t), 0], [0, 0, 1]]

    def mm(p, q):
        return [[sum(p[i][k] * q[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    n = math.sqrt(1 + 0.09 + 0.04)
    want = engine_rotation_to_sw(axis_angle([1 / n, 0.3 / n, -0.2 / n], math.radians(25)))
    got = mm(rx(c), mm(ry(b), rz(a)))
    for i in range(3):
        assert got[i] == pytest.approx(want[i], abs=1e-12)
    assert sw_euler(want) == pytest.approx((a, b, c))


def test_boolean_maps_to_combine():
    feats = [{"id": "f1", "type": "box", "params": {"merge": False}},
             {"id": "f2", "type": "cylinder", "params": {"merge": False}},
             {"id": "f3", "type": "boolean", "name": "Hole", "params": {"a": "f1", "b": "f2",
                                                                        "op": "subtract"}}]
    step = build_plan(feats, name="p").steps[-1]
    assert step == {"op": "combine", "id": "f3", "name": "Hole", "code": 15902, "a": "f1",
                    "b": "f2"}


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


def test_pattern_directions_and_axes_are_carried_in_solidworks_axes():
    seed = _extrude("f2", "f1", 5, mode="cut")
    lin = {"id": "f3", "type": "linear_pattern", "name": "LPattern1",
           "params": {"count1": 4, "spacing1": {"value": 20, "expr": None},
                      "count2": 2, "spacing2": {"value": -10, "expr": None}},
           "refs": {"dir1": {"kind": "axis", "axis": [1, 0, 0]},
                    "dir2": {"kind": "axis", "axis": [0, 1, 0]}, "features": ["f2"]}}
    circ = {"id": "f4", "type": "circular_pattern", "name": "CirPattern1",
            "params": {"count": 6, "angle": {"value": 360, "expr": None}},
            "refs": {"axis": {"kind": "axis", "axis": [0, 0, -1], "origin": [-0.4, 0, 5]},
                     "features": ["f2"]}}
    mirror = {"id": "f5", "type": "mirror", "name": "Mirror1", "params": {},
              "refs": {"plane": "plane.right", "features": ["f2"]}}
    plan = build_plan([_rect("f1", "plane.top", 0, 0, 1, 1), seed, lin, circ, mirror],
                      name="p")
    assert plan.unsupported == []
    lp, cp, mi = plan.steps[2:]
    assert lp["dirs"][0]["dir"] == pytest.approx([1, 0, 0])
    assert (lp["dirs"][0]["count"], lp["dirs"][0]["spacing"]) == (4, 20.0)
    # a negative spacing reverses the direction: engine -Y = SolidWorks +Z.
    assert lp["dirs"][1]["dir"] == pytest.approx([0, 0, 1]) and lp["dirs"][1]["spacing"] == 10
    assert lp["seeds"] == ["Extrudef2"]
    assert cp["axis"]["origin"] == pytest.approx([-0.4, 5, 0])
    assert cp["axis"]["dir"] == pytest.approx([0, -1, 0])
    assert mi["plane"] == "Right Plane" and mi["seeds"] == ["Extrudef2"]


def test_pattern_of_an_unexported_seed_is_refused():
    lin = {"id": "f3", "type": "linear_pattern", "params": {"count1": 2, "spacing1": 5},
           "refs": {"dir1": {"kind": "axis", "axis": [1, 0, 0]}, "features": ["f9"]}}
    plan = build_plan([lin], name="p")
    assert [u["id"] for u in plan.unsupported] == ["f3"]


def test_revolve_axis_from_a_construction_line_or_a_world_axis():
    sk = _rect("f1", "plane.front", 10, -30, 30, 30)
    sk["params"]["entities"].append({"id": "ax", "type": "line", "construction": True,
                                     "p1": [0, -40], "p2": [0, 40]})
    rev = {"id": "f2", "type": "revolve", "name": "Revolve1", "params": {"mode": "boss"},
           "refs": {"profile": [{"kind": "region", "sketch": "f1"}]}}
    plan = build_plan([sk, rev], name="ring")
    assert plan.unsupported == [] and plan.steps[1]["angle"] == 360.0
    sk2 = _rect("f1", "plane.front", 10, 1, 30, 30)
    rev2 = {"id": "f2", "type": "revolve", "params": {"mode": "cut"},
            "refs": {"profile": ["f1"], "axis": "axis.x"}}
    plan = build_plan([sk2, rev2], name="neck")
    assert plan.unsupported == []
    axis = [e for e in plan.steps[0]["entities"] if e.get("construction")]
    assert len(axis) == 1 and axis[0]["p1"][1:] == pytest.approx([0, 0])   # world X axis


def test_expected_mass_is_carried_in_solidworks_coordinates():
    plan = build_plan([], name="p", expected={"volume_mm3": 8000.0, "com": [20, 5, 10],
                                              "body_count": 2, "mass_g": 16000.0,
                                              "inertia": [2, 0, 0, 0, 4, 0, 0, 0, 6]})
    assert plan.expected == {"volume_mm3": 8000.0, "com_sw_mm": [20.0, 10.0, -5.0],
                             "body_count": 2, "inertia_sw_mm5": [1.0, 3.0, 2.0]}


def test_macro_replays_every_step_and_checks_the_result(tmp_path):
    seed = _extrude("f3", "f1", 5, mode="cut")
    fillet = {"id": "f4", "type": "fillet", "name": "Fillet1", "params": {"radius": 1},
              "refs": {"edges": []}}
    plan = build_plan([_rect("f1", "plane.top", -20, -15, 20, 15), _extrude("f2", "f1", 20),
                       seed, fillet], name="bracket",
                      edge_points=lambda fid: [[20, 15, 10]],
                      expected={"volume_mm3": 24000.0, "com": [0, 0, 10]}).as_dict()
    path = write_macro(plan, str(tmp_path / "bracket.swb"), fallback_step="bracket.step")
    text = open(path, encoding="cp1252").read()
    assert "Sub main()" in text and text.count("SM.CreateLine(p1(0)") == 4
    assert 'Extr "Sketchf1", "f2", "Extrudef2"' in text and '"cut"' in text
    assert "FM.FeatureFillet3(195, 0.001," in text
    assert 'SelNear("EDGE", 0.02, 0.01, -0.015, True, 0)' in text
    assert "24000" in text and "bracket.SLDPRT" in text and 'Fallback = "bracket.step"' in text
    # A VBA `End` makes RunMacro2 run the macro a second time.
    assert "Sub Step4()" in text and not any(ln.strip() == "End" for ln in text.splitlines())
    assert "\r\n" in open(path, "rb").read().decode("cp1252")


def test_body_macro_opens_the_step_beside_it(tmp_path):
    path = write_body_macro("plate", str(tmp_path / "plate.step"), str(tmp_path / "plate.swb"),
                            expected={"volume_mm3": 10.0}, reason="loft f3: no export")
    text = open(path, encoding="cp1252").read()
    assert 'Fallback = "plate.step"' in text and 'BodyFallback "loft f3: no export"' in text
    assert "ToSolidWorksAxes" in text and "plate.SLDPRT" in text and "If Abs(v - 10#)" in text


def test_component_placement_maps_to_a_solidworks_transform():
    from services.sw_export.assembly import sw_transform
    # 90 degrees about engine +Z, then (10, 20, 30) mm.
    x = [0, -1, 0, 10, 1, 0, 0, 20, 0, 0, 1, 30, 0, 0, 0, 1]
    t = sw_transform(x)
    # SolidWorks maps p -> p . M + T; engine +X -> engine +Y = SolidWorks -Z.
    p = [1.0, 0.0, 0.0]
    q = [sum(p[k] * t[3 * k + j] for k in range(3)) for j in range(3)]
    assert q == pytest.approx([0, 0, -1])
    assert t[9:13] == pytest.approx([0.01, 0.03, -0.02, 1.0])


def test_assembly_mass_combines_placed_parts():
    from services.sw_export.assembly import combine_mass
    ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    cube = {"volume_mm3": 8.0, "mass_g": 8.0, "com": [0, 0, 0],
            "inertia": [16 / 3, 0, 0, 0, 16 / 3, 0, 0, 0, 16 / 3]}
    shift = [1, 0, 0, 10, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    m = combine_mass([(cube, ident), (cube, shift)])
    assert m["volume_mm3"] == 16 and m["com"] == pytest.approx([5, 0, 0])
    # parallel axis: Iyy = 2 * (16/3 + 8 * 5^2)
    assert m["inertia"][4] == pytest.approx(2 * (16 / 3 + 200))
    assert combine_mass([({}, shift)]) is None


def test_assembly_export_writes_part_and_assembly_macros(tmp_path):
    from contextlib import contextmanager

    from services.sw_export.assembly import AssemblyExport
    src = tmp_path / "src"
    src.mkdir()
    (src / "Bracket.step+").write_text("x")
    trees = {"asm": {"schema": "aicad.asm/1", "name": "Top", "components": [
        {"name": "A", "source": {"path": "Bracket.step+"},
         "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]},
        {"name": "B", "source": {"path": "Bracket.step+"},
         "transform": [1, 0, 0, 50, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]},
        {"name": "C", "source": {"path": "Gone.step+"}, "suppressed": True}]},
        "part": {"schema": "aicad.part/1", "name": "Bracket",
                 "features": [_rect("f1", "plane.top", 0, 0, 10, 10),
                              _extrude("f2", "f1", 2)]}}
    calls = []

    def call(method, params):
        calls.append(method)
        if method == "doc_tree":
            return {"tree": trees[params["doc_id"]]}
        if method == "eval_mass":
            return {"volume_mm3": 200.0, "mass_g": 200.0, "com": [5, 5, 1], "body_count": 1,
                    "inertia": [1, 0, 0, 0, 1, 0, 0, 0, 1]}
        if method == "export_file":
            with open(params["path"], "w") as fh:
                fh.write("step")
            return {"ok": True}
        raise AssertionError(method)

    @contextmanager
    def open_part(path):
        assert path.endswith("Bracket.step+")
        yield "part"

    out = tmp_path / "out"
    out.mkdir()
    job = AssemblyExport(call, open_part, lambda d: (lambda fid: []), str(out))
    top = job.export("asm", str(src / "Top.step+"), "Top")
    assert [s.file for s in job.order] == ["Bracket.SLDPRT", "Top.SLDASM"]
    assert top.mass["volume_mm3"] == 400.0 and top.mass["com"] == pytest.approx([30, 5, 1])
    asm = (out / "Top.swb").read_text(encoding="cp1252")
    assert asm.count('Place "Bracket.SLDPRT"') == 2 and 'EnsureBuilt "Bracket.SLDPRT"' in asm
    assert "GetUserPreferenceStringValue(9)" in asm and "Top.SLDASM" in asm
    part = (out / "Bracket.swb").read_text(encoding="cp1252")
    assert 'Fallback = "Bracket.step"' in part and (out / "Bracket.step").exists()
    assert calls.count("doc_tree") == 2   # the part is read once for both instances


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
