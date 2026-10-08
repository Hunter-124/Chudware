"""Offline LandXML V&V: hand-authored coordinate/geometry and exact foot definitions."""
from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from . import landxml
from .errors import CivilError

DATA = Path(__file__).with_name("data")
NS = {"l": landxml.NS}


def _xml(name="landxml_metric.xml"):
    return (DATA / name).read_bytes()


def _assert_close(actual, expected):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for k, value in expected.items():
            _assert_close(actual[k], value)
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for x, y in zip(actual, expected):
            _assert_close(x, y)
    elif isinstance(expected, (int, float)):
        assert actual == pytest.approx(expected, abs=1e-9, rel=0)
    else:
        assert actual == expected


def test_metric_north_east_faces_profile_and_connection_inverts():
    result = landxml.read(DATA / "landxml_metric.xml")
    m = result["model_fragment"]
    # Non-ordinal P ids must resolve by id, not int(id)-1. N/E must swap.
    s = m["surfaces"][0]
    assert s["points"][0] == [100, 200, 10]
    assert s["triangles"] == [[0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4]]
    areas = []
    for i, j, k in s["triangles"]:
        a, b, c = [s["points"][n] for n in (i, j, k)]
        areas.append(((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2)
    assert areas == [25] * 4
    assert sum(areas) == 100  # 10 m x 10 m square, no gaps/overlaps.
    assert m["points"][0]["x"] == 100
    assert m["points"][0]["y"] == 200
    a = m["alignments"][0]
    assert [s["type"] for s in a["horizontal"]] == ["line", "spiral", "arc", "spiral", "line"]
    assert a["horizontal"][1]["radius_start"] is None
    assert a["horizontal"][1]["radius_end"] == 100
    assert a["horizontal"][1]["start_heading"] == pytest.approx(0, abs=1e-14)
    assert a["horizontal"][3]["start_heading"] == pytest.approx(.25, abs=1e-14)
    assert a["horizontal"][3]["radius_end"] is None
    assert a["vertical"]["pvis"] == [{"station": 0, "elevation": 100, "curve_length": 0},
                                      {"station": 30, "elevation": 103, "curve_length": 20},
                                      {"station": 60, "elevation": 100, "curve_length": 0}]
    g = m["gravity_networks"][0]
    assert g["kind"] == "sanitary"
    assert g["nodes"][0]["diameter"] == 1.2  # diameterUnit=millimeter, not linearUnit.
    assert g["nodes"][1]["invert"] == 98.5  # Sump must not replace pipe connection invert.
    assert g["conduits"][0]["inlet_invert"] == 100
    assert g["conduits"][0]["outlet_invert"] == 99
    assert g["conduits"][0]["diameter"] == .3
    assert g["conduits"][0]["length"] == 100
    assert g["conduits"][0]["n"] is None  # No fabricated Manning coefficient.
    assert result["provenance"]["linear_to_m"] == 1


def test_independent_clothoid_endpoints_and_roundtrip_without_features(tmp_path):
    # Fixture endpoints from scipy 1.17 quad of cos/sin(theta); not adapter output.
    from .alignment import point_at
    m = landxml.read(_xml())["model_fragment"]
    a = m["alignments"][0]
    assert point_at(a, 20)[:2] == pytest.approx([19.997500289335157, .16663690712922566], abs=1e-9)
    assert point_at(a, 50)[:2] == pytest.approx([49.34020068739754, 5.945689809346931], abs=1e-9)
    path = tmp_path / "native.xml"
    landxml.write(m, path)
    root = etree.fromstring(path.read_bytes())
    for feature in root.xpath("//l:Feature", namespaces=NS):
        feature.getparent().remove(feature)
    imported = landxml.read(etree.tostring(root))["model_fragment"]
    _assert_close(imported["alignments"], m["alignments"])
    _assert_close(imported["surfaces"][0]["points"], m["surfaces"][0]["points"])
    assert imported["gravity_networks"][0]["conduits"][0]["outlet_invert"] == 99


@pytest.mark.parametrize("filename,factor", [("landxml_foot.xml", .3048), ("landxml_survey_foot.xml", 1200 / 3937)])
def test_international_and_survey_feet_are_distinct(filename, factor):
    # NIST SP 811 Appendix B; international foot exactly .3048 m,
    # US survey foot exactly 1200/3937 m (not interchangeable).
    result = landxml.read(DATA / filename)
    p = result["model_fragment"]["points"][0]
    assert p["x"] == pytest.approx(1000 * factor, abs=1e-12)
    assert p["y"] == pytest.approx(2000 * factor, abs=1e-12)
    assert p["z"] == pytest.approx(100 * factor, abs=1e-12)
    assert result["provenance"]["linear_to_m"] == factor
    assert result["model_fragment"]["surfaces"][0]["points"][2] == pytest.approx([0, 10 * factor, 110 * factor])
    if filename == "landxml_foot.xml":
        w = result["model_fragment"]["water_networks"][0]
        assert w["pipes"][0]["diameter"] == pytest.approx(.3048)
        assert w["pipes"][0]["length"] == pytest.approx(30.48)
        assert w["junctions"][0]["elevation"] == pytest.approx(30.48)
        assert w["pipes"][0]["roughness"] is None
    else:
        assert abs(p["x"] - 304.8) > .0006
        assert any("elevationUnit omitted" in x for x in result["assumptions"])


def test_full_roundtrip_preserves_non_native_fields(tmp_path):
    m = landxml.read(_xml())["model_fragment"]
    m["surfaces"][0].update(boundary=[[100, 200], [110, 200], [110, 210], [100, 210]],
                           breaklines=[[[100, 200, 10], [105, 205, 11]]])
    m["gravity_networks"][0]["conduits"][0].update(n=.011, vertices=[[150, 200]], material="PVC")
    water = landxml.read(_xml("landxml_foot.xml"))["model_fragment"]["water_networks"][0]
    water.update(headloss="H-W", options={"duration_s": 86400}, patterns={"daily": [1, 1.2]})
    water["junctions"][0].update(demand_m3s=.003, pattern="daily")
    water["pipes"][0].update(roughness=130, minor_loss=.2, status="cv")
    m["water_networks"] = [water]
    p = tmp_path / "roundtrip.xml"
    exported = landxml.write(m, p)
    assert exported["counts"] == {"surfaces": 1, "alignments": 1, "gravity_networks": 1, "water_networks": 1, "points": 1}
    _assert_close(landxml.read(p)["model_fragment"], m)


def test_edited_native_geometry_does_not_get_overridden_by_metadata(tmp_path):
    m = landxml.read(_xml())["model_fragment"]
    path = tmp_path / "edited.xml"
    landxml.write(m, path)
    root = etree.fromstring(path.read_bytes())
    p = root.find("l:Surfaces/l:Surface/l:Definition/l:Pnts/l:P", NS)
    p.text = "200 100 20"
    result = landxml.read(etree.tostring(root))
    assert result["model_fragment"]["surfaces"][0]["points"][0][2] == 20
    assert any("Ignored stale" in w for w in result["warnings"])


@pytest.mark.parametrize("before,after,reason", [
    (b"linearUnit=\"meter\"", b"linearUnit=\"furlong\"", "unsupported_units"),
    (b"<F>10 20 50</F>", b"<F>10 20 999</F>", "invalid_landxml"),
    (b"spiType=\"clothoid\"", b"spiType=\"bloss\"", "unsupported_spiral"),
    (b"slope=\"0.01\"", b"slope=\"0.02\"", "inconsistent_pipe_slope"),
    (b"diameter=\"300\"", b"diameter=\"0\"", "invalid_geometry"),
    (b"<Invert refPipe=\"P1\" elev=\"100\"", b"<Invert refPipe=\"missing\" elev=\"100\"", "not_found"),
    (b"0.16663690712922566 19.997500289335157</End>", b"1 19.997500289335157</End>", "inconsistent_spiral_endpoint"),
])
def test_invalid_or_unsupported_input_has_stable_refusal(before, after, reason):
    with pytest.raises(CivilError) as caught:
        landxml.read(_xml().replace(before, after, 1))
    assert caught.value.reason == reason


def test_external_entities_are_not_loaded():
    raw = _xml().replace(b"?>", b"?><!DOCTYPE LandXML [<!ENTITY leak SYSTEM 'file:///etc/passwd'>]>", 1)
    with pytest.raises(CivilError) as caught:
        landxml.read(raw)
    assert caught.value.reason == "unsupported_xml_dtd"


def test_unknown_root_entities_report_dropped():
    raw = _xml().replace(b"</LandXML>", b"<Parcels/></LandXML>")
    result = landxml.read(raw)
    assert result["dropped"] == [{"element": "Parcels", "reason": "unsupported_element"}]


def test_clockwise_arc_is_not_reversed_on_export(tmp_path):
    m = {"alignments": [{"id": "cw", "name": "CW", "start_station": 0,
                         "horizontal": [{"type": "arc", "start": [0, 0], "end": [10, -10], "center": [0, -10], "radius": 10, "dir": "cw"}], "vertical": None}]}
    p = tmp_path / "cw.xml"
    landxml.write(m, p)
    root = etree.fromstring(p.read_bytes())
    for f in root.xpath("//l:Feature", namespaces=NS):
        f.getparent().remove(f)
    arc = landxml.read(etree.tostring(root))["model_fragment"]["alignments"][0]["horizontal"][0]
    assert arc == m["alignments"][0]["horizontal"][0]


def test_network_names_are_globally_unique_and_slope_is_native(tmp_path):
    model = landxml.read(_xml())["model_fragment"]
    water = {"id": "g1", "name": "Water", "headloss": "H-W",
             "junctions": [{"id": "MH1", "x": 0, "y": 0, "elevation": 10, "demand_m3s": 0, "pattern": None},
                           {"id": "MH2", "x": 10, "y": 0, "elevation": 10, "demand_m3s": .001, "pattern": None}],
             "reservoirs": [], "tanks": [], "pipes": [{"id": "P1", "from": "MH1", "to": "MH2",
             "diameter": .2, "length": 10, "roughness": 130, "vertices": [], "status": "open"}],
             "pumps": [], "valves": [], "patterns": {}, "options": {}}
    model["water_networks"] = [water]
    path = tmp_path / "scoped.xml"
    landxml.write(model, path)
    root = etree.fromstring(path.read_bytes())
    structs = root.xpath("//l:Struct", namespaces=NS)
    pipes = root.xpath("//l:Pipe", namespaces=NS)
    struct_names = {s.get("name") for s in structs}
    pipe_names = {p.get("name") for p in pipes}
    assert len(struct_names) == len(structs) == 4
    assert len(pipe_names) == len(pipes) == 2
    assert all(p.get("refStart") in struct_names and p.get("refEnd") in struct_names for p in pipes)
    assert all(i.get("refPipe") in pipe_names for i in root.xpath("//l:Invert", namespaces=NS))
    assert float(pipes[0].get("slope")) == .01
    _assert_close(landxml.read(path)["model_fragment"], model)


def test_spiral_heading_can_come_from_start_pi_tangent():
    raw = _xml().replace(b' dirStart="90"', b'').replace(b' dirStart="75.67605512172942"', b'')
    alignment = landxml.read(raw)["model_fragment"]["alignments"][0]
    assert alignment["horizontal"][1]["start_heading"] == pytest.approx(0, abs=1e-12)
    assert alignment["horizontal"][3]["start_heading"] == pytest.approx(.25, abs=1e-12)


def test_cgpoint_metadata_uses_collection_feature_not_simple_content_child(tmp_path):
    model = {"points": [{"id": "p1", "name": "Benchmark", "x": 1.2, "y": 3.4,
                         "z": 5.6, "description": "Control", "survey_code": "BM"}]}
    path = tmp_path / "points.xml"
    landxml.write(model, path)
    root = etree.fromstring(path.read_bytes())
    point = root.find("l:CgPoints/l:CgPoint", NS)
    assert len(point) == 0  # LandXML CgPoint extends simpleContent PointType.
    assert root.find("l:CgPoints/l:Feature", NS) is not None
    assert landxml.read(path)["model_fragment"]["points"] == model["points"]


@pytest.mark.parametrize("error_type", [TypeError, ValueError, KeyError])
def test_export_programming_errors_are_not_disguised_as_invalid_model(monkeypatch, tmp_path, error_type):
    def broken_sub(*args, **kwargs):
        raise error_type("injected implementation defect")
    monkeypatch.setattr(landxml, "_sub", broken_sub)
    with pytest.raises(error_type, match="injected implementation defect") as caught:
        landxml.write({}, tmp_path / "broken.xml")
    assert not isinstance(caught.value, CivilError)


def test_export_invalid_model_shape_is_an_explicit_data_refusal(tmp_path):
    with pytest.raises(CivilError) as caught:
        landxml.write([], tmp_path / "invalid.xml")
    assert caught.value.reason == "invalid_model"
