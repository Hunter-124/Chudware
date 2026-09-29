"""Exact camera math for focused CAD renders: the engine's focused orthographic frame, the
perspective `camera_pose.v1` fit, and the projection/clipping helpers every overlay uses to
land on the pixels the engine drew. Which camera to use is shot_planner's job.
"""
from __future__ import annotations

import math


try:
    import numpy as np
except Exception:  # pragma: no cover - exercised by environments without the optional overlay stack
    np = None


_ISO_FALLBACK = (1.0, 1.0, 0.8)


def _dot(a, b) -> float:
    return float(a[0]) * float(b[0]) + float(a[1]) * float(b[1]) + float(a[2]) * float(b[2])


def _cross(a, b) -> tuple[float, float, float]:
    return (
        float(a[1]) * float(b[2]) - float(a[2]) * float(b[1]),
        float(a[2]) * float(b[0]) - float(a[0]) * float(b[2]),
        float(a[0]) * float(b[1]) - float(a[1]) * float(b[0]),
    )


def _unit(v, fallback=None) -> tuple[float, float, float] | None:
    try:
        q = (float(v[0]), float(v[1]), float(v[2]))
    except (TypeError, ValueError, IndexError, KeyError):
        return _unit(fallback) if fallback is not None else None
    length = math.sqrt(_dot(q, q))
    if not math.isfinite(length) or length < 1e-15:
        return _unit(fallback) if fallback is not None else None
    return (q[0] / length, q[1] / length, q[2] / length)


def focus_frame(target_bbox: dict, view_dir, W: int, H: int, margin: float = 0.15) -> dict:
    """Mirror the engine's focused orthographic projection exactly.

    The focus AABB, rather than the whole part, supplies the camera centre and fit extents.
    Planar/linear projected boxes are widened to two percent of the dominant span before the
    explicit margin is applied.  Unlike whole-part auto-fit, focused fit has no 0.85 shrink.
    """
    lo0 = tuple(float(x) for x in target_bbox["min"][:3])
    hi0 = tuple(float(x) for x in target_bbox["max"][:3])
    lo = tuple(min(a, b) for a, b in zip(lo0, hi0))
    hi = tuple(max(a, b) for a, b in zip(lo0, hi0))
    center = tuple((a + b) * 0.5 for a, b in zip(lo, hi))

    d = _unit(view_dir, _ISO_FALLBACK)
    assert d is not None
    up_ref = (0.0, 1.0, 0.0) if abs(d[2]) > 0.985 else (0.0, 0.0, 1.0)
    r = _unit(_cross(up_ref, d))
    assert r is not None
    u = _cross(d, r)

    projected_u: list[float] = []
    projected_v: list[float] = []
    for x in (lo[0], hi[0]):
        for y in (lo[1], hi[1]):
            for z in (lo[2], hi[2]):
                rel = (x - center[0], y - center[1], z - center[2])
                projected_u.append(_dot(rel, r))
                projected_v.append(_dot(rel, u))
    minu, maxu = min(projected_u), max(projected_u)
    minv, maxv = min(projected_v), max(projected_v)

    span = max(maxu - minu, maxv - minv, 1e-3)
    if maxu - minu < span * 0.02:
        mid = (minu + maxu) * 0.5
        minu, maxu = mid - span * 0.01, mid + span * 0.01
    if maxv - minv < span * 0.02:
        mid = (minv + maxv) * 0.5
        minv, maxv = mid - span * 0.01, mid + span * 0.01

    pad = max(0.0, min(float(margin), 4.0))
    du = (maxu - minu) * pad
    dv = (maxv - minv) * pad
    minu, maxu = minu - du, maxu + du
    minv, maxv = minv - dv, maxv + dv

    width = max(64, min(int(W), 2048))
    height = max(64, min(int(H), 2048))
    scale = min(width / max(maxu - minu, 1e-6), height / max(maxv - minv, 1e-6))
    # The engine's near-clip plane: the front face of the focus box in camera depth. Fragments
    # ABOVE it sit between the camera and the target and are cut away (RenderFocus.clip_near).
    clip_depth = max(_dot((x - center[0], y - center[1], z - center[2]), d)
                     for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2]))
    return {
        "center": center,
        "r": r,
        "u": u,
        "d": d,
        "scale": scale,
        "cu": (minu + maxu) * 0.5,
        "cv": (minv + maxv) * 0.5,
        "W": width,
        "H": height,
        "clip_depth": float(clip_depth),
    }


