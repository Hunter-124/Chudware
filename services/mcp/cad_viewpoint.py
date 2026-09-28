"""Deterministic, measured camera selection for focused CAD renders.

The chooser deliberately uses a small software rasterizer rather than trusting a face-normal
heuristic: every lattice direction is projected through the same focused camera as the engine,
then scored for target visibility, useful screen area, and readable (not dead-on) relief.
"""
from __future__ import annotations

import math


try:
    import numpy as np
except Exception:  # pragma: no cover - exercised by environments without the optional overlay stack
    np = None


_ISO_FALLBACK = (1.0, 1.0, 0.8)
_DEDUP_DEG = 3.0


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


def _direction_from(value) -> tuple[float, float, float] | None:
    if isinstance(value, dict):
        value = value.get("dir", value.get("normal"))
    return _unit(value)


def _dominant_normal(normals) -> tuple[float, float, float] | None:
    acc = [0.0, 0.0, 0.0]
    found = False
    for item in normals or ():
        normal = _direction_from(item)
        if normal is None:
            continue
        if found and _dot(acc, normal) < 0.0:
            normal = (-normal[0], -normal[1], -normal[2])
        acc[0] += normal[0]
        acc[1] += normal[1]
        acc[2] += normal[2]
        found = True
    return _unit(acc) if found else None


def candidate_dirs(target_bbox: dict, hint=None, normals=None) -> list[dict]:
    """Return the fixed ideal-view candidate lattice, preserving deterministic order."""
    del target_bbox  # reserved for future shape-aware lattices; ordering is intentionally stable
    raw: list[dict] = []
    hinted = _direction_from(hint)
    if hinted is not None:
        label = str(hint.get("label") or "hint") if isinstance(hint, dict) else "hint"
        raw.append({"dir": hinted, "label": label})

    normal = _dominant_normal(normals)
    if normal is not None:
        # BOTH hemispheres. The sign of a bore's axis is arbitrary, and the sign of a face
        # normal only says which side is outside — sampling one hemisphere buried every
        # from-above camera of a downward-normal target under 32 from-below ones. Views from
        # the wrong side simply score zero on the occlusion test, so this costs only time.
        for sign, tag in ((1.0, "normal"), (-1.0, "anti")):
            axis_dir = tuple(sign * normal[k] for k in range(3))
            raw.append({"dir": axis_dir, "label": tag})
            # Pick a stable zero-azimuth tangent.  World +Z is preferred except at the poles,
            # matching the renderer's basis switch and avoiding a near-zero cross product.
            ref = (0.0, 1.0, 0.0) if abs(axis_dir[2]) > 0.985 else (0.0, 0.0, 1.0)
            tangent0 = _unit(_cross(ref, axis_dir))
            assert tangent0 is not None
            tangent1 = _cross(axis_dir, tangent0)
            # 20/35 read a planar face obliquely; 55/70 are what a bore or a boss needs, where
            # the reported "normal" IS the revolve axis and a shallow tilt still looks down it.
            for tilt_deg in (20, 35, 55, 70):
                tilt = math.radians(tilt_deg)
                ct, st = math.cos(tilt), math.sin(tilt)
                for az_deg in range(0, 360, 45):
                    az = math.radians(az_deg)
                    tangent = tuple(
                        math.cos(az) * tangent0[k] + math.sin(az) * tangent1[k]
                        for k in range(3)
                    )
                    direction = tuple(ct * axis_dir[k] + st * tangent[k] for k in range(3))
                    raw.append({"dir": _unit(direction),
                                "label": f"{tag}+{tilt_deg}/az{az_deg:03d}"})

    raw.extend([
        {"dir": (0.0, 0.0, 1.0), "label": "top"},
        {"dir": (0.0, 0.0, -1.0), "label": "bottom"},
        {"dir": (0.0, -1.0, 0.0), "label": "front"},
        {"dir": (0.0, 1.0, 0.0), "label": "back"},
        {"dir": (1.0, 0.0, 0.0), "label": "right"},
        {"dir": (-1.0, 0.0, 0.0), "label": "left"},
    ])
    for sx, sy, sz in (
        (1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1),
        (-1, 1, 1), (-1, 1, -1), (-1, -1, 1), (-1, -1, -1),
    ):
        raw.append({"dir": _unit((sx, sy, sz)),
                    "label": f"iso{'+' if sx > 0 else '-'}x"
                             f"{'+' if sy > 0 else '-'}y"
                             f"{'+' if sz > 0 else '-'}z"})

    cosine_limit = math.cos(math.radians(_DEDUP_DEG))
    unique: list[dict] = []
    for item in raw:
        direction = item["dir"]
        if direction is None:
            continue
        if any(_dot(direction, kept["dir"]) >= cosine_limit for kept in unique):
            continue
        unique.append({"dir": tuple(direction), "label": item["label"]})
    return unique


