"""Small deterministic rasterizer using the exact viewpoint perspective camera model.

The projection, six-plane clipping, half-pixel edge test, reciprocal-depth z buffer,
lighting and clean-shot colours intentionally mirror ``core/doc/session.cpp``'s
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

RASTERIZER_VERSION = "viewpoint-pixel-raster/1.1.0; engine-perspective-v1; cuda-batch-v1"
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


def _camera_triangle(
    vertices: np.ndarray,
    camera: CameraPose,
    *,
    right: np.ndarray | None = None,
    true_up: np.ndarray | None = None,
    forward: np.ndarray | None = None,
    tan_x: float | None = None,
    tan_y: float | None = None,
) -> list[np.ndarray]:
    """Clip one triangle after reusing the camera basis computed by its caller."""
    right = camera.right if right is None else right
    true_up = camera.true_up if true_up is None else true_up
    forward = camera.forward if forward is None else forward
    relative = vertices - camera.position
    polygon = [
        np.asarray((float(point @ right), float(point @ true_up), float(point @ forward)))
        for point in relative
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
    raw_normals = np.cross(triangle_vertices[:, 1] - triangle_vertices[:, 0], triangle_vertices[:, 2] - triangle_vertices[:, 0])
    lengths = np.linalg.norm(raw_normals, axis=1)
    valid_normals = lengths > 1e-12
    normals = np.zeros_like(raw_normals)
    normals[valid_normals] = raw_normals[valid_normals] / lengths[valid_normals, None]
    luminance = 0.35 + 0.65 * np.maximum(0.0, np.abs(normals @ _LIGHT))
    colors = np.minimum(255.0, _BASE_COLOR[None, :] * 255.0 * luminance[:, None]).astype(np.uint8)

    def paint(p0: np.ndarray, p1: np.ndarray, p2: np.ndarray, color: np.ndarray) -> None:
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
        target = rgb[min_y : max_y + 1, min_x : max_x + 1]
        target[update] = color

    for triangle, color, normal_ok in zip(triangle_vertices, colors, valid_normals):
        if not normal_ok:
            continue
        polygon = _camera_triangle(
            triangle,
            camera,
            right=right,
            true_up=true_up,
            forward=forward,
            tan_x=tan_x,
            tan_y=tan_y,
        )
        for index in range(1, len(polygon) - 1):
            paint(polygon[0], polygon[index], polygon[index + 1], color)

    mask = np.isfinite(reciprocal_depth)
    depth = np.zeros((height, width), dtype=np.float32)
    optical_depth = np.zeros_like(reciprocal_depth)
    optical_depth[mask] = 1.0 / reciprocal_depth[mask]
    depth[mask] = np.clip((optical_depth[mask] - camera.near) / (camera.far - camera.near), 0.0, 1.0)
    return RasterFrame(rgb, depth, mask)


def _prepared_triangles(
    camera: CameraPose,
    triangle_vertices: np.ndarray,
    colors: np.ndarray,
    valid_normals: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float]:
    tan_y = float(np.tan(np.deg2rad(camera.vertical_fov_deg) * 0.5))
    tan_x = tan_y * camera.image_width_px / camera.image_height_px
    right = camera.right
    true_up = camera.true_up
    forward = camera.forward
    clipped: list[np.ndarray] = []
    clipped_colors: list[np.ndarray] = []
    for triangle, color, normal_ok in zip(triangle_vertices, colors, valid_normals):
        if not normal_ok:
            continue
        polygon = _camera_triangle(
            triangle,
            camera,
            right=right,
            true_up=true_up,
            forward=forward,
            tan_x=tan_x,
            tan_y=tan_y,
        )
        for index in range(1, len(polygon) - 1):
            clipped.append(np.stack((polygon[0], polygon[index], polygon[index + 1])))
            clipped_colors.append(color)
    if not clipped:
        return np.empty((0, 3, 3), np.float64), np.empty((0, 3), np.uint8), (camera.image_height_px * 0.5) / tan_y
    return np.stack(clipped), np.stack(clipped_colors), (camera.image_height_px * 0.5) / tan_y


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
    raw_normals = np.cross(triangle_vertices[:, 1] - triangle_vertices[:, 0], triangle_vertices[:, 2] - triangle_vertices[:, 0])
    lengths = np.linalg.norm(raw_normals, axis=1)
    valid_normals = lengths > 1e-12
    normals = np.zeros_like(raw_normals)
    normals[valid_normals] = raw_normals[valid_normals] / lengths[valid_normals, None]
    luminance = 0.35 + 0.65 * np.maximum(0.0, np.abs(normals @ _LIGHT))
    colors = np.minimum(255.0, _BASE_COLOR[None, :] * 255.0 * luminance[:, None]).astype(np.uint8)
    prepared = [_prepared_triangles(camera, triangle_vertices, colors, valid_normals) for camera in cameras]
    max_triangles = max((len(item[0]) for item in prepared), default=0)
    batch = len(cameras)
    if batch == 0:
        return ()
    vertices = np.zeros((batch, max_triangles, 3, 3), np.float64)
    triangle_colors = np.zeros((batch, max_triangles, 3), np.uint8)
    valid = np.zeros((batch, max_triangles), bool)
    scales = np.empty(batch, np.float64)
    for index, (candidate_vertices, candidate_colors, camera_scale) in enumerate(prepared):
        count = len(candidate_vertices)
        vertices[index, :count] = candidate_vertices
        triangle_colors[index, :count] = candidate_colors
        valid[index, :count] = True
        scales[index] = camera_scale * height / cameras[index].image_height_px

    device = torch.device("cuda")
    vertex_tensor = torch.from_numpy(vertices).to(device=device)
    color_tensor = torch.from_numpy(triangle_colors).to(device=device)
    valid_tensor = torch.from_numpy(valid).to(device=device)
    scale_tensor = torch.from_numpy(scales).to(device=device)[:, None, None]
    reciprocal_depth = torch.full((batch, height, width), -torch.inf, dtype=torch.float64, device=device)
    rgb = torch.empty((batch, height, width, 3), dtype=torch.uint8, device=device)
    rgb[...] = torch.as_tensor(_BACKGROUND, dtype=torch.uint8, device=device)
    fx = (torch.arange(width, dtype=torch.float64, device=device) + 0.5)[None, None, None, :]
    fy = (torch.arange(height, dtype=torch.float64, device=device) + 0.5)[None, None, :, None]
    batch_indices = torch.arange(batch, device=device)[:, None, None]
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
        candidate = torch.where(inside, candidate, -torch.inf)
        chunk_depth, winner = torch.max(candidate, dim=1)
        update = chunk_depth > reciprocal_depth
        reciprocal_depth = torch.where(update, chunk_depth, reciprocal_depth)
        winner_color = color_tensor[:, start:stop][batch_indices, winner]
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
