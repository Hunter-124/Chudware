"""numpy ports of the generator's numba kernels (Utils/geom_utils_numba.py, numba_vec.py,
geom_utils.points_inside_rect). Loops over points/triangles are vectorised; the branch logic of
every kernel is the original's."""
from __future__ import annotations

import math

import numpy as np

# The original accepted any t > 0. A ray cast from a face's own plane re-hits that face at
# t = 0 +- rounding, so rounding noise alone vetoed bounds and blind depths; 1e-6 mm is far
# below min_len (2 mm) and removes only that self-hit.
RAY_T_MIN = 1e-6
_CHUNK_ELEMS = 1 << 21        # points x triangles per vectorised block


def unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def points_inside_rect(pnt0, pnt1, pnt2, pnt3, resolution=0.5) -> np.ndarray:
    pnt0, pnt1, pnt2 = (np.asarray(p, dtype=np.float64) for p in (pnt0, pnt1, pnt2))
    dir_w = pnt2 - pnt1
    dir_h = pnt0 - pnt1
    width = np.linalg.norm(dir_w)
    height = np.linalg.norm(dir_h)
    num_w = max(int(width / resolution), 1)
    num_h = max(int(height / resolution), 1)
    delta_w = width / num_w
    delta_h = height / num_h
    dir_w = dir_w / width
    dir_h = dir_h / height
    idx = np.array(np.meshgrid(np.arange(0, num_w + 1), np.arange(0, num_h + 1))).T.reshape(-1, 2)
    return (pnt1 + dir_w * delta_w * idx[:, 0].reshape(-1, 1)
            + dir_h * delta_h * idx[:, 1].reshape(-1, 1))


def _plane_basis(direction: np.ndarray) -> tuple:
    d = unit(direction)
    a = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = unit(np.cross(d, a))
    return e1, np.cross(d, e1)


