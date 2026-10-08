"""Piecewise-linear civil terrain geometry, in SI metres.

No proprietary mesher is embedded. SciPy/Qhull supplies the initial Delaunay
triangulation (BSD); constraints are recovered by diagonal swaps following
Sloan (1993), Computers & Structures 47(3), 441–450. Polygon intersection uses
Sutherland–Hodgman (1974), CACM 17(1), 32–42. Volumes integrate affine height
exactly on the common subdivision, including separate positive/negative parts.
"""
from __future__ import annotations

from collections import defaultdict, deque
import math
from typing import Callable

import numpy as np
from scipy.spatial import Delaunay, QhullError, cKDTree

from .errors import CivilError

_SWAP_REF = "Sloan (1993), A fast algorithm for generating constrained Delaunay triangulations, Computers & Structures 47(3), 441–450"
_VOLUME_REF = "Sutherland–Hodgman (1974), CACM 17(1), 32–42; affine triangle integral A*(h1+h2+h3)/3"


def _result(method_name, reference, **fields):
    return {"ok": True, **fields, "warnings": [], "assumptions": [],
            "method": {"name": method_name, "reference": reference}}


def _number(value, reason="invalid_number"):
    if isinstance(value, bool):
        raise CivilError(reason, "A finite real number is required.")
    try:
        out = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CivilError(reason, "A finite real number is required.") from exc
    if not math.isfinite(out):
        raise CivilError(reason, "NaN and infinity are not terrain coordinates.")
    return out


def _points(value, dimension, reason="invalid_points"):
    try:
        rows = list(value)
    except TypeError as exc:
        raise CivilError(reason) from exc
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple, np.ndarray)) or len(row) != dimension:
            raise CivilError(reason, f"Coordinates require exactly {dimension} components.")
        out.append([_number(v, reason) for v in row])
    return out


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _area(poly):
    if len(poly) < 3:
        return 0.0
    # Translate before summing, important for projected coordinates of ~1e6 m.
    return sum(_orient(poly[0], poly[i], poly[i + 1])
               for i in range(1, len(poly) - 1)) / 2.0


def _scale(poly):
    return max(1.0, max(p[0] for p in poly) - min(p[0] for p in poly),
               max(p[1] for p in poly) - min(p[1] for p in poly))


def _clean(poly):
    if not poly:
        return []
    eps = 1e-11 * _scale(poly)
    out = []
    for p in poly:
        if not out or math.dist(p[:2], out[-1][:2]) > eps:
            out.append(list(p))
    if len(out) > 1 and math.dist(out[0][:2], out[-1][:2]) <= eps:
        out.pop()
    return out


def _segment_parameters(a, b, c, d):
    r = [b[0] - a[0], b[1] - a[1]]
    s = [d[0] - c[0], d[1] - c[1]]
    det = _cross(r, s)
    if abs(det) <= 1e-13 * max(1.0, math.hypot(*r) * math.hypot(*s)):
        return None
    q = [c[0] - a[0], c[1] - a[1]]
    return _cross(q, s) / det, _cross(q, r) / det


def _proper_cross(a, b, c, d):
    params = _segment_parameters(a, b, c, d)
    return params is not None and all(1e-11 < t < 1 - 1e-11 for t in params)


def _on_segment(p, a, b, tol=1e-9):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.dist(p[:2], a[:2]) <= tol
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2
    return (-tol / math.sqrt(length2) <= t <= 1 + tol / math.sqrt(length2)
            and abs(_orient(a, b, p)) <= tol * math.sqrt(length2))


def _polygon(value, reason="invalid_polygon"):
    poly = _clean(_points(value, 2, reason))
    if len(poly) < 3 or abs(_area(poly)) <= 1e-13 * _scale(poly) ** 2:
        raise CivilError(reason, "A simple polygon with positive plan area is required.")
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        for j in range(i + 1, len(poly)):
            if j == i + 1 or (i == 0 and j == len(poly) - 1):
                continue
            c, d = poly[j], poly[(j + 1) % len(poly)]
            if (_proper_cross(a, b, c, d) or _on_segment(a, c, d)
                    or _on_segment(b, c, d) or _on_segment(c, a, b)
                    or _on_segment(d, a, b)):
                raise CivilError(reason, "Polygon self-intersection or nonadjacent touching edges.")
    if _area(poly) < 0:
        poly.reverse()
    return poly


def _inside(p, poly):
    inside = False
    for a, b in zip(poly, poly[1:] + poly[:1]):
        if _on_segment(p, a, b):
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x > p[0]:
                inside = not inside
    return inside


def _ear_triangles(poly):
    """Triangulate a simple CCW polygon without filling its concavities."""
    ring = list(range(len(poly)))
    out = []
    eps = 1e-13 * _scale(poly) ** 2
    while len(ring) > 3:
        found = False
        for k, b in enumerate(ring):
            a, c = ring[k - 1], ring[(k + 1) % len(ring)]
            turn = _orient(poly[a], poly[b], poly[c])
            if abs(turn) <= eps:
                ring.pop(k)
                found = True
                break
            if turn < 0:
                continue
            if any(min(_orient(poly[a], poly[b], poly[j]),
                       _orient(poly[b], poly[c], poly[j]),
                       _orient(poly[c], poly[a], poly[j])) >= -eps
                   for j in ring if j not in (a, b, c)):
                continue
            out.append([poly[a], poly[b], poly[c]])
            ring.pop(k)
            found = True
            break
        if not found:
            raise CivilError("polygon_triangulation_failed")
    if len(ring) == 3 and _orient(*(poly[j] for j in ring)) > eps:
        out.append([poly[j] for j in ring])
    return out


def _clip_halfplane(poly, signed, positive=True):
    if not poly:
        return []
    out = []
    a, va = poly[-1], signed(poly[-1])
    if not positive:
        va = -va
    for b in poly:
        vb = signed(b)
        if not positive:
            vb = -vb
        if (va >= 0) != (vb >= 0):
            t = va / (va - vb)
            out.append([a[j] + t * (b[j] - a[j]) for j in range(len(a))])
        if vb >= 0:
            out.append(list(b))
        a, va = b, vb
    return _clean(out)


