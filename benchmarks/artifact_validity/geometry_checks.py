"""Native geometry measurements for artifact checks, built on :meth:`Evidence.rpc`.

Every helper reads the REOPENED deliverable through the grader's in-process engine:

* cylindrical faces come from ``doc_query {of:"face", surf:"cylinder"}`` — each match
  carries ``concavity`` ("bore" for a hole wall, "boss" for an outer wall), ``radius``,
  ``axis``, an on-axis ``centroid`` and the face ``area_or_len``;
* circular edges come from ``doc_query {of:"edge", surf:"circle"}`` — each match carries
  its circle centre (``centroid``) and ``radius``; the two end circles of a bore are what
  prove it opens on both faces (a through hole), independent of the wall area;
* overall extents from ``doc_bbox``, exact volume from ``eval_mass``.

Nothing here infers a hole from volume, and nothing reads tool previews or the agent's
own report. The helpers raise on an engine fault; checks wrap them (see
:func:`benchmarks.artifact_validity.tasks.schema.check`) so a raise becomes ERROR.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:
    from benchmarks.artifact_validity.evidence import Evidence

Vec = tuple[float, float, float]

#: Two cylinder patches belong to one cylinder when they share radius and axis line.
_SAME_RADIUS_MM = 1e-3
_SAME_AXIS_MM = 1e-2
_SAME_AXIS_DEG = 0.5

#: A stable face/edge sid as doc_query / describe_part hand them out (core stable_sid()).
_SID_RE = re.compile(r"[FEV][0-9a-fA-F]{6}")


# --------------------------------------------------------------------------- vectors

def _v(x: Any) -> Vec:
    return (float(x[0]), float(x[1]), float(x[2]))


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _norm(a: Sequence[float]) -> float:
    return math.sqrt(_dot(a, a))


def unit(a: Sequence[float]) -> Vec:
    n = _norm(a)
    if n <= 1e-12:
        raise ValueError(f"zero-length direction {list(a)!r}")
    return (a[0] / n, a[1] / n, a[2] / n)


def canonical_axis(a: Sequence[float]) -> Vec:
    """An axis has no sign: flip it so its largest-magnitude component is positive."""
    u = unit(a)
    big = max(range(3), key=lambda i: abs(u[i]))
    return u if u[big] > 0 else (-u[0], -u[1], -u[2])


def angle_deg(a: Sequence[float], b: Sequence[float]) -> float:
    """Angle between two axis LINES, folded to 0..90 degrees."""
    c = min(1.0, abs(_dot(unit(a), unit(b))))
    return math.degrees(math.acos(c))


def _perp(p: Sequence[float], origin: Sequence[float], axis: Vec) -> float:
    w = _sub(p, origin)
    t = _dot(w, axis)
    return _norm((w[0] - t * axis[0], w[1] - t * axis[1], w[2] - t * axis[2]))


# --------------------------------------------------------------------------- cylinders

@dataclass(frozen=True)
class Cylinder:
    """One cylindrical wall of the reopened part (seam-split patches merged).

    ``axis`` is canonical (largest component positive); ``point`` is a point ON the axis
    (the first patch's ``axis_point`` — the face centroid projected onto the axis; engines
    that predate ``axis_point`` give the face centroid, which is on-axis only for a full
    revolution). ``length_mm`` is wall area / (2*pi*r), i.e. the axial length of a
    full-revolution wall. ``ends`` are the axial coordinates (``dot(centre, axis)``) of the
    circular edges of the same radius that lie on this axis — a through hole has one at
    each face it opens on."""

    kind: str                       # "bore" | "boss"
    diameter: float
    axis: Vec
    point: Vec
    area_mm2: float
    ends: tuple[float, ...]
    sids: tuple[str, ...]
    body: Any = None

    @property
    def radius(self) -> float:
        return self.diameter / 2.0

    @property
    def length_mm(self) -> float:
        return self.area_mm2 / (2.0 * math.pi * self.radius) if self.radius > 0 else 0.0

    @property
    def span(self) -> tuple[float, float] | None:
        return (min(self.ends), max(self.ends)) if self.ends else None

    def center_in_plane(self, plane_axis: Sequence[float]) -> Vec:
        """The point where this cylinder's axis line crosses the plane through the origin
        normal to ``plane_axis`` (callers subtract a reference centre themselves)."""
        n = unit(plane_axis)
        denom = _dot(self.axis, n)
        if abs(denom) < 1e-9:
            raise ValueError("cylinder axis lies in the measurement plane")
        t = -_dot(self.point, n) / denom
        return (self.point[0] + t * self.axis[0], self.point[1] + t * self.axis[1],
                self.point[2] + t * self.axis[2])

    def to_json(self) -> dict:
        return {"kind": self.kind, "diameter_mm": round(self.diameter, 4),
                "axis": [round(c, 5) for c in self.axis],
                "point": [round(c, 4) for c in self.point],
                "length_mm": round(self.length_mm, 4),
                "span": [round(s, 4) for s in self.span] if self.span else None,
                "sids": list(self.sids)}


def _matches(ev: "Evidence", query: dict) -> list[dict]:
    res = ev.rpc("doc_query", {"query": query})
    return [m for m in (res.get("matches") or []) if isinstance(m, dict)]


def cylinders(ev: "Evidence") -> list[Cylinder]:
    """Every cylindrical wall of the deliverable, bores and bosses, seam-split patches merged."""
    faces = [m for m in _matches(ev, {"of": "face", "surf": "cylinder"})
             if m.get("kind") == "face" and m.get("surf") == "cylinder"]
    circles = [m for m in _matches(ev, {"of": "edge", "surf": "circle"})
               if m.get("kind") == "edge" and m.get("surf") == "circle"
               and m.get("radius") is not None]
    groups: list[dict] = []
    for f in faces:
        if f.get("radius") is None or f.get("axis") is None or f.get("centroid") is None:
            raise ValueError(f"cylinder face without radius/axis/centroid: {f!r}")
        kind = "bore" if f.get("concavity") in ("bore", "concave") else "boss"
        r = float(f["radius"])
        axis = canonical_axis(_v(f["axis"]))
        c = _v(f.get("axis_point") or f["centroid"])
        area = float(f.get("area_or_len") or 0.0)
        for g in groups:
            if (g["kind"] == kind and abs(g["r"] - r) < _SAME_RADIUS_MM
                    and angle_deg(g["axis"], axis) < _SAME_AXIS_DEG
                    and _perp(c, g["point"], g["axis"]) < _SAME_AXIS_MM):
                g["area"] += area
                g["sids"].append(str(f.get("sid")))
                break
        else:
            groups.append({"kind": kind, "r": r, "axis": axis, "point": c, "area": area,
                           "sids": [str(f.get("sid"))], "body": f.get("body")})
    out: list[Cylinder] = []
    for g in groups:
        ends = sorted({round(_dot(_v(e["centroid"]), g["axis"]), 6) for e in circles
                       if abs(float(e["radius"]) - g["r"]) < _SAME_RADIUS_MM
                       and _perp(_v(e["centroid"]), g["point"], g["axis"]) < _SAME_AXIS_MM})
        out.append(Cylinder(kind=g["kind"], diameter=2.0 * g["r"], axis=g["axis"],
                            point=g["point"], area_mm2=g["area"], ends=tuple(ends),
                            sids=tuple(g["sids"]), body=g["body"]))
    return out


def bores(ev: "Evidence") -> list[Cylinder]:
    return [c for c in cylinders(ev) if c.kind == "bore"]


def spans(cyl: Cylinder, lo: float, hi: float, tol: float) -> bool:
    """True when the wall runs the whole axial interval [lo, hi]: an end circle at (or
    beyond) each limit AND a full-revolution wall at least that long (a slot or a partial
    wall has the circles' radius but not the area)."""
    s = cyl.span
    if s is None:
        return False
    return s[0] <= lo + tol and s[1] >= hi - tol and cyl.length_mm >= (hi - lo) - tol


def spans_exactly(cyl: Cylinder, lo: float, hi: float, tol: float) -> bool:
    """True when the wall runs EXACTLY [lo, hi]: end circles at both limits (none beyond)
    and a full-revolution wall of that length — a step, counterbore or through-leg bore."""
    s = cyl.span
    if s is None:
        return False
    return (abs(s[0] - lo) <= tol and abs(s[1] - hi) <= tol
            and abs(cyl.length_mm - (hi - lo)) <= tol)


def circle_edges(ev: "Evidence") -> list[dict]:
    """Every circular edge: {radius, centroid, axis (canonical)}. ``centroid`` is the circle
    centre only for a FULL circle (a partial arc's centroid lies off its centre)."""
    out = []
    for e in _matches(ev, {"of": "edge", "surf": "circle"}):
        if e.get("kind") != "edge" or e.get("radius") is None or e.get("centroid") is None:
            continue
        out.append({"radius": float(e["radius"]), "centroid": _v(e["centroid"]),
                    "axis": canonical_axis(_v(e["axis"])) if e.get("axis") else None})
    return out


def arc_axis_point(face: dict, length: float, near: Sequence[float]) -> tuple[Vec, float]:
    """Axis point of a PARTIAL cylindrical face (a fillet) of axial ``length``: the
    doc_query centroid of an arc of angle t lies r*sin(t/2)/(t/2) off the axis along the
    radial ``normal``. The normal's sign is not trusted, so of the two candidates the one
    nearest ``near`` is returned, with the swept angle t in degrees."""
    r = float(face["radius"])
    c = _v(face["centroid"])
    ax = unit(_v(face["axis"]))
    n = _v(face["normal"])
    t_n = _dot(n, ax)
    n = unit((n[0] - t_n * ax[0], n[1] - t_n * ax[1], n[2] - t_n * ax[2]))
    sweep = float(face.get("area_or_len") or 0.0) / (r * length)
    d = r * math.sin(sweep / 2.0) / (sweep / 2.0) if sweep > 0 else r
    cands = [(c[0] - s * d * n[0], c[1] - s * d * n[1], c[2] - s * d * n[2])
             for s in (1.0, -1.0)]
    return min(cands, key=lambda p: math.dist(p, near)), math.degrees(sweep)


def along(cyls: Iterable[Cylinder], direction: Sequence[float],
          max_deg: float = _SAME_AXIS_DEG) -> list[Cylinder]:
    return [c for c in cyls if angle_deg(c.axis, direction) <= max_deg]


# --------------------------------------------------------------------------- points

def match_points(found: Sequence[Sequence[float]], expected: Sequence[Sequence[float]],
                 tol: float) -> dict:
    """One-to-one nearest matching of 2-D points within ``tol`` (greedy by distance).

    Returns {matched, missing (expected without a partner), extra (found without one),
    max_err (worst matched distance)}."""
    pairs = sorted(((math.dist(f, e), i, j) for i, f in enumerate(found)
                    for j, e in enumerate(expected)), key=lambda t: t[0])
    used_f: set[int] = set()
    used_e: set[int] = set()
    worst = 0.0
    for d, i, j in pairs:
        if d > tol:
            break
        if i in used_f or j in used_e:
            continue
        used_f.add(i)
        used_e.add(j)
        worst = max(worst, d)
    return {"matched": len(used_e),
            "missing": [list(expected[j]) for j in range(len(expected)) if j not in used_e],
            "extra": [[round(c, 4) for c in found[i]] for i in range(len(found))
                      if i not in used_f],
            "max_err": round(worst, 5)}


# --------------------------------------------------------------------------- whole part

def part_bbox(ev: "Evidence") -> dict:
    """Exact model bbox {min,max,size} from ``doc_bbox`` (world bodies carry a tessellation
    margin; the B-rep box does not)."""
    bb = ev.rpc("doc_bbox")
    if not bb.get("size") or not bb.get("min") or not bb.get("max"):
        raise ValueError(f"doc_bbox returned no box: {bb!r}")
    return {"min": _v(bb["min"]), "max": _v(bb["max"]), "size": _v(bb["size"])}


def volume_mm3(ev: "Evidence") -> float:
    """Exact B-rep volume of the whole part (``eval_mass``)."""
    m = ev.rpc("eval_mass")
    v = m.get("volume_mm3")
    if v is None:
        raise ValueError(f"eval_mass returned no volume: {m!r}")
    return float(v)


def solid_bodies(ev: "Evidence") -> dict:
    """Body inventory verdict: {count, sound (all valid AND closed), unknown (flags absent)}."""
    rows = list(ev.bodies)
    unknown = [b.get("id") for b in rows if b.get("valid") is None or b.get("closed") is None]
    sound = bool(rows) and not unknown and all(b.get("valid") and b.get("closed") for b in rows)
    return {"count": len(rows), "sound": sound, "unknown": unknown,
            "bodies": [{k: b.get(k) for k in ("id", "name", "valid", "closed", "volume_mm3")}
                       for b in rows]}


def within(measured: Sequence[float], expected: Sequence[float], tol: float) -> bool:
    return len(measured) == len(expected) and all(
        abs(m - e) <= tol for m, e in zip(measured, expected))


# --------------------------------------------------------------------------- faces

def resolve_faces(ev: "Evidence", query: Any) -> list[dict]:
    """Faces of the deliverable a stored boundary-condition query names.

    Accepts what a persisted study records: a structured query dict, a plain side word /
    token string, or a stable face sid. Returns the doc_query face matches (centroid,
    normal, surf, area_or_len, sid)."""
    if isinstance(query, str) and _SID_RE.fullmatch(query.strip()):
        sid = query.strip().lower()
        return [m for m in _matches(ev, {"of": "face"})
                if str(m.get("sid") or "").lower() == sid]
    if isinstance(query, str):
        from services.mcp.cad_common import _as_face_query  # noqa: PLC0415

        query = _as_face_query(query)
    if not isinstance(query, dict):
        raise ValueError(f"unrecognised face query {query!r}")
    q = dict(query)
    q.setdefault("of", "face")
    return [m for m in _matches(ev, q) if m.get("kind") == "face"]


def planar_end_face(face: dict, normal: Sequence[float], coord_axis: int, coord: float,
                    tol: float, normal_deg: float = 1.0) -> bool:
    """A flat face whose outward normal is ``normal`` and whose centroid sits at
    ``coord`` along world axis ``coord_axis`` (0=X, 1=Y, 2=Z)."""
    if face.get("surf") not in ("plane", "flat") or face.get("normal") is None:
        return False
    n = unit(_v(face["normal"]))
    if math.degrees(math.acos(max(-1.0, min(1.0, _dot(n, unit(normal)))))) > normal_deg:
        return False
    return abs(_v(face["centroid"])[coord_axis] - coord) <= tol


# --------------------------------------------------------------------------- history

def param_value(feat: dict | None, key: str) -> Any:
    """A feature parameter's plain value: ``{"value": v, "expr": ...}`` -> v."""
    if not feat:
        return None
    v = (feat.get("params") or {}).get(key)
    return v.get("value") if isinstance(v, dict) and "value" in v else v


def history_faults(ev: "Evidence") -> list[str]:
    """Why the deliverable is NOT an editable parametric history ([] when it is)."""
    feats = ev.features
    faults: list[str] = []
    if not feats:
        faults.append("no feature history (doc_tree has no features)")
    for f in feats:
        tag = f"{f.get('id')} {f.get('type')} {f.get('name')!r}"
        if f.get("failed"):
            faults.append(f"{tag} failed to rebuild: {f.get('error')}")
        if f.get("rolled_back"):
            faults.append(f"{tag} rolled back on rebuild")
        if f.get("type") == "import":
            faults.append(f"{tag} is an imported dumb solid, not parametric history")
    if feats and all(f.get("suppressed") for f in feats):
        faults.append("every feature is suppressed")
    return faults