def ray_triangle_min_t(origins, direction, tris) -> np.ndarray:
    """Per origin, the smallest Moller-Trumbore hit distance t > RAY_T_MIN of a ray along
    `direction` over the triangle set (T, 3, 3); -inf where nothing is hit. Same arithmetic as
    ray_triangle_intersect / ray_triangle_set_intersect; triangles whose projection on the
    plane normal to the ray cannot contain any origin are culled first (exact)."""
    origins = np.atleast_2d(np.asarray(origins, dtype=np.float64))
    out = np.full(len(origins), -np.inf)
    tris = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
    if not len(tris) or not len(origins):
        return out
    d = np.asarray(direction, dtype=np.float64)
    e1, e2 = _plane_basis(d)
    tri_uv = np.stack([tris @ e1, tris @ e2], axis=-1)            # (T, 3, 2)
    org_uv = np.stack([origins @ e1, origins @ e2], axis=-1)     # (P, 2)
    lo, hi = org_uv.min(axis=0) - 1e-6, org_uv.max(axis=0) + 1e-6
    keep = np.all((tri_uv.max(axis=1) >= lo) & (tri_uv.min(axis=1) <= hi), axis=1)
    tris = tris[keep]
    if not len(tris):
        return out
    v0 = tris[:, 0]
    v0v1 = tris[:, 1] - v0
    v0v2 = tris[:, 2] - v0
    pvec = np.cross(d, v0v2)
    det = np.einsum("ij,ij->i", v0v1, pvec)
    ok = np.abs(det) >= 0.000001
    v0, v0v1, v0v2, pvec, det = v0[ok], v0v1[ok], v0v2[ok], pvec[ok], det[ok]
    if not len(det):
        return out
    inv_det = 1.0 / det
    step = max(1, _CHUNK_ELEMS // len(det))
    for s in range(0, len(origins), step):
        o = origins[s:s + step]
        tvec = o[:, None, :] - v0[None, :, :]
        u = np.einsum("ptk,tk->pt", tvec, pvec) * inv_det
        qvec = np.cross(tvec, v0v1[None, :, :])
        v = (qvec @ d) * inv_det
        t = np.einsum("ptk,tk->pt", qvec, v0v2) * inv_det
        hit = (u >= 0) & (u <= 1) & (v >= 0) & (u + v <= 1) & (t > RAY_T_MIN)
        t = np.where(hit, t, np.inf)
        best = t.min(axis=1)
        out[s:s + step] = np.where(np.isfinite(best), best, -np.inf)
    return out


def ray_segment_set_intersect(ray_pnt, ray_dir, segs) -> np.ndarray:
    """ray_segment_intersect over every segment (S, 2, 3); returns the distances >= 0."""
    segs = np.asarray(segs, dtype=np.float64).reshape(-1, 2, 3)
    if not len(segs):
        return np.zeros(0)
    thres = 0.000001
    p = np.asarray(ray_pnt, dtype=np.float64)
    r = unit(ray_dir)
    pnt1, pnt2 = segs[:, 0], segs[:, 1]
    seg_dir = pnt2 - pnt1
    vec1 = pnt1 - p
    vec2 = pnt2 - p
    normal = np.cross(seg_dir, r)
    nlen = np.linalg.norm(normal, axis=1)
    on_seg = np.linalg.norm(np.cross(vec1, vec2), axis=1) < thres
    d12 = np.einsum("ij,ij->i", vec1, vec2)
    dist1 = vec1 @ r
    dist2 = vec2 @ r
    res = np.full(len(segs), -np.inf)
    decided = np.zeros(len(segs), dtype=bool)
    # origin on the segment
    m = on_seg & (d12 < thres)
    res[m] = 0.0
    decided |= m
    m = on_seg & ~decided & (nlen < thres) & (dist1 > thres) & (dist2 > thres)
    res[m] = np.where(dist1[m] < dist2[m], np.linalg.norm(vec1[m], axis=1),
                      np.linalg.norm(vec2[m], axis=1))
    decided |= m
    decided |= on_seg & (nlen >= thres)                 # -> -inf
    # general position
    live = ~decided & (nlen >= thres)
    live &= ~((dist1 < 0) & (dist2 < 0))
    side = np.einsum("ij,ij->i", np.cross(vec1, r), np.cross(vec2, r))
    live &= ~(side > 0)
    if live.any():
        seg_normal = np.cross(normal[live], seg_dir[live])
        seg_normal /= np.linalg.norm(seg_normal, axis=1)[:, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            dist = np.einsum("ij,ij->i", vec1[live], seg_normal) / (seg_normal @ r)
        res[live] = np.where(np.isnan(dist), -np.inf, dist)
    return res[res >= 0.0]


def _edge_dists(pnts: np.ndarray, verts: np.ndarray, normal) -> np.ndarray:
    """dist_pnt_line of every point to every consecutive vertex pair: (P, len(verts) - 1)."""
    line = verts[1:] - verts[:-1]
    perp = np.cross(np.asarray(normal, dtype=np.float64), line)
    perp /= np.linalg.norm(perp, axis=1)[:, None]
    q = pnts[:, None, :] - verts[None, :-1, :]
    return np.einsum("pek,ek->pe", q, perp)


def point_in_polygon(pnt, verts, normal) -> bool:
    d = _edge_dists(np.asarray(pnt, dtype=np.float64)[None, :], np.asarray(verts, np.float64), normal)
    return not bool(np.any(d <= 0.000001))


def points_in_polygon(bnd_pnts, verts) -> np.ndarray:
    verts = np.asarray(verts, dtype=np.float64)
    normal = np.cross(verts[1] - verts[0], verts[2] - verts[1])
    closed = np.vstack([verts, verts[:1]])
    pnts = np.asarray(bnd_pnts, dtype=np.float64)
    if not len(pnts):
        return pnts
    inside = ~np.any(_edge_dists(pnts, closed, normal) <= 0.000001, axis=1)
    return pnts[inside]


def search_rect_inside_bound_1(verts, vec1, vec2, bnd_pnts) -> np.ndarray:
    in_pnts = points_in_polygon(bnd_pnts, verts)
    if in_pnts.shape[0] == 0:
        return verts
    normal = np.cross(vec2, verts[1] - verts[2])
    perp_dir = unit(np.cross(normal, verts[2] - verts[1]))
    norm1 = float(np.dot(vec1, perp_dir))
    norm2 = float(np.dot(vec2, perp_dir))
    dist = min(float(((in_pnts - verts[1]) @ perp_dir).min()), norm1, norm2)
    verts[0] = verts[1] + dist * vec1 / norm1
    verts[3] = verts[2] + dist * vec2 / norm2
    return verts


def search_rect_inside_bound_2(verts, vec0, vec2, bnd_pnts) -> np.ndarray:
    in_pnts = points_in_polygon(bnd_pnts, verts)
    if in_pnts.shape[0] == 0:
        return verts
    vec0_len = float(np.linalg.norm(vec0))
    vec0 = vec0 / vec0_len
    vec2_len = float(np.linalg.norm(vec2))
    vec2 = vec2 / vec2_len
    len2 = min(float(((in_pnts - verts[1]) @ vec2).min()), vec2_len)
    len0 = min(float(((in_pnts - verts[1]) @ vec0).min()), vec0_len)
    vec0 = len0 * vec0
    vec2 = len2 * vec2
    verts[0] = vec0 + verts[1]
    verts[2] = vec2 + verts[1]
    verts[3] = vec0 + vec2 + verts[1]
    if np.isnan(verts).any():
        raise ValueError("search_rect_inside_bound_2: vertex is not a number")
    return verts


def search_rect_inside_bound_3(verts, bnd_pnts) -> np.ndarray:
    vec1 = verts[0] - verts[1]
    vec2 = verts[3] - verts[2]
    in_pnts = points_in_polygon(bnd_pnts, verts)
    if in_pnts.shape[0] == 0:
        return verts
    normal = np.cross(vec2, verts[1] - verts[2])
    perp_dir = unit(np.cross(normal, verts[2] - verts[1]))
    norm1 = float(np.dot(vec1, perp_dir))
    norm2 = float(np.dot(vec2, perp_dir))
    dist = min(float(((in_pnts - verts[1]) @ perp_dir).min()), norm1, norm2)
    verts[0] = verts[1] + dist * vec1 / norm1
    verts[3] = verts[2] + dist * vec2 / norm2
    return verts


def outer_radius_triangle(pt1, pt2, pt3) -> float:
    a = float(np.linalg.norm(pt2 - pt1))
    b = float(np.linalg.norm(pt3 - pt2))
    c = float(np.linalg.norm(pt1 - pt3))
    p = (a + b + c) / 2
    area2 = p * (p - a) * (p - b) * (p - c)
    # numba (fastmath) returned inf for a degenerate triangle; keep that instead of raising
    return a * b * c / (4 * math.sqrt(area2)) if area2 > 0 else math.inf
