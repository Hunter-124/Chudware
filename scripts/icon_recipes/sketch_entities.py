"""Icon recipes: sketch_entities (Sketch ribbon tab entity-creation tools).

Engine facts proven live while authoring these (host/build-linux/bin/chudware,
CHUDWARE_SHOT=iconpipe):
  * core/geom/profile.cpp::build_edge accepts ONLY line / circle / arc / spline
    entities. slot, arc_slot, ellipse, ellipse_arc, parabola and conic entities are
    skipped, so the following extrude silently no-ops (step prints ok=1 but the doc
    revision never advances and the tile is 0/16384 opaque px). Every curve below is
    therefore authored from arcs (exact circular geometry) or through-point splines
    (interpolated, self-closing when first==last) -- smooth walls, no facet ribs.
  * A sweep path must LEAVE the profile plane: a plane.right (normal +X) circle profile
    needs a plane.top path whose first segment runs along +X. A path starting along +Y
    lies in the profile plane and the sweep produces nothing.
  * A multi-segment polyline sweep path renders as disconnected sausages; a single
    `spline` path entity sweeps as one continuous tube.
"""
from __future__ import annotations

import math

from icon_recipes.helpers import *  # noqa: F401,F403

STEEL_RGB = [0.66, 0.69, 0.73]


def steel(bid: str = "b1") -> dict:
    return {"op": "set_appearance", "id": bid, "color": STEEL_RGB,
            "metallic": 0.85, "roughness": 0.38}


# --- entity builders -------------------------------------------------------------------
def line_ent(a, b, eid: str) -> dict:
    return {"type": "line", "p1": [float(a[0]), float(a[1])],
            "p2": [float(b[0]), float(b[1])], "id": eid}


def poly(pts, pre: str = "p") -> list:
    """Closed polyline entity loop through pts."""
    out = []
    for i in range(len(pts)):
        out.append(line_ent(pts[i], pts[(i + 1) % len(pts)], f"{pre}{i}"))
    return out


def arc_ent(c, r, a0, a1, eid: str, ccw: bool = True) -> dict:
    return {"type": "arc", "center": [float(c[0]), float(c[1])], "radius": float(r),
            "start": float(a0), "end": float(a1), "ccw": ccw, "id": eid}


def spl(pts, eid: str) -> dict:
    return {"type": "spline", "points": [[float(p[0]), float(p[1])] for p in pts],
            "id": eid}


def rect_pts(w: float, h: float, cx: float = 0.0, cy: float = 0.0):
    return [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2],
            [cx + w / 2, cy + h / 2], [cx - w / 2, cy + h / 2]]


def ngon(n: int, r: float, rot: float = 90.0):
    return [[r * math.cos(math.radians(rot + 360.0 * i / n)),
             r * math.sin(math.radians(rot + 360.0 * i / n))] for i in range(n)]


def star(n: int, ro: float, ri: float, rot: float = 90.0):
    pts = []
    for i in range(2 * n):
        r = ro if i % 2 == 0 else ri
        a = math.radians(rot + 180.0 * i / n)
        pts.append([r * math.cos(a), r * math.sin(a)])
    return pts


def slot_ents(p1, p2, width: float, pre: str = "sl") -> list:
    """Stadium (straight slot) loop: two flank lines + two exact semicircular caps."""
    hw = width / 2.0
    ang = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0]))
    nx = math.cos(math.radians(ang + 90)) * hw
    ny = math.sin(math.radians(ang + 90)) * hw
    return [
        line_ent([p1[0] + nx, p1[1] + ny], [p2[0] + nx, p2[1] + ny], pre + "a"),
        arc_ent(p2, hw, ang + 90, ang - 90, pre + "b", ccw=False),
        line_ent([p2[0] - nx, p2[1] - ny], [p1[0] - nx, p1[1] - ny], pre + "c"),
        arc_ent(p1, hw, ang - 90, ang - 270, pre + "d", ccw=False),
    ]


