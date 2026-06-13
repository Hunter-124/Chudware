"""Design-For-Manufacturing (DFM) check suite for AI-ECAD PCB boards.

``run_dfm(board, rules=None) -> [Violation]``

DFM is **advisory**: violations carry ``severity`` "warning" (or "error" for
clearly-fatal conditions) but never block an operation and never raise. The
output is deterministic (sorted). The board document is never mutated.

Reuses geometry helpers from :mod:`engine.ecad.drc` (``seg_seg_dist``,
``_point_seg_dist``, ``_segs_intersect``, ``_dist``, ``_seg_box_dist``,
``_point_box_dist``, ``_box_box_dist``, ``edge_distance``, ``_Shape``
internals) and world-position helper :func:`engine.ecad.schema.pad_world`.

------------------------------------------------------------------------------
Rule catalogue (default thresholds, all overridable via ``rules`` dict):
------------------------------------------------------------------------------

silk_over_pad
    A silkscreen graphic line or text on F.SilkS / B.SilkS overlaps the
    exposed copper area of a pad on the same side (pad has no soldermask on
    that side). Silk ink printed directly on solder land prevents good
    soldering. Default threshold: 0 mm overlap (any overlap fires).
    Geometry: pad modelled as a circle of radius = max(w,h)/2 (round/oval) or
    a rotated-AABB box (rect/roundrect/etc.); silk line modelled as a zero-
    width segment (the stroke width check is separate via silk_min_width).

silk_min_width
    Silk line width below the fab minimum -> ink bleeds / smears.
    Default: 0.15 mm.

silk_min_text
    Silk text height (``size[1]`` or ``size`` scalar) below the fab minimum.
    Default: 0.8 mm (common fab house spec for legible silk text).

mask_sliver
    Soldermask web between two adjacent pads on the same copper layer narrower
    than the mask-sliver minimum -> sliver of mask between pads flakes off,
    causing shorts. Fires when EITHER pad has a mask layer (F.Mask / B.Mask)
    and the two pads are on the same copper layer (and on different nets, or
    either net is null -- same-net adjacent pads are less critical but still
    flagged as fab risk).
    Geometry: pad copper extents same as DRC (circle or AABB box). The mask
    opening is taken as the pad copper extent with zero additional expansion
    (soldermask expansion is a fab parameter; we conservatively assume no
    expansion, i.e. the opening equals the copper pad).
    Default sliver minimum: 0.1 mm.

acid_trap / acute_angle
    Two copper track segments of the **same net** meeting at an acute angle
    (angle between the outgoing directions < threshold) create a sharp inside
    corner where etchant is trapped, under-etching the inner corner.
    Measured as the angle between the two segments at their shared endpoint
    (the smaller of the two angles at the vertex). Fires when angle < threshold.
    Arc tracks are skipped (documented).
    Default threshold: 90 deg (right angle or sharper is a trap risk).

courtyard_overlap
    Two footprints' courtyard outlines (F.CrtYd / B.CrtYd graphics) overlap.
    Overlapping courtyards indicate that two components would physically
    collide at assembly. Courtyard graphics in the footprint's ``graphics``
    list that are ``type`` "line" or "rect" are walked as polygon segments;
    overlap is detected by segment intersection or one polygon containing the
    other.
    Geometry: courtyard lines converted to world coords (footprint pos + angle
    rotation; bottom-side footprints mirrored in X). Overlap tested via
    segment-segment intersection; containment via point-in-polygon.

annular_min (DFM grade)
    Via or tht-pad annular ring narrower than the DFM-grade fab minimum.
    The DFM minimum is tighter than the default design rule minimum to match
    real fab capability (many fabs require >= 0.15 mm ring even if the design
    rule is 0.13 mm).
    Default: 0.15 mm.

drill_min (DFM grade)
    Drilled hole (via or tht pad) smaller than the DFM-grade fab minimum.
    Many standard fabs cannot reliably drill < 0.2 mm; high-density fabs
    may manage 0.15 mm. Default: 0.2 mm (via drill) and 0.15 mm (mechanical
    pad drill, which tends to be larger in practice).
    Fires a "warning" at or below the default; upgrade to "error" when below
    0.1 mm (micro-drill territory).

------------------------------------------------------------------------------
Geometry approximations:
------------------------------------------------------------------------------
* Pad copper extent: circle of radius max(w,h)/2 for round/oval pads; rotated
  AABB (half-extents computed by ``_rotated_aabb_half`` from drc.py) for rect/
  roundrect/trapezoid/custom. Same convention as DRC clearance shapes.
* Silk line: treated as a zero-width line segment between its endpoints (the
  silk_min_width rule catches the stroke width separately).
* Courtyard polygon: footprint graphics of type "line" are converted to world
  segment pairs. Each "rect" graphic (p1/p2 corners) is expanded to 4 edges.
  Circles are approximated as 24-gon polygons. Point-in-polygon uses the
  ray-casting test on world coordinates.
* Acid-trap angle: computed from the two outgoing direction vectors at the
  shared vertex. Only segment tracks are examined; arc tracks are skipped.
"""

