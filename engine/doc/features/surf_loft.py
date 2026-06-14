"""Lofted SURFACE feature compiler — an open shell blended through 2+ profile sections.

``surf_loft`` is the surfacing sibling of the solid ``loft`` feature (``loft.py``). Where the
solid loft caps its ends and returns a closed ``Solid``, the surface loft returns the *open*
lofted SHELL — a sheet body with no end caps (a tube/funnel wall). It tags its output
``kind="surface"`` so the core stamps the BodyOut accordingly: tessellation emits a two-sided
mesh and ``mass_props`` reports AREA (``is_surface`` True, ``volume_mm3``≈0) instead of volume.

Refs (identical shapes to ``loft``; see ``loft.py`` for the full contract)
----
- ``refs.sections`` = an ordered list of region selectors (one per profile, ≥2). Each resolves
  to a material face via ``loft._resolve_section_faces``; the loft runs through each section's
  outer wire in list order.
- ``refs.guides`` (optional) = guide-curve refs (sketch-region / model-edge), resolved via
  ``loft._resolve_curve_wire``.
- ``refs.centerline`` (optional) = a single spine ref, resolved the same way.

Params
------
- ``ruled``   — straight (ruled) transitions vs a smooth blend (default False), exactly as
  ``loft`` (the ``BRepOffsetAPI_ThruSections`` ruled flag — only when no tangency smoothing).
- ``tangency_start`` / ``tangency_end`` — ``none`` (default) | ``normal`` | ``tangent`` |
  ``curvature``; a non-``none`` value turns on the smoothing approximation with the matching
  continuity so the surface leaves/enters its end sections tangentially (mirrors ``loft``).

Geometry strategy
-----------------
- **No guides / no centerline** → ``loft._build_thrusections(..., is_solid=False)``. The first
  ``BRepOffsetAPI_ThruSections`` arg is the OCCT ``isSolid`` flag; passing ``False`` yields an
  open lofted shell (no caps) instead of a capped solid. ``ruled`` picks straight vs smooth; a
  non-``none`` tangency enables smoothing + continuity.
- **Centerline and/or guides** → ``BRepOffsetAPI_MakePipeShell`` along the spine (an explicit
  centerline, else a straight spine through the section centroids), with each guide attached as
  an auxiliary spine — but WITHOUT ``MakeSolid``, so the result stays an open swept SHELL. This
  mirrors ``loft._build_pipeshell`` (same ``contact``/``correct`` reasoning) minus the closing
  step that the surface variant intentionally omits.

Surfaces never fuse into the running solid — the shell is appended as a NEW body, like
``surf_planar``.
"""

from __future__ import annotations

from build123d import Shell, Solid, Wire
from OCP.BRepFill import BRepFill_TypeOfContact
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell

from .base import CompileContext, CompileOutput, FeatureError, register
from .extrude import _store_topo_owners, _to_solid_normalized
from .loft import (
    _build_thrusections,
    _continuity_for,
    _resolve_curve_wire,
    _resolve_section_faces,
    _section_wire,
    _spine_through_centroids,
)


def _build_pipeshell_surface(
    wires: list[Wire], spine: Wire, guides: list[Wire], *, contact: bool, correct: bool,
):
    """Guided / centerline loft as an OPEN swept shell via ``BRepOffsetAPI_MakePipeShell``.

    Identical setup to ``loft._build_pipeshell`` (sections added as profiles along ``spine``,
    each guide an auxiliary spine), but it does NOT call ``MakeSolid`` — the surface loft keeps
    the open shell. ``contact``/``correct`` carry the same meaning as in the solid loft.
    """
    mk = BRepOffsetAPI_MakePipeShell(spine.wrapped)
    if guides:
        mk.SetMode(guides[0].wrapped, True, BRepFill_TypeOfContact.BRepFill_NoContact)
    for w in wires:
        mk.Add(w.wrapped, contact, correct)
    if not mk.IsReady():
        raise FeatureError(
            "surf_loft: guided/centerline setup not ready",
            hint="the centerline/guides must reach every section; keep them near the profiles",
        )
    mk.Build()
    if not mk.IsDone():
        raise FeatureError("surf_loft: OCCT MakePipeShell failed (IsDone=False)")
    return _to_solid_normalized(Shell(mk.Shape()).clean())


@register("surf_loft")
def compile_surf_loft(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    ruled = bool(p.get("ruled", False))
    tangency_start = str(p.get("tangency_start", "none") or "none").lower()
    tangency_end = str(p.get("tangency_end", "none") or "none").lower()
    want_tangency = tangency_start != "none" or tangency_end != "none"
    continuity = _continuity_for(tangency_start if tangency_start != "none" else tangency_end)

    # --- sections (≥2) ---  (reuses loft's resolution, identical to the solid loft)
    section_sels = refs.get("sections") or []
    if len(section_sels) < 2:
        raise FeatureError("surf_loft: refs.sections needs at least 2 profiles")
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
                f"surf_loft: centerline resolution failed: {exc}",
                dangling_refs=exc.dangling_refs,
            )

    guide_wires: list[Wire] = []
    for gi, gref in enumerate(guide_refs):
        try:
            guide_wires.append(_resolve_curve_wire(gref, ctx, f"guides[{gi}]"))
        except FeatureError as exc:
            raise FeatureError(
                f"surf_loft: guide[{gi}] resolution failed: {exc}",
                dangling_refs=exc.dangling_refs,
            )

    guided = centerline_wire is not None or bool(guide_wires)

    # --- build the open lofted shell ---
    try:
        if guided:
            if centerline_wire is not None:
                surface = _build_pipeshell_surface(
                    section_wires, centerline_wire, guide_wires, contact=True, correct=True
                )
            else:
                spine = _spine_through_centroids(faces)
                surface = _build_pipeshell_surface(
                    section_wires, spine, guide_wires, contact=False, correct=False
                )
        else:
            # Classic loft as an OPEN shell: is_solid=False → no end caps (a surface).
            surface = _build_thrusections(
                section_wires, ruled, want_tangency, continuity, is_solid=False
            )
    except FeatureError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeatureError(
            f"surf_loft: build failed: {exc}",
            hint="sections must be closed profiles ordered along the loft direction; "
            "guides/centerline must stay near every section",
        )

    if not isinstance(surface, (Shell, Solid)) and getattr(surface, "wrapped", None) is None:
        raise FeatureError("surf_loft: builder produced no shell geometry")

    resolved: dict[str, float | str] = {"_ruled": "yes" if ruled else "no"}
    if want_tangency:
        resolved["_tangency"] = f"start={tangency_start},end={tangency_end}"
    if guided:
        resolved["_guided"] = (
            ("centerline" if centerline_wire is not None else "")
            + ("+" if centerline_wire is not None and guide_wires else "")
            + (f"{len(guide_wires)} guide(s)" if guide_wires else "")
        )

    # Surfaces never fuse into the running solid — append as a NEW surface body.
    bodies = list(ctx.bodies)
    bodies.append(surface)
    _store_topo_owners(ctx, fid, surface, None)
    return CompileOutput(bodies=bodies, kind="surface", resolved=resolved)