def band_ents(center, r: float, a0: float, a1: float, width: float,
              pre: str = "bd") -> list:
    """Curved band (arc-spine capsule) loop: outer arc, end cap, inner arc, start cap.
    Exact circular geometry -- the replacement for the non-extrudable arc_slot entity."""
    hw = width / 2.0
    cx, cy = center
    e0 = [cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))]
    e1 = [cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))]
    return [
        arc_ent(center, r + hw, a0, a1, pre + "o"),
        arc_ent(e1, hw, a1, a1 + 180, pre + "e"),
        arc_ent(center, r - hw, a0, a1, pre + "i"),
        arc_ent(e0, hw, a0 + 180, a0 + 360, pre + "s"),
    ]


def ell_pt(center, rx, ry, a, rot=0.0):
    ar, rr = math.radians(a), math.radians(rot)
    x, y = rx * math.cos(ar), ry * math.sin(ar)
    return [center[0] + x * math.cos(rr) - y * math.sin(rr),
            center[1] + x * math.sin(rr) + y * math.cos(rr)]


def ell_pts(center, rx, ry, a0=0.0, a1=360.0, n: int = 16, rot=0.0):
    return [ell_pt(center, rx, ry, a0 + (a1 - a0) * i / n, rot) for i in range(n + 1)]


def parabola_pts(vertex, focus, t0, t1, n: int = 16):
    """P(t) = vertex + (t^2/4f)*u + t*w  (core/sketch/sketch.cpp parabola_curve)."""
    dx, dy = focus[0] - vertex[0], focus[1] - vertex[1]
    f = math.hypot(dx, dy)
    ux, uy = dx / f, dy / f
    wx, wy = -uy, ux
    out = []
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        k = t * t / (4.0 * f)
        out.append([vertex[0] + k * ux + t * wx, vertex[1] + k * uy + t * wy])
    return out


def conic_pts(p0, p1, apex, rho, n: int = 16):
    """Rational quadratic Bezier p0 -> p1 shouldered at apex (weight w = rho/(1-rho))."""
    w = rho / (1.0 - rho)
    out = []
    for i in range(n + 1):
        t = i / n
        b0, b1, b2 = (1 - t) ** 2, 2 * t * (1 - t) * w, t * t
        den = b0 + b1 + b2
        out.append([(b0 * p0[0] + b1 * apex[0] + b2 * p1[0]) / den,
                    (b0 * p0[1] + b1 * apex[1] + b2 * p1[1]) / den])
    return out


def sample(fn, x0, x1, n):
    """Sample fn over [x0,x1], translated so the first point sits at the origin
    (a sweep path must start on the profile plane)."""
    pts = [[x0 + (x1 - x0) * i / (n - 1), fn(x0 + (x1 - x0) * i / (n - 1))]
           for i in range(n)]
    return [[p[0] - pts[0][0], p[1] - pts[0][1]] for p in pts]


# --- recipe builders -------------------------------------------------------------------
def ent_prism(ents, depth: float, name: str, cx=0.0, cy=0.0, yaw=35, pitch=26) -> dict:
    """Teal extruded prism from one closed entity loop on plane.top."""
    return {"yaw": yaw, "pitch": pitch, "steps": [
        *sketch_with_id(ents, plane="plane.top", name=name + "Sk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": name,
            "params": {"mode": "boss", "depth": dim(depth)},
            "refs": {"profile": [region("f1", cx, cy)]}}},
        accent(),
    ]}


def prism(pts, depth: float, name: str, pre: str, cx=0.0, cy=0.0, yaw=35, pitch=26) -> dict:
    """Teal extruded prism from a closed polyline."""
    return ent_prism(poly(pts, pre), depth, name, cx, cy, yaw, pitch)


def tube(path_pts, r: float = 4.0, yaw=34, pitch=28) -> dict:
    """Teal tube swept along ONE spline path (plane.top) from a circle profile
    (plane.right). Path must start at the origin and head along +X."""
    mid = path_pts[len(path_pts) // 2]
    return {"yaw": yaw, "pitch": pitch, "steps": [
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": r, "id": "tc"}],
                        plane="plane.right", name="TubeProf", fid="f1"),
        *sketch_with_id([spl(path_pts, "tp")], plane="plane.top", name="TubePath",
                        fid="f2"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Tube",
            "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 0, 0)],
                     "path": region("f2", mid[0], mid[1])}}},
        accent(),
    ]}


