"""Mesh a Gaussian splat by fusing full-resolution median depth from its training views.

Brush's undistorted training dataset and its exported Gaussians use the same COLMAP
world gauge (see gaussian_pipeline._view_camera). No axis swap or scale is applied here.
The caller must supply the *matching* training model, not a model from an independently
imported splat or a different SfM solve.

1. Render.  Every selected registered view is rendered from all Gaussians inside the
   subject region (not just a pruned subset: removing splats that the optimizer used to
   form a surface lets the depth fall through to whatever lies behind them).  Each
   pixel's depth is where transmittance crosses one half (the median depth), using the
   ray-conditional depth of every Gaussian footprint rather than its center depth.
   Optical depth -ln(1 - alpha) is additive, so the crossing is located with two levels
   of per-pixel optical-depth histograms instead of a per-pixel sort; the result does not
   depend on splat order.  Views render in parallel worker processes.
2. Classify.  A pixel belongs to the subject when its median-depth surface lies inside
   the subject bounds and, unless disabled, above the plane the subject stands on
   (fitted to the rendered surfaces, so a table and a printed mat go with a tight band).
   Steep views, depth jumps and silhouettes are not fused, nor are surfaces that fewer
   than two nearby views reproduce (floaters, view-dependent splats).
3. Fuse.  Subject depth is integrated into a TSDF (Open3D VoxelBlockGrid, on CUDA when
   available) with the photographs' colors.  The voxel follows the views' pixel
   footprint unless the caller sets it.
4. Carve.  A vertex that registered views see through (their median surface lies
   clearly behind it, or they see nothing there) is free space and is removed; pieces
   far from the main component or lying flat on the support plane are dropped.

Depth is not a measurement: 3DGS can contain floaters, view-dependent appearance placed
behind a surface, and unobserved geometry.  The result stays approximate.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

MAX_GAUSSIANS = 8_000_000
MAX_VIEWS = 400
MAX_REGISTERED_VIEWS = 4096
DEFAULT_VIEWS = 120
MAX_RENDER_SIZE = 2048
DEFAULT_RENDER_SIZE = 960
TIME_BUDGET_SECONDS = 1500.0
MAX_TSDF_BLOCKS = 600_000          # 8^3-voxel blocks; about 10 KB each with color.
_K_COARSE, _K_FINE = 32, 64        # Two histogram levels: 2048 depth cells per view.
_STRIP_ROWS = 16                   # Image rows rendered together; keeps histograms ~4-8 MB.
_FRAGMENT_CHUNK = 1_500_000        # Fragments materialized at once (arrays stay below 32 MB).
_WORKER_BYTES = 0.6e9              # Peak memory of one render worker at the default size.
_LN2 = math.log(2.0)
_MIN_ALPHA = 1.0 / 255.0
_TRUNC_VOXELS = 4.0
_MAX_VIEW_ANGLE = 80.0             # Degrees between view ray and surface normal fused at most.


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("Gaussian TSDF meshing exceeded its time budget; no mesh was published")


def _rotation_matrices(quaternions):
    """Convert normalized wxyz quaternions to right-handed rotation matrices."""
    q = np.asarray(quaternions, dtype=np.float64)
    norms = np.linalg.norm(q, axis=-1, keepdims=True)
    if np.any(~np.isfinite(norms)) or np.any(norms < 1e-15):
        raise ValueError("invalid quaternion in Gaussian or COLMAP camera")
    w, x, y, z = np.moveaxis(q / norms, -1, 0)
    return np.stack((1 - 2*(y*y + z*z), 2*(x*y - w*z), 2*(x*z + w*y),
                     2*(x*y + w*z), 1 - 2*(x*x + z*z), 2*(y*z - w*x),
                     2*(x*z - w*y), 2*(y*z + w*x), 1 - 2*(x*x + y*y)),
                    axis=-1).reshape(q.shape[:-1] + (3, 3))


def _camera(image, camera, render_size):
    """Return pinhole calibration at the render size and COLMAP world-to-camera extrinsics."""
    p = np.asarray(camera.params, dtype=np.float64)
    if camera.model == "SIMPLE_PINHOLE" and p.size == 3:
        fx, cx, cy = p
        fy = fx
    elif camera.model == "PINHOLE" and p.size == 4:
        fx, fy, cx, cy = p
    else:
        raise ValueError("TSDF requires the matching undistorted COLMAP PINHOLE or SIMPLE_PINHOLE model")
    if (camera.width <= 0 or camera.height <= 0 or not np.isfinite(p).all()
            or min(fx, fy) <= 0):
        raise ValueError("invalid COLMAP camera calibration")
    factor = min(1.0, render_size / max(camera.width, camera.height))
    width = max(1, int(round(camera.width * factor)))
    height = max(1, int(round(camera.height * factor)))
    sx, sy = width / camera.width, height / camera.height
    rotation = _rotation_matrices(image.qvec)
    translation = np.asarray(image.tvec, dtype=np.float64)
    if translation.shape != (3,) or not np.isfinite(translation).all():
        raise ValueError("invalid COLMAP camera translation")
    return {"id": int(image.id), "name": str(image.name), "width": width, "height": height,
            "fx": fx*sx, "fy": fy*sy, "cx": cx*sx, "cy": cy*sy,
            "R": rotation, "t": translation}


def _select_views(cameras, center, limit):
    """Greedy coverage of viewing direction and distance, not consecutive video frames."""
    if len(cameras) <= limit:
        return cameras
    eyes = np.stack([-c["R"].T @ c["t"] for c in cameras])
    offsets = eyes - center
    distance = np.linalg.norm(offsets, axis=1)
    directions = np.stack([-c["R"][2] for c in cameras])
    # Distance enters as a fourth coordinate so close-ups and overviews both survive.
    log_distance = np.log(np.maximum(distance, 1e-12))
    spread = float(np.std(log_distance)) or 1.0
    features = np.column_stack((directions, 0.5 * (log_distance - log_distance.mean()) / spread))
    chosen = [int(np.argmin(np.abs(log_distance - np.median(log_distance))))]
    nearest = np.linalg.norm(features - features[chosen[0]], axis=1)
    for _ in range(1, limit):
        index = int(np.argmax(nearest))
        chosen.append(index)
        nearest = np.minimum(nearest, np.linalg.norm(features - features[index], axis=1))
    return [cameras[i] for i in sorted(chosen)]


def _covariances(scales, rotations):
    """Packed world covariances xx, xy, xz, yy, yz, zz as float32."""
    basis = _rotation_matrices(rotations) * np.asarray(scales, dtype=np.float64)[:, None, :]
    full = basis @ np.swapaxes(basis, 1, 2)
    return np.stack((full[:, 0, 0], full[:, 0, 1], full[:, 0, 2],
                     full[:, 1, 1], full[:, 1, 2], full[:, 2, 2]), axis=1).astype(np.float32)


def _project(means, cov6, opacity, camera):
    """Screen-space footprint, conic, and conditional-depth slope of each visible Gaussian."""
    R, t = camera["R"], camera["t"]
    fx, fy, cx, cy = (camera[k] for k in ("fx", "fy", "cx", "cy"))
    width, height = camera["width"], camera["height"]
    xyz = means.astype(np.float64) @ R.T + t
    z = xyz[:, 2]
    front = z > 1e-9 * max(1.0, float(np.max(np.abs(z))) if len(z) else 1.0)
    if not front.any():
        return None
    xyz, z, o = xyz[front], z[front], opacity[front].astype(np.float64)
    c = cov6[front].astype(np.float64)
    full = np.empty((len(z), 3, 3))
    full[:, 0, 0], full[:, 0, 1], full[:, 0, 2] = c[:, 0], c[:, 1], c[:, 2]
    full[:, 1, 0], full[:, 1, 1], full[:, 1, 2] = c[:, 1], c[:, 3], c[:, 4]
    full[:, 2, 0], full[:, 2, 1], full[:, 2, 2] = c[:, 2], c[:, 4], c[:, 5]
    cam_cov = np.einsum("ij,njk,lk->nil", R, full, R, optimize=True)
    x, y = xyz[:, 0], xyz[:, 1]
    u, v = fx*x/z + cx, fy*y/z + cy
    # Match Brush's expanded frustum clamp in the projection Jacobian. Without it,
    # off-screen, depth-elongated Gaussians smear over the entire view.
    qx = np.clip(x/z, (-cx - .15*width)/fx, (1.15*width - cx)/fx)
    qy = np.clip(y/z, (-cy - .15*height)/fy, (1.15*height - cy)/fy)
    j00, j02 = fx/z, -fx*qx/z
    j11, j12 = fy/z, -fy*qy/z
    # Sigma_2d = J Sigma J^T for J = [[j00, 0, j02], [0, j11, j12]].
    s = cam_cov
    a = j00*j00*s[:, 0, 0] + 2*j00*j02*s[:, 0, 2] + j02*j02*s[:, 2, 2] + .3
    b = j00*j11*s[:, 0, 1] + j00*j12*s[:, 0, 2] + j02*j11*s[:, 2, 1] + j02*j12*s[:, 2, 2]
    cc = j11*j11*s[:, 1, 1] + 2*j11*j12*s[:, 1, 2] + j12*j12*s[:, 2, 2] + .3
    det = a*cc - b*b
    # Radius at which alpha falls below 1/255.
    level = 2.0 * np.log(np.maximum(255.0 * o, 1.0))
    hx, hy = np.sqrt(level * np.maximum(a, 0)), np.sqrt(level * np.maximum(cc, 0))
    limit = 0.5 * max(width, height)
    keep = (np.isfinite(u) & np.isfinite(v) & np.isfinite(det) & (det > 1e-12) & (o > _MIN_ALPHA)
            & (hx < limit) & (hy < limit)
            & (u + hx >= 0) & (u - hx < width) & (v + hy >= 0) & (v - hy < height))
    if not keep.any():
        return None
    ia, ib, ic = cc/det, -b/det, a/det
    # Cov(z, projected xy) Sigma_2d^-1 gives the depth of the footprint's conditional
    # mean at a pixel: tilted thin splats then follow their plane instead of a step.
    cross_x = j00*s[:, 2, 0] + j02*s[:, 2, 2]
    cross_y = j11*s[:, 2, 1] + j12*s[:, 2, 2]
    kx = cross_x*ia + cross_y*ib
    ky = cross_x*ib + cross_y*ic
    sz = np.sqrt(np.maximum(s[:, 2, 2], 0))
    k = keep
    return {"u": u[k].astype(np.float32), "v": v[k].astype(np.float32), "z": z[k].astype(np.float32),
            "sz": sz[k].astype(np.float32),
            "ia": ia[k].astype(np.float32), "ib": ib[k].astype(np.float32), "ic": ic[k].astype(np.float32),
            "kx": kx[k].astype(np.float32), "ky": ky[k].astype(np.float32), "o": o[k].astype(np.float32),
            "x0": np.clip(np.floor(u[k] - hx[k]), 0, width - 1).astype(np.int32),
            "x1": np.clip(np.ceil(u[k] + hx[k]), 0, width - 1).astype(np.int32),
            "y0": np.clip(np.floor(v[k] - hy[k]), 0, height - 1).astype(np.int32),
            "y1": np.clip(np.ceil(v[k] + hy[k]), 0, height - 1).astype(np.int32)}


def _fragments(p, ids, row0, row1, width):
    """Yield (strip pixel, conditional depth, optical depth) of Gaussians ``ids`` in rows row0..row1.

    Only footprint pixels with alpha of at least 1/255 are produced; pixel indices are
    relative to the strip's first row.
    """
    y0 = np.maximum(p["y0"][ids], row0)
    w = (p["x1"][ids] - p["x0"][ids] + 1).astype(np.int64)
    counts = w * (np.minimum(p["y1"][ids], row1) - y0 + 1)
    ends = np.cumsum(counts)
    start = 0
    while start < len(ids):
        base = int(ends[start - 1]) if start else 0
        stop = max(start + 1, int(np.searchsorted(ends, base + _FRAGMENT_CHUNK, side="right")))
        n = counts[start:stop]
        local = np.repeat(np.arange(start, stop, dtype=np.int32), n)
        g = ids[local]
        offset = (np.arange(len(local), dtype=np.int64)
                  - np.repeat(ends[start:stop] - n - base, n)).astype(np.int32)
        wg = w[local].astype(np.int32)
        px = p["x0"][g] + offset % wg
        py = y0[local] + offset // wg
        dx = px.astype(np.float32) + np.float32(.5) - p["u"][g]
        dy = py.astype(np.float32) + np.float32(.5) - p["v"][g]
        d2 = p["ia"][g]*dx*dx + np.float32(2)*p["ib"][g]*dx*dy + p["ic"][g]*dy*dy
        alpha = np.minimum(np.float32(.99), p["o"][g] * np.exp(np.float32(-.5) * d2))
        m = alpha >= np.float32(_MIN_ALPHA)
        g, px, py, dx, dy, alpha = g[m], px[m], py[m], dx[m], dy[m], alpha[m]
        sz3 = np.float32(3) * p["sz"][g]
        zc = p["z"][g]
        zf = np.clip(zc + p["kx"][g]*dx + p["ky"][g]*dy, zc - sz3, zc + sz3)
        yield (py - np.int32(row0))*np.int32(width) + px, zf, -np.log1p(-alpha)
        start = stop


def render_median_depth(means, cov6, opacity, camera):
    """Median depth (0 where transmittance never reaches 1/2) and alpha of one view.

    Gaussian means and packed covariances are in world coordinates; ``camera`` is a
    ``_camera`` record.  Depth is camera z in world units.  The image is processed in
    strips of rows so every histogram and fragment array stays small: large temporary
    arrays would be fresh page-faulted mappings, which parallel workers serialize on.
    """
    width, height = camera["width"], camera["height"]
    depth = np.zeros(width * height, dtype=np.float32)
    alpha = np.zeros(width * height, dtype=np.float32)
    stats = {"fragments": 0, "visible_gaussians": 0}
    p = _project(means, cov6, opacity, camera)
    if p is None:
        return depth.reshape(height, width), alpha.reshape(height, width), stats
    stats["visible_gaussians"] = int(len(p["u"]))
    z_lo = float(np.min(p["z"] - 3*p["sz"]))
    z_hi = float(np.max(p["z"] + 3*p["sz"]))
    span = max(z_hi - z_lo, 1e-9) * (1 + 1e-6)
    coarse_scale = _K_COARSE / span
    by_top = np.argsort(p["y0"], kind="stable")
    tops = p["y0"][by_top]
    for row0 in range(0, height, _STRIP_ROWS):
        row1 = min(row0 + _STRIP_ROWS, height) - 1
        candidates = by_top[:int(np.searchsorted(tops, row1, side="right"))]
        ids = candidates[p["y1"][candidates] >= row0]
        if not len(ids):
            continue
        pixels = (row1 - row0 + 1) * width
        coarse = np.zeros(pixels * _K_COARSE)
        chunks = []
        for pix, zf, tau in _fragments(p, ids, row0, row1, width):
            cell = np.minimum(((zf - z_lo) * coarse_scale).astype(np.int32), _K_COARSE - 1)
            coarse += np.bincount(pix * np.int32(_K_COARSE) + cell, tau, minlength=pixels * _K_COARSE)
            chunks.append((pix, zf, tau))
            stats["fragments"] += len(pix)
        coarse = np.cumsum(coarse.reshape(pixels, _K_COARSE), axis=1)
        optical = coarse[:, -1]
        opaque = optical >= _LN2
        crossing = np.argmax(coarse >= _LN2, axis=1)
        rows = np.arange(pixels)
        before = np.where(crossing > 0, coarse[rows, np.maximum(crossing - 1, 0)], 0.0)
        fine = np.zeros(pixels * _K_FINE)
        for pix, zf, tau in chunks:
            position = (zf - z_lo) * coarse_scale
            cell = np.minimum(position.astype(np.int32), _K_COARSE - 1)
            m = opaque[pix] & (cell == crossing[pix])
            sub = np.clip(((position[m] - cell[m]) * _K_FINE).astype(np.int32), 0, _K_FINE - 1)
            fine += np.bincount(pix[m] * np.int32(_K_FINE) + sub, tau[m], minlength=pixels * _K_FINE)
        fine = fine.reshape(pixels, _K_FINE)
        cumulative = before[:, None] + np.cumsum(fine, axis=1)
        sub_crossing = np.argmax(cumulative >= _LN2 * (1 - 1e-9), axis=1)
        previous = np.where(sub_crossing > 0, cumulative[rows, np.maximum(sub_crossing - 1, 0)], before)
        fraction = np.clip((_LN2 - previous) / np.maximum(fine[rows, sub_crossing], 1e-30), 0, 1)
        median = z_lo + (crossing + (sub_crossing + fraction) / _K_FINE) / _K_COARSE * span
        start = row0 * width
        depth[start:start + pixels] = np.where(opaque, median, 0)
        alpha[start:start + pixels] = 1 - np.exp(-optical)
    return depth.reshape(height, width), alpha.reshape(height, width), stats


def _render_worker(job_path):
    """Worker-process entry: render the views listed in a job file into .npy depth maps."""
    job = json.loads(Path(job_path).read_text())
    root = Path(job["root"])
    means = np.load(root / "means.npy", mmap_mode="r")
    cov6 = np.load(root / "cov6.npy", mmap_mode="r")
    opacity = np.load(root / "opacity.npy", mmap_mode="r")
    means, cov6, opacity = np.asarray(means), np.asarray(cov6), np.asarray(opacity)
    for view in job["views"]:
        camera = dict(view)
        camera["R"] = np.asarray(view["R"], dtype=np.float64)
        camera["t"] = np.asarray(view["t"], dtype=np.float64)
        depth, _, stats = render_median_depth(means, cov6, opacity, camera)
        np.save(root / f"depth_{view['index']}.npy", depth)
        (root / f"depth_{view['index']}.json").write_text(json.dumps(stats))


def _available_memory():
    """Reclaimable-inclusive available memory in bytes (Linux MemAvailable), or None."""
    try:
        with open("/proc/meminfo") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None


def _worker_count(views, gaussians, render_size):
    cpus = os.cpu_count() or 2
    count = max(1, min(cpus - 1, 8, views))
    available = _available_memory()
    if available:
        # Histograms and fragments grow with the pixel count; the cloud is per worker.
        need = _WORKER_BYTES * (render_size / DEFAULT_RENDER_SIZE) ** 2 + 40 * gaussians
        count = max(1, min(count, int(0.7 * available // need)))
    return count


def _render_all(means, cov6, opacity, views, work, deadline):
    """Render every view in parallel worker processes; return depth maps and totals."""
    np.save(work / "means.npy", np.ascontiguousarray(means, dtype=np.float32))
    np.save(work / "cov6.npy", np.ascontiguousarray(cov6, dtype=np.float32))
    np.save(work / "opacity.npy", np.ascontiguousarray(opacity, dtype=np.float32))
    workers = _worker_count(len(views), len(means), max(max(v["width"], v["height"]) for v in views))
    jobs = [[] for _ in range(workers)]
    for index, view in enumerate(views):
        record = {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in view.items()}
        record["index"] = index
        jobs[index % workers].append(record)
    repo = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    # Numpy threading inside each worker would oversubscribe the cores.
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        env[key] = "1"
    processes = []
    try:
        for n, views_of_job in enumerate(jobs):
            path = work / f"job_{n}.json"
            path.write_text(json.dumps({"root": str(work), "views": views_of_job}))
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "services.vision.splat_depth", str(path)],
                cwd=repo, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                start_new_session=True))
        while any(proc.poll() is None for proc in processes):
            _check_deadline(deadline)
            time.sleep(0.2)
        for proc in processes:
            if proc.returncode:
                detail = proc.stderr.read().decode(errors="replace")[-2000:] if proc.stderr else ""
                raise RuntimeError(f"Gaussian depth worker failed ({proc.returncode}): {detail}")
    finally:
        for proc in processes:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    proc.kill()
                proc.wait()
            if proc.stderr:
                proc.stderr.close()
    depths, fragments = [], 0
    for index in range(len(views)):
        depths.append(np.load(work / f"depth_{index}.npy"))
        fragments += json.loads((work / f"depth_{index}.json").read_text())["fragments"]
    return depths, {"render_workers": workers, "fragments": fragments}


def _backproject(depth, camera, stride=1):
    """World points of a depth map's valid pixels (optionally strided) and their pixel indices."""
    height, width = depth.shape
    ys, xs = np.mgrid[0:height:stride, 0:width:stride]
    d = depth[ys, xs]
    valid = d > 0
    xs, ys, d = xs[valid], ys[valid], d[valid].astype(np.float64)
    cam = np.column_stack(((xs + .5 - camera["cx"]) * d / camera["fx"],
                           (ys + .5 - camera["cy"]) * d / camera["fy"], d))
    return (cam - camera["t"]) @ camera["R"], ys * width + xs


