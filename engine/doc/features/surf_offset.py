"""Offset-surface feature compiler — a surface parallel to one or more picked faces.

``surf_offset`` is the surfacing analogue of a thicken/offset: it resolves one or more picked
FACES from the running body and builds a new OPEN surface that is parallel to those faces and
displaced ``params.distance`` (signed) along their normals. The result is appended as a NEW
SURFACE body (``CompileOutput(..., kind="surface")``) — surfaces never fuse into the running
solid — so the core stamps ``BodyOut.kind == "surface"`` and ``mass_props`` reports AREA instead
of volume.

Reuse:
- ``dress._resolve_faces`` — identical face-selector resolution against the current top body
  (so a face pick resolves exactly as it does for fillet/chamfer/shell).
- ``extrude._store_topo_owners`` — face/edge owner tagging on the produced surface body.

Kernel: OCCT's ``BRepOffsetAPI_MakeOffsetShape.PerformBySimple(shape, distance)``. "Simple" mode
offsets each face along its surface normal without computing the side-pipe/sphere fill between
neighbouring parallels — exactly the open parallel-surface a surface offset wants (BRepOffset's
Skin/join modes are for *thick-solid* offsets and would try to close the side walls). The sign of
``distance`` follows the standard OCCT convention: positive moves OUTWARD along the face normal,
negative moves inward.

Topology of the input:
- a SINGLE picked face → offset a lone ``Face`` → an open single-face surface body;
- MULTIPLE picked faces → sew them into a ``Shell`` first, then offset the shell → an open
  multi-face surface body. Sewing first lets the kernel offset adjacent faces as one connected
  parallel sheet (a per-face offset would leave gaps at the shared edges).

Kernel approximation: ``PerformBySimple`` is the no-intersection ("simple") offset, so on a
non-planar/non-developable input the offset surface can self-intersect for a large ``distance``
(OCCT's documented limitation — keep the offset small relative to the local curvature). For
planar / cylindrical / spherical faces the offset is exact.
"""

from __future__ import annotations

from typing import Any

from build123d import Face, Shape, Shell

from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL
from OCP.TopoDS import TopoDS

from .base import CompileContext, CompileOutput, FeatureError, register
from .dress import _resolve_faces
from .extrude import _store_topo_owners


def _sew_faces(faces: list[Face]):
    """Sew >1 build123d faces into a single raw OCP shell (the offset input).

    A single face is returned as its own raw face — there is nothing to sew. Multiple faces
    are sewn at a tight tolerance so the offset sees one connected sheet rather than loose
    parallels with gaps at the shared edges.
    """
    if len(faces) == 1:
        return faces[0].wrapped
    sew = BRepBuilderAPI_Sewing(1e-6)
    for f in faces:
        sew.Add(f.wrapped)
    sew.Perform()
    return sew.SewedShape()


def _wrap_surface(raw):
    """Wrap a raw OCP offset result as a build123d Shell / Face / generic Shape.

    The core's surface machinery (``_is_surface_shape`` / topology tagging) works off
    ``.wrapped`` and ``.faces()`` / ``.edges()``, which every build123d Shape provides, so the
    only goal here is to pick the most specific wrapper the result allows.
    """
    try:
        st = raw.ShapeType()
    except Exception:  # noqa: BLE001
        return Shape(raw)
    if st == TopAbs_SHELL:
        return Shell(TopoDS.Shell_s(raw))
    if st == TopAbs_FACE:
        return Face(TopoDS.Face_s(raw))
    return Shape(raw)


@register("surf_offset")
def compile_surf_offset(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    distance = ctx.dim(p, "distance", 5.0)
    if abs(distance) < 1e-9:
        raise FeatureError("surf_offset: distance must be non-zero")

    # --- resolve the picked faces against the running top body --------------------------------
    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("surf_offset: requires an existing body to offset faces from")
    body = bodies[-1]

    face_sels = refs.get("faces") or []
    if not face_sels:
        raise FeatureError("surf_offset: refs.faces is required and must be non-empty")

    faces, dangling = _resolve_faces(face_sels, body, "faces", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"surf_offset: {len(dangling)} face selector(s) dangling",
            dangling_refs=dangling,
        )
    if not faces:
        raise FeatureError("surf_offset: no faces resolved")

    # --- build the parallel offset surface ----------------------------------------------------
    src = _sew_faces(faces)
    mk = BRepOffsetAPI_MakeOffsetShape()
    try:
        mk.PerformBySimple(src, float(distance))
        mk.Build()
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"surf_offset: offset build failed ({exc})",
            hint="reduce the offset distance; a large offset on a curved face can self-intersect",
        )
    if not mk.IsDone():
        raise FeatureError(
            f"surf_offset: offset of {len(faces)} face(s) by {distance:g} mm failed",
            hint="reduce the offset distance; a large offset on a curved face can self-intersect",
        )

    surface = _wrap_surface(mk.Shape())

    # Append as a NEW surface body (surfaces never merge into the running solid).
    bodies.append(surface)
    _store_topo_owners(ctx, fid, surface, None)

    return CompileOutput(bodies=bodies, kind="surface", resolved={"distance": distance})
