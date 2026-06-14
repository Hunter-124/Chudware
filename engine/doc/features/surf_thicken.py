"""Thicken feature compiler — turn an open SURFACE body into a closed SOLID.

feature_params.md §Surfacing > surf_thicken.

``surf_thicken`` is the surface→solid bridge: it takes an OPEN surface body (a ``Face`` or a
``Shell`` produced by ``surf_planar`` / ``surf_extrude`` / any other ``surf_*`` feature) and offsets
it by a wall ``thickness`` to produce a watertight SOLID (``kind="solid"``). A flat 40×40 plate
thickened by 2 mm becomes a 40×40×2 = 3200 mm³ slab; an open tube wall becomes a hollow tube solid.

The solid REPLACES the input surface body in the rolling body list (SolidWorks' Thicken consumes the
surface and leaves a solid in its place), and the output is tagged ``kind="solid"`` so the core
re-stamps the BodyOut and ``mass_props`` reports volume instead of area.

Side
----
``params.side`` controls where the wall grows relative to the surface:

* ``"one"`` (default) — the full ``thickness`` grows to ONE side of the surface (along its normal;
  ``params.flip`` reverses the side). z[0, t] for an XY plate.
* ``"both"`` / ``"mid"`` — the wall straddles the surface, ±``thickness``/2 each way. z[−t/2, t/2].

Geometry (OCCT ``BRepOffset_MakeOffset`` in *thickening* mode)
-------------------------------------------------------------
Thickening is the same kernel ``dress.py:_multi_thickness_shell`` and ``rib.py`` lean on, but run in
its *solid-from-sheet* mode: ``Initialize(..., Thickening=True)`` + ``MakeOffsetShape()`` offsets the
sheet by a signed distance and joins the original + offset sheets with side walls into a SOLID
(``Join=GeomAbs_Arc`` rounds open edges, matching the shell path). One-side is a single signed
thicken; ``both``/``mid`` thickens +t/2 and −t/2 and fuses the two halves (a single ``BRepOffset``
call cannot center the wall on the sheet).

Body-selector resolution
-------------------------
``refs.surface`` is a body selector (``{"kind":"body","owner_feature":<surf-fid>}``). We resolve it
to one of ``ctx.bodies`` reusing ``boolean.py:_resolve_body_set`` (signature: volume + bbox-centre)
when the owning feature left a geometric anchor in ``ctx.tool_bodies``. Surface features append their
body WITHOUT a ``tool_body`` anchor, so when that lookup has nothing to key on we fall back to picking
the OPEN surface body directly: among the rolling bodies the open ones (a ``Face``/``Shell`` reports
volume ≈ 0), preferring the body whose faces are owned by ``owner_feature`` (topo-owner store), else
the most recently created open body. Either path yields the surface body index; an empty result is a
contained :class:`FeatureError` with ``dangling_refs``.
"""

from __future__ import annotations

from typing import Any

from build123d import Solid

from OCP.BRepOffset import BRepOffset_MakeOffset, BRepOffset_Mode
from OCP.GeomAbs import GeomAbs_Arc

from .base import CompileContext, CompileOutput, FeatureError, register
from .boolean import _resolve_body_set
from .extrude import _get_topo_owners, _store_topo_owners, _to_solid_normalized

_VALID_SIDES = ("one", "both", "mid")


# --------------------------------------------------------------------------- thickening


def _is_surface_body(body: Any) -> bool:
    """True if *body* is an OPEN surface (a Face/Shell — zero enclosed volume)."""
    if getattr(body, "wrapped", None) is None:
        return False
    try:
        return abs(float(getattr(body, "volume", 0.0) or 0.0)) <= 1e-9
    except Exception:  # noqa: BLE001
        return False


