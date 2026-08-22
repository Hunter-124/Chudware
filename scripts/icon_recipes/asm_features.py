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

    # insert_component: grey base plate with a recess + orange component still in the air
    # above it, and a real 3D arrow plunging DOWN the insertion axis. The flat overlay drew
    # that arrow on the glass; arrow3d(direction=-1) puts it in the scene, so it is shaded
    # and foreshortened by the same camera as the part it is pushing home.
    "insert_component": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-18, -18, 18, 18, "bp"), "plane.top", "BaseSk", "f1"),
        ext("Base", 5, "f1", 0, 0),
        *sketch_with_id(rect(-9, -9, 9, 9, "rc"), "plane.top", "RecessSk", "f3", offset=5),
        ext("Recess", 3, "f3", 0, 0, mode="cut"),
        *sketch_with_id(rect(-9, -9, 9, 9, "cp"), "plane.top", "CompSk", "f5", offset=15),
        ext("Comp", 12, "f5", 0, 0, merge=False),
        # f7 = shaft sketch, f8 = shaft extrude, f9 = head sketch, f10 = head revolve.
        # Tip stops 3mm above the component's top face so the cone stays a clean silhouette
        # instead of merging into the block it points at.
        *arrow3d("f7", "f9", base=45, length=15, direction=-1),
        steel("b1"), accent("b2"), accent2("b3"), accent2("b4"),
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

    # move_component: the component itself in orange -- it is what the drag repositions --
    # with a real 3D arrow along +X. A translation has no privileged direction, so the
    # arrow goes on the X axis, which passes through the block's mid-height and reads as a
    # slide across the tile rather than as the extrude family's "grows upward" cue.
    "move_component": {"yaw": 35, "pitch": 26, "steps": [
        box(26, 20, 16, "MoveBlk"),
        # f2 = shaft sketch, f3 = shaft extrude, f4 = head sketch, f5 = head revolve
        *arrow3d("f2", "f4", base=17, length=22, axis="x"),
        accent("b1"), accent2("b2"), accent2("b3"),
    ]},

    # rotate_component: same orange component, but the cue is arc_arrow3d riding above the
    # top face at r=21 -- clear of the block's 16.4mm corner radius, so the radially flared
    # head never buries itself in the part it is turning.
    "rotate_component": {"yaw": 35, "pitch": 26, "steps": [
        box(26, 20, 16, "RotBlk"),
        # f2 = tube sketch, f3 = tube revolve, f4 = head sketch, f5 = head revolve
        *arc_arrow3d("f2", "f4", radius=21, height=13),
        accent("b1"), accent2("b2"), accent2("b3"),
    ]},

    # pattern_component: grey rail carrying three orange instances plus a real 3D arrow
    # continuing along the rail on +X, which is the actual pattern direction. The rail is
    # sketched at offset=-2 so it straddles z=0: the arrow can only lie ON a global axis,
    # and centring the rail on X is what puts the arrow down its middle instead of half
    # buried under it. The instances are pulled in from +/-16 to +/-12 because adding 17mm
    # of arrow to a 52mm rail turned the tile into a thin horizontal streak.
    "pattern_component": {"yaw": 34, "pitch": 28, "steps": [
        *sketch_with_id(rect(-20, -5, 20, 5, "rl"), "plane.top", "RailSk", "f1", offset=-2),
        ext("Rail", 4, "f1", 0, 0),
        *sketch_with_id(hexagon(-12, 0, 5.2, "h1"), "plane.top", "Hex1Sk", "f3", offset=2),
        ext("Hex1", 8, "f3", -12, 0, merge=False),
        *sketch_with_id(hexagon(0, 0, 5.2, "h2"), "plane.top", "Hex2Sk", "f5", offset=2),
        ext("Hex2", 8, "f5", 0, 0, merge=False),
        *sketch_with_id(hexagon(12, 0, 5.2, "h3"), "plane.top", "Hex3Sk", "f7", offset=2),
        ext("Hex3", 8, "f7", 12, 0, merge=False),
        # f9 = shaft sketch, f10 = shaft extrude, f11 = head sketch, f12 = head revolve
        *arrow3d("f9", "f11", base=24, length=17, axis="x", radius=2.6),
        steel("b1"), accent("b2"), accent("b3"), accent("b4"),
        accent2("b5"), accent2("b6"),
    ]},

    # smart_fasteners: steel hex bolt beside a drilled grey plate with an orange washer.
    "smart_fasteners": {"yaw": 34, "pitch": 26, "steps": [
        *sketch_with_id(hexagon(0, 0, 9, "hh"), "plane.top", "HeadSk", "f1"),
        ext("Head", 7, "f1", 0, 0),
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": 5, "id": "shk"}],
                        "plane.top", "ShankSk", "f3", offset=7),
        # merge=False: with the shank fusing into the head, the engine produced one body
        # fewer than the recipe counts, so the washer was painted as b3 and a fourth body
        # was left in the engine default material. One body per feature, all four painted.
        ext("Shank", 22, "f3", 0, 0, merge=False),
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
        steel("b2"), steel("b3"), accent("b4"),
    ]},

    # exploded_view: three plates separated by air gaps along Z, with a real 3D arrow
    # continuing up the stack axis. The retired overlay was a double-headed arrow drawn
    # beside the stack, which reads as a dimension; a single solid arrow standing on the
    # axis the parts actually travelled along reads as the explode direction.
    "exploded_view": {"yaw": 34, "pitch": 22, "steps": [
        *sketch_with_id(rect(-18, -18, 18, 18, "e1"), "plane.top", "L1Sk", "f1"),
        ext("L1", 5, "f1", 0, 0, merge=False),
        *sketch_with_id(rect(-14, -14, 14, 14, "e2"), "plane.top", "L2Sk", "f3", offset=14),
        ext("L2", 4, "f3", 0, 0, merge=False),
        *sketch_with_id(rect(-10, -10, 10, 10, "e3"), "plane.top", "L3Sk", "f5", offset=26),
        ext("L3", 4, "f5", 0, 0, merge=False),
        # f7 = shaft sketch, f8 = shaft extrude, f9 = head sketch, f10 = head revolve
        *arrow3d("f7", "f9", base=34, length=18),
        steel("b1"), accent("b2"), steel("b3"), accent2("b4"), accent2("b5"),
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

    # ref_plane: an orange datum plane floating proud of a grey host block and overhanging
    # it on every side. The old tile was a bare light-grey parallelogram with no accent at
    # all, which at 32px was indistinguishable from a blank slab -- nothing said "datum".
    # The offset gap plus the overhang is what makes it read as a plane RELATIVE TO a body.
    "ref_plane": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-13, -11, 13, 11, "hb"), "plane.top", "HostSk", "f1"),
        ext("HostBlock", 12, "f1", 0, 0),
        *sketch_with_id(rect(-19, -16, 19, 16, "dp"), "plane.top", "PlaneSk", "f3",
                        offset=20),
        ext("DatumPlane", 1.6, "f3", 0, 0, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # ref_axis: a chunky orange rod driven straight through a grey block. The old rod was
    # r=1.8 and rendered as a hairline diagonal that the downsample to 32px erased almost
    # entirely; r=3.4 survives it. Running the rod THROUGH a host body is what separates
    # "an axis" from "a stick", and it is the only thing that distinguished this tile from
    # ref_point once both were lone grey primitives.
    "ref_axis": {"yaw": 35, "pitch": 26, "steps": [
        box(30, 24, 14, "AxisHost"),
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": 3.4, "id": "ax"}],
                        "plane.top", "AxisSk", "f2", offset=-24),
        ext("AxisRod", 48, "f2", 0, 0, merge=False),
        steel("b1"), accent("b2"),
    ]},

    # ref_point: a small orange marker ball sitting on the CORNER of a grey plate. The old
    # tile was a lone grey sphere filling the frame, which reads as "a ball", not "a point":
    # a point needs something to be a point ON. The plate is sketched from the origin out to
    # (30,24) so the origin -- where sphere() always centres -- lands on its top corner
    # vertex, and that off-centre composition is what keeps this tile clear of mass_props,
    # which parks its marker in the MIDDLE of a face.
    "ref_point": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(0, 0, 30, 24, "hp"), "plane.top", "HostSk", "f1", offset=-7),
        ext("HostPlate", 7, "f1", 15, 12),
        sphere(5.5, "RefPt"),
        steel("b1"), accent("b2"),
    ]},

    # ref_csys: three real 3D arrows through the origin, one per global axis, replacing
    # three r=1.6 rods that at 32px were three coloured hairlines. Each arrow is a solid
    # shaft plus a cone, so the triad still reads as a coordinate system after the
    # downsample. Coloured by ROLE rather than by the red/green/blue axis convention: Z
    # takes the primary orange, X the secondary blue, Y stays desaturated grey -- so the
    # tile follows the user's chosen accent colors instead of freezing three hand-typed
    # hues the runtime recolor cannot remap.
    "ref_csys": {"yaw": 35, "pitch": 26, "steps": [
        # f1/f3 = X arrow sketches, f5/f7 = Y, f9/f11 = Z; two bodies each, in that order.
        *arrow3d("f1", "f3", base=0, length=27, axis="x", radius=2.8),
        *arrow3d("f5", "f7", base=0, length=27, axis="y", radius=2.8),
        *arrow3d("f9", "f11", base=0, length=27, axis="z", radius=2.8),
        accent2("b1"), accent2("b2"),
        light("b3"), light("b4"),
        accent("b5"), accent("b6"),
    ]},
}


ANN: dict[str, dict] = {
    # All five of these now carry real 3D arrow bodies (arrow3d / arc_arrow3d in their
    # recipes above). The keys stay, empty, to record that the flat overlay was deliberately
    # retired rather than quietly lost: a second arrow painted on the glass would fight the
    # shaded one's perspective and double the cue.
    "insert_component": {},
    "move_component": {},
    "rotate_component": {},
    "pattern_component": {},
    "exploded_view": {},
}
