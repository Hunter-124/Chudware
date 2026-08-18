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


# App accent teal (ui_theme.cpp dark accent #2DD4BF; also the icons-contract teal).
ACCENT_RGB = (0.176, 0.831, 0.749)


def accent(body_id: str = "b1") -> dict:
    """Color the op-created body with the selection-accent so the tile highlights
    what the operation produced (T-BL34 per-body emphasis, v1)."""
    return {"op": "set_appearance", "id": body_id, "color": list(ACCENT_RGB),
            "metallic": 0.05, "roughness": 0.45}


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
