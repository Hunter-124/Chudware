"""Icon recipes: Assembly group + remaining Features-tab tools (ref geometry, boundary, wrap)."""
from icon_recipes.helpers import *  # noqa: F401,F403

STEEL = {"op": "set_appearance", "id": "b1", "color": [0.66, 0.69, 0.73],
         "metallic": 0.85, "roughness": 0.38}


def steel(bid="b1"):
    return {"op": "set_appearance", "id": bid, "color": [0.66, 0.69, 0.73],
            "metallic": 0.85, "roughness": 0.38}


def light(bid="b1"):
    return {"op": "set_appearance", "id": bid, "color": [0.78, 0.81, 0.85],
            "metallic": 0.10, "roughness": 0.50}


def rect(x0, y0, x1, y1, pfx):
    return [
        {"type": "line", "p1": [x0, y0], "p2": [x1, y0], "id": pfx + "a"},
        {"type": "line", "p1": [x1, y0], "p2": [x1, y1], "id": pfx + "b"},
        {"type": "line", "p1": [x1, y1], "p2": [x0, y1], "id": pfx + "c"},
        {"type": "line", "p1": [x0, y1], "p2": [x0, y0], "id": pfx + "d"},
    ]


def hexagon(cx, cy, r, pfx):
    import math
    pts = [(cx + r * math.cos(math.radians(60 * i)),
            cy + r * math.sin(math.radians(60 * i))) for i in range(6)]
    out = []
    for i in range(6):
        p, q = pts[i], pts[(i + 1) % 6]
        out.append({"type": "line", "p1": [round(p[0], 3), round(p[1], 3)],
                    "p2": [round(q[0], 3), round(q[1], 3)], "id": f"{pfx}{i}"})
    return out

