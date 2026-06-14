"""Sweep feature compiler — boss/cut, profile region(s) swept along a path.

feature_params.md §Sketched features > sweep.

Profile source: ``refs.profile`` = region selector(s), resolved exactly like extrude/revolve
(``_resolve_profile_faces``). Whole-sketch profiles sweep their material regions.

Path source: ``refs.path`` — one of
- ``{"kind":"region","sketch":fid}`` → a wire built from that sketch's edge chain (open or
  closed). The path sketch's own regions are irrelevant; we use its raw geometry as the spine.
- ``{"kind":"edge",...}`` → a model edge on the running body.

SolidWorks-parity options (``params``):
- **guide curves** (``refs.guides`` = a list of sketch-region/edge selectors): each guide is an
  *auxiliary spine* fed to OCCT ``MakePipeShell.SetMode(guide, curvilinear=True)``. The guide
  steers the section orientation along the path (the section's binormal points toward the guide),
  giving SW's "the profile follows the guide curve" behaviour. Only the FIRST guide can drive a
  single ``MakePipeShell``; additional guides are accepted but a note is recorded (OCCT supports
  one auxiliary spine per shell).
- **twist** (``params.twist_turns`` and/or ``params.twist_degrees``): a constant-rate rotation of
  the section about the local path tangent, accumulated from 0 at the start to the requested total
  at the end. Realised by adding rotated copies of the profile as intermediate sections along the
  spine (multi-section ``Add``), which OCCT interpolates into a smooth twisted sweep.
- **orientation** (``params.orientation``): ``follow_path`` (corrected Frenet, default — the
  section rotates to stay normal to the spine), ``frenet`` (raw Frenet), or ``keep_normal``
  (fixed trihedron — all sections stay parallel to the start frame, SW's "Keep normal constant").
- **thin** (``params.thin`` with ``params.thin_thickness``): sweeps the profile as a thin wall.
  The profile outer wire is offset inward (or outward, ``thin_outward``) by the thickness and a
  second pipe is swept through the offset wire, then subtracted from the outer pipe → a hollow
  tube. Works for the simple and advanced paths alike.

Build strategy: when no advanced option is requested the original OCCT ``BRepOffsetAPI_MakePipe``
fast path is used (cheap, robust for raw faces). Guides/twist/orientation/thin route through
``BRepOffsetAPI_MakePipeShell``, which exposes the auxiliary-spine, multi-section, and trihedron
controls those options need. Per-profile pipes are unioned, then applied to the running body
(boss = union, cut = subtract).
"""

from __future__ import annotations

import math
from typing import Any

from build123d import Axis, Face, Solid, Wire
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe, BRepOffsetAPI_MakePipeShell
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from .base import CompileContext, CompileOutput, FeatureError, register
from engine.doc.selectors import describe_edges, resolve
from .extrude import (
    _body_bbox_diag,
    _resolve_profile_faces,
    _store_topo_owners,
    _to_solid_normalized,
)

_VALID_ORIENT = ("follow_path", "frenet", "keep_normal")


# --------------------------------------------------------------------------- ref → wire


def _region_wire(sketch_id: str, ctx: CompileContext, what: str) -> Wire:
    """Build a single spine/guide Wire from a sketch's raw edge chain (lifted to its plane)."""
    store = ctx.datums.get("_sketch_regions")
    data = store.obj.get(sketch_id) if store is not None else None
    if data is None:
        raise FeatureError(f"sweep {what}: sketch '{sketch_id}' not compiled (wrong order?)")
    edges = data.get("all_edges") or []
    if not edges:
        raise FeatureError(f"sweep {what}: sketch '{sketch_id}' has no path geometry")
    plane = data["plane"]
    lifted = [plane.location * e for e in edges]
    try:
        return Wire(lifted)
    except Exception:  # noqa: BLE001
        combined = Wire.combine(lifted)
        if not combined:
            raise FeatureError(f"sweep {what}: could not assemble a continuous wire")
        return combined[0]


