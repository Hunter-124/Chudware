"""Knit-surface feature compiler — sew ≥2 surface/face bodies into one shell.

``surf_knit`` is the surfacing "join" operation: it resolves ``refs.surfaces`` to bodies
already in the rolling body list (the open surface/face bodies produced upstream by
``surf_planar`` / ``surf_extrude`` / … ), sews them edge-to-edge into a single Shell, and:

- if the sewn shell is **watertight (closed)** it is promoted to a SOLID and the knitted body is
  returned with ``kind="solid"`` (so ``mass_props`` reports a real enclosed volume);
- otherwise it stays an open **Shell**, returned with ``kind="surface"`` (an area report).

This mirrors SolidWorks *Knit Surface* with "Try to form solid": six planar faces that bound a
box knit into a solid; two abutting open patches knit into a single larger open surface.

Body-selector resolution
-------------------------
``refs.surfaces`` is a list of BODY selectors (``{"kind":"body","owner_feature":<fid>}``). We
resolve them to indices into the running body list with ``boolean._resolve_body_set`` — the same
geometric-signature matcher the multi-body boolean uses (an owner feature's ``tool_body`` is the
geometric anchor for the body it produced). Surface features whose own ``tool_body`` is ``None``
(``surf_planar`` returns the sketch's bounding face; ``surf_extrude`` the swept wall) are still
resolvable because the **sketch** that drives them registers that face/profile as its tool_body
with an identical signature. For owners that the signature matcher cannot place (a surface
feature referenced by its OWN id), we add a contained fallback that maps the owner to a running
body through the feature's face-ownership entry in the ``_topo_owners`` store.

Sewing
------
We lift the proven sew block from ``formats._import_iges``: ``BRepBuilderAPI_Sewing(tol)`` over
every input shape + a ``ShapeFix_Shape`` pass. Closed-ness is decided on the SEWN shell
(``BRep_Tool.IsClosed`` / no free boundary edges); when closed we build a solid with
``BRepBuilderAPI_MakeSolid`` and clean it with ``ShapeFix_Solid``.

The knitted body REPLACES its inputs: every consumed surface body is removed from the rolling
list and the single result is appended LAST (so it becomes the top body the core owner-tags and
stamps the ``kind`` onto), exactly the way ``boolean.py`` rebuilds the list.

Error containment: a selector miss raises :class:`FeatureError` with ``dangling_refs``; the OCCT
sew/solid build is wrapped so a kernel failure becomes a contained FeatureError, never a crash.
"""

from __future__ import annotations

from typing import Any

from .base import CompileContext, CompileOutput, FeatureError, register
from .boolean import _resolve_body, _resolve_body_set  # reuse the boolean body matcher
from .extrude import _store_topo_owners


# --------------------------------------------------------------------------- topo-store fallback


def _owner_to_body_index(owner: str, bodies: list[Any], ctx: CompileContext) -> int | None:
    """Fallback: map a surface-feature *owner* id to a running-body index via _topo_owners.

    The ``_topo_owners`` store keys each surface feature to the face-owner map of the single body
    it produced (e.g. ``{"f2": {"face": {1: "f2"}}}``). The running body owned by *owner* is the
    one whose face count equals that feature's stored face count AND whose faces are all attributed
    to *owner*. Returns the body index, or ``None`` when no unambiguous match exists.
    """
    store_datum = ctx.datums.get("_topo_owners")
    if store_datum is None:
        return None
    store = store_datum.obj
    entry = store.get(owner)
    if not entry:
        return None
    face_map = entry.get("face", {})
    if not face_map:
        return None
    # The feature owns every face in its own entry → owned-face count is the body's face count.
    owned = sum(1 for v in face_map.values() if v == owner)
    if owned <= 0:
        return None

    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    matches: list[int] = []
    for i, b in enumerate(bodies):
        m = TopTools_IndexedMapOfShape()
        if getattr(b, "wrapped", None) is None:
            continue
        TopExp.MapShapes_s(b.wrapped, TopAbs_FACE, m)
        if m.Extent() == owned:
            matches.append(i)
    # Only accept an unambiguous single match (avoid silently grabbing the wrong same-size body).
    return matches[0] if len(matches) == 1 else None