def _target_mask(target_tris, triangle_count: int):
    if target_tris is None:
        return np.ones(triangle_count, dtype=bool)
    raw = np.asarray(target_tris)
    if raw.dtype == np.bool_:
        flat = raw.reshape(-1)
        if flat.size != triangle_count:
            raise ValueError("boolean target_tris must have one entry per mesh triangle")
        return flat.astype(bool, copy=False)
    mask = np.zeros(triangle_count, dtype=bool)
    ids = np.asarray(raw, dtype=np.int64).reshape(-1)
    ids = ids[(ids >= 0) & (ids < triangle_count)]
    mask[ids] = True
    return mask


def _screen_xyz(pos, frame: dict):
    rel = pos - np.asarray(frame["center"], dtype=np.float64)
    sx = ((rel @ np.asarray(frame["r"], dtype=np.float64)) - float(frame["cu"]))
    sx = sx * float(frame["scale"]) + int(frame["W"]) / 2.0
    sy = ((rel @ np.asarray(frame["u"], dtype=np.float64)) - float(frame["cv"]))
    sy = int(frame["H"]) / 2.0 - sy * float(frame["scale"])
    depth = rel @ np.asarray(frame["d"], dtype=np.float64)
    return np.stack((sx, sy, depth), axis=-1)


def _rasterize(screen, idx, triangle_ids, W: int, H: int, clip_depth=None):
    """Rasterize in mesh order with the engine's half-pixel barycentric span rule."""
    zbuf = np.full((H, W), -1e30, dtype=np.float64)
    winner = np.full((H, W), -1, dtype=np.int32)
    for tri_id in triangle_ids:
        t = int(tri_id)
        s0, s1, s2 = screen[idx[t, 0]], screen[idx[t, 1]], screen[idx[t, 2]]
        raw_minx = min(s0[0], s1[0], s2[0])
        raw_maxx = max(s0[0], s1[0], s2[0])
        raw_miny = min(s0[1], s1[1], s2[1])
        raw_maxy = max(s0[1], s1[1], s2[1])
        # Focused views routinely put most model triangles far outside the viewport.  Reject
        # those before constructing even a 1-D span, let alone a barycentric pixel grid.
        if raw_maxx < 0.0 or raw_minx > W or raw_maxy < 0.0 or raw_miny > H:
            continue
        minx = max(int(math.floor(raw_minx)), 0)
        maxx = min(int(math.ceil(raw_maxx)), W - 1)
        miny = max(int(math.floor(raw_miny)), 0)
        maxy = min(int(math.ceil(raw_maxy)), H - 1)
        if maxx < minx or maxy < miny:
            continue
        area = ((s1[0] - s0[0]) * (s2[1] - s0[1])
                - (s2[0] - s0[0]) * (s1[1] - s0[1]))
        if abs(area) < 1e-9:
            continue
        fx = np.arange(minx, maxx + 1, dtype=np.float64)[None, :] + 0.5
        fy = np.arange(miny, maxy + 1, dtype=np.float64)[:, None] + 0.5
        a0 = ((s1[0] - fx) * (s2[1] - fy) - (s2[0] - fx) * (s1[1] - fy)) / area
        a1 = ((s2[0] - fx) * (s0[1] - fy) - (s0[0] - fx) * (s2[1] - fy)) / area
        a2 = 1.0 - a0 - a1
        inside = (a0 >= 0.0) & (a1 >= 0.0) & (a2 >= 0.0)
        if not inside.any():
            continue
        depth = a0 * s0[2] + a1 * s1[2] + a2 * s2[2]
        if clip_depth is not None:
            inside = inside & (depth <= clip_depth)
            if not inside.any():
                continue
        zspan = zbuf[miny:maxy + 1, minx:maxx + 1]
        update = inside & (depth > zspan)
        if not update.any():
            continue
        zspan[update] = depth[update]
        winner[miny:maxy + 1, minx:maxx + 1][update] = t
    return zbuf, winner