def _clip(poly, convex):
    out = poly
    for a, b in zip(convex, convex[1:] + convex[:1]):
        out = _clip_halfplane(out, lambda p, a=a, b=b: _orient(a, b, p))
        if len(out) < 3:
            return []
    return out


def _subtract_convex(poly, convex):
    """Disjoint convex pieces of poly minus convex; boundary area is zero."""
    inside = poly
    outside = []
    for a, b in zip(convex, convex[1:] + convex[:1]):
        fn = lambda p, a=a, b=b: _orient(a, b, p)
        piece = _clip_halfplane(inside, fn, False)
        if len(piece) >= 3 and _area(piece) > 1e-14:
            outside.append(piece)
        inside = _clip_halfplane(inside, fn)
        if len(inside) < 3:
            break
    return outside


class _Dedupe:
    def __init__(self, tol):
        self.tol = tol
        self.points = []
        self.buckets = defaultdict(list)

    def add(self, point):
        x, y, z = point
        key = (math.floor(x / self.tol), math.floor(y / self.tol))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.buckets.get((key[0] + dx, key[1] + dy), ()):
                    old = self.points[i]
                    if math.dist(old[:2], point[:2]) <= self.tol:
                        if abs(old[2] - z) > self.tol:
                            raise CivilError("conflicting_duplicate_points",
                                             "Coincident XY coordinates have different elevations.",
                                             first=old, second=point, tolerance_m=self.tol)
                        return i
        i = len(self.points)
        self.points.append(list(point))
        self.buckets[key].append(i)
        return i


def _ccw(tri, points):
    a, b, c = tri
    return [a, b, c] if _orient(points[a], points[b], points[c]) > 0 else [a, c, b]


def _edge_map(triangles):
    edges = defaultdict(list)
    for i, tri in enumerate(triangles):
        for a, b in zip(tri, tri[1:] + tri[:1]):
            edges[tuple(sorted((a, b)))].append(i)
    return edges


def _recover(a, b, triangles, points, locked):
    target = tuple(sorted((a, b)))
    edges = _edge_map(triangles)
    if target in edges:
        locked.add(target)
        return
    crossing = [edge for edge in edges
                if _proper_cross(points[a], points[b], points[edge[0]], points[edge[1]])]
    crossing.sort(key=lambda e: _segment_parameters(points[a], points[b], points[e[0]], points[e[1]])[0])
    queue = deque(crossing)
    blocked = 0
    swaps = 0
    maximum = max(100, 20 * len(triangles) ** 2)
    while queue:
        edge = queue.popleft()
        if edge not in edges:
            continue
        if edge in locked:
            raise CivilError("crossing_constraints", "Intersecting breaklines must share a vertex.")
        adjacent = edges[edge]
        if len(adjacent) != 2:
            raise CivilError("constraint_recovery_failed", "Constraint intersects a surface boundary.")
        u, v = edge
        t1, t2 = adjacent
        c = next(j for j in triangles[t1] if j not in edge)
        d = next(j for j in triangles[t2] if j not in edge)
        if not _proper_cross(points[u], points[v], points[c], points[d]):
            queue.append(edge)
            blocked += 1
            if blocked > len(queue) + 1:
                raise CivilError("constraint_recovery_failed", "No convex quadrilateral available for an edge swap.")
            continue
        triangles[t1] = _ccw([c, d, u], points)
        triangles[t2] = _ccw([d, c, v], points)
        edges = _edge_map(triangles)
        replacement = tuple(sorted((c, d)))
        if _proper_cross(points[a], points[b], points[c], points[d]):
            queue.append(replacement)
        blocked = 0
        swaps += 1
        if swaps > maximum:
            raise CivilError("constraint_recovery_failed", "Edge-swap iteration bound reached.")
    if target not in edges:
        raise CivilError("constraint_recovery_failed", "Requested edge was not recovered.")
    locked.add(target)


def _diagnostics(points, triangles):
    edges = _edge_map(triangles)
    lengths = {edge: math.dist(points[edge[0]][:2], points[edge[1]][:2]) for edge in edges}
    median = float(np.median(list(lengths.values()))) if lengths else 0.0
    threshold = 3.0 * median
    long_edges = [{"vertices": list(e), "length_m": length}
                  for e, length in sorted(lengths.items()) if length > threshold]
    slivers = []
    for i, tri in enumerate(triangles):
        p = [points[j] for j in tri]
        angle = min(math.atan2(abs(_orient(p[k], p[(k + 1) % 3], p[(k + 2) % 3])),
                               ((p[(k + 1) % 3][0] - p[k][0]) * (p[(k + 2) % 3][0] - p[k][0])
                                + (p[(k + 1) % 3][1] - p[k][1]) * (p[(k + 2) % 3][1] - p[k][1])))
                    for k in range(3))
        if angle < math.pi / 180:
            slivers.append({"triangle": i, "min_angle_rad": angle})
    return {"long_edges": long_edges, "slivers": slivers,
            "long_edge_threshold_m": threshold, "sliver_angle_threshold_rad": math.pi / 180,
            "basis": "Mesh diagnostics only: >3 times median edge length; minimum angle <1 degree, not design-code limits."}


