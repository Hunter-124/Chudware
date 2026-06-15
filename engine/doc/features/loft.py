"""Loft feature compiler — boss/cut, a solid blended through 2+ profile sections.

feature_params.md §Sketched features > loft.  SolidWorks-parity option matrix
(docs/SW_PARITY_SPEC.md §2 "Loft — core"): 2+ sections PLUS guide curves, a centerline
spine, start/end tangency, and a thin-feature wall.

Refs
----
- ``refs.sections`` = an ordered list of region selectors (one per profile, ≥2). Each section
  resolves like an extrude profile (``_resolve_profile_faces``); we loft through that section's
  first material face outer wire. Order matters — the solid is blended in list order.
- ``refs.guides`` (optional) = a list of guide-curve refs. Each is a sketch-region ref
  (``{"kind":"region","sketch":fid}``) whose raw edge chain is used as a guide rail that the
  loft surface is steered to follow. (Model-edge guides resolve like sweep's path edges.)
- ``refs.centerline`` (optional) = a single sketch-region / model-edge ref used as the loft
  *spine* (SolidWorks "Centerline parameters"): the sections are blended along this curve.

Params
------
- ``mode``    — ``boss`` | ``cut``.
- ``merge``   — union with the running body (default True).
- ``ruled``   — straight (ruled) transitions vs a smooth blend (default False).
- ``tangency_start`` / ``tangency_end`` — ``none`` (default) | ``normal`` | ``tangent`` |
  ``curvature``. Anything other than ``none`` requests a tangent/curvature-continuous blend at
  that end (SolidWorks "Normal To Profile" / tangency-to-section). Realized with
  ``BRepOffsetAPI_ThruSections`` smoothing + an approximation continuity
  (``tangency=normal/tangent`` → C1, ``curvature`` → C2). This noticeably bows the loft away
  from the straight ruled blend whenever there are interior sections to constrain (see
  ``test_loft.py``).
- ``thin``           — build a thin-wall loft instead of a solid (default False).
- ``thin_thickness`` — wall thickness for the thin feature (default 1.0).
- ``thin_side``      — ``inside`` (default) | ``outside`` | ``both``; which way the wall grows
  off the section wires.

Geometry strategy
-----------------
- **No guides / no centerline** → ``BRepOffsetAPI_ThruSections`` (the classic loft). ``ruled``
  picks straight vs smooth; a non-``none`` tangency turns on smoothing + continuity so the
  surface leaves/enters its end sections tangentially.
- **Centerline and/or guides** → ``BRepOffsetAPI_MakePipeShell``. The centerline (or, absent
  one, a straight spine through the section centroids) is the spine; every section is ``Add``-ed
  as a profile; each guide curve is attached as an *auxiliary spine* (``SetMode``) that steers
  the section orientation. This is the geometrically sound OCCT realization of a guided loft —
  ThruSections cannot take guides directly. Limitation: OCCT drives a guided loft as a
  *swept* multi-section pipe, so the spine must be reasonably smooth and the guides must stay
  close enough to every section to intersect it; pathological guides raise a contained
  FeatureError rather than producing junk.  Per-section *matching* (vertex correspondence) is
  left to OCCT's automatic correspondence — SW's manual matching UI is not yet exposed.
- **Thin** → the chosen builder is run twice, once on the section wires (outer) and once on
  inner wires obtained by 2-D offsetting each section inward (``Wire.offset_2d``), and the inner
  loft is subtracted from the outer — yielding a constant-wall hollow loft.

Built with direct OCCT (mirroring revolve/sweep) for robustness with raw wires.
"""

from __future__ import annotations

from typing import Any

from build123d import Edge, Face, Solid, Vector, Wire
from OCP.BRepFill import BRepFill_TypeOfContact
from OCP.BRepOffsetAPI import (
    BRepOffsetAPI_MakePipeShell,
    BRepOffsetAPI_ThruSections,
)
from OCP.GeomAbs import GeomAbs_Shape

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import (
    _body_bbox_diag,
    _resolve_profile_faces,
    _store_topo_owners,
    _to_solid_normalized,
)
from engine.doc.selectors import describe_edges, resolve


# --------------------------------------------------------------------------- ref resolution


