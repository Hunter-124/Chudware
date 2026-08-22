"""Icon recipes: sketch_edits (Sketch tab edit/constraint/misc tools)."""
import math

from icon_recipes.helpers import (accent, accent2, arc_arrow3d, arrow3d, box, dim,
                                   region, sketch_with_id, sphere)

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


# --- rotate_ent: the grey "before" bar, swung 80 deg about the origin ------------
# The pivot MUST be the origin: arc_arrow3d revolves about axis.z, and the engine's datum
# axes all pass through it, so an off-origin pivot would leave the turn cue orbiting
# nothing. Both bars therefore run outward from the origin instead of sharing an endpoint
# somewhere off to the left, which is what the old recipe did.
_RO_ANG = 80.0
_RO_R = 20.0
_RO_CX = _RO_R * math.cos(math.radians(_RO_ANG))
_RO_CY = _RO_R * math.sin(math.radians(_RO_ANG))

# --- pattern_circ_ent: seed cube + 5 copies on a r=18 circle ---------------------
_PC = [(18.0 * math.cos(math.radians(a)), 18.0 * math.sin(math.radians(a)))
       for a in range(0, 360, 60)]

# --- text_ent: extruded capital 'A' (two slanted legs + crossbar) ----------------
_A_LEFT = [(-14, 0), (-9, 0), (0, 28), (-5, 28)]
_A_RIGHT = [(14, 0), (5, 28), (0, 28), (9, 0)]

