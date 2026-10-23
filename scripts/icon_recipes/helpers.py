"""Recipe-builder vocabulary for the icon pipeline — the SINGLE source for these
helpers. scripts/icon_pipeline.py re-exports them (its historical RECIPES table calls
them at import time); the per-group modules in this package import them from here.

Every function returns plain step dicts for the iconpipe runner (CHUDWARE_SHOT=iconpipe):
either a raw {"op": ...} applied verbatim, or a LIST of steps from the sketch helpers.
"""
from __future__ import annotations


def dim(v: float) -> dict:
    """Engine dimension object: a literal value with no driving expression."""
    return {"value": float(v), "expr": None}


def box(length=52.0, width=40.0, height=30.0, name="Box1") -> dict:
    """A centred rectangular block -- the common base body for the dress-up demos."""
    return {"op": "add_feature", "feature": {
        "type": "box", "name": name,
        "params": {"length": dim(length), "width": dim(width), "height": dim(height)},
        "centered": True}}


def sphere(radius=22.0, name="Ball1") -> dict:
    """A sphere primitive (engine `sphere` feature, radius param) centred at origin --
    the material-preset "ball" so the Appearance tiles read as rounded swatches."""
    return {"op": "add_feature", "feature": {
        "type": "sphere", "name": name,
        "params": {"radius": dim(radius)}}}


# ---------------------------------------------------------------------------------------
# Accent roles.  The atlas bakes these hues into the rendered pixels, and the C++ loader
# (host/src/ui/icons.cpp::recolorIconAtlas) rotates them to the user's chosen colors at
# runtime.  That remap keys off the REFERENCE hues below, so these two constants and
# ui::kIconAccentRef* in host/src/ui/icons.h are ONE contract — change them together.
#
# Two roles, deliberately far apart in hue so the runtime classifier can never confuse a
# primary pixel for a secondary one:
#   PRIMARY   (orange) = what the operation PRODUCED (the shaded result body).
#   SECONDARY (blue)   = the direction/annotation cue (3D arrows, motion indicators).
# Steel-grey stock stays desaturated and is therefore never remapped.
PRIMARY_RGB = (0.949, 0.510, 0.086)    # #F28216 — app primary accent (orange)
SECONDARY_RGB = (0.000, 0.471, 0.843)  # #0078D7 — app secondary accent (blue)

# Back-compat alias: icon_pipeline.py's historical table imports ACCENT_RGB by name.
ACCENT_RGB = PRIMARY_RGB


def accent(body_id: str = "b1") -> dict:
    """Color a body with the PRIMARY accent so the tile highlights what the operation
    produced (per-body emphasis)."""
    return {"op": "set_appearance", "id": body_id, "color": list(PRIMARY_RGB),
            "metallic": 0.05, "roughness": 0.45}


def accent2(body_id: str = "b1") -> dict:
    """Color a body with the SECONDARY accent — reserved for direction cues (the 3D
    arrows built by arrow3d/arc_arrow3d) so they read against a PRIMARY-colored result."""
    return {"op": "set_appearance", "id": body_id, "color": list(SECONDARY_RGB),
            "metallic": 0.05, "roughness": 0.40}


def appearance(metallic: float, roughness: float, rgb: tuple[float, float, float]) -> dict:
    """set_appearance on the first body -- the exact op the m1 scenario uses. Values must
    mirror ui::kMaterials (host/src/ui/appearance_common.cpp) so the icon IS the preset."""
    return {"op": "set_appearance", "id": "b1", "color": list(rgb),
            "metallic": metallic, "roughness": roughness}


def sketch(entities: list, plane: str = "plane.front", name: str = "Sk",
           sketch_id: str | None = None) -> list:
    """Returns a LIST of iconpipe steps: one empty add_feature + one sketch_add_entity
    per entity.  The runner applies each step as its own applyOps call; inline
    params.entities is NOT supported by the engine (the sketch is always created empty,
    then entities are added via sketch_add_entity — verified empirically).
    sketch_id is the persistent feature id the engine will assign (e.g. 'f1') — used so
    sketch_add_entity steps reference the right sketch.  If omitted, callers must pass
    the id via the recipe's steps directly."""
    steps: list = [{"op": "add_feature", "feature": {
        "type": "sketch", "name": name,
        "params": {"entities": [], "constraints": []},
        "refs": {"plane": plane}}}]
    if sketch_id:
        for ent in entities:
            steps.append({"op": "sketch_add_entity", "sketch": sketch_id, "entity": ent})
    else:
        # Caller must add sketch_add_entity steps manually with the correct id,
        # or use sketch_with_id() below.
        for ent in entities:
            steps.append({"_sketch_entity_deferred": True, "entity": ent})
    return steps


def sketch_with_id(entities: list, plane: str, name: str, fid: str,
                   offset: float | None = None) -> list:
    """Returns a LIST of iconpipe steps for a sketch at a known persistent feature id.
    Creates the sketch empty, then adds each entity via sketch_add_entity referencing fid.
    Optional offset (mm) shifts the plane along its normal (for loft sections at height)."""
    feat: dict = {
        "type": "sketch", "name": name,
        "params": {"entities": [], "constraints": []},
        "refs": {"plane": plane},
    }
    if offset is not None:
        feat["offset"] = offset
    steps: list = [{"op": "add_feature", "feature": feat}]
    for ent in entities:
        steps.append({"op": "sketch_add_entity", "sketch": fid, "entity": ent})
    return steps