# ------------------------------------------------------------------ perspective close-ups
# A focused ORTHO frame reads like a drawing: a rib standing on a plate and a rib hovering
# 4 mm above it project to the same silhouette from most angles. A true perspective camera
# (doc_render_view `camera_pose.v1`) adds the depth cues a reviewer uses — converging edges,
# near-is-bigger, a visible gap under a floating body — so close-ups default to it.
PERSPECTIVE_FOV_DEG = 30.0


def _box_corners(bbox: dict):
    lo = [float(x) for x in bbox["min"][:3]]
    hi = [float(x) for x in bbox["max"][:3]]
    lo, hi = [min(a, b) for a, b in zip(lo, hi)], [max(a, b) for a, b in zip(lo, hi)]
    return [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]


def perspective_camera(target_bbox: dict, view_dir, W: int, H: int, *,
                       fov_deg: float = PERSPECTIVE_FOV_DEG, margin: float = 0.35,
                       distance=None, scene_bbox=None, clip_near: bool = False,
                       points=None) -> dict:
    """An exact perspective pose that frames `target_bbox` from direction `view_dir`.

    `view_dir` is scene->camera (the same convention as every ortho view). The camera looks
    at the box centre and backs off until every box corner projects inside the frame with
    `margin` padding (fractional, per side, like the ortho focus margin): for corner offset
    (x, y, z) in the camera basis, depth is D - z, so the fit distance is
    ``max(z + |x| / (tan_x * fill), z + |y| / (tan_y * fill))`` with ``fill = 1 / (1 + 2m)``.
    `distance` (mm, camera to box centre) overrides the fit; it is clamped so the camera never
    sits inside the target box. `clip_near` puts the near plane just in front of the box
    (the perspective cutaway: everything between the eye and the target is removed).
    `scene_bbox` (the whole part) sizes the far plane so context behind the target renders.
    `points` (the target's actual surface points, e.g. its tessellation vertices) replace the
    8 box corners in the fit: an arc rib's AABB projects far wider than the rib itself, so a
    corner fit leaves the feature filling half the frame it was meant to fill.

    Returns the engine `view.camera` dict plus ``distance`` (mm) and ``fit_distance``."""
    corners = _box_corners(target_bbox)
    if points is not None and len(points) >= 2:
        pts = [tuple(float(v) for v in p[:3]) for p in points]
        lo = [min(p[k] for p in pts) for k in range(3)]
        hi = [max(p[k] for p in pts) for k in range(3)]
        center = tuple((lo[k] + hi[k]) * 0.5 for k in range(3))
        corners = pts
    else:
        center = tuple(sum(c[k] for c in corners) / 8.0 for k in range(3))
    d = _unit(view_dir, _ISO_FALLBACK)
    assert d is not None
    forward = (-d[0], -d[1], -d[2])
    up_ref = (0.0, 1.0, 0.0) if abs(d[2]) > 0.985 else (0.0, 0.0, 1.0)
    right = _unit(_cross(forward, up_ref))
    assert right is not None
    up = _cross(right, forward)
    fov = max(5.0, min(float(fov_deg), 120.0))
    tan_y = math.tan(math.radians(fov) / 2.0)
    tan_x = tan_y * float(W) / float(max(H, 1))
    fill = 1.0 / (1.0 + 2.0 * max(0.0, min(float(margin), 4.0)))

    def _fit(ctr):
        rel = [(c[0] - ctr[0], c[1] - ctr[1], c[2] - ctr[2]) for c in corners]
        span_ = max(math.sqrt(_dot(r, r)) for r in rel) * 2.0 or 1.0
        z_front_ = max(_dot(r, d) for r in rel)
        fit_ = max(max(_dot(r, d) + abs(_dot(r, right)) / (tan_x * fill),
                       _dot(r, d) + abs(_dot(r, up)) / (tan_y * fill)) for r in rel)
        return rel, span_, z_front_, max(fit_, z_front_ + 0.05 * span_)

    rel, span, z_front, fit = _fit(center)
    if points is not None and len(points) >= 2:
        # A symmetric fit about the box centre leaves a lopsided silhouette off-centre (and
        # small). Aim at the middle of the PROJECTED extent instead, then refit.
        for _ in range(2):
            sx = [_dot(r, right) / max(fit - _dot(r, d), 1e-9) for r in rel]
            sy = [_dot(r, up) / max(fit - _dot(r, d), 1e-9) for r in rel]
            ox, oy = (max(sx) + min(sx)) * 0.5 * fit, (max(sy) + min(sy)) * 0.5 * fit
            center = tuple(center[k] + right[k] * ox + up[k] * oy for k in range(3))
            rel, span, z_front, fit = _fit(center)
    dist = fit
    if distance is not None:
        try:
            dist = float(distance)
        except (TypeError, ValueError):
            dist = fit
        if not math.isfinite(dist):
            dist = fit
        dist = max(dist, z_front + 0.02 * span)
    position = tuple(center[k] + d[k] * dist for k in range(3))
    if clip_near:
        near = max(dist - z_front - 0.01 * span, 1e-4 * dist, 1e-4)
    else:
        near = max(1e-3 * dist, 1e-4)
    far_depth = dist + span
    if scene_bbox is not None:
        far_depth = max(far_depth, max(
            _dot((c[0] - position[0], c[1] - position[1], c[2] - position[2]), forward)
            for c in _box_corners(scene_bbox)))
    far = max(far_depth * 1.05, near * 10.0)
    return {
        "projection": "perspective",
        "position": [float(x) for x in position],
        "target": [float(x) for x in center],
        "up": [float(x) for x in up],
        "vertical_fov_deg": fov,
        "near": float(near),
        "far": float(far),
        "width": int(W),
        "height": int(H),
        "distance": float(dist),
        "fit_distance": float(fit),
    }


