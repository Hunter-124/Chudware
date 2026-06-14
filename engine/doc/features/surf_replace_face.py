"""Replace-face feature compiler — SolidWorks "Replace Face".

``surf_replace_face`` swaps one or more faces of the running SOLID with a (typically larger)
replacement surface, re-healing the result back into a solid. The replacement surface extends
or retrims the solid up to the new surface: SolidWorks "Replace Face" deletes the picked target
faces and grows/cuts the remaining body so that its boundary follows the replacement surface
instead.

Refs
----
- ``refs.faces``   — a list of FACE selectors on the running solid (the faces to replace). They
  are resolved with :func:`dress._resolve_faces` (the same resolver fillet/chamfer/shell use),
  with the centroid-map workaround below to defend against the latent ``describe_faces`` /
  ``body.faces()`` index mismatch.
- ``refs.surface`` — the replacement surface. A BODY selector (a surface body produced upstream,
  e.g. by ``surf_planar`` / ``surf_offset``) resolved via :func:`boolean._resolve_body_set`, OR a
  FACE selector resolved via :func:`split._resolve_tool_face`.

Approach
--------
The robust, kernel-friendly realisation is a *general-BOP split + keep-the-retained-side*:

1. Resolve the target faces and capture their centroids (the side of the body we are DELETING).
2. Split the running solid by the replacement surface with OCCT ``BRepAlgoAPI_Splitter`` (the
   same general splitter ``split.py`` uses for non-planar tools). The surface divides the solid
   into ≥2 pieces along the new boundary.
3. Keep the piece(s) that do NOT carry the target faces — i.e. the material on the retained side
   of the replacement surface. The deleted faces sit on the discarded piece(s); the retained
   piece's boundary now follows the replacement surface. This is exactly the "extend/retrim up
   to the new surface" semantics of Replace Face for a surface that passes through the part.

If the splitter cannot divide the solid (the surface misses the body, or BOP fails to heal a
solid), we fall back to a *shell rebuild*: drop the target faces from the solid's shell, sew the
replacement surface's faces in their place, and re-solidify with ``BRepBuilderAPI_MakeSolid``.

Either way the output is a single SOLID (``kind="solid"``). If neither path heals to a positive
volume solid, a clean :class:`FeatureError` is raised (contained — the rebuild continues on the
last good body).
"""

from __future__ import annotations

from typing import Any

from build123d import Solid

from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing, BRepBuilderAPI_MakeSolid
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.ShapeFix import ShapeFix_Solid, ShapeFix_Shape
from OCP.TopAbs import TopAbs_SOLID, TopAbs_FACE, TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

from .base import CompileContext, CompileOutput, FeatureError, register
from .dress import _resolve_faces
from .extrude import _store_topo_owners, _to_solid_normalized, _body_bbox_diag
from .boolean import _resolve_body_set


# --------------------------------------------------------------------------- geometry helpers


def _face_centroid(face_wrapped) -> tuple[float, float, float]:
    """Surface centre-of-mass of a raw OCP face."""
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face_wrapped, props)
    cm = props.CentreOfMass()
    return (cm.X(), cm.Y(), cm.Z())


def _solid_volume(solid_wrapped) -> float:
    """Enclosed volume of a raw OCP solid (0 for an open shell)."""
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(solid_wrapped, props)
    return float(props.Mass())


def _raw_faces(shape_wrapped) -> list:
    """All raw OCP faces of *shape_wrapped* (deduped enumeration)."""
    out: list = []
    exp = TopExp_Explorer(shape_wrapped, TopAbs_FACE)
    while exp.More():
        out.append(TopoDS.Face_s(exp.Current()))
        exp.Next()
    return out


def _target_centroids(faces: list) -> list[tuple[float, float, float]]:
    """Centroids of the resolved (build123d) target faces.

    The centroid-map workaround: ``dress._resolve_faces`` returns build123d face wrappers that
    are index-aligned with ``describe_faces`` only when that enumeration matches ``body.faces()``
    — which is NOT guaranteed across OCCT builds. We never trust the index downstream; instead we
    carry each resolved face's *centroid* (computed directly off its own ``.wrapped``) and use it
    geometrically to decide which split piece holds the deleted faces. Centroids are stable
    regardless of enumeration order.
    """
    cents: list[tuple[float, float, float]] = []
    for f in faces:
        try:
            c = f.center()
            cents.append((c.X, c.Y, c.Z))
        except Exception:  # noqa: BLE001
            cents.append(_face_centroid(f.wrapped))
    return cents


def _piece_has_face_at(piece_wrapped, centroid: tuple[float, float, float], tol: float) -> bool:
    """True if *piece_wrapped* still owns a face whose centroid matches *centroid* within tol."""
    for f in _raw_faces(piece_wrapped):
        fc = _face_centroid(f)
        d2 = ((fc[0] - centroid[0]) ** 2 + (fc[1] - centroid[1]) ** 2
              + (fc[2] - centroid[2]) ** 2)
        if d2 <= tol * tol:
            return True
    return False