def score_view(mesh: dict, target_tris, frame: dict, clip_near: bool = False) -> dict:
    """Rasterize one candidate and return measured target legibility metrics.

    `clip_near` reproduces the engine's cutaway: everything in front of the focus box is
    removed. Scoring an internal target WITHOUT it measures a picture nobody will ever be
    shown — a bore reads as heavily occluded from every angle except straight down its own
    axis, which is exactly the useless view."""
    empty = {"target_px": 0, "unoccluded_px": 0, "visible_frac": 0.0,
             "screen_frac": 0.0, "foreshorten": 0.0, "occluder_px": 0}
    if np is None or mesh is None:
        return empty

    pos = np.asarray(mesh["pos"], dtype=np.float64).reshape(-1, 3)
    idx = np.asarray(mesh["idx"], dtype=np.int64).reshape(-1, 3)
    if pos.size == 0 or idx.size == 0:
        return empty
    W, H = int(frame["W"]), int(frame["H"])
    target = _target_mask(target_tris, idx.shape[0])
    screen = _screen_xyz(pos, frame)

    p0, p1, p2 = pos[idx[:, 0]], pos[idx[:, 1]], pos[idx[:, 2]]
    crosses = np.cross(p1 - p0, p2 - p0)
    d = np.asarray(frame["d"], dtype=np.float64)
    front = (crosses @ d) > 0.0
    target_ids = np.flatnonzero(front & target)
    full_ids = np.flatnonzero(front)

    clip = float(frame["clip_depth"]) if (clip_near and frame.get("clip_depth") is not None) else None
    _target_z, target_winner = _rasterize(screen, idx, target_ids, W, H, clip)
    _full_z, full_winner = _rasterize(screen, idx, full_ids, W, H, clip)
    target_px = int(np.count_nonzero(target_winner >= 0))
    full_valid = full_winner >= 0
    visible = np.zeros((H, W), dtype=bool)
    visible[full_valid] = target[full_winner[full_valid]]
    unoccluded_px = int(np.count_nonzero(visible))

    foreshorten = 0.0
    if unoccluded_px:
        lengths = np.linalg.norm(crosses, axis=1)
        tri_cos = np.zeros(idx.shape[0], dtype=np.float64)
        good = lengths > 1e-15
        tri_cos[good] = np.abs(crosses[good] @ d) / lengths[good]
        # Pixels are equal projected-area samples, so this is the requested area-weighted mean
        # over visible target pixels (including partial visibility within a triangle).
        foreshorten = float(np.mean(tri_cos[full_winner[visible]]))

    visible_frac = unoccluded_px / max(target_px, 1)
    return {
        "target_px": target_px,
        "unoccluded_px": unoccluded_px,
        "visible_frac": float(visible_frac),
        "screen_frac": float(unoccluded_px / max(W * H, 1)),
        "foreshorten": foreshorten,
        "occluder_px": max(target_px - unoccluded_px, 0),
    }


# Visibility is a GATE, not a maximand. Ranking on raw visible area sounds right and is not:
# the view that exposes the most pixels of a bore is the one staring down its axis, and the
# view that exposes the most of a cut-open pocket is a flat orthographic section. Both are the
# least informative pictures available. Past this fraction the target is "seen"; the terms
# below then decide WHICH of the views that can see it reads best.
_VISIBLE_ENOUGH = 0.5
# A camera looking straight down a bore's (or a boss's) axis sees a filled circle: maximal
# target pixels, minimal information — the same failure mode as a dead-on view of a plane, and
# the one the foreshorten term cannot catch, because the far wall of a tube DOES face the
# camera. Penalise the last ~36 degrees of axial alignment instead.
_AXIS_VIEW_PENALTY = 4.0
_AXIS_VIEW_COS = 0.80
# An axis-aligned camera collapses one world dimension: front/top/left render a part as a flat
# 2-D silhouette with no depth cue at all. That is exactly what an ORTHO sheet is for, and
# exactly wrong for the single "show me this feature" close-up, so a near-principal direction
# is pushed below the three-quarter views. Full penalty within ~18 degrees of an axis.
_FLAT_VIEW_PENALTY = 6.0
_FLAT_VIEW_COS = 0.95
# Pure tie-break: a part is drawn from above by every convention there is, and a view from
# underneath inverts the renderer's +Z/-Z face tints for the reader.
_OVERHEAD_BONUS = 1.5