from __future__ import annotations

import math
from typing import Any

from engine.ecad import schema
from engine.ecad.drc import (
    _dist, _point_seg_dist, _segs_intersect, seg_seg_dist,
    _point_box_dist, _seg_box_dist, _box_box_dist,
    _rotated_aabb_half, same_net,
)

# ── numerical tolerance (same as drc.py) ────────────────────────────────────
EPS = 1e-9

# ── Violation (re-use the same dict shape as drc.py) ────────────────────────
Violation = dict


def _v(rule: str, severity: str, locs: list, items: list, message: str) -> Violation:
    return {
        "rule": rule,
        "severity": severity,
        "locs": [[float(x), float(y), str(layer)] for (x, y, layer) in locs],
        "items": sorted(str(i) for i in items),
        "message": message,
    }


# ── Default DFM thresholds ────────────────────────────────────────────────────
_DEFAULTS: dict[str, Any] = {
    # silk_over_pad: pad copper must not be overlapped by silk graphics (0 = any touch fires)
    "silk_over_pad_min_gap": 0.0,
    # silk_min_width: silk line width minimum (mm)
    "silk_min_width": 0.15,
    # silk_min_text: silk text height minimum (mm)
    "silk_min_text": 0.8,
    # mask_sliver: min soldermask web between adjacent pad openings (mm)
    "mask_sliver_min": 0.1,
    # acid_trap: acute angle threshold (deg) -- angle <= this fires
    "acid_trap_angle_max": 90.0,
    # courtyard_overlap: any overlap fires (no numeric threshold, just presence)
    # annular_min: DFM-grade annular ring minimum (mm) -- tighter than DRC default 0.13
    "annular_min": 0.15,
    # drill_min: DFM-grade drill diameter minimum for vias (mm)
    "drill_min_via": 0.2,
    # drill_min: DFM-grade drill diameter minimum for mechanical pad holes (mm)
    "drill_min_pad": 0.15,
    # drill_error: below this drill diameter it is an error not just a warning (mm)
    "drill_error_below": 0.1,
}


def _cfg(rules: dict | None, key: str) -> Any:
    """Return the override value from ``rules`` if present, else the DFM default."""
    return rules[key] if (rules and key in rules) else _DEFAULTS[key]


# ── Pad copper geometry helpers ───────────────────────────────────────────────
# These mirror the _pad_shape logic in drc.py but we want (center, kind, margin/half)
# as a plain tuple for use outside _Shape.

def _pad_copper(fp: dict, pad: dict) -> tuple[str, tuple, Any, str]:
    """Return ``(kind, center_world, margin_or_half, silk_side_layer)`` for a pad.

    kind: "circle" -> margin = radius float
          "box"    -> margin = (hx, hy) tuple
    silk_side_layer: the silk layer on the same side as this pad ("F.SilkS" or "B.SilkS").
    """
    wx, wy = schema.pad_world(fp, pad)
    size = pad.get("size") or [0.0, 0.0]
    w = float(size[0]) if len(size) > 0 else 0.0
    h = float(size[1]) if len(size) > 1 else w
    side = fp.get("side", "top")
    silk_layer = "F.SilkS" if side == "top" else "B.SilkS"
    shape = pad.get("shape", "rect")
    if shape in ("circle", "oval"):
        return ("circle", (wx, wy), max(w, h) / 2.0, silk_layer)
    pad_angle = float(pad.get("angle", 0.0)) + float(fp.get("angle", 0.0))
    hx, hy = _rotated_aabb_half(w, h, pad_angle)
    return ("box", (wx, wy), (hx, hy), silk_layer)