# --------------------------------------------------------------------------- replacement surface


def _resolve_replacement_surface(ref: Any, ctx: CompileContext, exclude_idx: int) -> Any:
    """Resolve ``refs.surface`` to the build123d shape of the replacement surface.

    Accepts a BODY selector (a surface body produced upstream) resolved through
    :func:`boolean._resolve_body_set`, or a FACE selector resolved through
    :func:`split._resolve_tool_face`. *exclude_idx* is the index of the running solid (so a body
    selector never resolves to the solid we are modifying).
    """
    if ref is None:
        raise FeatureError(
            "surf_replace_face: refs.surface is required (a replacement surface body or face)",
            dangling_refs=["surface: missing"],
        )

    bodies = list(ctx.bodies)

    # Body selector → a whole surface body in ctx.bodies.
    if isinstance(ref, dict) and ref.get("kind") == "body":
        idx_list = _resolve_body_set([ref], bodies, ctx, "surface")
        idx = next((i for i in idx_list if i != exclude_idx), None)
        if idx is None:
            raise FeatureError(
                "surf_replace_face: refs.surface body resolves to the body being modified",
                dangling_refs=["surface: same as target solid"],
            )
        return bodies[idx]

    # Face selector → the owning face on any running body (e.g. a face of a surface body).
    if isinstance(ref, dict) and ref.get("kind") == "face":
        from .split import _resolve_tool_face
        _owner, face, _bi = _resolve_tool_face(ref, ctx)
        return face

    raise FeatureError(
        "surf_replace_face: refs.surface must be a body or face selector",
        dangling_refs=[f"surface: unsupported selector {ref!r}"],
    )


def _surface_faces(surface_shape) -> list:
    """The raw OCP faces of the replacement surface (a Face, Shell, or surface body)."""
    wrapped = getattr(surface_shape, "wrapped", surface_shape)
    return _raw_faces(wrapped)


# --------------------------------------------------------------------------- BOP split path


def _split_solid_by_surface(solid, surface_shape) -> list:
    """Split *solid* (build123d) by the replacement surface using the general BOP splitter.

    Returns the list of resulting solid pieces (build123d ``Solid``), or ``[]`` when the surface
    does not actually divide the solid (a single piece comes back).
    """
    from OCP.TopTools import TopTools_ListOfShape

    args = TopTools_ListOfShape()
    args.Append(solid.wrapped)
    tools = TopTools_ListOfShape()
    for f in _surface_faces(surface_shape):
        tools.Append(f)

    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(args)
    splitter.SetTools(tools)
    try:
        splitter.Build()
    except Exception:  # noqa: BLE001
        return []
    if not splitter.IsDone():
        return []

    pieces: list = []
    exp = TopExp_Explorer(splitter.Shape(), TopAbs_SOLID)
    while exp.More():
        sw = TopoDS.Solid_s(exp.Current())
        if _solid_volume(sw) > 1e-9:
            pieces.append(Solid(sw))
        exp.Next()
    return pieces if len(pieces) >= 2 else []


def _keep_retained_pieces(pieces: list, target_cents: list[tuple[float, float, float]],
                          diag: float) -> Any:
    """From the split *pieces*, fuse and return the material on the RETAINED side.

    A piece is on the discarded side if it still carries any of the deleted target faces. The
    retained pieces are everything else; we fuse them into one solid. If every piece carries a
    target face (degenerate), keep the largest piece so we always emit a valid solid.
    """
    tol = max(diag * 1e-4, 1e-4)
    retained = [p for p in pieces
                if not any(_piece_has_face_at(p.wrapped, c, tol) for c in target_cents)]
    if not retained:
        # Degenerate: no clean retained piece — keep the largest by volume.
        retained = [max(pieces, key=lambda p: _solid_volume(p.wrapped))]

    fused = retained[0]
    for p in retained[1:]:
        try:
            fused = fused + p
        except Exception:  # noqa: BLE001
            pass
    return _to_solid_normalized(fused)


# --------------------------------------------------------------------------- shell-rebuild fallback


