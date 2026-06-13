"""Draft feature compiler — taper faces about a neutral plane (mold draft).

feature_params.md §Dress-up > draft. Tilts each selected face by ``params.angle`` degrees about
the line where the face meets the neutral plane, measured relative to the pull direction (the
mold-removal direction). Built with OCCT ``BRepOffsetAPI_DraftAngle``.

Refs:
- ``neutral`` — a datum plane / planar model face: the cross-section there is unchanged.
- ``pull``    — a direction (datum axis/plane, linear edge, or planar face normal). Defaults to
  the neutral plane normal when omitted.
- ``faces``   — the model faces to draft.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Plane, Solid, Vector
from OCP.BRepOffsetAPI import BRepOffsetAPI_DraftAngle
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import describe_faces, resolve
from .extrude import _body_bbox_diag, _store_topo_owners, _to_solid_normalized
from .pattern import _resolve_direction, _resolve_plane


def _gp_pln(plane: Plane) -> gp_Pln:
    o = Vector(plane.origin)
    n = Vector(plane.z_dir)
    return gp_Pln(gp_Pnt(o.X, o.Y, o.Z), gp_Dir(n.X, n.Y, n.Z))


@register("draft")
def compile_draft(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    angle_deg = ctx.dim(p, "angle", 3.0)
    if abs(angle_deg) < 1e-6:
        raise FeatureError("draft: angle must be non-zero")
    if abs(angle_deg) >= 90.0:
        raise FeatureError("draft: angle must be less than 90 degrees")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("draft: no body to draft")
    target = bodies[-1]

    neutral = _resolve_plane(refs.get("neutral"), ctx)
    pull = (_resolve_direction(refs.get("pull"), ctx) if refs.get("pull") is not None
            else Vector(neutral.z_dir))

    face_sels = refs.get("faces") or []
    if not face_sels:
        raise FeatureError("draft: refs.faces is required (one or more faces to taper)")

    infos = describe_faces(target.wrapped)
    diag = _body_bbox_diag(target)
    faces = []
    dangling: list[str] = []
    for i, sel in enumerate(face_sels):
        rr = resolve(sel, infos, diag)
        if rr.index is None:
            dangling.append(f"faces[{i}]: {rr.dangling_reason}")
            continue
        faces.append(target.faces()[rr.index])
    if dangling:
        raise FeatureError(f"draft: {len(dangling)} face selector(s) dangling",
                           dangling_refs=dangling)
    if not faces:
        raise FeatureError("draft: no faces resolved")

    try:
        builder = BRepOffsetAPI_DraftAngle(target.wrapped)
        pull_dir = gp_Dir(pull.X, pull.Y, pull.Z)
        neutral_pln = _gp_pln(neutral)
        ang = math.radians(angle_deg)
        for f in faces:
            builder.Add(f.wrapped, pull_dir, ang, neutral_pln, True)
        builder.Build()
        if not builder.IsDone():
            raise FeatureError(
                "draft: OCCT DraftAngle failed (IsDone=False)",
                hint="check the neutral plane intersects the drafted faces and the pull "
                     "direction is roughly normal to the neutral plane")
        result = _to_solid_normalized(Solid(builder.Shape()).clean())
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"draft: build failed: {exc}",
                           hint="drafting works on planar/cylindrical side faces about a "
                                "neutral plane they all touch")

    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, target)
    return CompileOutput(bodies=bodies, tool_body=None, resolved={"angle": angle_deg})