def _resolve_section_faces(section_sels: list, ctx: CompileContext) -> list[Face]:
    """Resolve ``refs.sections`` to one material face per section (ordered)."""
    faces: list[Face] = []
    dangling: list[str] = []
    for i, sel in enumerate(section_sels):
        try:
            resolved = _resolve_profile_faces(sel, ctx)
            if not resolved:
                dangling.append(f"sections[{i}]: resolved no region")
                continue
            faces.append(resolved[0])
        except FeatureError as exc:
            if exc.dangling_refs:
                dangling.extend([f"sections[{i}]: " + dr for dr in exc.dangling_refs])
            else:
                dangling.append(f"sections[{i}]: {exc}")
    if dangling:
        raise FeatureError(
            f"loft: {len(dangling)} section selector(s) failed", dangling_refs=dangling
        )
    if len(faces) < 2:
        raise FeatureError("loft: fewer than 2 sections resolved")
    return faces


def _resolve_curve_wire(ref: Any, ctx: CompileContext, what: str) -> Wire:
    """Resolve a guide/centerline ref to a world-space Wire (the loft spine / rail).

    Accepts the same ref shapes as sweep's path:
    - ``{"kind":"region","sketch":fid}`` → the sketch's raw edge chain, lifted into world space.
    - ``{"kind":"edge",...}`` → a model edge on the running body.
    """
    if isinstance(ref, dict) and ref.get("kind") == "region":
        sid = ref.get("sketch")
        if not sid:
            raise FeatureError(f"loft {what}: region selector missing 'sketch' field")
        store = ctx.datums.get("_sketch_regions")
        data = store.obj.get(sid) if store is not None else None
        if data is None:
            raise FeatureError(f"loft {what}: sketch '{sid}' not compiled (wrong order?)")
        edges = data.get("all_edges") or []
        if not edges:
            raise FeatureError(f"loft {what}: sketch '{sid}' has no curve geometry")
        plane = data["plane"]
        lifted = [plane.location * e for e in edges]
        try:
            return Wire(lifted)
        except Exception:  # noqa: BLE001
            combined = Wire.combine(lifted)
            if not combined:
                raise FeatureError(f"loft {what}: could not assemble a continuous wire")
            return combined[0]

    if isinstance(ref, dict) and ref.get("kind") == "edge":
        bodies = ctx.bodies
        if not bodies:
            raise FeatureError(f"loft {what}: edge selector but no bodies exist")
        body = bodies[-1]
        infos = describe_edges(body.wrapped)
        rr = resolve(ref, infos, _body_bbox_diag(body))
        if rr.index is None:
            raise FeatureError(
                f"loft {what}: edge selector dangling — {rr.dangling_reason}",
                dangling_refs=[f"{what}: {rr.dangling_reason}"],
            )
        return Wire([body.edges()[rr.index]])

    raise FeatureError(
        f"loft {what}: unsupported ref (expected a sketch region or a model edge)"
    )


# --------------------------------------------------------------------------- wire helpers


def _section_wire(face: Face) -> Wire:
    """The outer wire of a section face (the loft profile)."""
    return face.outer_wire()


def _inner_wire(face: Face, thickness: float, side: str) -> Wire:
    """Offset a section's outer wire to build the inner (or outer) thin-wall profile.

    ``side`` = inside → offset inward (negative); outside → outward (positive); both → ±half.
    Returns the wire that bounds the *void* (inside) — for ``outside`` it returns the original
    outer wire and the caller treats the offset wire as the new outer boundary.
    """
    if side == "outside":
        off = abs(thickness)
    elif side == "both":
        off = -abs(thickness) / 2.0
    else:  # inside
        off = -abs(thickness)
    try:
        w = face.outer_wire().offset_2d(off)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"loft thin: could not offset a section by {off:g} (wall too thick for the section?)",
            hint="reduce thin_thickness or widen the section",
        )
    # offset_2d may return a Wire or a list-like; normalize to a single Wire.
    if isinstance(w, (list, tuple)):
        if not w:
            raise FeatureError("loft thin: section offset produced no wire")
        w = w[0]
    return w


# --------------------------------------------------------------------------- builders


def _continuity_for(tangency: str) -> GeomAbs_Shape:
    if tangency in ("curvature", "g2", "c2"):
        return GeomAbs_Shape.GeomAbs_C2
    return GeomAbs_Shape.GeomAbs_C1