def _resolve_curve_ref(ref: Any, ctx: CompileContext, what: str) -> Wire:
    """Resolve a path/guide ref (sketch region or model edge) to a build123d Wire."""
    if isinstance(ref, dict) and ref.get("kind") == "region":
        sid = ref.get("sketch")
        if not sid:
            raise FeatureError(f"sweep {what}: region selector missing 'sketch' field")
        return _region_wire(sid, ctx, what)

    if isinstance(ref, dict) and ref.get("kind") == "edge":
        bodies = ctx.bodies
        if not bodies:
            raise FeatureError(f"sweep {what}: edge selector but no bodies exist")
        body = bodies[-1]
        infos = describe_edges(body.wrapped)
        rr = resolve(ref, infos, _body_bbox_diag(body))
        if rr.index is None:
            raise FeatureError(
                f"sweep {what}: edge selector dangling — {rr.dangling_reason}",
                dangling_refs=[f"{what}: {rr.dangling_reason}"],
            )
        return Wire([body.edges()[rr.index]])

    raise FeatureError(
        f"sweep {what}: unsupported ref (expected a sketch region or a model edge)"
    )


def _resolve_path_wire(path_ref: Any, ctx: CompileContext) -> Wire:
    """Resolve refs.path to a build123d Wire (the sweep spine)."""
    return _resolve_curve_ref(path_ref, ctx, "path")


# --------------------------------------------------------------------------- thin-wall helper


def _inner_wire(face: Face, thickness: float, outward: bool) -> Wire:
    """Offset the profile's outer wire by ``thickness`` for a thin-wall sweep.

    SW "thin feature": the wall grows inward from the sketch by default; ``thin_outward`` flips
    it to grow outward. We offset the OUTER boundary wire and let the caller sweep both wires and
    subtract, yielding a hollow tube.
    """
    outer = face.outer_wire()
    dist = abs(thickness) if outward else -abs(thickness)
    try:
        return outer.offset_2d(dist)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"sweep: thin offset failed ({exc})",
            hint="thin thickness must be smaller than the profile so the offset stays valid",
        )


# --------------------------------------------------------------------------- pipe builders


def _pipe_simple(path_wire: Wire, profile: Any) -> Solid:
    """Original fast path: BRepOffsetAPI_MakePipe over a raw face or wire."""
    shape = profile.wrapped
    mk = BRepOffsetAPI_MakePipe(path_wire.wrapped, shape)
    mk.Build()
    if not mk.IsDone():
        raise FeatureError("sweep: OCCT MakePipe failed (IsDone=False)")
    return Solid(mk.Shape()).clean()


def _fixed_trihedron(path_wire: Wire) -> gp_Ax2:
    """A gp_Ax2 frame at the path start, normal = start tangent (for keep-normal-constant)."""
    try:
        origin = path_wire.position_at(0.0)
        tangent = path_wire.tangent_at(0.0)
    except Exception:  # noqa: BLE001
        origin, tangent = path_wire.start_point(), None
    if tangent is None or tangent.length < 1e-9:
        tangent = type(origin)(0, 0, 1)
    tangent = tangent.normalized()
    # Pick any reference X perpendicular to the tangent.
    ref = type(origin)(1, 0, 0)
    if abs(tangent.dot(ref)) > 0.9:
        ref = type(origin)(0, 1, 0)
    xdir = (ref - tangent * tangent.dot(ref)).normalized()
    return gp_Ax2(
        gp_Pnt(origin.X, origin.Y, origin.Z),
        gp_Dir(tangent.X, tangent.Y, tangent.Z),
        gp_Dir(xdir.X, xdir.Y, xdir.Z),
    )


def _apply_orientation(mk: BRepOffsetAPI_MakePipeShell, orientation: str,
                       path_wire: Wire, guide_wire: Wire | None) -> None:
    """Configure the MakePipeShell trihedron / auxiliary spine for the requested options."""
    if guide_wire is not None:
        # Guide curve drives the section frame (auxiliary spine, curvilinear correspondence).
        mk.SetMode(guide_wire.wrapped, True)
        return
    if orientation == "keep_normal":
        mk.SetMode(_fixed_trihedron(path_wire))     # fixed trihedron → parallel sections
    elif orientation == "frenet":
        mk.SetMode(True)                            # raw Frenet
    else:  # follow_path (default)
        mk.SetMode(False)                           # corrected Frenet


