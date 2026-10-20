"""Deterministic mesh DFAM checks; geometric measurements are not slicer output.

Surface checks retain exact normals and areas. Grid checks report their pitch
and use signed depth crossings (union material, inward cavity surfaces). All
reported locations are transformed back into the original model frame.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull, QhullError
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

from .mesh import PrintMesh
from .params import PrintParams
from .geometry import BuildGeometry, RayCaster, depth_peel, fit_cylinder, surface_samples, voxelize

CHECK_NAMES = ("min_wall", "overhang", "bridges", "support", "small_holes", "internal_voids",
               "bed_adhesion", "build_volume")


def row(check: str, status: str, note: str, metrics=None, limits=None, regions=None, fix=None,
        omitted: int = 0, detail: bool = False) -> dict:
    if omitted:
        note += f" {omitted} more regions are not shown."
        if not detail:
            note += " To show up to 50 regions, set detail=true."
    return {"check": check, "status": status,
            "ok": False if status == "fail" else True if status in ("pass", "warn") else None,
            "note": note, "metrics": metrics or {}, "limits": limits or {},
            "regions": regions or [], "regions_omitted": int(omitted), "fix": fix or []}


_STATUS_RANK = {"fail": 0, "warn": 1}


def _ranked(regions: list, limit: int, severity) -> tuple[list, int]:
    """(shown, omitted): failing regions first, then warnings, then the rest.

    Inside one status the most severe region (lowest `severity(region)`) comes first, so a
    failing region is never cut off by warnings that measure worse (a knife edge thinner
    than the failing wall). The sort is stable, so equal regions keep their order."""
    order = sorted(regions, key=lambda r: (_STATUS_RANK.get(r.get("status"), 2), severity(r)))
    return order[:limit], max(len(order) - limit, 0)


def rounded(value):
    """Normalize numpy results to finite, deterministic JSON-compatible values."""
    if isinstance(value, dict):
        return {k: (round(float(v), 1) if k.endswith("_mm2") and
                    isinstance(v, (float, np.floating)) and np.isfinite(v) else rounded(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [rounded(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return round(float(value), 3) if np.isfinite(value) else None
    return value


def _edge_pairs(g, tids):
    """(left, right, unit shared-edge direction, edge length) for edge-adjacent triangles.

    Display and STL vertices are welded by position first; left/right index into `tids`."""
    corners = g.triangles[tids]
    _, welded = np.unique(np.round(corners.reshape(-1, 3) / 1e-4).astype(np.int64), axis=0,
                          return_inverse=True)
    faces = welded.reshape(-1, 3)
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    vectors = np.concatenate([corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 1],
                              corners[:, 0] - corners[:, 2]])
    edges.sort(axis=1)
    owners = np.tile(np.arange(len(tids)), 3)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    adjacent = np.all(edges[order[1:]] == edges[order[:-1]], axis=1)
    first = order[:-1][adjacent]
    lengths = np.linalg.norm(vectors[first], axis=1)
    direction = vectors[first] / np.maximum(lengths, 1e-30)[:, None]
    return owners[first], owners[order[1:][adjacent]], direction, lengths


def _labels(count: int, left, right) -> np.ndarray:
    """Connected-component label per node of the undirected (left, right) graph."""
    graph = csr_matrix((np.ones(2*len(left), dtype=np.int8),
                        (np.concatenate([left, right]), np.concatenate([right, left]))),
                       shape=(count, count))
    return connected_components(graph, directed=False)[1]


def _components(tids, labels) -> list:
    """Groups of `tids` (6+ triangles) sharing a label."""
    order = np.argsort(labels, kind="stable")
    offsets = np.searchsorted(labels[order], np.arange(labels.max(initial=-1)+2))
    return [tids[order[a:b]] for a, b in zip(offsets[:-1], offsets[1:]) if b-a >= 6]


def _smooth_groups(g, tids):
    """Weld display vertices by position, then split at sharp normal creases."""
    if len(tids) < 6:
        return []
    left, right, _, _ = _edge_pairs(g, tids)
    smooth = np.einsum("ij,ij->i", g.normals[tids[left]], g.normals[tids[right]]) > math.cos(math.radians(20))
    return _components(tids, _labels(len(tids), left[smooth], right[smooth]))


_PARALLEL_COS = math.cos(math.radians(1))


def _axial_patches(g, tids):
    """Cylinder patches inside one smooth raw-mesh group that is not a cylinder as a whole.

    A crease-angle smooth group welds a bore to the countersink, fillet or wall it meets
    tangentially (6-axis wrist STL: two 1.8 mm bores, their countersinks and every filleted
    face were one 5599-triangle group), so the group never fits a cylinder and the hole goes
    unseen. Every facet crease of a tessellated cylinder runs parallel to its axis, and that
    axial edge is each facet's longest crease. Triangles are joined only across a crease that
    is the longest crease of both, or across a flat edge (the other half of a facet quad)
    when both longest creases are parallel; a patch whose internal creases are not all
    parallel is not a cylinder and is dropped (torus and cone bands)."""
    left, right, direction, lengths = _edge_pairs(g, tids)
    dot = np.einsum("ij,ij->i", g.normals[tids[left]], g.normals[tids[right]])
    flat = dot >= math.cos(math.radians(.5))
    crease = ~flat & (dot > math.cos(math.radians(20)))
    if crease.sum() < 3:
        return []
    # Longest crease edge per triangle; a triangle without a crease joins no patch.
    edge = np.tile(np.flatnonzero(crease), 2)
    owner = np.concatenate([left[crease], right[crease]])
    order = np.lexsort((-lengths[edge], owner))
    owner, edge = owner[order], edge[order]
    first = np.flatnonzero(np.r_[True, owner[1:] != owner[:-1]])
    longest = np.full(len(tids), -1)
    longest[owner[first]] = edge[first]
    defined = longest >= 0
    axis = np.zeros((len(tids), 3))
    axis[defined] = direction[longest[defined]]
    index = np.arange(len(left))
    join = ((crease & (longest[left] == index) & (longest[right] == index)) |
            (flat & defined[left] & defined[right] &
             (np.abs(np.einsum("ij,ij->i", axis[left], axis[right])) >= _PARALLEL_COS)))
    labels = _labels(len(tids), left[join], right[join])
    labels[~defined] = -1
    # Every crease inside one patch must run along that patch's axis.
    inside = crease & (labels[left] == labels[right]) & (labels[left] >= 0)
    skew = inside & (np.abs(np.einsum("ij,ij->i", direction, axis[left])) < _PARALLEL_COS)
    labels[np.isin(labels, labels[left[skew]])] = -1
    keep = labels >= 0
    return _components(tids[keep], labels[keep])


@dataclass
class CheckContext:
    geom: BuildGeometry
    params: PrintParams
    detail: bool = False
    _peel: object | None = None
    _vox: object | None = None
    _support: dict | None = None
    _holes: list | None = None
    _bridges: dict | None = None
    _enclosed: tuple[float, float] | None = None
    _inaccessible: dict | None = None

    @property
    def limit(self) -> int:
        return 50 if self.detail else 5

    @property
    def peel(self):
        if self._peel is None:
            diag = np.linalg.norm(self.geom.bounds[1, :2] - self.geom.bounds[0, :2])
            self._peel = depth_peel(self.geom, cell=max(0.25, diag / 400))
        return self._peel

    @property
    def vox(self):
        if self._vox is None:
            self._vox = voxelize(self.geom)
        return self._vox

    def region(self, tids, **numbers) -> dict:
        tids = np.asarray(tids, dtype=int)
        faces, owners = set(), set()
        for fid in np.unique(self.geom.mesh.tri_face[tids]):
            if fid >= 0:
                info = self.geom.mesh.faces[fid]
                if info.sid:
                    faces.add(info.sid)
                if info.owner_feature:
                    owners.add(info.owner_feature)
        center = numbers.pop("center", None)
        if center is None:
            center = np.average(self.geom.centroids[tids], axis=0, weights=self.geom.areas[tids])
        return {"faces": sorted(faces), "built_by": sorted(owners),
                "centroid": self.geom.model_points(np.asarray(center).reshape(1, 3))[0], **numbers}

    def groups(self, tids):
        tids = np.asarray(tids, dtype=int)
        for fid in np.unique(self.geom.mesh.tri_face[tids]):
            yield tids[self.geom.mesh.tri_face[tids] == fid]

    @property
    def angles(self):
        return np.degrees(np.arcsin(np.clip(-self.geom.normals[:, 2], 0, 1)))

    @property
    def bed_contact(self):
        return ((self.geom.normals[:, 2] < -0.99) &
                (self.geom.triangles[:, :, 2].max(axis=1) <= 0.05))

    @property
    def holes(self):
        if self._holes is not None:
            return self._holes
        candidates = []
        g = self.geom
        groups = []  # (triangle ids, raw smooth group that may hide cylinders)
        for fid, info in enumerate(g.mesh.faces):
            tids = np.flatnonzero(g.mesh.tri_face == fid)
            if info.surf_type in (1, 4, 5) and len(tids) >= 6:
                groups.append((tids, False))
            elif info.surf_type == 6:
                groups.extend((group, True) for group in _smooth_groups(g, tids))
        groups.extend((group, True) for group in _smooth_groups(g, np.flatnonzero(g.mesh.tri_face < 0)))
        fits = []
        for tids, raw in groups:
            fit = fit_cylinder(g.triangles[tids].reshape(-1, 3), np.repeat(g.normals[tids], 3, axis=0))
            if fit is not None:
                fits.append((tids, fit))
            elif raw:
                for patch in _axial_patches(g, tids):
                    fit = fit_cylinder(g.triangles[patch].reshape(-1, 3), np.repeat(g.normals[patch], 3, axis=0))
                    if fit is not None:
                        fits.append((patch, fit))
        for tids, fit in fits:
            if fit["concave"]:
                fit = dict(fit, tids=tids)
                # CAD kernels can split a single cylindrical wall into multiple faces.
                duplicate = None
                for old in candidates:
                    axis = fit["axis"]
                    delta = fit["center"] - old["center"]
                    radial = delta - np.dot(delta, axis) * axis
                    if (abs(np.dot(axis, old["axis"])) > 0.999 and
                            abs(fit["radius"] - old["radius"]) < 0.05 and np.linalg.norm(radial) < 0.05):
                        duplicate = old
                        break
                if duplicate is None:
                    candidates.append(fit)
                else:
                    duplicate["tids"] = np.concatenate([duplicate["tids"], tids])
        self._holes = candidates
        return candidates

    @property
    def small_horizontal(self):
        tids = []
        for h in self.holes:
            if abs(h["axis"][2]) < math.sin(math.radians(15)) and 2 * h["radius"] <= self.params.teardrop_hole_mm:
                tids.extend(h["tids"])
        return np.asarray(tids, dtype=int)


def min_wall(ctx: CheckContext) -> dict:
    g, p = ctx.geom, ctx.params
    points, normals, tids, weights = surface_samples(g)
    reach = max(3 * p.min_wall_mm, 3.0, 2 * p.line_width_mm, 3 * p.layer_height_mm)
    eps = min(1e-4, p.nozzle_mm * 1e-3)
    distances, hits = RayCaster(g, cell=reach).cast(points - eps * normals, -normals,
                                                    reach, opposing_normals=normals)
    thickness = distances + eps
    finite = np.isfinite(thickness)
    vertical = np.abs(normals[:, 2]) < 0.5
    fdm = p.process == "fdm"
    required = max(p.min_wall_mm, p.nozzle_mm) if fdm else p.min_wall_mm
    # A wall has near-parallel opposing faces. Where they converge at more than ~25°
    # (a fillet or arc running out into a flat), the thin strip is a knife edge: the
    # slicer drops or feathers it, which is a cosmetic warning, not a wall that fails.
    opposing = np.full(len(points), -1.0)
    hit_ids = np.asarray(hits)
    valid_hits = finite & (hit_ids >= 0)
    opposing[valid_hits] = np.einsum("ij,ij->i", normals[valid_hits],
                                     g.normals[hit_ids[valid_hits].astype(int)])
    wedge = finite & (opposing > -0.9)
    thin = finite & (thickness < required - 1e-6)
    failing = thin & ~wedge
    knife = thin & wedge
    warning = (fdm & finite & ~thin & (((vertical & (thickness < 2 * p.line_width_mm)) |
                                       (~vertical & (thickness < 3 * p.layer_height_mm))))) | knife
    gap = fdm & finite & vertical & (thickness >= 2 * p.line_width_mm) & (thickness <= 6 * p.line_width_mm)
    perimeter_count = thickness[finite] / p.line_width_mm
    gap[finite] &= np.abs(perimeter_count - np.round(perimeter_count)) > 0.25
    regions = []
    for group in ctx.groups(tids[failing | warning]):
        mask = np.isin(tids, group) & (failing | warning)
        # The worst real wall names the region; knife-edge slivers only when nothing fails.
        pick = mask & failing if (mask & failing).any() else mask
        index = np.flatnonzero(pick)[np.argmin(thickness[pick])]
        t = float(thickness[index])
        regions.append(ctx.region(group, center=np.average(points[mask], axis=0, weights=weights[mask]),
                                  thickness_mm=t, thin_area_mm2=float(weights[mask].sum()),
                                  classification=("knife_edge" if knife[index] else
                                                  "vertical_wall" if vertical[index] else
                                                  "horizontal_skin"),
                                  perimeters=t / p.line_width_mm if vertical[index] else None,
                                  layers=t / p.layer_height_mm if not vertical[index] else None,
                                  status="fail" if failing[mask].any() else "warn"))
    shown, omitted = _ranked(regions, ctx.limit, lambda r: r["thickness_mm"])
    walls = finite & ~wedge
    value = float(thickness[walls].min()) if walls.any() else (
        float(thickness[finite].min()) if finite.any() else None)
    status = "fail" if failing.any() else "warn" if warning.any() else "pass"
    target = max(p.min_wall_mm, p.nozzle_mm, 2 * p.line_width_mm) if fdm else p.min_wall_mm
    remedy = (f"thicken to >= {target:g} mm ({math.ceil(target / p.line_width_mm)} perimeters at {p.line_width_mm:g} mm)"
              if fdm else f"thicken to >= {target:g} mm")
    result = row("min_wall", status,
                 f"Minimum measured wall {value:.3f} mm; required {p.min_wall_mm:g} mm." if value is not None else
                 f"No wall thinner than the {reach:g} mm search range; required {p.min_wall_mm:g} mm.",
                 {"min_thickness_mm": value, "search_distance_mm": reach,
                  "thin_area_mm2": float(weights[failing | warning].sum()),
                  "knife_edge_area_mm2": float(weights[knife].sum()), "samples": len(points),
                  "gap_fill": {"status": "warn" if gap.any() else "pass",
                               "area_mm2": float(weights[gap].sum()),
                               "note": "Walls between 2 and 6 line widths far from an integer perimeter count require gap fill."}},
                 {"min_wall_mm": p.min_wall_mm, "nozzle_mm": p.nozzle_mm,
                  "line_width_mm": p.line_width_mm, "minimum_skin_layers": 3}, shown,
                 [remedy] if status != "pass" else [], omitted, ctx.detail)
    result.update(value_mm=value, limit_mm=p.min_wall_mm)
    return result


def _material_at(peel, height: float) -> np.ndarray:
    winding = np.zeros(np.prod(peel.shape), dtype=np.int32)
    selected = peel.z < height - 1e-6
    # Down-facing crossings enter material; up-facing crossings leave it.
    crossing = getattr(peel, "_crossing_winding", -peel.sign)
    np.add.at(winding, peel.cells[selected], crossing[selected])
    return (winding > 0).reshape(peel.shape)


def _opposite_spans(ceiling: np.ndarray, anchors: np.ndarray, cell: float) -> np.ndarray:
    """Shortest opposing-anchor span along X/Y and both diagonals; reject cantilevers."""
    best = np.full(ceiling.shape, np.inf)
    for axis in (0, 1):
        a = np.moveaxis(anchors, axis, 0)
        c = np.moveaxis(ceiling, axis, 0)
        indexes = np.arange(a.shape[0])[:, None]
        left = np.maximum.accumulate(np.where(a, indexes, -100000), axis=0)
        right = np.minimum.accumulate(np.where(a, indexes, 100000)[::-1], axis=0)[::-1]
        # Reject anchors reached by crossing a gap outside this ceiling.
        barrier_left = np.maximum.accumulate(np.where(~(a | c), indexes, -100000), axis=0)
        barrier_right = np.minimum.accumulate(np.where(~(a | c), indexes, 100000)[::-1], axis=0)[::-1]
        valid = c & (left > barrier_left) & (right < barrier_right) & (left < indexes) & (right > indexes)
        span = np.where(valid, np.maximum(right - left - 1, 0) * cell, np.inf)
        best = np.minimum(best, np.moveaxis(span, 0, axis))
    nx, ny = ceiling.shape
    ii, jj = np.indices(ceiling.shape)
    stride = nx + 1
    for group in (ii - jj + ny - 1, ii + jj):
        keys = (group * stride + ii).ravel()
        order = np.argsort(keys, kind="stable")
        key = keys[order]
        a, c = anchors.ravel()[order], ceiling.ravel()[order]
        left = np.maximum.accumulate(np.where(a, key, -stride))
        right = np.minimum.accumulate(np.where(a, key, keys.max() + stride)[::-1])[::-1]
        barrier_left = np.maximum.accumulate(np.where(~(a | c), key, -stride))
        barrier_right = np.minimum.accumulate(np.where(~(a | c), key, keys.max() + stride)[::-1])[::-1]
        valid = (c & (left // stride == key // stride) & (right // stride == key // stride) &
                 (left > barrier_left) & (right < barrier_right) & (left < key) & (right > key))
        span = np.where(valid, np.maximum(right-left-1, 0) * math.sqrt(2) * cell, np.inf)
        target = best.ravel()
        target[order] = np.minimum(target[order], span)
    return best


def bridge_data(ctx: CheckContext) -> dict:
    if ctx._bridges is not None:
        return ctx._bridges
    peel, p = ctx.peel, ctx.params
    accepted = np.zeros(len(peel.z), dtype=bool)
    bridge = np.zeros(len(peel.z), dtype=bool)
    spans = np.full(len(peel.z), np.inf)
    cantilever = np.zeros(len(peel.z))
    active = getattr(peel, "_active_surface", np.ones(len(peel.z), dtype=bool))
    ceiling_idx = np.flatnonzero((ctx.angles[peel.tri] >= 80) & ~ctx.bed_contact[peel.tri] & active)
    if len(ceiling_idx):
        heights = np.round(peel.z[ceiling_idx] / max(p.layer_height_mm / 2, 0.05)).astype(int)
        for level in np.unique(heights):
            ids = ceiling_idx[heights == level]
            z = float(np.median(peel.z[ids]))
            ceiling = np.zeros(np.prod(peel.shape), dtype=bool)
            ceiling[peel.cells[ids]] = True
            ceiling = ceiling.reshape(peel.shape)
            anchors = _material_at(peel, z - max(p.layer_height_mm, 0.05))
            span_grid = _opposite_spans(ceiling, anchors, peel.cell)
            local = span_grid.reshape(-1)[peel.cells[ids]]
            anchor_distance = ndimage.distance_transform_edt(~anchors) * peel.cell if anchors.any() else np.full(peel.shape, np.inf)
            cantilever[ids] = anchor_distance.reshape(-1)[peel.cells[ids]]
            spans[ids] = local
            bridge[ids] = np.isfinite(local)
            accepted[ids] = local <= 2 * p.max_bridge_mm + 1e-6
    ctx._bridges = {"accepted": accepted, "bridge": bridge, "spans": spans, "cantilever": cantilever}
    return ctx._bridges


def bridges(ctx: CheckContext) -> dict:
    data, p, peel = bridge_data(ctx), ctx.params, ctx.peel
    indexes = np.flatnonzero(data["bridge"])
    value = float(data["spans"][indexes].max()) if len(indexes) else 0.0
    def tier(span: float) -> str:
        return "fail" if span > 2 * p.max_bridge_mm + 1e-6 else "warn" if span > p.max_bridge_mm + 1e-6 else "pass"
    status = tier(value)
    regions = []
    for group in ctx.groups(peel.tri[indexes]):
        ids = indexes[np.isin(peel.tri[indexes], group)]
        span = float(data["spans"][ids].max())
        regions.append(ctx.region(group, span_mm=span, status=tier(span),
                                  area_mm2=len(np.unique(peel.cells[ids])) * peel.cell ** 2))
    shown, omitted = _ranked(regions, ctx.limit, lambda r: -r["span_mm"])
    return row("bridges", status, f"Largest two-sided bridge span {value:.3f} mm; clean bridge limit {p.max_bridge_mm:g} mm, fail above {2*p.max_bridge_mm:g} mm.",
               {"max_span_mm": value, "bridge_area_mm2": len(indexes) * peel.cell ** 2,
                "grid_mm": peel.cell}, {"max_bridge_mm": p.max_bridge_mm, "fail_span_mm": 2*p.max_bridge_mm},
               shown, ["slicer bridges it but expect sag; add support/sacrificial layer or shorten"] if status == "warn" else
               ["shorten bridge span or add accessible supports"] if status == "fail" else [],
               omitted, ctx.detail)


def support_data(ctx: CheckContext) -> dict:
    if ctx._support is not None:
        return ctx._support
    peel, g, p = ctx.peel, ctx.geom, ctx.params
    active = getattr(peel, "_active_surface", np.ones(len(peel.z), dtype=bool))
    needed = ((ctx.angles[peel.tri] > p.overhang_max_deg + 1e-6) & ~ctx.bed_contact[peel.tri] & active)
    if p.process == "fdm":
        needed &= ~bridge_data(ctx)["accepted"]
        needed &= ~np.isin(peel.tri, ctx.small_horizontal)
    scale = float(g.bounds[1, 2] + 1)
    keys = peel.cells * scale
    up = np.where((peel.sign > 0) & active, keys + peel.z, -np.inf)
    running = np.maximum.accumulate(up)
    below = np.maximum(running - keys, 0)
    below = np.minimum(below, peel.z)
    heights = np.where(needed, np.maximum(peel.z - below, 0), 0)
    needed &= heights > 1e-5
    ids = np.flatnonzero(needed)
    # Union overlapping support intervals per column, rather than double-count stacks.
    volume = 0.0
    on_model = 0
    previous_cell = -1
    end = 0.0
    for i in ids:
        cid, lo, hi = int(peel.cells[i]), float(below[i]), float(peel.z[i])
        if cid != previous_cell:
            end = 0.0
            previous_cell = cid
        volume += max(hi - max(lo, end), 0) * peel.cell ** 2
        end = max(end, hi)
        on_model += lo > 0.05
    ctx._support = {"needed": needed, "below": below, "heights": heights,
                    "volume": volume, "on_model_area": on_model * peel.cell ** 2}
    return ctx._support


def overhang(ctx: CheckContext) -> dict:
    g, p = ctx.geom, ctx.params
    data = support_data(ctx)
    peel = ctx.peel
    counts = np.bincount(peel.tri[data["needed"]], minlength=len(g.areas))
    total = np.bincount(peel.tri, minlength=len(g.areas))
    projected = g.areas * np.abs(g.normals[:, 2])
    fraction = np.minimum(np.divide(counts * peel.cell**2, projected,
                                   out=np.zeros_like(g.areas), where=projected > 1e-12), 1.)
    # Sub-grid sloped surfaces still have exact normal evidence.
    unobserved = ((total == 0) & (projected <= 2*peel.cell**2) &
                  (ctx.angles > p.overhang_max_deg) & ~ctx.bed_contact)
    unobserved[ctx.small_horizontal] = False
    fraction[unobserved] = 1
    tids = np.flatnonzero(fraction > 0)
    area = float(np.sum(g.areas * fraction))
    ceiling_fail = False
    fail_tids = np.zeros(0, dtype=int)
    if p.process == "fdm":
        bridge_info = bridge_data(ctx)
        unbridged = data["needed"] & ((bridge_info["cantilever"] > 2*p.line_width_mm) |
                                      (bridge_info["bridge"] & ~bridge_info["accepted"]))
        ceiling_fail = bool(np.any(unbridged))
        fail_tids = np.unique(peel.tri[unbridged])
    regions, hints = [], []
    for group in ctx.groups(tids):
        z = float(g.triangles[group, :, 2].min())
        infos = [g.mesh.faces[f] for f in np.unique(g.mesh.tri_face[group]) if f >= 0]
        angle = float(ctx.angles[group].max())
        region_area = float(np.sum(g.areas[group] * fraction[group]))
        # Same rule as the row status, applied to this region alone.
        region_status = ("fail" if region_area > 1.0 or np.isin(group, fail_tids).any() else
                         "warn" if region_area > 1e-5 else "pass")
        regions.append(ctx.region(group, area_mm2=region_area, max_angle_deg=angle, z_mm=z,
                                  status=region_status))
        if any(info.surf_type in (1, 4) for info in infos) and z <= np.ptp(g.triangles[group, :, 2]):
            hints.append("replace bed-edge fillet with a 45 deg chamfer")
        elif any(np.intersect1d(group, h["tids"]).size for h in ctx.holes):
            hints.append("replace large horizontal hole crown with teardrop/diamond geometry")
        elif angle >= 80:
            hints.append("shorten ceiling to a bridgeable span or reorient")
        else:
            hints.append("add 45 deg chamfers or reorient")
    shown, omitted = _ranked(regions, ctx.limit, lambda r: -r["area_mm2"])
    small = [ctx.region(h["tids"], diameter_mm=2*h["radius"], classification="self_supporting_horizontal_hole")
             for h in ctx.holes if abs(h["axis"][2]) < math.sin(math.radians(15)) and 2*h["radius"] <= p.teardrop_hole_mm]
    # Any downward area above 1 mm² that needs support is a design failure (dfm_check's
    # long-standing contract); slivers below it are advice. Ceilings and cantilevers that
    # cannot even bridge fail regardless of area.
    status = "fail" if ceiling_fail or area > 1.0 else "warn" if area > 1e-5 else "pass"
    return row("overhang", status,
               f"{area:.1f} mm2 needs support above {p.overhang_max_deg:g} deg from vertical (bridges and bed contact excluded).",
               {"area_mm2": area, "face_count": len(regions),
                "max_angle_deg": float(ctx.angles[tids].max()) if len(tids) else 0.,
                "self_supporting_holes": small[:ctx.limit]}, {"overhang_max_deg": p.overhang_max_deg},
               shown, sorted(set(hints)), omitted, ctx.detail)


def _air_components(vox):
    labels, count = ndimage.label(~vox.solid)
    boundary = np.unique(np.concatenate([labels[0].ravel(), labels[-1].ravel(), labels[:, 0].ravel(),
                                          labels[:, -1].ravel(), labels[:, :, 0].ravel(), labels[:, :, -1].ravel()]))
    sealed = np.setdiff1d(np.arange(1, count+1), boundary)
    return labels, sealed


def _enclosed_support(ctx: CheckContext) -> tuple[float, float]:
    if ctx._enclosed is not None:
        return ctx._enclosed
    data, vox, peel = support_data(ctx), ctx.vox, ctx.peel
    ids = np.flatnonzero(data["needed"])
    if not len(ids):
        return 0., 0.
    # Map voxel XY centers to depth columns. Intervals remain exact in Z.
    nx, ny, nz = vox.solid.shape
    xx = vox.origin[0] + (np.arange(nx) + .5) * vox.cell
    yy = vox.origin[1] + (np.arange(ny) + .5) * vox.cell
    ix = np.floor((xx - peel.origin[0]) / peel.cell).astype(int)
    iy = np.floor((yy - peel.origin[1]) / peel.cell).astype(int)
    goodx, goody = (ix >= 0) & (ix < peel.shape[0]), (iy >= 0) & (iy < peel.shape[1])
    cid = np.clip(ix, 0, peel.shape[0]-1)[:, None] * peel.shape[1] + np.clip(iy, 0, peel.shape[1]-1)[None, :]
    zz = vox.origin[2] + (np.arange(nz) + .5) * vox.cell
    mask = np.zeros_like(vox.solid)
    # Vectorized column buckets: at most 3M voxels, bounded per support crossing.
    starts = np.searchsorted(peel.cells, cid.ravel(), side="left")
    ends = np.searchsorted(peel.cells, cid.ravel(), side="right")
    flat = mask.reshape(-1, nz)
    validxy = (goodx[:, None] & goody[None, :]).ravel()
    for slot in range(int((ends-starts).max(initial=0))):
        indexes = starts + slot
        valid = (indexes < ends) & validxy
        dest = np.flatnonzero(valid)
        indexes = indexes[valid]
        active = data["needed"][indexes]
        dest, indexes = dest[active], indexes[active]
        if len(dest):
            flat[dest] |= ((zz[None, :] >= data["below"][indexes, None]) &
                           (zz[None, :] < peel.z[indexes, None]))
    mask &= ~vox.solid
    air = ~vox.solid
    # Pad the distance transform so boundary cells see genuinely unbounded exterior.
    pad = max(1, math.ceil(ctx.params.access_mm / (2*vox.cell)) + 1)
    distance = ndimage.distance_transform_edt(np.pad(air, pad, constant_values=True))[pad:-pad, pad:-pad, pad:-pad] * vox.cell
    accessible_core = air & (distance >= ctx.params.access_mm / 2)
    labels, _ = ndimage.label(accessible_core)
    exterior_ids = np.unique(np.concatenate([labels[0].ravel(), labels[-1].ravel(), labels[:, 0].ravel(),
                                             labels[:, -1].ravel(), labels[:, :, 0].ravel(), labels[:, :, -1].ravel()]))
    exterior_ids = exterior_ids[exterior_ids != 0]
    exterior = np.isin(labels, exterior_ids)
    if exterior.any():
        # The tool reaches every air cell within one tool radius plus one voxel of an
        # exterior centre, through air. A Euclidean ball, not an iterated 6-neighbour
        # dilation: that is an octahedron reaching only 2/sqrt(3) mm along a 3D diagonal,
        # so every concave wall/ceiling corner of the support envelope read as unreachable
        # (6-axis upper arm: all 84 inaccessible voxels sat 1.5-1.73 mm from a tool centre,
        # 0.5 mm from a wall). Components keep the reach from jumping through a wall.
        steps = math.ceil(ctx.params.access_mm / (2 * vox.cell)) + 1
        near = air & (ndimage.distance_transform_edt(~exterior) <= steps)
        near_labels, _ = ndimage.label(near)
        touched = np.unique(near_labels[exterior])
        reachable = np.isin(near_labels, touched[touched != 0])
    else:
        reachable = np.zeros_like(air)
    inaccessible = mask & ~reachable
    # Sealed air never becomes reachable by dilation through a thin solid wall.
    air_labels, sealed = _air_components(vox)
    in_sealed = mask & np.isin(air_labels, sealed)
    inaccessible |= in_sealed
    ctx._inaccessible = {"mask": inaccessible, "sealed": in_sealed}
    ctx._enclosed = (float(inaccessible.sum() * vox.cell**3), float(mask.sum() * vox.cell**3))
    return ctx._enclosed


def _voxel_region(ctx: CheckContext, indexes: np.ndarray, **numbers) -> dict:
    """Region for one voxel cluster: centre, model-frame bbox, volume and the faces around it."""
    vox, g = ctx.vox, ctx.geom
    corners = np.array([vox.origin + indexes.min(axis=0)*vox.cell,
                        vox.origin + (indexes.max(axis=0)+1)*vox.cell])
    # Bbox is axis-aligned in MODEL frame, not two rotated opposite corners.
    allcorners = np.array([[x,y,z] for x in corners[:,0] for y in corners[:,1] for z in corners[:,2]])
    model = g.model_points(allcorners)
    tids = np.flatnonzero(np.all(g.triangles.max(axis=1) >= corners[0]-vox.cell, axis=1) &
                          np.all(g.triangles.min(axis=1) <= corners[1]+vox.cell, axis=1))
    return ctx.region(tids, center=vox.centers(indexes).mean(axis=0), volume_mm3=len(indexes)*vox.cell**3,
                      bbox_mm=[model.min(axis=0), model.max(axis=0)], **numbers)


def _inaccessible_regions(ctx: CheckContext) -> tuple[list, int, bool]:
    """(regions, cluster count, any cluster trapped) for the support that no tool can reach.

    One region per connected cluster of inaccessible support voxels, so the row says WHERE
    the trapped support is, not only how much support the part needs. Only the ctx.limit
    most severe clusters (trapped first, then by volume) become regions."""
    state, vox = ctx._inaccessible, ctx.vox
    if state is None or not state["mask"].any():
        return [], 0, False
    labels, count = ndimage.label(state["mask"], structure=np.ones((3, 3, 3), dtype=bool))
    sizes = np.bincount(labels.ravel(), minlength=count+1)
    sealed = np.bincount(labels[state["sealed"]], minlength=count+1)

    def trapped(label: int) -> bool:
        return _trapped(float(sizes[label] * vox.cell**3), vox.cell)

    selected = sorted(range(1, count+1), key=lambda label: (not trapped(label), -sizes[label], label))
    boxes = ndimage.find_objects(labels)
    regions = []
    for label in selected[:ctx.limit]:
        bounds = boxes[label-1]
        indexes = np.argwhere(labels[bounds] == label) + np.array([s.start for s in bounds])
        regions.append(_voxel_region(ctx, indexes, classification="inaccessible_support",
                                     cause="sealed_cavity" if sealed[label] else "narrow_opening",
                                     status="fail" if trapped(label) else "warn"))
    return regions, count, bool(selected) and trapped(selected[0])


def _trapped(enclosed: float, voxel_mm: float) -> bool:
    """One connected body of enclosed support worth failing a part for: at least 8 voxels
    and 10 mm³ (a ~2 mm cube of support no tool can reach). Smaller islands are raster
    noise at a cavity mouth or a crumb that falls out; they stay a warning, and separate
    crumbs do not add up to a trapped body (6-axis turntable: 50 wall-corner crumbs of at
    most 1.125 mm³, every region a warning, summed to a 12.4 mm³ row failure)."""
    return enclosed >= max(8 * voxel_mm ** 3, 10.0)


def support(ctx: CheckContext) -> dict:
    data, p, peel = support_data(ctx), ctx.params, ctx.peel
    volume = data["volume"]
    enclosed, voxel_volume = _enclosed_support(ctx) if volume > 0 else (0., 0.)
    clusters, cluster_count, trapped = _inaccessible_regions(ctx) if enclosed > 0 else ([], 0, False)
    status = "fail" if trapped else "warn" if data["on_model_area"] > 0 or enclosed > 0 else "pass"
    tids = np.unique(peel.tri[data["needed"]])
    regions = []
    for group in ctx.groups(tids):
        ids = np.flatnonzero(data["needed"] & np.isin(peel.tri, group))
        regions.append(ctx.region(group, volume_mm3=float(np.sum(data["heights"][ids]) * peel.cell**2),
                                  z_mm=float(peel.z[ids].max()), classification="support_demand"))
    # Trapped clusters lead; demand regions only say where support is needed.
    shown, omitted = _ranked(clusters + regions, ctx.limit, lambda r: -r["volume_mm3"])
    omitted += cluster_count - len(clusters)
    fix = []
    if volume > 0:
        fix = ["reorient with optimize_print_orientation; add 45 deg chamfers or split part"]
    if trapped:
        fix.append(f"add an access opening >= {p.access_mm:g} mm for support removal")
    if data["on_model_area"] > 0:
        fix.append("avoid support landing on model surfaces (scars and harder removal)")
    return row("support", status,
               f"Support envelope {volume:.1f} mm3; {data['on_model_area']:.1f} mm2 lands on model; {enclosed:.1f} mm3 enclosed/unremovable.",
               {"support_volume_mm3": volume, "support_material_mm3": volume*p.support_density,
                "landing_on_model_mm2": data["on_model_area"], "enclosed_support_mm3": enclosed,
                "voxel_support_mm3": voxel_volume, "grid_mm": peel.cell, "voxel_mm": ctx.vox.cell if volume > 0 else None},
               {"support_density": p.support_density, "access_mm": p.access_mm}, shown, fix,
               omitted, ctx.detail)


def small_holes(ctx: CheckContext) -> dict:
    p, g = ctx.params, ctx.geom
    if all(face.surf_type == 6 for face in g.mesh.faces):
        return row("small_holes", "not_evaluated", "Raw mesh has no CAD cylinder attribution; hole sizing is not reliably evaluated.")
    regions = []
    for h in ctx.holes:
        d = 2*h["radius"]
        vertical = abs(h["axis"][2]) >= math.cos(math.radians(15))
        horizontal = abs(h["axis"][2]) <= math.sin(math.radians(15))
        status = "fail" if vertical and d < p.min_hole_mm else "warn" if vertical and d < p.small_hole_warn_mm else "pass"
        if horizontal and d > p.teardrop_hole_mm:
            # Overhang row already owns the crown warning, avoid duplicate design alarms.
            if not np.any(np.isin(ctx.peel.tri[support_data(ctx)["needed"]], h["tids"])):
                status = "warn"
        if status != "pass":
            regions.append(ctx.region(h["tids"], diameter_mm=d, axis=h["axis"],
                                      classification="vertical_hole" if vertical else "horizontal_hole", status=status))
    shown, omitted = _ranked(regions, ctx.limit, lambda r: r["diameter_mm"])
    status = "fail" if any(r["status"] == "fail" for r in regions) else "warn" if regions else "pass"
    fixes = []
    if regions:
        fixes = ["vertical holes print ~0.1-0.2 mm undersize; size +0.2 mm or drill",
                 "use teardrop/diamond profiles for large horizontal holes"]
    return row("small_holes", status, f"{len(ctx.holes)} cylindrical holes evaluated; {len(regions)} below printability thresholds.",
               {"holes": len(ctx.holes), "problem_holes": len(regions)},
               {"min_hole_mm": p.min_hole_mm, "small_hole_warn_mm": p.small_hole_warn_mm,
                "teardrop_hole_mm": p.teardrop_hole_mm}, shown, fixes, omitted, ctx.detail)


def internal_voids(ctx: CheckContext) -> dict:
    vox, p, g = ctx.vox, ctx.params, ctx.geom
    labels, sealed = _air_components(vox)
    sizes = np.bincount(labels.ravel())
    void_volume = float(sizes[sealed].sum() * vox.cell**3)
    enclosed = _enclosed_support(ctx)[0] if len(sealed) and p.process == "fdm" else 0.
    # SLA/SLS trap resin or powder in every sealed cavity. FDM fails a cavity only when the
    # support inside it cannot be removed.
    trapped_in = np.zeros(len(sizes))
    if p.process == "fdm" and ctx._inaccessible is not None:
        trapped_in = np.bincount(labels[ctx._inaccessible["mask"]], minlength=len(sizes)) * vox.cell**3

    def cavity_status(label: int) -> str:
        return "fail" if p.process in ("sla", "sls") or _trapped(float(trapped_in[label]), vox.cell) else "warn"

    # The row fails on a cavity that traps support, not on crumbs summed across the part.
    trapped = any(cavity_status(label) == "fail" for label in sealed)
    cavities = ndimage.find_objects(labels)
    selected = sorted(sealed, key=lambda label: (cavity_status(label) != "fail", -sizes[label], label))
    regions = []
    for label in selected[:ctx.limit]:
        bounds = cavities[label-1]
        indexes = np.argwhere(labels[bounds] == label) + np.array([s.start for s in bounds])
        regions.append(_voxel_region(ctx, indexes, classification="sealed_cavity", status=cavity_status(label)))
    # Voxel components are welded by position, unlike display CAD tessellation vertices.
    bodies, _ = ndimage.label(vox.solid)
    floating = []
    slices = ndimage.find_objects(bodies)
    peel, data = ctx.peel, support_data(ctx)
    for label, bounds in enumerate(slices, 1):
        if bounds is None:
            continue
        lowest = vox.origin[2] + bounds[2].start * vox.cell
        if lowest <= vox.cell + .05:
            continue
        # A modeled support path is present when a requested support column reaches
        # this component's XY footprint and its lowest material surface.
        indexes = np.argwhere(bodies[bounds] == label) + np.array([s.start for s in bounds])
        points = vox.centers(indexes)
        low = points.min(axis=0)
        high = points.max(axis=0)
        support_ids = np.flatnonzero(data["needed"])
        xy = peel.xy_for_cells(peel.cells[support_ids])
        supported = ((xy[:,0] >= low[0]-vox.cell) & (xy[:,0] <= high[0]+vox.cell) &
                     (xy[:,1] >= low[1]-vox.cell) & (xy[:,1] <= high[1]+vox.cell) &
                     (peel.z[support_ids] <= lowest+2*vox.cell)).any()
        if not supported:
            tids = np.flatnonzero(np.all(g.centroids >= low-vox.cell, axis=1) &
                                  np.all(g.centroids <= high+vox.cell, axis=1))
            floating.append(ctx.region(tids, center=points.mean(axis=0),
                                       volume_mm3=len(indexes)*vox.cell**3, z_mm=lowest,
                                       classification="floating_body", status="fail"))
    shown, omitted = _ranked(floating + regions, ctx.limit, lambda r: -r["volume_mm3"])
    omitted += len(sealed) - len(regions)
    status = ("fail" if floating or (len(sealed) and p.process in ("sla", "sls")) or trapped
              else "warn" if len(sealed) else "pass")
    fix = ["add >= 2 drain holes >= 3.5 mm to release trapped resin/powder"] if len(sealed) and p.process in ("sla", "sls") else []
    if len(sealed) and p.process == "fdm":
        fix.append("sealed cavity: make ceiling bridgeable, split part, or add a support-removal access opening")
    if floating:
        fix.append("join floating bodies to material reaching the bed or add a support path")
    return row("internal_voids", status,
               f"{len(sealed)} sealed cavities ({void_volume:.1f} mm3); {len(floating)} floating bodies without a support path.",
               {"sealed_cavities": len(sealed), "void_volume_mm3": void_volume,
                "floating_bodies": len(floating), "enclosed_support_mm3": enclosed, "voxel_mm": vox.cell},
               {"access_mm": p.access_mm, "drain_holes": 2, "drain_diameter_mm": 3.5},
               shown, fix, omitted, ctx.detail)


def bed_adhesion(ctx: CheckContext) -> dict:
    g, p = ctx.geom, ctx.params
    contact = (g.normals[:,2] < -.99) & (g.triangles[:,:,2].max(axis=1) <= p.layer_height_mm+1e-6)
    area = float(g.areas[contact].sum())
    height = float(g.bounds[1,2])
    tip = height/math.sqrt(area) if area > 0 else math.inf
    t = g.triangles
    signed = np.einsum("ij,ij->i", t[:,0], np.cross(t[:,1],t[:,2]))/6
    volume = signed.sum()
    com = np.sum((t.sum(axis=1)/4)*signed[:,None],axis=0)/volume if abs(volume)>1e-9 else g.centroids.mean(axis=0)
    inside, footprint_diag = False, 0.
    if contact.any():
        xy = np.unique(t[contact,:,:2].reshape(-1,2),axis=0)
        footprint_diag = float(np.linalg.norm(np.ptp(xy,axis=0)))
        try:
            hull = ConvexHull(xy)
            inside = bool(np.all(hull.equations[:,:2] @ com[:2] + hull.equations[:,2] <= 1e-6))
        except QhullError:
            inside = False
    warp = p.process == "fdm" and p.material.upper() in ("ABS","ASA","PC","NYLON","PA","PA6","PA12") and footprint_diag > 150
    status = "warn" if area < 50 or tip > 4 or not inside or warp else "pass"
    return row("bed_adhesion",status,
               f"First-layer contact {area:.1f} mm2; height {height:.3f} mm; tip ratio {tip:.3f}; COM {'inside' if inside else 'outside'} contact hull.",
               {"contact_area_mm2":area,"height_mm":height,"tip_ratio":tip,"com_inside_contact_hull":inside,
                "center_of_mass":g.model_points(com.reshape(1,3))[0],"footprint_diagonal_mm":footprint_diag,"warp_risk":warp},
               {"minimum_contact_area_mm2":50,"maximum_tip_ratio":4,"warp_diagonal_mm":150},
               fix=(["brim 5 mm; reorient onto a larger stable face"] if status == "warn" else []) +
               (["use enclosure and brim for warp-prone material"] if warp else []))


def build_volume(ctx: CheckContext) -> dict:
    from .orient import best_bed_yaw
    p,g = ctx.params,ctx.geom
    if p.bed_size_mm is None:
        return row("build_volume","not_applicable","No printable bed size supplied.")
    yaw,footprint,fits = best_bed_yaw(g,p)
    height = float(g.bounds[1,2])
    overflow = np.maximum(np.array([*footprint,height])-np.array(p.bed_size_mm),0)
    return row("build_volume","pass" if fits else "fail",
               f"Best yaw adjustment {yaw:.3f} deg: {footprint[0]:.3f} x {footprint[1]:.3f} x {height:.3f} mm; overflow {overflow[0]:.3f}, {overflow[1]:.3f}, {overflow[2]:.3f} mm.",
               {"fits_bed":fits,"yaw_adjustment_deg":yaw,"footprint_mm":footprint,"height_mm":height,
                "overflow_mm":overflow,"fits_at_current_yaw":bool(np.all(g.bounds[1]-g.bounds[0] <= np.array(p.bed_size_mm)+1e-6))},
               {"bed_size_mm":p.bed_size_mm},fix=[] if fits else ["split part, scale down, or choose another build orientation"])


def estimate(ctx: CheckContext, support_volume: float = 0.) -> dict:
    """Ranking estimate: finite-volume shells/infill, flow time and 2 s/layer overhead.

    Does not account for acceleration, travel, cooling, raft, or slicer-specific
    toolpath decisions. It must never be presented as measured slicer truth.
    """
    g,p = ctx.geom,ctx.params
    t=g.triangles
    volume = abs(float(np.einsum("ij,ij->i",t[:,0],np.cross(t[:,1],t[:,2])).sum()/6))
    side = float(g.areas[np.abs(g.normals[:,2]) < .5].sum())
    horizontal = float(g.areas[np.abs(g.normals[:,2]) >= .5].sum())
    shell = min(volume, side*p.walls*p.line_width_mm + horizontal*p.top_bottom_mm)
    material = shell+p.infill_fraction*max(volume-shell,0)
    support_mat = support_volume*p.support_density
    layers = math.ceil(float(g.bounds[1,2])/p.layer_height_mm)
    total = material+support_mat
    return {"part_volume_mm3":volume,"support_volume_mm3":support_volume,
            "material_g":total*p.filament_density_g_cm3/1000,
            "support_g":support_mat*p.filament_density_g_cm3/1000,
            "time_h":(total/p.volumetric_speed_mm3_s+layers*2)/3600,"layers":layers,
            "support_share":support_mat/total if total>0 else 0.,
            "method":"deterministic shell/infill/flow ranking estimate, not slicer output"}
