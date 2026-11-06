"""Behavioral coverage for routed-harness formboard layouts and exports."""
import copy
import math
import xml.etree.ElementTree as ET

import pytest

from services.harness import formboard as F
from services.harness import model as M


def three_branch_harness():
    h = M.new_harness('Panel & sensors')
    for ref, position in (("J1", [-200, 0, 0]), ("J2", [180, 0, 0]),
                          ("J3", [0, 140, 100])):
        M.add_connector(h, ref=ref, family="jst_ph", pins=2,
                        frame=M.make_frame(position, [1, 0, 0]))
    h["nodes"].extend([{"id": "B", "kind": "breakout", "pos": [0, 0, 0]},
                       {"id": "K1", "kind": "clamp", "pos": [-100, 0, 0]}])
    for a, b in (("J1", "K1"), ("K1", "B"), ("B", "J2"), ("B", "J3")):
        M.add_segment(h, a, b, [M.get_node(h, a)["pos"], M.get_node(h, b)["pos"]])
    M.add_wire(h, from_="J1:1", to="J2:1", insulation_od_mm=2, gauge_awg=20,
               color="red", signal="supply", wire_id="W1")
    M.add_wire(h, from_="J1:2", to="J3:1", insulation_od_mm=2, gauge_awg=20,
               color="blue", signal="sense & return", wire_id="W2")
    return h


def test_three_branch_true_lengths_clearance_and_determinism():
    h = three_branch_harness()
    original = copy.deepcopy(h)
    layout = F.layout_formboard(h, scale=1)
    assert h == original
    assert layout == F.layout_formboard(h, scale=1)
    assert layout["ok"], layout["overlaps"]
    assert len(layout["segments"]) == 4
    assert layout["verification"]["segments_checked"] == 4
    actual = {s["id"]: M.seg_length(s) for s in h["segments"]}
    for s in layout["segments"]:
        assert s["length_mm"] == pytest.approx(actual[s["id"]])
        assert math.dist(s["p0"], s["p1"]) == pytest.approx(actual[s["id"]])
    for i, a in enumerate(layout["segments"]):
        for b in layout["segments"][i + 1:]:
            if not ({a["a"], a["b"]} & {b["a"], b["b"]}):
                assert F._seg_dist(a["p0"], a["p1"], b["p0"], b["p1"]) >= (
                    a["half_width"] + b["half_width"] + F.GAP_MM)


def test_scaled_geometry_and_tables():
    layout = F.layout_formboard(three_branch_harness(), scale=0.5)
    for s in layout["segments"]:
        assert math.dist(s["p0"], s["p1"]) == pytest.approx(s["length_mm"] * 0.5)
    tables = {t["title"]: t for t in layout["tables"]}
    wire = next(t for title, t in tables.items() if title.startswith("WIRE LIST"))
    assert any(row[:3] == ["W1", "J1:1", "J2:1"] for row in wire["rows"])
    assert any(row[:3] == ["W2", "J1:2", "J3:1"] for row in wire["rows"])
    pinout = next(t for title, t in tables.items() if title.startswith("PINOUT J1 "))
    assert any(row[0] == "2" and row[2:4] == ["W2", "J3:1"] for row in pinout["rows"])
    assert ["J3", "W2", "2", "1", "sense & return"] in tables["FROM-TO J1"]["rows"]
    clamps = next(t for title, t in tables.items() if title.startswith("CLAMPS"))
    assert any(row[0] == "K1" and "s1=" in row[1] and "s2=" in row[1] for row in clamps["rows"])


def test_svg_is_well_formed_and_contains_geometry_and_tables():
    h = three_branch_harness()
    h["segments"][-1]["id"] = 'special"&segment'
    layout = F.layout_formboard(h)
    root = ET.fromstring(F.render_svg(layout))
    ns = {"s": "http://www.w3.org/2000/svg"}
    lines = [e for e in root.findall("s:line", ns) if "data-segment" in e.attrib]
    assert len(lines) == len(layout["segments"])
    assert any(e.get("data-segment") == 'special"&segment' for e in lines)
    assert len(root.findall("s:polygon", ns)) == 3
    assert root.find('s:circle[@data-clamp="K1"]', ns) is not None
    texts = [e.text for e in root.findall("s:text", ns)]
    assert "sense & return" in texts
    assert "FROM-TO J1" in texts
    assert any(t and t.startswith("WIRE LIST") for t in texts)
    assert any(t and t.startswith("CLAMPS") for t in texts)


