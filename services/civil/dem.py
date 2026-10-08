"""North-up, metre-based DEM processing, without raster-library dependencies.

Rows run north to south; origin_x/origin_y are the lower-left *cell corner*.
Elevation samples are at cell centres. Public results are JSON-serializable.
D8 uses ESRI codes, 0 for a terminal cell and -1 for nodata. Flat routing is
breadth-first towards lower edges, not Garbrecht–Martz's dual gradient.
References: ESRI ASCII raster format; O'Callaghan & Mark (1984), Computer
Vision, Graphics, and Image Processing 28:323–344; Barnes, Lehman & Mulla
(2014), Computers & Geosciences 62:117–127; Horn (1981), Proceedings IEEE
69:14–47. Algorithms here are original implementations of published methods.
"""
from __future__ import annotations

from collections import deque
import heapq
import math
from pathlib import Path

import numpy as np

from .errors import CivilError

# Clockwise from east, with the north-up row convention.
_D8 = ((0, 1, 1), (1, 1, 2), (1, 0, 4), (1, -1, 8),
       (0, -1, 16), (-1, -1, 32), (-1, 0, 64), (-1, 1, 128))
_CODES = {code: (dr, dc) for dr, dc, code in _D8}
_GEOMETRY = ("origin_x", "origin_y", "cell_size", "nrows", "ncols")


def _result(name, reference, **values):
    units = {key: "m" for key in ("origin_x", "origin_y", "cell_size", "z", "points")
             if key in values}
    if "boundary" in values:
        units.update(boundary="m", boundary_rings="m")
    return {"ok": True, "warnings": [], "assumptions": [], "units": units,
            "method": {"name": name, "reference": reference}, **values}


def _number(value, reason):
    if isinstance(value, (bool, np.bool_)):
        raise CivilError(reason)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CivilError(reason) from exc
    if not math.isfinite(number):
        raise CivilError(reason)
    return number


def _integer(value, reason):
    number = _number(value, reason)
    if number != int(number) or number < 1:
        raise CivilError(reason)
    return int(number)


def _geometry(grid):
    if not isinstance(grid, dict) or any(k not in grid for k in _GEOMETRY):
        raise CivilError("invalid_grid", "Grid geometry fields are required.")
    meta = {k: _number(grid[k], "invalid_grid_geometry")
            for k in ("origin_x", "origin_y", "cell_size")}
    if meta["cell_size"] <= 0:
        raise CivilError("invalid_cell_size")
    meta.update({k: _integer(grid[k], "invalid_grid_dimensions")
                 for k in ("nrows", "ncols")})
    return meta