RECIPES = {
    # A steel boundary bar crossed by an accent bar that STOPS DEAD against it: the stub
    # that used to run on past is simply not there, and the empty quadrant to its right is
    # what "trimmed away" looks like. The old tile split the accent bar into two segments
    # with a 6 mm gap across a 60-degree crossing; at 32 px the gap closed up and the three
    # overlapping bars turned to mush.
    "trim": {"yaw": 34, "pitch": 27, "steps": [
        *slab("tg", "f1", 4, 0, 8, 34, 6),                      # boundary the trim cut to
        *slab("ts", "f3", -16, 0, 32, 9, 6, merge=False),        # survivor, ends at x=0
        steel("b1"), accent("b2"),
    ]},

    # Accent bar grown along +X until it lands on the steel wall face at x=+24, with the
    # short steel stub below showing the length it started at. The growth direction is a
    # REAL arrow on the X axis: the bars are parked at y>=7 and the arrow tip stops short
    # of the wall, so nothing interpenetrates the cue.
    "extend": {"yaw": 34, "pitch": 26, "steps": [
        *slab("ew", "f1", 27, 12, 6, 32, 6, offset=-3),                     # wall
        *slab("eg", "f3", -18, -15, 24, 10, 6, offset=-3, merge=False),     # original stub
        *slab("et", "f5", -3, 12, 54, 10, 6, offset=-3, merge=False),       # extended bar
        *arrow3d("f7", "f9", base=-14, length=32, axis="x",
                 radius=2.6, head_len=11.0, head_radius=6.0),
        steel("b1"), steel("b2"), accent("b3"), accent2("b4"), accent2("b5"),
    ]},

    # Steel bar + its accent offset copy, with the offset DISTANCE and direction carried by
    # a real arrow on the Y axis. The pair is pushed out to x>=10 so the arrow can sit on
    # the axis (arrow3d has no off-axis form — the engine only revolves about the datums)
    # and still lie in the sketch plane at z=0 rather than standing out of it.
    "offset_ent": {"yaw": 34, "pitch": 27, "steps": [
        *slab("og", "f1", 24, -9, 28, 8, 4, offset=-2),
        *slab("ot", "f3", 24, 9, 28, 8, 4, offset=-2, merge=False),
        *arrow3d("f5", "f7", base=-15, length=30, axis="y",
                 radius=2.6, head_len=11.0, head_radius=6.0),
        steel("b1"), accent("b2"), accent2("b3"), accent2("b4"),
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

    # Steel seed + 3 accent copies in a row, with the pattern direction as a real arrow on
    # the X axis running the full length of the row. The row is lifted to y>=9 so the arrow
    # can lie on the axis (arrow3d is axis-bound) instead of running through the blocks.
    "pattern_lin_ent": {"yaw": 34, "pitch": 27, "steps": [
        *slab("p0", "f1", -21, 15, 12, 12, 8, offset=-4),
        *slab("p1", "f3", -7, 15, 12, 12, 8, offset=-4, merge=False),
        *slab("p2", "f5", 7, 15, 12, 12, 8, offset=-4, merge=False),
        *slab("p3", "f7", 21, 15, 12, 12, 8, offset=-4, merge=False),
        *arrow3d("f9", "f11", base=-27, length=54, axis="x",
                 radius=2.8, head_len=13.0, head_radius=6.5),
        steel("b1"), accent("b2"), accent("b3"), accent("b4"),
        accent2("b5"), accent2("b6"),
    ]},

    # Steel seed cube + 5 accent copies around a circle, now under a real curved arrow
    # wrapping the same Z axis the pattern turns about. The arc floats at z=13, clear of the
    # 8 mm-tall cubes, so the top-down camera reads ring-of-parts plus turn.
    "pattern_circ_ent": {"yaw": 0, "pitch": 55, "steps": [
        *slab("q0", "f1", _PC[0][0], _PC[0][1], 10, 10, 8),
        *slab("q1", "f3", _PC[1][0], _PC[1][1], 10, 10, 8, merge=False),
        *slab("q2", "f5", _PC[2][0], _PC[2][1], 10, 10, 8, merge=False),
        *slab("q3", "f7", _PC[3][0], _PC[3][1], 10, 10, 8, merge=False),
        *slab("q4", "f9", _PC[4][0], _PC[4][1], 10, 10, 8, merge=False),
        *slab("q5", "f11", _PC[5][0], _PC[5][1], 10, 10, 8, merge=False),
        *arc_arrow3d("f13", "f15", radius=18, height=13, tube_radius=3.0,
                     sweep=175, head_len=12.0, head_radius=8.0, head_sweep=46),
        steel("b1"), accent("b2"), accent("b3"), accent("b4"),
        accent("b5"), accent("b6"), accent2("b7"), accent2("b8"),
    ]},

    # Steel original + accent copy displaced along +X, with a real arrow on the X axis for
    # the translation. Square blocks rather than the old diagonally-shifted bars: a pure
    # axial move of two collinear bars reads as "extend", and the axis-bound arrow cannot
    # point along the old diagonal at all.
    "move_ent": {"yaw": 34, "pitch": 27, "steps": [
        *slab("vg", "f1", -17, 17, 18, 18, 6, offset=-3),
        *slab("vt", "f3", 17, 17, 18, 18, 6, offset=-3, merge=False),
        *arrow3d("f5", "f7", base=-18, length=36, axis="x",
                 radius=2.6, head_len=12.0, head_radius=6.0),
        steel("b1"), accent("b2"), accent2("b3"), accent2("b4"),
    ]},

    # Steel "before" bar at 80 deg and the accent result at 0 deg, both radiating from the
    # origin, under a real curved arrow about axis.z. arc_arrow3d puts its flared tip at the
    # arc's START angle, so the accent bar is the one parked at 0 and the turn reads as
    # ending on the result rather than pointing away from it.
    "rotate_ent": {"yaw": 20, "pitch": 48, "steps": [
        *slab("rg", "f1", _RO_CX, _RO_CY, 40, 8, 5, ang=_RO_ANG),
        *slab("rt", "f3", _RO_R, 0, 40, 8, 5, merge=False),
        *arc_arrow3d("f5", "f7", radius=20, height=10, tube_radius=3.0,
                     sweep=90, head_len=11.0, head_radius=8.0, head_sweep=40),
        steel("b1"), accent("b2"), accent2("b3"), accent2("b4"),
    ]},

    # Accent profile plate crossed by a construction line that overhangs both edges at 24
    # deg. Construction geometry is reference, not result: it is thinner than the profile
    # and painted accent2 so it reads as an annotation on the sketch. The old tile ran a
    # 1.5 mm steel strip straight down the plate's own centreline, which at 32 px looked
    # like a seam in the plate rather than a separate reference entity.
    "construction": {"yaw": 34, "pitch": 30, "steps": [
        *slab("kp", "f1", 0, 0, 36, 22, 4),
        *slab("kc", "f3", 0, 0, 52, 3, 3, ang=24, offset=4, merge=False),
        accent("b1"), accent2("b2"),
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

    # Steel plane plate + the accent point resting on it. The point was r=5.5 on a 32 mm
    # plate — 12% of the frame, which downsampled to a speck and left the tile reading as a
    # bare grey slab. r=9 on the same plate makes the sphere a third of the tile: the plate
    # is now the supporting cue and the point is the subject.
    "point_ent": {"yaw": 34, "pitch": 27, "steps": [
        sphere(9.0, name="Pt"),
        *slab("pp", "f2", 0, 0, 32, 32, 2, offset=-11, merge=False),
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
    # ring re-aimed at the new crossing (x=0 in a bbox that runs x=-32..8), not the tile
    # centre it sat on when the accent bar was symmetric about the origin.
    "trim": {"ring": [0.68, 0.50, 0.13]},
    # extend / offset_ent / pattern_lin_ent / move_ent / rotate_ent / pattern_circ_ent:
    # retired, NOT deleted — each recipe now builds a real 3D arrow (arrow3d or
    # arc_arrow3d) shaded and foreshortened by the tile's own camera. A flat overlay on top
    # would double the cue and fight the perspective. The empty spec is falsy, so load_tile
    # skips the overlay; the key stays to document the retirement.
    "extend": {},
    "offset_ent": {},
    "convert_ent": {"arrow": [0.50, 0.12, 0.50, 0.30]},
    "mirror_ent": {"dash": [0.45, 0.14, 0.45, 0.86]},
    "pattern_lin_ent": {},
    "pattern_circ_ent": {},
    "move_ent": {},
    "rotate_ent": {},
    "relations": {"ring": [0.38, 0.55, 0.13]},
    "smart_dim": {"darrow": [0.24, 0.30, 0.76, 0.30]},
}