def _plane_candidate(points, extent, rng):
    """RANSAC plane with the most inliers, refined to its own noise: (normal, offset, tol)."""
    tol = 0.004 * extent
    best, best_count = None, 0
    for _ in range(300):
        a, b, c = points[rng.choice(len(points), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        norm = np.linalg.norm(normal)
        if norm < 1e-12:
            continue
        normal /= norm
        count = int(np.count_nonzero(np.abs((points - a) @ normal) < tol))
        if count > best_count:
            best, best_count = (normal, float(normal @ a)), count
    if best is None:
        return None, 0
    normal, offset = best
    for _ in range(3):
        inliers = np.abs(points @ normal - offset) < tol
        sample = points[inliers]
        centroid = sample.mean(axis=0)
        normal = np.linalg.svd(sample - centroid, full_matrices=False)[2][-1]
        offset = float(normal @ centroid)
        residual = points[inliers] @ normal - offset
        # Shrink the inlier band to the measured noise of the plane itself.
        tol = max(4.0 * 1.4826 * float(np.median(np.abs(residual))), 1e-9 * extent)
    return (normal, offset, tol), best_count


def _supports(points, eyes, normal, offset, tol, extent):
    """Whether a plane is a support under the subject; returns the camera-facing plane or None.

    The cameras must look at it from one side (a vertical subject face is seen from
    both), more off-plane material must stand on that side than lie behind it (a subject's
    top face has the subject behind it), and at least a fifth of its inliers must have no
    material above them on a grid of 1 % of the extent grown by one cell (a plate's
    face is covered by its own edges and body, a table extends beyond what stands on it).
    """
    side = eyes @ normal - offset
    if np.count_nonzero(side < 0) > np.count_nonzero(side > 0):
        normal, offset, side = -normal, -offset, -side
    if np.mean(side > 0) < 0.8:
        return None
    height = points @ normal - offset
    inliers = np.abs(height) < tol
    above = height > tol
    if np.count_nonzero(above) < 50 or np.count_nonzero(above) <= np.count_nonzero(height < -tol):
        return None
    u = np.cross(normal, [1.0, 0.0, 0.0] if abs(normal[0]) < 0.9 else [0.0, 1.0, 0.0])
    u /= np.linalg.norm(u)
    plane_2d = points @ np.column_stack((u, np.cross(normal, u)))
    cells = np.floor((plane_2d - plane_2d.min(axis=0)) / (0.01 * extent)).astype(np.int64) + 1
    covered = np.zeros(tuple(cells.max(axis=0) + 2), dtype=bool)
    covered[tuple(cells[above].T)] = True
    grown = covered.copy()
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            grown[1:-1, 1:-1] |= covered[1 + dx:covered.shape[0] - 1 + dx, 1 + dy:covered.shape[1] - 1 + dy]
    if np.mean(~grown[tuple(cells[inliers].T)]) < 0.2:
        return None
    return normal, offset, 1.4826 * float(np.median(np.abs(height[inliers])))


def _fit_support_plane(points, eyes, seed=0):
    """The plane the subject stands on, from rendered surface points: (normal, offset, noise) or None.

    The five most populated planes are tried in turn (each holding at least 5 % of the
    points); a rejected plane's inliers are set aside before the next attempt, so a
    subject's large top face cannot hide the floor.  The returned normal points toward
    the cameras and the subject.
    """
    if len(points) < 500:
        return None
    rng = np.random.default_rng(seed)
    extent = float(np.max(np.ptp(points, axis=0)))
    eyes = np.asarray(eyes, dtype=np.float64)
    remaining = points
    for _ in range(5):
        if len(remaining) < 0.05 * len(points) or len(remaining) < 3:
            return None
        candidate, count = _plane_candidate(remaining, extent, rng)
        if candidate is None or count < 0.05 * len(points):
            return None
        normal, offset, tol = candidate
        plane = _supports(points, eyes, normal, offset, tol, extent)
        if plane is not None:
            return plane
        remaining = remaining[np.abs(remaining @ normal - offset) >= tol]
    return None


def _color_image(images_dir, camera):
    """The view's photograph resized to the render size, float RGB in [0, 1], or None."""
    if not images_dir:
        return None
    path = Path(images_dir) / camera["name"]
    if not path.is_file():
        return None
    try:
        import cv2
    except ImportError:
        return None
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return None
    image = cv2.resize(image, (camera["width"], camera["height"]), interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(image[:, :, ::-1], dtype=np.float32) / 255.0


def _unreliable(depth, focal, max_angle=None):
    """Pixels whose surface is seen beyond ``max_angle`` from its normal, at a jump, or by a hole.

    The angle follows from the depth gradient: tan(angle) = |grad z| * focal / z.  Steep
    views and occlusion boundaries carry the least accurate median depth and seed TSDF
    fins.  The mask is grown by one pixel.
    """
    limit = math.tan(math.radians(_MAX_VIEW_ANGLE if max_angle is None else max_angle))
    bad = depth <= 0
    gx = np.zeros(depth.shape, dtype=np.float32)
    gy = np.zeros(depth.shape, dtype=np.float32)
    gx[:, 1:-1] = 0.5 * (depth[:, 2:] - depth[:, :-2])
    gy[1:-1] = 0.5 * (depth[2:] - depth[:-2])
    hole = bad.copy()
    hole[:, 1:-1] |= bad[:, 2:] | bad[:, :-2]
    hole[1:-1] |= bad[2:] | bad[:-2]
    with np.errstate(divide="ignore", invalid="ignore"):
        steep = np.hypot(gx, gy) * focal > limit * depth
    unreliable = hole | steep
    unreliable[[0, -1], :] = True
    unreliable[:, [0, -1]] = True
    grown = unreliable.copy()
    grown[1:] |= unreliable[:-1]
    grown[:-1] |= unreliable[1:]
    grown[:, 1:] |= unreliable[:, :-1]
    grown[:, :-1] |= unreliable[:, 1:]
    return grown


def _keep_consistent(views, depths, subject_depths, voxel, neighbors=6, required=2):
    """Zero subject pixels that fewer than ``required`` nearby views reproduce; return the count.

    A pixel's surface point is reprojected into the closest views looking the same way;
    it is reproduced when that view's median depth there agrees within the tolerance.
    Floaters and view-dependent splats that only one view renders do not survive.
    """
    centers = np.stack([-c["R"].T @ c["t"] for c in views])
    axes = np.stack([c["R"][2] for c in views])
    rejected = 0
    for i, (camera, subject) in enumerate(zip(views, subject_depths)):
        world, pixel = _backproject(subject, camera)
        if not len(world):
            continue
        similar = np.flatnonzero(axes @ axes[i] > 0.7)
        similar = similar[similar != i]
        nearest = similar[np.argsort(np.linalg.norm(centers[similar] - centers[i], axis=1))[:neighbors]]
        if not len(nearest):
            continue
        agree = np.zeros(len(world), dtype=np.int32)
        for j in nearest:
            other, depth = views[j], depths[j]
            pc = world @ other["R"].T + other["t"]
            z = pc[:, 2]
            front = z > 0
            u = np.where(front, other["fx"] * pc[:, 0] / np.where(front, z, 1) + other["cx"], -1)
            v = np.where(front, other["fy"] * pc[:, 1] / np.where(front, z, 1) + other["cy"], -1)
            inside = front & (u >= 0) & (u < other["width"]) & (v >= 0) & (v < other["height"])
            seen = np.zeros(len(world))
            seen[inside] = depth[v[inside].astype(np.int64), u[inside].astype(np.int64)]
            agree += inside & (seen > 0) & (np.abs(seen - z) <= np.maximum(2.0 * voxel, 0.005 * z))
        drop = agree < min(required, len(nearest))
        subject.reshape(-1)[pixel[drop]] = 0
        rejected += int(np.count_nonzero(drop))
    return rejected


def _carve(vertices, views, depths, voxel):
    """Vertices that registered views see through: free-space votes against observations."""
    seen = np.zeros(len(vertices), dtype=np.int32)
    through = np.zeros(len(vertices), dtype=np.int32)
    for camera, depth in zip(views, depths):
        height, width = depth.shape
        pc = vertices @ camera["R"].T + camera["t"]
        z = pc[:, 2]
        front = z > 0
        u = np.full(len(z), -1.0)
        v = np.full(len(z), -1.0)
        u[front] = camera["fx"] * pc[front, 0] / z[front] + camera["cx"]
        v[front] = camera["fy"] * pc[front, 1] / z[front] + camera["cy"]
        inside = front & (u >= 1) & (u < width - 1) & (v >= 1) & (v < height - 1)
        idx = np.flatnonzero(inside)
        surface = depth[v[idx].astype(np.int64), u[idx].astype(np.int64)]
        tolerance = np.maximum(3.0 * voxel, 0.01 * z[idx])
        visible = (surface <= 0) | (surface > z[idx] - tolerance)
        seen[idx[visible]] += 1
        # The median surface is clearly behind the vertex, or the ray hits nothing.
        through[idx[visible & ((surface <= 0) | (surface > z[idx] + tolerance))]] += 1
    return seen, through


def fuse_splat_depth(means, scales, rotations, opacity, cameras, images, *,
                     bounds, images_dir=None, remove_floor=True, voxel_size=None,
                     max_views=DEFAULT_VIEWS, render_size=DEFAULT_RENDER_SIZE,
                     time_budget=TIME_BUDGET_SECONDS):
    """Render matching registered undistorted cameras, then fuse subject depth with Open3D.

    ``bounds`` is the (lo, hi) world box of the subject; only Gaussians and surfaces inside
    it are rendered and fused.  All points and the resulting mesh stay in COLMAP
    reconstruction units.  ``voxel_size`` (reconstruction units) defaults to the views'
    median pixel footprint on the subject.  The time budget is cooperative between
    rendering, integration, and extraction.
    """
    for value, ceiling, name in ((max_views, MAX_VIEWS, "max_views"),
                                  (render_size, MAX_RENDER_SIZE, "render_size")):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise ValueError(f"{name} must be an integer between 1 and {ceiling}")
    if not math.isfinite(time_budget) or not 0 < time_budget <= TIME_BUDGET_SECONDS:
        raise ValueError(f"time_budget must be positive and at most {TIME_BUDGET_SECONDS} seconds")
    if voxel_size is not None and (not math.isfinite(voxel_size) or voxel_size <= 0):
        raise ValueError("voxel_size must be positive")
    started = time.monotonic()
    deadline = started + time_budget
    registered = len(images)
    if not registered:
        raise ValueError("no registered COLMAP camera poses")
    if registered > MAX_REGISTERED_VIEWS:
        raise ValueError(f"COLMAP model exceeds {MAX_REGISTERED_VIEWS} registered views")
    views = []
    for image in sorted(images.values(), key=lambda i: i.id):
        if image.camera_id not in cameras:
            raise ValueError("COLMAP image references a missing camera")
        views.append(_camera(image, cameras[image.camera_id], render_size))
    lo, hi = (np.asarray(b, dtype=np.float64) for b in bounds)
    if lo.shape != (3,) or hi.shape != (3,) or not (np.isfinite(lo).all() and np.isfinite(hi).all()) \
            or np.any(hi <= lo):
        raise ValueError("subject bounds must be a finite, nonempty box")
    means = np.asarray(means, dtype=np.float64)
    if len(means) > MAX_GAUSSIANS:
        raise ValueError(f"splat exceeds {MAX_GAUSSIANS} Gaussians")
    inside = np.all((means >= lo) & (means <= hi), axis=1)
    if np.count_nonzero(inside) < 30:
        raise ValueError("at least 30 Gaussian centers are needed inside the subject bounds")
    cov6 = _covariances(np.asarray(scales)[inside], np.asarray(rotations)[inside])
    region_means = means[inside]
    region_opacity = np.asarray(opacity, dtype=np.float64)[inside]
    views = _select_views(views, 0.5 * (lo + hi), max_views)
    stats = {"registered_views": registered, "selected_views": len(views),
             "gaussians_used": int(len(region_means)), "bounds": [lo.tolist(), hi.tolist()],
             "render_size": render_size}
    work = Path(tempfile.mkdtemp(prefix="splat-depth-"))
    try:
        depths, render_stats = _render_all(region_means, cov6, region_opacity, views, work, deadline)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    stats.update(render_stats, render_seconds=round(time.monotonic() - started, 1))
    _check_deadline(deadline)

    # Subject classification: inside the bounds and, optionally, above the support plane.
    sample = []
    rng = np.random.default_rng(0)
    for camera, depth in zip(views, depths):
        world, _ = _backproject(depth, camera, stride=4)
        if len(world):
            sample.append(world[rng.choice(len(world), min(len(world), 4000), replace=False)])
    if not sample:
        raise ValueError("no registered view produced opaque Gaussian depth; check the matching gauge and capture")
    sample = np.concatenate(sample)
    sample = sample[np.all((sample >= lo) & (sample <= hi), axis=1)]
    eyes = np.stack([-c["R"].T @ c["t"] for c in views])
    plane = _fit_support_plane(sample, eyes) if remove_floor else None
    footprints = []
    subject_depths = []
    subject_pixels = 0
    for camera, depth in zip(views, depths):
        world, pixel = _backproject(depth, camera)
        keep = np.all((world >= lo) & (world <= hi), axis=1)
        if plane is not None:
            keep &= world @ plane[0] - plane[1] > max(3.0 * plane[2], 1e-12)
        mask = np.zeros(depth.size, dtype=bool)
        mask[pixel[keep]] = True
        mask = mask.reshape(depth.shape)
        # Silhouette pixels mix subject and background depth, and depth jumps (occlusion
        # boundaries, near-tangent surfaces) seed TSDF fins; fuse neither.
        interior = mask & ~_unreliable(depth, camera["fx"])
        interior[1:, :] &= mask[:-1, :]
        interior[:-1, :] &= mask[1:, :]
        interior[:, 1:] &= mask[:, :-1]
        interior[:, :-1] &= mask[:, 1:]
        subject = np.where(interior, depth, 0).astype(np.float32)
        subject_depths.append(subject)
        count = int(np.count_nonzero(interior))
        subject_pixels += count
        if count:
            footprints.append(float(np.median(subject[interior])) / camera["fx"])
    if not subject_pixels:
        raise ValueError("no subject surface was rendered inside the bounds; check the subject region")
    footprint = float(np.median(footprints))
    voxel = float(voxel_size) if voxel_size is not None else max(1.5 * footprint, float(np.max(hi - lo)) / 4096)
    rejected = _keep_consistent(views, depths, subject_depths, voxel)
    subject_pixels -= rejected
    stats.update(support_plane=None if plane is None else
                 {"normal": plane[0].tolist(), "offset": plane[1], "noise": plane[2]},
                 subject_pixels=subject_pixels, inconsistent_pixels=rejected,
                 pixel_footprint=footprint, voxel_size=voxel)
    _check_deadline(deadline)

    import open3d as o3d
    import open3d.core as o3c
    device = o3c.Device("CUDA:0") if o3c.cuda.is_available() else o3c.Device("CPU:0")
    block = 8 * voxel
    occupied = set()
    for camera, depth in zip(views, subject_depths):
        world, _ = _backproject(depth, camera, stride=2)
        if len(world):
            occupied.update(map(tuple, np.unique(np.floor(world / block).astype(np.int64), axis=0)))
    # The truncation band and frustum allocation touch neighbors of surface blocks.
    block_count = int(min(MAX_TSDF_BLOCKS, max(4096, 4 * len(occupied))))
    if 2 * len(occupied) > MAX_TSDF_BLOCKS:
        raise ValueError("the subject needs more TSDF blocks than allowed; pass a larger voxel_size")
    grid = o3d.t.geometry.VoxelBlockGrid(
        attr_names=("tsdf", "weight", "color"),
        attr_dtypes=(o3c.float32, o3c.float32, o3c.float32),
        attr_channels=((1), (1), (3)), voxel_size=voxel, block_resolution=8,
        block_count=block_count, device=device)
    integrated = 0
    colored = 0
    for camera, depth in zip(views, subject_depths):
        _check_deadline(deadline)
        if not depth.any():
            continue
        intrinsic = o3c.Tensor([[camera["fx"], 0, camera["cx"] - .5], [0, camera["fy"], camera["cy"] - .5],
                                [0, 0, 1]], o3c.float64)
        extrinsic = np.eye(4)
        extrinsic[:3, :3], extrinsic[:3, 3] = camera["R"], camera["t"]
        extrinsic = o3c.Tensor(extrinsic, o3c.float64)
        depth_max = float(depth.max()) + _TRUNC_VOXELS * voxel * 2
        depth_image = o3d.t.geometry.Image(o3c.Tensor(depth)).to(device)
        color = _color_image(images_dir, camera)
        if color is None:
            color = np.full(depth.shape + (3,), .6, dtype=np.float32)
        else:
            colored += 1
        color_image = o3d.t.geometry.Image(o3c.Tensor(color)).to(device)
        coords = grid.compute_unique_block_coordinates(depth_image, intrinsic, extrinsic, 1.0, depth_max,
                                                       _TRUNC_VOXELS)
        grid.integrate(coords, depth_image, color_image, intrinsic, intrinsic, extrinsic, 1.0, depth_max,
                       _TRUNC_VOXELS)
        integrated += 1
    stats.update(integrated_views=integrated, colored_views=colored, tsdf_device=str(device),
                 tsdf_blocks=block_count)
    _check_deadline(deadline)
    if not integrated:
        raise ValueError("no registered view produced opaque subject depth; check the matching gauge and capture")
    weight_threshold = float(min(3.0, max(1.0, integrated / 8)))
    mesh = grid.extract_triangle_mesh(weight_threshold=weight_threshold).to_legacy()
    del grid
    _check_deadline(deadline)
    if not mesh.has_triangles():
        raise ValueError("TSDF fusion produced no triangles; capture more overlapping opaque views")
    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_unreferenced_vertices()
    seen, through = _carve(np.asarray(mesh.vertices), views, depths, voxel)
    carved = (through >= 3) & (through >= 0.3 * seen)
    stats["carved_vertices"] = int(np.count_nonzero(carved))
    mesh.remove_vertices_by_mask(carved)
    mesh.remove_unreferenced_vertices()
    if not mesh.has_triangles():
        raise ValueError("free-space carving removed every TSDF triangle")
    labels, counts, _ = mesh.cluster_connected_triangles()
    counts, labels = np.asarray(counts), np.asarray(labels)
    triangles = np.asarray(mesh.triangles)
    vertices = np.asarray(mesh.vertices)
    keep = counts >= max(50, int(counts.max() * .01))
    if plane is not None:
        # Pieces that never rise clear of the support plane are its curled edges and
        # printed-mat debris, not the subject standing on it.
        height = vertices @ plane[0] - plane[1]
        peak = np.full(len(counts), -np.inf)
        np.maximum.at(peak, labels, height[triangles].max(axis=1))
        keep &= peak > max(10.0 * plane[2], 8.0 * voxel)
    if not keep.any():
        raise ValueError("no TSDF component rises clear of the support plane; pass remove_floor=false")
    # The subject is the largest piece and whatever lies near it; distant pieces are
    # other objects inside the bounds (a table edge, a box behind the subject).
    from scipy.spatial import cKDTree
    main = int(np.argmax(np.where(keep, counts, -1)))
    main_points = vertices[np.unique(triangles[labels == main])]
    gap = 0.02 * float(np.max(np.ptp(main_points, axis=0)))
    tree = cKDTree(main_points[::max(1, len(main_points) // 200_000)])
    for label in np.flatnonzero(keep):
        if label != main:
            points = vertices[np.unique(triangles[labels == label])]
            distance, _ = tree.query(points[::max(1, len(points) // 2000)], k=1)
            keep[label] = float(distance.min()) <= gap
    removed_triangles = int(np.count_nonzero(~keep[labels]))
    mesh.remove_triangles_by_mask(~keep[labels])
    mesh.remove_unreferenced_vertices()
    mesh.compute_vertex_normals()
    stats.update(components_found=int(len(counts)), components_kept=int(np.count_nonzero(keep)),
                 component_triangles_removed=removed_triangles, weight_threshold=weight_threshold,
                 elapsed_seconds=round(time.monotonic() - started, 1))
    return mesh, stats


if __name__ == "__main__":
    _render_worker(sys.argv[1])