def _dem(dem):
    meta = _geometry(dem)
    if "z" not in dem or "nodata" not in dem:
        raise CivilError("invalid_dem", "z and an explicit nodata value are required.")
    try:
        z = np.asarray(dem["z"], dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CivilError("invalid_elevations") from exc
    if z.shape != (meta["nrows"], meta["ncols"]):
        raise CivilError("grid_shape_mismatch")
    nodata = dem["nodata"]
    if nodata is not None:
        try:
            nodata = float(nodata)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CivilError("invalid_nodata") from exc
        if math.isinf(nodata):
            raise CivilError("invalid_nodata")
        if math.isnan(nodata):
            nodata = None
    valid = ~np.isnan(z) if nodata is None else z != nodata
    if np.any(~np.isfinite(z[valid])):
        raise CivilError("invalid_elevations", "Nonfinite cells must match declared nodata.")
    if not np.any(valid):
        raise CivilError("empty_dem")
    meta["nodata"] = nodata
    return meta, z, valid


def _array_json(values, valid, missing=None):
    return [[float(values[r, c]) if valid[r, c] else missing
             for c in range(values.shape[1])] for r in range(values.shape[0])]


def _grid_result(meta, z, valid, name, reference, **values):
    return _result(name, reference, **meta,
                   z=_array_json(z, valid, meta["nodata"]), **values)


def _neighbors(r, c, nr, nc):
    for dr, dc, code in _D8:
        rr, cc = r + dr, c + dc
        if 0 <= rr < nr and 0 <= cc < nc:
            yield rr, cc, code


def _edge(r, c, valid):
    nr, nc = valid.shape
    return (r == 0 or c == 0 or r == nr - 1 or c == nc - 1
            or any(not valid[rr, cc] for rr, cc, _ in _neighbors(r, c, nr, nc)))


def _center(meta, r, c):
    h = meta["cell_size"]
    return [meta["origin_x"] + (c + 0.5) * h,
            meta["origin_y"] + (meta["nrows"] - r - 0.5) * h]


def read_ascii_grid(path):
    """Read ESRI ASCII raster, including xllcenter/yllcenter origins (ESRI spec)."""
    try:
        with Path(path).open(encoding="utf-8-sig") as handle:
            header, body = {}, []
            keys = {"ncols", "nrows", "xllcorner", "yllcorner", "xllcenter",
                    "yllcenter", "cellsize", "nodata_value"}
            in_body = False
            for line in handle:
                tokens = line.split()
                if not tokens:
                    continue
                key = tokens[0].lower()
                if not in_body and key in keys:
                    if len(tokens) != 2 or key in header:
                        raise CivilError("invalid_ascii_header")
                    header[key] = _number(tokens[1], "invalid_ascii_header")
                else:
                    in_body = True
                    body.extend(tokens)
    except (OSError, TypeError, UnicodeError) as exc:
        raise CivilError("grid_read_failed", str(exc)) from exc
    if any(k not in header for k in ("ncols", "nrows", "cellsize")):
        raise CivilError("invalid_ascii_header")
    if (sum(k in header for k in ("xllcorner", "xllcenter")) != 1
            or sum(k in header for k in ("yllcorner", "yllcenter")) != 1):
        raise CivilError("invalid_ascii_origin")
    h = header["cellsize"]
    origin_x = header.get("xllcorner", header.get("xllcenter", 0) - h / 2)
    origin_y = header.get("yllcorner", header.get("yllcenter", 0) - h / 2)
    nr = _integer(header["nrows"], "invalid_grid_dimensions")
    nc = _integer(header["ncols"], "invalid_grid_dimensions")
    if len(body) != nr * nc:
        raise CivilError("ascii_cell_count_mismatch")
    try:
        z = np.asarray(body, dtype=float).reshape(nr, nc)
    except ValueError as exc:
        raise CivilError("invalid_elevations") from exc
    grid = dict(origin_x=origin_x, origin_y=origin_y, cell_size=h,
                nrows=nr, ncols=nc, z=z, nodata=header.get("nodata_value", -9999.0))
    meta, z, valid = _dem(grid)
    out = _grid_result(meta, z, valid, "ESRI ASCII raster import", "ESRI ASCII raster format")
    if "nodata_value" not in header:
        out["assumptions"].append("Omitted NODATA_value uses ESRI's -9999 default.")
    out["assumptions"].append("Input horizontal and vertical coordinates are metres in the project CRS.")
    return out


def write_ascii_grid(dem, path):
    """Write north-up ESRI ASCII raster with full double precision (ESRI spec)."""
    meta, z, valid = _dem(dem)
    sentinel = meta["nodata"]
    if sentinel is None:
        sentinel = -9999.0
        while np.any(z[valid] == sentinel):
            sentinel -= 1.0
    lines = [f"ncols {meta['ncols']}", f"nrows {meta['nrows']}",
             f"xllcorner {meta['origin_x']:.17g}", f"yllcorner {meta['origin_y']:.17g}",
             f"cellsize {meta['cell_size']:.17g}", f"NODATA_value {sentinel:.17g}"]
    for r in range(meta["nrows"]):
        lines.append(" ".join(f"{z[r, c] if valid[r, c] else sentinel:.17g}"
                              for c in range(meta["ncols"])))
    try:
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except (OSError, TypeError) as exc:
        raise CivilError("grid_write_failed", str(exc)) from exc
    out = _result("ESRI ASCII raster export", "ESRI ASCII raster format",
                  path=str(path), nodata=sentinel)
    if meta["nodata"] is None:
        out["assumptions"].append(f"Null nodata encoded with finite sentinel {sentinel:g}.")
    return out


def read_xyz(path, cell_size):
    """Read complete regular XYZ centre samples; arbitrary record order is accepted."""
    h = _number(cell_size, "invalid_cell_size")
    if h <= 0:
        raise CivilError("invalid_cell_size")
    try:
        points = np.loadtxt(path, dtype=float, ndmin=2)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        raise CivilError("xyz_read_failed", str(exc)) from exc
    if points.shape[1] != 3 or not len(points) or not np.all(np.isfinite(points)):
        raise CivilError("invalid_xyz")
    xs, ys = np.unique(points[:, 0]), np.unique(points[:, 1])
    tolerance = 1e-8 * h
    if ((len(xs) > 1 and np.any(np.abs(np.diff(xs) - h) > tolerance))
            or (len(ys) > 1 and np.any(np.abs(np.diff(ys) - h) > tolerance))):
        raise CivilError("irregular_xyz_grid")
    z = np.full((len(ys), len(xs)), np.nan)
    for x, y, elevation in points:
        c, r = int(np.searchsorted(xs, x)), len(ys) - 1 - int(np.searchsorted(ys, y))
        if not np.isnan(z[r, c]):
            raise CivilError("duplicate_xyz_cell")
        z[r, c] = elevation
    if np.any(np.isnan(z)):
        raise CivilError("incomplete_xyz_grid")
    meta = dict(origin_x=float(xs[0] - h / 2), origin_y=float(ys[0] - h / 2),
                cell_size=h, nrows=len(ys), ncols=len(xs), nodata=None)
    out = _grid_result(meta, z, np.ones(z.shape, dtype=bool), "Regular XYZ import",
                       "Regular cell-centre sampling")
    out["assumptions"].append("XYZ coordinates are cell centres, in metres in the project CRS.")
    return out


def sample(dem, x, y):
    """Bilinear centre-sample interpolation, without extrapolation or nodata blending."""
    meta, z, valid = _dem(dem)
    x, y = _number(x, "invalid_coordinate"), _number(y, "invalid_coordinate")
    u = (x - meta["origin_x"]) / meta["cell_size"] - 0.5
    v = meta["nrows"] - 0.5 - (y - meta["origin_y"]) / meta["cell_size"]
    out = _result("Bilinear interpolation", "Tensor product linear interpolation", elevation_m=None)
    out["assumptions"].append("Interpolation domain is the rectangle of cell centres; no edge extrapolation.")
    if not (0 <= u <= meta["ncols"] - 1 and 0 <= v <= meta["nrows"] - 1):
        out["warnings"].append("outside_interpolation_domain")
        return out
    c0, r0 = int(math.floor(u)), int(math.floor(v))
    c1, r1 = min(c0 + 1, meta["ncols"] - 1), min(r0 + 1, meta["nrows"] - 1)
    tx, ty = u - c0, v - r0
    terms = ((r0, c0, (1 - tx) * (1 - ty)), (r0, c1, tx * (1 - ty)),
             (r1, c0, (1 - tx) * ty), (r1, c1, tx * ty))
    if any(w > 0 and not valid[r, c] for r, c, w in terms):
        out["warnings"].append("nodata_interpolation_stencil")
        return out
    out["elevation_m"] = float(sum(z[r, c] * w for r, c, w in terms if w > 0))
    return out


def to_tin_points(dem, stride=1):
    """Return valid cell-centre XYZ samples for terrain.build_tin(points)."""
    meta, z, valid = _dem(dem)
    stride = _integer(stride, "invalid_stride")
    points = [(*_center(meta, r, c), float(z[r, c]))
              for r in range(0, meta["nrows"], stride)
              for c in range(0, meta["ncols"], stride) if valid[r, c]]
    return _result("DEM cell-centre sampling", "Regular grid subsampling",
                   points=[list(p) for p in points], stride=stride)


def fill_depressions(dem, *, epsilon=0):
    """Priority-flood to boundary spill elevations (Barnes et al., 2014).

    Nodata is an open drainage boundary. epsilon is a requested minimum upward
    elevation increment along the flood tree, in metres, not a numerical fudge.
    epsilon=0 preserves exact spill elevations and leaves flats for D8 routing.
    """
    meta, z, valid = _dem(dem)
    epsilon = _number(epsilon, "invalid_epsilon")
    if epsilon < 0:
        raise CivilError("invalid_epsilon")
    filled, visited, heap = z.copy(), ~valid.copy(), []
    nr, nc = valid.shape
    for r in range(nr):
        for c in range(nc):
            if valid[r, c] and _edge(r, c, valid):
                visited[r, c] = True
                heapq.heappush(heap, (float(z[r, c]), r, c))
    while heap:
        elevation, r, c = heapq.heappop(heap)
        for rr, cc, _ in _neighbors(r, c, nr, nc):
            if visited[rr, cc]:
                continue
            visited[rr, cc] = True
            level = max(float(z[rr, cc]), elevation + epsilon)
            if not math.isfinite(level):
                raise CivilError("elevation_overflow")
            filled[rr, cc] = level
            heapq.heappush(heap, (level, rr, cc))
    change = filled[valid] - z[valid]
    out = _grid_result(meta, filled, valid, "Priority-flood depression filling",
                       "Barnes, Lehman & Mulla (2014), Computers & Geosciences 62:117–127",
                       filled_cells=int(np.count_nonzero(change)),
                       fill_volume_m3=float(np.sum(change) * meta["cell_size"] ** 2),
                       epsilon_m=epsilon)
    out["warnings"].append("Filling removes real closed depressions as well as raster artefacts.")
    out["assumptions"].append("Outer edges and nodata-adjacent cells are open drainage boundaries (D8).")
    return out


def flow_direction_d8(dem):
    """Steepest-descent D8; exact-elevation flats route by BFS towards lower edges.

    Equally steep directions break ties E, SE, S, SW, W, NW, N, NE. On a flat
    with no lower edge, boundary cells are terminals. Closed flat depressions
    route to one explicit sink and are reported, never silently filled.
    """
    meta, z, valid = _dem(dem)
    nr, nc = valid.shape
    directions = np.where(valid, 0, -1).astype(np.int16)
    for r in range(nr):
        for c in range(nc):
            if not valid[r, c]:
                continue
            best = 0.0
            for rr, cc, code in _neighbors(r, c, nr, nc):
                if valid[rr, cc]:
                    distance = meta["cell_size"] * (math.sqrt(2) if rr != r and cc != c else 1)
                    descent = (z[r, c] - z[rr, cc]) / distance
                    if descent > best:
                        best, directions[r, c] = descent, code
    seen = ~valid.copy()
    sink_count = 0
    for r in range(nr):
        for c in range(nc):
            if seen[r, c]:
                continue
            component, queue = [], deque([(r, c)])
            seen[r, c] = True
            while queue:
                rr, cc = queue.popleft()
                component.append((rr, cc))
                for ar, ac, _ in _neighbors(rr, cc, nr, nc):
                    if not seen[ar, ac] and z[ar, ac] == z[r, c]:
                        seen[ar, ac] = True
                        queue.append((ar, ac))
            if all(directions[rr, cc] != 0 for rr, cc in component):
                continue
            seeds = [(rr, cc) for rr, cc in component if directions[rr, cc] > 0]
            if not seeds:
                seeds = [(rr, cc) for rr, cc in component if _edge(rr, cc, valid)]
            if not seeds:
                seeds = [min(component)]
                sink_count += 1
            reached = set(seeds)
            queue = deque(sorted(seeds))
            while queue:
                rr, cc = queue.popleft()
                for ar, ac, _ in _neighbors(rr, cc, nr, nc):
                    if valid[ar, ac] and z[ar, ac] == z[r, c] and (ar, ac) not in reached:
                        reached.add((ar, ac))
                        # Direction from the newly reached cell back towards its seed.
                        directions[ar, ac] = next(code for dr, dc, code in _D8
                                                   if ar + dr == rr and ac + dc == cc)
                        queue.append((ar, ac))
    out = _result("Steepest D8 with towards-lower-edge flat routing",
                  "O'Callaghan & Mark (1984); breadth-first shortest-hop flat resolution",
                  **meta, directions=directions.tolist(), valid=valid.tolist(),
                  closed_depressions=sink_count)
    out["assumptions"].extend(["0 is a terminal cell; -1 is nodata; ESRI powers-of-two D8 codes.",
                               "Equal-height flats route to lower edges before open boundary edges."])
    if sink_count:
        out["warnings"].append(f"unfilled_closed_depressions:{sink_count}")
    return out


def _routing(fdir):
    meta = _geometry(fdir)
    try:
        raw = np.asarray(fdir["directions"], dtype=float)
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise CivilError("invalid_flow_direction") from exc
    if raw.shape != (meta["nrows"], meta["ncols"]):
        raise CivilError("grid_shape_mismatch")
    if not np.all(np.isin(raw, [-1, 0, *_CODES])):
        raise CivilError("invalid_d8_code")
    directions, valid = raw.astype(np.int16), raw != -1
    if "valid" in fdir:
        try:
            declared = np.asarray(fdir["valid"])
        except (TypeError, ValueError) as exc:
            raise CivilError("flow_mask_mismatch") from exc
        if declared.shape != raw.shape or not np.array_equal(declared, valid):
            raise CivilError("flow_mask_mismatch")
    if not np.any(valid):
        raise CivilError("empty_flow_grid")
    nr, nc = valid.shape
    downstream = np.full(nr * nc, -1, dtype=np.int64)
    indegree = np.zeros(nr * nc, dtype=np.int64)
    for r in range(nr):
        for c in range(nc):
            code = int(directions[r, c])
            if code <= 0:
                continue
            dr, dc = _CODES[code]
            rr, cc = r + dr, c + dc
            if 0 <= rr < nr and 0 <= cc < nc:
                if not valid[rr, cc]:
                    raise CivilError("flow_into_nodata")
                downstream[r * nc + c] = rr * nc + cc
                indegree[rr * nc + cc] += 1
            # A supplied code pointing outside the raster is an explicit open outlet.
    queue = deque(int(i) for i in np.flatnonzero(valid.ravel() & (indegree == 0)))
    order = []
    while queue:
        i = queue.popleft()
        order.append(i)
        target = downstream[i]
        if target >= 0:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(int(target))
    if len(order) != int(np.count_nonzero(valid)):
        raise CivilError("flow_direction_cycle", "D8 routing must be an acyclic drainage graph.")
    return meta, directions, valid, downstream, order


def flow_accumulation(fdir, weights=None):
    """Topological D8 accumulation, INCLUDING each cell's own contribution.

    cells counts upstream cells; area_m2 is cells times cell area, independent
    of optional dimensionless weights. weighted_accumulation sums those weights.
    """
    meta, directions, valid, downstream, order = _routing(fdir)
    counts = valid.ravel().astype(np.int64)
    if weights is None:
        weighted = valid.ravel().astype(float)
    else:
        try:
            w = np.asarray(weights, dtype=float)
        except (TypeError, ValueError, OverflowError) as exc:
            raise CivilError("invalid_flow_weights") from exc
        if w.shape != valid.shape or np.any(~np.isfinite(w[valid])) or np.any(w[valid] < 0):
            raise CivilError("invalid_flow_weights")
        weighted = np.where(valid, w, 0.0).ravel().copy()
    for i in order:
        target = downstream[i]
        if target >= 0:
            counts[target] += counts[i]
            weighted[target] += weighted[i]
    if np.any(~np.isfinite(weighted)):
        raise CivilError("accumulation_overflow")
    shape = valid.shape
    out = _result("Topological D8 accumulation", "Directed acyclic graph mass accumulation",
                  **meta, directions=directions.tolist(), valid=valid.tolist(),
                  cells=np.where(valid, counts.reshape(shape), 0).tolist(),
                  area_m2=_array_json(counts.reshape(shape) * meta["cell_size"] ** 2, valid),
                  weighted_accumulation=_array_json(weighted.reshape(shape), valid))
    out["assumptions"].append("Counts and areas include the receiving cell; nodata cells have count 0 and area null.")
    return out


def _inside(point, ring):
    x, y = point
    inside = False
    for a, b in zip(ring, ring[1:]):
        if (a[1] > y) != (b[1] > y):
            crossing = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x < crossing:
                inside = not inside
    return inside


def _boundaries(mask, meta):
    # Edges in Cartesian integer grid coordinates, directed with filled cell left.
    nr, nc = mask.shape
    edges = set()
    for r, c in zip(*np.nonzero(mask)):
        x, y = int(c), nr - int(r) - 1
        if r == nr - 1 or not mask[r + 1, c]:
            edges.add(((x, y), (x + 1, y)))
        if c == nc - 1 or not mask[r, c + 1]:
            edges.add(((x + 1, y), (x + 1, y + 1)))
        if r == 0 or not mask[r - 1, c]:
            edges.add(((x + 1, y + 1), (x, y + 1)))
        if c == 0 or not mask[r, c - 1]:
            edges.add(((x, y + 1), (x, y)))
    outgoing = {}
    for a, b in edges:
        outgoing.setdefault(a, set()).add(b)
    rings, areas = [], []
    starts = deque(sorted(edges))
    while edges:
        while starts[0] not in edges:
            starts.popleft()
        start, nxt = starts.popleft()
        ring = [start]
        previous, current = start, nxt
        edges.remove((previous, current))
        outgoing[previous].remove(current)
        while current != start:
            ring.append(current)
            options = outgoing.get(current, set())
            if not options:
                raise CivilError("boundary_trace_failed")
            dx, dy = current[0] - previous[0], current[1] - previous[1]
            # At corner-touch junctions, left turn keeps distinct rings separate.
            def turn(candidate):
                ex, ey = candidate[0] - current[0], candidate[1] - current[1]
                return math.atan2(dx * ey - dy * ex, dx * ex + dy * ey)
            following = max(options, key=turn)
            edges.remove((current, following))
            outgoing[current].remove(following)
            previous, current = current, following
        ring.append(start)
        area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:])) / 2
        areas.append(area)
        rings.append([[meta["origin_x"] + x * meta["cell_size"],
                       meta["origin_y"] + y * meta["cell_size"]] for x, y in ring])
    polygons = [[ring] for ring, area in zip(rings, areas) if area > 0]
    for ring, area in zip(rings, areas):
        if area < 0:
            owners = [polygon for polygon in polygons if _inside(ring[0], polygon[0])]
            if len(owners) != 1:
                raise CivilError("boundary_hole_assignment_failed")
            owners[0].append(ring)
    geometry = ({"type": "Polygon", "coordinates": polygons[0]} if len(polygons) == 1
                else {"type": "MultiPolygon", "coordinates": polygons})
    return geometry, rings


