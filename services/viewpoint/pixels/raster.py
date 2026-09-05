"""Small deterministic rasterizer using the exact viewpoint perspective camera model.

The projection, six-plane clipping, half-pixel edge test, reciprocal-depth z buffer,
Gouraud lighting and clean-shot colours intentionally mirror ``core/doc/session.cpp``'s
``render_view_to_rgb``.  This is the fast corpus renderer; it never sees feature or oracle data.
"""
from __future__ import annotations

from dataclasses import dataclass
import binascii
import hashlib
import struct
import zlib
import importlib
from typing import Sequence

import numpy as np

from services.viewpoint.geometry import CameraPose, Mesh

RASTERIZER_VERSION = "viewpoint-pixel-raster/1.2.0; engine-perspective-v1; cuda-batch-v1"
_BACKGROUND = np.asarray((245, 245, 245), dtype=np.uint8)
_BASE_COLOR = np.asarray((0.62, 0.70, 0.82), dtype=np.float64)
_LIGHT = np.asarray((0.4, 0.5, 1.0), dtype=np.float64)
_LIGHT /= np.linalg.norm(_LIGHT)


@dataclass(frozen=True, slots=True)
class RasterFrame:
    rgb: np.ndarray
    depth: np.ndarray
    depth_mask: np.ndarray


def _clip_polygon(polygon: list[np.ndarray], signed_distance) -> list[np.ndarray]:
    if not polygon:
        return []
    output: list[np.ndarray] = []
    previous = polygon[-1]
    previous_distance = float(signed_distance(previous))
    previous_inside = previous_distance >= 0.0
    for current in polygon:
        current_distance = float(signed_distance(current))
        current_inside = current_distance >= 0.0
        if current_inside != previous_inside:
            t = previous_distance / (previous_distance - current_distance)
            output.append(previous + t * (current - previous))
        if current_inside:
            output.append(current)
        previous = current
        previous_distance = current_distance
        previous_inside = current_inside
    return output


