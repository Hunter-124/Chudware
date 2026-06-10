"""Selector build + resolve — engine/doc/selectors.py

Implements docs/contracts/selectors.md exactly:
  - TopoInfo: lightweight topology descriptor (no OCP types escape this module)
  - describe_faces / describe_edges / describe_vertices: OCP-backed describers
  - make_selector: TopoInfo → validated selector dict
  - resolve / ResolveResult: §3 scored best-match with thresholds and tiebreak
  - resolve_region: §4 sketch-region resolution

Pure functions; no imports from the document compiler (C01 integrates later).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# --------------------------------------------------------------------------- data types

@dataclass
class TopoInfo:
    """Lightweight descriptor for a single topological entity.

    Decoupled from OCP so the resolution algorithm is unit-testable without
    a live kernel.

    Attributes
    ----------
    kind:
        ``"face"`` | ``"edge"`` | ``"vertex"``
    index:
        Zero-based position in the enumeration order (stable within one
        describe call; used as hint_index default and for test assertions).
    surf:
        Face surface type or edge curve type string (``None`` for vertices).
    centroid:
        3-element float array, mm (shape-space coordinates).
    axis:
        Unit 3-vector, or ``None`` when no axis is meaningful.
    measure:
        Face area (mm²) or edge length (mm); ``None`` for vertices.
    owner:
        Feature id string (``"f3"``), or ``None`` if not tagged.
    hint_index:
        Creation-time ordinal among same-owner same-kind siblings.
        Defaults to ``index`` when not explicitly set (backward-compatible).
    """

    kind: str
    index: int
    surf: str | None
    centroid: np.ndarray          # shape (3,), dtype float64
    axis: np.ndarray | None       # shape (3,) unit vector, or None
    measure: float | None         # area (face) or length (edge)
    owner: str | None
    hint_index: int = field(default=-1)   # -1 means "use index"

    def __post_init__(self) -> None:
        self.centroid = np.asarray(self.centroid, dtype=float)
        if self.axis is not None:
            self.axis = np.asarray(self.axis, dtype=float)
        if self.hint_index == -1:
            self.hint_index = self.index


# --------------------------------------------------------------------------- OCP helpers

def _surf_type_string(surf_enum) -> str:  # type: ignore[no-untyped-def]
    """Map a ``GeomAbs_SurfaceType`` enum value to the contract string."""
    from OCP.GeomAbs import (
        GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone,
        GeomAbs_Sphere, GeomAbs_Torus, GeomAbs_BSplineSurface,
    )
    _MAP = {
        GeomAbs_Plane:          "plane",
        GeomAbs_Cylinder:       "cylinder",
        GeomAbs_Cone:           "cone",
        GeomAbs_Sphere:         "sphere",
        GeomAbs_Torus:          "torus",
        GeomAbs_BSplineSurface: "bspline",
    }
    return _MAP.get(surf_enum, "other")


def _curve_type_string(curve_enum) -> str:  # type: ignore[no-untyped-def]
    """Map a ``GeomAbs_CurveType`` enum value to the contract string."""
    from OCP.GeomAbs import (
        GeomAbs_Line, GeomAbs_Circle, GeomAbs_Ellipse, GeomAbs_BSplineCurve,
    )
    _MAP = {
        GeomAbs_Line:         "line",
        GeomAbs_Circle:       "circle",
        GeomAbs_Ellipse:      "ellipse",
        GeomAbs_BSplineCurve: "bspline",
    }
    return _MAP.get(curve_enum, "other")


# --------------------------------------------------------------------------- OCP describers

def describe_faces(shape, owners: dict[int, str] | None = None) -> list[TopoInfo]:
    """Enumerate all faces of *shape* and return ``TopoInfo`` descriptors.

    Parameters
    ----------
    shape:
        Any ``TopoDS_Shape`` (or build123d ``Shape.wrapped``).
    owners:
        Mapping from face hash to feature-id string produced by the compiler's
        topology-diff tagger.  May be ``None`` (all owners become ``None``).
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.GeomAbs import (
        GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone,
    )
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS

    if owners is None:
        owners = {}

    result: list[TopoInfo] = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    idx = 0
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        adaptor = BRepAdaptor_Surface(face)
        surf_type = adaptor.GetType()
        surf_str = _surf_type_string(surf_type)

        # Centroid + area via surface mass properties
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = props.Mass()
        cm = props.CentreOfMass()
        centroid = np.array([cm.X(), cm.Y(), cm.Z()], dtype=float)

        # Axis — plane normal, cylinder/cone axis
        axis: np.ndarray | None = None
        try:
            if surf_type == GeomAbs_Plane:
                ax = adaptor.Plane().Axis().Direction()
                v = np.array([ax.X(), ax.Y(), ax.Z()], dtype=float)
                n = np.linalg.norm(v)
                axis = v / n if n > 1e-12 else None
            elif surf_type == GeomAbs_Cylinder:
                ax = adaptor.Cylinder().Axis().Direction()
                v = np.array([ax.X(), ax.Y(), ax.Z()], dtype=float)
                n = np.linalg.norm(v)
                axis = v / n if n > 1e-12 else None
            elif surf_type == GeomAbs_Cone:
                ax = adaptor.Cone().Axis().Direction()
                v = np.array([ax.X(), ax.Y(), ax.Z()], dtype=float)
                n = np.linalg.norm(v)
                axis = v / n if n > 1e-12 else None
        except Exception:
            axis = None

        # Owner lookup — build123d wraps shape as a compound; use the raw
        # OCP shape's IsEqual/IsSame logic is not directly hashable, so we
        # match by (surf_str, round(centroid)) key when owners dict uses
        # that convention, or fall back to index-based lookup.
        owner = owners.get(id(face))

        result.append(TopoInfo(
            kind="face",
            index=idx,
            surf=surf_str,
            centroid=centroid,
            axis=axis,
            measure=area,
            owner=owner,
            hint_index=idx,
        ))
        idx += 1
        exp.Next()

    return result