def _twisted_sections(path_wire: Wire, profile_wire: Wire, total_deg: float,
                      samples: int) -> list[Wire]:
    """Rotated copies of ``profile_wire`` placed along ``path_wire`` for a constant-rate twist.

    At parameter t∈[0,1] the section sits at ``path.location_at(t)`` (orientation following the
    path) and is pre-rotated about its local normal (Z of the local frame) by ``total_deg * t``.
    OCCT interpolates the listed sections into a smooth twisted sweep.
    """
    sections: list[Wire] = []
    n = max(samples, 2)
    for k in range(n + 1):
        t = k / n
        loc = path_wire.location_at(t)
        ang = total_deg * t
        sec = profile_wire.rotate(Axis.Z, ang).locate(loc)
        sections.append(sec)
    return sections


def _pipe_shell(path_wire: Wire, profile: Face, *, orientation: str,
                guide_wire: Wire | None, twist_deg: float, make_solid: bool = True):
    """Advanced path: BRepOffsetAPI_MakePipeShell with guide/twist/orientation controls.

    ``profile`` is a face; we sweep its outer wire and let MakeSolid cap the ends. When a twist is
    requested we add rotated section copies along the spine; otherwise we add the single profile.

    ``make_solid`` (default True) calls ``MakeSolid()`` to cap the swept shell into a closed Solid
    — the classic solid-sweep behaviour. Pass False to skip the cap and return the open swept
    Shell (a SURFACE sweep). Solid callers keep ``make_solid=True``, so their result is unchanged.
    """
    prof_wire = profile.outer_wire()
    mk = BRepOffsetAPI_MakePipeShell(path_wire.wrapped)
    _apply_orientation(mk, orientation, path_wire, guide_wire)

    if abs(twist_deg) > 1e-9:
        # Sample density: at least 8 per full turn so the interpolated twist stays smooth.
        samples = max(8, int(math.ceil(abs(twist_deg) / 360.0 * 12.0)))
        for sec in _twisted_sections(path_wire, prof_wire, twist_deg, samples):
            mk.Add(sec.wrapped, False, False)
    else:
        # WithContact=False, WithCorrection=False — keep the profile where the sketch placed it.
        mk.Add(prof_wire.wrapped, False, False)

    mk.Build()
    if not mk.IsDone():
        raise FeatureError("sweep: OCCT MakePipeShell failed (IsDone=False)")
    if not make_solid:
        from build123d import Shell
        return Shell(mk.Shape()).clean()
    if not mk.MakeSolid():
        raise FeatureError(
            "sweep: MakePipeShell could not close the swept shell into a solid",
            hint="the profile must be a closed region for a solid sweep",
        )
    return Solid(mk.Shape()).clean()


# --------------------------------------------------------------------------- per-profile tool


def _build_profile_tool(face: Face, path_wire: Wire, *, advanced: bool, orientation: str,
                        guide_wire: Wire | None, twist_deg: float, thin: bool,
                        thin_thickness: float, thin_outward: bool) -> Solid:
    """Build the swept solid for ONE profile face, honouring thin/advanced options."""

    def _solid(prof_face: Face) -> Solid:
        if advanced:
            return _pipe_shell(
                path_wire, prof_face, orientation=orientation,
                guide_wire=guide_wire, twist_deg=twist_deg,
            )
        return _pipe_simple(path_wire, prof_face)

    outer_solid = _solid(face)

    if not thin:
        return outer_solid

    if thin_thickness <= 0.0:
        raise FeatureError(
            "sweep: thin feature requires a positive thickness",
            hint="set params.thin_thickness > 0",
        )
    inner_wire = _inner_wire(face, thin_thickness, thin_outward)
    try:
        inner_face = Face(inner_wire)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"sweep: thin inner profile invalid ({exc})")
    inner_solid = _solid(inner_face)
    if thin_outward:
        # Outward wall: the offset wire is the OUTER boundary; subtract the original profile pipe.
        hollow = inner_solid - outer_solid
    else:
        hollow = outer_solid - inner_solid
    return _to_solid_normalized(hollow)


# --------------------------------------------------------------------------- compiler