def _resolve_surface_index(sel: Any, bodies: list[Any], ctx: CompileContext) -> int:
    """Resolve ``refs.surface`` to an index into *bodies* picking the surface body.

    Tries the boolean body-selector matcher first (works when the owning feature left a
    geometric anchor in ``ctx.tool_bodies``); falls back to picking the open surface body by
    owner / recency when no anchor exists (surface features set no ``tool_body``).
    """
    sels = sel if isinstance(sel, list) else [sel]

    # 1) Signature match via the shared boolean resolver (anchor in ctx.tool_bodies).
    try:
        idx = _resolve_body_set(sels, bodies, ctx, "surface")
        if idx:
            return idx[0]
    except FeatureError:
        pass

    # 2) Fallback: pick an OPEN surface body, preferring an owner_feature match.
    owner = None
    for s in sels:
        if isinstance(s, dict) and s.get("owner_feature"):
            owner = s.get("owner_feature")
            break

    open_idx = [i for i, b in enumerate(bodies) if _is_surface_body(b)]
    if not open_idx:
        raise FeatureError(
            "surf_thicken: refs.surface did not resolve to a surface body",
            dangling_refs=[f"surface: no open surface body matches owner '{owner}'"],
        )

    if owner is not None:
        face_owners = _get_topo_owners(ctx, "face")
        if owner in face_owners.values():
            # The topo-owner store holds the LATEST snapshot; if the requested owner tagged faces
            # and exactly one open body remains, that is unambiguously it. Otherwise fall through
            # to recency (the most recently created open body).
            if len(open_idx) == 1:
                return open_idx[0]

    # Most recently created open body (appended last) — the natural "current surface".
    return open_idx[-1]


def _thicken_one_side(surface: Any, offset: float) -> Any:
    """Thicken a sheet (Face/Shell) by a signed *offset* into a SOLID via BRepOffset."""
    mk = BRepOffset_MakeOffset()
    mk.Initialize(
        surface.wrapped,
        offset,
        1e-6,                       # Tol
        BRepOffset_Mode.BRepOffset_Skin,
        False,                      # Intersection
        False,                      # SelfInter
        GeomAbs_Arc,                # Join (rounds open boundary edges)
        True,                       # Thickening  ← solid-from-sheet
        False,                      # RemoveIntEdges
    )
    mk.MakeOffsetShape()
    if not mk.IsDone():
        raise RuntimeError("BRepOffset_MakeOffset (thicken) IsDone=False")
    return Solid(mk.Shape()).clean()


# --------------------------------------------------------------------------- compiler


@register("surf_thicken")
def compile_surf_thicken(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    thickness = ctx.dim(p, "thickness", 2.0)
    if thickness <= 0.0:
        raise FeatureError("surf_thicken: thickness must be > 0")

    side = p.get("side", "one")
    if side not in _VALID_SIDES:
        raise FeatureError(
            f"surf_thicken: unknown side '{side}' (expected 'one', 'both', or 'mid')"
        )
    flip = bool(p.get("flip", False))

    surf_sel = refs.get("surface")
    if surf_sel is None or (isinstance(surf_sel, list) and not surf_sel):
        raise FeatureError("surf_thicken: refs.surface is required")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError(
            "surf_thicken: no surface body to thicken",
            hint="create a surface (surf_planar / surf_extrude / …) before thickening it",
        )

    s_idx = _resolve_surface_index(surf_sel, bodies, ctx)
    surface = bodies[s_idx]
    if not _is_surface_body(surface):
        raise FeatureError(
            "surf_thicken: the referenced body is a solid, not an open surface",
            hint="Thicken offsets an open sheet into a solid; pick a surface body",
        )

    # Build the solid. One-side: a single signed thicken (flip reverses the side). Both/mid:
    # thicken +t/2 and −t/2 and fuse so the wall straddles the sheet.
    try:
        if side == "one":
            off = -thickness if flip else thickness
            result = _thicken_one_side(surface, off)
        else:  # both / mid
            half = thickness / 2.0
            fwd = _thicken_one_side(surface, half)
            rev = _thicken_one_side(surface, -half)
            result = fwd + rev
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"surf_thicken: thickening the surface failed: {exc}",
            hint="the surface may be self-intersecting or non-manifold; try a smaller thickness",
        )

    result = _to_solid_normalized(result)
    vol = float(getattr(result, "volume", 0.0) or 0.0)
    if vol <= 1e-9:
        raise FeatureError(
            "surf_thicken: thickening produced an empty solid",
            hint="check the surface is a valid open sheet and thickness > 0",
        )

    # The solid REPLACES the consumed surface body in place. Tag topology against the prior
    # surface so downstream face/edge selectors resolve to this feature.
    new_bodies = [b for i, b in enumerate(bodies) if i != s_idx]
    new_bodies.append(result)
    _store_topo_owners(ctx, fid, result, surface)

    resolved = {"thickness": thickness, "volume": vol}
    return CompileOutput(
        bodies=new_bodies, tool_body=result, op="add", kind="solid", resolved=resolved
    )