def test_dxf_has_real_geometry_and_tables(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    layout = F.layout_formboard(three_branch_harness())
    path = tmp_path / "board.dxf"
    assert F.write_dxf(layout, str(path)) == str(path)
    drawing = ezdxf.readfile(path)
    assert drawing.units == ezdxf.units.MM
    msp = drawing.modelspace()
    bundles = list(msp.query('LWPOLYLINE[layer=="HARNESS_BUNDLE"]'))
    assert len(bundles) == len(layout["segments"])
    for entity, segment in zip(bundles, layout["segments"]):
        points = list(entity.get_points("xy"))
        assert math.dist(points[0], points[1]) == pytest.approx(segment["length_mm"] * layout["scale"])
    assert len(msp.query('LWPOLYLINE[layer=="HARNESS_CONNECTOR"]')) == 3
    assert len(msp.query('CIRCLE[layer=="HARNESS_CLAMP"]')) == 1
    table_text = [e.dxf.text for e in msp.query('TEXT[layer=="HARNESS_TABLE"]')]
    assert "FROM-TO J1" in table_text
    assert "W1" in table_text and "W2" in table_text
    assert any(t.startswith("CLAMPS") for t in table_text)
    assert len(msp.query('LINE[layer=="HARNESS_TABLE"]')) > 0


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), "bad"])
def test_invalid_scale_and_sheet_are_actionable(value):
    h = three_branch_harness()
    with pytest.raises(M.HarnessError, match="scale") as exc:
        F.layout_formboard(h, scale=value)
    assert exc.value.code == "bad_scale"
    with pytest.raises(M.HarnessError, match="max_sheet_mm") as exc:
        F.layout_formboard(h, max_sheet_mm=value)
    assert exc.value.code == "bad_sheet_size"


def test_empty_and_cyclic_harnesses_are_refused():
    with pytest.raises(M.HarnessError) as exc:
        F.layout_formboard(M.new_harness("empty"))
    assert exc.value.code == "nothing_to_lay_out"
    h = three_branch_harness()
    M.add_segment(h, "J2", "J3", [M.get_node(h, "J2")["pos"], M.get_node(h, "J3")["pos"]])
    with pytest.raises(M.HarnessError) as exc:
        F.layout_formboard(h)
    assert exc.value.code == "not_a_tree"


def test_unrouted_wire_is_not_reported_as_success():
    h = three_branch_harness()
    M.add_connector(h, ref="J4", family="jst_ph", pins=1,
                    frame=M.make_frame([500, 0, 0], [1, 0, 0]))
    M.add_wire(h, from_="J2:2", to="J4:1", insulation_od_mm=2, gauge_awg=20, wire_id="W3")
    layout = F.layout_formboard(h)
    assert layout["unrouted_wires"] == ["W3"]
    assert not layout["ok"]
    assert any("not routed" in warning for warning in layout["warnings"])


def test_unknown_bundle_od_marks_layout_and_exports_incomplete():
    h = three_branch_harness()
    # A legacy or explicitly incomplete harness can still have no packing factor.
    h["defaults"]["packing_factor"] = None
    M.add_wire(h, from_="J1:1", to="J2:2", insulation_od_mm=2, gauge_awg=20,
               wire_id="W3", allow_shared_pin=True)
    layout = F.layout_formboard(h)
    assert not layout["ok"]
    assert set(layout["unknown_od_segments"]) == {"s1", "s2"}
    assert all(s["od_mm"] is None for s in layout["segments"]
               if s["id"] in layout["unknown_od_segments"])
    assert any("clearance is unverified" in w for w in layout["warnings"])
    root = ET.fromstring(F.render_svg(layout))
    assert "CLEARANCE UNVERIFIED" in root.find("{http://www.w3.org/2000/svg}title").text
    h["defaults"]["packing_factor"] = 0.65
    layout = F.layout_formboard(h)
    assert not layout["unknown_od_segments"]
    assert all(s["od_mm"] is not None for s in layout["segments"])


def test_overcrowded_breakout_is_not_reported_as_success():
    h = M.new_harness("crowded")
    M.add_connector(h, ref="J0", family="jst_ph", pins=15,
                    frame=M.make_frame([-200, 0, 0], [1, 0, 0]))
    h["nodes"].append({"id": "B", "kind": "breakout", "pos": [0, 0, 0]})
    M.add_segment(h, "J0", "B", [[-200, 0, 0], [0, 0, 0]])
    for i in range(15):
        theta = 2 * math.pi * i / 15
        pos = [200 * math.cos(theta), 200 * math.sin(theta), 0]
        ref = f"J{i + 1}"
        M.add_connector(h, ref=ref, family="jst_ph", pins=1,
                        frame=M.make_frame(pos, [1, 0, 0]))
        M.add_segment(h, "B", ref, [[0, 0, 0], pos])
        M.add_wire(h, from_=f"J0:{i + 1}", to=f"{ref}:1", insulation_od_mm=2,
                   gauge_awg=20)
    layout = F.layout_formboard(h)
    assert not layout["ok"]
    assert F.overlap_count(layout) > 0
    assert any(o["kind"] == "adjacent_angle" for o in layout["overlaps"])
