"""Icon recipes: restyle of the 34 existing Feature/SheetMetal/Weldment/Workspace tiles.

Style law (Hunter, T-BL34 follow-up):
  * steel grey = base/stock material,
  * app teal   = what the operation PRODUCED,
  * one obvious annotation cue in the direction of the action.
"""
from icon_recipes.helpers import *  # noqa: F401,F403


def grey(body_id: str = "b1") -> dict:
    """Steel-grey base material (the stock the tool acts on)."""
    return {"op": "set_appearance", "id": body_id, "color": [0.66, 0.69, 0.73],
            "metallic": 0.85, "roughness": 0.38}


def rect(x0, y0, x1, y1, tag):
    return [
        {"type": "line", "p1": [x0, y0], "p2": [x1, y0], "id": tag + "1"},
        {"type": "line", "p1": [x1, y0], "p2": [x1, y1], "id": tag + "2"},
        {"type": "line", "p1": [x1, y1], "p2": [x0, y1], "id": tag + "3"},
        {"type": "line", "p1": [x0, y1], "p2": [x0, y0], "id": tag + "4"},
    ]


RECIPES: dict[str, dict] = {

    # -------------------------------------------------------------- 3D Body
    # extrude_boss: thin GREY sketch-plane plate, ORANGE block grown out of it, and a real
    # 3D BLUE arrow continuing along the extrude direction so the tile shows which way the
    # material went (Hunter: "arrows in 3d pointing out of them in the direction").
    "extrude_boss": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-22, -18, 22, 18, "pl"), plane="plane.top",
                        name="PlateSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Plate",
            "params": {"mode": "boss", "depth": dim(3)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id(rect(-15, -11, 15, 11, "bx"), plane="plane.top",
                        name="BossSk", fid="f3", offset=3),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss",
            "params": {"mode": "boss", "depth": dim(26)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        # f5 = shaft sketch, f6 = shaft extrude, f7 = head sketch, f8 = head revolve
        *arrow3d("f5", "f7", base=34, length=30),
        grey("b1"),
        accent("b2"),
        accent2("b3"), accent2("b4"),
    ]},

    # extrude_cut: grey block, pocket plunged into its top face.
    "extrude_cut": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-26, -20, 26, 20, "b"), plane="plane.top",
                        name="CutBlockSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "CutBlock",
            "params": {"mode": "boss", "depth": dim(30)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id(rect(-15, -11, 15, 11, "p"), plane="plane.top",
                        name="PocketSk", fid="f3", offset=30),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Pocket",
            "params": {"mode": "cut", "depth": dim(22)},
            "refs": {"profile": [region("f3", 0, 0)]}}},
        grey("b1"),
    ]},

    # revolve_boss: TEAL revolved cup + thin GREY axis rod proud of both ends.
    "revolve_boss": {"yaw": 35, "pitch": 20, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [5, -18], "p2": [20, -18], "id": "e1"},
            {"type": "line", "p1": [20, -18], "p2": [20, 18], "id": "e2"},
            {"type": "line", "p1": [20, 18], "p2": [5, 18], "id": "e3"},
            {"type": "line", "p1": [5, 18], "p2": [5, -18], "id": "e4"},
        ], plane="plane.front", name="RevolveSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Revolve1",
            "params": {"mode": "boss", "angle": dim(360)},
            "refs": {"profile": [region("f1", 12, 0)], "axis": datum("axis.z")}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 3.2, "id": "rod"},
        ], plane="plane.top", name="AxisSk", fid="f3", offset=-28),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "AxisRod",
            "params": {"mode": "boss", "depth": dim(56)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        accent("b1"),
        grey("b2"),
    ]},

    # revolve_cut: grey cylinder with a revolved groove.
    "revolve_cut": {"yaw": 35, "pitch": 20, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 20, "id": "c1"},
        ], plane="plane.top", name="CylSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cyl1",
            "params": {"mode": "boss", "depth": dim(40)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "line", "p1": [0, -25], "p2": [0, 25], "id": "axis",
             "construction": True},
            {"type": "line", "p1": [12, 17], "p2": [20, 17], "id": "g1"},
            {"type": "line", "p1": [20, 17], "p2": [20, 23], "id": "g2"},
            {"type": "line", "p1": [20, 23], "p2": [12, 23], "id": "g3"},
            {"type": "line", "p1": [12, 23], "p2": [12, 17], "id": "g4"},
        ], plane="plane.front", name="GrooveSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Groove1",
            "params": {"mode": "cut", "angle": dim(360)},
            "refs": {"profile": [region("f3", 16, 20)], "axis": datum("axis.z")}}},
        grey("b1"),
    ]},

    # sweep: teal pipe swept along an L path.
    "sweep": {"yaw": 40, "pitch": 28, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 5, "id": "c1"},
        ], plane="plane.right", name="SwProf", fid="f1"),
        *sketch_with_id([
            {"type": "line", "p1": [0, 0], "p2": [30, 0], "id": "p1"},
            {"type": "line", "p1": [30, 0], "p2": [30, 25], "id": "p2"},
        ], plane="plane.top", name="SwPath", fid="f2"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Pipe1",
            "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 0, 0)], "path": region("f2", 15, 0)}}},
        accent(),
    ]},

    # sweep_cut: grey block with a swept channel carved through it.
    "sweep_cut": {"yaw": 40, "pitch": 28, "steps": [
        *sketch_with_id(rect(-25, -15, 25, 15, "b"), plane="plane.top",
                        name="BaseSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Base1",
            "params": {"mode": "boss", "depth": dim(20)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id(rect(-4.5, 14, 4.5, 28, "c"), plane="plane.right",
                        name="CutProf", fid="f3"),
        *sketch_with_id([
            {"type": "line", "p1": [-20, 0], "p2": [20, 0], "id": "p1"},
        ], plane="plane.top", name="CutPath", fid="f4"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "SlotCut",
            "params": {"mode": "cut"},
            "refs": {"profile": [region("f3", 0, 21)], "path": region("f4", 0, 0)}}},
        grey("b1"),
    ]},

    # loft: teal blend between a GREY bottom disc and a GREY top square plate
    # (the two end profiles stay visible as thin grey outline plates).
    "loft": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 18, "id": "c1"},
        ], plane="plane.top", name="LoftBot", fid="f1"),
        *sketch_with_id(rect(-12, -12, 12, 12, "s"), plane="plane.top",
                        name="LoftTop", fid="f2", offset=50),
        {"op": "add_feature", "feature": {
            "type": "loft", "name": "Blend1",
            "params": {"mode": "boss"},
            "refs": {"sections": [region("f1", 0, 0), region("f2", 0, 0)]}}},
        # bottom profile plate (grey), just under Z=0
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 21, "id": "pb"},
        ], plane="plane.top", name="BotPlate", fid="f4", offset=-3),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "BotPlate",
            "params": {"mode": "boss", "depth": dim(3)}, "merge": False,
            "refs": {"profile": [region("f4", 0, 0)]}}},
        # top profile plate (grey), just above Z=50
        *sketch_with_id(rect(-15, -15, 15, 15, "pt"), plane="plane.top",
                        name="TopPlate", fid="f6", offset=50),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "TopPlate",
            "params": {"mode": "boss", "depth": dim(3)}, "merge": False,
            "refs": {"profile": [region("f6", 0, 0)]}}},
        accent("b1"),
        grey("b2"),
        grey("b3"),
    ]},

    # loft_cut: grey block with a lofted taper pocket.
    "loft_cut": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id(rect(-26, -20, 26, 20, "b"), plane="plane.top",
                        name="BlockSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Block",
            "params": {"mode": "boss", "depth": dim(36)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 16, "id": "c1"},
        ], plane="plane.top", name="LCTop", fid="f3", offset=36),
        *sketch_with_id(rect(-5, -5, 5, 5, "s"), plane="plane.top",
                        name="LCMid", fid="f4", offset=16),
        {"op": "add_feature", "feature": {
            "type": "loft", "name": "TaperPocket",
            "params": {"mode": "cut"},
            "refs": {"sections": [region("f3", 0, 0), region("f4", 0, 0)]}}},
        grey("b1"),
    ]},

    # draft: GREY straight-walled lower block + TEAL drafted upper portion.
    "draft": {"yaw": 35, "pitch": 22, "steps": [
        *sketch_with_id(rect(-30, -23, 30, 23, "b"), plane="plane.top",
                        name="StraightSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "StraightBlock",
            "params": {"mode": "boss", "depth": dim(12)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id(rect(-26, -20, 26, 20, "d"), plane="plane.top",
                        name="DraftSk", fid="f3", offset=12),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "DraftBlock",
            "params": {"mode": "boss", "depth": dim(28), "draft": dim(18)},
            "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        grey("b1"),
        accent("b2"),
    ]},

    # rib: GREY L walls + TEAL reinforcing web (baked as its own thin body so the
    # rib itself can carry the accent -- the real rib feature fuses into the base).
    "rib": {"yaw": 40, "pitch": 24, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-24, -4], "p2": [24, -4], "id": "b1"},
            {"type": "line", "p1": [24, -4], "p2": [24, 4], "id": "b2"},
            {"type": "line", "p1": [24, 4], "p2": [-16, 4], "id": "b3"},
            {"type": "line", "p1": [-16, 4], "p2": [-16, 40], "id": "b4"},
            {"type": "line", "p1": [-16, 40], "p2": [-24, 40], "id": "b5"},
            {"type": "line", "p1": [-24, 40], "p2": [-24, -4], "id": "b6"},
        ], plane="plane.front", name="LBaseSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "LWalls",
            "params": {"mode": "boss", "depth": dim(30)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 6)]}}},
        # Triangular web bridging the inside corner, on the mid plane.
        *sketch_with_id([
            {"type": "line", "p1": [-16, 38], "p2": [-16, 4], "id": "w1"},
            {"type": "line", "p1": [-16, 4], "p2": [22, 4], "id": "w2"},
            {"type": "line", "p1": [22, 4], "p2": [-16, 38], "id": "w3"},
        ], plane="plane.front", name="WebSk", fid="f3", offset=12.5),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Web",
            "params": {"mode": "boss", "depth": dim(5)}, "merge": False,
            "refs": {"profile": [region("f3", -8, 12)]}}},
        grey("b1"),
        accent("b2"),
    ]},

    # hole_wizard: steel-grey block, counterbored hole reads as the dark stepped bore.
    "hole_wizard": {"yaw": 35, "pitch": 40, "steps": [
        *sketch_with_id(rect(-24, -20, 24, 20, "b"), plane="plane.top",
                        name="HWBlockSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "HWBlock",
            "params": {"mode": "boss", "depth": dim(26)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 11, "id": "cb1"},
        ], plane="plane.top", name="CboreSk", fid="f3", offset=26),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cbore",
            "params": {"mode": "cut", "depth": dim(8)},
            "refs": {"profile": [region("f3", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 6, "id": "tb1"},
        ], plane="plane.top", name="ThruSk", fid="f5", offset=26),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Thru",
            "params": {"mode": "cut", "depth": dim(30)},
            "refs": {"profile": [region("f5", 0, 0)]}}},
        grey("b1"),
    ]},

    # -------------------------------------------------------------- Pattern
    # linear_pattern: GREY plate + TEAL patterned bosses.
    "linear_pattern": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-35, -15, 35, 15, "b"), plane="plane.top",
                        name="PlateSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Plate",
            "params": {"mode": "boss", "depth": dim(8)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [-22, 0], "radius": 7, "id": "c1"},
        ], plane="plane.top", name="BossSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss",
            "params": {"mode": "boss", "depth": dim(12)}, "merge": False,
            "refs": {"profile": [region("f3", -22, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "linear_pattern", "name": "LinPat",
            "params": {"count1": 3, "spacing1": dim(22)},
            "refs": {"dir1": datum("axis.x"), "features": ["f4"]}}},
        grey("b1"),
        accent("b2"),
    ]},

    # circular_pattern: GREY disc + TEAL patterned spokes.
    "circular_pattern": {"yaw": 0, "pitch": 60, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 22, "id": "base"},
        ], plane="plane.top", name="DiscSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Disc",
            "params": {"mode": "boss", "depth": dim(6)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id(rect(6, -3, 20, 3, "s"), plane="plane.top",
                        name="SpokeSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Spoke",
            "params": {"mode": "boss", "depth": dim(10)}, "merge": False,
            "refs": {"profile": [region("f3", 13, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "circular_pattern", "name": "Wheel",
            "params": {"count": 6, "angle": dim(360), "equal_spacing": True},
            "refs": {"axis": datum("axis.z"), "features": ["f4"]}}},
        grey("b1"),
        accent("b2"),
    ]},

    # mirror: seed + mirrored copy. The mirror feature only survives on a MERGED
    # body (see T-BL34 note), so the whole part is steel grey and the dashed
    # symmetry-plane trace carries the intent.
    "mirror": {"yaw": 35, "pitch": 32, "steps": [
        *sketch_with_id(rect(-26, -16, 26, 16, "b"), plane="plane.top",
                        name="BaseSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Base",
            "params": {"mode": "boss", "depth": dim(6)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "line", "p1": [4, -14], "p2": [22, -14], "id": "l1"},
            {"type": "line", "p1": [22, -14], "p2": [22, -4], "id": "l2"},
            {"type": "line", "p1": [22, -4], "p2": [13, -4], "id": "l3"},
            {"type": "line", "p1": [13, -4], "p2": [13, -9], "id": "l4"},
            {"type": "line", "p1": [13, -9], "p2": [4, -9], "id": "l5"},
            {"type": "line", "p1": [4, -9], "p2": [4, -14], "id": "l6"},
        ], plane="plane.top", name="LBSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "LBoss",
            "params": {"mode": "boss", "depth": dim(22)},
            "refs": {"profile": [region("f3", 16, -8)]}}},
        {"op": "add_feature", "feature": {
            "type": "mirror", "name": "Mir1",
            "params": {},
            "refs": {"plane": datum("plane.front"), "features": ["f4"]}}},
        grey("b1"),
    ]},

    # ----------------------------------------------------------- Multi-body
    "combine": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-20, -14, 20, 14, "b"), plane="plane.top",
                        name="Body1Sk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Body1",
            "params": {"mode": "boss", "depth": dim(24)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [16, 0], "radius": 14, "id": "c1"},
        ], plane="plane.top", name="Body2Sk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Body2",
            "params": {"mode": "boss", "depth": dim(24)}, "merge": False,
            "refs": {"profile": [region("f3", 16, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "boolean", "name": "Union1",
            "params": {"op": "union"},
            "refs": {"targets": [body_sel("f2")], "tools": [body_sel("f4")]}}},
        accent("b1"),   # the fused RESULT is what the tool produced
    ]},

    "subtract": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-22, -16, 22, 16, "b"), plane="plane.top",
                        name="TgtSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Target",
            "params": {"mode": "boss", "depth": dim(28)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 11, "id": "c1"},
        ], plane="plane.top", name="ToolSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Tool",
            "params": {"mode": "boss", "depth": dim(34)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "boolean", "name": "Sub1",
            "params": {"op": "subtract"},
            "refs": {"targets": [body_sel("f2")], "tools": [body_sel("f4")]}}},
        grey("b1"),     # material was REMOVED: what remains is stock
    ]},

    "intersect": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-20, -20, 20, 20, "b"), plane="plane.top",
                        name="BlkSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Block",
            "params": {"mode": "boss", "depth": dim(30)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 18, "id": "c1"},
        ], plane="plane.top", name="CylSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cyl",
            "params": {"mode": "boss", "depth": dim(30)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "boolean", "name": "Isect1",
            "params": {"op": "intersect"},
            "refs": {"targets": [body_sel("f2")], "tools": [body_sel("f4")]}}},
        accent("b1"),   # the common volume is the produced body
    ]},

    # split: TEAL half + GREY half drawn apart.
    "split": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-24, -18, -2, 18, "l"), plane="plane.top",
                        name="LeftSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "LeftHalf",
            "params": {"mode": "boss", "depth": dim(26)}, "merge": False,
            "refs": {"profile": [region("f1", -13, 0)]}}},
        *sketch_with_id(rect(2, -18, 24, 18, "r"), plane="plane.top",
                        name="RightSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "RightHalf",
            "params": {"mode": "boss", "depth": dim(26)}, "merge": False,
            "refs": {"profile": [region("f3", 13, 0)]}}},
        accent("b1"),
        grey("b2"),
    ]},

    # ------------------------------------------------------------- Dress-up
    "fillet": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "fillet", "name": "Fillet1",
                     "params": {"radius": dim(5.0)}},
         "selrefs": {"edges": {"select": "all_edges", "n": 12}}},
        grey("b1"),
    ]},

    "chamfer": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "chamfer", "name": "Chamfer1",
                     "params": {"length": dim(4.5)}},
         "selrefs": {"edges": {"select": "all_edges", "n": 12}}},
        grey("b1"),
    ]},

    "shell": {"yaw": 40, "pitch": 34, "steps": [
        box(52, 40, 30),
        {"feature": {"type": "shell", "name": "Shell1",
                     "params": {"thickness": dim(3.0)}},
         "selrefs": {"remove": {"select": "top_face", "n": 1}}},
        grey("b1"),
    ]},

    # stock: raw aluminium chudware, unchanged.
    "stock": {"yaw": 32, "pitch": 24, "steps": [
        box(56, 40, 26),
        appearance(0.90, 0.32, (0.80, 0.82, 0.85)),
    ]},

    # --------------------------------------------------------------- Curves
    # curve_helix: a spring is STEEL, not teal; the teal arc annotates the coil.
    "curve_helix": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [10, 0], "radius": 1.5, "id": "c1"},
        ], plane="plane.front", name="WireProf", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "helix", "name": "CoilPath",
            "params": {"pitch": dim(5), "height": dim(20), "radius": dim(10)},
            "refs": {"base": "plane.top"}}},
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Spring",
            "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 10, 0)], "path": "f2"}}},
        grey("b1"),
    ]},

    # --------------------------------------------------------- Hole/Thread
    "thread": {"yaw": 35, "pitch": 24, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 7.5, "id": "sc1"},
        ], plane="plane.top", name="StudSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Stud",
            "params": {"mode": "boss", "depth": dim(42)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "thread", "name": "Thread",
            "params": {"major_dia": dim(15), "pitch": dim(3.5),
                       "style": "external", "mode": "cut", "form": "v",
                       "turns": dim(14)},
            "refs": {"target_face": {"of": "face", "kind": "cylindrical",
                                     "pick": "largest"}}}},
        {"op": "set_appearance", "id": "b1", "color": [0.70, 0.72, 0.75],
         "metallic": 0.9, "roughness": 0.35},
    ]},

    # ---------------------------------------------------------- Sheet Metal
    "sm_base_flange": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id(rect(-25, -20, 25, 20, "b"), plane="plane.top",
                        name="FlangeSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "sm_base_flange", "name": "BaseFlange",
            "params": {"thickness": dim(2)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        grey("b1"),
    ]},

    "sm_edge_flange": {"yaw": 40, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-24, 0], "p2": [24, 0], "id": "e1"},
            {"type": "line", "p1": [24, 0], "p2": [24, 18], "id": "e2"},
            {"type": "line", "p1": [24, 18], "p2": [22, 18], "id": "e3"},
            {"type": "line", "p1": [22, 18], "p2": [22, 2], "id": "e4"},
            {"type": "line", "p1": [22, 2], "p2": [-24, 2], "id": "e5"},
            {"type": "line", "p1": [-24, 2], "p2": [-24, 0], "id": "e6"},
        ], plane="plane.front", name="EdgeFlSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "EdgeFlange",
            "params": {"mode": "boss", "depth": dim(36)},
            "refs": {"profile": [region("f1", 0, 1)]}}},
        grey("b1"),
    ]},

    "sm_hem": {"yaw": 40, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-24, 0], "p2": [20, 0], "id": "h1"},
            {"type": "line", "p1": [20, 0], "p2": [20, 9], "id": "h2"},
            {"type": "line", "p1": [20, 9], "p2": [4, 9], "id": "h3"},
            {"type": "line", "p1": [4, 9], "p2": [4, 7], "id": "h4"},
            {"type": "line", "p1": [4, 7], "p2": [18, 7], "id": "h5"},
            {"type": "line", "p1": [18, 7], "p2": [18, 2], "id": "h6"},
            {"type": "line", "p1": [18, 2], "p2": [-24, 2], "id": "h7"},
            {"type": "line", "p1": [-24, 2], "p2": [-24, 0], "id": "h8"},
        ], plane="plane.front", name="HemSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Hem",
            "params": {"mode": "boss", "depth": dim(36)},
            "refs": {"profile": [region("f1", -10, 1)]}}},
        grey("b1"),
    ]},

    "sm_jog": {"yaw": 40, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-24, 0], "p2": [2, 0], "id": "j1"},
            {"type": "line", "p1": [2, 0], "p2": [2, 12], "id": "j2"},
            {"type": "line", "p1": [2, 12], "p2": [24, 12], "id": "j3"},
            {"type": "line", "p1": [24, 12], "p2": [24, 14], "id": "j4"},
            {"type": "line", "p1": [24, 14], "p2": [4, 14], "id": "j5"},
            {"type": "line", "p1": [4, 14], "p2": [4, 2], "id": "j6"},
            {"type": "line", "p1": [4, 2], "p2": [-24, 2], "id": "j7"},
            {"type": "line", "p1": [-24, 2], "p2": [-24, 0], "id": "j8"},
        ], plane="plane.front", name="JogSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Jog",
            "params": {"mode": "boss", "depth": dim(36)},
            "refs": {"profile": [region("f1", -10, 1)]}}},
        grey("b1"),
    ]},

    "sm_flat_pattern": {"yaw": 0, "pitch": 90, "steps": [
        *sketch_with_id(rect(-25, -20, 25, 20, "b"), plane="plane.top",
                        name="FlangeSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "sm_base_flange", "name": "BaseFlange",
            "params": {"thickness": dim(2)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "sm_flat_pattern", "name": "Flat", "params": {}}},
        grey("b1"),
    ]},

    # ------------------------------------------------------------ Weldments
    # weld_member: structural steel I-beam (grey, not teal).
    "weld_member": {"yaw": 38, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-12, -14], "p2": [12, -14], "id": "i1"},
            {"type": "line", "p1": [12, -14], "p2": [12, -10], "id": "i2"},
            {"type": "line", "p1": [12, -10], "p2": [3, -10], "id": "i3"},
            {"type": "line", "p1": [3, -10], "p2": [3, 10], "id": "i4"},
            {"type": "line", "p1": [3, 10], "p2": [12, 10], "id": "i5"},
            {"type": "line", "p1": [12, 10], "p2": [12, 14], "id": "i6"},
            {"type": "line", "p1": [12, 14], "p2": [-12, 14], "id": "i7"},
            {"type": "line", "p1": [-12, 14], "p2": [-12, 10], "id": "i8"},
            {"type": "line", "p1": [-12, 10], "p2": [-3, 10], "id": "i9"},
            {"type": "line", "p1": [-3, 10], "p2": [-3, -10], "id": "i10"},
            {"type": "line", "p1": [-3, -10], "p2": [-12, -10], "id": "i11"},
            {"type": "line", "p1": [-12, -10], "p2": [-12, -14], "id": "i12"},
        ], plane="plane.right", name="IBeamSk", fid="f1"),
        *sketch_with_id([
            {"type": "line", "p1": [0, 0], "p2": [60, 0], "id": "p1"},
        ], plane="plane.top", name="MemberPath", fid="f2"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Member",
            "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 0, 0)], "path": region("f2", 30, 0)}}},
        grey("b1"),
    ]},

    # weld_gusset: two GREY members meeting at a corner + TEAL gusset plate on top.
    "weld_gusset": {"yaw": 35, "pitch": 34, "steps": [
        *sketch_with_id(rect(-24, -6, 4, 6, "m"), plane="plane.top",
                        name="Member1Sk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Member1",
            "params": {"mode": "boss", "depth": dim(9)}, "merge": False,
            "refs": {"profile": [region("f1", -10, 0)]}}},
        *sketch_with_id(rect(-4, 6, 4, 26, "n"), plane="plane.top",
                        name="Member2Sk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Member2",
            "params": {"mode": "boss", "depth": dim(9)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 16)]}}},
        # Gusset plate: right triangle bridging both members, sitting on their tops.
        *sketch_with_id([
            {"type": "line", "p1": [-20, -2], "p2": [2, -2], "id": "g1"},
            {"type": "line", "p1": [2, -2], "p2": [2, 22], "id": "g2"},
            {"type": "line", "p1": [2, 22], "p2": [-20, -2], "id": "g3"},
        ], plane="plane.top", name="GussetSk", fid="f5", offset=9),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Gusset",
            "params": {"mode": "boss", "depth": dim(4)}, "merge": False,
            "refs": {"profile": [region("f5", -5, 2)]}}},
        grey("b1"),
        grey("b2"),
        accent("b3"),
    ]},

    # weld_end_cap: GREY tube member + TEAL cap plate closing the end.
    "weld_end_cap": {"yaw": 38, "pitch": 26, "steps": [
        *sketch_with_id(rect(-14, -12, 14, 12, "m"), plane="plane.right",
                        name="TubeSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Tube",
            "params": {"mode": "boss", "depth": dim(50)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id(rect(-16, -14, 16, 14, "cp"), plane="plane.right",
                        name="CapSk", fid="f3", offset=50),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cap",
            "params": {"mode": "boss", "depth": dim(4)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        grey("b1"),
        accent("b2"),
    ]},

    # ----------------------------------------------------------- Workspaces
    # ws_model: steel-grey L-bracket -- reads as a real machined part.
    "ws_model": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-22, -16], "p2": [22, -16], "id": "l1"},
            {"type": "line", "p1": [22, -16], "p2": [22, -4], "id": "l2"},
            {"type": "line", "p1": [22, -4], "p2": [-10, -4], "id": "l3"},
            {"type": "line", "p1": [-10, -4], "p2": [-10, 16], "id": "l4"},
            {"type": "line", "p1": [-10, 16], "p2": [-22, 16], "id": "l5"},
            {"type": "line", "p1": [-22, 16], "p2": [-22, -16], "id": "l6"},
        ], plane="plane.top", name="BracketSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Bracket",
            "params": {"mode": "boss", "depth": dim(10)},
            "refs": {"profile": [region("f1", 0, -10)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [10, -10], "radius": 4.5, "id": "h1"},
        ], plane="plane.top", name="Hole1Sk", fid="f3", offset=10),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Hole1",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f3", 10, -10)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [-16, -10], "radius": 4.5, "id": "h2"},
        ], plane="plane.top", name="Hole2Sk", fid="f5", offset=10),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Hole2",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f5", -16, -10)]}}},
        grey("b1"),
    ]},

    # ws_drawing: face-on grey elevation plate, unchanged.
    "ws_drawing": {"yaw": 0, "pitch": 0, "steps": [
        *sketch_with_id(rect(-24, -16, 24, 16, "pl"), plane="plane.front",
                        name="PlateSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Plate",
            "params": {"mode": "boss", "depth": dim(8)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id(rect(-9, -4, 9, 4, "s"), plane="plane.front",
                        name="SlotSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Slot",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f3", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [-17, 0], "radius": 3, "id": "bh1"},
        ], plane="plane.front", name="Bolt1Sk", fid="f5"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Bolt1",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f5", -17, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [17, 0], "radius": 3, "id": "bh2"},
        ], plane="plane.front", name="Bolt2Sk", fid="f7"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Bolt2",
            "params": {"mode": "cut", "depth": dim(12)},
            "refs": {"profile": [region("f7", 17, 0)]}}},
        {"op": "set_appearance", "id": "b1", "color": [0.62, 0.66, 0.70],
         "metallic": 0.1, "roughness": 0.6},
    ]},

    # ws_parts: steel hex-head bolt, unchanged.
    "ws_parts": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [14, 0], "p2": [7, 12.12], "id": "hx1"},
            {"type": "line", "p1": [7, 12.12], "p2": [-7, 12.12], "id": "hx2"},
            {"type": "line", "p1": [-7, 12.12], "p2": [-14, 0], "id": "hx3"},
            {"type": "line", "p1": [-14, 0], "p2": [-7, -12.12], "id": "hx4"},
            {"type": "line", "p1": [-7, -12.12], "p2": [7, -12.12], "id": "hx5"},
            {"type": "line", "p1": [7, -12.12], "p2": [14, 0], "id": "hx6"},
        ], plane="plane.top", name="HexSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Head",
            "params": {"mode": "boss", "depth": dim(9)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 6.5, "id": "sk1"},
        ], plane="plane.top", name="ShankSk", fid="f3", offset=9),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Shank",
            "params": {"mode": "boss", "depth": dim(30)},
            "refs": {"profile": [region("f3", 0, 0)]}}},
        *sketch_with_id([
            {"type": "line", "p1": [0, 0], "p2": [0, 42], "id": "axis1",
             "construction": True},
            {"type": "line", "p1": [5.5, 30], "p2": [6.6, 30], "id": "g1a"},
            {"type": "line", "p1": [6.6, 30], "p2": [6.6, 32], "id": "g1b"},
            {"type": "line", "p1": [6.6, 32], "p2": [5.5, 32], "id": "g1c"},
            {"type": "line", "p1": [5.5, 32], "p2": [5.5, 30], "id": "g1d"},
        ], plane="plane.front", name="Groove1Sk", fid="f5"),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Groove1",
            "params": {"mode": "cut", "angle": dim(360)},
            "refs": {"profile": [region("f5", 6.0, 31)], "axis": datum("axis.z")}}},
        *sketch_with_id([
            {"type": "line", "p1": [0, 0], "p2": [0, 42], "id": "axis2",
             "construction": True},
            {"type": "line", "p1": [5.5, 35], "p2": [6.6, 35], "id": "g2a"},
            {"type": "line", "p1": [6.6, 35], "p2": [6.6, 37], "id": "g2b"},
            {"type": "line", "p1": [6.6, 37], "p2": [5.5, 37], "id": "g2c"},
            {"type": "line", "p1": [5.5, 37], "p2": [5.5, 35], "id": "g2d"},
        ], plane="plane.front", name="Groove2Sk", fid="f7"),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Groove2",
            "params": {"mode": "cut", "angle": dim(360)},
            "refs": {"profile": [region("f7", 6.0, 36)], "axis": datum("axis.z")}}},
        {"op": "set_appearance", "id": "b1", "color": [0.70, 0.72, 0.75],
         "metallic": 0.9, "roughness": 0.35},
    ]},
}