# --- shared geometry ------------------------------------------------------------------
PLATE = rect_pts(40, 28)

# Curved teal wall / arc-slot band (arc-spine capsule, exact arcs).
ARC_WALL = band_ents([0, -10], 22, 20, 160, 6, "aw")
ARC_SLOT = band_ents([0, -10], 22, 20, 160, 10, "as")
_ARC_MID = [0.0, 12.0]           # spine mid point (angle 90) -> region pick in the band

# Tangent bar at the a1=160 end of the arc wall, running outward along the tangent.
_a = math.radians(160)
_ex, _ey = 22 * math.cos(_a), -10 + 22 * math.sin(_a)
_tx, _ty = -math.sin(_a), math.cos(_a)
TAN_BAR = slot_ents([_ex - _tx * 7, _ey - _ty * 7],
                    [_ex + _tx * 22, _ey + _ty * 22], 6, "tb")
_TAN_MID = [_ex + _tx * 10, _ey + _ty * 10]

# Sweep paths (all start at the origin heading +X).
S_PTS = [[0, 0], [16, 11], [32, -11], [48, 0]]
STYLE_PTS = [[0, 0], [22, -8], [36, 8], [26, 32], [2, 38]]
SINE_PTS = sample(lambda x: 8.0 * math.sin(x / 7.0), 0.0, 70.0, 17)

# Airfoil section: two open splines (upper nose->tail, lower tail->nose) so the nose
# and the tail stay SHARP instead of being rounded by a single closed interpolation.
AF_UPPER = [[-24, 0], [-21, 4.6], [-14, 7.6], [-6, 9.0], [2, 9.2], [12, 7.0],
            [20, 3.6], [26, 0]]
AF_LOWER = [[26, 0], [18, -2.6], [10, -4.4], [0, -5.0], [-10, -4.4], [-18, -3.0],
            [-22, -1.4], [-24, 0]]