def _pad_has_mask_opening(pad: dict, side: str) -> bool:
    """True if this pad has an exposed copper area (soldermask opening) on ``side``."""
    mask_layer = "F.Mask" if side == "top" else "B.Mask"
    # A pad has a mask opening when the mask layer is in its layers list.
    return mask_layer in pad.get("layers", [])


def _pad_copper_layers(pad: dict) -> set[str]:
    """Copper layers this pad sits on (for mask_sliver same-layer test)."""
    cu = {"F.Cu", "B.Cu"}
    for lyr in schema.TECH_LAYERS:
        cu.discard(lyr)
    # Include In*.Cu layers from the pad's layer list too.
    return {lyr for lyr in pad.get("layers", []) if lyr.endswith(".Cu")}


# ── point-in-polygon (ray-cast) ───────────────────────────────────────────────

def _point_in_polygon(px: float, py: float, poly: list[tuple]) -> bool:
    """Ray-casting test: True if (px,py) is inside the polygon defined by ``poly``
    (list of (x,y) vertices, closed implicitly)."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + EPS) + xi):
            inside = not inside
        j = i
    return inside


# ── courtyard polygon extraction ──────────────────────────────────────────────

def _world_rotate(x: float, y: float, cx: float, cy: float,
                  angle_deg: float, mirror_x: bool) -> tuple[float, float]:
    """Rotate point (x,y) around origin, optionally mirror in X, then translate by (cx,cy)."""
    if mirror_x:
        x = -x
    r = math.radians(angle_deg)
    rx = x * math.cos(r) - y * math.sin(r)
    ry = x * math.sin(r) + y * math.cos(r)
    return (rx + cx, ry + cy)


def _courtyard_segments(fp: dict, side_filter: str) -> list[tuple]:
    """Return world-coordinate line segments for the courtyard of ``fp`` on ``side_filter``.

    ``side_filter`` is "F.CrtYd" or "B.CrtYd". Handles line, rect, and circle graphic types.
    Returns list of ((x1,y1),(x2,y2)) pairs in world coordinates.
    """
    cx, cy = float(fp["pos"][0]), float(fp["pos"][1])
    angle = float(fp.get("angle", 0.0))
    mirror_x = fp.get("side", "top") == "bottom"

    def _w(x, y):
        return _world_rotate(x, y, cx, cy, angle, mirror_x)

    segs = []
    for g in fp.get("graphics", []):
        if g.get("layer") != side_filter:
            continue
        gtype = g.get("type", "line")
        if gtype == "line":
            p1 = g.get("p1", [0, 0])
            p2 = g.get("p2", [0, 0])
            segs.append((_w(float(p1[0]), float(p1[1])), _w(float(p2[0]), float(p2[1]))))
        elif gtype == "rect":
            # rect graphic: p1 and p2 are opposite corners
            p1 = g.get("p1", [0, 0])
            p2 = g.get("p2", [0, 0])
            x0, y0 = float(p1[0]), float(p1[1])
            x1, y1 = float(p2[0]), float(p2[1])
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            wc = [_w(px, py) for (px, py) in corners]
            for i in range(4):
                segs.append((wc[i], wc[(i + 1) % 4]))
        elif gtype == "circle":
            center = g.get("center", [0, 0])
            r = float(g.get("radius", 0.0))
            N = 24
            pts = []
            for i in range(N):
                a = 2.0 * math.pi * i / N
                pts.append(_w(float(center[0]) + r * math.cos(a),
                              float(center[1]) + r * math.sin(a)))
            for i in range(N):
                segs.append((pts[i], pts[(i + 1) % N]))
    return segs


def _courtyard_polygon(fp: dict, side_filter: str) -> list[tuple]:
    """Extract the (open) polygon of courtyard vertices for point-in-polygon tests.

    We take the segment list and build an ordered vertex list via an O(n^2) greedy walk.
    If we cannot close the loop cleanly we return whatever vertices we have (graceful degradation).
    """
    segs = _courtyard_segments(fp, side_filter)
    if not segs:
        return []
    # Build vertex list by chaining segment endpoints
    used = [False] * len(segs)
    poly = [segs[0][0], segs[0][1]]
    used[0] = True
    for _ in range(len(segs) - 1):
        tail = poly[-1]
        best_idx, best_rev = -1, False
        best_d = math.inf
        for k, seg in enumerate(segs):
            if used[k]:
                continue
            d0 = _dist(tail, seg[0])
            d1 = _dist(tail, seg[1])
            if d0 < best_d:
                best_d, best_idx, best_rev = d0, k, False
            if d1 < best_d:
                best_d, best_idx, best_rev = d1, k, True
        if best_idx < 0:
            break
        used[best_idx] = True
        seg = segs[best_idx]
        poly.append(seg[1] if not best_rev else seg[0])
    return poly


# ── Rule: silk_over_pad ───────────────────────────────────────────────────────

def _check_silk_over_pad(board: dict, rules: dict | None, out: list) -> None:
    """Silk line / text graphic overlapping an exposed pad copper area."""
    gap_min = float(_cfg(rules, "silk_over_pad_min_gap"))
    # Collect all silk graphics (board-level + footprint-level)
    silk_items: list[dict] = []
    for g in board.get("graphics", []):
        if g.get("layer") in ("F.SilkS", "B.SilkS"):
            silk_items.append({"g": g, "fp": None})
    for fp in board.get("footprints", []):
        for g in fp.get("graphics", []):
            if g.get("layer") in ("F.SilkS", "B.SilkS"):
                silk_items.append({"g": g, "fp": fp})

    # Collect all pads with mask openings grouped by silk layer
    pad_entries: list[dict] = []
    for fp in board.get("footprints", []):
        side = fp.get("side", "top")
        silk_layer = "F.SilkS" if side == "top" else "B.SilkS"
        for pad in fp.get("pads", []):
            if not _pad_has_mask_opening(pad, side):
                continue
            kind, center, margin_or_half, _ = _pad_copper(fp, pad)
            pad_entries.append({
                "pad": pad,
                "fp": fp,
                "silk_layer": silk_layer,
                "kind": kind,
                "center": center,
                "margin": margin_or_half,  # float (circle) or (hx,hy) (box)
            })

    if not pad_entries:
        return

    for silk_item in silk_items:
        g = silk_item["g"]
        g_layer = g.get("layer")
        gtype = g.get("type", "line")
        g_id = g.get("id", "")
        # Footprint-local silk: transform to world coords
        fp_host = silk_item["fp"]

        def _silk_seg_world(p1_local, p2_local):
            if fp_host is None:
                return (
                    (float(p1_local[0]), float(p1_local[1])),
                    (float(p2_local[0]), float(p2_local[1])),
                )
            cx, cy = float(fp_host["pos"][0]), float(fp_host["pos"][1])
            angle = float(fp_host.get("angle", 0.0))
            mirror_x = fp_host.get("side", "top") == "bottom"
            return (
                _world_rotate(float(p1_local[0]), float(p1_local[1]), cx, cy, angle, mirror_x),
                _world_rotate(float(p2_local[0]), float(p2_local[1]), cx, cy, angle, mirror_x),
            )

        # Build list of world segments for this graphic
        w_segs: list[tuple] = []
        silk_center_world = None
        if gtype == "line":
            p1 = g.get("p1", [0, 0])
            p2 = g.get("p2", [0, 0])
            w_segs.append(_silk_seg_world(p1, p2))
        elif gtype == "rect":
            p1 = g.get("p1", [0, 0])
            p2 = g.get("p2", [0, 0])
            corners_local = [
                (float(p1[0]), float(p1[1])),
                (float(p2[0]), float(p1[1])),
                (float(p2[0]), float(p2[1])),
                (float(p1[0]), float(p2[1])),
            ]
            wc = [_silk_seg_world(c, c)[0] for c in corners_local]
            for i in range(4):
                w_segs.append((wc[i], wc[(i + 1) % 4]))
        elif gtype in ("text", "reference", "value"):
            # Text: model as its anchor point and check if it falls inside a pad.
            pos = g.get("pos", [0, 0])
            w_pos = _silk_seg_world(pos, pos)[0]
            silk_center_world = w_pos
            # Treat as a zero-length degenerate segment so the distance check works
            w_segs.append((w_pos, w_pos))
        elif gtype == "circle":
            center = g.get("center", [0, 0])
            r = float(g.get("radius", 0.0))
            N = 24
            pts_local = [
                (float(center[0]) + r * math.cos(2 * math.pi * i / N),
                 float(center[1]) + r * math.sin(2 * math.pi * i / N))
                for i in range(N)
            ]
            pts_w = [_silk_seg_world(p, p)[0] for p in pts_local]
            for i in range(N):
                w_segs.append((pts_w[i], pts_w[(i + 1) % N]))

        if not w_segs:
            continue

        for pe in pad_entries:
            if pe["silk_layer"] != g_layer:
                continue
            # Measure distance from each silk segment to the pad copper extent
            pad_id = pe["pad"].get("id", "")
            if pe["kind"] == "circle":
                r = float(pe["margin"])
                cx_p, cy_p = pe["center"]
                for (sa, sb) in w_segs:
                    d = _point_seg_dist(pe["center"], sa, sb)
                    gap = d - r  # edge-to-edge gap (negative means overlap)
                    if gap < gap_min + EPS:
                        out.append(_v(
                            "silk_over_pad", "warning",
                            [(cx_p, cy_p, g_layer)],
                            [g_id, pad_id],
                            f"silk graphic {g_id!r} on {g_layer} overlaps exposed pad "
                            f"{pad_id!r} (gap {gap:.4g}mm)"))
                        break
            else:
                hx, hy = pe["margin"]
                cx_p, cy_p = pe["center"]
                for (sa, sb) in w_segs:
                    d = _seg_box_dist(sa, sb, cx_p, cy_p, hx, hy)
                    if d < gap_min + EPS:
                        out.append(_v(
                            "silk_over_pad", "warning",
                            [(cx_p, cy_p, g_layer)],
                            [g_id, pad_id],
                            f"silk graphic {g_id!r} on {g_layer} overlaps exposed pad "
                            f"{pad_id!r} (gap {d:.4g}mm)"))
                        break


# ── Rule: silk_min_width ──────────────────────────────────────────────────────

def _check_silk_min_width(board: dict, rules: dict | None, out: list) -> None:
    """Silk line stroke width below fab minimum."""
    min_w = float(_cfg(rules, "silk_min_width"))
    all_graphics = list(board.get("graphics", []))
    for fp in board.get("footprints", []):
        all_graphics.extend(fp.get("graphics", []))
    for g in all_graphics:
        if g.get("layer") not in ("F.SilkS", "B.SilkS"):
            continue
        if g.get("type") not in ("line", "rect", "circle", "arc"):
            continue
        w = float(g.get("width", g.get("stroke_width", 0.0)) or 0.0)
        if w < min_w - EPS:
            pos = g.get("p1") or g.get("center") or g.get("pos") or [0, 0]
            layer = g.get("layer", "F.SilkS")
            out.append(_v(
                "silk_min_width", "warning",
                [(float(pos[0]), float(pos[1]), layer)],
                [g.get("id", "")],
                f"silk graphic {g.get('id', '')!r} stroke width {w:.4g}mm < "
                f"fab minimum {min_w:.4g}mm"))


# ── Rule: silk_min_text ───────────────────────────────────────────────────────

def _check_silk_min_text(board: dict, rules: dict | None, out: list) -> None:
    """Silk text height below fab minimum."""
    min_h = float(_cfg(rules, "silk_min_text"))
    all_graphics = list(board.get("graphics", []))
    for fp in board.get("footprints", []):
        all_graphics.extend(fp.get("graphics", []))
    for g in all_graphics:
        if g.get("layer") not in ("F.SilkS", "B.SilkS"):
            continue
        if g.get("type") not in ("text", "reference", "value"):
            continue
        sz = g.get("size", g.get("height", 0.0))
        if isinstance(sz, (list, tuple)):
            h = float(sz[1]) if len(sz) > 1 else float(sz[0])
        else:
            h = float(sz or 0.0)
        if h < min_h - EPS:
            pos = g.get("pos", [0, 0])
            layer = g.get("layer", "F.SilkS")
            out.append(_v(
                "silk_min_text", "warning",
                [(float(pos[0]), float(pos[1]), layer)],
                [g.get("id", "")],
                f"silk text {g.get('id', '')!r} height {h:.4g}mm < "
                f"fab minimum {min_h:.4g}mm"))


# ── Rule: mask_sliver ─────────────────────────────────────────────────────────

def _check_mask_sliver(board: dict, rules: dict | None, out: list) -> None:
    """Soldermask web between two adjacent pad openings narrower than the minimum."""
    sliver_min = float(_cfg(rules, "mask_sliver_min"))

    # Build list of all pad copper extents that have a mask opening
    entries: list[dict] = []
    for fp in board.get("footprints", []):
        side = fp.get("side", "top")
        cu_layer = "F.Cu" if side == "top" else "B.Cu"
        mask_layer = "F.Mask" if side == "top" else "B.Mask"
        for pad in fp.get("pads", []):
            layers = pad.get("layers", [])
            if mask_layer not in layers:
                continue  # pad has no mask opening on this side
            kind, center, margin_or_half, _ = _pad_copper(fp, pad)
            entries.append({
                "pad": pad,
                "fp": fp,
                "kind": kind,
                "center": center,
                "margin": margin_or_half,
                "cu_layer": cu_layer,
                "net": pad.get("net"),
            })

    # O(n^2) pair check -- boards rarely have >1000 pads
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a, b = entries[i], entries[j]
            # Must be on the same copper layer to share soldermask
            if a["cu_layer"] != b["cu_layer"]:
                continue
            # Compute edge-to-edge gap between the two mask openings (= pad copper extents)
            # Using same pad-shape pair dispatch as drc.edge_distance:
            gap = _pad_pair_gap(a, b)
            if gap < sliver_min - EPS:
                layer = a["cu_layer"]
                cx_a, cy_a = a["center"]
                cx_b, cy_b = b["center"]
                out.append(_v(
                    "mask_sliver", "warning",
                    [(cx_a, cy_a, layer), (cx_b, cy_b, layer)],
                    [a["pad"].get("id", ""), b["pad"].get("id", "")],
                    f"soldermask sliver {gap:.4g}mm between pads "
                    f"{a['pad'].get('id', '')!r} and {b['pad'].get('id', '')!r} "
                    f"(< {sliver_min:.4g}mm minimum)"))


def _pad_pair_gap(a: dict, b: dict) -> float:
    """Edge-to-edge gap between two pad copper extents (circle or box)."""
    if a["kind"] == "circle" and b["kind"] == "circle":
        return max(_dist(a["center"], b["center"]) - float(a["margin"]) - float(b["margin"]), 0.0)
    if a["kind"] == "circle" and b["kind"] == "box":
        hx, hy = b["margin"]
        d = _point_box_dist(a["center"], b["center"][0], b["center"][1], hx, hy)
        return max(d - float(a["margin"]), 0.0)
    if a["kind"] == "box" and b["kind"] == "circle":
        return _pad_pair_gap(b, a)
    # box / box
    return _box_box_dist(a["center"], a["margin"], b["center"], b["margin"])


# ── Rule: acid_trap / acute_angle ─────────────────────────────────────────────

def _check_acid_trap(board: dict, rules: dict | None, out: list) -> None:
    """Two same-net track segments meeting at an acute angle -> etchant trap."""
    angle_max = float(_cfg(rules, "acid_trap_angle_max"))

    # Index segment tracks by their endpoints (rounded to 1 nm) for fast adjacency lookup.
    # Only segment tracks; arc tracks skipped (documented).
    seg_tracks = [t for t in board.get("tracks", [])
                  if t.get("kind", "segment") == "segment"]

    if not seg_tracks:
        return

    def _key(p):
        # Round to 1e-6 mm to absorb float noise in endpoint matching
        return (round(float(p[0]) * 1e6) / 1e6, round(float(p[1]) * 1e6) / 1e6)

    # Build endpoint -> list of (track, endpoint_role) for adjacency
    from collections import defaultdict
    ep_map: dict = defaultdict(list)
    for t in seg_tracks:
        k1 = _key(t["p1"])
        k2 = _key(t["p2"])
        ep_map[k1].append(t)
        ep_map[k2].append(t)

    seen_pairs: set = set()
    for ep_key, tracks_at_ep in ep_map.items():
        if len(tracks_at_ep) < 2:
            continue
        # Check all pairs of tracks sharing this endpoint
        for i in range(len(tracks_at_ep)):
            for j in range(i + 1, len(tracks_at_ep)):
                ta = tracks_at_ep[i]
                tb = tracks_at_ep[j]
                pair_key = tuple(sorted([ta.get("id", ""), tb.get("id", "")]) + [str(ep_key)])
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                if not same_net(ta.get("net"), tb.get("net")):
                    continue
                # Direction vectors pointing AWAY from the shared endpoint
                def _dir_from(t, ep):
                    k_p1 = _key(t["p1"])
                    if abs(k_p1[0] - ep[0]) < EPS and abs(k_p1[1] - ep[1]) < EPS:
                        # ep is p1, direction is toward p2
                        dx = float(t["p2"][0]) - float(t["p1"][0])
                        dy = float(t["p2"][1]) - float(t["p1"][1])
                    else:
                        # ep is p2, direction is toward p1
                        dx = float(t["p1"][0]) - float(t["p2"][0])
                        dy = float(t["p1"][1]) - float(t["p2"][1])
                    length = math.hypot(dx, dy)
                    if length < EPS:
                        return (0.0, 0.0)
                    return (dx / length, dy / length)

                da = _dir_from(ta, ep_key)
                db = _dir_from(tb, ep_key)
                if da == (0.0, 0.0) or db == (0.0, 0.0):
                    continue
                dot = max(-1.0, min(1.0, da[0] * db[0] + da[1] * db[1]))
                angle_deg = math.degrees(math.acos(dot))
                if angle_deg <= angle_max + EPS:
                    out.append(_v(
                        "acid_trap", "warning",
                        [(ep_key[0], ep_key[1], ta.get("layer", "F.Cu"))],
                        [ta.get("id", ""), tb.get("id", "")],
                        f"acid trap: tracks {ta.get('id', '')!r} and {tb.get('id', '')!r} "
                        f"meet at {angle_deg:.1f}deg on net {ta.get('net')!r} "
                        f"(<= {angle_max:.4g}deg threshold)"))


# ── Rule: courtyard_overlap ───────────────────────────────────────────────────

def _check_courtyard_overlap(board: dict, rules: dict | None, out: list) -> None:
    """Two footprints' courtyard outlines overlapping -> assembly collision."""
    fps = board.get("footprints", [])
    if len(fps) < 2:
        return

    def _cy_layer(fp):
        return "F.CrtYd" if fp.get("side", "top") == "top" else "B.CrtYd"

    # Build (segs, poly, layer) per footprint, skip empty courtyards
    cy_data = []
    for fp in fps:
        layer = _cy_layer(fp)
        segs = _courtyard_segments(fp, layer)
        if not segs:
            continue
        poly = _courtyard_polygon(fp, layer)
        cy_data.append({"fp": fp, "layer": layer, "segs": segs, "poly": poly})

    for i in range(len(cy_data)):
        for j in range(i + 1, len(cy_data)):
            ca = cy_data[i]
            cb = cy_data[j]
            if ca["layer"] != cb["layer"]:
                # Different sides cannot physically collide (unless stacked boards, not modelled)
                continue
            overlap = False
            # 1. Segment-segment intersection between the two courtyard outlines
            for sa in ca["segs"]:
                for sb in cb["segs"]:
                    if _segs_intersect(sa[0], sa[1], sb[0], sb[1]):
                        overlap = True
                        break
                if overlap:
                    break
            # 2. One polygon contains the other (segment intersection misses concentric polygons)
            if not overlap and ca["poly"] and cb["poly"]:
                # Test a vertex of each polygon against the other's polygon
                if _point_in_polygon(ca["poly"][0][0], ca["poly"][0][1], cb["poly"]):
                    overlap = True
                elif _point_in_polygon(cb["poly"][0][0], cb["poly"][0][1], ca["poly"]):
                    overlap = True
            if overlap:
                fp_a = ca["fp"]
                fp_b = cb["fp"]
                ax, ay = float(fp_a["pos"][0]), float(fp_a["pos"][1])
                bx, by = float(fp_b["pos"][0]), float(fp_b["pos"][1])
                layer = ca["layer"]
                out.append(_v(
                    "courtyard_overlap", "error",
                    [(ax, ay, layer), (bx, by, layer)],
                    [fp_a.get("id", ""), fp_b.get("id", "")],
                    f"courtyard overlap: footprints {fp_a.get('ref', fp_a.get('id', ''))!r} "
                    f"and {fp_b.get('ref', fp_b.get('id', ''))!r} courtyards intersect"))


