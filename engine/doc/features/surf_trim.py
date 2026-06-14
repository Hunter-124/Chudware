"""Trim / untrim surface feature compilers — cut a surface body with a tool, or restore a
trimmed face to its full natural domain.

feature_params.md §Surfacing > trim / untrim; docs/SW_PARITY_SPEC.md "Trim Surface".

SolidWorks "Trim Surface" cuts a surface body with a *tool* (another surface, a model face, or a
curve) and keeps the picked side(s); "Untrim Surface" does the inverse — it regrows a trimmed face
back out to the full natural domain of its underlying geometric surface.

``surf_trim``
-------------
``refs.surface`` is a BODY selector naming a SURFACE body in the rolling list; ``refs.tool`` is a
FACE/surface selector (a model face, a datum plane, or a face on ANOTHER surface body). The faces
of the surface body are split by the tool with OCCT's general BOP splitter (``BRepAlgoAPI_Splitter``
— the same operator ``split.py`` uses to divide a *solid* by a surface; surfaces split the same
way). Each resulting face piece is bucketed by its centroid projection along the tool's local
normal, exactly as ``split._bucket_pieces`` does for solids, and ``params.keep`` selects which
side survives:

- ``keep="both"``     → keep every piece (the surface is just sub-divided);
- ``keep="positive"`` → keep the +normal-side piece(s);
- ``keep="negative"`` → keep the −normal-side piece(s).

The kept faces are re-sewn into a surface body that REPLACES the input surface body in place; the
result stays ``kind="surface"`` (volume ≈ 0). Keeping one side yields strictly LESS area than the
untrimmed surface — the defining property the trim test asserts.

``surf_untrim``
---------------
``refs.faces`` are FACE selectors on a surface body. Each face is rebuilt directly on its
underlying ``BRep_Tool.Surface_s`` (NO trim wire) via ``BRepBuilderAPI_MakeFace`` over the
surface's FULL natural domain — its own natural bounds where finite (sphere, bounded B-spline),
or the trimmed face's UV span grown by a generous margin where a direction is naturally infinite
(a plane in U,V; a cylinder along its axis, which OCCT marks ±2e100). Either way the trim contour
is discarded, so the rebuilt face is strictly LARGER. The grown faces replace the trimmed ones → a
larger surface body (``kind="surface"``). Untrimming a trimmed face grows its area back, the
inverse of trim.

Error containment (contained :class:`FeatureError`): no body / a dangling surface or tool selector
/ a tool that does not divide the surface / an unbounded underlying surface that cannot be capped.
"""

from __future__ import annotations

from typing import Any

from build123d import Face, Shape, Shell, Vector

from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_Sewing
from OCP.BRep import BRep_Tool
from OCP.BRepTools import BRepTools
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_ListOfShape

from .base import CompileContext, CompileOutput, FeatureError, register
from .boolean import _resolve_body_set
from .dress import _resolve_faces
from .extrude import _store_topo_owners
from .split import _bucket_pieces, _resolve_tool_face

_VALID_KEEP = ("positive", "negative", "both")


# --------------------------------------------------------------------------- surface-body wrapping


def _wrap_surface(raw):
    """Wrap a raw OCP face/shell as the most specific build123d surface wrapper.

    The core's surface machinery (``mass_props`` / topology tagging) only needs ``.wrapped`` and
    ``.faces()`` / ``.edges()``, so a generic ``Shape`` is always safe; we just pick the tightest
    wrapper the result allows (``Face`` for one face, ``Shell`` for a sewn sheet)."""
    try:
        st = raw.ShapeType()
    except Exception:  # noqa: BLE001
        return Shape(raw)
    if st == TopAbs_FACE:
        return Face(TopoDS.Face_s(raw))
    if st == TopAbs_SHELL:
        return Shell(TopoDS.Shell_s(raw))
    return Shape(raw)


def _sew_faces(faces: list):
    """Sew a list of raw OCP faces into a single surface body wrapper.

    One face → a lone ``Face``. Several faces → sew them into a ``Shell`` so adjacent pieces share
    edges (a loose compound would still measure the same total area but tessellate as disjoint
    sheets). Falls back to whatever the sewing produced if it is not a clean single shell."""
    if not faces:
        raise FeatureError("surf_trim: nothing left after trim (every piece was discarded)")
    if len(faces) == 1:
        return _wrap_surface(faces[0])
    sew = BRepBuilderAPI_Sewing(1e-6)
    for f in faces:
        sew.Add(f)
    sew.Perform()
    return _wrap_surface(sew.SewedShape())


