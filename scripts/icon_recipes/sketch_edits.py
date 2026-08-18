"""Icon recipes: sketch_edits (Sketch tab edit/constraint/misc tools)."""
import math

from icon_recipes.helpers import (ACCENT_RGB, accent, appearance, axis_edge, body_sel,
                                   box, datum, dim, region, sketch, sketch_with_id,
                                   sphere)

STEEL = {"color": [0.66, 0.69, 0.73], "metallic": 0.85, "roughness": 0.38}


def steel(bid="b1"):
    return {"op": "set_appearance", "id": bid, **STEEL}


def _poly(pts, prefix):
    ents = []
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        ents.append({"type": "line",
                     "p1": [round(a[0], 4), round(a[1], 4)],
                     "p2": [round(b[0], 4), round(b[1], 4)],
                     "id": f"{prefix}{i}"})
    return ents


def _rect_pts(cx, cy, l, w, ang=0.0):
    c, s = math.cos(math.radians(ang)), math.sin(math.radians(ang))
    out = []
    for dx, dy in ((-l / 2, -w / 2), (l / 2, -w / 2), (l / 2, w / 2), (-l / 2, w / 2)):
        out.append((cx + dx * c - dy * s, cy + dx * s + dy * c))
    return out


def slab(prefix, fid, cx, cy, l, w, h, ang=0.0, offset=None, merge=True,
         plane="plane.top"):
    """sketch(rect) + extrude -> one prismatic body. fid consumes fid and fid+1."""
    steps = sketch_with_id(_poly(_rect_pts(cx, cy, l, w, ang), prefix),
                           plane=plane, name=prefix, fid=fid, offset=offset)
    feat = {"type": "extrude", "name": prefix + "E",
            "params": {"mode": "boss", "depth": dim(h)},
            "refs": {"profile": [region(fid, cx, cy)]}}
    if not merge:
        feat["merge"] = False
    steps.append({"op": "add_feature", "feature": feat})
    return steps


def loop(prefix, fid, pts, rx, ry, h, offset=None, merge=True, plane="plane.top"):
    """sketch(closed polyline) + extrude; (rx,ry) must be inside the loop."""
    steps = sketch_with_id(_poly(pts, prefix), plane=plane, name=prefix, fid=fid,
                           offset=offset)
    feat = {"type": "extrude", "name": prefix + "E",
            "params": {"mode": "boss", "depth": dim(h)},
            "refs": {"profile": [region(fid, rx, ry)]}}
    if not merge:
        feat["merge"] = False
    steps.append({"op": "add_feature", "feature": feat})
    return steps


# --- trim: grey bar crossing a teal bar that is SPLIT at the crossing -------------
_TR_ANG = 60.0
_TR_C, _TR_S = math.cos(math.radians(_TR_ANG)), math.sin(math.radians(_TR_ANG))


def _tr_seg(t0, t1):
    tm = (t0 + t1) / 2.0
    return (tm * _TR_C, tm * _TR_S, t1 - t0)


_TRA = _tr_seg(-22, -3)
_TRB = _tr_seg(3, 22)

# --- pattern_circ_ent: seed cube + 5 copies on a r=18 circle ---------------------
_PC = [(18.0 * math.cos(math.radians(a)), 18.0 * math.sin(math.radians(a)))
       for a in range(0, 360, 60)]

# --- text_ent: extruded capital 'A' (two slanted legs + crossbar) ----------------
_A_LEFT = [(-14, 0), (-9, 0), (0, 28), (-5, 28)]
_A_RIGHT = [(14, 0), (5, 28), (0, 28), (9, 0)]

