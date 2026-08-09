"""Batched Torch accelerator for the frozen NumPy viewpoint oracle.

Only the profiled dominant operation -- Möller--Trumbore ray/triangle testing used by
visibility, silhouette probes, and ambiguity signatures -- moves to Torch.  Polygon
clipping, convex hull construction, projection, and label assembly deliberately reuse the
frozen NumPy reference so the accelerator does not create a second oracle definition.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import os
from typing import Literal, Sequence

import numpy as np

from ..geometry import CameraPose, Feature, Scene, project_points
from ..oracle import (
    InspectabilityLabel,
    OracleConfig,
    _apparent_scale,
    _convex_hull,
    _foreshortening,
    _in_frame_coverage,
    _pixel_ray,
    _ray_hits,
    _surface_samples,
    aggregate_quality,
    evaluate,
)

DeviceRequest = Literal["auto", "cuda", "cpu", "numpy"]


@dataclass(frozen=True)
class BackendInfo:
    """Observed execution backend and adaptive ray chunk chosen for one call."""

    backend: Literal["cuda", "torch-cpu", "numpy"]
    device: str
    ray_count: int
    triangle_count: int
    ray_chunk_size: int
    repaired_boundary_rays: int


def _load_torch():
    """Import Torch lazily; absence or a broken optional install means NumPy fallback."""

    try:
        return importlib.import_module("torch")
    except (ImportError, OSError):
        return None


def _resolve_device(request: DeviceRequest):
    if request not in ("auto", "cuda", "cpu", "numpy"):
        raise ValueError("device must be 'auto', 'cuda', 'cpu', or 'numpy'")
    if request == "numpy":
        return None, None, "numpy"
    torch = _load_torch()
    if torch is None:
        return None, None, "numpy"
    if request in ("auto", "cuda") and torch.cuda.is_available():
        return torch, torch.device("cuda"), "cuda"
    return torch, torch.device("cpu"), "torch-cpu"


def _available_host_memory() -> int:
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return max(1, int(pages) * int(page_size))
    except (ValueError, OSError):
        return 512 * 1024 * 1024


def _adaptive_ray_chunk(torch, device, triangle_count: int, ray_count: int) -> int:
    """Size a ray chunk from currently free memory and the kernel's live tensors.

    Each ray/triangle pair has five 3-vector temporaries, five float64 scalars, and
    comparison masks.  Reserving only one quarter of currently free memory leaves room for
    Torch/CUDA allocator fragmentation and all non-kernel tensors.
    """

    if ray_count == 0:
        return 0
    if triangle_count == 0:
        return ray_count
    if device.type == "cuda":
        free_bytes, _ = torch.cuda.mem_get_info(device)
    else:
        free_bytes = _available_host_memory()
    float_bytes = torch.empty((), dtype=torch.float64).element_size()
    bool_bytes = torch.empty((), dtype=torch.bool).element_size()
    bytes_per_pair = (5 * 3 + 5) * float_bytes + 8 * bool_bytes
    memory_budget = max(bytes_per_pair * triangle_count, int(free_bytes) // 4)
    by_memory = max(1, memory_budget // (bytes_per_pair * triangle_count))
    return min(ray_count, int(by_memory))


@dataclass
class _RayBatch:
    origins: list[np.ndarray]
    directions: list[np.ndarray]
    minimums: list[float]
    maximums: list[float]
    excluded_features: list[int]

    @classmethod
    def empty(cls) -> "_RayBatch":
        return cls([], [], [], [], [])

    def add(
        self,
        origin: np.ndarray,
        direction: np.ndarray,
        minimum: float,
        maximum: float,
        excluded_feature: int,
    ) -> int:
        index = len(self.origins)
        self.origins.append(origin)
        self.directions.append(direction)
        self.minimums.append(float(minimum))
        self.maximums.append(float(maximum))
        self.excluded_features.append(excluded_feature)
        return index


def _torch_ray_hits(
    scene: Scene,
    rays: _RayBatch,
    feature_triangle_mask: np.ndarray,
    epsilon: float,
    torch,
    device,
) -> tuple[np.ndarray, int, int]:
    ray_count = len(rays.origins)
    triangle_array = scene.mesh.vertices[scene.mesh.triangles]
    triangle_count = len(triangle_array)
    if ray_count == 0 or triangle_count == 0:
        return np.zeros(ray_count, dtype=bool), ray_count, 0

    triangles = torch.as_tensor(triangle_array, dtype=torch.float64, device=device)
    edge1 = triangles[:, 1] - triangles[:, 0]
    edge2 = triangles[:, 2] - triangles[:, 0]
    origins = torch.as_tensor(np.asarray(rays.origins), dtype=torch.float64, device=device)
    directions = torch.as_tensor(np.asarray(rays.directions), dtype=torch.float64, device=device)
    minimums = torch.as_tensor(rays.minimums, dtype=torch.float64, device=device)
    maximums = torch.as_tensor(rays.maximums, dtype=torch.float64, device=device)
    excluded = torch.as_tensor(feature_triangle_mask, dtype=torch.bool, device=device)
    excluded_features = torch.as_tensor(rays.excluded_features, dtype=torch.int64, device=device)

    chunk_size = _adaptive_ray_chunk(torch, device, triangle_count, ray_count)
    output = np.zeros(ray_count, dtype=bool)
    boundary_rays: list[int] = []
    # The repair band covers only numerically undecidable comparisons.  Normal rays remain
    # entirely on the selected Torch device; exact-boundary/grazing rays are resolved by
    # the frozen NumPy predicate rather than allowing backend rounding to change a label.
    repair_atol = 64.0 * np.finfo(np.float64).eps

    with torch.inference_mode():
        for start in range(0, ray_count, chunk_size):
            stop = min(start + chunk_size, ray_count)
            origin = origins[start:stop]
            direction = directions[start:stop]
            pvec = torch.linalg.cross(direction[:, None, :].expand(-1, triangle_count, -1), edge2[None, :, :])
            determinant = torch.sum(edge1[None, :, :] * pvec, dim=2)
            active = torch.abs(determinant) > epsilon
            inverse = torch.where(active, determinant.reciprocal(), torch.zeros_like(determinant))
            tvec = origin[:, None, :] - triangles[None, :, 0, :]
            u = torch.sum(tvec * pvec, dim=2) * inverse
            qvec = torch.linalg.cross(tvec, edge1[None, :, :])
            v = torch.sum(qvec * direction[:, None, :], dim=2) * inverse
            distance = torch.sum(edge2[None, :, :] * qvec, dim=2) * inverse
            hit = active & (u >= -epsilon) & (v >= -epsilon) & (u + v <= 1.0 + epsilon)
            hit &= (distance >= minimums[start:stop, None]) & (distance <= maximums[start:stop, None])
            feature_rows = excluded_features[start:stop]
            has_exclusion = feature_rows >= 0
            if bool(torch.any(has_exclusion)):
                hit[has_exclusion] &= ~excluded[feature_rows[has_exclusion]]
            output[start:stop] = torch.any(hit, dim=1).cpu().numpy()

            scale = 1.0 + torch.maximum(torch.abs(distance), torch.abs(determinant))
            band = repair_atol * scale
            uncertain = (torch.abs(torch.abs(determinant) - epsilon) <= band)
            uncertain |= active & (torch.abs(u + epsilon) <= band)
            uncertain |= active & (torch.abs(v + epsilon) <= band)
            uncertain |= active & (torch.abs((u + v) - (1.0 + epsilon)) <= band)
            uncertain |= active & (torch.abs(distance - minimums[start:stop, None]) <= band)
            uncertain |= active & (torch.abs(distance - maximums[start:stop, None]) <= band)
            uncertain |= ~torch.isfinite(determinant) | ~torch.isfinite(u) | ~torch.isfinite(v) | ~torch.isfinite(distance)
            local = torch.nonzero(torch.any(uncertain, dim=1), as_tuple=False).flatten().cpu().tolist()
            boundary_rays.extend(start + int(index) for index in local)

    for ray_index in boundary_rays:
        feature_index = rays.excluded_features[ray_index]
        if feature_index >= 0:
            triangle_subset = triangle_array[~feature_triangle_mask[feature_index]]
        else:
            triangle_subset = triangle_array
        output[ray_index] = _ray_hits(
            rays.origins[ray_index],
            rays.directions[ray_index],
            triangle_subset,
            minimum=rays.minimums[ray_index],
            maximum=rays.maximums[ray_index],
            epsilon=epsilon,
        )
    return output, chunk_size, len(boundary_rays)


def _signature_without_occlusion(scene: Scene, feature: Feature, camera: CameraPose):
    projection = project_points(scene.feature_points(feature), camera)
    valid = projection.in_front
    pixels = projection.pixels[valid]
    depth = projection.depth[valid]
    if len(pixels) == 0:
        empty = np.empty(0, dtype=np.float64)
        return empty, empty, empty
    centered = pixels - pixels.mean(axis=0)
    radii = np.sort(np.linalg.norm(centered, axis=1))
    pairwise = np.linalg.norm(pixels[:, None, :] - pixels[None, :, :], axis=2)
    pairwise = np.sort(pairwise[np.triu_indices(len(pixels), k=1)])
    focal_px = camera.image_height_px / (2.0 * np.tan(np.deg2rad(camera.vertical_fov_deg) * 0.5))
    depth_pixels = np.sort((depth - depth.mean()) * focal_px / max(float(depth.mean()), 1e-12))
    return radii, pairwise, depth_pixels


def evaluate_many(
    scene: Scene,
    requests: Sequence[tuple[str, CameraPose]],
    config: OracleConfig = OracleConfig(),
    *,
    device: DeviceRequest = "auto",
    return_info: bool = False,
):
    """Evaluate arbitrary ``(feature_id, camera)`` requests in one batched kernel.

    ``auto`` selects CUDA when available, then Torch CPU, then the frozen NumPy oracle.
    Ordering matches ``requests``.  The CUDA/CPU Torch path batches occlusion and silhouette
    rays across every requested camera and feature before intersecting them with triangles.
    """

    normalized = tuple((str(feature_id), camera) for feature_id, camera in requests)
    for feature_id, _ in normalized:
        scene.feature(feature_id)
    torch, torch_device, backend = _resolve_device(device)
    if not normalized or backend == "numpy":
        labels = tuple(evaluate(scene, feature_id, camera, config) for feature_id, camera in normalized)
        info = BackendInfo("numpy", "numpy", 0, len(scene.mesh.triangles), 0, 0)
        return (labels, info) if return_info else labels

    camera_by_identity: dict[int, int] = {}
    cameras: list[CameraPose] = []
    request_camera_indices: list[int] = []
    for _, camera in normalized:
        identity = id(camera)
        camera_index = camera_by_identity.get(identity)
        if camera_index is None:
            camera_index = len(cameras)
            camera_by_identity[identity] = camera_index
            cameras.append(camera)
        request_camera_indices.append(camera_index)

    features = scene.features
    feature_by_id = {feature.feature_id: index for index, feature in enumerate(features)}
    feature_triangle_mask = np.zeros((len(features), len(scene.mesh.triangles)), dtype=bool)
    for feature_index, feature in enumerate(features):
        if feature.triangle_indices:
            feature_triangle_mask[feature_index, np.asarray(feature.triangle_indices, dtype=np.int64)] = True

    rays = _RayBatch.empty()
    occlusion_rays: dict[tuple[int, int], list[tuple[int, float]]] = {}
    # Ambiguity compares every scene feature, so generate each (camera, feature) visibility
    # once and share it across all requested target labels at that camera.
    for camera_index, camera in enumerate(cameras):
        for feature_index, feature in enumerate(features):
            samples, weights = _surface_samples(scene, feature)
            entries: list[tuple[int, float]] = []
            for sample, weight in zip(samples, weights):
                delta = sample - camera.position
                distance = float(np.linalg.norm(delta))
                if distance <= config.ray_epsilon:
                    continue
                ray_index = rays.add(
                    camera.position,
                    delta / distance,
                    config.ray_epsilon,
                    distance - config.ray_epsilon,
                    feature_index if feature.triangle_indices else -1,
                )
                entries.append((ray_index, float(weight)))
            occlusion_rays[(camera_index, feature_index)] = entries

    silhouette_segments: list[list[tuple[int, int, float]]] = []
    for (feature_id, camera), camera_index in zip(normalized, request_camera_indices):
        feature = features[feature_by_id[feature_id]]
        projection = project_points(scene.feature_points(feature), camera)
        hull = _convex_hull(projection.pixels[projection.in_front])
        segments: list[tuple[int, int, float]] = []
        if len(hull) >= 2:
            segment_count = len(hull) if len(hull) >= 3 else 1
            for index in range(segment_count):
                start = hull[index]
                end = hull[(index + 1) % len(hull)]
                segment = end - start
                length = float(np.linalg.norm(segment))
                if length <= 1e-12:
                    continue
                normal = np.asarray((-segment[1], segment[0])) / length
                midpoint = (start + end) * 0.5
                first = rays.add(camera.position, _pixel_ray(camera, midpoint - config.silhouette_probe_px * normal), camera.near, camera.far, -1)
                second = rays.add(camera.position, _pixel_ray(camera, midpoint + config.silhouette_probe_px * normal), camera.near, camera.far, -1)
                segments.append((first, second, length))
        silhouette_segments.append(segments)

    try:
        hit, chunk_size, repaired = _torch_ray_hits(
            scene, rays, feature_triangle_mask, config.ray_epsilon, torch, torch_device
        )
    except RuntimeError as error:
        if backend != "cuda" or "out of memory" not in str(error).lower():
            raise
        torch.cuda.empty_cache()
        # Device selection is a preference, not permission to fail a corpus.  A transient
        # VRAM shortage continues down the documented CUDA -> Torch CPU -> NumPy chain.
        return evaluate_many(scene, normalized, config, device="cpu", return_info=return_info)
    occlusions: dict[tuple[int, int], float] = {}
    for key, entries in occlusion_rays.items():
        value = 0.0
        for ray_index, weight in entries:
            if hit[ray_index]:
                value += float(weight)
        occlusions[key] = float(np.clip(value, 0.0, 1.0))

    signatures: dict[tuple[int, int], tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    for camera_index, camera in enumerate(cameras):
        pixel_scale = max(camera.image_width_px, camera.image_height_px)
        for feature_index, feature in enumerate(features):
            base = _signature_without_occlusion(scene, feature, camera)
            signatures[(camera_index, feature_index)] = (*base, np.asarray((occlusions[(camera_index, feature_index)] * pixel_scale,)))

    labels: list[InspectabilityLabel] = []
    for request_index, ((feature_id, camera), camera_index) in enumerate(zip(normalized, request_camera_indices)):
        feature_index = feature_by_id[feature_id]
        feature = features[feature_index]
        points = scene.feature_points(feature)
        projection = project_points(points, camera)
        coverage = _in_frame_coverage(scene, feature, camera)
        occlusion = occlusions[(camera_index, feature_index)]
        angle, foreshortening_quality = _foreshortening(feature, points.mean(axis=0), camera)
        scale = _apparent_scale(projection)
        scale_quality = float(np.clip(scale / config.min_resolvable_px, 0.0, 1.0))
        segments = silhouette_segments[request_index]
        total_length = 0.0
        informative_length = 0.0
        for first, second, length in segments:
            total_length += length
            if not hit[first] or not hit[second]:
                informative_length += length
        silhouette = 0.0 if total_length <= 1e-12 else float(np.clip(informative_length / total_length, 0.0, 1.0))
        depth_samples, depth_weights = _surface_samples(scene, feature)
        sample_depth = project_points(depth_samples, camera).depth
        depth = float(np.clip(depth_weights @ ((sample_depth >= camera.near) & (sample_depth <= camera.far)), 0.0, 1.0))

        reference = signatures[(camera_index, feature_index)]
        matches: list[str] = []
        for other_index, other in enumerate(features):
            if other_index == feature_index:
                continue
            candidate = signatures[(camera_index, other_index)]
            if any(len(left) != len(right) for left, right in zip(reference, candidate)):
                continue
            maximum_difference = max(
                (float(np.max(np.abs(left - right))) if len(left) else 0.0)
                for left, right in zip(reference, candidate)
            )
            if maximum_difference <= config.ambiguity_tolerance_px:
                matches.append(other.feature_id)
        ambiguity = 1.0 if matches else 0.0
        components = {
            "coverage": coverage,
            "visibility": 1.0 - occlusion,
            "foreshortening": foreshortening_quality,
            "scale": scale_quality,
            "silhouette": silhouette,
            "depth": depth,
            "uniqueness": 1.0 - ambiguity,
        }
        labels.append(
            InspectabilityLabel(
                in_frame_coverage=coverage,
                occlusion_fraction=occlusion,
                foreshortening_angle_deg=angle,
                foreshortening_quality=foreshortening_quality,
                apparent_scale_px=scale,
                scale_quality=scale_quality,
                silhouette_informativeness=silhouette,
                depth_validity=depth,
                ambiguity=ambiguity,
                ambiguous_feature_ids=tuple(matches),
                aggregate=aggregate_quality(components, config),
            )
        )

    info = BackendInfo(
        backend,
        str(torch_device),
        len(rays.origins),
        len(scene.mesh.triangles),
        chunk_size,
        repaired,
    )
    result = tuple(labels)
    return (result, info) if return_info else result


def evaluate_grid(
    scene: Scene,
    feature_ids: Sequence[str],
    cameras: Sequence[CameraPose],
    config: OracleConfig = OracleConfig(),
    *,
    device: DeviceRequest = "auto",
    return_info: bool = False,
):
    """Evaluate the feature-major Cartesian product of features and cameras."""

    requests = tuple((feature_id, camera) for feature_id in feature_ids for camera in cameras)
    return evaluate_many(scene, requests, config, device=device, return_info=return_info)