# ── Rule: annular_min (DFM grade) ────────────────────────────────────────────

def _check_annular_min(board: dict, rules: dict | None, out: list) -> None:
    """Via / tht-pad annular ring below the DFM-grade minimum (tighter than DRC default)."""
    ar_min = float(_cfg(rules, "annular_min"))
    for v in board.get("vias", []):
        dia = float(v.get("diameter", 0.0))
        drill = float(v.get("drill", 0.0))
        ring = (dia - drill) / 2.0
        if ring < ar_min - EPS:
            at = v.get("at", [0, 0])
            out.append(_v(
                "annular_min", "warning",
                [(float(at[0]), float(at[1]), "F.Cu")],
                [v.get("id", "")],
                f"via {v.get('id', '')!r} annular ring {ring:.4g}mm < DFM minimum "
                f"{ar_min:.4g}mm"))
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            drill_info = pad.get("drill")
            if not drill_info:
                continue
            dia_d = drill_info.get("diameter") if isinstance(drill_info, dict) else drill_info
            if dia_d is None:
                continue
            size = pad.get("size") or [0.0, 0.0]
            pad_min_dim = min(float(size[0]), float(size[1])) if len(size) >= 2 else float(size[0])
            ring = (pad_min_dim - float(dia_d)) / 2.0
            if ring < ar_min - EPS:
                wx, wy = schema.pad_world(fp, pad)
                out.append(_v(
                    "annular_min", "warning",
                    [(wx, wy, "F.Cu")],
                    [pad.get("id", "")],
                    f"pad {pad.get('id', '')!r} annular ring {ring:.4g}mm < DFM minimum "
                    f"{ar_min:.4g}mm"))