RECIPES = {
    # Two crossing bars; the teal one is cut into two segments with a 6mm gap
    # exactly where the grey bar crosses -> "trim away the overlap".
    "trim": {"yaw": 34, "pitch": 27, "steps": [
        *slab("tb", "f1", 0, 0, 44, 6, 4),
        *slab("ta", "f3", _TRA[0], _TRA[1], _TRA[2], 6, 4, ang=_TR_ANG, merge=False),
        *slab("tc", "f5", _TRB[0], _TRB[1], _TRB[2], 6, 4, ang=_TR_ANG, merge=False),
        steel("b1"), accent("b2"), accent("b3"),
    ]},

    # Short grey stub vs the teal bar that now REACHES the wall face at x=+20.
    "extend": {"yaw": 34, "pitch": 26, "steps": [
        *slab("eg", "f1", -15, -8, 14, 8, 8),                       # grey stub
        *slab("et", "f3", -1, 6, 42, 8, 8, merge=False),            # extended teal bar
        *slab("ew", "f5", 0, 10, 30, 20, 6, offset=20, merge=False,
              plane="plane.right"),                                  # wall plate
        steel("b1"), accent("b2"), steel("b3"),
    ]},

    # Grey bar + a parallel teal copy offset 10mm.
    "offset_ent": {"yaw": 34, "pitch": 27, "steps": [
        *slab("og", "f1", 0, -5, 36, 8, 4),
        *slab("ot", "f3", 0, 5, 36, 8, 4, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # Steel solid + a teal wire-like bar lying on its top front edge.
    "convert_ent": {"yaw": 34, "pitch": 26, "steps": [
        box(36, 24, 16, name="Solid"),
        *slab("cv", "f2", 0, -12, 36, 4, 4, offset=6, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # Grey L on the left, teal mirror-image L on the right.
    "mirror_ent": {"yaw": 34, "pitch": 27, "steps": [
        *loop("ml", "f1", [(-24, -11), (-6, -11), (-6, -3), (-16, -3),
                           (-16, 11), (-24, 11)], -20, 4, 6),
        *loop("mr", "f3", [(24, -11), (6, -11), (6, -3), (16, -3),
                           (16, 11), (24, 11)], 20, 4, 6, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # Grey seed bar + 3 teal copies in a row.
    "pattern_lin_ent": {"yaw": 34, "pitch": 27, "steps": [
        *slab("p0", "f1", -21, 0, 10, 10, 8),
        *slab("p1", "f3", -7, 0, 10, 10, 8, merge=False),
        *slab("p2", "f5", 7, 0, 10, 10, 8, merge=False),
        *slab("p3", "f7", 21, 0, 10, 10, 8, merge=False),
        steel("b1"), accent("b2"), accent("b3"), accent("b4"),
    ]},

    # Grey seed cube + 5 teal copies around a circle (top-down-ish camera).
    "pattern_circ_ent": {"yaw": 0, "pitch": 55, "steps": [
        *slab("q0", "f1", _PC[0][0], _PC[0][1], 10, 10, 8),
        *slab("q1", "f3", _PC[1][0], _PC[1][1], 10, 10, 8, merge=False),
        *slab("q2", "f5", _PC[2][0], _PC[2][1], 10, 10, 8, merge=False),
        *slab("q3", "f7", _PC[3][0], _PC[3][1], 10, 10, 8, merge=False),
        *slab("q4", "f9", _PC[4][0], _PC[4][1], 10, 10, 8, merge=False),
        *slab("q5", "f11", _PC[5][0], _PC[5][1], 10, 10, 8, merge=False),
        steel("b1"), accent("b2"), accent("b3"), accent("b4"),
        accent("b5"), accent("b6"),
    ]},

    # Grey original + teal copy displaced up-right.
    "move_ent": {"yaw": 34, "pitch": 27, "steps": [
        *slab("vg", "f1", -7, -7, 34, 8, 5),
        *slab("vt", "f3", 7, 7, 34, 8, 5, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # Grey original + teal copy rotated ~50 deg about their shared endpoint.
    "rotate_ent": {"yaw": 34, "pitch": 27, "steps": [
        *slab("rg", "f1", 2, -10, 40, 8, 5),
        *slab("rt", "f3", -18 + 20 * math.cos(math.radians(50)),
              -10 + 20 * math.sin(math.radians(50)), 40, 8, 5, ang=50, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # Teal solid plate + a thin grey ghost centerline strip standing proud.
    "construction": {"yaw": 34, "pitch": 27, "steps": [
        *slab("kp", "f1", 0, 0, 36, 20, 4),
        *slab("kc", "f3", 0, 0, 36, 1.5, 5, merge=False),
        accent("b1"), steel("b2"),
    ]},

    # Two teal bars meeting flush at a corner -> a geometric relation.
    "relations": {"yaw": 34, "pitch": 27, "steps": [
        *slab("na", "f1", 0, -8, 24, 8, 4),
        *slab("nb", "f3", -8, 8, 8, 24, 4, merge=False),
        accent("b1"), accent("b2"),
    ]},

    # Teal bar clamped between two grey fixture blocks (vice) -> fixed.
    "fix": {"yaw": 34, "pitch": 26, "steps": [
        *slab("xb", "f1", 0, 0, 40, 10, 6),
        *slab("xl", "f3", -18, 0, 10, 14, 12, merge=False),
        *slab("xr", "f5", 18, 0, 10, 14, 12, merge=False),
        accent("b1"), steel("b2"), steel("b3"),
    ]},

    # Plain steel plate; the dimension itself is the annotation.
    "smart_dim": {"yaw": 34, "pitch": 27, "steps": [
        *slab("dp", "f1", 0, 0, 40, 16, 4),
        steel("b1"),
    ]},

    # Grey plane plate (top at z=0) + a small teal sphere point resting on it.
    "point_ent": {"yaw": 34, "pitch": 27, "steps": [
        sphere(5.5, name="Pt"),
        *slab("pp", "f2", 0, 0, 32, 32, 2, offset=-8, merge=False),
        accent("b1"), steel("b2"),
    ]},

    # Extruded capital 'A' standing on the front plane.
    "text_ent": {"yaw": 30, "pitch": 18, "steps": [
        *loop("al", "f1", _A_LEFT, -8, 10, 4, plane="plane.front"),
        *loop("ar", "f3", _A_RIGHT, 8, 10, 4, plane="plane.front", merge=False),
        *slab("ax", "f5", 0, 13, 14, 4, 4, plane="plane.front", merge=False),
        accent("b1"), accent("b2"), accent("b3"),
    ]},
}

ANN = {
    "trim": {"ring": [0.50, 0.50, 0.13]},
    "extend": {"arrow": [0.42, 0.50, 0.72, 0.50]},
    "offset_ent": {"darrow": [0.50, 0.38, 0.50, 0.62]},
    "convert_ent": {"arrow": [0.50, 0.12, 0.50, 0.30]},
    "mirror_ent": {"dash": [0.45, 0.14, 0.45, 0.86]},
    "pattern_lin_ent": {"arrow": [0.25, 0.35, 0.75, 0.35]},
    "pattern_circ_ent": {"arc": [0.50, 0.50, 0.21, -40, 200]},
    "move_ent": {"arrow": [0.36, 0.62, 0.62, 0.38]},
    "rotate_ent": {"arc": [0.32, 0.62, 0.28, 200, 290]},
    "relations": {"ring": [0.38, 0.55, 0.13]},
    "smart_dim": {"darrow": [0.24, 0.30, 0.76, 0.30]},
}
