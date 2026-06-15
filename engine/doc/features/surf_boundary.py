"""Boundary SURFACE feature compiler — a surface filled inside a net of boundary curves.

``surf_boundary`` is SolidWorks' "Boundary Surface" (and the surfacing cousin of the boundary
boss): the user supplies one or two *directions* of boundary curves and the feature fits an open
surface that passes through that curve net. Like the other ``surf_*`` features it tags its output
``kind="surface"``, so the core stamps the BodyOut accordingly — tessellation emits a two-sided
mesh and ``mass_props`` reports AREA (``is_surface`` True, ``volume_mm3``≈0) instead of volume.

Refs (each entry is a sketch-region / model-edge selector, resolved exactly like loft/sweep
guides — see ``loft._resolve_curve_wire``, which this module reuses)
----
- ``refs.dir1`` (required, ≥1) = the direction-1 boundary curves. Each resolves to a Wire; the
  wire's constituent edges become boundary constraints of the fitted surface.
- ``refs.dir2`` (optional) = the direction-2 boundary curves. Same shape as ``dir1``; together
  the two directions describe the closed net the surface is filled inside of.

Geometry strategy
-----------------
The boundary curves of both directions are exploded into their edges and handed to OCCT
``BRepFill_Filling`` as ``GeomAbs_C0`` constraints (the surface must pass through each edge's 3-D
representation). ``Build()`` fits a single covering ``Face`` bounded by that net — the natural,
robust realization of a boundary surface for an arbitrary (planar or gently-curved) closed loop.

Fallback: if the filling does not converge (``IsDone`` False, or it yields a degenerate face)
*and* there are ≥2 separable direction-1 wires, the feature falls back to lofting an open shell
through those wires with ``BRepOffsetAPI_ThruSections`` (``isSolid=False``) — the same builder the
surface loft uses. This covers the "rails + sections" case where ``dir1`` is a stack of section
curves rather than one closed boundary loop, mirroring SW's boundary-surface dual nature.

Surfaces never fuse into the running solid — the result is appended as a NEW body, like
``surf_planar`` / ``surf_loft``.

Approximation / limitation: ``BRepFill_Filling`` always fits with C0 continuity (pass-through),
so SW's per-edge tangency-to-adjacent-face influence is not applied — the surface meets the
boundary positionally but not tangentially. Manual curve *matching* between the two directions is
likewise left to OCCT's automatic correspondence.
"""

from __future__ import annotations

from typing import Any

from build123d import Edge, Face, Shell, Wire
from OCP.BRepFill import BRepFill_Filling
from OCP.GeomAbs import GeomAbs_Shape

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _store_topo_owners, _to_solid_normalized
from .loft import _build_thrusections, _resolve_curve_wire


# --------------------------------------------------------------------------- ref resolution


def _resolve_direction_wires(
    sels: list, ctx: CompileContext, what: str
) -> list[Wire]:
    """Resolve a direction's selector list to boundary Wires (reusing loft's resolution)."""
    wires: list[Wire] = []
    dangling: list[str] = []
    for i, sel in enumerate(sels):
        try:
            wires.append(_resolve_curve_wire(sel, ctx, f"{what}[{i}]"))
        except FeatureError as exc:
            if exc.dangling_refs:
                dangling.extend([f"{what}[{i}]: " + dr for dr in exc.dangling_refs])
            else:
                dangling.append(f"{what}[{i}]: {exc}")
    if dangling:
        raise FeatureError(
            f"surf_boundary: {len(dangling)} {what} selector(s) failed",
            dangling_refs=dangling,
        )
    return wires


def _wire_edges(wire: Wire) -> list[Edge]:
    """The constituent edges of a boundary Wire (one constraint each for the filling)."""
    try:
        edges = list(wire.edges())
    except Exception:  # noqa: BLE001
        edges = []
    return edges


# --------------------------------------------------------------------------- builders


def _fill_boundary(wires: list[Wire]) -> Face:
    """Fit a covering Face inside the boundary net via ``BRepFill_Filling`` (C0 pass-through)."""
    mk = BRepFill_Filling()
    n_edges = 0
    for w in wires:
        for e in _wire_edges(w):
            mk.Add(e.wrapped, GeomAbs_Shape.GeomAbs_C0)
            n_edges += 1
    if n_edges < 2:
        raise FeatureError(
            "surf_boundary: need at least a 2-edge boundary net to fill",
            hint="supply a closed loop of boundary curves (e.g. a 3-4 sided sketch loop)",
        )
    try:
        mk.Build()
        done = mk.IsDone()
    except Exception as exc:  # noqa: BLE001  (OCCT may throw StdFail on a non-fillable net)
        raise FeatureError(f"surf_boundary: BRepFill_Filling raised ({exc})")
    if not done:
        raise FeatureError("surf_boundary: OCCT BRepFill_Filling failed (IsDone=False)")
    try:
        face = Face(mk.Face())
        area = face.area
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"surf_boundary: filling produced no valid face ({exc})")
    if getattr(face, "wrapped", None) is None or area <= 1e-9:
        raise FeatureError("surf_boundary: filling produced a degenerate (zero-area) face")
    return face


# --------------------------------------------------------------------------- compile


@register("surf_boundary")
def compile_surf_boundary(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    dir1_sels = refs.get("dir1") or []
    if not dir1_sels:
        raise FeatureError("surf_boundary: refs.dir1 is required (>=1 boundary curve)")
    dir2_sels = refs.get("dir2") or []

    dir1_wires = _resolve_direction_wires(dir1_sels, ctx, "dir1")
    dir2_wires = _resolve_direction_wires(dir2_sels, ctx, "dir2") if dir2_sels else []
    if not dir1_wires:
        raise FeatureError("surf_boundary: no dir1 boundary curves resolved")

    all_wires = dir1_wires + dir2_wires

    surface: Any
    method: str
    try:
        surface = _fill_boundary(all_wires)
        method = "filling"
    except FeatureError as fill_exc:
        # Fallback: if filling won't converge and dir1 is a stack of >=2 section curves, loft an
        # OPEN shell through them (guided in spirit by dir2 ordering). Mirrors surf_loft.
        if len(dir1_wires) >= 2:
            try:
                surface = _build_thrusections(
                    dir1_wires, ruled=False, smooth=False,
                    continuity=GeomAbs_Shape.GeomAbs_C1, is_solid=False,
                )
                method = "thrusections"
            except Exception as loft_exc:  # noqa: BLE001
                raise FeatureError(
                    f"surf_boundary: boundary fill failed ({fill_exc}); "
                    f"section-loft fallback also failed ({loft_exc})",
                    hint="dir1 curves must form a closed boundary net, or be >=2 ordered "
                    "section curves for the loft fallback",
                )
        else:
            raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"surf_boundary: build failed: {exc}",
            hint="the boundary curves must form a connected net the surface can span",
        )

    if not isinstance(surface, (Face, Shell)) and getattr(surface, "wrapped", None) is None:
        raise FeatureError("surf_boundary: builder produced no surface geometry")

    resolved: dict[str, float | str] = {
        "_method": method,
        "_dir1": float(len(dir1_wires)),
    }
    if dir2_wires:
        resolved["_dir2"] = float(len(dir2_wires))

    # Surfaces never fuse into the running solid — append as a NEW surface body.
    surface = _to_solid_normalized(surface)
    bodies = list(ctx.bodies)
    bodies.append(surface)
    _store_topo_owners(ctx, fid, surface, None)
    return CompileOutput(bodies=bodies, kind="surface", resolved=resolved)