def _shell_rebuild(solid, target_cents: list[tuple[float, float, float]],
                   surface_shape, diag: float) -> Any | None:
    """Fallback: drop the target faces from the solid shell, sew in the replacement, re-solidify.

    Returns a healed build123d ``Solid`` or ``None`` if it cannot form a positive-volume solid.
    """
    tol = max(diag * 1e-4, 1e-4)

    sew = BRepBuilderAPI_Sewing(max(diag * 1e-5, 1e-6))
    kept = 0
    for f in _raw_faces(solid.wrapped):
        fc = _face_centroid(f)
        is_target = any(
            ((fc[0] - c[0]) ** 2 + (fc[1] - c[1]) ** 2 + (fc[2] - c[2]) ** 2) <= tol * tol
            for c in target_cents
        )
        if is_target:
            continue
        sew.Add(f)
        kept += 1
    if kept == 0:
        return None

    # Sew the replacement surface's faces into the opening.
    for f in _surface_faces(surface_shape):
        sew.Add(f)

    try:
        sew.Perform()
        sewn = sew.SewedShape()
    except Exception:  # noqa: BLE001
        return None

    # Collect the resulting shell(s) and build a solid.
    shells: list = []
    exp = TopExp_Explorer(sewn, TopAbs_SHELL)
    while exp.More():
        shells.append(TopoDS.Shell_s(exp.Current()))
        exp.Next()
    if not shells:
        return None

    try:
        mk = BRepBuilderAPI_MakeSolid()
        for sh in shells:
            mk.Add(sh)
        if not mk.IsDone():
            return None
        raw = mk.Solid()
        fix = ShapeFix_Solid(raw)
        fix.Perform()
        healed = fix.Solid()
        sfx = ShapeFix_Shape(healed)
        sfx.Perform()
        healed = sfx.Shape()
    except Exception:  # noqa: BLE001
        return None

    exp = TopExp_Explorer(healed, TopAbs_SOLID)
    best = None
    best_vol = 0.0
    while exp.More():
        sw = TopoDS.Solid_s(exp.Current())
        v = abs(_solid_volume(sw))
        if v > best_vol:
            best_vol = v
            best = sw
        exp.Next()
    if best is None or best_vol <= 1e-9:
        return None
    return _to_solid_normalized(Solid(best))


# --------------------------------------------------------------------------- compiler


@register("surf_replace_face")
def compile_surf_replace_face(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("surf_replace_face: requires an existing solid to replace faces on")

    # The running solid is the modification target: the last body with a real enclosed volume.
    solid_idx = None
    for i in range(len(bodies) - 1, -1, -1):
        try:
            if abs(bodies[i].volume) > 1e-9:
                solid_idx = i
                break
        except Exception:  # noqa: BLE001
            continue
    if solid_idx is None:
        raise FeatureError("surf_replace_face: no solid body to modify")
    solid = bodies[solid_idx]
    diag = _body_bbox_diag(solid)

    # --- resolve the target faces to replace --------------------------------------------------
    face_sels = refs.get("faces") or []
    if not face_sels:
        raise FeatureError("surf_replace_face: refs.faces is required and must be non-empty")

    faces, dangling = _resolve_faces(face_sels, solid, "faces", ctx=ctx)
    if dangling:
        raise FeatureError(
            f"surf_replace_face: {len(dangling)} face selector(s) dangling",
            dangling_refs=dangling,
        )
    if not faces:
        raise FeatureError("surf_replace_face: no target faces resolved")
    target_cents = _target_centroids(faces)

    # --- resolve the replacement surface ------------------------------------------------------
    surface_shape = _resolve_replacement_surface(refs.get("surface"), ctx, exclude_idx=solid_idx)
    if not _surface_faces(surface_shape):
        raise FeatureError(
            "surf_replace_face: the replacement surface has no faces",
            dangling_refs=["surface: empty geometry"],
        )

    # --- primary path: BOP split + keep the retained side -------------------------------------
    result = None
    method = ""
    try:
        pieces = _split_solid_by_surface(solid, surface_shape)
    except Exception:  # noqa: BLE001
        pieces = []
    if pieces:
        try:
            cand = _keep_retained_pieces(pieces, target_cents, diag)
            if cand is not None and abs(getattr(cand, "volume", 0.0)) > 1e-9:
                result = cand
                method = "split"
        except Exception:  # noqa: BLE001
            result = None

    # --- fallback path: shell rebuild ---------------------------------------------------------
    if result is None:
        cand = _shell_rebuild(solid, target_cents, surface_shape, diag)
        if cand is not None and abs(getattr(cand, "volume", 0.0)) > 1e-9:
            result = cand
            method = "shell"

    if result is None:
        raise FeatureError(
            "surf_replace_face: could not heal the replacement into a solid",
            hint="the replacement surface must fully cross the picked faces so the body can "
                 "be retrimmed up to it; extend the surface past the part's footprint",
        )

    # --- emit the modified solid (replace the target solid in the rolling list) ---------------
    new_bodies = list(ctx.bodies)
    new_bodies[solid_idx] = result
    # Keep the modified solid resolvable downstream: make it the top body if it isn't already.
    if solid_idx != len(new_bodies) - 1:
        new_bodies.append(new_bodies.pop(solid_idx))

    top = new_bodies[-1]
    _store_topo_owners(ctx, fid, top, solid)

    return CompileOutput(
        bodies=new_bodies,
        tool_body=top,
        op="add",
        kind="solid",
        resolved={
            "_replaced_faces": float(len(faces)),
            "_method_split": 1.0 if method == "split" else 0.0,
            "_method_shell": 1.0 if method == "shell" else 0.0,
            "volume": float(abs(getattr(top, "volume", 0.0))),
        },
    )
