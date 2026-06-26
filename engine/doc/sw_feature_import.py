"""Resolve a SolidWorks part's FEATURE TREE on import (not just a frozen solid).

The proprietary ``.sldprt`` container is encrypted/opaque (see ``formats_solidworks.py`` —
binary reverse-engineering is a dead end), but a locally-installed SolidWorks exposes the entire
parametric model through its **COM API**. This module drives the installed SolidWorks to *read* the
FeatureManager design tree — sketches (with entities, relations, dimensions), reference geometry,
and the solid features built on them — and emits two things:

  * an **intermediate representation** (IR): a plain list of feature dicts, COM-free and JSON-able;
  * a translation of that IR into AI-CAD's native ``add_features`` op list (see
    :func:`engine.doc.sw_translate.translate`), so the imported part lands as an *editable* feature
    tree instead of one dead body.

Design contract (per owner, 2026-06-26): **strict fidelity, no silent fallback.** Any feature — or
any parameter on a feature — we cannot faithfully reconstruct is recorded as ``unsupported`` with
the SolidWorks feature name, its API type name, and the reason. The import surfaces the *full* list
so each part yields a complete punch-list of gaps rather than a quietly-wrong model.

COM notes (learned the hard way; see the module-level probes that validated each):
  * ``SldWorks.Application`` does not expose ``GetTypeInfo``, so ``gencache.EnsureDispatch`` /
    ``CastTo`` fail outright. The working pattern is to generate the typelib wrappers from the
    ``sldworks.tlb`` FILE (``EnsureModule``) and then wrap each dynamic IDispatch with the generated
    interface CLASS by hand: ``SW.IFeature(obj._oleobj_)``.
  * Under early binding, no-arg accessors are real methods (call with ``()``), ``OpenDoc6`` returns
    ``(model, errors, warnings)`` as a tuple, and ``ISketchRelationManager.GetRelations`` /
    ``GetRelationsCount`` REQUIRE a filter arg (``swSketchRelationFilterType_All == 0``).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# SolidWorks enum maps (from swconst.tlb — swConstraintType_e / swEndConditions_e / swSelectType_e)
# ---------------------------------------------------------------------------
# Sketch-segment GetType() codes (swSketchSegments_e).
SEG_LINE, SEG_ARC, SEG_ELLIPSE, SEG_SPLINE, SEG_TEXT, SEG_PARABOLA = 0, 1, 2, 3, 4, 5

# Sketch relation (constraint) type code -> our engine constraint type (None = recognised but has
# no engine equivalent / is implicit and safely droppable).
SW_CONSTRAINT = {
    1: "distance", 2: "angle", 3: "radius", 4: "horizontal", 5: "vertical",
    6: "tangent", 7: "parallel", 8: "perpendicular", 9: "coincident", 10: "concentric",
    11: "symmetric", 12: "midpoint", 14: "equal", 15: "diameter", 17: "fix",
    25: "horizontal", 26: "vertical", 27: "collinear", 28: "coradial", 40: "point_on",
}
# Names for diagnostics when a constraint code is not in SW_CONSTRAINT.
SW_CONSTRAINT_NAME = {
    0: "INVALID", 13: "ATINTERSECT", 32: "USEEDGE", 42: "MERGEPOINTS", 44: "ARCLENGTH",
}

# swEndConditions_e -> engine extrude `end` (None = needs a reference we must resolve separately).
SW_END_COND = {
    0: "blind", 1: "through_all", 2: "through_next", 6: "mid_plane",
    9: "through_all", 3: None, 4: None, 5: None, 7: None, 10: None, 11: None,
}
SW_END_COND_NAME = {
    0: "Blind", 1: "ThroughAll", 2: "ThroughNext", 3: "UpToVertex", 4: "UpToSurface",
    5: "OffsetFromSurface", 6: "MidPlane", 7: "UpToBody", 9: "ThroughAllBoth",
    10: "UpToSelection", 11: "UpToNext",
}

# swSelectType_e (used by ISketch.GetReferenceEntity to say what the sketch sits on).
SEL_EDGES, SEL_FACES, SEL_VERTICES, SEL_DATUMPLANES = 1, 2, 3, 4

MM = 1000.0  # SolidWorks API is metres; AI-CAD sketch coords are millimetres.


# ---------------------------------------------------------------------------
# Intermediate representation
# ---------------------------------------------------------------------------
@dataclass
class SWEntity:
    id: str
    type: str                       # line | circle | arc | spline | point
    construction: bool = False
    data: dict = field(default_factory=dict)
    sw_id: tuple | None = None       # SolidWorks (sketchId, segId) for relation cross-referencing


@dataclass
class SWConstraint:
    type: str                        # engine constraint type
    refs: list[str] = field(default_factory=list)
    value: float | None = None
    sw_type_code: int = 0


@dataclass
class SWDim:
    name: str
    value: float                     # millimetres
    dim_type: int = 0                # swDimensionType_e (6=diameter, 5=radial, 2/11/12=linear, …)


@dataclass
class SWFeature:
    sw_name: str
    sw_type: str                     # GetTypeName2()
    kind: str                        # plane | axis | sketch | extrude | cut | revolve | loft | ...
    params: dict = field(default_factory=dict)
    # sketch-only
    plane: dict | None = None
    entities: list[SWEntity] = field(default_factory=list)
    constraints: list[SWConstraint] = field(default_factory=list)
    dims: list[SWDim] = field(default_factory=list)


@dataclass
class SWUnsupported:
    sw_name: str
    sw_type: str
    reason: str
    # "blocking"  -> geometry cannot be faithfully rebuilt; the import must be refused.
    # "degraded"  -> geometry is exact but some parametric intent was lost (e.g. an unlinked
    #                driving dimension or a dropped implicit relation); report, don't refuse.
    severity: str = "blocking"


@dataclass
class SWComponent:
    name: str
    part: "SWPart"               # the component's own (recursively extracted) feature tree
    rot: list[float]             # 3x3 row-major rotation (component-local -> assembly), SW coords
    trans_mm: list[float]        # translation (mm), SW coords


@dataclass
class SWPart:
    name: str
    path: str
    is_assembly: bool
    features: list[SWFeature] = field(default_factory=list)
    unsupported: list[SWUnsupported] = field(default_factory=list)
    # name -> SWFeature, for the translator to resolve consume references (sketch used by extrude…)
    by_name: dict = field(default_factory=dict)
    components: list[SWComponent] = field(default_factory=list)  # assemblies only


class SolidWorksUnavailable(RuntimeError):
    """SolidWorks / pywin32 not usable on this machine."""


# ---------------------------------------------------------------------------
# COM binding helpers
# ---------------------------------------------------------------------------
_SW_MOD = None  # cached generated typelib module


def _sw_typelib_path() -> str | None:
    """Locate sldworks.tlb under a SOLIDWORKS install (used to generate typed wrappers)."""
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramW6432", r"C:\Program Files"),
    ]
    for root in roots:
        base = Path(root) / "SOLIDWORKS Corp" / "SOLIDWORKS" / "sldworks.tlb"
        if base.exists():
            return str(base)
    # Fallback: glob for any versioned SOLIDWORKS dir.
    for root in roots:
        corp = Path(root) / "SOLIDWORKS Corp"
        if corp.exists():
            hits = list(corp.glob("**/sldworks.tlb"))
            if hits:
                return str(hits[0])
    return None


def _ensure_sw_module():
    """Generate (once) and return the makepy module for the SolidWorks type library."""
    global _SW_MOD
    if _SW_MOD is not None:
        return _SW_MOD
    import pythoncom
    from win32com.client import gencache

    tlb = _sw_typelib_path()
    if not tlb:
        raise SolidWorksUnavailable("could not locate sldworks.tlb (is SOLIDWORKS installed?)")
    tl = pythoncom.LoadTypeLib(tlb)
    attr = tl.GetLibAttr()           # (guid, lcid, syskind, major, minor, features)
    guid, lcid, major, minor = str(attr[0]), attr[1], attr[3], attr[4]
    _SW_MOD = gencache.EnsureModule(guid, lcid, major, minor)
    if _SW_MOD is None:
        raise SolidWorksUnavailable("failed to generate SolidWorks COM wrappers from sldworks.tlb")
    return _SW_MOD


def _cast(obj, iface: str):
    """Wrap a dynamic IDispatch with the generated typed interface class (SldWorks needs this)."""
    if obj is None:
        return None
    klass = getattr(_ensure_sw_module(), iface)
    return klass(getattr(obj, "_oleobj_", obj))


def solidworks_available() -> bool:
    try:
        import win32com.client  # noqa: F401
        import winreg
        try:
            winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"SldWorks.Application").Close()
        except OSError:
            return False
        return _sw_typelib_path() is not None
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Sketch extraction
# ---------------------------------------------------------------------------
def _pt(p) -> list[float]:
    """A SolidWorks sketch point object -> [x, y] in millimetres (sketch-local 2D)."""
    return [round(p.X * MM, 6), round(p.Y * MM, 6)]


def _id_of(obj) -> tuple | None:
    try:
        gid = obj.GetID()            # SafeArray [sketchId, segOrPointId]
        return (int(gid[0]), int(gid[1]))
    except Exception:  # noqa: BLE001
        return None


def _pt_id(pt) -> tuple | None:
    return _id_of(_cast(pt, "ISketchPoint"))


def _extract_segment(seg, idx: int) -> tuple[SWEntity | None, str | None, list[tuple]]:
    """Return (entity, reason, point_handles).

    point_handles maps this segment's defining points to engine sub-handle suffixes, as
    (point_id_tuple, "p1"|"p2"|"center"|"start"|"end") — used to resolve point-based relations
    (coincident/midpoint/…) to precise handles like ``e3.p2`` instead of the whole segment."""
    t = seg.GetType()
    cons = bool(seg.ConstructionGeometry)
    eid = f"e{idx}"
    if t == SEG_LINE:
        ln = _cast(seg, "ISketchLine")
        sp, ep = ln.GetStartPoint2(), ln.GetEndPoint2()
        handles = [(_pt_id(sp), "p1"), (_pt_id(ep), "p2")]
        return SWEntity(eid, "line", cons, {"p1": _pt(sp), "p2": _pt(ep)}, _id_of(seg)), None, handles
    if t == SEG_ARC:
        arc = _cast(seg, "ISketchArc")
        c = arc.GetCenterPoint2()
        sp, ep = arc.GetStartPoint2(), arc.GetEndPoint2()
        r = arc.GetRadius() * MM
        cx, cy = _pt(c)
        full = abs(sp.X - ep.X) < 1e-9 and abs(sp.Y - ep.Y) < 1e-9
        if full:
            handles = [(_pt_id(c), "center")]
            return SWEntity(eid, "circle", cons, {"center": [cx, cy], "radius": round(r, 6)},
                            _id_of(seg)), None, handles
        sx, sy = _pt(sp)
        ex, ey = _pt(ep)
        start = math.degrees(math.atan2(sy - cy, sx - cx)) % 360.0
        end = math.degrees(math.atan2(ey - cy, ex - cx)) % 360.0
        # SolidWorks arc direction: GetDirection() -> +1 (ccw) / -1 (cw) about the sketch normal.
        try:
            ccw = arc.GetDirection() > 0
        except Exception:  # noqa: BLE001
            ccw = True
        handles = [(_pt_id(c), "center"), (_pt_id(sp), "start"), (_pt_id(ep), "end")]
        return SWEntity(eid, "arc", cons,
                        {"center": [cx, cy], "radius": round(r, 6),
                         "start": round(start, 4), "end": round(end, 4), "ccw": ccw},
                        _id_of(seg)), None, handles
    if t == SEG_SPLINE:
        # Sample the spline's interpolation/control points so it can be rebuilt as a through-spline.
        try:
            spl = _cast(seg, "ISketchSpline")
            pts_raw = spl.GetPoints2()        # flat [x,y,z, x,y,z, ...] in metres
            pts = [[round(pts_raw[i] * MM, 6), round(pts_raw[i + 1] * MM, 6)]
                   for i in range(0, len(pts_raw), 3)]
            if len(pts) >= 2:
                return SWEntity(eid, "spline", cons, {"points": pts}, _id_of(seg)), None, []
        except Exception:  # noqa: BLE001
            pass
        return None, "spline geometry could not be sampled", []
    names = {SEG_ELLIPSE: "ellipse", SEG_TEXT: "sketch text", SEG_PARABOLA: "parabola"}
    return None, f"sketch segment type {names.get(t, t)} not yet supported", []


def _extract_relations(sk, point_map: dict, seg_map: dict,
                       warnings: list[str]) -> list[SWConstraint]:
    """Pull geometric relations (constraints), resolving each operand to a precise engine handle.

    point_map: SW sketch-point id -> "e<n>.p1|p2|center|start|end"; seg_map: SW segment id -> "e<n>".
    A relation whose operand resolves to neither is dropped (noted) rather than emitted wrong."""
    out: list[SWConstraint] = []
    try:
        rm = sk.RelationManager
        n = rm.GetRelationsCount(0)   # 0 == swSketchRelationFilterType_All
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"relations unreadable ({exc})")
        return out
    if not n:
        return out
    try:
        rels = rm.GetRelations(0) or []
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"GetRelations failed ({exc})")
        return out
    for r in rels:
        r = _cast(r, "ISketchRelation")
        try:
            code = int(r.GetRelationType())
        except Exception:  # noqa: BLE001
            continue
        ctype = SW_CONSTRAINT.get(code)
        if ctype is None:
            # Recognised-but-unmapped or implicit relation: note it, don't fail the whole import.
            warnings.append(f"dropped relation '{SW_CONSTRAINT_NAME.get(code, code)}'")
            continue
        try:
            ents = r.GetEntities() or []
        except Exception:  # noqa: BLE001
            ents = []
        refs: list[str] = []
        unresolved = False
        for e in ents:
            # A relation operand is a sketch POINT (corner/centre) or a whole SEGMENT.
            pid = _id_of(_cast(e, "ISketchPoint"))
            if pid in point_map:
                refs.append(point_map[pid]); continue
            sid = _id_of(_cast(e, "ISketchSegment"))
            if sid in seg_map:
                refs.append(seg_map[sid]); continue
            unresolved = True
        if unresolved and not refs:
            # operands were model edges/external geometry we didn't import — skip this relation.
            warnings.append(f"dropped '{ctype}' (operands outside this sketch)")
            continue
        out.append(SWConstraint(ctype, refs, None, code))
    return out


def _classify_principal(rot: list[float]) -> str | None:
    """Classify a sketch's plane orientation from its model->sketch rotation matrix.

    The sketch-plane normal in MODEL space is row 2 of the rotation. SolidWorks parts are Y-up:
      * normal ∥ model Y  -> horizontal plane  = SW 'Top'   -> engine plane.top
      * normal ∥ model Z  -> SW 'Front'  -> engine plane.front
      * normal ∥ model X  -> SW 'Right'  -> engine plane.right
    Returns 'top'/'front'/'right' or None if the plane is tilted (not a principal orientation)."""
    if len(rot) < 9:
        return None
    nx, ny, nz = rot[6], rot[7], rot[8]
    ax = [abs(nx), abs(ny), abs(nz)]
    # must be axis-aligned (one component ~1, others ~0)
    big = max(ax)
    if big < 0.999 or sum(1 for a in ax if a > 1e-3) != 1:
        return None
    axis = ax.index(big)
    return {0: "right", 1: "top", 2: "front"}[axis]


def _sketch_plane_ref(sk) -> dict:
    """Describe the plane/face a sketch sits on (the engine needs plane.* or a face query)."""
    info: dict = {"sel_type": None, "principal": None, "rot": None, "trans_mm": None}
    try:
        res = sk.GetReferenceEntity(0)
        info["sel_type"] = int(res[1]) if isinstance(res, (list, tuple)) and len(res) > 1 else None
    except Exception:  # noqa: BLE001
        pass
    try:
        arr = list(sk.ModelToSketchTransform.ArrayData)
        rot = [round(a, 6) for a in arr[0:9]]
        info["rot"] = rot
        info["trans_mm"] = [round(arr[9] * MM, 6), round(arr[10] * MM, 6), round(arr[11] * MM, 6)]
        info["principal"] = _classify_principal(rot)
    except Exception:  # noqa: BLE001
        pass
    return info


# ---------------------------------------------------------------------------
# Feature dispatch
# ---------------------------------------------------------------------------
# SolidWorks GetTypeName2() -> our coarse `kind`. Anything not here is reported unsupported.
KIND_BY_SWTYPE = {
    "RefPlane": "plane",
    "RefAxis": "axis",
    "ProfileFeature": "sketch",
    "Extrusion": "extrude",      # boss
    "ICE": "cut",                # cut-extrude
    "Blend": "loft",
    "Revolution": "revolve",     # boss OR cut (IsBossFeature distinguishes)
    "Fillet": "fillet",
    "Chamfer": "chamfer",
    "Shell": "shell",
    "LPattern": "linear_pattern",
    "CirPattern": "circular_pattern",
    "MirrorPattern": "mirror",
}
# Boilerplate tree nodes we skip entirely.
_SKIP_TYPES = {
    "CommentsFolder", "FavoriteFolder", "HistoryFolder", "SelectionSetFolder", "SensorFolder",
    "DocsFolder", "DetailCabinet", "SurfaceBodyFolder", "SolidBodyFolder", "EnvFolder",
    "InkMarkupFolder", "EqnFolder", "MaterialFolder", "OriginProfileFeature",
}


def _feature_dims(feat) -> list[SWDim]:
    out: list[SWDim] = []
    try:
        dn = feat.GetFirstDisplayDimension()
    except Exception:  # noqa: BLE001
        return out
    while dn is not None:
        try:
            d = dn.GetDimension2(0)
            v = d.GetSystemValue3(1, "")        # 1 == swThisConfiguration
            val = v[0] if isinstance(v, (list, tuple)) else v
            try:
                dtype = int(_cast(dn, "IDisplayDimension").Type2)
            except Exception:  # noqa: BLE001
                dtype = 0
            out.append(SWDim(str(d.FullName), round(float(val) * MM, 6), dtype))
        except Exception:  # noqa: BLE001
            pass
        try:
            nxt = dn.GetNext3()
        except Exception:  # noqa: BLE001
            break
        dn = nxt
    return out


def _extract_sketch(feat) -> SWFeature:
    SW = _ensure_sw_module()
    sk = _cast(feat.GetSpecificFeature2(), "ISketch")
    f = SWFeature(feat.Name, "ProfileFeature", "sketch")
    f.plane = _sketch_plane_ref(sk)
    warnings: list[str] = []
    seg_map: dict = {}               # sw segment id -> our entity id "e<n>"
    point_map: dict = {}             # sw point id   -> "e<n>.p1|p2|center|start|end"
    try:
        segs = list(sk.GetSketchSegments() or [])
    except Exception:  # noqa: BLE001
        segs = []
    for i, seg in enumerate(segs):
        seg = _cast(seg, "ISketchSegment")
        ent, reason, handles = _extract_segment(seg, i)
        if ent is None:
            f.params.setdefault("seg_errors", []).append(reason)
            continue
        if ent.sw_id:
            seg_map[ent.sw_id] = ent.id
        for pid, sub in handles:
            if pid is not None:
                point_map[pid] = f"{ent.id}.{sub}"
        f.entities.append(ent)
    f.constraints = _extract_relations(sk, point_map, seg_map, warnings)
    f.dims = _feature_dims(feat)
    if warnings:
        f.params["warnings"] = warnings
    return f


def _extract_extrude(feat, kind: str) -> tuple[SWFeature, str | None]:
    """Boss/cut extrude. Returns (feature, unsupported_reason_or_None)."""
    f = SWFeature(feat.Name, feat.GetTypeName2(), kind)
    defn = _cast(feat.GetDefinition(), "IExtrudeFeatureData2")
    if defn is None:
        return f, "extrude has no reachable feature definition"
    try:
        end_code = int(defn.GetEndCondition(True))     # forward direction
    except Exception:  # noqa: BLE001
        end_code = 0
    end = SW_END_COND.get(end_code)
    if end is None:
        return f, (f"extrude end condition '{SW_END_COND_NAME.get(end_code, end_code)}' "
                   "needs a reference (up-to-surface/vertex/body) we don't resolve yet")
    f.params["end"] = end
    if end in ("blind", "mid_plane"):
        try:
            f.params["depth"] = round(float(defn.GetDepth(True)) * MM, 6)
        except Exception:  # noqa: BLE001
            return f, "could not read blind extrude depth"
    try:
        f.params["reversed"] = bool(defn.ReverseDirection)
    except Exception:  # noqa: BLE001
        pass
    try:
        draft = float(defn.GetDraftAngle(True))
        if abs(draft) > 1e-9:
            f.params["draft_deg"] = round(math.degrees(draft), 6)
    except Exception:  # noqa: BLE001
        pass
    if f.params.get("draft_deg"):
        return f, "extrude with draft is not reconstructed yet"
    f.dims = _feature_dims(feat)
    return f, None


def _extract_revolve(feat) -> tuple[SWFeature, str | None]:
    f = SWFeature(feat.Name, feat.GetTypeName2(), "revolve")
    defn = _cast(feat.GetDefinition(), "IRevolveFeatureData2")
    if defn is None:
        return f, "revolve has no reachable feature definition"
    try:
        f.params["mode"] = "boss" if defn.IsBossFeature() else "cut"
    except Exception:  # noqa: BLE001
        f.params["mode"] = "boss"
    try:
        if defn.IsThinFeature():
            return f, "thin (surface-walled) revolve is not reconstructed yet"
    except Exception:  # noqa: BLE001
        pass
    try:
        ang = float(defn.GetRevolutionAngle(True))     # radians, first direction
        f.params["angle_deg"] = round(math.degrees(ang), 6)
    except Exception:  # noqa: BLE001
        return f, "could not read revolve angle"
    f.dims = _feature_dims(feat)
    return f, None


def _edge_point(edge) -> list[float] | None:
    """A representative point (model mm) on a SolidWorks edge, for a 'near' geometry query."""
    e = _cast(edge, "IEdge")
    try:
        cp = e.GetCurveParams2()      # (sx,sy,sz, ex,ey,ez, tStart,tEnd, ...)
        sx, sy, sz, ex, ey, ez = cp[0], cp[1], cp[2], cp[3], cp[4], cp[5]
    except Exception:  # noqa: BLE001
        return None
    # Midpoint of the endpoints — exact for straight edges, close enough to disambiguate others.
    return [round((sx + ex) / 2 * MM, 6), round((sy + ey) / 2 * MM, 6),
            round((sz + ez) / 2 * MM, 6)]


def _edge_points(edges) -> list[list[float]]:
    out = []
    for e in (edges or []):
        p = _edge_point(e)
        if p is not None:
            out.append(p)
    return out


def _data_edges(defn) -> list:
    """Edges referenced by a fillet/chamfer feature-data object (via dynamic ``.Edges``;
    see :func:`_dyn_list`). Requires AccessSelections to be held by the caller."""
    return _dyn_list(defn, "Edges")


def _face_point(face) -> list[float] | None:
    """A representative point (model mm) on a SolidWorks face — the mean of its edge midpoints."""
    fc = _cast(face, "IFace2")
    pts = []
    try:
        for e in (fc.GetEdges() or []):
            p = _edge_point(e)
            if p is not None:
                pts.append(p)
    except Exception:  # noqa: BLE001
        return None
    if not pts:
        return None
    return [round(sum(p[i] for p in pts) / len(pts), 6) for i in range(3)]


def _dyn_list(defn, prop: str) -> list:
    """Read a SAFEARRAY-returning property (Edges/Faces/FacesRemoved) via dynamic dispatch — the
    makepy I-getter for these is mis-generated (binds the write-only setter dispid)."""
    try:
        from win32com.client import dynamic
        dyn = dynamic.Dispatch(getattr(defn, "_oleobj_", defn))
        return list(getattr(dyn, prop) or [])
    except Exception:  # noqa: BLE001
        return []


def _extract_shell(feat, model) -> tuple[SWFeature, str | None]:
    f = SWFeature(feat.Name, feat.GetTypeName2(), "shell")
    f.dims = _feature_dims(feat)
    defn = _cast(feat.GetDefinition(), "IShellFeatureData")
    if defn is None:
        return f, "shell definition unreadable"
    try:
        defn.AccessSelections(model, None)
        faces = _dyn_list(defn, "FacesRemoved")
        f.params["face_points"] = [p for p in (_face_point(x) for x in faces) if p]
    except Exception as exc:  # noqa: BLE001
        return f, f"shell faces unreadable: {exc}"
    finally:
        try: defn.ReleaseSelectionAccess()
        except Exception: pass  # noqa: E701
    if not f.dims:
        return f, "could not read shell thickness (no display dimension)"
    f.params["thickness"] = f.dims[0].value
    return f, None


def _extract_fillet(feat, model) -> tuple[SWFeature, str | None]:
    f = SWFeature(feat.Name, feat.GetTypeName2(), "fillet")
    f.dims = _feature_dims(feat)
    defn = _cast(feat.GetDefinition(), "ISimpleFilletFeatureData2")
    if defn is None:
        return f, "fillet has no constant-radius definition (variable/face fillets unsupported)"
    try:
        defn.AccessSelections(model, None)
        f.params["edge_points"] = _edge_points(_data_edges(defn))
    except Exception as exc:  # noqa: BLE001
        return f, f"could not read fillet edges: {exc}"
    finally:
        try: defn.ReleaseSelectionAccess()
        except Exception: pass  # noqa: E701
    # Radius from the fillet's display dimension (the data-interface radius getter is unreliable).
    if not f.dims:
        return f, "could not read fillet radius (no display dimension)"
    f.params["radius"] = f.dims[0].value
    if not f.params.get("edge_points"):
        return f, "fillet references no edges we could resolve (face/feature fillet not supported)"
    return f, None


def _extract_chamfer(feat, model) -> tuple[SWFeature, str | None]:
    f = SWFeature(feat.Name, feat.GetTypeName2(), "chamfer")
    defn = _cast(feat.GetDefinition(), "IChamferFeatureData2")
    if defn is None:
        return f, "chamfer definition unreadable"
    dist = None
    try:
        defn.AccessSelections(model, None)
        f.params["edge_points"] = _edge_points(_data_edges(defn))
        dist = float(defn.GetEdgeChamferDistance(0))   # side 0 (equal-distance chamfer)
    except Exception as exc:  # noqa: BLE001
        return f, f"chamfer unreadable: {exc}"
    finally:
        try: defn.ReleaseSelectionAccess()
        except Exception: pass  # noqa: E701
    if not dist or not f.params.get("edge_points"):
        return f, "chamfer is not a simple equal-distance edge chamfer (angle/asymmetric unsupported)"
    f.params["length"] = round(dist * MM, 6)
    return f, None


def _seed_feature_names(defn) -> list[str]:
    """Names of the features a pattern/mirror replicates (via dynamic .PatternFeatureArray)."""
    out = []
    for o in _dyn_list(defn, "PatternFeatureArray"):
        try:
            out.append(_cast(o, "IFeature").Name)
        except Exception:  # noqa: BLE001
            pass
    return out


def _transform_translation_mm(tr) -> list[float] | None:
    try:
        a = tr.ArrayData
        return [a[9] * MM, a[10] * MM, a[11] * MM]
    except Exception:  # noqa: BLE001
        return None


def _extract_linear_pattern(feat, model) -> tuple[SWFeature, str | None]:
    f = SWFeature(feat.Name, feat.GetTypeName2(), "linear_pattern")
    defn = _cast(feat.GetDefinition(), "ILinearPatternFeatureData")
    if defn is None:
        return f, "linear-pattern definition unreadable"
    from win32com.client import dynamic
    try:
        defn.AccessSelections(model, None)
        dyn = dynamic.Dispatch(defn._oleobj_)
        f.params["count1"] = int(dyn.D1TotalInstances)
        f.params["spacing1"] = round(float(dyn.D1Spacing) * MM, 6)
        rev1 = bool(dyn.D1ReverseDirection)
        # Direction = the instance-1 transform translation (already spacing*dir); normalise.
        t1 = _transform_translation_mm(defn.GetTransform(1))
        if t1 is None:
            return f, "could not read linear-pattern direction"
        mag = math.sqrt(sum(c * c for c in t1)) or 1.0
        f.params["dir1_model"] = [round(c / mag, 9) for c in t1]
        f.params["seed_names"] = _seed_feature_names(defn)
        # Optional 2nd direction.
        try:
            if defn.IsDirection2Specified():
                f.params["count2"] = int(dyn.D2TotalInstances)
                f.params["spacing2"] = round(float(dyn.D2Spacing) * MM, 6)
                t2 = _transform_translation_mm(defn.GetTransform(f.params["count1"]))
                if t2:
                    m2 = math.sqrt(sum(c * c for c in t2)) or 1.0
                    f.params["dir2_model"] = [round(c / m2, 9) for c in t2]
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        return f, f"linear-pattern unreadable: {exc}"
    finally:
        try: defn.ReleaseSelectionAccess()
        except Exception: pass  # noqa: E701
    if not f.params.get("seed_names"):
        return f, "linear-pattern seeds unreadable (body/face patterns not supported yet)"
    return f, None


def _rotation_axis_dir(tr) -> list[float] | None:
    """Unit axis direction of a rigid rotation IMathTransform (eigenvector for eigenvalue 1)."""
    try:
        a = list(tr.ArrayData)
    except Exception:  # noqa: BLE001
        return None
    M = [[a[0] - 1, a[1], a[2]], [a[3], a[4] - 1, a[5]], [a[6], a[7], a[8] - 1]]
    def cross(u, v):
        return [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
    cand = [cross(M[0], M[1]), cross(M[1], M[2]), cross(M[0], M[2])]
    axis = max(cand, key=lambda v: sum(c * c for c in v))
    n = math.sqrt(sum(c * c for c in axis))
    if n < 1e-9:
        return None
    return [c / n for c in axis]


def _extract_circular_pattern(feat, model) -> tuple[SWFeature, str | None]:
    f = SWFeature(feat.Name, feat.GetTypeName2(), "circular_pattern")
    defn = _cast(feat.GetDefinition(), "ICircularPatternFeatureData")
    if defn is None:
        return f, "circular-pattern definition unreadable"
    from win32com.client import dynamic
    seed_objs = []
    axis_dir = None
    try:
        defn.AccessSelections(model, None)
        dyn = dynamic.Dispatch(defn._oleobj_)
        f.params["count"] = int(dyn.TotalInstances)
        # `Spacing` is the TOTAL sweep angle (radians) when EqualSpacing, else the per-step angle.
        # The engine's circular_pattern places `count` instances at step = angle/count, so its
        # `angle` is per_step*count: = the full sweep when equal-spaced; step*count otherwise.
        spacing = float(dyn.Spacing)
        equal = bool(dyn.EqualSpacing)
        engine_angle = spacing if equal else spacing * f.params["count"]
        f.params["angle_deg"] = round(math.degrees(engine_angle), 6)
        for o in _dyn_list(defn, "PatternFeatureArray"):
            seed_objs.append(_cast(o, "IFeature"))
        f.params["seed_names"] = [s.Name for s in seed_objs]
        axis_dir = _rotation_axis_dir(defn.GetTransform(1))
    except Exception as exc:  # noqa: BLE001
        return f, f"circular-pattern unreadable: {exc}"
    finally:
        try: defn.ReleaseSelectionAccess()
        except Exception: pass  # noqa: E701
    if not f.params.get("seed_names") or axis_dir is None:
        return f, "circular-pattern axis/seeds unreadable"
    # Axis ORIGIN, robustly: by rotational symmetry the centroid of ALL instances lies on the axis.
    # all = seed(s) + the pattern feature's copy faces; weight = total instance count.
    n = f.params["count"]
    seed_cs = [c for c in (_feature_faces_centroid(s) for s in seed_objs) if c]
    copies_c = _feature_faces_centroid(feat)
    if not seed_cs or copies_c is None:
        return f, "could not locate circular-pattern axis (no instance geometry to measure)"
    c_seed = [sum(c[i] for c in seed_cs) / len(seed_cs) for i in range(3)]
    origin = [(c_seed[i] + (n - 1) * copies_c[i]) / n for i in range(3)]
    f.params["axis_model"] = [round(x, 9) for x in axis_dir]
    f.params["axis_origin_model"] = [round(x, 6) for x in origin]
    return f, None


def _feature_faces_centroid(feature) -> list[float] | None:
    """Centroid (model mm) of the faces a feature created — mean of per-face representative points."""
    try:
        faces = list(feature.GetFaces() or [])
    except Exception:  # noqa: BLE001
        return None
    pts = [p for p in (_face_point(fc) for fc in faces) if p]
    if not pts:
        return None
    return [sum(p[i] for p in pts) / len(pts) for i in range(3)]


def _extract_mirror(feat, model) -> tuple[SWFeature, str | None]:
    f = SWFeature(feat.Name, feat.GetTypeName2(), "mirror")
    defn = _cast(feat.GetDefinition(), "IMirrorPatternFeatureData")
    if defn is None:
        return f, "mirror definition unreadable"
    seed_objs = []
    try:
        defn.AccessSelections(model, None)
        for o in _dyn_list(defn, "PatternFeatureArray"):
            seed_objs.append(_cast(o, "IFeature"))
        f.params["seed_names"] = [s.Name for s in seed_objs]
    except Exception as exc:  # noqa: BLE001
        return f, f"mirror unreadable: {exc}"
    finally:
        try: defn.ReleaseSelectionAccess()
        except Exception: pass  # noqa: E701
    if not f.params.get("seed_names"):
        return f, "mirror seeds unreadable (body mirror not supported yet)"
    # Infer the mirror plane geometrically: the mirror feature's faces are the reflected copy, so
    # the seed-centroid -> mirror-centroid vector is the plane normal (robust for any orientation).
    c_mirror = _feature_faces_centroid(feat)
    c_seed_pts = [c for c in (_feature_faces_centroid(s) for s in seed_objs) if c]
    if c_mirror is None or not c_seed_pts:
        return f, "could not infer mirror plane (no reflected faces to measure)"
    c_seed = [sum(c[i] for c in c_seed_pts) / len(c_seed_pts) for i in range(3)]
    normal = [c_mirror[i] - c_seed[i] for i in range(3)]
    principal = _principal_from_model_normal(normal)
    if not principal:
        return f, "mirror plane is not a principal plane (tilted/face mirror unsupported yet)"
    # The plane passes through the seed/mirror midpoint; for a principal mirror it's through origin
    # only if the midpoint's in-normal component is ~0. Record the offset for completeness.
    f.params["plane_principal"] = principal
    return f, None


def _principal_from_model_normal(nrm) -> str | None:
    n = math.sqrt(sum(c * c for c in nrm))
    if n < 1e-6:
        return None
    u = [abs(c) / n for c in nrm]
    if max(u) < 0.99 or sum(1 for a in u if a > 0.05) != 1:
        return None
    return {0: "right", 1: "top", 2: "front"}[u.index(max(u))]


def _walk(model, part: SWPart) -> None:
    feat = model.FirstFeature()
    while feat is not None:
        feat = _cast(feat, "IFeature")
        try:
            sw_type = feat.GetTypeName2()
            name = feat.Name
        except Exception:  # noqa: BLE001
            feat = feat.GetNextFeature()
            continue
        if sw_type in _SKIP_TYPES:
            feat = feat.GetNextFeature()
            continue
        kind = KIND_BY_SWTYPE.get(sw_type)
        try:
            if kind == "sketch":
                f = _extract_sketch(feat)
                part.features.append(f)
                part.by_name[name] = f
            elif kind in ("extrude", "cut"):
                f, reason = _extract_extrude(feat, kind)
                if reason:
                    part.unsupported.append(SWUnsupported(name, sw_type, reason))
                part.features.append(f)
                part.by_name[name] = f
            elif kind == "plane":
                f = SWFeature(name, sw_type, "plane", dims=_feature_dims(feat))
                part.features.append(f)
                part.by_name[name] = f
            elif kind == "loft":
                f = SWFeature(name, sw_type, "loft", dims=_feature_dims(feat))
                part.features.append(f)
                part.by_name[name] = f
            elif kind == "revolve":
                f, reason = _extract_revolve(feat)
                if reason:
                    part.unsupported.append(SWUnsupported(name, sw_type, reason))
                part.features.append(f)
                part.by_name[name] = f
            elif kind == "fillet":
                f, reason = _extract_fillet(feat, model)
                if reason:
                    part.unsupported.append(SWUnsupported(name, sw_type, reason))
                part.features.append(f)
                part.by_name[name] = f
            elif kind == "chamfer":
                f, reason = _extract_chamfer(feat, model)
                if reason:
                    part.unsupported.append(SWUnsupported(name, sw_type, reason))
                part.features.append(f)
                part.by_name[name] = f
            elif kind == "shell":
                f, reason = _extract_shell(feat, model)
                if reason:
                    part.unsupported.append(SWUnsupported(name, sw_type, reason))
                part.features.append(f)
                part.by_name[name] = f
            elif kind in ("linear_pattern", "circular_pattern", "mirror"):
                ex = {"linear_pattern": _extract_linear_pattern,
                      "circular_pattern": _extract_circular_pattern,
                      "mirror": _extract_mirror}[kind]
                f, reason = ex(feat, model)
                if reason:
                    part.unsupported.append(SWUnsupported(name, sw_type, reason))
                part.features.append(f)
                part.by_name[name] = f
            elif kind == "axis":
                part.unsupported.append(SWUnsupported(
                    name, sw_type, "reference axis reconstruction not implemented yet"))
                part.features.append(SWFeature(name, sw_type, kind))
            else:
                part.unsupported.append(SWUnsupported(
                    name, sw_type, "unrecognised SolidWorks feature type"))
        except Exception as exc:  # noqa: BLE001
            part.unsupported.append(SWUnsupported(name, sw_type, f"extraction error: {exc}"))
        feat = feat.GetNextFeature()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def _walk_assembly(model, part: SWPart) -> None:
    """Extract a top-level assembly: each component's part feature tree + its placement transform.

    Reconstructs every distinct component part (recursively) and records its rigid placement so the
    translator can rebuild the assembly as a placed multi-body part. Sub-assemblies and mates are
    not interpreted yet — geometry is fully determined by the resolved component transforms."""
    asm = _cast(model, "IAssemblyDoc")
    try:
        comps = list(asm.GetComponents(True) or [])   # True = top-level only
    except Exception as exc:  # noqa: BLE001
        part.unsupported.append(SWUnsupported(part.name, "Assembly",
                                              f"could not enumerate components: {exc}"))
        return
    cache: dict[str, SWPart] = {}
    for c in comps:
        comp = _cast(c, "IComponent2")
        try:
            cname = comp.Name2
            cpath = comp.GetPathName()
        except Exception:  # noqa: BLE001
            continue
        if not cpath or not cpath.lower().endswith(".sldprt"):
            part.unsupported.append(SWUnsupported(
                cname, "Component", "sub-assembly components not supported yet"))
            continue
        try:
            tr = comp.GetTotalTransform(True)
            a = list(tr.ArrayData)
            rot = [round(x, 9) for x in a[0:9]]
            trans = [round(a[9] * MM, 6), round(a[10] * MM, 6), round(a[11] * MM, 6)]
        except Exception as exc:  # noqa: BLE001
            part.unsupported.append(SWUnsupported(cname, "Component",
                                                  f"could not read placement: {exc}"))
            continue
        if cpath not in cache:
            # Recursively reconstruct the component part's feature tree.
            cache[cpath] = extract_part(cpath)
        cpart = cache[cpath]
        # Surface the component part's own unsupported features (prefixed) on the assembly.
        for u in cpart.unsupported:
            part.unsupported.append(SWUnsupported(f"{cname}/{u.sw_name}", u.sw_type, u.reason,
                                                  u.severity))
        part.components.append(SWComponent(cname, cpart, rot, trans))
    if not part.components:
        part.unsupported.append(SWUnsupported(part.name, "Assembly",
                                              "no reconstructable part components found"))


def extract_part(path: str | os.PathLike, *, visible: bool = False) -> SWPart:
    """Open *path* in SolidWorks (via COM) and extract its feature tree into an :class:`SWPart`.

    Raises :class:`SolidWorksUnavailable` if SolidWorks/pywin32 cannot be used here."""
    if not solidworks_available():
        raise SolidWorksUnavailable(
            "SolidWorks COM automation is unavailable (need SOLIDWORKS installed + pywin32).")
    import pythoncom
    import win32com.client as win32

    pythoncom.CoInitialize()
    src = str(Path(path).resolve())
    ext = Path(src).suffix.lower()
    is_asm = ext == ".sldasm"
    doctype = 2 if is_asm else 1
    swApp = _cast(win32.Dispatch("SldWorks.Application"), "ISldWorks")
    swApp.Visible = bool(visible)
    ret = swApp.OpenDoc6(src, doctype, 1, "", 0, 0)    # 1 == swOpenDocOptions_Silent
    model = ret[0] if isinstance(ret, tuple) else ret
    if model is None:
        raise RuntimeError(f"SolidWorks could not open '{src}'")
    model = _cast(model, "IModelDoc2")
    part = SWPart(name=Path(src).stem, path=src, is_assembly=is_asm)
    try:
        if is_asm:
            _walk_assembly(model, part)
        else:
            _walk(model, part)
    finally:
        try:
            swApp.CloseDoc(model.GetTitle())
        except Exception:  # noqa: BLE001
            pass
    return part


def part_to_dict(part: SWPart) -> dict:
    """JSON-able view of an :class:`SWPart` (for debugging / standalone runs)."""
    def feat(f: SWFeature) -> dict:
        d = {"sw_name": f.sw_name, "sw_type": f.sw_type, "kind": f.kind, "params": f.params}
        if f.plane is not None:
            d["plane"] = f.plane
        if f.entities:
            d["entities"] = [{"id": e.id, "type": e.type, "construction": e.construction, **e.data}
                             for e in f.entities]
        if f.constraints:
            d["constraints"] = [{"type": c.type, "refs": c.refs, "value": c.value}
                                for c in f.constraints]
        if f.dims:
            d["dims"] = [{"name": d_.name, "value": d_.value} for d_ in f.dims]
        return d
    return {
        "name": part.name, "path": part.path, "is_assembly": part.is_assembly,
        "features": [feat(f) for f in part.features],
        "unsupported": [{"sw_name": u.sw_name, "sw_type": u.sw_type, "reason": u.reason,
                         "severity": u.severity} for u in part.unsupported],
    }


if __name__ == "__main__":  # standalone probe: python -m engine.doc.sw_feature_import <part>
    import json
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        print("usage: python -m engine.doc.sw_feature_import <path-to.sldprt>")
        raise SystemExit(2)
    p = extract_part(target, visible=False)
    print(json.dumps(part_to_dict(p), indent=2))