RECIPES: dict[str, dict] = {
    # sketch: grey sketch-plane plate with a teal rectangle outline drawn on it.
    "sketch": {"yaw": 34, "pitch": 30, "steps": [
        box(40, 40, 2, name="Plane"),
        *sketch_with_id(poly(rect_pts(30, 3, 0, 13.5), "ob"), plane="plane.top",
                        name="OL1Sk", fid="f2", offset=1),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "OL1",
            "params": {"mode": "boss", "depth": dim(2)}, "merge": False,
            "refs": {"profile": [region("f2", 0, 13.5)]}}},
        *sketch_with_id(poly(rect_pts(30, 3, 0, -13.5), "oc"), plane="plane.top",
                        name="OL2Sk", fid="f4", offset=1),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "OL2",
            "params": {"mode": "boss", "depth": dim(2)}, "merge": False,
            "refs": {"profile": [region("f4", 0, -13.5)]}}},
        *sketch_with_id(poly(rect_pts(3, 24, -13.5, 0), "od"), plane="plane.top",
                        name="OL3Sk", fid="f6", offset=1),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "OL3",
            "params": {"mode": "boss", "depth": dim(2)}, "merge": False,
            "refs": {"profile": [region("f6", -13.5, 0)]}}},
        *sketch_with_id(poly(rect_pts(3, 24, 13.5, 0), "oe"), plane="plane.top",
                        name="OL4Sk", fid="f8", offset=1),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "OL4",
            "params": {"mode": "boss", "depth": dim(2)}, "merge": False,
            "refs": {"profile": [region("f8", 13.5, 0)]}}},
        steel("b1"), accent("b2"), accent("b3"), accent("b4"), accent("b5"),
    ]},

    # line: a single teal bar.
    "line": prism(rect_pts(44, 6), 4, "Line", "ln", yaw=34, pitch=30),

    # rectangle family: one teal plate; the annotation dots carry the pick semantics.
    "rect_corner": prism(PLATE, 4, "Rect", "rc", yaw=34, pitch=30),
    "rect_center": prism(PLATE, 4, "Rect", "rd", yaw=34, pitch=30),
    "rect_3pt_corner": prism(PLATE, 4, "Rect", "re", yaw=34, pitch=30),
    "rect_3pt_center": prism(PLATE, 4, "Rect", "rf", yaw=34, pitch=30),

    # parallelogram: skewed plate.
    "parallelogram": prism([[-20, -10], [8, -10], [20, 10], [-8, 10]], 4,
                           "Para", "pg", yaw=34, pitch=30),

    # circle family: teal squat cylinder.
    "circle": ent_prism([{"type": "circle", "center": [0, 0], "radius": 20, "id": "c1"}],
                        12, "Circle"),
    "circle_perimeter": ent_prism(
        [{"type": "circle", "center": [0, 0], "radius": 20, "id": "c2"}], 12, "Circle"),

    # arc family: curved teal wall.
    "arc_3pt": ent_prism(ARC_WALL, 10, "ArcWall", _ARC_MID[0], _ARC_MID[1]),
    "arc_center": ent_prism(band_ents([0, -10], 22, 20, 160, 6, "ac"), 10, "ArcWall",
                            _ARC_MID[0], _ARC_MID[1]),

    # arc_tangent: arc wall + a separate tangent bar body (both teal).
    "arc_tangent": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(ARC_WALL, plane="plane.top", name="ArcSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "ArcWall",
            "params": {"mode": "boss", "depth": dim(10)},
            "refs": {"profile": [region("f1", _ARC_MID[0], _ARC_MID[1])]}}},
        *sketch_with_id(TAN_BAR, plane="plane.top", name="TanSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "TanBar",
            "params": {"mode": "boss", "depth": dim(10)}, "merge": False,
            "refs": {"profile": [region("f3", _TAN_MID[0], _TAN_MID[1])]}}},
        accent("b1"), accent("b2"),
    ]},

    # polygon family.
    "polygon": prism(ngon(6, 20), 10, "Hex", "hx"),
    "poly_triangle": prism(ngon(3, 20), 8, "Tri", "tr"),
    "poly_square": prism(rect_pts(30, 30), 8, "Sq", "sq"),
    "poly_pentagon": prism(ngon(5, 20), 8, "Pent", "pt"),
    "poly_octagon": prism(ngon(8, 20), 8, "Oct", "oc"),
    "poly_star": prism(star(5, 22, 9), 8, "Star", "st"),

    # straight slot family (stadium: lines + exact semicircular caps).
    "slot": ent_prism(slot_ents([-18, 0], [18, 0], 14, "s1"), 8, "Slot"),
    "slot_ctr_str": ent_prism(slot_ents([-18, 0], [18, 0], 14, "s2"), 8, "Slot"),

    # arc slot family (wider curved band).
    "slot_arc_3pt": ent_prism(ARC_SLOT, 8, "ArcSlot", _ARC_MID[0], _ARC_MID[1]),
    "slot_arc_ctr": ent_prism(band_ents([0, -10], 22, 20, 160, 10, "a9"), 8, "ArcSlot",
                              _ARC_MID[0], _ARC_MID[1]),

    # spline family: teal tubes swept along the characteristic spline path.
    "spline": tube(S_PTS),
    "spline_style": tube(STYLE_PTS),
    "spline_equation": tube(SINE_PTS),

    # spline_surface: teal spline tube laid on the top face of a grey plate.
    "spline_surface": {"yaw": 34, "pitch": 30, "steps": [
        box(56, 40, 3, name="Plate"),
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": 4, "id": "sc"}],
                        plane="plane.right", name="SProf", fid="f2", offset=-28),
        *sketch_with_id([spl([[p[0] - 28, p[1]] for p in SINE_PTS], "sp")],
                        plane="plane.top", name="SPath", fid="f3", offset=5),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "STube",
            "params": {"mode": "boss"}, "merge": False,
            "refs": {"profile": [region("f2", 0, 0)],
                     "path": region("f3", SINE_PTS[6][0] - 28, SINE_PTS[6][1])}}},
        steel("b1"), accent("b2"),
    ]},

    # ellipse: teal elliptical boss (closed interpolated spline through the ellipse).
    "ellipse": ent_prism([spl(ell_pts([0, 0], 24, 14, 0, 360, 20), "el")],
                         10, "Ellipse"),

    # ellipse_partial: teal elliptical band -- outer + inner elliptical arc, chord-closed.
    "ellipse_partial": ent_prism([
        spl(ell_pts([0, -6], 22, 12, 15, 165, 14), "eo"),
        spl(ell_pts([0, -6], 16, 7, 15, 165, 14), "ei"),
        line_ent(ell_pt([0, -6], 22, 12, 165), ell_pt([0, -6], 16, 7, 165), "ec1"),
        line_ent(ell_pt([0, -6], 22, 12, 15), ell_pt([0, -6], 16, 7, 15), "ec2"),
    ], 6, "EllBand", 0, 3.5),

    # parabola: teal parabolic wall -- a 6mm band following the curve so the tile reads
    # as the CURVE, not a plate. P(t) = vertex + (t^2/4f)*u + t*w
    # (core/sketch/sketch.cpp parabola_curve); vertex (0,-16), focus (0,-8) -> f=8.
    "parabola": ent_prism([
        spl(parabola_pts([0, -16], [0, -8], -24, 24), "pbo"),
        spl(parabola_pts([0, -10], [0, -2], -21, 21), "pbi"),
        line_ent(parabola_pts([0, -16], [0, -8], -24, -24, 1)[0],
                 parabola_pts([0, -10], [0, -2], -21, -21, 1)[0], "pc1"),
        line_ent(parabola_pts([0, -16], [0, -8], 24, 24, 1)[0],
                 parabola_pts([0, -10], [0, -2], 21, 21, 1)[0], "pc2"),
    ], 10, "Parab", 0, -13, yaw=0, pitch=52),

    # conic: teal conic wall -- band between two concentric conics (pointier than the
    # parabola thanks to rho=0.62).
    "conic": ent_prism([
        spl(conic_pts([-24, 12], [24, 12], [0, -20], 0.62), "cno"),
        spl(conic_pts([-18, 12], [18, 12], [0, -13], 0.62), "cni"),
        line_ent([-24, 12], [-18, 12], "cc1"),
        line_ent([24, 12], [18, 12], "cc2"),
    ], 10, "Conic", 0, -8, yaw=0, pitch=52),

    # airfoil: teal wing section (sharp nose + tail via two chained open splines).
    "airfoil": ent_prism([spl(AF_UPPER, "au"), spl(AF_LOWER, "al")],
                         12, "Airfoil", 0, 1, yaw=30, pitch=26),
}

ANN: dict[str, dict] = {
    "rect_corner": {"dots": [[0.28, 0.62]]},
    "rect_center": {"dots": [[0.5, 0.45]]},
    "rect_3pt_corner": {"dots": [[0.28, 0.62], [0.72, 0.62], [0.72, 0.30]]},
    "rect_3pt_center": {"dots": [[0.5, 0.45], [0.72, 0.45], [0.5, 0.22]]},
    "circle_perimeter": {"dots": [[0.5, 0.18], [0.24, 0.55], [0.76, 0.55]]},
    "arc_3pt": {"dots": [[0.5, 0.22], [0.22, 0.5], [0.78, 0.5]]},
    "arc_center": {"dots": [[0.5, 0.62]]},
    "arc_tangent": {"arrow": [0.42, 0.16, 0.06, 0.34]},
    "slot_ctr_str": {"dash": [0.22, 0.5, 0.78, 0.5]},
    "slot_arc_3pt": {"dots": [[0.5, 0.22], [0.22, 0.5], [0.78, 0.5]]},
    "slot_arc_ctr": {"dots": [[0.5, 0.62]]},
}