def perspective_frame(camera: dict) -> dict:
    """The overlay frame for a perspective `camera` — the same record `frame_from_echo`
    builds from the engine's echo, so tests and callers can project without a render."""
    pos = tuple(float(x) for x in camera["position"])
    tgt = tuple(float(x) for x in camera["target"])
    forward = _unit((tgt[0] - pos[0], tgt[1] - pos[1], tgt[2] - pos[2]))
    right = _unit(_cross(forward, _unit(camera["up"])))
    up = _cross(right, forward)
    W, H = int(camera["width"]), int(camera["height"])
    focal = (H * 0.5) / math.tan(math.radians(float(camera["vertical_fov_deg"])) / 2.0)
    depth = _dot((tgt[0] - pos[0], tgt[1] - pos[1], tgt[2] - pos[2]), forward)
    return {"projection": "perspective", "position": pos, "r": right, "u": up, "f": forward,
            "d": (-forward[0], -forward[1], -forward[2]), "focal": focal,
            "cx": W / 2.0, "cy": H / 2.0, "near": float(camera["near"]),
            "far": float(camera["far"]), "W": W, "H": H, "target_depth": depth,
            "scale": focal / max(depth, 1e-9)}


def frame_from_echo(frame: dict, camera: dict | None = None) -> dict:
    """Engine perspective echo (`doc_render_view` frame, projection "perspective") → the
    overlay frame. `camera` (the echoed pose) supplies the look-at target, whose depth sets
    the px-per-mm `scale` a scale bar is drawn at."""
    pos = tuple(float(x) for x in frame["position"])
    forward = tuple(float(x) for x in frame["forward"])
    out = {"projection": "perspective", "position": pos,
           "r": tuple(float(x) for x in frame["right"]),
           "u": tuple(float(x) for x in frame["up"]), "f": forward,
           "d": (-forward[0], -forward[1], -forward[2]),
           "focal": float(frame["focal_length_px"]),
           "cx": float(frame["principal_x_px"]), "cy": float(frame["principal_y_px"]),
           "near": float(frame["near"]), "far": float(frame["far"]),
           "W": int(frame["width"]), "H": int(frame["height"])}
    depth = None
    if camera and camera.get("target") is not None:
        tgt = tuple(float(x) for x in camera["target"])
        depth = _dot((tgt[0] - pos[0], tgt[1] - pos[1], tgt[2] - pos[2]), forward)
    depth = depth if depth and depth > 0 else out["near"] * 10.0
    out["target_depth"] = depth
    out["scale"] = out["focal"] / depth
    return out


