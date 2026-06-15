"""Extruded-surface feature compiler — the open side-wall surface of an extruded profile.

``surf_extrude`` is the surfacing analogue of the solid ``extrude``: it resolves a sketch
profile, computes an extrude DIRECTION and a linear AMOUNT exactly the way the solid extrude
does (``blind`` / ``through_all`` / ``mid_plane`` reading ``params.depth``), then sweeps the
profile's BOUNDARY WIRE(s) along the direction to produce the SIDE-WALL surface only — NO end
caps. The result is an OPEN ``Shell`` (a closed profile yields the open tube wall; a profile
with holes yields the outer tube plus an inner tube). It is returned as a single SURFACE body
(``CompileOutput(..., kind="surface")``), so the core stamps ``BodyOut.kind == "surface"`` and
``mass_props`` reports AREA (= boundary perimeter × amount) instead of volume.

Reuse:
- ``extrude._resolve_profile_faces`` — identical profile/region resolution (a region selector
  resolves to one or more material faces exactly as for a solid extrude).
- ``extrude._store_topo_owners`` — face/edge owner tagging on the produced body.

The wall is built with ``BRepPrimAPI_MakePrism`` over each boundary WIRE (1-D → 2-D = a shell of
side faces, no caps), the same primitive sheet-metal uses to extrude a wire strip. Extruding the
filled FACE instead would close the volume; extruding the WIRE keeps both ends open.

Surfaces never fuse into the running solid — the shell is appended as a NEW body, mirroring
``surf_planar``.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Face, Shape, Shell, Vector, Wire

from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.gp import gp_Vec
from OCP.TopoDS import TopoDS_Builder, TopoDS_Compound

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _resolve_profile_faces, _store_topo_owners


_VALID_ENDS = ("blind", "through_all", "mid_plane")


def _profile_extent_diag(face: Face) -> float:
    """Bounding-box diagonal of a profile face (fallback for through_all with no body)."""
    try:
        bb = face.bounding_box()
        dx = bb.max.X - bb.min.X
        dy = bb.max.Y - bb.min.Y
        dz = bb.max.Z - bb.min.Z
        return max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-3)
    except Exception:  # noqa: BLE001
        return 100.0


def _extrude_wire_wall(wire: Wire, vec: Vector, *, label: str):
    """Sweep a boundary WIRE along *vec* → an open side-wall shell (no end caps)."""
    mk = BRepPrimAPI_MakePrism(
        wire.wrapped, gp_Vec(vec.X, vec.Y, vec.Z), False, True
    )
    if not mk.IsDone():
        raise FeatureError(
            f"surf_extrude: side-wall prism build failed ({label})",
            hint="check that the profile is a valid closed loop in the sketch plane",
        )
    return mk.Shape()


@register("surf_extrude")
def compile_surf_extrude(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    end = p.get("end", "blind")
    if end not in _VALID_ENDS:
        raise FeatureError(
            f"surf_extrude: unknown end '{end}' "
            f"(expected blind | through_all | mid_plane)"
        )
    flip = bool(p.get("flip", False))

    depth = ctx.dim(p, "depth", 10.0)

    # --- resolve profile faces (same machinery as the solid extrude) ---------------------
    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("surf_extrude: refs.profile is required")

    profile_faces: list[Face] = []
    dangling: list[str] = []
    for i, sel in enumerate(profile_sels):
        try:
            profile_faces.extend(_resolve_profile_faces(sel, ctx))
        except FeatureError as exc:
            if exc.dangling_refs:
                dangling.extend([f"profile[{i}]: " + dr for dr in exc.dangling_refs])
            else:
                dangling.append(f"profile[{i}]: {exc}")
    if dangling:
        raise FeatureError(
            f"surf_extrude: {len(dangling)} profile selector(s) failed",
            dangling_refs=dangling,
        )
    if not profile_faces:
        raise FeatureError("surf_extrude: no profiles resolved")

    # --- direction from the first profile face normal (flip negates) ---------------------
    face0 = profile_faces[0]
    try:
        normal = face0.normal_at()
    except Exception:  # noqa: BLE001
        normal = Vector(0, 0, 1)
    normal = Vector(normal).normalized()
    if flip:
        normal = normal * -1.0

    # --- linear amount: blind / through_all / mid_plane (depth = TOTAL height) ------------
    if end == "blind":
        amount = depth
    elif end == "mid_plane":
        amount = depth  # built as a single ±depth/2 prism (see vec below)
    else:  # through_all — no body to bound a surface against, so use the profile span.
        amount = 1.05 * _profile_extent_diag(face0) * 2.0

    if amount <= 0.0:
        raise FeatureError(f"surf_extrude: effective depth {amount:.3g} must be > 0")

    # For mid_plane the wall straddles the sketch plane: start at −depth/2 and sweep +depth.
    if end == "mid_plane":
        start_shift = normal * (-depth / 2.0)
        vec = normal * depth
    else:
        start_shift = None
        vec = normal * amount

    # --- build the side-wall shell(s): one prism per BOUNDARY WIRE (outer + holes) --------
    comp = TopoDS_Compound()
    builder = TopoDS_Builder()
    builder.MakeCompound(comp)
    n_faces = 0
    for fi, face in enumerate(profile_faces):
        try:
            wires = list(face.wires())
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"surf_extrude: could not read profile boundary wires: {exc}"
            )
        if not wires:
            raise FeatureError(
                f"surf_extrude: profile[{fi}] has no boundary wire to sweep"
            )
        for wi, wire in enumerate(wires):
            w = wire
            if start_shift is not None:
                # mid_plane: translate the boundary loop to the −depth/2 start plane.
                w = wire.translate(start_shift)
            wall = _extrude_wire_wall(w, vec, label=f"profile[{fi}].wire[{wi}]")
            builder.Add(comp, wall)
            n_faces += 1

    if n_faces == 0:
        raise FeatureError("surf_extrude: produced no side-wall faces")

    # A single boundary wire → one shell; multiple wires/profiles → a shell-compound. Wrap as a
    # build123d Shell when possible (single shell) else a generic Shape over the compound. Both
    # carry ``.wrapped`` / ``.faces()`` / ``.edges()`` for the core's topology tagging and report
    # is_surface via mass_props.
    surface: Any
    if n_faces == 1:
        # The compound wraps exactly one shell; unwrap it so the body is a plain Shell.
        from OCP.TopAbs import TopAbs_SHELL
        from OCP.TopExp import TopExp
        from OCP.TopTools import TopTools_IndexedMapOfShape

        shells = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(comp, TopAbs_SHELL, shells)
        surface = Shell(shells.FindKey(1)) if shells.Extent() == 1 else Shape(comp)
    else:
        surface = Shape(comp)

    # Append as a NEW surface body (surfaces never merge into the running solid).
    bodies = list(ctx.bodies)
    bodies.append(surface)
    _store_topo_owners(ctx, fid, surface, None)

    resolved: dict[str, float] = {"depth": amount}
    return CompileOutput(bodies=bodies, kind="surface", resolved=resolved)