def arc_band(r_in, r_out, a0, a1, pfx, n=10):
    """Closed polyline loop approximating an annular sector (a curved rail)."""
    import math
    outer = [(r_out * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
              r_out * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]
    inner = [(r_in * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
              r_in * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]
    loop = outer + inner[::-1]
    out = []
    for i in range(len(loop)):
        p, q = loop[i], loop[(i + 1) % len(loop)]
        out.append({"type": "line", "p1": [round(p[0], 3), round(p[1], 3)],
                    "p2": [round(q[0], 3), round(q[1], 3)], "id": f"{pfx}{i}"})
    return out



def ext(name, depth, fid, cx, cy, mode="boss", merge=True):
    f = {"type": "extrude", "name": name,
         "params": {"mode": mode, "depth": dim(depth)},
         "refs": {"profile": [region(fid, cx, cy)]}}
    if not merge:
        f["merge"] = False
    return {"op": "add_feature", "feature": f}


RECIPES: dict[str, dict] = {

    # insert_component: recessed grey base plate + teal component hovering above it.
    "insert_component": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-18, -18, 18, 18, "bp"), "plane.top", "BaseSk", "f1"),
        ext("Base", 5, "f1", 0, 0),
        *sketch_with_id(rect(-9, -9, 9, 9, "rc"), "plane.top", "RecessSk", "f3", offset=5),
        ext("Recess", 3, "f3", 0, 0, mode="cut"),
        *sketch_with_id(rect(-9, -9, 9, 9, "cp"), "plane.top", "CompSk", "f5", offset=15),
        ext("Comp", 12, "f5", 0, 0, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # mate: two upright plates pressed face-to-face (grey source, teal mated part).
    "mate": {"yaw": 38, "pitch": 22, "steps": [
        *sketch_with_id(rect(-15, -10, 15, 10, "pa"), "plane.front", "PlateASk", "f1",
                        offset=-6),
        ext("PlateA", 6, "f1", 0, 0, merge=False),
        *sketch_with_id(rect(-13, -8, 13, 8, "pb"), "plane.front", "PlateBSk", "f3",
                        offset=0),
        ext("PlateB", 6, "f3", 0, 0, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # move_component: a single grey block; the arrow carries the translation.
    "move_component": {"yaw": 35, "pitch": 26, "steps": [
        box(26, 20, 16, "MoveBlk"), steel("b1"),
    ]},

    # rotate_component: same grey block; the arc carries the rotation.
    "rotate_component": {"yaw": 35, "pitch": 26, "steps": [
        box(26, 20, 16, "RotBlk"), steel("b1"),
    ]},

    # pattern_component: grey rail carrying three teal hex-prism instances.
    "pattern_component": {"yaw": 34, "pitch": 28, "steps": [
        *sketch_with_id(rect(-26, -5, 26, 5, "rl"), "plane.top", "RailSk", "f1"),
        ext("Rail", 4, "f1", 0, 0),
        *sketch_with_id(hexagon(-16, 0, 5.6, "h1"), "plane.top", "Hex1Sk", "f3", offset=4),
        ext("Hex1", 8, "f3", -16, 0, merge=False),
        *sketch_with_id(hexagon(0, 0, 5.6, "h2"), "plane.top", "Hex2Sk", "f5", offset=4),
        ext("Hex2", 8, "f5", 0, 0, merge=False),
        *sketch_with_id(hexagon(16, 0, 5.6, "h3"), "plane.top", "Hex3Sk", "f7", offset=4),
        ext("Hex3", 8, "f7", 16, 0, merge=False),
        steel("b1"), accent("b2"), accent("b3"), accent("b4"),
    ]},

    # smart_fasteners: steel hex bolt beside a drilled grey plate with a teal washer.
    "smart_fasteners": {"yaw": 34, "pitch": 26, "steps": [
        *sketch_with_id(hexagon(0, 0, 9, "hh"), "plane.top", "HeadSk", "f1"),
        ext("Head", 7, "f1", 0, 0),
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": 5, "id": "shk"}],
                        "plane.top", "ShankSk", "f3", offset=7),
        ext("Shank", 22, "f3", 0, 0),
        *sketch_with_id(rect(15, -15, 45, 15, "pl"), "plane.top", "PlateSk", "f5"),
        ext("Plate", 5, "f5", 30, 0, merge=False),
        *sketch_with_id([{"type": "circle", "center": [30, 0], "radius": 6, "id": "hl"}],
                        "plane.top", "HoleSk", "f7", offset=5),
        ext("Hole", 6, "f7", 30, 0, mode="cut"),
        *sketch_with_id([
            {"type": "circle", "center": [30, 0], "radius": 8, "id": "wo"},
            {"type": "circle", "center": [30, 0], "radius": 5.5, "id": "wi"},
        ], "plane.top", "WasherSk", "f9", offset=5),
        ext("Washer", 2, "f9", 36.7, 0, merge=False),
        {"op": "set_appearance", "id": "b1", "color": [0.66, 0.69, 0.73],
         "metallic": 0.90, "roughness": 0.35},
        steel("b2"), accent("b3"),
    ]},

    # exploded_view: three plates separated by air gaps along Z.
    "exploded_view": {"yaw": 34, "pitch": 22, "steps": [
        *sketch_with_id(rect(-18, -18, 18, 18, "e1"), "plane.top", "L1Sk", "f1"),
        ext("L1", 5, "f1", 0, 0, merge=False),
        *sketch_with_id(rect(-14, -14, 14, 14, "e2"), "plane.top", "L2Sk", "f3", offset=14),
        ext("L2", 4, "f3", 0, 0, merge=False),
        *sketch_with_id(rect(-10, -10, 10, 10, "e3"), "plane.top", "L3Sk", "f5", offset=26),
        ext("L3", 4, "f5", 0, 0, merge=False),
        steel("b1"), accent("b2"), steel("b3"),
    ]},

    # bom: upright grey plate with three cut "rows" + a teal header strip.
    "bom": {"yaw": 34, "pitch": 20, "steps": [
        *sketch_with_id(rect(-15, -18, 15, 18, "bp"), "plane.front", "PlateSk", "f1"),
        ext("Plate", 3, "f1", 0, 0),
        *sketch_with_id(rect(-12, 3, 12, 5, "r1"), "plane.front", "Row1Sk", "f3"),
        ext("Row1", 4, "f3", 0, 4, mode="cut"),
        *sketch_with_id(rect(-12, -3, 12, -1, "r2"), "plane.front", "Row2Sk", "f5"),
        ext("Row2", 4, "f5", 0, -2, mode="cut"),
        *sketch_with_id(rect(-12, -9, 12, -7, "r3"), "plane.front", "Row3Sk", "f7"),
        ext("Row3", 4, "f7", 0, -8, mode="cut"),
        *sketch_with_id(rect(-12, 10, 12, 14, "hd"), "plane.front", "HeadSk", "f9",
                        offset=3),
        ext("Header", 1.5, "f9", 0, 12, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # interference: a grey block pierced by a red bar (clash volume).
    "interference": {"yaw": 35, "pitch": 24, "steps": [
        *sketch_with_id(rect(-13, -11, 13, 11, "bx"), "plane.top", "BlockSk", "f1"),
        ext("Block", 18, "f1", 0, 0, merge=False),
        *sketch_with_id(rect(-5, 4, 5, 14, "br"), "plane.right", "BarSk", "f3", offset=-20),
        ext("Bar", 40, "f3", 0, 9, merge=False),
        steel("b1"),
        {"op": "set_appearance", "id": "b2", "color": [0.85, 0.32, 0.28],
         "metallic": 0.30, "roughness": 0.45},
    ]},

    # boundary: two grey curved rails bridged by a teal boundary surface.
    "boundary": {"yaw": 34, "pitch": 30, "steps": [
        *sketch_with_id(arc_band(14, 18, 45, 135, "ra"), "plane.top", "Rail1Sk", "f1"),
        ext("Rail1", 3, "f1", 0, 16, merge=False),
        *sketch_with_id(arc_band(28, 32, 45, 135, "rb"), "plane.top", "Rail2Sk", "f3"),
        ext("Rail2", 3, "f3", 0, 30, merge=False),
        *sketch_with_id(rect(-13, 16, 13, 29, "sf"), "plane.top", "SurfSk", "f5", offset=3),
        ext("Surf", 1.5, "f5", 0, 22.5, merge=False),
        steel("b1"), steel("b2"), accent("b3"),
    ]},

    # wrap: teal band wrapped around the waist of a grey cylinder.
    "wrap": {"yaw": 35, "pitch": 22, "steps": [
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": 14, "id": "cy"}],
                        "plane.top", "CylSk", "f1"),
        ext("Cyl", 30, "f1", 0, 0),
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 16.5, "id": "bo"},
            {"type": "circle", "center": [0, 0], "radius": 14.5, "id": "bi"},
        ], "plane.top", "BandSk", "f3", offset=13),
        ext("Band", 4, "f3", 15.5, 0, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # ref_plane: a thin light-grey datum plate.
    "ref_plane": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-17, -17, 17, 17, "dp"), "plane.top", "PlaneSk", "f1"),
        ext("DatumPlate", 1.5, "f1", 0, 0),
        light("b1"),
    ]},

    # ref_axis: a long thin rod along X.
    "ref_axis": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": 1.8, "id": "ax"}],
                        "plane.right", "AxisSk", "f1", offset=-22),
        ext("AxisRod", 44, "f1", 0, 0),
        light("b1"),
    ]},

    # ref_point: a small light-grey sphere.
    "ref_point": {"yaw": 35, "pitch": 26, "steps": [
        sphere(8, "RefPt"), light("b1"),
    ]},

    # ref_csys: three coloured rods meeting at the origin (X red, Y green, Z blue).
    "ref_csys": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": 1.6, "id": "rx"}],
                        "plane.right", "XSk", "f1"),
        ext("XRod", 26, "f1", 0, 0, merge=False),
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": 1.6, "id": "ry"}],
                        "plane.front", "YSk", "f3"),
        ext("YRod", 26, "f3", 0, 0, merge=False),
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": 1.6, "id": "rz"}],
                        "plane.top", "ZSk", "f5"),
        ext("ZRod", 26, "f5", 0, 0, merge=False),
        {"op": "set_appearance", "id": "b1", "color": [0.85, 0.30, 0.28],
         "metallic": 0.30, "roughness": 0.45},
        {"op": "set_appearance", "id": "b2", "color": [0.30, 0.75, 0.35],
         "metallic": 0.30, "roughness": 0.45},
        {"op": "set_appearance", "id": "b3", "color": [0.28, 0.45, 0.90],
         "metallic": 0.30, "roughness": 0.45},
    ]},
}


ANN: dict[str, dict] = {
    "insert_component": {"arrow": [0.50, 0.12, 0.50, 0.38]},
    "move_component": {"arrow": [0.25, 0.60, 0.70, 0.30]},
    "rotate_component": {"arc": [0.50, 0.45, 0.30, -30, 150]},
    "pattern_component": {"arrow": [0.22, 0.35, 0.78, 0.35]},
    "exploded_view": {"darrow": [0.82, 0.20, 0.82, 0.75]},
}