def perspective_project(pts, frame: dict):
    """World points -> (sx, sy, 1/depth) through a perspective overlay frame.

    Matches the engine rasterizer: ``sx = cx + f*x/z``, ``sy = cy - f*y/z`` and the depth
    buffer holds reciprocal camera depth, so LARGER is nearer exactly as for the ortho
    frames. A point in front of the near plane (or behind the eye) gets NaN screen coords and
    depth -1e30: it can never win a depth test or land on screen."""
    rel = np.asarray(pts, dtype=np.float64).reshape(-1, 3) - np.asarray(frame["position"])
    x = rel @ np.asarray(frame["r"], dtype=np.float64)
    y = rel @ np.asarray(frame["u"], dtype=np.float64)
    z = rel @ np.asarray(frame["f"], dtype=np.float64)
    ok = z >= float(frame["near"]) * (1.0 - 1e-9)
    zz = np.where(ok, z, 1.0)
    sx = np.where(ok, float(frame["cx"]) + float(frame["focal"]) * x / zz, np.nan)
    sy = np.where(ok, float(frame["cy"]) - float(frame["focal"]) * y / zz, np.nan)
    dep = np.where(ok, 1.0 / zz, -1e30)
    return np.stack((sx, sy, dep), axis=-1)


def camera_space(pts, frame: dict):
    """World points -> camera-basis (x right, y up, z forward depth) for near/far clipping."""
    rel = np.asarray(pts, dtype=np.float64).reshape(-1, 3) - np.asarray(frame["position"])
    return np.stack((rel @ np.asarray(frame["r"], dtype=np.float64),
                     rel @ np.asarray(frame["u"], dtype=np.float64),
                     rel @ np.asarray(frame["f"], dtype=np.float64)), axis=-1)


def clip_polygon_depth(poly: list, near: float, far: float) -> list:
    """Sutherland-Hodgman clip of a camera-space polygon to near <= z <= far — the same two
    half-spaces the engine clips against before rasterizing a perspective triangle."""
    def _clip(points, inside, t_of):
        out = []
        if not points:
            return out
        prev = points[-1]
        prev_in = inside(prev)
        for cur in points:
            cur_in = inside(cur)
            if cur_in != prev_in:
                t = t_of(prev, cur)
                out.append(tuple(prev[k] + t * (cur[k] - prev[k]) for k in range(3)))
            if cur_in:
                out.append(cur)
            prev, prev_in = cur, cur_in
        return out

    poly = _clip(poly, lambda p: p[2] >= near, lambda a, b: (near - a[2]) / (b[2] - a[2]))
    return _clip(poly, lambda p: p[2] <= far, lambda a, b: (far - a[2]) / (b[2] - a[2]))
