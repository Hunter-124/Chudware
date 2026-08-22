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
    produced (T-BL34 per-body emphasis)."""
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
    then entities are added via sketch_add_entity — proven empirically, see T-BL29 log).
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
# Construction: a cylindrical shaft (circle extruded along the plane normal) plus a conical
# head (a right triangle revolved a full turn).  Both are separate, non-merged bodies so
# the caller can paint them with accent2().
#
# Plane/axis convention, as used by the existing recipes:
#   plane.top   sketch (u,v) -> world (x,y); extrude runs along +Z.
#   plane.front sketch (u,v) -> world (u = radius from Z axis, v = z); this is the plane a
#               revolve about axis.z sweeps, so a triangle here becomes a cone on the axis.
# ---------------------------------------------------------------------------------------

_ARROW_SEQ = [0]


def _arrow_tag() -> str:
    """Unique suffix so several arrows in one recipe never collide on sketch/entity ids."""
    _ARROW_SEQ[0] += 1
    return f"a{_ARROW_SEQ[0]}"


def arrow3d(fid_shaft: str, fid_head: str, *, base: float, length: float,
            radius: float = 2.6, head_len: float | None = None,
            head_radius: float | None = None, plane: str = "plane.top",
            offset_axis: float = 0.0) -> list:
    """A solid 3D arrow on the Z axis pointing along +Z (the extrude direction).

    `base` is where the shaft starts (mm along the axis), `length` the total arrow length
    including the head.  `fid_shaft`/`fid_head` are the persistent feature ids the engine
    will assign to the two sketches — the caller must count features to get these right,
    exactly like sketch_with_id elsewhere in this package.

    `offset_axis` shifts the whole arrow along the axis without changing its length, which
    is how a caller nudges the arrow clear of the body it grows out of.
    """
    hl = head_len if head_len is not None else max(6.0, length * 0.40)
    hr = head_radius if head_radius is not None else radius * 2.35
    b = base + offset_axis
    shaft_len = max(1.0, length - hl)
    tip = b + length
    neck = b + shaft_len
    tag = _arrow_tag()
    return [
        # Shaft: a circle on the base plane pushed along the normal.
        *sketch_with_id([{"type": "circle", "center": [0, 0], "radius": radius,
                          "id": tag + "s"}],
                        plane=plane, name="ArrShaftSk" + tag, fid=fid_shaft, offset=b),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "ArrShaft" + tag,
            "params": {"mode": "boss", "depth": dim(shaft_len)}, "merge": False,
            "refs": {"profile": [region(fid_shaft, 0, 0)]}}},
        # Head: right triangle (axis edge -> base radius -> apex) revolved about Z.
        *sketch_with_id([
            {"type": "line", "p1": [0, neck], "p2": [hr, neck], "id": tag + "h1"},
            {"type": "line", "p1": [hr, neck], "p2": [0, tip], "id": tag + "h2"},
            {"type": "line", "p1": [0, tip], "p2": [0, neck], "id": tag + "h3"},
        ], plane="plane.front", name="ArrHeadSk" + tag, fid=fid_head),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "ArrHead" + tag,
            "params": {"mode": "boss", "angle": dim(360)}, "merge": False,
            "refs": {"profile": [region(fid_head, hr * 0.33, neck + (tip - neck) * 0.25)],
                     "axis": datum("axis.z")}}},
    ]


def arc_arrow3d(fid_tube: str, fid_head: str, *, radius: float, height: float,
                tube_radius: float = 2.2, sweep: float = 250.0,
                head_len: float = 11.0, head_radius: float = 5.2) -> list:
    """A curved 3D arrow that wraps around the Z axis — the rotational cue for revolve,
    circular patterns and anything else whose action is a turn rather than a push.

    The tube is a small circle revolved `sweep` degrees about axis.z at `radius` from it,
    sitting at `height`.  The head is a cone revolved about the same axis, so it lands
    tangent to the tube's leading end and reads as the arrowhead of the turn.
    """
    tag = _arrow_tag()
    return [
        # Tube: circle in the front plane at (radius, height), swept partway round Z.
        *sketch_with_id([{"type": "circle", "center": [radius, height],
                          "radius": tube_radius, "id": tag + "t"}],
                        plane="plane.front", name="ArcTubeSk" + tag, fid=fid_tube),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "ArcTube" + tag,
            "params": {"mode": "boss", "angle": dim(sweep)}, "merge": False,
            "refs": {"profile": [region(fid_tube, radius, height)],
                     "axis": datum("axis.z")}}},
        # Head: a cone pointing along +Z, revolved about Z then left at the tube radius.
        *sketch_with_id([
            {"type": "line", "p1": [radius - head_radius, height], "p2": [radius + head_radius, height],
             "id": tag + "c1"},
            {"type": "line", "p1": [radius + head_radius, height], "p2": [radius, height + head_len],
             "id": tag + "c2"},
            {"type": "line", "p1": [radius, height + head_len], "p2": [radius - head_radius, height],
             "id": tag + "c3"},
        ], plane="plane.front", name="ArcHeadSk" + tag, fid=fid_head),
        {"op": "add_feature", "feature": {
            "type": "revolve", "name": "ArcHead" + tag,
            "params": {"mode": "boss", "angle": dim(26)}, "merge": False,
            "refs": {"profile": [region(fid_head, radius, height + head_len * 0.3)],
                     "axis": datum("axis.z")}}},
    ]