@register("sweep")
def compile_sweep(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    mode = p.get("mode", "boss")
    merge = p.get("merge", True)
    fid = feat.get("id", "?")

    orientation = p.get("orientation", "follow_path")
    if orientation not in _VALID_ORIENT:
        raise FeatureError(
            f"sweep: unknown orientation '{orientation}' "
            f"(expected one of {', '.join(_VALID_ORIENT)})"
        )

    # Twist — accept turns and/or degrees; total twist is their sum.
    twist_turns = ctx.dim(p, "twist_turns", 0.0) if p.get("twist_turns") is not None else 0.0
    twist_extra_deg = ctx.dim(p, "twist_degrees", 0.0) if p.get("twist_degrees") is not None else 0.0
    twist_deg = twist_turns * 360.0 + twist_extra_deg

    # Thin feature.
    thin = bool(p.get("thin", False))
    thin_thickness = ctx.dim(p, "thin_thickness", 0.0) if p.get("thin_thickness") is not None else 0.0
    thin_outward = bool(p.get("thin_outward", False))

    # Profiles.
    profile_sels = refs.get("profile") or []
    if not profile_sels:
        raise FeatureError("sweep: refs.profile is required")

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
        raise FeatureError(f"sweep: {len(dangling)} profile selector(s) failed",
                           dangling_refs=dangling)
    if not profile_faces:
        raise FeatureError("sweep: no profiles resolved")

    # Path.
    path_ref = refs.get("path")
    if path_ref is None:
        raise FeatureError("sweep: refs.path is required")
    try:
        path_wire = _resolve_path_wire(path_ref, ctx)
    except FeatureError as exc:
        raise FeatureError(f"sweep: path resolution failed: {exc}",
                           dangling_refs=exc.dangling_refs)

    # Guide curves (optional). OCCT MakePipeShell drives one auxiliary spine per shell — we use
    # the first guide and note any extras.
    guide_sels = refs.get("guides") or []
    guide_wire: Wire | None = None
    guide_note: str | None = None
    if guide_sels:
        try:
            guide_wire = _resolve_curve_ref(guide_sels[0], ctx, "guide[0]")
        except FeatureError as exc:
            raise FeatureError(f"sweep: guide resolution failed: {exc}",
                               dangling_refs=exc.dangling_refs)
        if len(guide_sels) > 1:
            guide_note = (
                f"{len(guide_sels)} guides given; OCCT uses 1 auxiliary spine per sweep — "
                "the first guide was applied"
            )

    # Advanced path needed when any SW-parity option is active. (Thin alone still uses the simple
    # pipe per wall, so it does not force the shell route.)
    advanced = (
        guide_wire is not None
        or abs(twist_deg) > 1e-9
        or orientation != "follow_path"
    )

    tool_bodies: list[Any] = []
    for face in profile_faces:
        try:
            tool_bodies.append(_build_profile_tool(
                face, path_wire,
                advanced=advanced, orientation=orientation, guide_wire=guide_wire,
                twist_deg=twist_deg, thin=thin, thin_thickness=thin_thickness,
                thin_outward=thin_outward,
            ))
        except FeatureError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FeatureError(
                f"sweep: build failed: {exc}",
                hint="the profile must be a closed region positioned at the path start",
            )

    tool_solid = tool_bodies[0]
    for tb in tool_bodies[1:]:
        tool_solid = tool_solid + tb
    tool_solid = _to_solid_normalized(tool_solid)

    resolved: dict[str, Any] = {}
    if abs(twist_deg) > 1e-9:
        resolved["twist_degrees"] = twist_deg
    if orientation != "follow_path":
        resolved["orientation"] = orientation
    if thin:
        resolved["thin_thickness"] = thin_thickness
    if guide_wire is not None:
        resolved["guides"] = len(guide_sels)
    if guide_note:
        resolved["_guide_note"] = guide_note

    bodies = list(ctx.bodies)
    if not bodies or not merge:
        if mode == "cut":
            raise FeatureError("sweep: cut mode requires an existing body to subtract from")
        bodies.append(tool_solid)
        _store_topo_owners(ctx, fid, tool_solid, None)
        return CompileOutput(bodies=bodies, tool_body=tool_solid, op="add", resolved=resolved)

    target = bodies[-1]
    try:
        result = (target - tool_solid) if mode == "cut" else (target + tool_solid)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(f"sweep: boolean {mode} failed: {exc}",
                           hint="the swept tool may not intersect the body correctly")
    result = _to_solid_normalized(result)

    if mode == "cut":
        if abs(getattr(result, "volume", 0.0) - getattr(target, "volume", 0.0)) < 1e-9:
            raise FeatureError("sweep cut removed no material",
                               hint="the swept tool does not intersect the target body")

    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, target)
    return CompileOutput(bodies=bodies, tool_body=tool_solid,
                         op=("cut" if mode == "cut" else "add"), resolved=resolved)