def _resolve_surface_indices(sels: Any, bodies: list[Any], ctx: CompileContext) -> list[int]:
    """Resolve ``refs.surfaces`` to de-duplicated running-body indices.

    Primary path is ``boolean._resolve_body_set`` (signature match against owner tool_bodies).
    Any selector it cannot place — typically a surface feature referenced by its OWN id, whose
    ``tool_body`` is ``None`` — is retried through the ``_topo_owners`` fallback before failing.
    """
    if not isinstance(sels, list) or not sels:
        raise FeatureError("surf_knit: refs.surfaces must be a non-empty list of body selectors")

    out: list[int] = []
    dangling: list[str] = []
    for i, sel in enumerate(sels):
        slot = f"surfaces[{i}]"
        idx: int | None = None
        try:
            idx = _resolve_body(sel, bodies, ctx, slot)
        except FeatureError as exc:
            # Retry surface-feature owners through the topo-owner store.
            owner = sel.get("owner_feature") if isinstance(sel, dict) else None
            if isinstance(owner, str) and owner:
                idx = _owner_to_body_index(owner, bodies, ctx)
            if idx is None:
                dangling.extend(exc.dangling_refs or [f"{slot}: {exc}"])
                continue
        if idx not in out:
            out.append(idx)
    if dangling:
        raise FeatureError(
            f"surf_knit: {len(dangling)} surface selector(s) failed",
            dangling_refs=dangling,
        )
    return out


# --------------------------------------------------------------------------- sewing helpers


def _free_edge_count(raw) -> int:
    """Number of boundary (free) edges of *raw* — edges adjacent to exactly ONE face.

    A watertight (closed) shell has zero free edges; any open patch has >0.
    """
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(raw, TopAbs_EDGE, TopAbs_FACE, m)
    free = 0
    for i in range(1, m.Extent() + 1):
        if m.FindFromIndex(i).Extent() == 1:
            free += 1
    return free


def _first_shell(raw):
    """Return the (first) TopoDS_Shell inside *raw*, or ``None`` if there is none."""
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    shells = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(raw, TopAbs_SHELL, shells)
    return shells.FindKey(1) if shells.Extent() >= 1 else None


def _sew(shapes: list[Any], tol: float):
    """Sew *shapes* into one shell (lifted from formats._import_iges) → (sewn_raw, fixed_raw)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    from OCP.ShapeFix import ShapeFix_Shape

    sewing = BRepBuilderAPI_Sewing(tol)
    for s in shapes:
        raw = s.wrapped if hasattr(s, "wrapped") else s
        if raw is None:
            raise FeatureError("surf_knit: a surface body has no geometry to sew")
        sewing.Add(raw)
    sewing.Perform()
    sewn = sewing.SewedShape()
    fixer = ShapeFix_Shape(sewn)
    fixer.Perform()
    return sewn, fixer.Shape()


def _make_solid(shell_raw):
    """Build + fix a solid from a (closed) shell. Returns the fixed solid raw shape, or None."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.ShapeFix import ShapeFix_Solid

    mk = BRepBuilderAPI_MakeSolid()
    from OCP.TopoDS import TopoDS

    mk.Add(TopoDS.Shell_s(shell_raw))
    if not mk.IsDone():
        return None
    solid = mk.Solid()
    fixer = ShapeFix_Solid(solid)
    fixer.Perform()
    return fixer.Solid()


# --------------------------------------------------------------------------- compiler


