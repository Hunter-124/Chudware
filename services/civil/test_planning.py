"""Offline V&V: hand geometry, published seven-activity CPM and MSPDI units."""
import copy
import csv
import io
import json
import math
from xml.etree import ElementTree as ET

import pytest

from .errors import CivilError
from .planning import (cost_estimate, export_csv, export_msproject_xml,
                       import_msproject_xml, link_4d, quantity_takeoff,
                       requirement_evidence, schedule_cpm, state_at)
from services.requirements.requirements import Trace


def network_model():
    # Euclidean lengths: 3-4-5 triangle, 12 m tangent, another 3-4-5.
    # Invert rises 12 m on the 5 m first reach => 13 m 3D length.
    return {"gravity_networks": [{"id": "sewer", "nodes": [
        {"id": "a", "type": "manhole", "x": 0, "y": 0, "invert": 0, "rim": 3, "diameter": 1.2},
        {"id": "b", "type": "manhole", "x": 3, "y": 4, "invert": 12, "rim": 15, "diameter": 1.2},
        {"id": "c", "type": "manhole", "x": 15, "y": 4, "invert": 12, "rim": 15, "diameter": 1.2},
        {"id": "d", "type": "outfall", "x": 18, "y": 8, "invert": 12, "rim": 15, "diameter": 0.6}],
        "conduits": [
            {"id": "p1", "from": "a", "to": "b", "diameter": 0.3, "material": "PVC"},
            {"id": "p2", "from": "b", "to": "c", "diameter": 0.3, "material": "PVC"},
            {"id": "p3", "from": "c", "to": "d", "diameter": 0.6, "material": "RCP"}]}]}


def single_pipe(rims=(2, 2), inverts=(0, 0), vertices=None):
    return {"gravity_networks": [{"id": "g", "nodes": [
        {"id": "a", "type": "manhole", "x": 0, "y": 0, "rim": rims[0], "invert": inverts[0], "diameter": 1.2},
        {"id": "b", "type": "manhole", "x": 10, "y": 0, "rim": rims[1], "invert": inverts[1], "diameter": 1.2}],
        "conduits": [{"id": "p", "from": "a", "to": "b", "diameter": 0.4,
                      "outer_diameter_m": 0.5, "material": "PVC", "vertices": vertices or []}]}]}


def trench_options(**extra):
    return {"side_clearance_m": 0.25, "bedding_depth_m": 0.2,
            "sample_step_m": 3, **extra}


def quantity(qto, item):
    return sum(row["quantity"] for row in qto["items"] if row["item"] == item)


def test_three_conduit_lengths_and_group_trace_exact():
    model = network_model()
    old = copy.deepcopy(model)
    qto = quantity_takeoff(model)
    assert quantity(qto, "pipe_length_plan") == 22
    assert quantity(qto, "pipe_length_3d") == 30
    pvc_plan = next(r for r in qto["items"] if r["item"] == "pipe_length_plan" and r["basis"]["material"] == "PVC")
    assert pvc_plan["quantity"] == 17
    assert pvc_plan["element_ids"] == ["p1", "p2"]
    assert pvc_plan["basis"]["diameter_m"] == 0.3
    assert pvc_plan["basis"]["network_id"] == "sewer"
    structures = [r for r in qto["items"] if r["item"] == "structure_count"]
    assert {(r["basis"]["type"], r["quantity"]) for r in structures} == {("manhole", 3), ("outfall", 1)}
    assert model == old
    json.dumps(qto, allow_nan=False)


def test_vertices_measured_not_hydraulic_length():
    model = single_pipe(vertices=[[5, 12]])
    model["gravity_networks"][0]["conduits"][0]["length"] = 10
    qto = quantity_takeoff(model)
    assert quantity(qto, "pipe_length_plan") == 26
    assert quantity(qto, "pipe_length_3d") == 26
    assert any("hydraulic length differs" in w for w in qto["warnings"])


