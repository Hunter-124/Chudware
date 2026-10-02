"""Deterministic orientation ranking, not a replacement for slicer toolpaths.

All dimensions are millimetres. Support is a projected-area/landing-height model:
a bounded coarse signed depth peel removes solid-overlap crossings and estimates
nearer landing surfaces; unsampled triangles conservatively land on the bed.
Bridging, support styles, toolpath speed and anisotropic material strength remain
slicer/engineering decisions. Scores are normalized within the candidate pool;
smaller is better. No check/analyze module or engine service is called here.
"""
from __future__ import annotations

import math
import numpy as np
from scipy.spatial import ConvexHull, QhullError
from numpy.typing import ArrayLike
from .mesh import PrintMesh

from .geometry import BuildGeometry, depth_peel
from .params import PrintParams, frame_for_build_dir


_BED_TOL = 0.05


def _xy_hull(points):
    xy = np.unique(np.asarray(points)[:, :2], axis=0)
    if len(xy) < 3:
        return xy
    try:
        return xy[ConvexHull(xy).vertices]
    except QhullError:
        # A projected line has only two relevant support points.
        axis = int(np.argmax(np.ptp(xy, axis=0)))
        return xy[[np.argmin(xy[:, axis]), np.argmax(xy[:, axis])]]


def _extent_at(hull, angles):
    angles = np.asarray(angles)
    c, s = np.cos(angles), np.sin(angles)
    x = hull[:, 0, None] * c - hull[:, 1, None] * s
    y = hull[:, 0, None] * s + hull[:, 1, None] * c
    return np.column_stack((np.ptp(x, axis=0), np.ptp(y, axis=0)))


def _roots(a, b, limit, lo, hi):
    radius = math.hypot(a, b)
    if radius < limit or radius < 1e-12:
        return []
    phase = math.atan2(b, a)
    delta = math.acos(np.clip(limit / radius, -1., 1.))
    roots = []
    for base in (phase - delta, phase + delta):
        for turn in (-1, 0, 1):
            angle = base + turn * 2 * math.pi
            if lo - 1e-12 <= angle <= hi + 1e-12:
                roots.append(float(np.clip(angle, lo, hi)))
    return roots


def best_bed_yaw(geom: BuildGeometry, params: PrintParams) -> tuple[float, list[float], bool]:
    """Return additional yaw, exact XY footprint, and XYZ bed-fit flag.

    Hull-edge yaws minimize bounding-box area, but that does not imply bed fit:
    a 280 mm thin bar fits diagonally on a 220 mm square. Between support-vertex
    switching angles, footprint dimensions are sinusoids. Include their exact
    bed-boundary roots and feasible interval midpoints, as well as 0/45/135 deg.
    This searches feasible yaw continuously, not just a coarse angular grid.
    With no bed specified, fits is True and the minimum-area yaw is returned.
    """
    hull = _xy_hull(geom.vertices)
    edges = np.roll(hull, -1, axis=0) - hull
    nonzero = np.linalg.norm(edges, axis=1) > 1e-12
    edge_angles = np.arctan2(edges[nonzero, 1], edges[nonzero, 0])
    switches = np.unique(np.r_[0., np.pi, (-edge_angles) % np.pi,
                                (np.pi / 2 - edge_angles) % np.pi])
    candidates = list(switches[:-1]) + [np.pi / 4, 3 * np.pi / 4]
    bed = params.bed_size_mm
    if bed is not None:
        for lo, hi in zip(switches[:-1], switches[1:]):
            mid = (lo + hi) / 2
            c, s = math.cos(mid), math.sin(mid)
            x = hull[:, 0] * c - hull[:, 1] * s
            y = hull[:, 0] * s + hull[:, 1] * c
            dx = hull[np.argmax(x)] - hull[np.argmin(x)]
            dy = hull[np.argmax(y)] - hull[np.argmin(y)]
            roots = _roots(dx[0], -dx[1], bed[0], lo, hi)
            roots += _roots(dy[1], dy[0], bed[1], lo, hi)
            cuts = sorted([lo, hi, *roots])
            candidates.extend(roots)
            candidates.extend((a + b) / 2 for a, b in zip(cuts[:-1], cuts[1:]))
    angles = np.unique(np.round(np.asarray(candidates) % np.pi, 13))
    extents = _extent_at(hull, angles)
    area = extents[:, 0] * extents[:, 1]
    if bed is None:
        fits = np.ones(len(angles), dtype=bool)
        ratio = np.zeros(len(angles))
    else:
        fits = ((extents <= np.asarray(bed[:2]) + 1e-7).all(axis=1)
                & (np.ptp(geom.vertices[:, 2]) <= bed[2] + 1e-7))
        ratio = np.max(extents / np.asarray(bed[:2]), axis=1)
    # Round tie keys only, leaving dimensions and yaw exact for downstream use.
    if fits.any():
        order = np.lexsort((angles, np.round(ratio, 10), np.round(area, 8), ~fits))
    else:
        order = np.lexsort((angles, np.round(area, 8), np.round(ratio, 10)))
    i = int(order[0])
    return float(np.degrees(angles[i])), extents[i].tolist(), bool(fits[i])