def datum(axis: str) -> dict:
    """Datum axis selector (axis.x / axis.y / axis.z) for pattern/mirror ops."""
    return {"kind": "datum", "feature": axis}


def region(sketch_id: str, cx: float = 0.0, cy: float = 0.0) -> dict:
    """Region selector for a sketch profile."""
    return {"kind": "region", "sketch": sketch_id, "centroid": [cx, cy], "hint_index": 0}


def axis_edge(sketch_id: str, cx: float, cy: float, cz: float,
              ax: float, ay: float, az: float, length: float = 40.0) -> dict:
    """Line edge selector (for revolve axis) -- a straight edge in the named sketch."""
    return {"kind": "edge", "surf": "line",
            "centroid": [cx, cy, cz], "axis": [ax, ay, az],
            "area_or_len": length, "owner_feature": sketch_id, "hint_index": 0}


def body_sel(feature_id: str) -> dict:
    """Body selector for boolean ops (targets / tools).
    Canonical format: {"kind":"body","owner_feature":"fN"} — see document.cpp line 389."""
    return {"kind": "body", "owner_feature": feature_id}


# ---------------------------------------------------------------------------------------
# 3D direction arrows.  These build REAL geometry in the engine rather than painting a flat
# overlay on the finished PNG, so the arrow is shaded, occluded and foreshortened by the
# same camera as the part — which is the whole point: it has to look like it is pointing
# out of the model in 3-space, not stuck on the glass in front of it.
#
# Construction: a cylindrical shaft (a circle extruded along its sketch plane's normal)
# plus a conical head (a right triangle revolved a full turn about the same axis).  Both
# are separate, non-merged bodies so the caller can paint them with accent2().
#
# Sketch-plane convention, confirmed against the existing recipes and by render:
#   plane.top   = XY, normal +Z.  Sketch (u,v) -> world (x,y).
#   plane.front = XZ, normal +Y.  Sketch (u,v) -> world (x,z).
#   plane.right = YZ, normal +X.  Sketch (u,v) -> world (y,z).
# An extrude therefore runs along the plane's normal, and a revolve about a datum axis
# treats the in-plane coordinate perpendicular to that axis as the radius.
#
# LIMITATION, stated plainly because it shapes every caller: the engine revolves only about
# the global datum axes, which all pass through the origin.  A cone can therefore only be
# built centred on X, Y or Z.  Arrows are consequently available along +/-X, +/-Y and +/-Z
# THROUGH THE ORIGIN, and a tangential arrow (one pointing along a circle's tangent, as a
# true rotation arrowhead would) is not constructible at all — which is why arc_arrow3d
# below flares its tip radially instead of pretending otherwise.
# ---------------------------------------------------------------------------------------

_ARROW_SEQ = [0]

# axis -> (shaft sketch plane, head profile plane, datum axis, axial coord is first?).
#
# The last flag is the one that matters and the one that was wrong first time round: in the
# head's sketch, WHICH of (u,v) runs along the arrow and which is the revolve radius. Get
# it backwards and the triangle revolves into a wide flat disc instead of a cone — it still
# builds, every step still reports ok=1, and you only notice by looking at the render.
#   axis.z, profile on plane.front (u=x, v=z): x is the radius, z runs along  -> False
#   axis.y, profile on plane.top   (u=x, v=y): x is the radius, y runs along  -> False
#   axis.x, profile on plane.top   (u=x, v=y): y is the radius, x runs along  -> True
_ARROW_AXES = {
    "z": ("plane.top",   "plane.front", "axis.z", False),
    "y": ("plane.front", "plane.top",   "axis.y", False),
    "x": ("plane.right", "plane.top",   "axis.x", True),
}


def _arrow_tag() -> str:
    """Unique suffix so several arrows in one recipe never collide on sketch/entity ids."""
    _ARROW_SEQ[0] += 1
    return f"a{_ARROW_SEQ[0]}"


