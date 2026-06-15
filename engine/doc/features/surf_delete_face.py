"""Delete-Face feature compiler — remove picked faces from a SOLID, leaving an open SHELL.

``surf_delete_face`` is SolidWorks "Delete Face → Delete": it resolves one or more picked FACES
from the running SOLID body and removes them from the body's skin, producing an OPEN ``Shell``
surface body. Removing a face from a closed solid skin punches a hole in the boundary, so the
result can no longer enclose a volume — the body therefore flips ``kind="solid"`` → ``kind="surface"``
and REPLACES the solid in the body list (it is the same body, just opened, not a new appended one).
The core stamps ``BodyOut.kind == "surface"`` so tessellation emits a two-sided mesh and
``mass_props`` reports AREA (= remaining-face area, ``volume_mm3`` ≈ 0) instead of volume.

Reuse / face resolution:
- Face selectors resolve with the SAME ``describe_faces + resolve`` machinery the dress-up
  compilers use. ``dress._resolve_faces`` is the obvious helper, but it carries a latent index
  bug: ``describe_faces`` enumerates MORE descriptors than ``body.faces()`` (a solid's
  ``describe_faces`` can list duplicates / a wider index space than the de-duplicated build123d
  ``faces()`` list), so indexing ``face_objs[idx]`` can pick the wrong face. We therefore resolve
  the descriptor as usual but map the resolved descriptor's CENTROID back to the nearest unique
  ``body.faces()`` face — exactly what ``dress._resolve_vertices`` does for the analogous
  vertex-duplication issue — which is robust regardless of the enumeration width.
- ``extrude._store_topo_owners`` — face/edge owner tagging on the produced surface body.

Kernel: explode the solid into its faces, then sew the REMAINING (un-deleted) faces into a single
``Shell`` via ``BRepBuilderAPI_Sewing``. The deleted faces are simply never added to the sewing,
so the sewn shell is the original skin minus those faces — an open shell with free boundary edges
around each removed patch. ``BRepBuilderAPI_Sewing`` stitches the shared edges back together so the
result is one connected shell (not a loose face compound).
"""

from __future__ import annotations

from typing import Any

from build123d import Face, Shape, Shell

from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL
from OCP.TopExp import TopExp
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS

from .base import CompileContext, CompileOutput, FeatureError, register
from .dress import _bbox_diag, _describe_with_owners, _resolve_with_hint_fallback
from .extrude import _store_topo_owners
from engine.doc.selectors import resolve


def _resolve_faces_to_objs(
    sel_list: list[dict[str, Any]],
    body,
    slot_name: str,
    ctx=None,
) -> tuple[list, list[str]]:
    """Resolve face selectors against *body*, mapping each hit to a unique ``body.faces()`` face.

    This is ``dress._resolve_faces`` with the latent index bug closed: instead of indexing
    ``body.faces()[idx]`` (whose index space can be narrower than ``describe_faces``'), we map the
    resolved descriptor's centroid back to the nearest unique build123d face — the same safeguard
    ``dress._resolve_vertices`` applies for duplicated vertices.
    """
    infos = _describe_with_owners(body.wrapped, "face", ctx)
    diag = _bbox_diag(body)
    face_objs = body.faces()

    resolved: list = []
    dangling: list[str] = []
    for i, sel in enumerate(sel_list):
        idx = _resolve_with_hint_fallback(sel, infos, diag)
        if idx is None:
            rr = resolve(sel, infos, diag)
            dangling.append(f"{slot_name}[{i}]: {rr.dangling_reason}")
            continue
        target = infos[idx].centroid
        nearest = min(
            face_objs,
            key=lambda f, t=target: (
                (f.center().X - t[0]) ** 2
                + (f.center().Y - t[1]) ** 2
                + (f.center().Z - t[2]) ** 2
            ),
        )
        resolved.append(nearest)
    return resolved, dangling


def _face_key(face) -> tuple[float, float, float]:
    """Stable geometric key (rounded centroid) for a build123d / raw face, for set membership."""
    if hasattr(face, "center"):
        c = face.center()
        return (round(c.X, 5), round(c.Y, 5), round(c.Z, 5))
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    cm = props.CentreOfMass()
    return (round(cm.X(), 5), round(cm.Y(), 5), round(cm.Z(), 5))


@register("surf_delete_face")
def compile_surf_delete_face(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("surf_delete_face: requires an existing body to delete faces from")
    body = bodies[-1]

    face_sels = refs.get("faces") or []
    if not face_sels:
        raise FeatureError("surf_delete_face: refs.faces is required and must be non-empty")

    del_faces, dangling = _resolve_faces_to_objs(face_sels, body, "faces", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"surf_delete_face: {len(dangling)} face selector(s) dangling",
            dangling_refs=dangling,
        )
    if not del_faces:
        raise FeatureError("surf_delete_face: no faces resolved")

    del_keys = {_face_key(f) for f in del_faces}

    # Explode the solid into its faces and sew back the ones we are KEEPING. The deleted faces are
    # simply never added, so the sewn shell is the original skin minus those patches (open shell).
    raw = body.wrapped
    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(raw, TopAbs_FACE, face_map)

    sew = BRepBuilderAPI_Sewing(1e-6)
    kept = 0
    for i in range(1, face_map.Extent() + 1):
        f_raw = TopoDS.Face_s(face_map.FindKey(i))
        if _face_key(f_raw) in del_keys:
            continue
        sew.Add(f_raw)
        kept += 1

    if kept == 0:
        raise FeatureError(
            "surf_delete_face: deleting the selected faces would remove the whole body"
        )

    sew.Perform()
    sewn = sew.SewedShape()

    # Prefer a single Shell wrapper; the sewn result is usually one shell, but fall back to a
    # generic Shape if the sewing yields a compound (e.g. disconnected remaining patches).
    try:
        st = sewn.ShapeType()
    except Exception:  # noqa: BLE001
        st = None
    if st == TopAbs_SHELL:
        surface: Any = Shell(TopoDS.Shell_s(sewn))
    elif st == TopAbs_FACE:
        surface = Face(TopoDS.Face_s(sewn))
    else:
        # Compound of shells — unwrap to a lone Shell when there is exactly one, else keep generic.
        shells = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(sewn, TopAbs_SHELL, shells)
        surface = Shell(TopoDS.Shell_s(shells.FindKey(1))) if shells.Extent() == 1 else Shape(sewn)

    # REPLACE the solid with the opened shell (same body, now a surface) — not a new body.
    bodies[-1] = surface
    _store_topo_owners(ctx, fid, surface, body)

    return CompileOutput(
        bodies=bodies,
        kind="surface",
        resolved={"deleted_faces": float(len(del_faces))},
    )