# ── Rule: drill_min (DFM grade) ──────────────────────────────────────────────

def _check_drill_min(board: dict, rules: dict | None, out: list) -> None:
    """Drilled hole below the DFM-grade fab minimum drill diameter."""
    via_min = float(_cfg(rules, "drill_min_via"))
    pad_min = float(_cfg(rules, "drill_min_pad"))
    err_below = float(_cfg(rules, "drill_error_below"))

    for v in board.get("vias", []):
        d = float(v.get("drill", 0.0))
        if d < via_min - EPS:
            sev = "error" if d < err_below - EPS else "warning"
            at = v.get("at", [0, 0])
            out.append(_v(
                "drill_min", sev,
                [(float(at[0]), float(at[1]), "F.Cu")],
                [v.get("id", "")],
                f"via {v.get('id', '')!r} drill {d:.4g}mm < DFM minimum {via_min:.4g}mm"))

    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            drill_info = pad.get("drill")
            if not drill_info:
                continue
            dia = drill_info.get("diameter") if isinstance(drill_info, dict) else drill_info
            if dia is None:
                continue
            d = float(dia)
            if d < pad_min - EPS:
                sev = "error" if d < err_below - EPS else "warning"
                wx, wy = schema.pad_world(fp, pad)
                out.append(_v(
                    "drill_min", sev,
                    [(wx, wy, "F.Cu")],
                    [pad.get("id", "")],
                    f"pad {pad.get('id', '')!r} drill {d:.4g}mm < DFM minimum {pad_min:.4g}mm"))


