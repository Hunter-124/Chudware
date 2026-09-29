"""Software iso render of a triangle mesh — the thumbnail fallback when the engine can't.

The engine's product shot needs a live document session; a ``.step+`` whose components
cannot be resolved (an assembly moved into an archive folder), a bare STL, or a box
without the engine still has a display mesh (``cache.aicm`` / the STL triangles). This
rasterises that mesh with the engine's iso camera (scene→camera direction (1, 1, 0.8), Z
up), flat Lambert shading in the body's own colour, a z-buffer, and dark silhouette /
crease lines, at 2x supersampling. Pure numpy + Pillow; ~0.3 s for 50k triangles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np

BACKGROUND = (245, 245, 245)
DEFAULT_COLOR = (0.62, 0.70, 0.82)
_VIEW_DIR = np.array([1.0, 1.0, 0.8]) / np.linalg.norm([1.0, 1.0, 0.8])
_LIGHT = np.array([0.35, 0.55, 1.0]) / np.linalg.norm([0.35, 0.55, 1.0])
_EDGE = np.array([58, 64, 76], dtype=np.float64)
_CHUNK = 4_000_000  # candidate pixels per vectorised batch


def render_mesh_png(pos: np.ndarray, tris: np.ndarray, colors: np.ndarray, out: str | Path,
                    size: tuple[int, int] = (512, 400)) -> None:
    """Render ``pos`` (N,3) / ``tris`` (M,3) with per-triangle ``colors`` (M,3, 0..1)."""
    from PIL import Image

    width, height = size
    ss = 2 if len(tris) <= 400_000 else 1
    rgb = _rasterize(np.asarray(pos, np.float64), np.asarray(tris, np.int64),
                     np.asarray(colors, np.float64), width * ss, height * ss)
    image = Image.fromarray(rgb, "RGB")
    if ss > 1:
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    image.save(out, format="PNG")


def body_mesh(bodies: Sequence[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Concatenate ``read_aicm`` bodies into (pos, tris, per-triangle colours)."""
    pos, tris, colors, offset = [], [], [], 0
    for body in bodies:
        if not body.get("n_tris"):
            continue
        p = np.asarray(body["pos"], np.float64)
        t = np.asarray(body["idx"], np.int64) + offset
        c = body.get("color") or DEFAULT_COLOR
        pos.append(p)
        tris.append(t)
        colors.append(np.tile(np.asarray(c, np.float64), (len(t), 1)))
        offset += len(p)
    if not pos:
        raise ValueError("mesh has no triangles")
    return np.concatenate(pos), np.concatenate(tris), np.concatenate(colors)


def _camera() -> tuple[np.ndarray, np.ndarray]:
    forward = -_VIEW_DIR
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    return right, up