def _shading(mesh: Mesh, triangle_vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-triangle-corner diffuse terms plus the usable-triangle mask.

    The engine (``render_view_to_rgb``) shades Gouraud from the body's per-vertex ``nrm``
    whenever the tessellated body carries them.  A corpus mesh reaches the engine through the
    mesh-import lane, whose ``compute_mesh_smooth_normals`` (``core/doc/formats.cpp``) is
    reproduced here exactly: positions are welded into 1e-6 mm buckets, and corner ``k`` of
    triangle ``t`` takes the area-weighted sum (a raw cross product is already twice the
    area times the unit normal) of the triangles incident to that welded position whose unit
    normal is within the 40 degree crease angle of ``t``'s own — so a tessellated cylinder
    band shades smoothly while a hard edge keeps a distinct normal per side.  The engine
    stores those corner normals as float32; a corner whose sum cancels, and a degenerate
    triangle, keep the flat geometric term.
    """
    if len(mesh.triangles) == 0:
        return np.empty((0, 3), np.float64), np.empty(0, bool)
    raw_normals = np.cross(triangle_vertices[:, 1] - triangle_vertices[:, 0], triangle_vertices[:, 2] - triangle_vertices[:, 0])
    lengths = np.linalg.norm(raw_normals, axis=1)
    # The mesh-import lane drops a triangle whose cross product is shorter than 1e-12, so
    # such a triangle never reaches the engine's renderer at all.
    valid = lengths > 1e-12
    unit_normals = np.zeros_like(raw_normals)
    usable = lengths > 1e-30
    unit_normals[usable] = raw_normals[usable] / lengths[usable, None]

    scaled = mesh.vertices * 1e6
    keys = np.where(scaled >= 0.0, np.floor(scaled + 0.5), np.ceil(scaled - 0.5)).astype(np.int64)
    _, welded = np.unique(keys, axis=0, return_inverse=True)
    welded = welded.reshape(len(mesh.vertices))
    corner_position = welded[mesh.triangles]

    # CSR adjacency: the triangles incident to each welded position.
    triangle_of_corner = np.repeat(np.arange(len(mesh.triangles), dtype=np.int64), 3)
    order = np.argsort(corner_position.ravel(), kind="stable")
    incident = triangle_of_corner[order]
    counts = np.bincount(corner_position.ravel(), minlength=int(welded.max()) + 1)
    offsets = np.concatenate((np.zeros(1, np.int64), np.cumsum(counts)))

    # Expand every (triangle, corner) into its incident-triangle candidates.
    flat_positions = corner_position.ravel()
    candidate_counts = counts[flat_positions]
    owner = np.repeat(np.arange(len(flat_positions), dtype=np.int64), candidate_counts)
    starts = offsets[flat_positions]
    within = np.arange(len(owner), dtype=np.int64) - np.repeat(np.cumsum(candidate_counts) - candidate_counts, candidate_counts)
    neighbour = incident[starts[owner] + within]
    owner_triangle = triangle_of_corner[owner]
    cosine = np.einsum("ij,ij->i", unit_normals[owner_triangle], unit_normals[neighbour])
    keep = cosine >= np.cos(np.deg2rad(40.0))
    accumulated = np.zeros((len(flat_positions), 3), np.float64)
    np.add.at(accumulated, owner[keep], raw_normals[neighbour[keep]])

    accumulated_lengths = np.linalg.norm(accumulated, axis=1)
    smooth = (accumulated_lengths > 1e-30) & np.repeat(usable, 3)
    normals = np.repeat(unit_normals, 3, axis=0)
    # The engine keeps these corner normals as float32 and re-normalizes them as a gp_Dir.
    single = (accumulated[smooth] / accumulated_lengths[smooth, None]).astype(np.float32).astype(np.float64)
    normals[smooth] = single / np.linalg.norm(single, axis=1)[:, None]
    luminance = 0.35 + 0.65 * np.maximum(0.0, np.abs(normals @ _LIGHT))
    return luminance.reshape(len(mesh.triangles), 3), valid


def _camera_triangle(
    vertices: np.ndarray,
    luminance: np.ndarray,
    camera: CameraPose,
    *,
    right: np.ndarray | None = None,
    true_up: np.ndarray | None = None,
    forward: np.ndarray | None = None,
    tan_x: float | None = None,
    tan_y: float | None = None,
) -> list[np.ndarray]:
    """Clip one triangle after reusing the camera basis computed by its caller.

    Each polygon vertex is ``(camera_x, camera_y, camera_depth, diffuse_term)``; the diffuse
    term rides through clipping exactly like the engine's ``ProjectedVertex::lam``.
    """
    right = camera.right if right is None else right
    true_up = camera.true_up if true_up is None else true_up
    forward = camera.forward if forward is None else forward
    relative = vertices - camera.position
    polygon = [
        np.asarray((float(point @ right), float(point @ true_up), float(point @ forward), float(lam)))
        for point, lam in zip(relative, luminance)
    ]
    tan_y = float(np.tan(np.deg2rad(camera.vertical_fov_deg) * 0.5)) if tan_y is None else tan_y
    tan_x = tan_y * camera.image_width_px / camera.image_height_px if tan_x is None else tan_x
    planes = (
        lambda p: p[2] - camera.near,
        lambda p: camera.far - p[2],
        lambda p: p[0] + p[2] * tan_x,
        lambda p: p[2] * tan_x - p[0],
        lambda p: p[1] + p[2] * tan_y,
        lambda p: p[2] * tan_y - p[1],
    )
    for plane in planes:
        polygon = _clip_polygon(polygon, plane)
        if not polygon:
            break
    return polygon


def rasterize(mesh: Mesh, camera: CameraPose, *, resolution: tuple[int, int]) -> RasterFrame:
    """Render an opaque mesh without semantic/feature input.

    Depth is finite normalized optical-axis depth in ``[0, 1]``; background is zero and
    marked false in ``depth_mask``.  ``resolution`` may downsample the camera sensor while
    retaining the exact FOV, aspect and extrinsics.
    """
    width, height = (int(resolution[0]), int(resolution[1]))
    if width <= 0 or height <= 0 or abs(width / height - camera.image_width_px / camera.image_height_px) > 1e-9:
        raise ValueError("resolution must be positive and preserve the camera aspect ratio")
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[...] = _BACKGROUND
    reciprocal_depth = np.full((height, width), -np.inf, dtype=np.float64)
    tan_y = float(np.tan(np.deg2rad(camera.vertical_fov_deg) * 0.5))
    tan_x = tan_y * camera.image_width_px / camera.image_height_px
    scale = (height * 0.5) / tan_y
    right = camera.right
    true_up = camera.true_up
    forward = camera.forward

    triangle_vertices = mesh.vertices[mesh.triangles]
    luminance, valid_normals = _shading(mesh, triangle_vertices)

    def paint(p0: np.ndarray, p1: np.ndarray, p2: np.ndarray) -> None:
        projected = []
        for p in (p0, p1, p2):
            projected.append(np.asarray((p[0] * scale / p[2] + width * 0.5, height * 0.5 - p[1] * scale / p[2])))
        s0, s1, s2 = projected
        min_x = max(int(np.floor(min(s0[0], s1[0], s2[0]))), 0)
        max_x = min(int(np.ceil(max(s0[0], s1[0], s2[0]))), width - 1)
        min_y = max(int(np.floor(min(s0[1], s1[1], s2[1]))), 0)
        max_y = min(int(np.ceil(max(s0[1], s1[1], s2[1]))), height - 1)
        if min_x > max_x or min_y > max_y:
            return
        area = (s1[0] - s0[0]) * (s2[1] - s0[1]) - (s2[0] - s0[0]) * (s1[1] - s0[1])
        if abs(area) < 1e-9:
            return
        xs = np.arange(min_x, max_x + 1, dtype=np.float64) + 0.5
        ys = np.arange(min_y, max_y + 1, dtype=np.float64) + 0.5
        fx, fy = np.meshgrid(xs, ys)
        a0 = ((s1[0] - fx) * (s2[1] - fy) - (s2[0] - fx) * (s1[1] - fy)) / area
        a1 = ((s2[0] - fx) * (s0[1] - fy) - (s0[0] - fx) * (s2[1] - fy)) / area
        a2 = 1.0 - a0 - a1
        inside = (a0 >= 0.0) & (a1 >= 0.0) & (a2 >= 0.0)
        candidate = a0 / p0[2] + a1 / p1[2] + a2 / p2[2]
        zslice = reciprocal_depth[min_y : max_y + 1, min_x : max_x + 1]
        update = inside & (candidate > zslice)
        if not np.any(update):
            return
        zslice[update] = candidate[update]
        # Gouraud: the diffuse term is interpolated per pixel, the base colour stays flat.
        lam = a0 * p0[3] + a1 * p1[3] + a2 * p2[3]
        shaded = np.minimum(255.0, _BASE_COLOR[None, None, :] * 255.0 * lam[:, :, None]).astype(np.uint8)
        target = rgb[min_y : max_y + 1, min_x : max_x + 1]
        target[update] = shaded[update]

    for triangle, triangle_luminance, normal_ok in zip(triangle_vertices, luminance, valid_normals):
        if not normal_ok:
            continue
        polygon = _camera_triangle(
            triangle,
            triangle_luminance,
            camera,
            right=right,
            true_up=true_up,
            forward=forward,
            tan_x=tan_x,
            tan_y=tan_y,
        )
        for index in range(1, len(polygon) - 1):
            paint(polygon[0], polygon[index], polygon[index + 1])

    mask = np.isfinite(reciprocal_depth)
    depth = np.zeros((height, width), dtype=np.float32)
    optical_depth = np.zeros_like(reciprocal_depth)
    optical_depth[mask] = 1.0 / reciprocal_depth[mask]
    depth[mask] = np.clip((optical_depth[mask] - camera.near) / (camera.far - camera.near), 0.0, 1.0)
    return RasterFrame(rgb, depth, mask)


def _prepared_triangles(
    camera: CameraPose,
    triangle_vertices: np.ndarray,
    luminance: np.ndarray,
    valid_normals: np.ndarray,
) -> tuple[np.ndarray, float]:
    """Clipped ``(x, y, depth, diffuse)`` triangles for one candidate camera."""
    tan_y = float(np.tan(np.deg2rad(camera.vertical_fov_deg) * 0.5))
    tan_x = tan_y * camera.image_width_px / camera.image_height_px
    right = camera.right
    true_up = camera.true_up
    forward = camera.forward
    clipped: list[np.ndarray] = []
    for triangle, triangle_luminance, normal_ok in zip(triangle_vertices, luminance, valid_normals):
        if not normal_ok:
            continue
        polygon = _camera_triangle(
            triangle,
            triangle_luminance,
            camera,
            right=right,
            true_up=true_up,
            forward=forward,
            tan_x=tan_x,
            tan_y=tan_y,
        )
        for index in range(1, len(polygon) - 1):
            clipped.append(np.stack((polygon[0], polygon[index], polygon[index + 1])))
    if not clipped:
        return np.empty((0, 3, 4), np.float64), (camera.image_height_px * 0.5) / tan_y
    return np.stack(clipped), (camera.image_height_px * 0.5) / tan_y


def _rasterize_many_cuda(
    mesh: Mesh,
    cameras: Sequence[CameraPose],
    *,
    resolution: tuple[int, int],
    torch,
) -> tuple[RasterFrame, ...]:
    """Batch clipped triangles over candidates; Torch stays lazy and GPU-resident."""
    width, height = (int(resolution[0]), int(resolution[1]))
    if width <= 0 or height <= 0:
        raise ValueError("resolution must be positive")
    for camera in cameras:
        if abs(width / height - camera.image_width_px / camera.image_height_px) > 1e-9:
            raise ValueError("resolution must preserve every camera aspect ratio")
    triangle_vertices = mesh.vertices[mesh.triangles]
    luminance, valid_normals = _shading(mesh, triangle_vertices)
    prepared = [_prepared_triangles(camera, triangle_vertices, luminance, valid_normals) for camera in cameras]
    max_triangles = max((len(item[0]) for item in prepared), default=0)
    batch = len(cameras)
    if batch == 0:
        return ()
    vertices = np.zeros((batch, max_triangles, 3, 4), np.float64)
    valid = np.zeros((batch, max_triangles), bool)
    scales = np.empty(batch, np.float64)
    for index, (candidate_vertices, camera_scale) in enumerate(prepared):
        count = len(candidate_vertices)
        vertices[index, :count] = candidate_vertices
        valid[index, :count] = True
        scales[index] = camera_scale * height / cameras[index].image_height_px

    device = torch.device("cuda")
    vertex_tensor = torch.from_numpy(vertices).to(device=device)
    base_color = torch.as_tensor(_BASE_COLOR, dtype=torch.float64, device=device)
    valid_tensor = torch.from_numpy(valid).to(device=device)
    scale_tensor = torch.from_numpy(scales).to(device=device)[:, None, None]
    reciprocal_depth = torch.full((batch, height, width), -torch.inf, dtype=torch.float64, device=device)
    rgb = torch.empty((batch, height, width, 3), dtype=torch.uint8, device=device)
    rgb[...] = torch.as_tensor(_BACKGROUND, dtype=torch.uint8, device=device)
    fx = (torch.arange(width, dtype=torch.float64, device=device) + 0.5)[None, None, None, :]
    fy = (torch.arange(height, dtype=torch.float64, device=device) + 0.5)[None, None, :, None]
    triangle_chunk = 32
    for start in range(0, max_triangles, triangle_chunk):
        stop = min(start + triangle_chunk, max_triangles)
        points = vertex_tensor[:, start:stop]
        z = points[..., 2]
        sx = points[..., 0] * scale_tensor / z + width * 0.5
        sy = height * 0.5 - points[..., 1] * scale_tensor / z
        s0x, s1x, s2x = sx.unbind(dim=2)
        s0y, s1y, s2y = sy.unbind(dim=2)
        area = (s1x - s0x) * (s2y - s0y) - (s2x - s0x) * (s1y - s0y)
        area4 = area[..., None, None]
        a0 = ((s1x[..., None, None] - fx) * (s2y[..., None, None] - fy) - (s2x[..., None, None] - fx) * (s1y[..., None, None] - fy)) / area4
        a1 = ((s2x[..., None, None] - fx) * (s0y[..., None, None] - fy) - (s0x[..., None, None] - fx) * (s2y[..., None, None] - fy)) / area4
        a2 = 1.0 - a0 - a1
        inside = (a0 >= 0.0) & (a1 >= 0.0) & (a2 >= 0.0)
        inside &= valid_tensor[:, start:stop, None, None] & (torch.abs(area4) >= 1.0e-9)
        candidate = a0 / z[:, :, 0, None, None] + a1 / z[:, :, 1, None, None] + a2 / z[:, :, 2, None, None]
        lam = points[..., 3]
        # Gouraud: the per-pixel diffuse term of whichever triangle wins this chunk's depth test.
        pixel_lam = a0 * lam[:, :, 0, None, None] + a1 * lam[:, :, 1, None, None] + a2 * lam[:, :, 2, None, None]
        candidate = torch.where(inside, candidate, -torch.inf)
        chunk_depth, winner = torch.max(candidate, dim=1)
        update = chunk_depth > reciprocal_depth
        reciprocal_depth = torch.where(update, chunk_depth, reciprocal_depth)
        winner_lam = torch.gather(pixel_lam, 1, winner[:, None]).squeeze(1)
        winner_color = torch.clamp(base_color * 255.0 * winner_lam[..., None], max=255.0).to(torch.uint8)
        rgb = torch.where(update[..., None], winner_color, rgb)

    mask = torch.isfinite(reciprocal_depth)
    near = torch.as_tensor([camera.near for camera in cameras], dtype=torch.float64, device=device)[:, None, None]
    far = torch.as_tensor([camera.far for camera in cameras], dtype=torch.float64, device=device)[:, None, None]
    optical_depth = torch.where(mask, 1.0 / reciprocal_depth, 0.0)
    depth = torch.where(mask, torch.clamp((optical_depth - near) / (far - near), 0.0, 1.0), 0.0).to(torch.float32)
    rgb_cpu = rgb.cpu().numpy()
    depth_cpu = depth.cpu().numpy()
    mask_cpu = mask.cpu().numpy()
    return tuple(RasterFrame(rgb_cpu[index], depth_cpu[index], mask_cpu[index]) for index in range(batch))


def rasterize_many(
    mesh: Mesh,
    cameras: Sequence[CameraPose],
    *,
    resolution: tuple[int, int],
    device: str = "auto",
) -> tuple[RasterFrame, ...]:
    """Render candidate cameras together on CUDA, with a Torch-free exact CPU fallback."""
    if device not in ("auto", "cuda", "cpu", "numpy"):
        raise ValueError("device must be auto, cuda, cpu, or numpy")
    torch = None
    if device in ("auto", "cuda"):
        try:
            torch = importlib.import_module("torch")
        except ImportError:
            if device == "cuda":
                raise RuntimeError("CUDA rasterization requested but Torch is unavailable") from None
        if torch is not None and not torch.cuda.is_available():
            if device == "cuda":
                raise RuntimeError("CUDA rasterization requested but CUDA is unavailable")
            torch = None
    if torch is None:
        return tuple(rasterize(mesh, camera, resolution=resolution) for camera in cameras)
    return _rasterize_many_cuda(mesh, cameras, resolution=resolution, torch=torch)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


def encode_png(rgb: np.ndarray) -> bytes:
    """Encode RGB8 with deterministic filter-0 rows using only the standard library."""
    image = np.ascontiguousarray(rgb, dtype=np.uint8)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("rgb must have shape (H, W, 3)")
    height, width = image.shape[:2]
    scanlines = b"".join(b"\0" + image[row].tobytes() for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(scanlines, 6)) + _chunk(b"IEND", b"")


def decode_png(content: bytes) -> np.ndarray:
    """Decode PNGs produced by :func:`encode_png`; rejects any expanded capability."""
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG")
    cursor = 8
    width = height = 0
    compressed = bytearray()
    while cursor < len(content):
        length = struct.unpack(">I", content[cursor : cursor + 4])[0]
        kind = content[cursor + 4 : cursor + 8]
        payload = content[cursor + 8 : cursor + 8 + length]
        cursor += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                raise ValueError("unsupported PNG format")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    raw = zlib.decompress(bytes(compressed))
    stride = width * 3
    if len(raw) != height * (stride + 1) or any(raw[row * (stride + 1)] != 0 for row in range(height)):
        raise ValueError("PNG is not the corpus filter-0 RGB encoding")
    return np.stack([np.frombuffer(raw[row * (stride + 1) + 1 : (row + 1) * (stride + 1)], np.uint8).reshape(width, 3) for row in range(height)])


def image_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


__all__ = [
    "RASTERIZER_VERSION",
    "RasterFrame",
    "decode_png",
    "encode_png",
    "image_digest",
    "rasterize",
    "rasterize_many",
]