@register("surf_knit")
def compile_surf_knit(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("surf_knit: no bodies to knit")

    surf_idx = _resolve_surface_indices(refs.get("surfaces"), bodies, ctx)
    if len(surf_idx) < 2:
        raise FeatureError(
            "surf_knit: needs at least 2 surface bodies to sew",
            hint="select two or more surface/face bodies to knit together",
        )

    inputs = [bodies[i] for i in surf_idx]

    # --- sew the inputs into one shell --------------------------------------------------
    # Tolerance scaled to the model: a fraction of the combined bounding-box diagonal, clamped to
    # a sane floor/ceiling so abutting walls actually merge without over-fusing distinct edges.
    tol = _sew_tolerance(inputs)
    try:
        sewn, fixed = _sew(inputs, tol)
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"surf_knit: sewing failed: {exc}",
            hint="the selected surfaces may not share coincident edges within tolerance",
        )
    if getattr(fixed, "IsNull", None) is not None and fixed.IsNull():
        raise FeatureError("surf_knit: sewing produced an empty result")

    # --- decide closed (→ solid) vs open (→ surface) ------------------------------------
    shell_raw = _first_shell(fixed) or fixed
    closed = _free_edge_count(shell_raw) == 0

    from build123d import Shape, Shell, Solid

    result: Any
    kind: str
    if closed:
        # Watertight → promote to a SOLID so mass_props reports a real enclosed volume.
        try:
            solid_raw = _make_solid(shell_raw)
        except Exception as exc:  # noqa: BLE001
            solid_raw = None
        if solid_raw is None or solid_raw.IsNull():
            # Couldn't lift to a solid; keep the (closed) shell as a surface body rather than fail.
            result = Shell(shell_raw) if Shell.cast(shell_raw) is not None else Shape.cast(fixed)
            kind = "surface"
        else:
            cast = Solid.cast(solid_raw)
            result = cast if cast is not None else Shape.cast(solid_raw)
            kind = "solid"
            if float(getattr(result, "volume", 0.0) or 0.0) <= 1e-9:
                raise FeatureError(
                    "surf_knit: knit closed but the solid has no volume",
                    hint="the surfaces enclose no space or are self-intersecting",
                )
    else:
        # Open shell stays a surface body.
        wrapped = Shell.cast(shell_raw)
        result = wrapped if wrapped is not None else Shape.cast(fixed)
        kind = "surface"

    # --- rebuild the rolling body list: drop inputs, append the knit result LAST --------
    consumed = set(surf_idx)
    new_bodies: list[Any] = [b for i, b in enumerate(bodies) if i not in consumed]
    new_bodies.append(result)

    # Tag topology so downstream selectors resolve against THIS feature.
    _store_topo_owners(ctx, fid, result, None)

    resolved: dict[str, float] = {
        "_input_count": float(len(surf_idx)),
        "_closed": 1.0 if kind == "solid" else 0.0,
    }
    if kind == "solid":
        resolved["volume"] = float(getattr(result, "volume", 0.0) or 0.0)

    # A solid knit fuses into the running set as a regular solid (tool_body so patterns/booleans can
    # see it); a surface knit appends a new surface body with no tool_body (like surf_planar).
    if kind == "solid":
        return CompileOutput(bodies=new_bodies, tool_body=result, op="add",
                             resolved=resolved, kind="solid")
    return CompileOutput(bodies=new_bodies, resolved=resolved, kind="surface")


# --------------------------------------------------------------------------- tolerance


def _sew_tolerance(bodies: list[Any]) -> float:
    """A model-scaled sewing tolerance: 1e-3 × the combined bbox diagonal, clamped [1e-4, 0.1]."""
    import math

    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    for b in bodies:
        try:
            bb = b.bounding_box()
        except Exception:  # noqa: BLE001
            continue
        lo[0], lo[1], lo[2] = min(lo[0], bb.min.X), min(lo[1], bb.min.Y), min(lo[2], bb.min.Z)
        hi[0], hi[1], hi[2] = max(hi[0], bb.max.X), max(hi[1], bb.max.Y), max(hi[2], bb.max.Z)
    if not all(math.isfinite(v) for v in lo + hi):
        return 1e-3
    diag = math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3)))
    return max(1e-4, min(0.1, diag * 1e-3))