@pytest.mark.parametrize("slope,expected", [(0, 22), (1.5, 94.6)])
def test_constant_depth_trench_closed_form_and_partition(slope, expected):
    # Width=0.5+2*0.25=1; depth=2+0.2=2.2; L=10.
    # Vertical V=22, 1.5H:1V V=(1*2.2+1.5*2.2²)*10=94.6.
    qto = quantity_takeoff(single_pipe(), trench=trench_options(side_slope=slope))
    assert quantity(qto, "trench_excavation") == pytest.approx(expected)
    # Cross-section primitive F(h)=w*h+s*h², circle displacement pi*OD²*L/4.
    assert quantity(qto, "bedding") == pytest.approx((0.2 + slope * 0.04) * 10)
    assert quantity(qto, "pipe_displaced") == pytest.approx(math.pi * 0.5 ** 2 * 10 / 4)
    # Haunch datum invert->springline, lower half pipe displacement removed.
    assert quantity(qto, "haunch") == pytest.approx((0.25 + slope * (0.45 ** 2 - 0.2 ** 2)) * 10 - math.pi * 0.5 ** 2 * 10 / 8)
    assert sum(quantity(qto, i) for i in ("bedding", "haunch", "backfill", "pipe_displaced")) == pytest.approx(expected)
    trench_rows = [r for r in qto["items"] if r["unit"] == "m3"]
    assert all(r["element_ids"] == ["p"] for r in trench_rows)


@pytest.mark.parametrize("step", [0.7, 3, 100])
def test_linear_rim_depth_is_exact_prismoidal(step):
    # Rims 2->4, invert constant, bedding .2 => d=2.2->4.2.
    # w=1, s=.5: mean d=3.2, mean d²=(2.2²+2.2*4.2+4.2²)/3.
    expected = 10 * (3.2 + 0.5 * (2.2 ** 2 + 2.2 * 4.2 + 4.2 ** 2) / 3)
    qto = quantity_takeoff(single_pipe((2, 4)), trench=trench_options(side_slope=0.5, sample_step_m=step))
    assert quantity(qto, "trench_excavation") == pytest.approx(expected, rel=1e-12)
    assert any("interpolated linearly" in a for a in qto["assumptions"])


def test_ground_sampler_and_table_width_and_od():
    model = single_pipe((100, 100))
    del model["gravity_networks"][0]["conduits"][0]["outer_diameter_m"]
    qto = quantity_takeoff(model, ground=lambda x, y: 2 + x / 5,
                          trench=trench_options(width_table=[{"max_diameter_m": 0.6, "width_m": 2}],
                                                outer_diameter_by_material={"PVC": [[0.4, 0.5]]}))
    assert quantity(qto, "trench_excavation") == pytest.approx(2 * 3.2 * 10)
    assert quantity(qto, "bedding") == pytest.approx(4)


def test_pavement_crossing_union_area_and_earthwork_trace():
    polygon = {"id": "road", "polygon": [[2, -2], [7, -2], [7, 2], [2, 2]]}
    qto = quantity_takeoff(single_pipe(), trench=trench_options(paved_polygons=[polygon, polygon]),
                          surfaces_volume=[{"cut_m3": 120, "fill_m3": 80, "surface_ids": ["eg", "fg"]}])
    # 5 m centerline inside paved polygon, width=1 m (overlap not double counted).
    assert quantity(qto, "pavement_restoration") == pytest.approx(5)
    assert quantity(qto, "earthwork_cut") == 120
    assert quantity(qto, "earthwork_fill") == 80
    assert next(r for r in qto["items"] if r["item"] == "earthwork_cut")["element_ids"] == ["eg", "fg"]


@pytest.mark.parametrize("failure", ["outer_diameter_missing", "ground_outside_surface", "insufficient_trench_depth", "trench_width_missing"])
def test_trench_refusals(failure):
    model = single_pipe()
    options = trench_options()
    ground = None
    if failure == "outer_diameter_missing":
        del model["gravity_networks"][0]["conduits"][0]["outer_diameter_m"]
    elif failure == "ground_outside_surface":
        ground = lambda x, y: None
    elif failure == "insufficient_trench_depth":
        ground = lambda x, y: 0.1
    else:
        options["width_table"] = [{"max_diameter_m": 0.1, "width_m": 1}]
    with pytest.raises(CivilError) as exc:
        quantity_takeoff(model, ground=ground, trench=options)
    assert exc.value.reason == failure


def test_water_reservoir_head_not_substituted_for_elevation():
    model = {"water_networks": [{"id": "w", "reservoirs": [{"id": "r", "x": 0, "y": 0, "head": 100}],
                                "junctions": [{"id": "j", "x": 10, "y": 0, "elevation": 0}],
                                "pipes": [{"id": "p", "from": "r", "to": "j", "diameter": 0.1}]}]}
    qto = quantity_takeoff(model)
    assert quantity(qto, "pipe_length_plan") == 10
    assert quantity(qto, "pipe_length_3d") == 0
    assert any("reservoir head is not elevation" in w for w in qto["warnings"])