def watershed(fdir, outlet_xy):
    """Trace upstream D8 cells and their exact cell-edge Polygon/MultiPolygon.

    outlet_xy selects its containing cell; outer raster edges are half-open.
    Holes and corner-connected components are retained, not convex-hull filled.
    Boundary coordinates remain in the projected project CRS, not RFC 7946.
    """
    meta, directions, valid, downstream, _ = _routing(fdir)
    if not isinstance(outlet_xy, (list, tuple, np.ndarray)) or len(outlet_xy) != 2:
        raise CivilError("invalid_outlet")
    x, y = (_number(v, "invalid_outlet") for v in outlet_xy)
    c = math.floor((x - meta["origin_x"]) / meta["cell_size"])
    south_row = math.floor((y - meta["origin_y"]) / meta["cell_size"])
    r = meta["nrows"] - 1 - south_row
    if not (0 <= r < meta["nrows"] and 0 <= c < meta["ncols"]):
        raise CivilError("outlet_outside_grid")
    if not valid[r, c]:
        raise CivilError("outlet_on_nodata")
    mask = np.zeros(valid.shape, dtype=bool)
    mask[r, c] = True
    queue = deque([(r, c)])
    while queue:
        rr, cc = queue.popleft()
        target = rr * meta["ncols"] + cc
        for ar, ac, _ in _neighbors(rr, cc, *valid.shape):
            if valid[ar, ac] and not mask[ar, ac] and downstream[ar * meta["ncols"] + ac] == target:
                mask[ar, ac] = True
                queue.append((ar, ac))
    boundary, rings = _boundaries(mask, meta)
    return _result("D8 upstream delineation and cell-edge tracing",
                   "Directed graph reverse reachability; raster cell-union boundary",
                   **meta, mask=mask.tolist(), cells=int(np.count_nonzero(mask)),
                   area_m2=float(np.count_nonzero(mask) * meta["cell_size"] ** 2),
                   outlet_xy_m=[x, y], outlet_cell=[r, c],
                   boundary=boundary, boundary_rings=rings)