def build_tin(points, breaklines=None, boundary=None, *, id, name, dedupe_tol=1e-6):
    """Build CCW TIN with actual constrained edges (Sloan 1993 diagonal swaps).

    XY duplicates within ``dedupe_tol`` metres merge only if Z also agrees within
    that tolerance. Crossing breaklines acquire their exact shared intersection,
    and refuse inconsistent elevations. Boundary removal uses triangle centroids,
    as specified by the civil terrain API (not exact polygon-edge clipping).
    """
    tol = _number(dedupe_tol, "invalid_dedupe_tolerance")
    if tol <= 0:
        raise CivilError("invalid_dedupe_tolerance")
    if not isinstance(id, str) or not id or not isinstance(name, str):
        raise CivilError("invalid_surface_identity")
    dedupe = _Dedupe(tol)
    original = _points(points, 3)
    for p in original:
        dedupe.add(p)
    lines = []
    try:
        input_lines = iter(() if breaklines is None else breaklines)
    except TypeError as exc:
        raise CivilError("invalid_breakline") from exc
    for line in input_lines:
        row = _points(line, 3, "invalid_breakline")
        if len(row) < 2:
            raise CivilError("invalid_breakline", "A breakline needs at least two vertices.")
        indices = [dedupe.add(p) for p in row]
        indices = [j for k, j in enumerate(indices) if k == 0 or j != indices[k - 1]]
        if len(indices) < 2:
            raise CivilError("invalid_breakline", "A breakline needs two distinct XY vertices.")
        lines.append(indices)
    segments = [(a, b) for line in lines for a, b in zip(line, line[1:]) if a != b]
    for i, (a, b) in enumerate(segments):
        p, q = dedupe.points[a], dedupe.points[b]
        for c, d in segments[i + 1:]:
            r, s = dedupe.points[c], dedupe.points[d]
            params = _segment_parameters(p, q, r, s)
            if params is None or not all(-1e-11 <= t <= 1 + 1e-11 for t in params):
                continue
            t, u = params
            z1, z2 = p[2] + t * (q[2] - p[2]), r[2] + u * (s[2] - r[2])
            if abs(z1 - z2) > tol:
                raise CivilError("conflicting_breakline_elevations", "Crossing breaklines disagree in Z.")
            dedupe.add([p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]), z1])
    pts = dedupe.points
    if len(pts) < 3:
        raise CivilError("degenerate_point_set", "At least three non-collinear XY points are required.")
    xy = np.asarray(pts, dtype=float)[:, :2]
    local = xy - xy[0]
    singular = np.linalg.svd(local, compute_uv=False)
    if singular[-1] <= 1e-13 * max(1.0, singular[0]):
        raise CivilError("degenerate_point_set", "Survey points are collinear in XY.")
    try:
        base = Delaunay(local)
    except QhullError as exc:
        raise CivilError("degenerate_point_set", "Qhull could not form a nondegenerate planar triangulation.") from exc
    triangles = [_ccw(list(map(int, t)), pts) for t in base.simplices]
    used = {j for tri in triangles for j in tri}
    if len(used) != len(pts):
        raise CivilError("triangulation_omitted_points", "Qhull omitted survey vertices; adjust the survey tolerance.")
    constraints = []
    rebuilt_lines = []
    for line in lines:
        rebuilt = []
        for a, b in zip(line, line[1:]):
            if a == b:
                continue
            p, q = pts[a], pts[b]
            length2 = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
            along = []
            for j, r in enumerate(pts):
                # Use floating-point collinearity, not XY dedupe tolerance: a
                # merely nearby survey vertex must not kink a hard breakline.
                if _on_segment(r, p, q, 1e-11 * max(1.0, math.sqrt(length2))):
                    t = ((r[0] - p[0]) * (q[0] - p[0]) + (r[1] - p[1]) * (q[1] - p[1])) / length2
                    if abs(r[2] - (p[2] + t * (q[2] - p[2]))) > tol:
                        raise CivilError("conflicting_breakline_elevations", "A vertex on a breakline disagrees in Z.")
                    along.append((t, j))
            chain = [j for _, j in sorted(along)]
            constraints.extend(zip(chain, chain[1:]))
            rebuilt.extend(chain if not rebuilt else chain[1:])
        rebuilt_lines.append([pts[j] for j in rebuilt])
    locked = set()
    for a, b in constraints:
        _recover(a, b, triangles, pts, locked)
    poly = None if boundary is None else _polygon(boundary, "invalid_boundary")
    clipped_constraints = False
    if poly is not None:
        triangles = [t for t in triangles
                     if _inside([sum(pts[j][k] for j in t) / 3 for k in (0, 1)], poly)]
        if not triangles:
            raise CivilError("empty_surface", "Boundary removes every triangle.")
        # Only persist breakline portions still represented by triangle edges.
        # Retaining discarded constraints would violate contract §3.1.
        remaining_edges = _edge_map(triangles)
        lookup = {tuple(p): i for i, p in enumerate(pts)}
        retained_lines = []
        for line in rebuilt_lines:
            run = []
            for p, q in zip(line, line[1:]):
                if tuple(sorted((lookup[tuple(p)], lookup[tuple(q)]))) in remaining_edges:
                    if not run:
                        run.append(p)
                    run.append(q)
                else:
                    clipped_constraints = True
                    if len(run) >= 2:
                        retained_lines.append(run)
                    run = []
            if len(run) >= 2:
                retained_lines.append(run)
        rebuilt_lines = retained_lines
    triangles.sort(key=lambda t: tuple(sorted(t)))
    diagnostics = _diagnostics(pts, triangles)
    out = _result("constrained TIN / centroid boundary clipping", _SWAP_REF,
                  id=id, name=name, kind="tin", points=pts, triangles=triangles,
                  breaklines=rebuilt_lines, boundary=poly, source={"kind": "points", "ref": ""},
                  diagnostics=diagnostics)
    if diagnostics["long_edges"]:
        out["warnings"].append("long_edges")
    if diagnostics["slivers"]:
        out["warnings"].append("slivers")
    if clipped_constraints:
        out["warnings"].append("breaklines_clipped_by_boundary")
    if poly is not None:
        out["assumptions"].append("Boundary clipping removes triangles by centroid; boundary-straddling triangles are not subdivided.")
    return out