def price(unit="m", unit_price=20, currency="USD", **extra):
    return {"unit": unit, "unit_price": unit_price, "currency": currency,
            "source": "User quote Q-123", "date": "2026-10-01", **extra}


def simple_quantities():
    return {"items": [
        {"item": "pipe_length_3d", "element_ids": ["p"], "quantity": 10, "unit": "m", "basis": {"material": "PVC", "diameter_m": 0.4, "category": "pipe"}},
        {"item": "bedding", "element_ids": ["p"], "quantity": 2, "unit": "m3", "basis": {"category": "earthwork"}},
        {"item": "backfill", "element_ids": ["p"], "quantity": 8, "unit": "m3", "basis": {"category": "earthwork"}}]}


def test_cost_totals_escalation_contingency_and_unpriced_csv():
    book = {"PVC400": price(match={"item": "pipe_length_3d", "material": "PVC", "diameter_m": 0.4}),
            "bedding": price("m3", 50)}
    cost = cost_estimate(simple_quantities(), book, contingency_pct=10, escalation={"rate_pct": 5, "years": 2})
    totals = cost["totals_by_currency"]["USD"]
    assert totals["subtotal"] == 300
    assert totals["categories"] == {"pipe": 200, "earthwork": 100}
    assert totals["escalation"] == pytest.approx(30.75)
    assert totals["contingency"] == pytest.approx(33.075)
    assert totals["total"] == pytest.approx(363.825)
    assert totals["category_totals"]["pipe"] == pytest.approx(242.55)
    assert cost["complete"] is False
    assert [r["item"] for r in cost["unpriced"]] == ["backfill"]
    assert "amount" not in cost["unpriced"][0]
    csv_rows = list(csv.DictReader(io.StringIO(export_csv(cost))))
    assert len(csv_rows) == 3
    assert csv_rows[-1]["priced"] == "False"
    assert csv_rows[-1]["amount"] == ""
    assert json.loads(csv_rows[0]["element_ids"]) == ["p"]
    assert json.loads(csv_rows[0]["basis"])["diameter_m"] == 0.4
    qto_rows = list(csv.DictReader(io.StringIO(export_csv(simple_quantities()))))
    assert float(qto_rows[0]["quantity"]) == 10


def test_cost_currency_separation_and_exact_unit_refusal():
    cost = cost_estimate(simple_quantities(), {"pipe_length_3d": price(), "bedding": price("m3", 50, "EUR")})
    assert cost["totals_by_currency"]["USD"]["total"] == 200
    assert cost["totals_by_currency"]["EUR"]["total"] == 100
    with pytest.raises(CivilError) as exc:
        cost_estimate(simple_quantities(), {"pipe_length_3d": price("ft")})
    assert exc.value.reason == "unit_mismatch"
    with pytest.raises(CivilError) as exc:
        cost_estimate(simple_quantities(), {"pipe_length_3d": price(), "also": price(match={"item": "pipe_length_3d"})})
    assert exc.value.reason == "ambiguous_price_match"


def textbook_tasks():
    # Published seven-activity worked example, AtoZmath/Piyush N Shah:
    # https://cbom.atozmath.com/example/CBOM/PertCPM.aspx?q=1.3&q1=E1
    # Expected PERT times (to+4tm+tp)/6: A2 B4 C3 D1 E6 F5 G7.
    # Dependencies D<-A, E<-B, F<-C, G<-D,E. Published CP B-E-G,
    # project duration 17. Treat the published expected times as deterministic CPM.
    durations = {"A": 2, "B": 4, "C": 3, "D": 1, "E": 6, "F": 5, "G": 7}
    predecessors = {"D": ["A"], "E": ["B"], "F": ["C"], "G": ["D", "E"]}
    return [{"id": key, "name": "Activity " + key, "duration_d": value,
             "predecessors": [{"id": p, "type": "FS", "lag_d": 0} for p in predecessors.get(key, [])],
             "element_ids": ["p"] if key in ("B", "G") else []} for key, value in durations.items()]


