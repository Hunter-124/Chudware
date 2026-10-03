"""Deterministic layout of several objects on one build plate for a single slicer job.

Two print modes share this layout:

* all objects together, layer by layer (PrusaSlicer's default), spaced by the usual
  arrange distance plus the brim;
* PrusaSlicer's "Complete individual objects" (``complete_objects = 1``): each object is
  printed top to bottom before the nozzle moves to the next one.

Sequential printing is only safe when the hotend and the gantry never meet a finished
object, which the printer preset describes with ``extruder_clearance_radius`` (the zone
around the nozzle) and ``extruder_clearance_height`` (nozzle tip to the X gantry). Stock
PrusaSlicer 2.9.6 does not check either from the CLI: with ``complete_objects = 1`` it
exported G-code for objects 10 mm apart on the Ender-3's 55 mm clearance, and printed a
30 mm object before others under its 25 mm gantry clearance (probed 2026-10-03). So the
layout is decided here, and ``gcode_check`` verifies the toolpath it produced:

* hull gaps of at least the clearance radius (plus both brims and a margin);
* at most one object taller than the clearance height, and it prints last;
* print order is ascending height, so a travel between objects never runs below the top
  of anything already printed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

# Extra gap beyond the clearance radius: wipes and retract moves leave the hull slightly.
SEQUENTIAL_MARGIN_MM = 2.0
# PrusaSlicer's own arrange distance for layer-by-layer plates.
MIN_GAP_MM = 6.0
_EPS = 1e-6


@dataclass(frozen=True)
class PlateItem:
    """One object instance: its footprint hull in the bed plane (any origin) and height."""

    key: str
    part: str
    copy: int
    hull: tuple[tuple[float, float], ...]
    height_mm: float


class PlateRefusal(Exception):
    """The plate cannot be laid out safely; ``details`` name the objects responsible."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"ok": False, "reason": self.code, "content": self.message, **self.details}


def footprint_hull(xy: Any) -> tuple[tuple[float, float], ...]:
    """Convex hull of bed-plane points, counter-clockwise (degenerate inputs keep their bbox)."""
    points = np.unique(np.round(np.asarray(xy, dtype=np.float64).reshape(-1, 2), 4), axis=0)
    if len(points) >= 3:
        try:
            from scipy.spatial import ConvexHull  # noqa: PLC0415

            hull = ConvexHull(points)
            return tuple((float(x), float(y)) for x, y in points[hull.vertices])
        except Exception:  # noqa: BLE001 — collinear footprint: fall back to its bbox
            pass
    low, high = points.min(axis=0), points.max(axis=0)
    return ((float(low[0]), float(low[1])), (float(high[0]), float(low[1])),
            (float(high[0]), float(high[1])), (float(low[0]), float(high[1])))


def _point_segment(p, a, b) -> float:
    ax, ay = b[0] - a[0], b[1] - a[1]
    length = ax * ax + ay * ay
    t = 0.0 if length <= 0 else max(0.0, min(1.0, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / length))
    return math.hypot(p[0] - a[0] - t * ax, p[1] - a[1] - t * ay)


def _inside(p, polygon) -> bool:
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        if _point_segment(p, a, b) < 1e-6:
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            if p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]:
                inside = not inside
    return inside


def hull_distance(a: Sequence, b: Sequence) -> float:
    """Distance between two convex polygons (0 when they touch or overlap)."""
    a, b = list(a), list(b)
    if any(_inside(p, b) for p in a) or any(_inside(p, a) for p in b):
        return 0.0
    best = math.inf
    for first, second in ((a, b), (b, a)):
        for p in first:
            for s, e in zip(second, second[1:] + second[:1]):
                best = min(best, _point_segment(p, s, e))
    return best