class _Grid:
    """Uniform plan buckets for triangle point/box queries."""
    def __init__(self, triangles):
        self.boxes = [(min(p[0] for p in t), min(p[1] for p in t),
                       max(p[0] for p in t), max(p[1] for p in t)) for t in triangles]
        self.x0 = min(b[0] for b in self.boxes)
        self.y0 = min(b[1] for b in self.boxes)
        self.x1 = max(b[2] for b in self.boxes)
        self.y1 = max(b[3] for b in self.boxes)
        n = max(1, math.ceil(math.sqrt(len(triangles))))
        self.dx = max((self.x1 - self.x0) / n, 1e-12)
        self.dy = max((self.y1 - self.y0) / n, 1e-12)
        self.n = n
        self.buckets = defaultdict(list)
        for i, box in enumerate(self.boxes):
            for key in self.keys(box):
                self.buckets[key].append(i)

    def keys(self, box):
        lo_x = max(0, min(self.n - 1, math.floor((box[0] - self.x0) / self.dx)))
        lo_y = max(0, min(self.n - 1, math.floor((box[1] - self.y0) / self.dy)))
        hi_x = max(0, min(self.n - 1, math.floor((box[2] - self.x0) / self.dx)))
        hi_y = max(0, min(self.n - 1, math.floor((box[3] - self.y0) / self.dy)))
        return ((i, j) for i in range(lo_x, hi_x + 1) for j in range(lo_y, hi_y + 1))

    def query(self, box):
        if box[2] < self.x0 or box[3] < self.y0 or box[0] > self.x1 or box[1] > self.y1:
            return []
        candidates = set()
        for key in self.keys(box):
            candidates.update(self.buckets.get(key, ()))
        return [i for i in sorted(candidates)
                if self.boxes[i][0] <= box[2] and self.boxes[i][2] >= box[0]
                and self.boxes[i][1] <= box[3] and self.boxes[i][3] >= box[1]]


def _surface(surface):
    if not isinstance(surface, dict) or surface.get("kind") != "tin":
        raise CivilError("invalid_surface", "A civil TIN surface is required.")
    pts = _points(surface.get("points", []), 3, "invalid_surface")
    triangles = []
    try:
        input_triangles = iter(surface.get("triangles", []))
    except TypeError as exc:
        raise CivilError("invalid_triangle") from exc
    for tri in input_triangles:
        if (not isinstance(tri, (list, tuple)) or len(tri) != 3
                or any(isinstance(j, bool) or not isinstance(j, (int, np.integer))
                       or j < 0 or j >= len(pts) for j in tri)):
            raise CivilError("invalid_triangle")
        p = [pts[j] for j in tri]
        if _orient(*p) <= 0:
            raise CivilError("invalid_triangle", "Triangles must be nondegenerate and CCW in plan.")
        triangles.append(p)
    if not triangles:
        raise CivilError("empty_surface")
    return pts, triangles


def _facet(tri):
    a, b, c = tri
    dx1, dy1, dz1 = (b[k] - a[k] for k in range(3))
    dx2, dy2, dz2 = (c[k] - a[k] for k in range(3))
    det = dx1 * dy2 - dy1 * dx2
    gx = (dz1 * dy2 - dy1 * dz2) / det
    gy = (dx1 * dz2 - dz1 * dx2) / det
    return gx, gy, lambda p: a[2] + gx * (p[0] - a[0]) + gy * (p[1] - a[1])


def surface_sampler(surface) -> Callable[[float, float], float | None]:
    """Barycentric (affine-facet) elevation with a uniform spatial bucket index.

    Returns None outside actual triangle coverage, including holes; never
    extrapolates the convex hull. The callable snapshots the supplied geometry.
    Reference: barycentric linear finite-element triangle interpolation.
    """
    _, triangles = _surface(surface)
    grid = _Grid(triangles)
    facets = [_facet(t)[2] for t in triangles]

    def z_at(x, y):
        p = [_number(x, "invalid_sample_coordinate"), _number(y, "invalid_sample_coordinate")]
        eps = 1e-10
        for i in grid.query((p[0] - eps, p[1] - eps, p[0] + eps, p[1] + eps)):
            tri = triangles[i]
            area2 = _orient(*tri)
            if min(_orient(tri[0], tri[1], p), _orient(tri[1], tri[2], p),
                   _orient(tri[2], tri[0], p)) >= -1e-11 * area2:
                return float(facets[i](p))
        return None

    return z_at


def _join_segments(segments):
    """Join using exact mesh-topological endpoint keys, not rounded XY."""
    coords = {}
    edges = set()
    for (ka, pa), (kb, pb) in segments:
        if ka == kb:
            continue
        coords[ka], coords[kb] = pa, pb
        edges.add(tuple(sorted((ka, kb))))
    adjacency = defaultdict(set)
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)
    remaining = set(edges)
    out = []
    starts = sorted(k for k in adjacency if len(adjacency[k]) != 2)
    starts.extend(k for k in sorted(adjacency) if k not in starts)
    for start in starts:
        for nxt in sorted(adjacency[start]):
            edge = tuple(sorted((start, nxt)))
            if edge not in remaining:
                continue
            remaining.remove(edge)
            chain = [start, nxt]
            prev, current = start, nxt
            while len(adjacency[current]) == 2:
                options = [j for j in sorted(adjacency[current])
                           if tuple(sorted((current, j))) in remaining]
                if not options:
                    break
                following = options[0]
                remaining.remove(tuple(sorted((current, following))))
                chain.append(following)
                prev, current = current, following
                if current == start:
                    break
            out.append([coords[k] for k in chain])
    return out