def _build_thrusections(
    wires: list[Wire], ruled: bool, smooth: bool, continuity: GeomAbs_Shape,
    is_solid: bool = True,
):
    """Classic loft through ordered section wires via ``BRepOffsetAPI_ThruSections``.

    ``smooth`` (tangency requested) turns on the smoothing approximation with the given
    continuity so the surface leaves/enters its end sections tangentially rather than as a
    straight ruled span.

    ``is_solid`` (default True) builds a capped Solid (the classic solid loft). Pass False to
    build an open lofted SHELL (a SURFACE loft) — the first ThruSections arg is the OCCT
    ``isSolid`` flag, so we thread it straight through. Solid callers keep True (byte-identical).
    """
    builder = BRepOffsetAPI_ThruSections(is_solid, ruled and not smooth, 1e-6)
    if smooth:
        builder.SetSmoothing(True)
        builder.SetContinuity(continuity)
    for w in wires:
        builder.AddWire(w.wrapped)
    builder.Build()
    if not builder.IsDone():
        raise FeatureError("loft: OCCT ThruSections failed (IsDone=False)")
    if not is_solid:
        from build123d import Shell
        return _to_solid_normalized(Shell(builder.Shape()).clean())
    return _to_solid_normalized(Solid(builder.Shape()).clean())


