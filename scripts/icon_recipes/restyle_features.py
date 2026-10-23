"""Icon recipes: the Feature/Dress-up/Multi-body/SheetMetal/Weldment/Workspace tiles.

Style law (re-cut for the two-role accent scheme):
  * steel grey     = the base/stock material the tool acted on,
  * PRIMARY orange = what the operation PRODUCED,
  * SECONDARY blue = the direction of the action, built as REAL 3D geometry (arrow3d /
    arc_arrow3d) wherever that direction lies on a global datum axis, so the cue is shaded
    and foreshortened by the same camera as the part instead of stuck on the glass in
    front of it.  Tiles that took a flat overlay instead keep an empty ANN entry below.

Two engine facts shape nearly every recipe here, and either one taken for granted yields a
tile that builds clean and renders wrong:
  * Body ordinals count SURVIVING solids in production order -- "solid results whose id was
    never consumed by a later feature" (document.cpp).  A consuming feature (cut, boolean,
    pattern, fillet/chamfer/shell, thread, flatten) re-appends its result at the END of that
    order, so arrows have to be built AFTER it or the part slides down the numbering.
  * scripts/check_icon_recipes.py counts one body per body-producing FEATURE, which is the
    same number only when nothing was consumed.  Where the two genuinely disagree the recipe
    says which is which; everywhere else they are kept in step on purpose.
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
    # material went (3D arrows pointing out of the part in the direction of the action).
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

    # extrude_cut: grey block, pocket plunged into its top face, and a real 3D BLUE arrow
    # driving DOWN the pocket axis (arrow3d direction=-1).  It is the CUT direction, which
    # is the whole difference between this tile and extrude_boss -- the same tile with the
    # arrow the other way up would say "boss".  The head straddles the rim on purpose:
    # sunk to the pocket floor the shaded cone is swallowed by the pocket walls at this
    # pitch, held clear above the block it stops reading as a plunge at all.
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
        # f5 = shaft sketch, f6 = shaft extrude, f7 = head sketch, f8 = head revolve.
        # Built after the cut: the cut consumes the block and re-appends the result, so an
        # arrow made earlier would take b1/b2 and leave the block on b3.
        *arrow3d("f5", "f7", base=50, length=30, direction=-1,
                 radius=3.2, head_len=15, head_radius=8.5),
        grey("b1"),
        accent2("b2"), accent2("b3"),
    ]},

    # revolve_boss: ORANGE revolved cup + thin GREY axis rod proud of both ends, with a BLUE
    # arc arrow wrapping that same axis above it -- the turn that swept the profile round.
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
        # f5 = tube sketch, f6 = tube revolve, f7 = head sketch, f8 = head revolve.
        # radius 24 clears the r20 cup wall, height 30 clears the rod tip at z=28 -- an arc
        # that grazes either one fuses with it visually and stops reading as an arrow.
        *arc_arrow3d("f5", "f7", radius=24, height=30),
        accent("b1"),
        grey("b2"),
        accent2("b3"), accent2("b4"),
    ]},

    # revolve_cut: grey barrel with a revolved groove + the same BLUE rotational cue.
    # The groove is cut deep (r20 -> r11) and tall (z14..26) on purpose: the shallow 3mm
    # version left two faint bands that downsampled into a stack of discs rather than one
    # barrel with a waist, and a cut you cannot see is not an icon for cutting.
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
            {"type": "line", "p1": [11, 14], "p2": [20, 14], "id": "g1"},
            {"type": "line", "p1": [20, 14], "p2": [20, 26], "id": "g2"},
            {"type": "line", "p1": [20, 26], "p2": [11, 26], "id": "g3"},
            {"type": "line", "p1": [11, 26], "p2": [11, 14], "id": "g4"},
        ], plane="plane.front", name="GrooveSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "Groove1",
            "params": {"mode": "cut", "angle": dim(360)},
            "refs": {"profile": [region("f3", 15, 20)], "axis": datum("axis.z")}}},
        # f5 = tube sketch, f6 = tube revolve, f7 = head sketch, f8 = head revolve.
        # After the groove cut, which consumes the barrel and re-appends it.
        *arc_arrow3d("f5", "f7", radius=26, height=48),
        grey("b1"),
        accent2("b2"), accent2("b3"),
    ]},

    # sweep: ORANGE pipe swept along an L path that ENDS on the +Y axis, with a real 3D BLUE
    # arrow carrying on off that end -- the way the profile travelled.
    #
    # The path was re-cut to land on a datum: arrow3d revolves its head about a global datum
    # axis, all of which pass through the origin, so an arrow can only lie ON x, y or z.  The
    # profile rides the plane.right sketch out to x=-22 so it still sits at the path start.
    #
    # Proportions matter more here than anywhere else in the file. The tile is fitted to the
    # WHOLE bounding box, arrow included, so a long arrow on a long path shrinks the pipe
    # until it is a two-pixel thread at ribbon size. Legs cut to 22/20 and the arrow to 19 to
    # keep the box near square, and the profile opened to r=7 so the pipe still reads as a
    # swept solid rather than a wire.
    "sweep": {"yaw": 40, "pitch": 28, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 7, "id": "c1"},
        ], plane="plane.right", name="SwProf", fid="f1", offset=-22),
        *sketch_with_id([
            {"type": "line", "p1": [-22, 0], "p2": [0, 0], "id": "p1"},
            {"type": "line", "p1": [0, 0], "p2": [0, 20], "id": "p2"},
        ], plane="plane.top", name="SwPath", fid="f2"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Pipe1",
            "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 0, 0)], "path": region("f2", -11, 0)}}},
        # f4 = shaft sketch, f5 = shaft extrude, f6 = head sketch, f7 = head revolve
        *arrow3d("f4", "f6", axis="y", base=27, length=19,
                 radius=3.4, head_len=10, head_radius=8.5),
        accent("b1"),
        accent2("b2"), accent2("b3"),
    ]},

    # sweep_cut: grey block with a square channel swept clean THROUGH it along the X axis,
    # and a BLUE arrow leaving the far mouth on that same axis.  Block and channel are both
    # centred on z so the channel lies on the X datum -- the arrow has to share it, and a
    # channel cut across the top face (the old layout) leaves nothing for it to come out of.
    "sweep_cut": {"yaw": 40, "pitch": 28, "steps": [
        *sketch_with_id(rect(-24, -16, 24, 16, "b"), plane="plane.top",
                        name="BaseSk", fid="f1", offset=-11),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Base1",
            "params": {"mode": "boss", "depth": dim(22)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id(rect(-5, -5, 5, 5, "c"), plane="plane.right",
                        name="CutProf", fid="f3", offset=-30),
        *sketch_with_id([
            {"type": "line", "p1": [-30, 0], "p2": [30, 0], "id": "p1"},
        ], plane="plane.top", name="CutPath", fid="f4"),
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "SlotCut",
            "params": {"mode": "cut"},
            "refs": {"profile": [region("f3", 0, 0)], "path": region("f4", 0, 0)}}},
        # f6 = shaft sketch, f7 = shaft extrude, f8 = head sketch, f9 = head revolve
        *arrow3d("f6", "f8", axis="x", base=34, length=28,
                 radius=3.2, head_len=14, head_radius=8.5),
        grey("b1"),
        accent2("b2"), accent2("b3"),
    ]},

    # loft: ORANGE blend between a GREY bottom disc and a GREY top square plate (the two end
    # profiles stay visible as thin grey outline plates), with a BLUE arrow rising off the
    # top section along the axis the sections stack on.
    #
    # The blend is 30 tall, not 50. The tile is fitted to the whole bounding box, so the old
    # 50mm column plus a 24mm arrow made an 84x42 tower that shrank to a two-pixel-wide
    # stick at ribbon size — and the round-to-square transition, which is the ONLY thing
    # that says "loft" rather than "extrude", was the first detail to disappear.
    "loft": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 20, "id": "c1"},
        ], plane="plane.top", name="LoftBot", fid="f1"),
        *sketch_with_id(rect(-12, -12, 12, 12, "s"), plane="plane.top",
                        name="LoftTop", fid="f2", offset=30),
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
        # top profile plate (grey), just above the top section
        *sketch_with_id(rect(-15, -15, 15, 15, "pt"), plane="plane.top",
                        name="TopPlate", fid="f6", offset=30),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "TopPlate",
            "params": {"mode": "boss", "depth": dim(3)}, "merge": False,
            "refs": {"profile": [region("f6", 0, 0)]}}},
        # f8 = shaft sketch, f9 = shaft extrude, f10 = head sketch, f11 = head revolve.
        # Short and blunt: it only has to say "upward", and every millimetre it adds is a
        # millimetre the blend itself loses once the tile is fitted to a square cell.
        *arrow3d("f8", "f10", base=38, length=20,
                 radius=3.2, head_len=10, head_radius=8.0),
        accent("b1"),
        grey("b2"),
        grey("b3"),
        accent2("b4"), accent2("b5"),
    ]},

    # loft_cut: grey block with a lofted taper pocket + a BLUE arrow plunging down it.
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
        # f6 = shaft sketch, f7 = shaft extrude, f8 = head sketch, f9 = head revolve.
        # Head crosses the z=36 mouth: r8.5 against the r16 opening, so it enters the pocket
        # without touching its wall.
        *arrow3d("f6", "f8", base=54, length=30, direction=-1,
                 radius=3.2, head_len=15, head_radius=8.5),
        grey("b1"),
        accent2("b2"), accent2("b3"),
    ]},

    # draft: GREY straight-walled pad + ORANGE drafted block.  The taper is pushed to 24 deg
    # over 30 mm -- the old 18 deg over 28 shrank the top by 9 mm a side, which is under two
    # pixels once the tile is 32 wide, i.e. an invisible draft on the draft icon.  24 deg is
    # as far as it goes before the 40 mm width closes to a knife edge (40 - 2*30*tan24 = 13).
    "draft": {"yaw": 35, "pitch": 22, "steps": [
        *sketch_with_id(rect(-31, -25, 31, 25, "b"), plane="plane.top",
                        name="StraightSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "StraightBlock",
            "params": {"mode": "boss", "depth": dim(8)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id(rect(-26, -20, 26, 20, "d"), plane="plane.top",
                        name="DraftSk", fid="f3", offset=8),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "DraftBlock",
            "params": {"mode": "boss", "depth": dim(30), "draft": dim(24)},
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
    # linear_pattern: GREY plate + ORANGE patterned bosses + a real 3D BLUE arrow carrying
    # on along the +X pattern direction.  Plate and bosses are dropped 20 mm so the boss row
    # straddles z=0: an arrow can only lie ON a global datum axis, and one running along the
    # plate's underside would read as a loose part floating below the tile.
    "linear_pattern": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-32, -14, 32, 14, "b"), plane="plane.top",
                        name="PlateSk", fid="f1", offset=-20),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Plate",
            "params": {"mode": "boss", "depth": dim(12)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [-20, 0], "radius": 7, "id": "c1"},
        ], plane="plane.top", name="BossSk", fid="f3", offset=-8),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss",
            "params": {"mode": "boss", "depth": dim(12)}, "merge": False,
            "refs": {"profile": [region("f3", -20, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "linear_pattern", "name": "LinPat",
            "params": {"count1": 3, "spacing1": dim(20)},
            "refs": {"dir1": datum("axis.x"), "features": ["f4"]}}},
        # f6 = shaft sketch, f7 = shaft extrude, f8 = head sketch, f9 = head revolve.
        # After the pattern, which consumes the seed boss and re-appends the instanced body.
        # Tail at x=37 clears the last instance (x=20, r7) without a visible gap.
        *arrow3d("f6", "f8", axis="x", base=37, length=26,
                 radius=3.2, head_len=13, head_radius=8.5),
        grey("b1"),
        accent("b2"),
        accent2("b3"), accent2("b4"),
    ]},

    # circular_pattern: GREY disc + ORANGE patterned spokes + a BLUE arc arrow turning about
    # the same axis the pattern revolves around, clear of the r22 rim and the spoke tops.
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
        # f6 = tube sketch, f7 = tube revolve, f8 = head sketch, f9 = head revolve.
        # After the pattern, which consumes the seed spoke and re-appends the wheel.
        *arc_arrow3d("f6", "f8", radius=28, height=18),
        grey("b1"),
        accent("b2"),
        accent2("b3"), accent2("b4"),
    ]},

    # mirror: an asymmetric stepped boss and its reflection across the front plane, built as
    # TWO separate bodies with a gap either side of y=0, so both instances are legible and
    # the copy can carry the PRIMARY accent.  The dashed symmetry trace (ANN) stays: the
    # plane itself is a marker, not a direction, and has no 3-space form to shade.
    #
    # Why the `mirror` feature is not used: it only survives on a MERGED body, and
    # the instance it makes fuses into the seed -- one body, one appearance, so the copy is
    # indistinguishable from the original in the render, which is the only thing this icon
    # has to say.  Same trade as the rib tile's hand-built web.
    "mirror": {"yaw": 35, "pitch": 32, "steps": [
        *sketch_with_id([
            {"type": "line", "p1": [-20, 4], "p2": [20, 4], "id": "s1"},
            {"type": "line", "p1": [20, 4], "p2": [20, 10], "id": "s2"},
            {"type": "line", "p1": [20, 10], "p2": [0, 10], "id": "s3"},
            {"type": "line", "p1": [0, 10], "p2": [0, 20], "id": "s4"},
            {"type": "line", "p1": [0, 20], "p2": [-20, 20], "id": "s5"},
            {"type": "line", "p1": [-20, 20], "p2": [-20, 4], "id": "s6"},
        ], plane="plane.top", name="SeedSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Seed",
            "params": {"mode": "boss", "depth": dim(14)}, "merge": False,
            "refs": {"profile": [region("f1", -10, 12)]}}},
        *sketch_with_id([
            {"type": "line", "p1": [-20, -4], "p2": [20, -4], "id": "m1"},
            {"type": "line", "p1": [20, -4], "p2": [20, -10], "id": "m2"},
            {"type": "line", "p1": [20, -10], "p2": [0, -10], "id": "m3"},
            {"type": "line", "p1": [0, -10], "p2": [0, -20], "id": "m4"},
            {"type": "line", "p1": [0, -20], "p2": [-20, -20], "id": "m5"},
            {"type": "line", "p1": [-20, -20], "p2": [-20, -4], "id": "m6"},
        ], plane="plane.top", name="CopySk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Copy",
            "params": {"mode": "boss", "depth": dim(14)}, "merge": False,
            "refs": {"profile": [region("f3", -10, -12)]}}},
        grey("b1"),
        accent("b2"),
    ]},

    # ----------------------------------------------------------- Multi-body
    # The three boolean tiles show their OPERANDS -- GREY target, SECONDARY-blue tool -- and
    # let the +/-/n badge name the operation.
    #
    # Why the `boolean` feature itself is not run: it consumes BOTH operands (document.cpp
    # drops a_id and b_id into `consumed`) and only unconsumed solids are tessellated, so a
    # rendered boolean is one body wearing one colour.  Union, difference and intersection of
    # the same two shapes would then differ by a badge and nothing else, and the one thing
    # these icons have to say -- which shape went in where -- would be gone.  Each tile is
    # therefore two distinct solids arranged the way that operation reads, plus the badge.
    "combine": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-20, -14, 20, 14, "b"), plane="plane.top",
                        name="Body1Sk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Body1",
            "params": {"mode": "boss", "depth": dim(24)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Tool set into the block's flank so the pair overlaps -- two shapes about to become
        # one, which is what union means.
        *sketch_with_id([
            {"type": "circle", "center": [16, 0], "radius": 14, "id": "c1"},
        ], plane="plane.top", name="Body2Sk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Body2",
            "params": {"mode": "boss", "depth": dim(24)}, "merge": False,
            "refs": {"profile": [region("f3", 16, 0)]}}},
        grey("b1"),
        # The tool takes the PRIMARY, not the secondary: secondary is reserved for
        # direction cues, and painting a solid operand with it would make the one blue on
        # this tile mean two different things. Orange operand, blue badge.
        accent("b2"),
    ]},

    "subtract": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-22, -16, 22, 16, "b"), plane="plane.top",
                        name="TgtSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Target",
            "params": {"mode": "boss", "depth": dim(28)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        # Tool driven straight down through the target and standing 6 mm proud of it: the
        # plug about to be taken out, which is what difference means.
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 11, "id": "c1"},
        ], plane="plane.top", name="ToolSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Tool",
            "params": {"mode": "boss", "depth": dim(34)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        grey("b1"),
        # The tool takes the PRIMARY, not the secondary: secondary is reserved for
        # direction cues, and painting a solid operand with it would make the one blue on
        # this tile mean two different things. Orange operand, blue badge.
        accent("b2"),
    ]},

    # intersect completes the trio. It was the one boolean still running the real `boolean`
    # feature, which consumes both operands and leaves a single body wearing a single
    # colour — so the tile said nothing the badge did not already say, and neither operand
    # got painted at all (the engine default material is why it read as a flat blue box).
    # Same treatment as its siblings: show the two solids overlapping, let the badge name
    # the operation. The overlap is deliberately large, because intersection is ABOUT the
    # shared volume and a token clip would not show one.
    "intersect": {"yaw": 35, "pitch": 26, "steps": [
        *sketch_with_id(rect(-22, -16, 10, 16, "b"), plane="plane.top",
                        name="BlkSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Block",
            "params": {"mode": "boss", "depth": dim(26)}, "merge": False,
            "refs": {"profile": [region("f1", -6, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [6, 0], "radius": 15, "id": "c1"},
        ], plane="plane.top", name="CylSk", fid="f3"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Cyl",
            "params": {"mode": "boss", "depth": dim(26)}, "merge": False,
            "refs": {"profile": [region("f3", 6, 0)]}}},
        grey("b1"),
        accent("b2"),
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
    # fillet and chamfer used to be the same grey box carrying the same small ring: at 32 px
    # that is one icon printed twice.  Both now dress EVERY edge hard enough to change the
    # silhouette -- a rounded soap bar against a faceted gem -- and the dressed body takes the
    # PRIMARY accent, because a dress-up feature's product IS the body it transformed.
    # Appearance is per BODY, not per face (icon_pipeline's "per-body v1"), so the treated
    # edge cannot be picked out on its own; the grey pad underneath supplies the stock role
    # and the tonal contrast that sends the eye to the dressed edges.
    #
    # Two ordering rules are load-bearing here:
    #   * the dress-up step carries an explicit "op" so check_icon_recipes counts its feature
    #     id.  The runner turns ANY step with a "feature" into an add_feature whether or not
    #     "op" is present (scenarios.cpp runIconPipe), so a validator blind to it would number
    #     every later sketch one too low and pass a recipe the engine mis-builds.
    #   * it runs BEFORE the pad exists, because it consumes the box and re-appends the
    #     dressed result; dressing after the pad would leave the pad on b1.
    "fillet": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"op": "add_feature",
         "feature": {"type": "fillet", "name": "Fillet1",
                     "params": {"radius": dim(8.0)}},
         "selrefs": {"edges": {"select": "all_edges", "n": 12}}},
        *sketch_with_id(rect(-32, -25, 32, 25, "pd"), plane="plane.top",
                        name="PadSk", fid="f3", offset=-21),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Pad",
            "params": {"mode": "boss", "depth": dim(6)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        accent("b1"),
        grey("b2"),
    ]},

    "chamfer": {"yaw": 38, "pitch": 24, "steps": [
        box(52, 40, 30),
        {"op": "add_feature",
         "feature": {"type": "chamfer", "name": "Chamfer1",
                     "params": {"length": dim(7.0)}},
         "selrefs": {"edges": {"select": "all_edges", "n": 12}}},
        *sketch_with_id(rect(-32, -25, 32, 25, "pd"), plane="plane.top",
                        name="PadSk", fid="f3", offset=-21),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Pad",
            "params": {"mode": "boss", "depth": dim(6)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        accent("b1"),
        grey("b2"),
    ]},

    # shell: ORANGE hollowed box with a BLUE arrow driving down into the cavity it opened.
    # The head starts exactly at the z=15 rim and ends mid-cavity, so the plunge is read
    # against the wall it just went past rather than floating above the part.
    "shell": {"yaw": 40, "pitch": 34, "steps": [
        box(52, 40, 30),
        {"op": "add_feature",
         "feature": {"type": "shell", "name": "Shell1",
                     "params": {"thickness": dim(3.0)}},
         "selrefs": {"remove": {"select": "top_face", "n": 1}}},
        # f3 = shaft sketch, f4 = shaft extrude, f5 = head sketch, f6 = head revolve.
        # After the shell, which consumes the box and re-appends the hollow: build the arrow
        # first and the hollow lands on b3 while the shaft takes b1.
        *arrow3d("f3", "f5", base=32, length=32, direction=-1,
                 radius=3.0, head_len=15, head_radius=8.0),
        accent("b1"),
        accent2("b2"), accent2("b3"),
    ]},

    # stock: raw aluminium chudware, unchanged.
    "stock": {"yaw": 32, "pitch": 24, "steps": [
        box(56, 40, 26),
        appearance(0.90, 0.32, (0.80, 0.82, 0.85)),
    ]},

    # --------------------------------------------------------------- Curves
    # curve_helix: the spring IS the visible form of the curve this feature produces, so it
    # takes the PRIMARY accent, and a BLUE arc arrow above it gives the wind direction.  Coil
    # and wire are both scaled up: a 1.5 mm wire on a 20 mm coil is a sub-pixel scribble once
    # the tile is downsampled to 32, which is the whole reason this tile read as a smudge.
    #
    # Body numbering diverges here, uniquely in this file.  `helix` stores a CurveResult and
    # pushes NOTHING onto solid_order (document.cpp: "Stores a CurveResult a sweep consumes"),
    # so the engine's b1 is the SWEPT SPRING and the arrow lands on b2/b3.  check_icon_recipes
    # counts helix as a solid and therefore names one body more than exists; the trailing
    # accent2("b4") covers that phantom id, so the tile is provably fully painted under either
    # numbering and neither reader is left wondering which body was missed.
    "curve_helix": {"yaw": 35, "pitch": 30, "steps": [
        *sketch_with_id([
            {"type": "circle", "center": [14, 0], "radius": 3, "id": "c1"},
        ], plane="plane.front", name="WireProf", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "helix", "name": "CoilPath",
            "params": {"pitch": dim(9), "height": dim(36), "radius": dim(14)},
            "refs": {"base": "plane.top"}}},
        {"op": "add_feature", "feature": {
            "type": "sweep", "name": "Spring",
            "params": {"mode": "boss"},
            "refs": {"profile": [region("f1", 14, 0)], "path": "f2"}}},
        # f4 = tube sketch, f5 = tube revolve, f6 = head sketch, f7 = head revolve
        *arc_arrow3d("f4", "f6", radius=24, height=46),
        accent("b1"),
        accent2("b2"), accent2("b3"), accent2("b4"),
    ]},

    # --------------------------------------------------------- Hole/Thread
    # thread: a real helical thread CUT into a rod by the engine's one-op `thread` feature.
    # The rod is its own body (merge False) so it can carry the PRIMARY accent while the boss
    # it stands on stays stock grey -- appearance is per body, so this is as close as the
    # renderer gets to "orange thread on a grey shank", and it is the difference between this
    # tile and the all-grey one it replaces, which had no accent at all.
    #
    # The boss is deliberately SQUARE: target_face picks the largest CYLINDRICAL face in the
    # model, and a second round body would put that pick up for grabs.
    "thread": {"yaw": 35, "pitch": 24, "steps": [
        *sketch_with_id(rect(-11, -11, 11, 11, "bs"), plane="plane.top",
                        name="BossSk", fid="f1"),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "SquareBoss",
            "params": {"mode": "boss", "depth": dim(12)}, "merge": False,
            "refs": {"profile": [region("f1", 0, 0)]}}},
        *sketch_with_id([
            {"type": "circle", "center": [0, 0], "radius": 7.5, "id": "sc1"},
        ], plane="plane.top", name="StudSk", fid="f3", offset=12),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Stud",
            "params": {"mode": "boss", "depth": dim(34)}, "merge": False,
            "refs": {"profile": [region("f3", 0, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "thread", "name": "Thread",
            "params": {"major_dia": dim(15), "pitch": dim(3.5),
                       "style": "external", "mode": "cut", "form": "v",
                       "turns": dim(9)},
            "refs": {"target_face": {"of": "face", "kind": "cylindrical",
                                     "pick": "largest"}}}},
        grey("b1"),
        accent("b2"),
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

    # sm_flat_pattern: the folded part laid out flat, with two real 3D BLUE arrows fanning
    # out along +X and +Y -- the unfold, in the plane it unfolds into.
    # The blank is dropped half its thickness so both datum axes run through its mid-plane:
    # an arrow can only lie ON a global axis, and one skimming the underside of the sheet
    # would read as a loose part beside it.  Two orthogonal arrows also keep the bounding box
    # near square, where a single opposed pair along X squashed the tile into a strip.
    # Pitch is 62, not straight down: at 90 the sheet's top face takes the full key light
    # and the tile is a flat bright rectangle with no thickness and no depth cue at all.
    "sm_flat_pattern": {"yaw": 22, "pitch": 62, "steps": [
        *sketch_with_id(rect(-25, -20, 25, 20, "b"), plane="plane.top",
                        name="FlangeSk", fid="f1", offset=-1),
        {"op": "add_feature", "feature": {
            "type": "sm_base_flange", "name": "BaseFlange",
            "params": {"thickness": dim(2)},
            "refs": {"profile": [region("f1", 0, 0)]}}},
        {"op": "add_feature", "feature": {
            "type": "sm_flat_pattern", "name": "Flat", "params": {}}},
        # f3/f5 = +X arrow sketches, f7/f9 = +Y arrow sketches; 4 features and 2 bodies
        # each.  sm_flat_pattern above takes NO slot in the f-numbering — it flattens the
        # existing flange in place rather than adding to the tree — so everything after it
        # shifts down by one. Proven by probe render: naming the next sketch f4 makes its
        # sketch_add_entity return ok=0 while the sketch itself still reports ok=1, which
        # is why this only showed up as a missing arrow rather than an error.
        *arrow3d("f3", "f5", axis="x", base=31, length=24,
                 radius=3.0, head_len=12, head_radius=8.0),
        *arrow3d("f7", "f9", axis="y", base=26, length=24,
                 radius=3.0, head_len=12, head_radius=8.0),
        # The flattened blank IS what this operation produced, so it takes the primary.
        accent("b1"),
        accent2("b2"), accent2("b3"),
        accent2("b4"), accent2("b5"),
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

    # ws_parts: steel hex-head bolt.  The shank is kept as its OWN body (merge False) rather
    # than fusing into the head: fused, the engine has one body while check_icon_recipes
    # counts two, and the second id it names cannot be painted because it does not exist.
    # Unfused, both numberings agree and both halves are explicitly steel.  The groove cuts
    # still land on the shank -- a cut takes the most-recently-produced unconsumed solid.
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
            "params": {"mode": "boss", "depth": dim(30)}, "merge": False,
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
        {"op": "set_appearance", "id": "b2", "color": [0.70, 0.72, 0.75],
         "metallic": 0.9, "roughness": 0.35},
    ]},
}


ANN: dict[str, dict] = {
    # Tiles whose cue is now REAL geometry keep their key with an EMPTY spec, rather than
    # losing it: load_tile skips a falsy spec, and the key staying here records that the flat
    # overlay was retired on purpose instead of quietly dropped.  A flat arrow painted over a
    # shaded 3D one doubles the cue and fights the perspective the render just established.
    "extrude_boss":     {},   # arrow3d +Z, out of the boss
    "extrude_cut":      {},   # arrow3d -Z, into the pocket
    "revolve_boss":     {},   # arc_arrow3d about Z
    "revolve_cut":      {},   # arc_arrow3d about Z
    "sweep":            {},   # arrow3d +Y, off the end of the path
    "sweep_cut":        {},   # arrow3d +X, out of the channel
    "loft":             {},   # arrow3d +Z, on through the stacked sections
    "loft_cut":         {},   # arrow3d -Z, into the tapered pocket
    "linear_pattern":   {},   # arrow3d +X, along the pattern direction
    "circular_pattern": {},   # arc_arrow3d about the pattern axis
    "curve_helix":      {},   # arc_arrow3d, the wind direction
    "shell":            {},   # arrow3d -Z, into the hollow
    "sm_flat_pattern":  {},   # arrow3d +X and +Y, the unfold
    # Still flat, and rightly so.  A ring is a spotlight, a badge names an operation and a
    # dash traces a plane: none of them is a direction, so none has a 3-space form to shade.
    # The draft cue is the exception that proves the rule -- it IS a direction, but the pull
    # runs down a tapered wall, not along a datum axis, and arrows only build on the datums.
    "draft":            {"arrow": [0.30, 0.30, 0.20, 0.66]},
    "rib":              {"ring": [0.45, 0.48, 0.16]},
    "hole_wizard":      {"ring": [0.50, 0.30, 0.14]},
    "mirror":           {"dash": [0.10, 0.63, 0.94, 0.38]},
    "combine":          {"badge": "+"},
    "subtract":         {"badge": "-"},
    "intersect":        {"badge": "n"},
    "fillet":           {"ring": [0.23, 0.48, 0.14]},
    "chamfer":          {"ring": [0.23, 0.48, 0.14]},
    "sm_edge_flange":   {"arc": [0.62, 0.56, 0.18, 200, 330]},
    # split / stock / thread / sm_base_flange / sm_hem / sm_jog / weld_* / ws_*:
    # the geometry itself is the icon -- no overlay.
}