def contours(surface, interval, base=0.0):
    """Intersect facets with level planes and join by vertex/edge identity.

    Exact-level vertices use a deterministic upper-side convention; exact-level
    edges are emitted once. Flat facets have no unique isoline, so only the
    boundary of a coplanar plateau is emitted, never its internal diagonals.
    Reference: piecewise-linear TIN elevation-plane intersection.
    """
    pts, triangles = _surface(surface)
    interval = _number(interval, "invalid_contour_interval")
    base = _number(base, "invalid_contour_base")
    if interval <= 0:
        raise CivilError("invalid_contour_interval")
    lo, hi = min(p[2] for p in pts), max(p[2] for p in pts)
    first, last = math.ceil((lo - base) / interval), math.floor((hi - base) / interval)
    if last - first > 100000:
        raise CivilError("too_many_contours", "Increase the contour interval.")
    rows = []
    source_tris = surface["triangles"]
    for n in range(first, last + 1):
        level = base + n * interval
        segments = []
        plateau_edges = defaultdict(int)
        for indices, tri in zip(source_tris, triangles):
            dz = [p[2] - level for p in tri]
            if all(z == 0 for z in dz):
                for a, b in zip(indices, indices[1:] + indices[:1]):
                    plateau_edges[tuple(sorted((a, b)))] += 1
                continue
            exact = [k for k, z in enumerate(dz) if z == 0]
            if len(exact) == 2:
                a, b = (indices[k] for k in exact)
                segments.append(((('v', a), pts[a]), (('v', b), pts[b])))
                continue
            hits = {}
            for k in range(3):
                j = (k + 1) % 3
                # A vertex belongs to the upper side. Isolated tangencies
                # produce one hit and hence no spurious zero-length segment.
                if (dz[k] >= 0) == (dz[j] >= 0):
                    continue
                if dz[k] == 0:
                    key, p = ('v', indices[k]), pts[indices[k]]
                elif dz[j] == 0:
                    key, p = ('v', indices[j]), pts[indices[j]]
                else:
                    a, b = sorted((indices[k], indices[j]))
                    key = ('e', a, b)
                    t = (level - pts[a][2]) / (pts[b][2] - pts[a][2])
                    p = [pts[a][v] + t * (pts[b][v] - pts[a][v]) for v in range(3)]
                    p[2] = level
                hits[key] = p
            if len(hits) == 2:
                segments.append(tuple(hits.items()))
        for (a, b), count in plateau_edges.items():
            if count == 1:
                segments.append(((('v', a), pts[a]), (('v', b), pts[b])))
        polylines = _join_segments(segments)
        if polylines:
            rows.append({"level_m": level, "polylines": polylines})
    out = _result("TIN level-plane intersections", "Affine triangle interpolation; topological segment joining", contours=rows)
    out["assumptions"].append("Plateau contours describe their perimeter; exact-level edges occur once; saddle vertices terminate branched polylines deterministically.")
    return out


def slope_aspect(surface):
    """Facet gradient; aspect is downhill math angle CCW from +easting.

    Aspect is None on a horizontal facet. Summary mean is plan-area weighted.
    Reference: z=ax+by+c, slope=100*sqrt(a²+b²), aspect=atan2(-b,-a).
    """
    _, triangles = _surface(surface)
    rows = []
    for i, tri in enumerate(triangles):
        gx, gy, _ = _facet(tri)
        slope = 100 * math.hypot(gx, gy)
        rows.append({"triangle": i, "area_m2": _area(tri), "slope_pct": slope,
                     "aspect_rad": None if slope == 0 else math.atan2(-gy, -gx) % (2 * math.pi)})
    area = sum(row["area_m2"] for row in rows)
    return _result("TIN facet slope and downhill aspect", "Gradient of z=ax+by+c", triangles=rows,
                   summary={"area_m2": area, "min_slope_pct": min(r["slope_pct"] for r in rows),
                            "max_slope_pct": max(r["slope_pct"] for r in rows),
                            "mean_slope_pct": sum(r["slope_pct"] * r["area_m2"] for r in rows) / area})


def _integrate(poly, height):
    if len(poly) < 3:
        return 0.0
    return sum(_orient(poly[0], poly[i], poly[i + 1]) / 6
               * (height(poly[0]) + height(poly[i]) + height(poly[i + 1]))
               for i in range(1, len(poly) - 1))


def _parts_in_boundary(poly, boundary_tris):
    if boundary_tris is None:
        return [poly]
    return [part for tri in boundary_tris if len(part := _clip(poly, tri)) >= 3 and _area(part) > 0]


def _coverage_area(triangles, boundary_tris):
    return sum(_area(part) for tri in triangles for part in _parts_in_boundary(tri, boundary_tris))


def volume_between(top, bottom, boundary=None):
    """Exact signed earthwork on the common XY TIN subdivision.

    Positive (top-bottom) is fill, negative is cut; net=fill-cut. Without a
    boundary, excluded area is the union of the two coverages minus their
    intersection. With a boundary, it is requested polygon area minus common
    coverage. No shrink/swell factors. Triangles must form nonoverlapping TINs.
    """
    _, top_tris = _surface(top)
    _, bottom_tris = _surface(bottom)
    polygon = None if boundary is None else _polygon(boundary, "invalid_boundary")
    boundary_tris = None if polygon is None else _ear_triangles(polygon)
    top_facets = [_facet(t)[2] for t in top_tris]
    bottom_facets = [_facet(t)[2] for t in bottom_tris]
    grid = _Grid(bottom_tris)
    fill = cut = area = 0.0
    for i, tri in enumerate(top_tris):
        box = (min(p[0] for p in tri), min(p[1] for p in tri),
               max(p[0] for p in tri), max(p[1] for p in tri))
        for j in grid.query(box):
            overlay = _clip(tri, bottom_tris[j])
            if len(overlay) < 3 or _area(overlay) <= 0:
                continue
            diff = lambda p, i=i, j=j: top_facets[i](p) - bottom_facets[j](p)
            for part in _parts_in_boundary(overlay, boundary_tris):
                area += _area(part)
                fill += _integrate(_clip_halfplane(part, diff), diff)
                cut -= _integrate(_clip_halfplane(part, diff, False), diff)
    top_area = _coverage_area(top_tris, boundary_tris)
    bottom_area = _coverage_area(bottom_tris, boundary_tris)
    requested = _area(polygon) if polygon is not None else top_area + bottom_area - area
    excluded = requested - area
    if abs(excluded) <= 1e-10 * max(1, requested):
        excluded = 0.0  # Roundoff only, not a missing-data clamp.
    if excluded < 0:
        raise CivilError("overlapping_surface_triangles", "Common coverage exceeds requested area.")
    out = _result("exact TIN overlay / signed affine integration", _VOLUME_REF,
                  cut_m3=cut, fill_m3=fill, net_m3=fill - cut, area_m2=area,
                  excluded_area_m2=excluded,
                  surface_ids=list(dict.fromkeys(s["id"] for s in (top, bottom)
                                                 if isinstance(s.get("id"), str) and s["id"])))
    out["assumptions"].append("Fill means top above bottom; cut means top below bottom. Volumes are geometric in-situ volumes, without shrink/swell.")
    if excluded > 0:
        out["warnings"].append("excluded_area")
    return out