def _legibility_score(metrics: dict, direction=None, axis=None) -> float:
    """Stable scalar: a visibility gate first, then which visible view reads best."""
    seen = min(float(metrics["visible_frac"]) / _VISIBLE_ENOUGH, 1.0)
    screen = max(float(metrics["screen_frac"]), 0.0)
    foreshorten = float(metrics["foreshorten"])
    relief = math.exp(-0.5 * ((foreshorten - 0.85) / 0.22) ** 2)
    score = 1000.0 * seen + 12.0 * math.sqrt(screen) + 8.0 * relief
    if direction is not None:
        if axis is not None:
            align = abs(_dot(direction, axis))
            if align > _AXIS_VIEW_COS:
                score -= _AXIS_VIEW_PENALTY * (align - _AXIS_VIEW_COS) / (1.0 - _AXIS_VIEW_COS)
        flat = max(abs(float(direction[0])), abs(float(direction[1])), abs(float(direction[2])))
        if flat > _FLAT_VIEW_COS:
            score -= _FLAT_VIEW_PENALTY * (flat - _FLAT_VIEW_COS) / (1.0 - _FLAT_VIEW_COS)
        score += _OVERHEAD_BONUS * max(0.0, float(direction[2]))
    return score


def ideal_view_dirs(mesh, target_bbox, *, target_tris=None, normals=None, n=1, hint=None,
                    axis=None, clip_near=False, min_sep_deg=50.0,
                    W=160, H=128, margin=0.15) -> list[dict]:
    """Measure the lattice and return the best separated camera directions.

    The stable score is ``1000*min(visible_frac/0.5, 1) + 12*sqrt(screen_frac) + 8*G - A - F
    + U``, where ``G = exp(-0.5*((foreshorten-0.85)/0.22)**2)``, ``A`` penalises alignment with
    a revolved target's `axis` (down a bore is a filled circle), ``F`` penalises a
    principal-axis camera (a flat 2-D section with no depth cue), and ``U`` prefers a camera
    above the part. Visibility GATES — a target you cannot see loses outright — and the
    remaining terms decide which of the views that CAN see it reads best. Ranking on raw
    visible area instead picks the bore-axis stare and the flat section, because those expose
    the most pixels. Exact ties retain lattice order.

    `axis` is the target's revolve axis when it has one (cylinder/cone/torus faces, circular
    edges); pass None for planar targets, where the foreshorten term already does the work.

    `clip_near` MUST match how the caller will render. Score an internal target without it and
    every oblique camera measures as ~80% occluded while the useless straight-down-the-bore
    view measures as clear — the ranking then optimises for a picture of a filled circle.
    """
    candidates = candidate_dirs(target_bbox, hint=hint, normals=normals)
    count = max(int(n), 1)
    axis = _unit(axis) if axis is not None else None
    ranked: list[tuple[int, dict]] = []
    if mesh is None or np is None:
        ranked = [(order, {
            "dir": item["dir"], "label": item["label"], "score": None,
            "visible_frac": None, "screen_frac": None, "foreshorten": None,
            "target_px": None, "unoccluded_px": None,
        }) for order, item in enumerate(candidates)]
    else:
        for order, item in enumerate(candidates):
            frame = focus_frame(target_bbox, item["dir"], W, H, margin)
            metrics = score_view(mesh, target_tris, frame, clip_near=bool(clip_near))
            ranked.append((order, {
                "dir": item["dir"],
                "label": item["label"],
                "score": _legibility_score(metrics, direction=item["dir"], axis=axis),
                "visible_frac": metrics["visible_frac"],
                "screen_frac": metrics["screen_frac"],
                "foreshorten": metrics["foreshorten"],
                "target_px": metrics["target_px"],
                "unoccluded_px": metrics["unoccluded_px"],
            }))
        ranked.sort(key=lambda pair: (-pair[1]["score"], pair[0]))

    selected: list[dict] = []
    # Separation is measured on the UNDIRECTED axis (|dot|), so a camera 180 degrees from one
    # already chosen is rejected too: front and back of the same target are mirror images and
    # are the single most wasteful pair a multi-panel sheet can contain.
    cosine_limit = math.cos(math.radians(max(0.0, min(float(min_sep_deg), 90.0))))
    for _order, item in ranked:
        if any(abs(_dot(item["dir"], prior["dir"])) > cosine_limit for prior in selected):
            continue
        selected.append(item)
        if len(selected) >= count:
            break
    # Fewer than `n` views is the honest answer when `min_sep_deg` admits no more: padding the
    # tail would hand the caller a panel that duplicates one it already has while claiming the
    # separation held. Callers size their sheet from len(result).
    return selected