def streams(acc, threshold_cells):
    """Extract centre-to-centre stream reaches, split at sources/junctions/outlets.

    acc must retain directions from flow_accumulation. Single selected outlet
    cells are returned separately as isolated_points, never fictitious lines.
    """
    meta, _, valid, downstream, order = _routing(acc)
    threshold = _number(threshold_cells, "invalid_stream_threshold")
    if threshold <= 0:
        raise CivilError("invalid_stream_threshold")
    try:
        values = np.asarray(acc["cells"], dtype=float)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CivilError("invalid_accumulation") from exc
    if (values.shape != valid.shape or np.any(~np.isfinite(values[valid]))
            or np.any(values[valid] < 1) or np.any(values[valid] != np.floor(values[valid]))):
        raise CivilError("invalid_accumulation")
    selected = (valid & (values >= threshold)).ravel()
    incoming = np.zeros(selected.size, dtype=np.int64)
    for i in order:
        j = downstream[i]
        if selected[i] and j >= 0 and selected[j]:
            incoming[j] += 1
    lines, isolated = [], []
    for i in order:
        if not selected[i] or incoming[i] == 1:
            continue
        indices = [i]
        j = downstream[i]
        while j >= 0 and selected[j]:
            indices.append(int(j))
            if incoming[j] != 1:
                break
            j = downstream[j]
        coordinates = [_center(meta, *divmod(k, meta["ncols"])) for k in indices]
        if len(coordinates) > 1:
            lines.append(coordinates)
        elif incoming[i] == 0:
            isolated.append(coordinates[0])
    return _result("D8 threshold stream extraction", "Upstream cell-count threshold and graph reach segmentation",
                   polylines=lines, isolated_points=isolated, threshold_cells=threshold,
                   units={"polylines": "m", "isolated_points": "m"})