def volume_to_elevation(surface, z, boundary=None):
    """Earthwork surface relative to a datum: above datum=fill, below=cut.

    Reference: affine triangle integral after zero-height clipping. Use
    ``stage_storage`` for water volume (which is the cut part in this convention).
    """
    z = _number(z, "invalid_elevation")
    _surface(surface)
    plane = {"kind": "tin", "points": [[p[0], p[1], z] for p in surface["points"]],
             "triangles": surface["triangles"]}
    return volume_between(surface, plane, boundary)


def stage_storage(surface, polygon, stages):
    """Water volume below each increasing stage inside the pond footprint.

    Exact clipped-facet integral V=integral(max(stage-z_ground,0)) dA.
    Reference: prismoidal formula for a piecewise-linear basin. The table is in
    ``stage_storage`` inside the standard civil analysis envelope, not a bare list.
    """
    poly = _polygon(polygon)
    try:
        levels = [_number(s, "invalid_stages") for s in stages]
    except TypeError as exc:
        raise CivilError("invalid_stages") from exc
    if not levels or any(b <= a for a, b in zip(levels, levels[1:])):
        raise CivilError("invalid_stages", "Stages must be nonempty and strictly increasing.")
    rows = []
    for stage in levels:
        result = volume_to_elevation(surface, stage, poly)
        if result["excluded_area_m2"] > 1e-9 * _area(poly):
            raise CivilError("incomplete_surface_coverage", "Pond footprint extends into unsurveyed terrain.",
                             excluded_area_m2=result["excluded_area_m2"])
        rows.append([stage, result["cut_m3"]])
    return _result("exact pond stage-storage", "V=integral(max(stage-z_ground,0)) dA; affine facet/prismoidal integration",
                   stage_storage=rows, area_m2=_area(poly))


def profile_along(surface, polyline, step):
    """Plan-station elevation profile; preserve None values at coverage gaps.

    Stations include uniform step locations, every polyline corner, and the end.
    Reference: cumulative horizontal segment length + affine TIN interpolation.
    """
    line = _points(polyline, 2, "invalid_polyline")
    step = _number(step, "invalid_profile_step")
    if len(line) < 2 or step <= 0:
        raise CivilError("invalid_profile_step" if step <= 0 else "invalid_polyline")
    lengths = [math.dist(a, b) for a, b in zip(line, line[1:])]
    if any(length == 0 for length in lengths):
        raise CivilError("invalid_polyline", "Consecutive profile vertices must be distinct.")
    stations = [0.0]
    for length in lengths:
        stations.append(stations[-1] + length)
    total = stations[-1]
    if total / step > 1000000:
        raise CivilError("too_many_profile_samples")
    sample_stations = sorted(set(stations + [k * step for k in range(math.floor(total / step) + 1)]))
    z_at = surface_sampler(surface)
    samples = []
    segment = 0
    for s in sample_stations:
        while segment < len(lengths) - 1 and s > stations[segment + 1]:
            segment += 1
        t = (s - stations[segment]) / lengths[segment]
        x, y = [line[segment][k] + t * (line[segment + 1][k] - line[segment][k]) for k in (0, 1)]
        samples.append({"station_m": s, "x_m": x, "y_m": y, "z_m": z_at(x, y)})
    out = _result("TIN plan-station profile", "Cumulative horizontal length; affine TIN interpolation", samples=samples, length_m=total)
    if any(p["z_m"] is None for p in samples):
        out["warnings"].append("surface_coverage_gaps")
    return out


def _ray_daylight(p, direction, pad_z, slope, sign, triangles):
    """First root along a ray, exactly within each affine ground facet."""
    intervals = []
    for tri in triangles:
        lo, hi = 0.0, math.inf
        for a, b in zip(tri, tri[1:] + tri[:1]):
            initial = _orient(a, b, p)
            change = _cross([b[0] - a[0], b[1] - a[1]], direction)
            if abs(change) <= 1e-13:
                if initial < -1e-10:
                    hi = -1.0
                    break
            elif change > 0:
                lo = max(lo, -initial / change)
            else:
                hi = min(hi, -initial / change)
        if hi >= lo and hi >= 0:
            intervals.append((max(0.0, lo), hi, _facet(tri)[2]))
    intervals.sort(key=lambda item: (item[0], item[1]))
    covered_to = 0.0
    for lo, hi, ground in intervals:
        if lo > covered_to + 1e-8:
            break
        covered_to = max(covered_to, hi)
        q0 = [p[0] + lo * direction[0], p[1] + lo * direction[1]]
        q1 = [p[0] + hi * direction[0], p[1] + hi * direction[1]]
        f0 = pad_z + sign * lo / slope - ground(q0)
        f1 = pad_z + sign * hi / slope - ground(q1)
        if abs(f0) <= 1e-9:
            return [q0[0], q0[1], ground(q0)]
        if f0 * f1 <= 0 and f0 != f1:
            t = lo + (hi - lo) * f0 / (f0 - f1)
            q = [p[0] + t * direction[0], p[1] + t * direction[1]]
            return [q[0], q[1], ground(q)]
    raise CivilError("daylight_not_found", "A grading ray leaves surveyed terrain before meeting ground.", origin_m=p, direction=direction)