def arrow3d(fid_shaft: str, fid_head: str, *, base: float, length: float,
            axis: str = "z", direction: int = 1,
            radius: float = 3.0, head_len: float | None = None,
            head_radius: float | None = None, offset_axis: float = 0.0) -> list:
    """A solid 3D arrow lying on a global axis, pointing along it.

    `axis` is "x", "y" or "z"; `direction` is +1 or -1.  `base` is where the tail sits (mm
    along that axis, signed world coordinate) and `length` the total arrow length including
    the head, measured in the pointing direction.  `offset_axis` slides the whole arrow
    along the axis without resizing it — how a caller nudges an arrow clear of the body it
    grows out of.

    Adds exactly 4 features (shaft sketch, shaft extrude, head sketch, head revolve) and
    exactly 2 bodies.  `fid_shaft`/`fid_head` are the persistent feature ids the engine will
    assign to the two SKETCHES; scripts/check_icon_recipes.py resolves them statically, so
    run it rather than counting by hand.
    """
    if axis not in _ARROW_AXES:
        raise ValueError(f"arrow3d: axis must be x, y or z (got {axis!r})")
    if direction not in (1, -1):
        raise ValueError(f"arrow3d: direction must be +1 or -1 (got {direction!r})")
    shaft_plane, head_plane, datum_axis, axial_first = _ARROW_AXES[axis]

    hl = head_len if head_len is not None else max(7.0, length * 0.42)
    hr = head_radius if head_radius is not None else radius * 2.6
    shaft_len = max(1.0, length - hl)
    tail = base + offset_axis
    neck = tail + direction * shaft_len       # where the shaft stops and the cone starts
    tip = tail + direction * length

    # The shaft sketch sits at whichever end is lower along the axis, because an extrude
    # only runs in the +normal direction: for a -direction arrow the sketch goes at the
    # NECK and grows back up to the tail.
    shaft_origin = tail if direction > 0 else neck

    def head_pt(along: float, radial: float) -> list:
        """Head-profile point in its sketch's own (u,v), given axial and radial extent."""
        return [along, radial] if axial_first else [radial, along]

    tag = _arrow_tag()
    return [
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": radius,
                          "id": tag + "s"}],
                        plane=shaft_plane, name="ArrShaftSk" + tag, fid=fid_shaft,
                        offset=shaft_origin),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "ArrShaft" + tag,
            "params": {"mode": "boss", "depth": dim(shaft_len)}, "merge": False,
            "refs": {"profile": [region(fid_shaft, 0, 0)]}}},
        # Head: a right triangle with one edge ON the revolve axis (radius 0), so the full
        # turn closes it into a cone rather than a tube.
        *sketch_with_id([
            {"type": "line", "p1": head_pt(neck, 0), "p2": head_pt(neck, hr),
             "id": tag + "h1"},
            {"type": "line", "p1": head_pt(neck, hr), "p2": head_pt(tip, 0),
             "id": tag + "h2"},
            {"type": "line", "p1": head_pt(tip, 0), "p2": head_pt(neck, 0),
             "id": tag + "h3"},
        ], plane=head_plane, name="ArrHeadSk" + tag, fid=fid_head),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "ArrHead" + tag,
            "params": {"mode": "boss", "angle": dim(360)}, "merge": False,
            "refs": {"profile": [region(fid_head,
                                        *head_pt(neck + direction * hl * 0.3, hr * 0.3))],
                     "axis": datum(datum_axis)}}},
    ]


def arc_arrow3d(fid_tube: str, fid_head: str, *, radius: float, height: float,
                tube_radius: float = 3.4, sweep: float = 175.0,
                head_len: float = 16.0, head_radius: float = 10.0,
                head_sweep: float = 46.0) -> list:
    """A curved 3D arrow wrapping the Z axis — the rotational cue for revolve, circular
    patterns, helices and anything else whose action is a turn rather than a push.

    The tube is a circle at (`radius`, `height`) in the front plane swept `sweep` degrees
    about axis.z.  The head is a wedge at the arc's leading end, swept `head_sweep` degrees.

    On the head: a real arrowhead would taper along the SWEEP, and that cannot be built
    here.  A revolve has a constant cross-section by definition, and the engine only
    revolves about the global datum axes, so there is no way to stand a cone up tangent to
    the arc.  What this builds instead is a short wedge that flares RADIALLY well proud of
    the tube — which is the convention low-resolution CAD icons use for a rotation arrow,
    and which does survive the downsample.  The defaults were picked by rendering three
    variants at 32px and taking the one that still read as a turn; a thinner tube or a
    fuller sweep closes the arc into a plain ring and loses the direction entirely.
    """
    tag = _arrow_tag()
    return [
        *sketch_with_id([{"type": "circle", "center": [radius, height],
                          "radius": tube_radius, "id": tag + "t"}],
                        plane="plane.front", name="ArcTubeSk" + tag, fid=fid_tube),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "ArcTube" + tag,
            "params": {"mode": "boss", "angle": dim(sweep)}, "merge": False,
            "refs": {"profile": [region(fid_tube, radius, height)],
                     "axis": datum("axis.z")}}},
        # Head: a radially-flared wedge sharing the tube's start angle, so the arc reads as
        # running from its far end round to this tip.
        *sketch_with_id([
            {"type": "line", "p1": [radius - head_radius, height],
             "p2": [radius + head_radius, height], "id": tag + "c1"},
            {"type": "line", "p1": [radius + head_radius, height],
             "p2": [radius, height + head_len], "id": tag + "c2"},
            {"type": "line", "p1": [radius, height + head_len],
             "p2": [radius - head_radius, height], "id": tag + "c3"},
        ], plane="plane.front", name="ArcHeadSk" + tag, fid=fid_head),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "ArcHead" + tag,
            "params": {"mode": "boss", "angle": dim(head_sweep)}, "merge": False,
            "refs": {"profile": [region(fid_head, radius, height + head_len * 0.3)],
                     "axis": datum("axis.z")}}},
    ]