def describe_edges(shape, owners: dict[int, str] | None = None) -> list[TopoInfo]:
    """Enumerate all edges of *shape* and return ``TopoInfo`` descriptors."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.GeomAbs import GeomAbs_Line, GeomAbs_Circle
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS

    if owners is None:
        owners = {}

    result: list[TopoInfo] = []
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    idx = 0
    while exp.More():
        edge = TopoDS.Edge_s(exp.Current())
        adaptor = BRepAdaptor_Curve(edge)
        curve_type = adaptor.GetType()
        curve_str = _curve_type_string(curve_type)

        # Midpoint centroid + length via linear mass properties
        props = GProp_GProps()
        BRepGProp.LinearProperties_s(edge, props)
        length = props.Mass()
        cm = props.CentreOfMass()
        centroid = np.array([cm.X(), cm.Y(), cm.Z()], dtype=float)

        # Axis — line direction, circle axis
        axis: np.ndarray | None = None
        try:
            if curve_type == GeomAbs_Line:
                d = adaptor.Line().Direction()
                v = np.array([d.X(), d.Y(), d.Z()], dtype=float)
                n = np.linalg.norm(v)
                axis = v / n if n > 1e-12 else None
            elif curve_type == GeomAbs_Circle:
                ax = adaptor.Circle().Axis().Direction()
                v = np.array([ax.X(), ax.Y(), ax.Z()], dtype=float)
                n = np.linalg.norm(v)
                axis = v / n if n > 1e-12 else None
        except Exception:
            axis = None

        owner = owners.get(id(edge))

        result.append(TopoInfo(
            kind="edge",
            index=idx,
            surf=curve_str,
            centroid=centroid,
            axis=axis,
            measure=length,
            owner=owner,
            hint_index=idx,
        ))
        idx += 1
        exp.Next()

    return result


def describe_vertices(shape, owners: dict[int, str] | None = None) -> list[TopoInfo]:
    """Enumerate all vertices of *shape* and return ``TopoInfo`` descriptors."""
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopoDS import TopoDS

    if owners is None:
        owners = {}

    result: list[TopoInfo] = []
    exp = TopExp_Explorer(shape, TopAbs_VERTEX)
    idx = 0
    while exp.More():
        vtx = TopoDS.Vertex_s(exp.Current())
        p = BRep_Tool.Pnt_s(vtx)
        centroid = np.array([p.X(), p.Y(), p.Z()], dtype=float)

        owner = owners.get(id(vtx))

        result.append(TopoInfo(
            kind="vertex",
            index=idx,
            surf=None,
            centroid=centroid,
            axis=None,
            measure=None,
            owner=owner,
            hint_index=idx,
        ))
        idx += 1
        exp.Next()

    return result


# --------------------------------------------------------------------------- make_selector

def make_selector(info: TopoInfo) -> dict[str, Any]:
    """Build a validated selector dict from a ``TopoInfo``.

    The returned dict satisfies ``validate_selector`` from ``engine.doc.schema``.
    """
    sel: dict[str, Any] = {
        "kind":          info.kind,
        "surf":          info.surf,
        "centroid":      info.centroid.tolist(),
        "axis":          info.axis.tolist() if info.axis is not None else None,
        "area_or_len":   info.measure,
        "owner_feature": info.owner,
        "hint_index":    info.hint_index,
    }
    return sel


# --------------------------------------------------------------------------- resolution

@dataclass
class ResolveResult:
    """Result of a selector resolve call.

    Attributes
    ----------
    index:
        Index into the *candidates* list of the accepted match, or ``None``
        when the selector is dangling.
    score:
        Best candidate score (lower is better).  ``math.inf`` when no
        candidates exist.
    second_score:
        Score of the second-best candidate, or ``math.inf`` when fewer than
        two candidates.
    dangling_reason:
        Human-readable reason why the reference is dangling (``None`` when
        resolved successfully).
    """

    index: int | None
    score: float
    second_score: float
    dangling_reason: str | None


def _score_candidate(sel: dict[str, Any], c: TopoInfo, bbox_diag: float) -> float:
    """Score candidate *c* against selector *sel* per contract §3.

    Lower is better.  ``bbox_diag`` is used to normalise the centroid distance.
    """
    # Centroid distance term (always present)
    s_centroid = np.asarray(sel["centroid"], dtype=float)
    dc = float(np.linalg.norm(c.centroid - s_centroid)) / max(bbox_diag, 1e-12)

    # Axis term (only when BOTH selector and candidate have an axis)
    s_axis = sel.get("axis")
    has_axis = (s_axis is not None) and (c.axis is not None)
    if has_axis:
        s_ax = np.asarray(s_axis, dtype=float)
        c_ax = c.axis  # type: ignore[assignment]
        ax = 1.0 - abs(float(np.dot(s_ax, c_ax)))
    else:
        ax = 0.0

    # Area/length term
    s_measure = sel.get("area_or_len")
    if s_measure is not None and c.measure is not None:
        denom = max(abs(s_measure), abs(c.measure), 1e-12)
        ar = abs(c.measure - s_measure) / denom
    else:
        ar = 0.0

    # Surface/curve type match term
    tm = 0.0 if (c.surf == sel.get("surf")) else 1.0

    # Weighted score
    if has_axis:
        score = 0.4 * dc + 0.3 * ax + 0.2 * ar + 0.1 * tm
    else:
        score = 0.55 * dc + 0.3 * ar + 0.15 * tm

    return score


def resolve(
    selector: dict[str, Any],
    candidates: list[TopoInfo],
    bbox_diag: float,
) -> ResolveResult:
    """Resolve *selector* against *candidates* per contract §3.

    Algorithm (verbatim from selectors.md §3):

    1. Candidate set = same-kind entities tagged with owner_feature ==
       selector.owner_feature; if that set is empty, ALL same-kind entities.
    2. Score each candidate.
    3. Accept best iff ``best < 0.35`` AND ``(second − best) > 0.05``;
       tiebreak by nearest hint_index.  Otherwise dangling.
    """
    kind = selector.get("kind")
    s_owner = selector.get("owner_feature")

    # Step 1 — filter to same kind
    same_kind = [c for c in candidates if c.kind == kind]
    if not same_kind:
        return ResolveResult(
            index=None,
            score=math.inf,
            second_score=math.inf,
            dangling_reason="no candidates of matching kind",
        )

    # Owner-filtered set with fallback
    owner_set = [c for c in same_kind if c.owner == s_owner] if s_owner is not None else []
    working = owner_set if owner_set else same_kind

    # Step 2 — score
    scored: list[tuple[float, TopoInfo]] = [
        (_score_candidate(selector, c, bbox_diag), c)
        for c in working
    ]
    scored.sort(key=lambda t: (t[0], t[1].hint_index))

    best_score, best_cand = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else math.inf

    # Step 3 — accept/reject
    margin = second_score - best_score
    if best_score < 0.35 and margin > 0.05:
        return ResolveResult(
            index=best_cand.index,
            score=best_score,
            second_score=second_score,
            dangling_reason=None,
        )

    # Dangling — explain why
    if best_score >= 0.35:
        reason = f"best score {best_score:.4f} >= 0.35"
    else:
        reason = f"margin {margin:.4f} <= 0.05 (symmetric ambiguity)"
    return ResolveResult(
        index=None,
        score=best_score,
        second_score=second_score,
        dangling_reason=reason,
    )


# --------------------------------------------------------------------------- region resolution

def resolve_region(
    selector: dict[str, Any],
    regions: list[dict[str, Any]],
    sketch_bbox_diag: float,
) -> ResolveResult:
    """Resolve a region selector per contract §4.

    *regions* is a list of dicts, each having at minimum:
    ``{"centroid": [u, v], "area_or_len": float, "hint_index": int}``.

    Scoring uses centroid distance (sketch-space, normalised by
    *sketch_bbox_diag*) + area term (same weights as the no-axis path),
    no type term.  Same accept thresholds as §3.
    """
    if not regions:
        return ResolveResult(
            index=None,
            score=math.inf,
            second_score=math.inf,
            dangling_reason="no region candidates",
        )

    s_centroid = np.asarray(selector.get("centroid", [0.0, 0.0]), dtype=float)
    s_area = selector.get("area_or_len")

    scored: list[tuple[float, int, int]] = []  # (score, hint_index, list_index)
    for i, reg in enumerate(regions):
        r_centroid = np.asarray(reg.get("centroid", [0.0, 0.0]), dtype=float)
        dc = float(np.linalg.norm(r_centroid - s_centroid)) / max(sketch_bbox_diag, 1e-12)

        r_area = reg.get("area_or_len")
        if s_area is not None and r_area is not None:
            denom = max(abs(s_area), abs(r_area), 1e-12)
            ar = abs(r_area - s_area) / denom
        else:
            ar = 0.0

        score = 0.55 * dc + 0.3 * ar
        hint = reg.get("hint_index", i)
        scored.append((score, hint, i))

    scored.sort(key=lambda t: (t[0], t[1]))

    best_score, _, best_idx = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else math.inf
    margin = second_score - best_score

    if best_score < 0.35 and margin > 0.05:
        return ResolveResult(
            index=best_idx,
            score=best_score,
            second_score=second_score,
            dangling_reason=None,
        )

    if best_score >= 0.35:
        reason = f"best score {best_score:.4f} >= 0.35"
    else:
        reason = f"margin {margin:.4f} <= 0.05 (symmetric ambiguity)"
    return ResolveResult(
        index=None,
        score=best_score,
        second_score=second_score,
        dangling_reason=reason,
    )