def _mesh_from_facets(facets, id, name, boundary, source):
    """Merge facets and split T-junctions to preserve conforming edge topology."""
    dedupe = _Dedupe(1e-8)
    triangles = []
    seen = set()
    for tri in facets:
        if _orient(*tri) <= 1e-14:
            continue
        indices = [dedupe.add(p) for p in tri]
        key = tuple(sorted(indices))
        if len(set(indices)) == 3 and key not in seen:
            triangles.append(indices)
            seen.add(key)
    points = dedupe.points
    if not triangles:
        return {"id": id, "name": name, "kind": "tin", "points": points,
                "triangles": [], "breaklines": [], "boundary": boundary, "source": source}
    tree = cKDTree(np.asarray(points)[:, :2])
    refined = []
    for indices in triangles:
        ring = []
        for a, b in zip(indices, indices[1:] + indices[:1]):
            p, q = points[a], points[b]
            midpoint = [(p[k] + q[k]) / 2 for k in (0, 1)]
            length = math.dist(p[:2], q[:2])
            candidates = tree.query_ball_point(midpoint, length / 2 + 1e-8)
            along = []
            for j in candidates:
                if _on_segment(points[j], p, q, 1e-8):
                    t = ((points[j][0] - p[0]) * (q[0] - p[0])
                         + (points[j][1] - p[1]) * (q[1] - p[1])) / length ** 2
                    if -1e-10 <= t < 1 - 1e-10:
                        along.append((t, j))
            ring.extend(j for _, j in sorted(along))
        if len(ring) == 3:
            refined.append(indices)
        else:
            center = dedupe.add([sum(points[j][k] for j in indices) / 3 for k in range(3)])
            for a, b in zip(ring, ring[1:] + ring[:1]):
                if _orient(points[center], points[a], points[b]) > 1e-14:
                    refined.append([center, a, b])
    return {"id": id, "name": name, "kind": "tin", "points": points,
            "triangles": refined, "breaklines": [], "boundary": boundary, "source": source}


def _connected_facets(facets, a, b):
    """Discard disconnected ground intersections beyond the daylight region."""
    if not facets:
        return []
    mesh = _mesh_from_facets(facets, "strip", "strip", None, {"kind": "grading", "ref": ""})
    points, triangles = mesh["points"], mesh["triangles"]
    vertex_tris = defaultdict(list)
    for i, tri in enumerate(triangles):
        for j in tri:
            vertex_tris[j].append(i)
    selected = {i for i, tri in enumerate(triangles)
                if any(_on_segment(points[j], a, b, 1e-8) for j in tri)}
    queue = deque(selected)
    while queue:
        i = queue.popleft()
        for j in triangles[i]:
            for neighbor in vertex_tris[j]:
                if neighbor not in selected:
                    selected.add(neighbor)
                    queue.append(neighbor)
    return [[points[j] for j in triangles[i]] for i in sorted(selected)]


