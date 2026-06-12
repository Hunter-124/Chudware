"""Hole feature compiler — SolidWorks-style Hole Wizard (simple / cbore / csink / tap).

feature_params.md §hole.

A single hole feature drills N bores into the running body in ONE boolean — exactly
SolidWorks' "Hole Wizard" multi-position behaviour: one feature, many positions on a
target face. All position tools are unioned, then subtracted from the body in a single
cut so downstream selectors see one coherent feature.

Position model (C33-A)
----------------------
The target ``refs.face`` defines a local frame (build123d ``Plane`` on the face: origin =
face centroid, +Z = outward normal). Positions are resolved INTO that frame and each
becomes a hole axis pointing along ``-Z`` (into the material). Sources, in priority order:

1. ``refs.points`` — explicit ``[[u, v], ...]`` offsets in the face's local mm frame
   (NEW, append-only). The primary multi-position path.
2. params ``nx``/``ny`` (counts, ≥1) + ``dx``/``dy`` (pitch, mm) — a rectangular GRID of
   positions, centred on the face frame origin. "Sketch-driven holes lite": the honest,
   fully-functional form-driven path SolidWorks users recognise. Combined with ``points``
   the grid is *appended* to the explicit list.
3. ``refs.positions`` — a list of vertex selectors (contract form). Each picked vertex's
   world point is projected into the face's local frame. Vertex picks are the only viewport
   picks that carry an exact world coordinate today, so this is the click-to-place bridge.
4. Nothing of the above → ONE hole at the face-frame origin (face centre). This is the
   back-compatible single-position default.

A future wave wanting true click-anywhere-on-face placement needs the pick pipeline to
carry the surface hit point (u,v or world xyz) on FACE picks — see PickHit in the host;
today only vertex picks carry coordinates, hence path 3.

Cap: ``_MAX_POSITIONS`` (64) positions; exceeding it is a clean FeatureError.
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Axis, Plane, Solid, Vector

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import describe_faces, resolve

_MAX_POSITIONS = 64

# ISO metric tap-drill / clearance table (mm). diameter = clearance (close fit); the cbore/
# csink dims follow ISO 273 / common shop practice. "custom" uses params.diameter verbatim.
_STD_TABLE: dict[str, dict[str, float]] = {
    #          clearance  cbore_d  cbore_depth  csink_d
    "M2":  {"diameter": 2.4,  "cbore_d": 4.4,  "cbore_depth": 2.0,  "csink_d": 4.0},
    "M2.5":{"diameter": 2.9,  "cbore_d": 5.0,  "cbore_depth": 2.5,  "csink_d": 5.0},
    "M3":  {"diameter": 3.4,  "cbore_d": 6.0,  "cbore_depth": 3.0,  "csink_d": 6.0},
    "M4":  {"diameter": 4.5,  "cbore_d": 8.0,  "cbore_depth": 4.0,  "csink_d": 8.0},
    "M5":  {"diameter": 5.5,  "cbore_d": 10.0, "cbore_depth": 5.0,  "csink_d": 10.0},
    "M6":  {"diameter": 6.6,  "cbore_d": 11.0, "cbore_depth": 6.0,  "csink_d": 12.0},
    "M8":  {"diameter": 9.0,  "cbore_d": 15.0, "cbore_depth": 8.0,  "csink_d": 16.0},
    "M10": {"diameter": 11.0, "cbore_d": 18.0, "cbore_depth": 10.0, "csink_d": 20.0},
    "M12": {"diameter": 13.5, "cbore_d": 20.0, "cbore_depth": 12.0, "csink_d": 24.0},
}

_VALID_STYLES = ("simple", "cbore", "csink", "tap")
_VALID_ENDS = ("through_all", "blind")


# --------------------------------------------------------------------------- helpers


def _body_bbox_diag(body) -> float:
    try:
        bb = body.bounding_box()
        dx = bb.max.X - bb.min.X
        dy = bb.max.Y - bb.min.Y
        dz = bb.max.Z - bb.min.Z
        return max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-3)
    except Exception:  # noqa: BLE001
        return 100.0


def _resolve_face(refs: dict[str, Any], ctx: CompileContext, body) -> Any:
    """Resolve ``refs.face`` to a build123d Face (or raise dangling FeatureError).

    Accepts a face selector (preferred) or a datum-plane id; a datum plane resolves to a
    synthetic Face is NOT supported here — a planar face on the body is the working case.
    """
    sel = refs.get("face")
    if sel is None:
        raise FeatureError(
            "hole: refs.face is required (the planar face the holes are drilled into)"
        )
    if not isinstance(sel, dict):
        raise FeatureError("hole: refs.face must be a face selector")

    infos = describe_faces(body.wrapped)
    diag = _body_bbox_diag(body)
    rr = resolve(sel, infos, diag)
    if rr.index is None:
        raise FeatureError(
            f"hole: face selector dangling — {rr.dangling_reason}",
            dangling_refs=[f"face: {rr.dangling_reason}"],
        )
    return body.faces()[rr.index]


def _face_plane(face) -> Plane:
    """Build a local frame on *face*: origin = centroid, +Z = outward normal."""
    try:
        return Plane(face)
    except Exception:  # noqa: BLE001
        # Fall back to centroid + normal_at for non-trivial faces.
        c = face.center()
        try:
            n = face.normal_at()
        except Exception:  # noqa: BLE001
            n = Vector(0, 0, 1)
        return Plane(origin=c, z_dir=n)


def _collect_positions(
    refs: dict[str, Any], p: dict[str, Any], ctx: CompileContext, plane: Plane,
) -> list[tuple[float, float]]:
    """Gather all (u, v) local-frame positions from points / grid / vertices.

    Empty result → caller drills a single hole at the frame origin (0, 0).
    """
    out: list[tuple[float, float]] = []

    # 1. explicit [u, v] points
    raw_pts = refs.get("points")
    if isinstance(raw_pts, list):
        for i, pt in enumerate(raw_pts):
            if not (isinstance(pt, (list, tuple)) and len(pt) >= 2):
                raise FeatureError(f"hole: refs.points[{i}] must be [u, v]")
            try:
                out.append((float(pt[0]), float(pt[1])))
            except (TypeError, ValueError):
                raise FeatureError(f"hole: refs.points[{i}] is not numeric")

    # 2. rectangular grid (nx × ny at dx, dy pitch), centred on the frame origin
    nx = int(p.get("nx", 1) or 1)
    ny = int(p.get("ny", 1) or 1)
    if nx < 1 or ny < 1:
        raise FeatureError("hole: nx and ny must be >= 1")
    if nx > 1 or ny > 1:
        dx = ctx.dim(p, "dx", 20.0)
        dy = ctx.dim(p, "dy", 20.0)
        x0 = -(nx - 1) * dx / 2.0
        y0 = -(ny - 1) * dy / 2.0
        for iy in range(ny):
            for ix in range(nx):
                out.append((x0 + ix * dx, y0 + iy * dy))

    # 3. vertex selectors → world point projected into the face frame
    positions = refs.get("positions")
    if isinstance(positions, list):
        for i, vsel in enumerate(positions):
            world = _vertex_world(vsel)
            if world is None:
                raise FeatureError(
                    f"hole: refs.positions[{i}] could not yield a point",
                    dangling_refs=[f"positions[{i}]: no centroid on vertex selector"],
                )
            loc = plane.to_local_coords(Vector(*world))
            out.append((float(loc.X), float(loc.Y)))

    return out


def _vertex_world(vsel: Any) -> tuple[float, float, float] | None:
    """Extract a world point from a vertex selector (its stamped centroid)."""
    if isinstance(vsel, dict):
        c = vsel.get("centroid") or vsel.get("point")
        if isinstance(c, (list, tuple)) and len(c) == 3:
            try:
                return (float(c[0]), float(c[1]), float(c[2]))
            except (TypeError, ValueError):
                return None
    return None


def _resolve_dims(style: str, p: dict, ctx: CompileContext) -> dict[str, float]:
    """Resolve diameter / cbore / csink dims from the standard table + custom overrides."""
    standard = p.get("standard", "M3")
    table = _STD_TABLE.get(standard, {})

    if standard == "custom":
        diameter = ctx.dim(p, "diameter", 3.4)
    else:
        # custom param.diameter overrides the table only when explicitly provided.
        diameter = ctx.dim(p, "diameter", table.get("diameter", 3.4))
        if standard in _STD_TABLE and "diameter" not in p:
            diameter = table["diameter"]

    if diameter <= 0.0:
        raise FeatureError("hole: diameter must be > 0")

    dims: dict[str, float] = {"diameter": diameter}

    if style == "cbore":
        dims["cbore_d"] = (
            ctx.dim(p, "cbore_d", table.get("cbore_d", diameter * 1.8))
            if ("cbore_d" in p or "cbore_d" not in table)
            else table["cbore_d"]
        )
        dims["cbore_depth"] = (
            ctx.dim(p, "cbore_depth", table.get("cbore_depth", diameter))
            if ("cbore_depth" in p or "cbore_depth" not in table)
            else table["cbore_depth"]
        )
        if dims["cbore_d"] <= diameter:
            raise FeatureError("hole: cbore_d must be larger than diameter")
    elif style == "csink":
        dims["csink_d"] = (
            ctx.dim(p, "csink_d", table.get("csink_d", diameter * 2.0))
            if ("csink_d" in p or "csink_d" not in table)
            else table["csink_d"]
        )
        dims["csink_angle"] = ctx.dim(p, "csink_angle", 90.0)
        if dims["csink_d"] <= diameter:
            raise FeatureError("hole: csink_d must be larger than diameter")

    return dims


def _make_hole_tool(plane: Plane, uv: tuple[float, float], depth: float,
                    style: str, dims: dict[str, float]) -> Solid:
    """Build one hole tool (cylinder + optional cbore/csink) at local (u, v), drilling -Z.

    The shaft cylinder is over-driven slightly above the face (lift) so a flush bore cuts
    cleanly through the surface; depth is measured into the material from the face.
    """
    lift = 0.05  # mm above the face so the boolean clears the surface coplanarity
    origin = plane.from_local_coords((uv[0], uv[1], lift))
    down = -plane.z_dir
    tool_plane = Plane(origin=origin, z_dir=down)

    r = dims["diameter"] / 2.0
    shaft = Solid.make_cylinder(r, depth + lift, tool_plane)
    tool = shaft

    if style == "cbore":
        cb_r = dims["cbore_d"] / 2.0
        cb_h = dims["cbore_depth"] + lift
        cbore = Solid.make_cylinder(cb_r, cb_h, tool_plane)
        tool = tool + cbore
    elif style == "csink":
        cs_r = dims["csink_d"] / 2.0
        ang = dims.get("csink_angle", 90.0)
        # Cone depth so the top radius is cs_r at a half-angle of ang/2.
        half = math.radians(ang / 2.0)
        cone_h = cs_r / max(math.tan(half), 1e-6)
        # Cone tip points into material: top (at face) radius cs_r, tip radius 0.
        cone = Solid.make_cone(cs_r, 0.0, cone_h + lift, tool_plane)
        tool = tool + cone

    return tool


# --------------------------------------------------------------------------- compiler


@register("hole")
def compile_hole(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    style = p.get("style", "simple")
    end = p.get("end", "through_all")
    if style not in _VALID_STYLES:
        raise FeatureError(f"hole: unknown style '{style}'")
    if end not in _VALID_ENDS:
        raise FeatureError(f"hole: unknown end '{end}'")

    bodies = list(ctx.bodies)
    if not bodies:
        raise FeatureError("hole: no body to drill into")
    body = bodies[-1]

    face = _resolve_face(refs, ctx, body)
    plane = _face_plane(face)

    positions = _collect_positions(refs, p, ctx, plane)
    if not positions:
        positions = [(0.0, 0.0)]  # back-compat single hole at face centre

    if len(positions) > _MAX_POSITIONS:
        raise FeatureError(
            f"hole: {len(positions)} positions exceeds the cap of {_MAX_POSITIONS}",
            hint="split into multiple hole features or reduce the grid count",
        )

    dims = _resolve_dims(style, p, ctx)

    # Depth: through_all = 1.05× body extent along the drill direction; blind = depth.
    drill_dir = -plane.z_dir
    if end == "through_all":
        try:
            bb = body.bounding_box()
            corners = [Vector(bb.min.X, bb.min.Y, bb.min.Z),
                       Vector(bb.max.X, bb.max.Y, bb.max.Z)]
            extent = abs(drill_dir.dot(corners[1]) - drill_dir.dot(corners[0]))
        except Exception:  # noqa: BLE001
            extent = _body_bbox_diag(body)
        depth = max(extent, _body_bbox_diag(body)) * 1.05
    else:
        depth = ctx.dim(p, "depth", 10.0)
        if depth <= 0.0:
            raise FeatureError("hole: blind depth must be > 0")

    # Build every position's tool, union into one tool body (one feature, N holes).
    tools: list[Solid] = []
    for uv in positions:
        try:
            tools.append(_make_hole_tool(plane, uv, depth, style, dims))
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(f"hole: failed to build tool at {uv}: {exc}")

    tool_solid = tools[0]
    for t in tools[1:]:
        tool_solid = tool_solid + t

    target = body
    try:
        result = target - tool_solid
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"hole: boolean cut failed: {exc}",
            hint="check that the holes intersect the target face",
        )

    # Normalise to a single solid where possible.
    try:
        solids = result.solids()
        if len(solids) == 1:
            result = solids[0]
    except Exception:  # noqa: BLE001
        pass

    old_vol = getattr(target, "volume", 0.0)
    new_vol = getattr(result, "volume", 0.0)
    if abs(new_vol - old_vol) < 1e-9:
        raise FeatureError(
            "hole removed no material",
            hint="the positions may fall outside the face / body",
        )

    resolved: dict[str, float] = {
        "diameter": dims["diameter"],
        "depth": depth,
        "_hole_count": float(len(positions)),
    }
    for k in ("cbore_d", "cbore_depth", "csink_d", "csink_angle"):
        if k in dims:
            resolved[k] = dims[k]

    bodies[-1] = result
    return CompileOutput(bodies=bodies, tool_body=tool_solid, resolved=resolved)
