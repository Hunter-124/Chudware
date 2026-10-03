"""Plate layout rules a sequential ("Complete individual objects") print depends on."""
from __future__ import annotations

import pytest

from .plate import PlateItem, PlateRefusal, footprint_hull, hull_distance, plan_plate

_ENDER3_BED = [(3, 3), (228, 3), (228, 228), (3, 228)]


def _box(key, width, depth, height, part=None):
    hull = footprint_hull([(-width / 2, -depth / 2), (width / 2, -depth / 2),
                           (width / 2, depth / 2), (-width / 2, depth / 2)])
    return PlateItem(key, part or key, 1, hull, height)


def _placed(plan, items):
    hulls = {i.key: i.hull for i in items}
    return {o["object"]: [(x + o["offset_mm"][0], y + o["offset_mm"][1])
                          for x, y in hulls[o["object"]]] for o in plan["objects"]}


def test_sequential_spacing_clears_the_hotend_and_the_tall_object_prints_last():
    items = [_box("tall", 12, 12, 30), _box("low", 20, 20, 6), _box("mid", 30, 10, 12)]
    plan = plan_plate(items, bed_polygon=_ENDER3_BED, sequential=True,
                      clearance_radius_mm=55, clearance_height_mm=25, brim_mm=3)
    assert [o["object"] for o in plan["objects"]] == ["low", "mid", "tall"]
    hulls = _placed(plan, items)
    keys = list(hulls)
    gaps = [hull_distance(hulls[a], hulls[b]) for i, a in enumerate(keys) for b in keys[i + 1:]]
    assert min(gaps) >= 55 + 2 * 3
    assert all(3 <= x <= 228 and 3 <= y <= 228 for hull in hulls.values() for x, y in hull)


def test_more_than_one_object_above_the_gantry_is_refused_by_name():
    items = [_box("pin_a", 10, 10, 30), _box("pin_b", 10, 10, 26), _box("low", 10, 10, 5)]
    with pytest.raises(PlateRefusal) as caught:
        plan_plate(items, bed_polygon=_ENDER3_BED, sequential=True,
                   clearance_radius_mm=55, clearance_height_mm=25)
    assert caught.value.code == "too_tall_for_sequential"
    assert {row["object"] for row in caught.value.details["objects"]} == {"pin_a", "pin_b"}
    # Layer by layer has no gantry constraint.
    plan = plan_plate(items, bed_polygon=_ENDER3_BED, sequential=False)
    assert len(plan["objects"]) == 3


def test_sequential_capacity_boundary_names_what_does_not_fit():
    # 30 mm squares at 57 mm pitch: three per row on the Ender-3 bed, so nine fit.
    nine = [_box(f"b{i}", 30, 30, 5) for i in range(9)]
    plan = plan_plate(nine, bed_polygon=_ENDER3_BED, sequential=True,
                      clearance_radius_mm=55, clearance_height_mm=25)
    assert len(plan["objects"]) == 9
    with pytest.raises(PlateRefusal) as caught:
        plan_plate(nine + [_box("b9", 30, 30, 5)], bed_polygon=_ENDER3_BED, sequential=True,
                   clearance_radius_mm=55, clearance_height_mm=25)
    assert caught.value.code == "plate_does_not_fit"
    assert len(caught.value.details["unplaced"]) == 1


def test_sequential_without_printer_clearances_is_refused():
    with pytest.raises(PlateRefusal) as caught:
        plan_plate([_box("a", 10, 10, 5)], bed_polygon=_ENDER3_BED, sequential=True,
                   clearance_radius_mm=None, clearance_height_mm=25)
    assert caught.value.code == "clearance_unknown"