def grade_pad(existing, pad_polygon, pad_elevation, cut_slope, fill_slope, *, step=1.0):
    """Grade a pad, daylight its H:V slopes, and merge with unchanged ground.

    Corner rays use mitered offset directions, edge rays outward normals. This
    reproduces a straight-sided frustum on flat ground. Side planes are overlaid
    with ground facets and split at their exact intersection: daylight follows
    terrain facet changes rather than guessed chords. Disconnected intersections
    beyond the region attached to the pad are discarded; crossed slopes refuse.
    Reference: slope-to-surface grading; V=int(height) dA, prismoidal frusta.
    """
    _, ground_triangles = _surface(existing)
    poly = _polygon(pad_polygon)
    z = _number(pad_elevation, "invalid_pad_elevation")
    cut = _number(cut_slope, "invalid_grading_slope")
    fill = _number(fill_slope, "invalid_grading_slope")
    step = _number(step, "invalid_grading_step")
    if cut <= 0 or fill <= 0:
        raise CivilError("invalid_grading_slope", "Slopes are positive horizontal:vertical ratios.")
    if step <= 0:
        raise CivilError("invalid_grading_step")
    z_at = surface_sampler(existing)
    coverage = volume_to_elevation(existing, z, poly)
    if coverage["excluded_area_m2"] > 1e-9 * _area(poly):
        raise CivilError("incomplete_surface_coverage", "Pad footprint is not fully surveyed.")
    normals = []
    for a, b in zip(poly, poly[1:] + poly[:1]):
        length = math.dist(a, b)
        normals.append([(b[1] - a[1]) / length, -(b[0] - a[0]) / length])
    origins = []
    directions = []
    for i, p in enumerate(poly):
        before, after = normals[i - 1], normals[i]
        denom = 1 + before[0] * after[0] + before[1] * after[1]
        if denom <= 1e-12:
            raise CivilError("invalid_pad_polygon", "A pad corner reverses direction.")
        direction = [(before[k] + after[k]) / denom for k in (0, 1)]
        origins.append(p)
        directions.append(direction)
        q = poly[(i + 1) % len(poly)]
        count = math.ceil(math.dist(p, q) / step)
        if count > 100000:
            raise CivilError("too_many_grading_samples")
        # Include exact intersections of the pad edge with the ground triangles:
        # they locate cut/fill transitions without guessing across a facet break.
        fractions = {k / count for k in range(1, count)}
        for tri in ground_triangles:
            for a, b in zip(tri, tri[1:] + tri[:1]):
                params = _segment_parameters(p, q, a, b)
                if params is not None:
                    t, u = params
                    if 1e-10 < t < 1 - 1e-10 and -1e-10 <= u <= 1 + 1e-10:
                        fractions.add(t)
        fractions = sorted({0.0, 1.0, *fractions})
        for ta, tb in zip(fractions, fractions[1:]):
            a = [p[k] + ta * (q[k] - p[k]) for k in (0, 1)]
            b = [p[k] + tb * (q[k] - p[k]) for k in (0, 1)]
            za, zb = z_at(*a), z_at(*b)
            if za is None or zb is None:
                raise CivilError("incomplete_surface_coverage")
            if (za - z) * (zb - z) < 0:
                fractions.append(ta + (tb - ta) * (z - za) / (zb - za))
        for t in sorted(set(fractions)):
            if 1e-10 < t < 1 - 1e-10:
                origins.append([p[k] + t * (q[k] - p[k]) for k in (0, 1)])
                directions.append(after)
    for p, direction in zip(origins, directions):
        ground_z = z_at(*p)
        if ground_z is None:
            raise CivilError("incomplete_surface_coverage")
        sign = 1 if ground_z > z else -1
        _ray_daylight(p, direction, z, cut if sign > 0 else fill, sign, ground_triangles)
    # Build side-plane sectors beyond the survey extent, then clip against the
    # ground and the zero-difference contour. The extent is purely geometric:
    # no sampling or capped search substitutes for finding real daylight.
    extent = 4 * _scale([p for tri in ground_triangles for p in tri])
    design_facets = [[[p[0], p[1], z] for p in t] for t in _ear_triangles(poly)]
    for i, a in enumerate(origins):
        j = (i + 1) % len(origins)
        b = origins[j]
        midpoint = [(a[k] + b[k]) / 2 for k in (0, 1)]
        ground_z = z_at(*midpoint)
        if ground_z is None:
            raise CivilError("incomplete_surface_coverage")
        sign = 1 if ground_z > z else -1
        slope = cut if sign > 0 else fill
        d = [a[k] + extent * directions[i][k] for k in (0, 1)]
        c = [b[k] + extent * directions[j][k] for k in (0, 1)]
        quad = _polygon([a, d, c, b], "grading_overlap")
        if not _inside(midpoint, quad):
            raise CivilError("grading_overlap", "Outward rays invert a grading sector.")
        # Both rays have unit projection onto this edge's outward normal;
        # therefore the side-plane gradient is exactly the specified H:V.
        edge = [b[k] - a[k] for k in (0, 1)]
        length = math.hypot(*edge)
        normal = [edge[1] / length, -edge[0] / length]
        height = lambda p, a=a, normal=normal, sign=sign, slope=slope: (
            z + sign * ((p[0] - a[0]) * normal[0] + (p[1] - a[1]) * normal[1]) / slope)
        cells = []
        for sector in _ear_triangles(quad):
            for ground_tri in ground_triangles:
                part = _clip(sector, ground_tri)
                if len(part) < 3 or _area(part) <= 1e-14:
                    continue
                ground_height = _facet(ground_tri)[2]
                diff = lambda p, ground_height=ground_height: height(p) - ground_height(p)
                part = _clip_halfplane(part, diff, positive=sign < 0)
                for k in range(1, len(part) - 1):
                    cells.append([[p[0], p[1], height(p)]
                                  for p in (part[0], part[k], part[k + 1])])
        design_facets.extend(_connected_facets(cells, a, b))
    design_mesh = _mesh_from_facets(design_facets, "pad", "Pad slopes", None,
                                   {"kind": "grading", "ref": str(existing.get("id", ""))})
    _, design_tris = _surface(design_mesh)
    # Confirm the true intersection boundary, including facet-change vertices,
    # and refuse overlaps/gaps rather than manufacture a successful grading.
    edges = _edge_map(design_mesh["triangles"])
    boundary_segments = []
    for (a, b), adjacent in edges.items():
        if len(adjacent) > 2:
            raise CivilError("grading_overlap", "More than two facets meet a grading edge.")
        if len(adjacent) != 1:
            continue
        p, q = design_mesh["points"][a], design_mesh["points"][b]
        for t in (0.0, 0.5, 1.0):
            xy = [p[k] + t * (q[k] - p[k]) for k in (0, 1)]
            ground_z = z_at(*xy)
            if ground_z is None or abs(p[2] + t * (q[2] - p[2]) - ground_z) > 1e-7:
                raise CivilError("daylight_not_found", "A slope boundary is not a ground intersection.")
        boundary_segments.append(((('v', a), p), (('v', b), q)))
    daylight_lines = _join_segments(boundary_segments)
    if not daylight_lines or any(line[0] != line[-1] for line in daylight_lines):
        raise CivilError("grading_overlap", "Daylight does not form closed boundaries.")
    daylight_lines.sort(key=lambda line: -abs(_area(line)))
    for i, tri in enumerate(design_tris):
        for other in design_tris[i + 1:]:
            part = _clip(tri, other)
            if len(part) >= 3 and _area(part) > 1e-8:
                raise CivilError("grading_overlap", "Cut/fill slope sectors overlap.")
    # Replace precisely the designed footprint, retaining all unchanged ground
    # outside it. Convex subtraction preserves non-convex boundaries and holes.
    merged_facets = list(design_tris)
    design_grid = _Grid(design_tris)
    for tri in ground_triangles:
        remaining = [tri]
        box = (min(p[0] for p in tri), min(p[1] for p in tri),
               max(p[0] for p in tri), max(p[1] for p in tri))
        for index in design_grid.query(box):
            remaining = [piece for part in remaining
                         for piece in _subtract_convex(part, design_tris[index])]
        height = _facet(tri)[2]
        for part in remaining:
            for k in range(1, len(part) - 1):
                merged_facets.append([[p[0], p[1], height(p)]
                                      for p in (part[0], part[k], part[k + 1])])
    graded = _mesh_from_facets(merged_facets, str(existing.get("id", "ground")) + "_graded",
                               str(existing.get("name", "Ground")) + " graded pad",
                               existing.get("boundary"), {"kind": "grading", "ref": str(existing.get("id", ""))})
    graded["breaklines"] = [[[p[0], p[1], z] for p in origins] + [[origins[0][0], origins[0][1], z]],
                            *daylight_lines]
    out = _result("pad slope-to-ground grading", "Mitered H:V grading sectors; exact affine-ground intersection; prismoidal earthwork",
                  surface=graded, daylight_line=daylight_lines[0], daylight_lines=daylight_lines,
                  volumes=volume_between(graded, existing), step_m=step)
    out["assumptions"].extend(["Cut/fill slopes are positive H:V ratios; corners are mitered rather than rounded.",
                              "Side planes intersect ground facets exactly. Disconnected regions beyond daylight are discarded; overlapping slope sectors are refused."])
    return out
