"""Pure-geometry inspectability oracle.

Visibility is estimated by deterministic, area-weighted surface samples and explicit
Möller–Trumbore ray/triangle intersections. No rasterizer, renderer, GPU, or learned
component participates. The aggregate is intentionally transparent: a weighted geometric
mean of seven normalized qualities. A zero in any required component makes the aggregate
zero; callers should retain the component vector rather than train on the aggregate alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, log
from typing import Mapping

import numpy as np

from .geometry import CameraPose, Feature, Projection, Scene, project_points, unit


ORACLE_VERSION = "viewpoint-geometric-oracle/1.0.0"
GEOMETRIC_LIMITATIONS = (
    "triangle meshes are treated as perfectly opaque, infinitesimally thin surfaces",
    "occlusion is an area-weighted fixed-sample estimate, not exact visible polygon area",
    "no material, texture, lighting, transparency, reflection, shadow, sensor noise, or lens distortion",
    "no tolerance stack, deformation, motion, manufacturing uncertainty, or semantic task usefulness",
    "silhouette probes use the convex hull of feature landmarks and do not represent concave contour detail",
    "ambiguity compares projected geometric landmarks, depth relief, scale, and visibility; it does not compare rendered appearance",
)
DEFAULT_WEIGHTS = (
    ("coverage", 1.0),
    ("visibility", 1.25),
    ("foreshortening", 1.0),
    ("scale", 1.0),
    ("silhouette", 0.75),
    ("depth", 1.25),
    ("uniqueness", 0.5),
)
COMPONENT_POLICY = (
    ("coverage", "included", "benchmark poses vary framing target and field of view, producing edge and clipped features"),
    ("visibility", "included", "mechanical walls, bosses, ribs, webs, and overhangs create pose-dependent self-occlusion"),
    ("foreshortening", "included", "feature axes and face normals vary independently of the global part frame"),
    ("scale", "included", "benchmark distance and field of view vary, and small details become unresolved"),
    ("silhouette", "included", "isolated bosses and embedded pocket features exercise both contour regimes"),
    ("depth", "included", "benchmark near/far margins deliberately clip different parts of feature geometry"),
    ("uniqueness", "included", "matched twin holes become distinguishable only when asymmetric context occludes one twin"),
)


@dataclass(frozen=True)
class OracleConfig:
    min_resolvable_px: float = 8.0
    ray_epsilon: float = 1e-7
    silhouette_probe_px: float = 1.5
    ambiguity_tolerance_px: float = 0.5
    weights: tuple[tuple[str, float], ...] = DEFAULT_WEIGHTS

    def __post_init__(self) -> None:
        if self.min_resolvable_px <= 0.0:
            raise ValueError("min_resolvable_px must be positive")
        if self.ray_epsilon <= 0.0 or self.silhouette_probe_px <= 0.0:
            raise ValueError("ray_epsilon and silhouette_probe_px must be positive")
        if self.ambiguity_tolerance_px < 0.0:
            raise ValueError("ambiguity_tolerance_px must be non-negative")
        keys = tuple(key for key, _ in self.weights)
        if keys != tuple(key for key, _ in DEFAULT_WEIGHTS):
            raise ValueError("weights must name the seven quality components in canonical order")
        if any(not np.isfinite(weight) or weight <= 0.0 for _, weight in self.weights):
            raise ValueError("aggregate weights must be finite and positive")


@dataclass(frozen=True)
class InspectabilityLabel:
    """Raw measurements and their normalized qualities.

    ``foreshortening_angle_deg`` is the acute angle between feature-to-camera direction
    and the unoriented face normal/axis. Face, hole, boss and pocket quality is cosine of
    that angle (axial/head-on is best); edge quality is sine (side-on is best). Regions
    without an orientation are explicitly not applicable (``None``) and get neutral 1.0.

    ``apparent_scale_px`` is the maximum pairwise distance between projected feature
    landmarks. ``silhouette_informativeness`` is the projected-hull perimeter fraction
    for which a 1.5-pixel outward probe sees background rather than any mesh triangle.
    ``ambiguity`` is binary: 1 when another feature has the same translation-invariant
    projected landmark/depth signature within the configured pixel tolerance.
    """

    in_frame_coverage: float
    occlusion_fraction: float
    foreshortening_angle_deg: float | None
    foreshortening_quality: float
    apparent_scale_px: float
    scale_quality: float
    silhouette_informativeness: float
    depth_validity: float
    ambiguity: float
    ambiguous_feature_ids: tuple[str, ...]
    aggregate: float

    def quality_components(self) -> dict[str, float]:
        return {
            "coverage": self.in_frame_coverage,
            "visibility": 1.0 - self.occlusion_fraction,
            "foreshortening": self.foreshortening_quality,
            "scale": self.scale_quality,
            "silhouette": self.silhouette_informativeness,
            "depth": self.depth_validity,
            "uniqueness": 1.0 - self.ambiguity,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "in_frame_coverage": self.in_frame_coverage,
            "occlusion_fraction": self.occlusion_fraction,
            "foreshortening_angle_deg": self.foreshortening_angle_deg,
            "foreshortening_quality": self.foreshortening_quality,
            "apparent_scale_px": self.apparent_scale_px,
            "scale_quality": self.scale_quality,
            "silhouette_informativeness": self.silhouette_informativeness,
            "depth_validity": self.depth_validity,
            "ambiguity": self.ambiguity,
            "ambiguous_feature_ids": list(self.ambiguous_feature_ids),
            "quality_components": self.quality_components(),
            "aggregate": self.aggregate,
        }


def aggregate_quality(components: Mapping[str, float], config: OracleConfig = OracleConfig()) -> float:
    """Weighted geometric mean, ``exp(sum(w*log(q))/sum(w))``, on [0, 1] values."""

    total_weight = 0.0
    weighted_log = 0.0
    for name, weight in config.weights:
        value = float(components[name])
        if not 0.0 <= value <= 1.0 or not np.isfinite(value):
            raise ValueError(f"quality {name!r} is outside [0, 1]")
        if value == 0.0:
            return 0.0
        weighted_log += weight * log(value)
        total_weight += weight
    return float(exp(weighted_log / total_weight))


def _polygon_area(polygon: np.ndarray) -> float:
    if len(polygon) < 3:
        return 0.0
    return abs(float(np.dot(polygon[:, 0], np.roll(polygon[:, 1], -1)) - np.dot(polygon[:, 1], np.roll(polygon[:, 0], -1)))) * 0.5


def _clip_axis(polygon: np.ndarray, axis: int, bound: float, keep_less: bool) -> np.ndarray:
    if len(polygon) == 0:
        return polygon
    output: list[np.ndarray] = []
    previous = polygon[-1]
    previous_inside = previous[axis] <= bound if keep_less else previous[axis] >= bound
    for current in polygon:
        current_inside = current[axis] <= bound if keep_less else current[axis] >= bound
        if current_inside != previous_inside:
            delta = current - previous
            if abs(float(delta[axis])) > 1e-15:
                t = (bound - previous[axis]) / delta[axis]
                output.append(previous + t * delta)
        if current_inside:
            output.append(current)
        previous = current
        previous_inside = current_inside
    return np.asarray(output, dtype=np.float64).reshape((-1, 2))


def _clip_viewport(polygon: np.ndarray) -> np.ndarray:
    for axis, bound, keep_less in ((0, -1.0, False), (0, 1.0, True), (1, -1.0, False), (1, 1.0, True)):
        polygon = _clip_axis(polygon, axis, bound, keep_less)
    return polygon


def _surface_samples(scene: Scene, feature: Feature) -> tuple[np.ndarray, np.ndarray]:
    if not feature.triangle_indices:
        points = scene.feature_points(feature)
        return points, np.full(len(points), 1.0 / len(points), dtype=np.float64)
    triangle_ids = np.asarray(feature.triangle_indices, dtype=np.int64)
    triangles = scene.mesh.vertices[scene.mesh.triangles[triangle_ids]]
    # Six fixed interior samples avoid shared-edge/vertex visibility degeneracy.
    barycentric = np.asarray(
        (
            (0.6, 0.2, 0.2),
            (0.2, 0.6, 0.2),
            (0.2, 0.2, 0.6),
            (0.45, 0.45, 0.10),
            (0.45, 0.10, 0.45),
            (0.10, 0.45, 0.45),
        ),
        dtype=np.float64,
    )
    samples = np.einsum("sk,tkd->tsd", barycentric, triangles).reshape((-1, 3))
    areas = np.linalg.norm(np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]), axis=1) * 0.5
    if float(areas.sum()) <= 1e-15:
        weights = np.full(len(samples), 1.0 / len(samples), dtype=np.float64)
    else:
        weights = np.repeat(areas / areas.sum() / len(barycentric), len(barycentric))
    return samples, weights


def _in_frame_coverage(scene: Scene, feature: Feature, camera: CameraPose) -> float:
    if not feature.triangle_indices:
        projection = project_points(scene.feature_points(feature), camera)
        inside = (
            projection.in_front
            & (np.abs(projection.ndc[:, 0]) <= 1.0)
            & (np.abs(projection.ndc[:, 1]) <= 1.0)
        )
        return float(inside.mean())
    triangle_ids = np.asarray(feature.triangle_indices, dtype=np.int64)
    triangles = scene.mesh.vertices[scene.mesh.triangles[triangle_ids]]
    projected = project_points(triangles.reshape((-1, 3)), camera)
    ndc = projected.ndc.reshape((-1, 3, 2))
    depth = projected.depth.reshape((-1, 3))
    total = 0.0
    inside = 0.0
    for polygon, triangle_depth in zip(ndc, depth):
        if np.any(triangle_depth <= 0.0):
            continue
        area = _polygon_area(polygon)
        total += area
        inside += _polygon_area(_clip_viewport(polygon))
    if total <= 1e-15:
        return 0.0
    return float(np.clip(inside / total, 0.0, 1.0))


def _ray_hits(origin: np.ndarray, direction: np.ndarray, triangles: np.ndarray, *, minimum: float, maximum: float, epsilon: float) -> bool:
    if len(triangles) == 0:
        return False
    edge1 = triangles[:, 1] - triangles[:, 0]
    edge2 = triangles[:, 2] - triangles[:, 0]
    pvec = np.cross(np.broadcast_to(direction, edge2.shape), edge2)
    determinant = np.einsum("ij,ij->i", edge1, pvec)
    active = np.abs(determinant) > epsilon
    inverse = np.zeros_like(determinant)
    inverse[active] = 1.0 / determinant[active]
    tvec = origin - triangles[:, 0]
    u = np.einsum("ij,ij->i", tvec, pvec) * inverse
    qvec = np.cross(tvec, edge1)
    v = qvec @ direction * inverse
    distance = np.einsum("ij,ij->i", edge2, qvec) * inverse
    hit = active & (u >= -epsilon) & (v >= -epsilon) & (u + v <= 1.0 + epsilon)
    hit &= (distance >= minimum) & (distance <= maximum)
    return bool(np.any(hit))


def _occlusion_fraction(scene: Scene, feature: Feature, camera: CameraPose, config: OracleConfig) -> float:
    samples, weights = _surface_samples(scene, feature)
    all_ids = np.arange(len(scene.mesh.triangles), dtype=np.int64)
    if feature.triangle_indices:
        mask = ~np.isin(all_ids, np.asarray(feature.triangle_indices, dtype=np.int64))
        triangles = scene.mesh.vertices[scene.mesh.triangles[mask]]
    else:
        triangles = scene.mesh.vertices[scene.mesh.triangles]
    occluded = 0.0
    for sample, weight in zip(samples, weights):
        delta = sample - camera.position
        distance = float(np.linalg.norm(delta))
        if distance <= config.ray_epsilon:
            continue
        direction = delta / distance
        if _ray_hits(
            camera.position,
            direction,
            triangles,
            minimum=config.ray_epsilon,
            maximum=distance - config.ray_epsilon,
            epsilon=config.ray_epsilon,
        ):
            occluded += float(weight)
    return float(np.clip(occluded, 0.0, 1.0))


def _convex_hull(points: np.ndarray) -> np.ndarray:
    unique = sorted(set(map(tuple, np.asarray(points, dtype=np.float64))))
    if len(unique) <= 1:
        return np.asarray(unique, dtype=np.float64).reshape((-1, 2))

    def cross(origin: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)
    return np.asarray(lower[:-1] + upper[:-1], dtype=np.float64)


def _pixel_ray(camera: CameraPose, pixel: np.ndarray) -> np.ndarray:
    ndc_x = 2.0 * float(pixel[0]) / camera.image_width_px - 1.0
    ndc_y = 1.0 - 2.0 * float(pixel[1]) / camera.image_height_px
    tan_y = np.tan(np.deg2rad(camera.vertical_fov_deg) * 0.5)
    tan_x = tan_y * camera.image_width_px / camera.image_height_px
    return unit(camera.forward + ndc_x * tan_x * camera.right + ndc_y * tan_y * camera.true_up, name="pixel ray")


def _silhouette_informativeness(scene: Scene, feature: Feature, camera: CameraPose, config: OracleConfig) -> float:
    projection = project_points(scene.feature_points(feature), camera)
    usable = projection.in_front
    hull = _convex_hull(projection.pixels[usable])
    if len(hull) < 2:
        return 0.0
    triangles = scene.mesh.vertices[scene.mesh.triangles]
    informative_length = 0.0
    total_length = 0.0
    segment_count = len(hull) if len(hull) >= 3 else 1
    for index in range(segment_count):
        start = hull[index]
        end = hull[(index + 1) % len(hull)]
        segment = end - start
        length = float(np.linalg.norm(segment))
        if length <= 1e-12:
            continue
        total_length += length
        normal = np.asarray((-segment[1], segment[0])) / length
        midpoint = (start + end) * 0.5
        sees_background = False
        for sign in (-1.0, 1.0):
            ray = _pixel_ray(camera, midpoint + sign * config.silhouette_probe_px * normal)
            if not _ray_hits(camera.position, ray, triangles, minimum=camera.near, maximum=camera.far, epsilon=config.ray_epsilon):
                sees_background = True
                break
        if sees_background:
            informative_length += length
    return 0.0 if total_length <= 1e-12 else float(np.clip(informative_length / total_length, 0.0, 1.0))


def _apparent_scale(projection: Projection) -> float:
    points = projection.pixels[projection.in_front]
    if len(points) < 2:
        return 0.0
    difference = points[:, None, :] - points[None, :, :]
    return float(np.sqrt(np.max(np.einsum("ijk,ijk->ij", difference, difference))))


def _foreshortening(feature: Feature, center: np.ndarray, camera: CameraPose) -> tuple[float | None, float]:
    if feature.orientation is None:
        return None, 1.0
    to_camera = unit(camera.position - center, name="feature-to-camera direction")
    cosine = float(np.clip(abs(np.dot(to_camera, feature.orientation)), 0.0, 1.0))
    angle = float(np.rad2deg(np.arccos(cosine)))
    quality = float(np.sqrt(max(0.0, 1.0 - cosine * cosine))) if feature.kind == "edge" else cosine
    return angle, quality


def _visual_signature(
    scene: Scene, feature: Feature, camera: CameraPose, config: OracleConfig
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    projection = project_points(scene.feature_points(feature), camera)
    valid = projection.in_front
    pixels = projection.pixels[valid]
    depth = projection.depth[valid]
    if len(pixels) == 0:
        empty = np.empty(0, dtype=np.float64)
        return empty, empty, empty, np.asarray((0.0,))
    centered = pixels - pixels.mean(axis=0)
    radii = np.sort(np.linalg.norm(centered, axis=1))
    pairwise = np.linalg.norm(pixels[:, None, :] - pixels[None, :, :], axis=2)
    pairwise = np.sort(pairwise[np.triu_indices(len(pixels), k=1)])
    focal_px = camera.image_height_px / (2.0 * np.tan(np.deg2rad(camera.vertical_fov_deg) * 0.5))
    depth_in_pixel_units = np.sort((depth - depth.mean()) * focal_px / max(float(depth.mean()), 1e-12))
    occlusion_in_pixel_units = np.asarray(
        (_occlusion_fraction(scene, feature, camera, config) * max(camera.image_width_px, camera.image_height_px),)
    )
    return radii, pairwise, depth_in_pixel_units, occlusion_in_pixel_units


def _ambiguity(scene: Scene, feature: Feature, camera: CameraPose, config: OracleConfig) -> tuple[float, tuple[str, ...]]:
    reference = _visual_signature(scene, feature, camera, config)
    matches: list[str] = []
    for other in scene.features:
        if other.feature_id == feature.feature_id:
            continue
        candidate = _visual_signature(scene, other, camera, config)
        if any(len(left) != len(right) for left, right in zip(reference, candidate)):
            continue
        maximum_difference = max(
            (float(np.max(np.abs(left - right))) if len(left) else 0.0)
            for left, right in zip(reference, candidate)
        )
        if maximum_difference <= config.ambiguity_tolerance_px:
            matches.append(other.feature_id)
    return (1.0 if matches else 0.0), tuple(matches)


def evaluate(scene: Scene, feature_id: str, camera: CameraPose, config: OracleConfig = OracleConfig()) -> InspectabilityLabel:
    """Compute all inspectability components for one named feature and camera pose."""

    feature = scene.feature(feature_id)
    points = scene.feature_points(feature)
    projection = project_points(points, camera)
    coverage = _in_frame_coverage(scene, feature, camera)
    occlusion = _occlusion_fraction(scene, feature, camera, config)
    angle, foreshortening_quality = _foreshortening(feature, points.mean(axis=0), camera)
    scale = _apparent_scale(projection)
    scale_quality = float(np.clip(scale / config.min_resolvable_px, 0.0, 1.0))
    silhouette = _silhouette_informativeness(scene, feature, camera, config)
    depth_samples, depth_weights = _surface_samples(scene, feature)
    sample_depth = project_points(depth_samples, camera).depth
    depth = float(np.clip(depth_weights @ ((sample_depth >= camera.near) & (sample_depth <= camera.far)), 0.0, 1.0))
    ambiguity, ambiguous_ids = _ambiguity(scene, feature, camera, config)
    components = {
        "coverage": coverage,
        "visibility": 1.0 - occlusion,
        "foreshortening": foreshortening_quality,
        "scale": scale_quality,
        "silhouette": silhouette,
        "depth": depth,
        "uniqueness": 1.0 - ambiguity,
    }
    return InspectabilityLabel(
        in_frame_coverage=coverage,
        occlusion_fraction=occlusion,
        foreshortening_angle_deg=angle,
        foreshortening_quality=foreshortening_quality,
        apparent_scale_px=scale,
        scale_quality=scale_quality,
        silhouette_informativeness=silhouette,
        depth_validity=depth,
        ambiguity=ambiguity,
        ambiguous_feature_ids=ambiguous_ids,
        aggregate=aggregate_quality(components, config),
    )