def _shelf(rects: dict, order: list, usable: tuple, gap: float) -> tuple[dict, list]:
    """Rows along X: {key: (x_min, y_min)} for the bbox corner, plus keys that did not fit."""
    x0, y0, x1, y1 = usable
    placed, unplaced = {}, []
    x, y, row_depth = x0, y0, 0.0
    for key in order:
        width, depth = rects[key]
        if width > x1 - x0 + _EPS or depth > y1 - y0 + _EPS:
            unplaced.append(key)
            continue
        if x + width > x1 + _EPS:
            x, y, row_depth = x0, y + row_depth + gap, 0.0
        if y + depth > y1 + _EPS:
            unplaced.append(key)
            continue
        placed[key] = (x, y)
        x += width + gap
        row_depth = max(row_depth, depth)
    return placed, unplaced


def plan_plate(items: Sequence[PlateItem], *, bed_polygon: Sequence, sequential: bool,
               clearance_radius_mm: Optional[float] = None,
               clearance_height_mm: Optional[float] = None,
               brim_mm: float = 0.0, skirt_mm: float = 0.0) -> dict:
    """Place every item on the bed, or raise PlateRefusal naming what cannot go.

    Returns objects in PRINT ORDER with ``offset_mm`` — the XY translation that moves the
    item's hull (as given) to its plate position — plus the gaps and clearances used."""
    items = list(items)
    if not items:
        raise PlateRefusal("plate_empty", "No objects to place on the plate.")
    keys = [item.key for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError("plate item keys must be unique")
    brim_mm, skirt_mm = max(0.0, float(brim_mm)), max(0.0, float(skirt_mm))
    clearance = None
    if sequential:
        radius = clearance_radius_mm
        height = clearance_height_mm
        if radius is None or height is None or not radius > 0 or not height > 0:
            raise PlateRefusal(
                "clearance_unknown",
                "Complete individual objects needs the printer's extruder_clearance_radius and "
                "extruder_clearance_height, and this preset gives none; measure the hotend "
                "and pass both through overrides.",
                extruder_clearance_radius=radius, extruder_clearance_height=height)
        tall = [item for item in items if item.height_mm > height + _EPS]
        if len(tall) > 1:
            names = ", ".join(f"{item.key} ({item.height_mm:.1f} mm)" for item in tall)
            raise PlateRefusal(
                "too_tall_for_sequential",
                f"{names} are taller than the {height:g} mm extruder clearance height. With "
                f"Complete individual objects only ONE object may be taller, and it prints "
                f"last; otherwise the gantry hits it while the next object prints. Put the "
                f"tall objects on separate plates, reorient them lower, or print this plate "
                f"layer by layer.",
                objects=[{"object": item.key, "part": item.part, "height_mm": round(item.height_mm, 3)}
                         for item in tall],
                extruder_clearance_height_mm=height)
        gap = radius + 2.0 * brim_mm + SEQUENTIAL_MARGIN_MM
        clearance = {"extruder_clearance_radius_mm": radius,
                     "extruder_clearance_height_mm": height}
    else:
        gap = max(MIN_GAP_MM, 2.0 * brim_mm + 3.0)
    edge = brim_mm + skirt_mm + 1.0
    polygon = [(float(p[0]), float(p[1])) for p in bed_polygon]
    if len(polygon) < 3:
        raise ValueError("bed polygon needs at least three points")
    bx0, by0 = min(p[0] for p in polygon), min(p[1] for p in polygon)
    bx1, by1 = max(p[0] for p in polygon), max(p[1] for p in polygon)
    usable = (bx0 + edge, by0 + edge, bx1 - edge, by1 - edge)
    boxes = {}
    for item in items:
        xs = [p[0] for p in item.hull]
        ys = [p[1] for p in item.hull]
        boxes[item.key] = (min(xs), min(ys), max(xs), max(ys))
    rects = {k: (b[2] - b[0], b[3] - b[1]) for k, b in boxes.items()}

    best = None
    sorts = (lambda k: (-rects[k][1], -rects[k][0]), lambda k: (-rects[k][0], -rects[k][1]),
             lambda k: -rects[k][0] * rects[k][1], None)
    for axis in (0, 1):
        swap = (lambda r: (r[1], r[0])) if axis else (lambda r: r)
        shelf_rects = {k: swap(r) for k, r in rects.items()}
        shelf_usable = usable if not axis else (usable[1], usable[0], usable[3], usable[2])
        for sort in sorts:
            order = sorted(keys, key=sort) if sort else list(keys)
            placed, unplaced = _shelf(shelf_rects, order, shelf_usable, gap)
            if axis:
                placed = {k: (v[1], v[0]) for k, v in placed.items()}
            if placed:
                gx0 = min(v[0] for v in placed.values())
                gy0 = min(v[1] for v in placed.values())
                gx1 = max(v[0] + rects[k][0] for k, v in placed.items())
                gy1 = max(v[1] + rects[k][1] for k, v in placed.items())
                area = (gx1 - gx0) * (gy1 - gy0)
            else:
                area = math.inf
            rank = (len(unplaced), area)
            if best is None or rank < best[0]:
                best = (rank, placed, unplaced)
    _, placed, unplaced = best
    gap_note = (f"{gap:g} mm between objects (extruder clearance radius "
                f"{clearance_radius_mm:g} mm + brims + {SEQUENTIAL_MARGIN_MM:g} mm)"
                if sequential else f"{gap:g} mm between objects")
    if unplaced:
        raise PlateRefusal(
            "plate_does_not_fit",
            f"{len(unplaced)} of {len(items)} objects do not fit on the bed with {gap_note}: "
            f"{', '.join(unplaced)}. Move them to another plate"
            + (" or print layer by layer (smaller spacing)" if sequential else "") + ".",
            placed=sorted(placed), unplaced=unplaced, gap_mm=round(gap, 3),
            usable_rect_mm=[round(v, 3) for v in usable])

    gx0 = min(v[0] for v in placed.values())
    gy0 = min(v[1] for v in placed.values())
    gx1 = max(v[0] + rects[k][0] for k, v in placed.items())
    gy1 = max(v[1] + rects[k][1] for k, v in placed.items())
    shift = ((usable[0] + usable[2]) / 2 - (gx0 + gx1) / 2,
             (usable[1] + usable[3]) / 2 - (gy0 + gy1) / 2)
    by_key = {item.key: item for item in items}
    hulls, offsets = {}, {}
    for key, (x, y) in placed.items():
        box = boxes[key]
        offsets[key] = (x + shift[0] - box[0], y + shift[1] - box[1])
        hulls[key] = [(p[0] + offsets[key][0], p[1] + offsets[key][1]) for p in by_key[key].hull]
        # A non-rectangular bed (delta, chamfered corners) may not contain the bbox slot.
        outside = [p for p in hulls[key] if not _inside(p, polygon)]
        if outside:
            raise PlateRefusal(
                "plate_does_not_fit",
                f"{key} would overhang the bed outline at {outside[0][0]:.1f}, "
                f"{outside[0][1]:.1f} mm; move objects to another plate.",
                placed=sorted(placed), unplaced=[key], gap_mm=round(gap, 3))
    index = {key: i for i, key in enumerate(keys)}
    order = sorted(keys, key=lambda k: (by_key[k].height_mm, index[k])) if sequential else keys
    pair_gaps = [hull_distance(hulls[a], hulls[b])
                 for i, a in enumerate(keys) for b in keys[i + 1:]]
    objects = []
    for position, key in enumerate(order, 1):
        item = by_key[key]
        xs = [p[0] for p in hulls[key]]
        ys = [p[1] for p in hulls[key]]
        objects.append({
            "object": key, "part": item.part, "copy": item.copy, "print_order": position,
            "offset_mm": [round(offsets[key][0], 4), round(offsets[key][1], 4)],
            "bbox_mm": [round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3)],
            "height_mm": round(item.height_mm, 3),
            **({"above_clearance_height": item.height_mm > clearance_height_mm + _EPS}
               if sequential else {}),
        })
    return {
        "complete_individual_objects": bool(sequential),
        "objects": objects,
        "gap_mm": round(gap, 3),
        "min_object_gap_mm": round(min(pair_gaps), 3) if pair_gaps else None,
        "edge_margin_mm": round(edge, 3),
        "usable_rect_mm": [round(v, 3) for v in usable],
        "clearance": clearance,
    }
