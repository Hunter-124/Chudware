"""Station profiles and deterministic fail-closed transform resolution."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import heapq
import math
from typing import Iterable, Mapping, Sequence

Vector3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]
Covariance6 = tuple[tuple[float, ...], ...]


def _quat_normalize(q: Sequence[float]) -> Quaternion:
    if len(q) != 4 or any(not math.isfinite(value) for value in q):
        raise ValueError("quaternion must contain four finite values")
    norm = math.sqrt(math.fsum(value * value for value in q))
    if norm == 0:
        raise ValueError("zero quaternion is invalid")
    return tuple(value / norm for value in q)  # type: ignore[return-value]


def _quat_mul(a: Quaternion, b: Quaternion) -> Quaternion:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return _quat_normalize((aw * bx + ax * bw + ay * bz - az * by,
                            aw * by - ax * bz + ay * bw + az * bx,
                            aw * bz + ax * by - ay * bx + az * bw,
                            aw * bw - ax * bx - ay * by - az * bz))


def _rotate(q: Quaternion, v: Vector3) -> Vector3:
    x, y, z, w = q
    vx, vy, vz = v
    tx, ty, tz = (2 * (y * vz - z * vy), 2 * (z * vx - x * vz),
                  2 * (x * vy - y * vx))
    return (vx + w * tx + (y * tz - z * ty),
            vy + w * ty + (z * tx - x * tz),
            vz + w * tz + (x * ty - y * tx))


@dataclass(frozen=True, slots=True)
class Pose:
    translation_mm: Vector3 = (0.0, 0.0, 0.0)
    rotation_xyzw: Quaternion = (0.0, 0.0, 0.0, 1.0)

    def __post_init__(self) -> None:
        if len(self.translation_mm) != 3 or any(not math.isfinite(v) for v in self.translation_mm):
            raise ValueError("translation must contain three finite values")
        object.__setattr__(self, "translation_mm", tuple(float(v) for v in self.translation_mm))
        object.__setattr__(self, "rotation_xyzw", _quat_normalize(self.rotation_xyzw))

    def compose(self, after: "Pose") -> "Pose":
        rotated = _rotate(self.rotation_xyzw, after.translation_mm)
        translation = tuple(self.translation_mm[i] + rotated[i] for i in range(3))
        return Pose(translation, _quat_mul(self.rotation_xyzw, after.rotation_xyzw))  # type: ignore[arg-type]

    def inverse(self) -> "Pose":
        x, y, z, w = self.rotation_xyzw
        inverse_q = (-x, -y, -z, w)
        neg = tuple(-v for v in self.translation_mm)
        return Pose(_rotate(inverse_q, neg), inverse_q)  # type: ignore[arg-type]

    def apply(self, point: Sequence[float]) -> Vector3:
        if len(point) != 3:
            raise ValueError("point must have three coordinates")
        rotated = _rotate(self.rotation_xyzw, tuple(float(v) for v in point))  # type: ignore[arg-type]
        return tuple(rotated[i] + self.translation_mm[i] for i in range(3))  # type: ignore[return-value]


class EdgeSource(str, Enum):
    PROBED = "probed"
    DECLARED = "declared"
    ASSUMED = "assumed"
    SIMULATED = "simulated"
    ADVISORY = "advisory"


class EdgeState(str, Enum):
    LIVE = "live"
    UNKNOWN = "unknown"
    SUPERSEDED = "superseded"


def _validate_covariance(covariance: Sequence[Sequence[float]] | None) -> Covariance6 | None:
    if covariance is None:
        return None
    if len(covariance) != 6 or any(len(row) != 6 for row in covariance):
        raise ValueError("transform covariance must be 6x6")
    result = tuple(tuple(float(value) for value in row) for row in covariance)
    if any(not math.isfinite(value) for row in result for value in row):
        raise ValueError("transform covariance must be finite")
    return result


@dataclass(frozen=True, slots=True)
class TransformEdge:
    id: str
    source_frame: str
    target_frame: str
    pose: Pose
    covariance: Covariance6 | None
    source: EdgeSource
    revision: int
    calibration_id: str | None = None
    valid_until: datetime | None = None
    state: EdgeState = EdgeState.LIVE

    def __post_init__(self) -> None:
        if not self.id or not self.source_frame or not self.target_frame:
            raise ValueError("edge id and frames must not be empty")
        if self.source_frame == self.target_frame:
            raise ValueError("self transform is not an edge")
        if self.revision < 0:
            raise ValueError("edge revision must be non-negative")
        object.__setattr__(self, "covariance", _validate_covariance(self.covariance))
        if self.valid_until is not None and self.valid_until.tzinfo is None:
            raise ValueError("valid_until must be timezone-aware")

    def is_live(self, at: datetime) -> bool:
        return self.state is EdgeState.LIVE and (self.valid_until is None or self.valid_until >= at)

    @property
    def uncertainty_score(self) -> float:
        return math.inf if self.covariance is None else math.fsum(self.covariance[i][i] for i in range(6))


@dataclass(frozen=True, slots=True)
class ResolvedTransform:
    pose: Pose
    covariance: Covariance6 | None
    edge_ids: tuple[str, ...]
    uncertainty_unknown: bool


class TransformResolutionError(RuntimeError):
    def __init__(self, message: str, *, edge: str | None = None) -> None:
        super().__init__(message)
        self.edge = edge


class TransformGraph:
    """An acyclic physical frame graph with deterministic bidirectional resolution."""
    def __init__(self) -> None:
        self._edges: dict[str, TransformEdge] = {}
        self._adjacency: dict[str, list[tuple[str, bool]]] = {}

    @property
    def edges(self) -> tuple[TransformEdge, ...]:
        return tuple(self._edges[key] for key in sorted(self._edges))

    def _connected(self, source: str, target: str) -> bool:
        pending = [source]
        seen = set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            for edge_id, forward in self._adjacency.get(node, ()):
                edge = self._edges[edge_id]
                pending.append(edge.target_frame if forward else edge.source_frame)
        return False

    def add_edge(self, edge: TransformEdge) -> None:
        if edge.id in self._edges:
            raise ValueError(f"duplicate transform edge id: {edge.id}")
        if self._connected(edge.source_frame, edge.target_frame):
            raise ValueError("transform edge would create a cycle")
        self._edges[edge.id] = edge
        self._adjacency.setdefault(edge.source_frame, []).append((edge.id, True))
        self._adjacency.setdefault(edge.target_frame, []).append((edge.id, False))

    def resolve(self, source: str, target: str, *, at: datetime | None = None) -> ResolvedTransform:
        if source == target:
            zero = tuple(tuple(0.0 for _ in range(6)) for _ in range(6))
            return ResolvedTransform(Pose(), zero, (), False)
        now = at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("resolve time must be timezone-aware")
        # Tuple ordering implements hop count, summed uncertainty, then edge-id path.
        queue: list[tuple[int, float, tuple[str, ...], str, Pose,
                          tuple[tuple[float, ...], ...] | None]] = [
            (0, 0.0, (), source, Pose(), tuple(tuple(0.0 for _ in range(6)) for _ in range(6)))]
        best: dict[str, tuple[int, float, tuple[str, ...]]] = {}
        while queue:
            hops, score, ids, node, pose, covariance = heapq.heappop(queue)
            rank = (hops, score, ids)
            if node in best and best[node] <= rank:
                continue
            best[node] = rank
            if node == target:
                for edge_id in ids:
                    edge = self._edges[edge_id]
                    if not edge.is_live(now):
                        raise TransformResolutionError("chosen transform path contains stale or unknown edge",
                                                       edge=edge_id)
                return ResolvedTransform(pose, covariance, ids, covariance is None)
            for edge_id, forward in sorted(self._adjacency.get(node, ()), key=lambda item: item[0]):
                edge = self._edges[edge_id]
                next_node = edge.target_frame if forward else edge.source_frame
                step_pose = edge.pose if forward else edge.pose.inverse()
                next_covariance: Covariance6 | None
                if covariance is None or edge.covariance is None:
                    next_covariance = None
                else:
                    next_covariance = tuple(tuple(covariance[r][c] + edge.covariance[r][c]
                                                  for c in range(6)) for r in range(6))
                heapq.heappush(queue, (hops + 1, score + edge.uncertainty_score,
                                       ids + (edge_id,), next_node, pose.compose(step_pose),
                                       next_covariance))
        raise TransformResolutionError(f"no transform path from {source} to {target}")


@dataclass(frozen=True, slots=True)
class CameraMode:
    width: int
    height: int
    fps: float
    fourcc: str


@dataclass(frozen=True, slots=True)
class StationProfile:
    station_id: str
    camera_serial: str
    device_id: str
    bus: str
    mode: CameraMode
    locked_controls: Mapping[str, float]
    controls_readback_verified: bool
    calibration_id: str
    station_plane: Pose
    working_distance_mm: float
    px_per_mm_map: tuple[float, float, float]
    reference_fiducial: str
    verification_ref: str
    valid_until: datetime
    derived_from: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.valid_until.tzinfo is None:
            raise ValueError("station profile validity must be timezone-aware")
        if len(self.px_per_mm_map) != 3 or min(self.px_per_mm_map) <= 0:
            raise ValueError("station scale map needs positive min/max/median")
        minimum, maximum, median = self.px_per_mm_map
        if not minimum <= median <= maximum:
            raise ValueError("station scale median must lie inside min/max")

    def is_live(self, at: datetime | None = None) -> bool:
        now = at or datetime.now(timezone.utc)
        return self.valid_until >= now