# ── Public entry point ────────────────────────────────────────────────────────

def run_dfm(board: dict, rules: dict | None = None) -> list[Violation]:
    """Run the DFM rule catalogue over a board document.

    Pure + advisory: never raises on a malformed board, never mutates the
    input. Returns a deterministic, sorted list of :data:`Violation` dicts.

    ``rules`` is an optional dict of threshold overrides (keys match
    ``_DEFAULTS`` above). Omit a key to use the built-in default.

    DFM violations carry ``severity`` "warning" except ``courtyard_overlap``
    (components would physically collide -> "error") and sub-micro drill sizes.
    """
    if not schema.is_board(board):
        return []

    out: list[Violation] = []

    _check_silk_over_pad(board, rules, out)
    _check_silk_min_width(board, rules, out)
    _check_silk_min_text(board, rules, out)
    _check_mask_sliver(board, rules, out)
    _check_acid_trap(board, rules, out)
    _check_courtyard_overlap(board, rules, out)
    _check_annular_min(board, rules, out)
    _check_drill_min(board, rules, out)

    # Deterministic final ordering (same key strategy as drc.py)
    out.sort(key=lambda v: (
        v["rule"], v["severity"],
        tuple(v["items"]),
        tuple(tuple(loc) for loc in v["locs"]),
        v["message"],
    ))
    return out