def _spine_through_centroids(faces: list[Face]) -> Wire:
    """A straight poly-line spine joining the section centroids in order.

    Used as the MakePipeShell spine when the user supplied guides but no explicit centerline.
    """
    pts = [Vector(f.center()) for f in faces]
    edges = [Edge.make_line(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    if not edges:
        raise FeatureError("loft: cannot build a spine from fewer than 2 sections")
    if len(edges) == 1:
        return Wire(edges)
    try:
        return Wire(edges)
    except Exception:  # noqa: BLE001
        combined = Wire.combine(edges)
        if not combined:
            raise FeatureError("loft: could not assemble the centroid spine")
        return combined[0]


def _build_pipeshell(
    wires: list[Wire],
    spine: Wire,
    guides: list[Wire],
    *,
    contact: bool,
    correct: bool,
) -> Solid:
    """Guided / centerline loft via ``BRepOffsetAPI_MakePipeShell``.

    Sections are added as profiles along ``spine``; each guide is attached as an auxiliary
    spine (``SetMode``) so the section orientation tracks the guide.

    ``contact``  — translate each section onto the spine (SolidWorks centerline rides through
    the section centroids). True for an explicit centerline; False for a guide-only loft along
    a straight centroid spine, where translating onto the spine would squash off-axis sections.
    ``correct``  — rotate each section to stay orthogonal to the spine tangent (normal-to-
    section-plane departure). True only when a centerline drives the orientation.

    Limitation: OCCT's stable auxiliary-spine mode (``BRepFill_NoContact``) steers section
    *orientation* (the binormal), so a guide that is *coplanar* with the spine's natural frame
    re-parameterizes the blend but does not bulge it radially; a guide that genuinely leaves
    that plane (a 3-D rail) does pull the section corners toward it. The contact-based modes
    that force the surface boundary onto the guide (``BRepFill_Contact`` /
    ``BRepFill_ContactOnBorder``) are far less robust (they routinely fail ``MakeSolid``), so we
    deliberately use ``NoContact``. SolidWorks-style manual section *matching* is likewise left
    to OCCT's automatic vertex correspondence.
    """
    mk = BRepOffsetAPI_MakePipeShell(spine.wrapped)
    # Auxiliary-spine guide rails (SolidWorks "guide curves"). The first guide drives the
    # binormal; OCCT supports a single auxiliary spine, so additional guides are best-effort.
    if guides:
        mk.SetMode(guides[0].wrapped, True, BRepFill_TypeOfContact.BRepFill_NoContact)
    for w in wires:
        mk.Add(w.wrapped, contact, correct)
    if not mk.IsReady():
        raise FeatureError(
            "loft: guided/centerline setup not ready",
            hint="the centerline/guides must reach every section; keep them near the profiles",
        )
    mk.Build()
    if not mk.IsDone():
        raise FeatureError("loft: OCCT MakePipeShell failed (IsDone=False)")
    if not mk.MakeSolid():
        raise FeatureError(
            "loft: could not close the guided loft into a solid",
            hint="every section must be a closed profile",
        )
    return _to_solid_normalized(Solid(mk.Shape()).clean())


# --------------------------------------------------------------------------- compile


@register("loft")
def compile_loft(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})

    mode = p.get("mode", "boss")
    merge = p.get("merge", True)
    ruled = bool(p.get("ruled", False))
    fid = feat.get("id", "?")

    tangency_start = str(p.get("tangency_start", "none") or "none").lower()
    tangency_end = str(p.get("tangency_end", "none") or "none").lower()
    want_tangency = tangency_start != "none" or tangency_end != "none"
    continuity = _continuity_for(tangency_start if tangency_start != "none" else tangency_end)

    thin = bool(p.get("thin", False))
    thin_thickness = ctx.dim(p, "thin_thickness", 1.0) if thin else 0.0
    thin_side = str(p.get("thin_side", "inside") or "inside").lower()

    # --- sections (≥2) ---
    section_sels = refs.get("sections") or []
    if len(section_sels) < 2:
        raise FeatureError("loft: refs.sections needs at least 2 profiles")
    faces = _resolve_section_faces(section_sels, ctx)
    section_wires = [_section_wire(f) for f in faces]

    # --- optional centerline + guides ---
    centerline_ref = refs.get("centerline")
    guide_refs = refs.get("guides") or []

    centerline_wire: Wire | None = None
    if centerline_ref is not None:
        try:
            centerline_wire = _resolve_curve_wire(centerline_ref, ctx, "centerline")
        except FeatureError as exc:
            raise FeatureError(
                f"loft: centerline resolution failed: {exc}", dangling_refs=exc.dangling_refs
            )

    guide_wires: list[Wire] = []
    for gi, gref in enumerate(guide_refs):
        try:
            guide_wires.append(_resolve_curve_wire(gref, ctx, f"guides[{gi}]"))
        except FeatureError as exc:
            raise FeatureError(
                f"loft: guide[{gi}] resolution failed: {exc}", dangling_refs=exc.dangling_refs
            )

    guided = centerline_wire is not None or bool(guide_wires)

    # --- build the loft tool (solid, or thin = outer minus inner) ---
    def _loft_wireset(wires: list[Wire]) -> Solid:
        if guided:
            if centerline_wire is not None:
                # An explicit centerline rides through the section centroids and drives the
                # orientation: translate each section onto the spine and keep it orthogonal.
                return _build_pipeshell(
                    wires, centerline_wire, guide_wires, contact=True, correct=True
                )
            # Guide-only loft: straight centroid spine, guides steer the binormal. Do NOT
            # translate/rotate sections onto the spine (that squashes off-axis sections).
            spine = _spine_through_centroids(faces)
            return _build_pipeshell(
                wires, spine, guide_wires, contact=False, correct=False
            )
        return _build_thrusections(wires, ruled, want_tangency, continuity)

    try:
        if thin:
            outer = _loft_wireset(section_wires)
            inner_wires = [_inner_wire(f, thin_thickness, thin_side) for f in faces]
            if thin_side == "outside":
                # The offset (larger) wires become the new outer boundary; originals are inner.
                outer_big = _loft_wireset(inner_wires)
                inner_small = _loft_wireset(section_wires)
                tool_solid = _to_solid_normalized(outer_big - inner_small)
            else:
                inner = _loft_wireset(inner_wires)
                tool_solid = _to_solid_normalized(outer - inner)
            if getattr(tool_solid, "volume", 0.0) <= 1e-9:
                raise FeatureError(
                    "loft thin: empty wall (thickness consumed the whole section)",
                    hint="reduce thin_thickness",
                )
        else:
            tool_solid = _loft_wireset(section_wires)
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"loft: build failed: {exc}",
            hint="sections must be closed profiles ordered along the loft direction; "
            "guides/centerline must stay near every section",
        )

    resolved: dict[str, float | str] = {}
    if want_tangency:
        resolved["_tangency"] = f"start={tangency_start},end={tangency_end}"
    if guided:
        resolved["_guided"] = (
            ("centerline" if centerline_wire is not None else "")
            + ("+" if centerline_wire is not None and guide_wires else "")
            + (f"{len(guide_wires)} guide(s)" if guide_wires else "")
        )
    if thin:
        resolved["thin_thickness"] = thin_thickness
        resolved["_thin_side"] = thin_side

    # --- apply to the running body ---
    bodies = list(ctx.bodies)
    if not bodies or not merge:
        if mode == "cut":
            raise FeatureError("loft: cut mode requires an existing body to subtract from")
        bodies.append(tool_solid)
        _store_topo_owners(ctx, fid, tool_solid, None)
        return CompileOutput(bodies=bodies, tool_body=tool_solid, op="add", resolved=resolved)

    target = bodies[-1]
    try:
        result = (target - tool_solid) if mode == "cut" else (target + tool_solid)
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"loft: boolean {mode} failed: {exc}",
            hint="the lofted tool may not intersect the body correctly",
        )
    result = _to_solid_normalized(result)

    if mode == "cut":
        if abs(getattr(result, "volume", 0.0) - getattr(target, "volume", 0.0)) < 1e-9:
            raise FeatureError(
                "loft cut removed no material",
                hint="the lofted tool does not intersect the target body",
            )

    bodies[-1] = result
    _store_topo_owners(ctx, fid, result, target)
    return CompileOutput(
        bodies=bodies,
        tool_body=tool_solid,
        op=("cut" if mode == "cut" else "add"),
        resolved=resolved,
    )