# --------------------------------------------------------------------------- surface body resolution


def _surface_body_indices(ctx: CompileContext) -> list[int]:
    """Indices of bodies in ``ctx.bodies`` that the core has stamped ``kind=="surface"``.

    The compiler core records non-solid kinds in ``ctx.datums["_topo_owners"]`` only as ownership,
    not kind; the live kind map lives on the core, not the context. So we detect surface bodies
    geometrically: a surface body has (near-)zero enclosed volume but positive area. This matches
    ``evaluate._is_surface_shape`` and needs no extra plumbing."""
    out: list[int] = []
    for i, b in enumerate(ctx.bodies):
        vol = abs(float(getattr(b, "volume", 0.0) or 0.0))
        area = 0.0
        try:
            area = float(getattr(b, "area", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            area = 0.0
        if vol < 1e-9 and area > 1e-9:
            out.append(i)
    return out


def _resolve_surface_body(sel: Any, ctx: CompileContext) -> int:
    """Resolve ``refs.surface`` to an INDEX into ``ctx.bodies`` (a surface body).

    Primary path — a BODY selector ``{"kind":"body","owner_feature":<fid>}`` resolved by
    ``boolean._resolve_body_set`` (geometric signature match against the owning feature's tool
    solid). Surface features register no ``tool_body``, so that path only fires for surface bodies
    produced by a solid-style feature; when it misses we fall back to the surface bodies present in
    the rolling list (the last one — SolidWorks trims the active surface body), and finally to the
    last body of any kind. This keeps the common "trim the surface I just built" case working
    whether or not the owner feature happened to stash a tool solid."""
    surf_idx = _surface_body_indices(ctx)

    if isinstance(sel, dict) and sel.get("kind") == "body":
        try:
            idx_list = _resolve_body_set([sel], list(ctx.bodies), ctx, "surface")
            if idx_list:
                return idx_list[0]
        except FeatureError:
            # Surface owners carry no tool_body anchor — fall through to the surface-body fallback.
            pass

    if surf_idx:
        return surf_idx[-1]
    if ctx.bodies:
        return len(ctx.bodies) - 1
    raise FeatureError(
        "surf_trim: refs.surface does not resolve to any surface body",
        dangling_refs=["surface: no surface body in the rolling list"],
    )


# --------------------------------------------------------------------------- face splitting


def _split_face_by_tool(face_wrapped, tool_face) -> list:
    """Split a single raw OCP FACE by ``tool_face`` (a build123d Face) with the general BOP
    splitter, returning the resulting raw FACE pieces. Empty when the tool does not divide the
    face (BOP yields a single face — the tool misses / is tangent / lies off the face)."""
    args = TopTools_ListOfShape()
    args.Append(face_wrapped)
    tools = TopTools_ListOfShape()
    tools.Append(tool_face.wrapped)

    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(args)
    splitter.SetTools(tools)
    splitter.Build()
    if not splitter.IsDone():
        return []

    pieces: list = []
    exp = TopExp_Explorer(splitter.Shape(), TopAbs_FACE)
    while exp.More():
        pieces.append(TopoDS.Face_s(exp.Current()))
        exp.Next()
    # A single resulting face means the tool did not actually divide this face.
    return pieces if len(pieces) >= 2 else []


def _face_centroid(face_wrapped) -> Vector:
    """Area centroid of a raw OCP face (used to bucket pieces along the tool normal)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face_wrapped, props)
    c = props.CentreOfMass()
    return Vector(c.X(), c.Y(), c.Z())


def _keep_to_split_side(keep: str) -> str:
    """Map the surf_trim keep vocabulary onto ``split._bucket_pieces``' above/below vocabulary."""
    if keep == "positive":
        return "above"
    if keep == "negative":
        return "below"
    return "both"


@register("surf_trim")
def compile_surf_trim(feat: dict, ctx: CompileContext) -> CompileOutput:
    p = feat.get("params", {})
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    keep = p.get("keep", "both")
    if keep not in _VALID_KEEP:
        raise FeatureError(
            f"surf_trim: unknown keep '{keep}' (expected positive | negative | both)"
        )

    if not ctx.bodies:
        raise FeatureError("surf_trim: no body to trim")

    surf_sel = refs.get("surface")
    if surf_sel is None:
        raise FeatureError(
            "surf_trim: refs.surface is required (a surface body)",
            dangling_refs=["surface: missing"],
        )
    surf_idx = _resolve_surface_body(surf_sel, ctx)
    surface_body = ctx.bodies[surf_idx]

    tool_sel = refs.get("tool")
    if tool_sel is None:
        raise FeatureError(
            "surf_trim: refs.tool is required (a face / surface to cut with)",
            dangling_refs=["tool: missing"],
        )
    # Resolve the tool to a single build123d Face. ``split._resolve_tool_face`` searches every
    # running body (so a face on ANOTHER surface body, a datum-plane face, or a model face all
    # resolve) and raises a contained dangling FeatureError on a miss.
    if isinstance(tool_sel, dict) and tool_sel.get("kind") == "face":
        _owner_body, tool_face, _owner_idx = _resolve_tool_face(tool_sel, ctx)
    else:
        raise FeatureError(
            "surf_trim: refs.tool must be a face/surface selector",
            dangling_refs=[f"tool: unsupported selector {tool_sel!r}"],
        )

    # The tool's local normal — the axis along which keep-side is measured (median bucket split),
    # exactly the convention split.py uses for a non-planar surface tool.
    try:
        ref_normal = Vector(tool_face.normal_at()).normalized()
    except Exception:  # noqa: BLE001
        ref_normal = Vector(0, 0, 1)

    # Split EACH face of the surface body by the tool; faces the tool misses pass through whole.
    # ``_bucket_pieces`` keys on build123d ``.center()``, so the split pieces are wrapped as
    # build123d ``Face`` objects (the splitter hands back raw ``TopoDS_Face`` shapes).
    split_count = 0
    kept_faces: list = []
    for face in surface_body.faces():
        pieces_raw = _split_face_by_tool(face.wrapped, tool_face)
        if not pieces_raw:
            kept_faces.append(face)
            continue
        split_count += 1
        pieces = [Face(TopoDS.Face_s(p)) for p in pieces_raw]
        kept = _bucket_pieces(pieces, ref_normal, _keep_to_split_side(keep))
        kept_faces.extend(kept)

    if split_count == 0:
        raise FeatureError(
            "surf_trim: the tool does not intersect/divide the surface",
            hint="position the tool so it passes across the surface body",
        )
    if not kept_faces:
        raise FeatureError(
            "surf_trim: every face piece was discarded — nothing left to keep",
            hint="check the keep side; 'both' keeps all pieces",
        )

    new_surface = _sew_faces([f.wrapped for f in kept_faces])

    # Replace the input surface body with the trimmed one, moved to the END of the rolling list so
    # it becomes the TOP body: the compiler core only stamps ``kind`` (and owner-tags topology) on
    # the top body, so the result must be last for it to be reported as a surface. Every other
    # body (the tool solid, etc.) keeps its place; only the trimmed surface is repositioned.
    bodies = [b for i, b in enumerate(ctx.bodies) if i != surf_idx]
    bodies.append(new_surface)
    _store_topo_owners(ctx, fid, new_surface, surface_body)

    resolved = {
        "_faces_split": float(split_count),
        "_faces_kept": float(len(kept_faces)),
        "keep_positive": 1.0 if keep in ("both", "positive") else 0.0,
        "keep_negative": 1.0 if keep in ("both", "negative") else 0.0,
    }
    return CompileOutput(bodies=bodies, kind="surface", resolved=resolved)


# --------------------------------------------------------------------------- untrim


_INF = 1e99           # OCCT marks a naturally-unbounded surface parameter with ±2e100.
_UNTRIM_MARGIN = 10.0  # how far an unbounded direction grows past the trimmed face's own UV span.


def _untrim_face(face: Face):
    """Rebuild *face* over the FULL natural domain of its underlying geometric surface.

    ``BRep_Tool.Surface_s`` hands back the unbounded ``Geom_Surface`` carrying the face. We rebuild
    a fresh face directly on that surface (NO trim wire), so the result is the surface's own patch,
    not the trimmed contour — exactly SolidWorks "Untrim".

    Where the surface is naturally bounded (a sphere in both U and V, a cylinder/cone in U, a
    bounded B-spline) we use those natural bounds. Where a direction is naturally infinite (a
    plane in both U,V; a cylinder along its axis — OCCT reports ±2e100 there) there is no finite
    "natural rim" to restore to, so we cap that direction by expanding the trimmed face's CURRENT
    UV span by a generous margin. Either way the rebuilt face is strictly LARGER than the trimmed
    one (the trim contour is gone), which is the defining property of untrim. Returns a raw OCP
    face; raises a contained error only if the kernel cannot build any capped face at all."""
    surf = BRep_Tool.Surface_s(face.wrapped)

    nu0, nu1, nv0, nv1 = surf.Bounds()                 # natural surface bounds (may be ±inf)
    fu0, fu1, fv0, fv1 = BRepTools.UVBounds_s(face.wrapped)  # the trimmed face's own UV span

    def _pick(natural_lo: float, natural_hi: float, face_lo: float, face_hi: float):
        # Prefer the finite natural bound; otherwise grow past the trimmed face's span.
        lo = natural_lo if abs(natural_lo) < _INF else (face_lo - _UNTRIM_MARGIN)
        hi = natural_hi if abs(natural_hi) < _INF else (face_hi + _UNTRIM_MARGIN)
        return lo, hi

    u0, u1 = _pick(nu0, nu1, fu0, fu1)
    v0, v1 = _pick(nv0, nv1, fv0, fv1)

    mk = BRepBuilderAPI_MakeFace(surf, u0, u1, v0, v1, 1e-6)
    if not mk.IsDone():
        raise FeatureError(
            "surf_untrim: could not rebuild the face on its underlying surface",
            hint="the base surface may be degenerate; untrim needs a recoverable base surface",
        )
    return mk.Face()


@register("surf_untrim")
def compile_surf_untrim(feat: dict, ctx: CompileContext) -> CompileOutput:
    refs = feat.get("refs", {})
    fid = feat.get("id", "?")

    if not ctx.bodies:
        raise FeatureError("surf_untrim: no body to untrim")

    face_sels = refs.get("faces") or []
    if not face_sels:
        raise FeatureError("surf_untrim: refs.faces is required and must be non-empty")

    # Untrim acts on a SURFACE body — resolve faces against the surface body the picks belong to.
    # The picks are face selectors; resolve them against each surface body (and, failing that, the
    # top body) and keep the body that resolves them all.
    surf_idx = _surface_body_indices(ctx)
    candidate_indices = surf_idx + [len(ctx.bodies) - 1]
    seen: set[int] = set()

    chosen_idx: int | None = None
    chosen_faces: list[Face] = []
    last_dangling: list[str] = []
    for bi in candidate_indices:
        if bi in seen:
            continue
        seen.add(bi)
        body = ctx.bodies[bi]
        faces, dangling = _resolve_faces(face_sels, body, "faces", ctx=ctx)
        if not dangling and len(faces) == len(face_sels):
            chosen_idx = bi
            chosen_faces = faces
            break
        last_dangling = dangling

    if chosen_idx is None:
        raise FeatureError(
            f"surf_untrim: {len(last_dangling)} face selector(s) dangling",
            dangling_refs=last_dangling or ["faces: no surface body resolved the picks"],
        )

    body = ctx.bodies[chosen_idx]

    # Key each picked face by a rounded geometric signature so we can swap the rebuilt (larger)
    # face in for the trimmed one while leaving every other face of the body untouched.
    def _key(f) -> tuple:
        c = f.center()
        return (round(c.X, 5), round(c.Y, 5), round(c.Z, 5))

    pick_keys = {_key(f) for f in chosen_faces}

    new_faces: list = []
    untrimmed = 0
    grown_any = False
    for f in body.faces():
        if _key(f) in pick_keys:
            rebuilt = _untrim_face(f)
            new_faces.append(rebuilt)
            untrimmed += 1
            try:
                grown_any = grown_any or (Face(TopoDS.Face_s(rebuilt)).area > float(f.area) + 1e-9)
            except Exception:  # noqa: BLE001
                pass
        else:
            new_faces.append(f.wrapped)

    if untrimmed == 0:
        raise FeatureError("surf_untrim: no picked face was found on the surface body")

    new_surface = _sew_faces(new_faces)

    # Move the rebuilt surface body to the END so it becomes the top body (see surf_trim — the core
    # only stamps ``kind``/topology owners on the top body).
    bodies = [b for i, b in enumerate(ctx.bodies) if i != chosen_idx]
    bodies.append(new_surface)
    _store_topo_owners(ctx, fid, new_surface, body)

    resolved = {
        "_faces_untrimmed": float(untrimmed),
        "_grew_area": 1.0 if grown_any else 0.0,
    }
    return CompileOutput(bodies=bodies, kind="surface", resolved=resolved)