def test_published_seven_activity_cpm_forward_backward_and_float():
    tasks = textbook_tasks()
    before = copy.deepcopy(tasks)
    schedule = schedule_cpm(tasks)
    assert schedule["project_duration_d"] == 17
    assert schedule["critical_path"] == ["B", "E", "G"]
    # ES, EF, LS, LF, total float, free float: independent hand forward/back pass.
    expected = {"A": (0, 2, 7, 9, 7, 0), "B": (0, 4, 0, 4, 0, 0),
                "C": (0, 3, 9, 12, 9, 0), "D": (2, 3, 9, 10, 7, 7),
                "E": (4, 10, 4, 10, 0, 0), "F": (3, 8, 12, 17, 9, 9),
                "G": (10, 17, 10, 17, 0, 0)}
    fields = ("es_d", "ef_d", "ls_d", "lf_d", "total_float_d", "free_float_d")
    for row in schedule["tasks"]:
        assert tuple(row[k] for k in fields) == expected[row["id"]]
    assert tasks == before


def test_generalized_ss_ff_sf_and_negative_fs_lags():
    # A:0..10, B SS(A,+2):2..6, C FF(A,+3):7..13,
    # D SF(C,+8):11..15, E FS(D,-2):13..16. Finish=16.
    tasks = [
        {"id": "A", "duration_d": 10},
        {"id": "B", "duration_d": 4, "predecessors": [{"id": "A", "type": "SS", "lag_d": 2}]},
        {"id": "C", "duration_d": 6, "predecessors": [{"id": "A", "type": "FF", "lag_d": 3}]},
        {"id": "D", "duration_d": 4, "predecessors": [{"id": "C", "type": "SF", "lag_d": 8}]},
        {"id": "E", "duration_d": 3, "predecessors": [{"id": "D", "type": "FS", "lag_d": -2}]}]
    schedule = schedule_cpm(tasks)
    rows = {r["id"]: r for r in schedule["tasks"]}
    assert schedule["project_duration_d"] == 16
    assert [(rows[k]["es_d"], rows[k]["ef_d"]) for k in rows] == [(0, 10), (2, 6), (7, 13), (11, 15), (13, 16)]
    assert schedule["critical_path"] == ["A", "C", "D", "E"]
    assert rows["B"]["total_float_d"] == 10
    assert rows["B"]["free_float_d"] == 10
    assert all(rows[k]["total_float_d"] == 0 for k in ("A", "C", "D", "E"))


def test_cycles_missing_predecessor_and_empty_schedule():
    with pytest.raises(CivilError) as exc:
        schedule_cpm([{"id": "a", "duration_d": 1, "predecessors": [{"id": "b"}]},
                      {"id": "b", "duration_d": 1, "predecessors": [{"id": "a"}]}])
    assert exc.value.reason == "schedule_cycle"
    with pytest.raises(CivilError) as exc:
        schedule_cpm([{"id": "a", "duration_d": 1, "predecessors": [{"id": "missing"}]}])
    assert exc.value.reason == "not_found"
    assert schedule_cpm([])["project_duration_d"] == 0
    assert schedule_cpm([])["critical_path"] == []


def test_msproject_round_trip_identity_elements_lags_and_type_codes():
    tasks = textbook_tasks()
    tasks[3]["predecessors"][0].update(type="SS", lag_d=0.5)
    tasks[4]["predecessors"][0].update(type="FF", lag_d=1)
    tasks[5]["predecessors"][0].update(type="SF", lag_d=2)
    schedule = schedule_cpm(tasks)
    text = export_msproject_xml(schedule, "2026-10-01")
    root = ET.fromstring(text)
    ns = {"p": "http://schemas.microsoft.com/project"}
    assert root.findtext("p:StartDate", namespaces=ns) == "2026-10-01T08:00:00"
    b = next(t for t in root.findall("p:Tasks/p:Task", ns) if t.findtext("p:Name", namespaces=ns) == "Activity B")
    assert b.findtext("p:Duration", namespaces=ns) == "PT32H"
    links = root.findall("p:Tasks/p:Task/p:PredecessorLink", ns)
    assert {int(l.findtext("p:Type", namespaces=ns)) for l in links} == {0, 1, 2, 3}
    assert "2400" in [l.findtext("p:LinkLag", namespaces=ns) for l in links]
    restored = import_msproject_xml(text)
    assert restored["tasks"] == schedule["tasks"]
    assert restored["critical_path"] == schedule["critical_path"]
    assert restored["project_duration_d"] == schedule["project_duration_d"]