ANN: dict[str, dict] = {
    # extrude_boss now carries a REAL 3D arrow body (arrow3d in its recipe); a flat
    # overlay on top of it would double the cue and fight the perspective.
    "extrude_boss":     {},
    "extrude_cut":      {"arrow": [0.50, 0.06, 0.50, 0.44]},
    "revolve_boss":     {"arc": [0.50, 0.22, 0.17, 210, 330]},
    "revolve_cut":      {"arc": [0.50, 0.22, 0.17, 210, 330]},
    "sweep":            {"arrow": [0.68, 0.52, 0.68, 0.16]},
    "sweep_cut":        {"arrow": [0.22, 0.36, 0.72, 0.36]},
    "loft":             {"arrow": [0.85, 0.72, 0.85, 0.24]},
    "loft_cut":         {"arrow": [0.50, 0.10, 0.50, 0.42]},
    "draft":            {"arrow": [0.30, 0.30, 0.20, 0.66]},
    "rib":              {"ring": [0.45, 0.48, 0.16]},
    "hole_wizard":      {"ring": [0.50, 0.30, 0.14]},
    "circular_pattern": {"arc": [0.50, 0.48, 0.34, -50, 190]},
    "curve_helix":      {"arc": [0.50, 0.26, 0.18, 220, 320]},
    "linear_pattern":   {"arrow": [0.24, 0.28, 0.78, 0.28]},
    "mirror":           {"dash": [0.10, 0.63, 0.94, 0.38]},
    "combine":          {"badge": "+"},
    "subtract":         {"badge": "-"},
    "intersect":        {"badge": "n"},
    "fillet":           {"ring": [0.23, 0.48, 0.14]},
    "chamfer":          {"ring": [0.23, 0.48, 0.14]},
    "shell":            {"arrow": [0.50, 0.08, 0.50, 0.38]},
    "sm_edge_flange":   {"arc": [0.62, 0.56, 0.18, 200, 330]},
    "sm_flat_pattern":  {"darrow": [0.20, 0.42, 0.80, 0.42]},
    # split / stock / thread / sm_base_flange / sm_hem / sm_jog / weld_* / ws_*:
    # the geometry itself is the icon -- no overlay.
}