def _rasterize(pos, tris, colors, width, height) -> np.ndarray:
    right, up = _camera()
    sx, sy, depth = pos @ right, pos @ up, pos @ _VIEW_DIR
    span = max(sx.max() - sx.min(), 1e-9) / (width * 0.88), max(sy.max() - sy.min(), 1e-9) / (height * 0.88)
    scale = 1.0 / max(span)
    px = (sx - (sx.max() + sx.min()) / 2) * scale + width / 2
    py = height / 2 - (sy - (sy.max() + sy.min()) / 2) * scale

    corners = pos[tris]
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    length = np.linalg.norm(normals, axis=1)
    keep = length > 1e-12
    tris, colors, normals = tris[keep], colors[keep], normals[keep] / length[keep, None]
    normals[normals @ _VIEW_DIR < 0] *= -1  # two-sided: shade the side facing the camera
    shade = 0.38 + 0.62 * np.clip(normals @ _LIGHT, 0.0, 1.0) + 0.10 * (normals @ _VIEW_DIR)
    tri_rgb = np.clip(colors * shade[:, None], 0.0, 1.0) * 255.0

    x, y, z = (a.astype(np.float32) for a in (px[tris], py[tris], depth[tris]))
    x0 = np.clip(np.floor(x.min(axis=1)).astype(np.int64), 0, width - 1)
    x1 = np.clip(np.ceil(x.max(axis=1)).astype(np.int64), 0, width - 1)
    y0 = np.clip(np.floor(y.min(axis=1)).astype(np.int64), 0, height - 1)
    y1 = np.clip(np.ceil(y.max(axis=1)).astype(np.int64), 0, height - 1)
    # Group triangles by their bbox rounded up to powers of two PER AXIS, so a long thin
    # sliver is tested over its own rectangle, not the square of its longest side.
    kx = _pow2(x1 - x0 + 1)
    ky = _pow2(y1 - y0 + 1)

    zbuf = np.full(width * height, -np.inf)
    owner = np.full(width * height, -1, dtype=np.int64)
    klass = kx * 65536 + ky
    order = np.argsort(klass, kind="stable")
    bounds = np.flatnonzero(np.r_[True, klass[order][1:] != klass[order][:-1], True])
    for begin, end in zip(bounds[:-1], bounds[1:]):
        pick = order[begin:end]
        wx, wy = int(kx[pick[0]]), int(ky[pick[0]])
        oy, ox = np.divmod(np.arange(wx * wy), wx)
        step = max(1, _CHUNK // (wx * wy))
        for start in range(0, len(pick), step):
            idx = pick[start:start + step]
            _raster_batch(idx, ox, oy, x, y, z, x0, x1, y0, y1, width, zbuf, owner)

    image = np.empty((width * height, 3))
    image[:] = BACKGROUND
    hit = owner >= 0
    image[hit] = tri_rgb[owner[hit]]
    image = image.reshape(height, width, 3)
    _draw_edges(image, owner.reshape(height, width), zbuf.reshape(height, width),
                normals, scale)
    return np.clip(image, 0, 255).astype(np.uint8)


def _pow2(values: np.ndarray) -> np.ndarray:
    return (1 << np.ceil(np.log2(np.maximum(values, 1))).astype(np.int64)).astype(np.int64)


def _raster_batch(idx, ox, oy, x, y, z, x0, x1, y0, y1, width, zbuf, owner) -> None:
    cx = x0[idx, None] + ox[None, :]
    cy = y0[idx, None] + oy[None, :]
    inside_box = (cx <= x1[idx, None]) & (cy <= y1[idx, None])
    px, py = cx + 0.5, cy + 0.5
    ax, ay = x[idx, 0, None], y[idx, 0, None]
    bx, by = x[idx, 1, None], y[idx, 1, None]
    qx, qy = x[idx, 2, None], y[idx, 2, None]
    area = (bx - ax) * (qy - ay) - (by - ay) * (qx - ax)
    w0 = (bx - px) * (qy - py) - (by - py) * (qx - px)
    w1 = (qx - px) * (ay - py) - (qy - py) * (ax - px)
    w2 = (ax - px) * (by - py) - (ay - py) * (bx - px)
    sign = np.sign(area)
    inside = inside_box & (w0 * sign >= 0) & (w1 * sign >= 0) & (w2 * sign >= 0) & (area != 0)
    if not inside.any():
        return
    safe = np.where(area == 0, 1.0, area)
    depth = (w0 * z[idx, 0, None] + w1 * z[idx, 1, None] + w2 * z[idx, 2, None]) / safe
    rows, cols = np.nonzero(inside)
    pixel = cy[rows, cols] * width + cx[rows, cols]
    d = depth[rows, cols]
    tri = idx[rows]
    order = np.lexsort((-d, pixel))
    pixel, d, tri = pixel[order], d[order], tri[order]
    first = np.flatnonzero(np.r_[True, pixel[1:] != pixel[:-1]])
    pixel, d, tri = pixel[first], d[first], tri[first]
    better = d > zbuf[pixel]
    zbuf[pixel[better]] = d[better]
    owner[pixel[better]] = tri[better]


def _draw_edges(image, owner, zbuf, normals, scale) -> None:
    """Darken silhouettes (part vs background), depth steps and creases > ~35°."""
    fg = owner >= 0
    edge = np.zeros_like(fg)
    depth_step = 1.5 / max(scale, 1e-9) * 2.0  # ~2 px of model distance
    for dy, dx in ((0, 1), (1, 0)):
        a = (slice(None, owner.shape[0] - dy), slice(None, owner.shape[1] - dx))
        b = (slice(dy, None), slice(dx, None))
        fa, fb = fg[a], fg[b]
        silhouette = fa != fb
        both = fa & fb
        za, zb = np.where(both, zbuf[a], 0.0), np.where(both, zbuf[b], 0.0)
        step = both & (np.abs(za - zb) > depth_step)
        na = normals[np.where(both, owner[a], 0)]
        nb = normals[np.where(both, owner[b], 0)]
        crease = both & ((na * nb).sum(axis=-1) < 0.82)
        hit = silhouette | step | crease
        edge[a] |= hit
        edge[b] |= hit & ~fg[b] & False  # silhouettes stay one pixel, on the part side
    image[edge & fg] = image[edge & fg] * 0.25 + _EDGE * 0.75