def _support_metrics(geom, params):
    nz = geom.normals[:, 2]
    bed = (geom.triangles[:, :, 2].max(axis=1) <= _BED_TOL) & (nz < -0.999)
    downward = (nz < -math.sin(math.radians(params.overhang_max_deg)) - 1e-10) & ~bed
    ids = np.flatnonzero(downward)
    heights = geom.centroids[ids, 2].copy()
    active = np.ones(len(ids), dtype=bool)
    if len(ids):
        footprint = np.ptp(geom.vertices[:, :2], axis=0)
        # Small enough for every candidate to remain cheap on a 50k-triangle mesh.
        pitch = max(1., math.sqrt(float(np.prod(footprint)) / 4096.))
        peel = depth_peel(geom, cell=pitch, max_cells=60000)
        if len(peel.cells):
            starts = np.r_[True, peel.cells[1:] != peel.cells[:-1]]
            base = np.maximum.accumulate(np.where(starts, np.arange(len(starts)), 0))
            # Public signs deliberately omit coincident-sheet multiplicity.
            # Geometry's union transitions retain it and exclude buried tops.
            enters = (peel.sign < 0) & peel._active_surface
            leaves = (peel.sign > 0) & peel._active_surface
            landing_indices = np.maximum.accumulate(np.where(leaves, np.arange(len(leaves)), -1))
            landing = np.zeros(len(peel.z))
            has_landing = landing_indices >= base
            landing[has_landing] = peel.z[landing_indices[has_landing]]
            selected = downward[peel.tri] & (peel.sign < 0)
            sampled_tri = peel.tri[selected]
            count = np.bincount(sampled_tri, minlength=len(nz))[ids]
            gap = np.where(enters, np.maximum(peel.z - landing, 0.), 0.)
            total_gap = np.bincount(sampled_tri, weights=gap[selected], minlength=len(nz))[ids]
            sampled = count > 0
            heights[sampled] = total_gap[sampled] / count[sampled]
            active[sampled] = total_gap[sampled] > _BED_TOL
    support_faces = np.zeros(len(nz), dtype=bool)
    support_faces[ids[active]] = True
    projected = geom.areas[ids] * -nz[ids]
    support_volume = float(np.dot(projected, heights))
    return support_volume, float(geom.areas[ids[active]].sum()), float(geom.areas[bed].sum()), bed, support_faces