def test_msproject_refuses_entities_and_unsupported_calendars():
    with pytest.raises(CivilError) as exc:
        import_msproject_xml('<!DOCTYPE Project [<!ENTITY x SYSTEM "file:///etc/passwd">]><Project/>')
    assert exc.value.reason == "invalid_schedule_xml"
    text = export_msproject_xml(schedule_cpm(textbook_tasks()), "2026-10-01")
    text = text.replace("<MinutesPerDay>480</MinutesPerDay>", "<MinutesPerDay>600</MinutesPerDay>")
    with pytest.raises(CivilError) as exc:
        import_msproject_xml(text)
    assert exc.value.reason == "unsupported_schedule_calendar"


def test_four_d_multiple_tasks_gap_boundaries_and_missing_element():
    schedule = schedule_cpm(textbook_tasks())
    linked = link_4d(schedule, single_pipe())
    assert linked["elements"]["p"]["task_ids"] == ["B", "G"]
    assert linked["elements"]["p"]["install_window_d"] == [0, 17]
    assert linked["elements"]["a"]["install_window_d"] is None
    assert state_at(schedule, -1)["elements"]["p"] == "planned"
    assert state_at(schedule, 0)["elements"]["p"] == "in-progress"
    assert state_at(schedule, 7)["elements"]["p"] == "in-progress"  # gap, not complete
    assert state_at(schedule, 17)["elements"]["p"] == "complete"
    milestone = schedule_cpm([{"id": "m", "duration_d": 0, "element_ids": ["p"]}])
    assert state_at(milestone, 0)["complete"] == ["p"]
    with pytest.raises(CivilError) as exc:
        link_4d(schedule, {"structures": []})
    assert exc.value.reason == "not_found"


def test_requirement_evidence_preserves_failed_checks_and_trace_round_trip():
    row = {"check": "minimum_cover", "element_id": "p", "value": 0.7, "limit": 0.9,
           "units": "m", "pass": False, "criterion": "min_cover_m", "reference": "User design basis §3", "study": "civil_site/site"}
    result = requirement_evidence([row], {"minimum_cover": ["REQ-1", "REQ-2"]})
    evidence = result["evidence_by_requirement"]["REQ-1"]
    assert evidence == [{"source": "civil", "check": "minimum_cover", "element_id": "p",
                         "value": 0.7, "limit": 0.9, "units": "m", "pass": False,
                         "reference": "User design basis §3", "study": "civil_site/site"}]
    assert Trace.from_dict(Trace(features=["p"], study=row["study"], evidence=evidence).to_dict()).evidence == evidence
    assert requirement_evidence([row], {})["unmapped"] == [row]
    with pytest.raises(CivilError) as exc:
        requirement_evidence([{**row, "pass": "false"}], {"minimum_cover": "REQ-1"})
    assert exc.value.reason == "invalid_check_row"


def test_pavement_lateral_intersection_and_sloped_top_width():
    # Centerline y=0 misses pavement y=.1..1, but vertical strip y=-.5...5
    # overlaps pavement for 5 m length × .4 m width => 2 m² restoration.
    polygon = {"id": "road", "polygon": [[2, 0.1], [7, 0.1], [7, 1], [2, 1]]}
    qto = quantity_takeoff(single_pipe(), trench=trench_options(paved_polygons=[polygon]))
    assert quantity(qto, "pavement_restoration") == pytest.approx(2)
    # 1H:1V top width = 1 + 2*2.2 = 5.4 m; entire 10 m strip paved.
    wide = {"id": "wide", "polygon": [[-1, -10], [11, -10], [11, 10], [-1, 10]]}
    qto = quantity_takeoff(single_pipe(), trench=trench_options(side_slope=1, paved_polygons=[wide]))
    assert quantity(qto, "pavement_restoration") == pytest.approx(54)


def test_structure_groups_and_ambiguous_four_d_refusal():
    model = {"structures": [{"id": "s1", "kind": "culvert", "diameter": 0.6},
                            {"id": "s2", "kind": "culvert", "diameter": 0.6}]}
    rows = quantity_takeoff(model)["items"]
    assert len(rows) == 1
    assert rows[0]["quantity"] == 2
    assert rows[0]["element_ids"] == ["s1", "s2"]
    ambiguous = {"structures": [{"id": "p", "kind": "culvert"}],
                 "surfaces": [{"id": "p"}]}
    with pytest.raises(CivilError) as exc:
        link_4d(schedule_cpm([{"id": "t", "duration_d": 1, "element_ids": ["p"]}]), ambiguous)
    assert exc.value.reason == "ambiguous_element_id"