def slope_percent(dem):
    """Horn 3x3 weighted gradients, slope=100*hypot(dz/dx,dz/dy) (Horn 1981).

    Raster edges and stencils touching nodata return null, without padding or
    inventing elevations. At least three rows and columns are required.
    """
    meta, z, valid = _dem(dem)
    nr, nc = valid.shape
    if nr < 3 or nc < 3:
        raise CivilError("grid_too_small_for_horn")
    slope = np.zeros(z.shape)
    output_valid = np.zeros(z.shape, dtype=bool)
    for r in range(1, nr - 1):
        for c in range(1, nc - 1):
            if not np.all(valid[r - 1:r + 2, c - 1:c + 2]):
                continue
            a, b, d = z[r - 1, c - 1:c + 2]
            e, _, f = z[r, c - 1:c + 2]
            g, j, k = z[r + 1, c - 1:c + 2]
            dx = ((d + 2 * f + k) - (a + 2 * e + g)) / (8 * meta["cell_size"])
            dy = ((a + 2 * b + d) - (g + 2 * j + k)) / (8 * meta["cell_size"])
            slope[r, c] = 100 * math.hypot(dx, dy)
            output_valid[r, c] = True
    out = _result("Horn terrain slope", "Horn (1981), Proceedings IEEE 69:14–47",
                  **meta, slope_pct=_array_json(slope, output_valid))
    out["assumptions"].append("Only complete valid 3x3 stencils produce slopes; edges are null.")
    return out