def _metrics(geom, params):
    support, overhang, contact, bed, supported = _support_metrics(geom, params)
    dims = np.ptp(geom.vertices, axis=0)
    t = geom.triangles
    volume = abs(float(np.einsum('ij,ij->i', t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6))
    surface = float(geom.areas.sum())
    # Bounding shell approximation, capped at solid volume; same deterministic
    # extrusion model across candidates, not slicer volume or a time guarantee.
    shell = min(volume, surface * params.walls * params.line_width_mm)
    solid = shell + (volume - shell) * params.infill_fraction
    support_extrusion = support * params.support_density
    layers = int(math.ceil(dims[2] / params.layer_height_mm))
    time_h = ((solid + support_extrusion) / params.volumetric_speed_mm3_s + layers * 2.) / 3600.
    fits = params.bed_size_mm is None or bool((dims <= np.asarray(params.bed_size_mm) + 1e-7).all())
    metrics = {
        'support_volume_mm3': support, 'overhang_area_mm2': overhang,
        'contact_area_mm2': contact, 'height_mm': float(dims[2]),
        'footprint_mm': dims[:2].tolist(), 'part_volume_mm3': volume, 'surface_mm2': surface,
        'est_time_h': time_h,
        'est_material_g': (solid + support_extrusion) * params.filament_density_g_cm3 / 1000.,
        'tip_ratio': float(dims[2] / max(math.sqrt(contact), params.line_width_mm)),
        'fits_bed': fits, 'preserved_faces_on_support': 0, 'preserved_faces_on_bed': 0,
        'preserved_faces_affected': 0,
    }
    return metrics, bed, supported


def _rounded_metrics(metrics):
    return {key: ([round(float(x), 3) for x in value] if isinstance(value, list)
                  and key == 'footprint_mm' else round(value, 3) if isinstance(value, float) else value)
            for key, value in metrics.items()}


def evaluate_orientation(mesh: PrintMesh, params: PrintParams, build_dir: ArrayLike, yaw_deg: float = 0.0) -> dict:
    """Evaluate the requested frame exactly; no hidden yaw optimization."""
    geom = BuildGeometry(mesh, build_dir, yaw_deg)
    metrics, _, _ = _metrics(geom, params)
    direction = np.asarray(build_dir, dtype=float)
    direction = direction / np.linalg.norm(direction)
    return {'build_dir': np.round(direction, 8).tolist(), 'yaw_deg': round(float(yaw_deg), 3),
            'rotation': geom.rotation.tolist(), 'metrics': _rounded_metrics(metrics),
            'why': 'Projected-area support and extrusion/time estimates; validate with the stock slicer.'}


def _candidates(mesh):
    directions = [(axis * sign, 'axis') for axis in np.eye(3) for sign in (1., -1.)]
    model = BuildGeometry(mesh)
    try:
        hull = ConvexHull(mesh.vertices)
        triangles = mesh.vertices[hull.simplices]
        areas = np.linalg.norm(np.cross(triangles[:, 1] - triangles[:, 0],
                                        triangles[:, 2] - triangles[:, 0]), axis=1) / 2
        order = np.argsort(-areas, kind='stable')
        hull_directions = []
        for i in order:
            direction = -hull.equations[i, :3]
            if all(float(direction @ old) < math.cos(math.radians(2)) for old in hull_directions):
                hull_directions.append(direction)
                if len(hull_directions) == 24:
                    break
        directions.extend((direction, 'hull_face') for direction in hull_directions)
    except QhullError:
        pass
    planar = []
    for face_id, face in enumerate(mesh.faces):
        if face.surf_type != 0:
            continue
        ids = np.flatnonzero(mesh.tri_face == face_id)
        if not len(ids):
            continue
        area = float(model.areas[ids].sum())
        normal = (model.normals[ids] * model.areas[ids, None]).sum(axis=0)
        length = np.linalg.norm(normal)
        if length > 1e-12:
            planar.append((area, face_id, -normal / length))
    planar.sort(key=lambda row: (-row[0], row[1]))
    directions.extend((row[2], 'cad_face') for row in planar[:12])
    result = []
    for direction, source in directions:
        direction = direction / np.linalg.norm(direction)
        if all(float(direction @ old[0]) < math.cos(math.radians(2)) for old in result):
            result.append((direction, source))
    return result


def _preserved_sids(mesh, selected, requested):
    faces = np.unique(mesh.tri_face[selected])
    return {mesh.faces[i].sid for i in faces if i >= 0 and mesh.faces[i].sid in requested}


# Per-goal weights for (support, time, overhang, stability, strength). Every term is an
# absolute, dimensionless measure of the candidate itself — never min-max scaled over the
# candidate set, where one absurd pose (a part balanced on an edge, tip ratio 120) squashes
# the real differences between sensible ones to nothing.
_GOAL_WEIGHTS = {
    'balanced': (1.0, 0.6, 1.0, 0.5, 0.5),
    'min_support': (3.0, 0.1, 0.5, 0.3, 0.0),
    'min_time': (0.3, 2.0, 0.2, 0.3, 0.0),
    'strength': (0.5, 0.2, 0.3, 0.3, 2.0),
}


def _stability(metrics) -> float:
    """0 (flat, squat) .. 2 (resting on an edge and tall): contact below 100 mm² and a
    height/sqrt(contact) above 3 both cost, each saturating at 1."""
    contact = max(0.0, 1.0 - metrics['contact_area_mm2'] / 100.0)
    tip = min(1.0, max(0.0, (metrics['tip_ratio'] - 3.0) / 10.0))
    return contact + tip


def _scores(rows, goal, has_strength) -> np.ndarray:
    w_support, w_time, w_overhang, w_stability, w_strength = _GOAL_WEIGHTS[goal]
    fastest = min(row['metrics']['est_time_h'] for row in rows) or 1e-9
    scores = []
    for row in rows:
        m = row['metrics']
        support = m['support_volume_mm3'] / max(m['part_volume_mm3'], 1e-9)
        time = m['est_time_h'] / fastest - 1.0
        overhang = m['overhang_area_mm2'] / max(m['surface_mm2'], 1e-9)
        strength = m['strength_out_of_plane'] if has_strength else 0.0
        affected = m['preserved_faces_affected']
        scores.append(w_support * support + w_time * time + w_overhang * overhang
                      + w_stability * _stability(m) + w_strength * strength
                      + (2.0 + 0.2 * affected if affected else 0.0))
    return np.asarray(scores, dtype=float)


def optimize_orientation(mesh: PrintMesh, params: PrintParams, *, top_k: int = 5,
                         preserve_faces: tuple[str, ...] | list[str] = (),
                         strength_dir: ArrayLike | None = None, goal: str = 'balanced') -> list[dict]:
    """Rank deduplicated axis, hull and attributed planar-face build directions.

    Preserved SIDs are penalized once if on the bed or under support. Strength
    favors a specified model-space load direction lying in the layer plane,
    not a physical stress prediction. Non-fitting candidates always sort last.
    """
    if goal not in ('balanced', 'min_support', 'min_time', 'strength'):
        raise ValueError('goal must be balanced, min_support, min_time, or strength')
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise ValueError('top_k must be a positive integer')
    strength = None
    if strength_dir is not None:
        strength = np.asarray(strength_dir, dtype=float)
        if strength.shape != (3,) or not np.isfinite(strength).all() or np.linalg.norm(strength) < 1e-12:
            raise ValueError('strength_dir must be a finite nonzero 3-vector')
        strength = strength / np.linalg.norm(strength)
    if goal == 'strength' and strength is None:
        raise ValueError('strength goal requires strength_dir')
    requested = set(preserve_faces)
    rows = []
    for direction, source in _candidates(mesh):
        geom = BuildGeometry(mesh, direction)
        yaw, _, _ = best_bed_yaw(geom, params)
        # A boundary solution may cease fitting when the public yaw is rounded.
        # Select the nearest representable yaw that still fits, and measure the
        # footprint for that reported frame rather than for the unrounded root.
        yaws = np.array([round(yaw, 3), round(yaw, 3) - .001, round(yaw, 3) + .001])
        footprints = _extent_at(geom.vertices[:, :2], np.radians(yaws))
        if params.bed_size_mm is None:
            fit_flags = np.ones(3, dtype=bool)
        else:
            bed_dims = np.asarray(params.bed_size_mm)
            fit_flags = ((footprints <= bed_dims[:2] + 1e-7).all(axis=1)
                         & (np.ptp(geom.vertices[:, 2]) <= bed_dims[2] + 1e-7))
        chosen = int(np.flatnonzero(fit_flags)[0]) if fit_flags.any() else 0
        yaw, footprint, fits = float(yaws[chosen]), footprints[chosen].tolist(), bool(fit_flags[chosen])
        metrics, bed, support = _metrics(geom, params)
        metrics['footprint_mm'] = footprint
        metrics['fits_bed'] = fits
        bed_sids = _preserved_sids(mesh, bed, requested)
        support_sids = _preserved_sids(mesh, support, requested)
        metrics['preserved_faces_on_bed'] = len(bed_sids)
        metrics['preserved_faces_on_support'] = len(support_sids)
        metrics['preserved_faces_affected'] = len(bed_sids | support_sids)
        metrics['preserved_face_sids'] = sorted(bed_sids | support_sids)
        metrics['strength_out_of_plane'] = abs(float(direction @ strength)) if strength is not None else 0.
        rows.append({'build_dir': direction.tolist(), 'yaw_deg': yaw,
                     'rotation': frame_for_build_dir(direction, yaw).tolist(),
                     'source': source, 'metrics': metrics})
    scores = _scores(rows, goal, strength is not None)
    for i, row in enumerate(rows):
        row['score'] = float(scores[i] + (10. if not row['metrics']['fits_bed'] else 0.))
    rows.sort(key=lambda row: (row['score'], row['metrics']['support_volume_mm3'],
                              row['metrics']['tip_ratio'], row['metrics']['height_mm'],
                              tuple(row['build_dir']), row['yaw_deg']))
    for rank, row in enumerate(rows[:top_k], 1):
        row['rank'] = rank
        metrics = row['metrics']
        row['why'] = (f"{goal}: support {metrics['support_volume_mm3']:.3f} mm³, "
                      f"bed contact {metrics['contact_area_mm2']:.3f} mm², "
                      f"height {metrics['height_mm']:.3f} mm, estimated time {metrics['est_time_h']:.3f} h; "
                      f"bed {'fits' if metrics['fits_bed'] else 'does not fit'}, "
                      f"{metrics['preserved_faces_affected']} preserved face(s) on bed/support, "
                      f"load out-of-plane {metrics['strength_out_of_plane']:.3f}. "
                      'Approximate ranking; verify slicer supports and toolpaths.')
        row['metrics'] = _rounded_metrics(metrics)
        row['build_dir'] = np.round(row['build_dir'], 8).tolist()
        row['yaw_deg'] = round(row['yaw_deg'], 3)
        row['score'] = round(row['score'], 6)
    return rows[:top_k]
