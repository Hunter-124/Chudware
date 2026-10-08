"""Dependency-free SPF checks and hand-calculated IFC4X3 design/solid fixtures.

ifc_model.json was additionally parsed by IfcOpenShell 0.9.0 during development;
permanent tests intentionally require no IfcOpenShell runtime or daemon.
"""
from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from pathlib import Path

import pytest

from .errors import CivilError
from .ifc_export import write_ifc

DATA = Path(__file__).with_name("data")


def _model():
    return json.loads((DATA / "ifc_model.json").read_text())


def _entities(text):
    return {int(i): (kind, args) for i, kind, args in re.findall(r"^#(\d+)=(\w+)\((.*)\);$", text, re.M)}


def _args(raw):
    # Split STEP arguments without mistaking commas in aggregates/quoted text.
    values, start, depth, quoted, i = [], 0, 0, False, 0
    while i < len(raw):
        c = raw[i]
        if c == "'":
            if quoted and i + 1 < len(raw) and raw[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif not quoted:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == "," and depth == 0:
                values.append(raw[start:i])
                start = i + 1
        i += 1
    values.append(raw[start:])
    assert depth == 0 and not quoted
    return values


def _by_type(entities, name):
    return [(i, _args(raw)) for i, (kind, raw) in entities.items() if kind == name.upper()]


def _coords(entities, reference):
    kind, raw = entities[int(reference[1:])]
    assert kind in ("IFCCARTESIANPOINT", "IFCDIRECTION")
    return [float(n) for n in _args(raw)[0][1:-1].split(",")]


def _write(tmp_path, model=None):
    path = tmp_path / "civil.ifc"
    result = write_ifc(_model() if model is None else model, path)
    text = path.read_text()
    return result, text, _entities(text)


def test_spf_header_references_counts_guids_and_relationships(tmp_path):
    result, text, entities = _write(tmp_path)
    assert "FILE_SCHEMA(('IFC4X3_ADD2'));" in text
    assert text.startswith("ISO-10303-21;\nHEADER;")
    assert text.endswith("ENDSEC;\nEND-ISO-10303-21;\n")
    assert len(entities) == sum(result["entity_counts"].values())
    counts = Counter(kind for kind, _ in entities.values())
    assert counts == {k.upper(): v for k, v in result["entity_counts"].items()}
    assert counts["IFCPROJECT"] == 1
    assert counts["IFCSITE"] == 1
    assert counts["IFCALIGNMENT"] == 1
    assert counts["IFCALIGNMENTHORIZONTALSEGMENT"] == 5
    assert counts["IFCALIGNMENTVERTICALSEGMENT"] == 3
    assert counts["IFCPIPESEGMENT"] == 3
    assert counts["IFCDISTRIBUTIONCHAMBERELEMENT"] == 2
    assert counts["IFCDISTRIBUTIONSYSTEM"] == 3
    # Strip string literals before scanning references: names deliberately contain #99.
    for _, raw in entities.values():
        outside_strings = re.sub(r"'(?:''|[^'])*'", "''", raw)
        assert all(int(ref) in entities for ref in re.findall(r"#(\d+)", outside_strings))
    root_types = {"IFCPROJECT", "IFCSITE", "IFCALIGNMENT", "IFCALIGNMENTHORIZONTAL", "IFCALIGNMENTVERTICAL",
                  "IFCALIGNMENTSEGMENT", "IFCPIPESEGMENT", "IFCDISTRIBUTIONCHAMBERELEMENT", "IFCDISTRIBUTIONSYSTEM", "IFCPROPERTYSET"}
    guids = [_args(raw)[0][1:-1] for kind, raw in entities.values() if kind in root_types or kind.startswith("IFCREL")]
    assert len(guids) == len(set(guids))
    assert all(re.fullmatch(r"[0-3][0-9A-Za-z_$]{21}", g) for g in guids)
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"
    # 22 IFC base64 digits must fit exactly in a 128-bit UUID.
    assert all(sum(alphabet.index(c) << (6 * (21 - i)) for i, c in enumerate(g)) < 2 ** 128 for g in guids)
    assert "'Civil reference O''Brien #99'" in text
    assert counts["IFCRELAGGREGATES"] == 1
    assert counts["IFCRELNESTS"] == 3
    assert counts["IFCRELCONTAINEDINSPATIALSTRUCTURE"] == 1
    systems = _by_type(entities, "IfcDistributionSystem")
    assert {args[-1] for _, args in systems} == {".SEWAGE.", ".STORMWATER.", ".DOMESTICCOLDWATER."}
    groups = _by_type(entities, "IfcRelAssignsToGroup")
    assert len(groups) == 3
    grouped = set()
    for _, args in groups:
        grouped.update(int(x[1:]) for x in args[4][1:-1].split(","))
    pipe_chamber = {i for i, (kind, _) in entities.items() if kind in ("IFCPIPESEGMENT", "IFCDISTRIBUTIONCHAMBERELEMENT")}
    assert grouped == pipe_chamber
    containment = _by_type(entities, "IfcRelContainedInSpatialStructure")[0][1]
    contained = {int(x[1:]) for x in containment[4][1:-1].split(",")}
    assert contained == pipe_chamber | {i for i, _ in _by_type(entities, "IfcAlignment")}


def test_horizontal_segments_and_signed_radii(tmp_path):
    _, _, entities = _write(tmp_path)
    segments = [args for _, args in _by_type(entities, "IfcAlignmentHorizontalSegment")]
    assert [a[-1] for a in segments] == [".LINE.", ".CLOTHOID.", ".CIRCULARARC.", ".CLOTHOID.", ".LINE."]
    assert [float(a[6]) for a in segments] == pytest.approx([10, 10, 20, 10, 10], abs=1e-10)
    assert [(float(a[4]), float(a[5])) for a in segments] == [(0, 0), (0, 100), (100, 100), (100, 0), (0, 0)]
    assert float(segments[3][3]) == .25
    model = _model()
    model["alignments"][0]["horizontal"] = [{"type": "arc", "start": [0, 0], "end": [10, -10], "center": [0, -10], "radius": 10, "dir": "cw"}]
    model["alignments"][0]["vertical"] = None
    _, _, entities = _write(tmp_path, model)
    args = _by_type(entities, "IfcAlignmentHorizontalSegment")[0][1]
    assert float(args[3]) == pytest.approx(0, abs=1e-14)
    assert (float(args[4]), float(args[5])) == (-10, -10)
    assert float(args[6]) == pytest.approx(5 * math.pi)


def test_vertical_parabola_pvc_pvt_and_station_origin(tmp_path):
    # Symmetric crest: L=20, g1=.1, g2=-.1; PVC/PVT z=102,
    # radius of curvature parameter = L/(g2-g1)=-100 m.
    model = _model()
    model["alignments"][0]["start_station"] = 1000
    for p in model["alignments"][0]["vertical"]["pvis"]:
        p["station"] += 1000
    _, _, entities = _write(tmp_path, model)
    rows = [args for _, args in _by_type(entities, "IfcAlignmentVerticalSegment")]
    assert [r[-1] for r in rows] == [".CONSTANTGRADIENT.", ".PARABOLICARC.", ".CONSTANTGRADIENT."]
    assert [float(r[2]) for r in rows] == [0, 20, 40]
    assert [float(r[3]) for r in rows] == [20, 20, 20]
    assert [float(r[4]) for r in rows] == [100, 102, 102]
    assert (float(rows[1][5]), float(rows[1][6]), float(rows[1][7])) == pytest.approx((.1, -.1, -100))
    properties = _by_type(entities, "IfcPropertySingleValue")
    assert properties[0][1][2] == "IFCLENGTHMEASURE(1000.)"


def test_sloping_pipe_solid_is_oriented_and_uses_invert_plus_radius(tmp_path):
    _, _, entities = _write(tmp_path)
    solid = _by_type(entities, "IfcExtrudedAreaSolid")[0][1]
    profile = _args(entities[int(solid[0][1:])][1])
    assert float(profile[3]) == .2
    axis = _args(entities[int(solid[1][1:])][1])
    assert _coords(entities, axis[0]) == pytest.approx([0, 0, 10.2])
    direction = _coords(entities, axis[1])
    assert direction == pytest.approx([3 / math.sqrt(34), 4 / math.sqrt(34), -3 / math.sqrt(34)])
    assert float(solid[3]) == pytest.approx(math.sqrt(34))
    assert _coords(entities, solid[2]) == [0, 0, 1]
    end = [a + b * float(solid[3]) for a, b in zip(_coords(entities, axis[0]), direction)]
    assert end == pytest.approx([3, 4, 7.2])
    # Two straight extrusion reaches preserve a water pipe's intermediate vertex.
    water = _by_type(entities, "IfcPipeSegment")[-1][1]
    product = _args(entities[int(water[6][1:])][1])
    representation = _args(entities[int(product[2][2:-1])][1])
    assert len(representation[3][1:-1].split(",")) == 2


def test_georeferencing_map_conversion_matches_contract_transform(tmp_path):
    _, _, entities = _write(tmp_path)
    assert _by_type(entities, "IfcProjectedCRS")[0][1][0] == "'EPSG:26915'"
    assert _by_type(entities, "IfcProjectedCRS")[0][1][3] == "'EPSG:5703'"
    mapping = _by_type(entities, "IfcMapConversion")[0][1]
    assert [float(v) for v in mapping[2:]] == pytest.approx([500000, 4500000, 0, math.cos(.2), math.sin(.2), .9996])
    # Contract local grid X=origin+s(x cos theta-y sin theta).
    x, y = 100, 200
    transformed = [float(mapping[2]) + float(mapping[7]) * (x * float(mapping[5]) - y * float(mapping[6])),
                   float(mapping[3]) + float(mapping[7]) * (x * float(mapping[6]) + y * float(mapping[5]))]
    assert transformed == pytest.approx([500058.24948250846, 4500215.793896548], abs=1e-9, rel=0)


def test_guids_deterministic_scoped_and_strings_unicode_safe(tmp_path):
    model = _model()
    result, text, first = _write(tmp_path, model)
    model["name"] = "Civil reference O'Brien #99"  # Same identity, different output path.
    other = tmp_path / "other.ifc"
    write_ifc(model, other)
    assert other.read_text() == text
    # All three networks intentionally use pipe id P1; GUID scoping avoids collisions.
    pipe_ids = [_args(raw)[0] for kind, raw in first.values() if kind == "IFCPIPESEGMENT"]
    assert len(set(pipe_ids)) == 3
    model["alignments"][0]["name"] = "道路 \U00020000\\survey"
    _, text, _ = _write(tmp_path, model)
    assert "\\X4\\" in text and "\\X0\\" in text
    assert text.isascii()


def test_unknown_physical_water_elevation_is_reported_not_head_as_z(tmp_path):
    model = _model()
    water = model["water_networks"][0]
    water["junctions"] = [water["junctions"][1]]
    water["reservoirs"] = [{"id": "J1", "x": 0, "y": 10, "head": 99}]
    result, _, entities = _write(tmp_path, model)
    assert len(_by_type(entities, "IfcPipeSegment")) == 2
    assert any(s["reason"] == "missing_pipe_elevation" and s["id"] == "P1" for s in result["skipped"])


@pytest.mark.parametrize("change,reason", [
    ("duplicate_alignment", "duplicate_id"), ("missing_endpoint", "not_found"),
    ("zero_diameter", "invalid_geometry"), ("overlap_vertical", "overlapping_vertical_curves"),
])
def test_invalid_input_refuses_without_writing(tmp_path, change, reason):
    model = _model()
    if change == "duplicate_alignment":
        model["alignments"].append(copy.deepcopy(model["alignments"][0]))
    elif change == "missing_endpoint":
        model["water_networks"][0]["pipes"][0]["from"] = "absent"
    elif change == "zero_diameter":
        model["water_networks"][0]["pipes"][0]["diameter"] = 0
    else:
        model["alignments"][0]["vertical"]["pvis"][1]["curve_length"] = 80
    path = tmp_path / "invalid.ifc"
    with pytest.raises(CivilError) as caught:
        write_ifc(model, path)
    assert caught.value.reason == reason
    assert not path.exists()
