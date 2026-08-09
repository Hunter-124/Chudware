"""Dependency-free serial-chain forward kinematics and analysis."""
from __future__ import annotations

from dataclasses import dataclass
import math
from itertools import islice
from typing import Iterable

from .model import Matrix4, RobotChain, ToolCenterPoint, Vector3


class UnreachableTargetError(ValueError):
    pass


@dataclass(frozen=True)
class ForwardKinematics:
    link_poses: dict[str, Matrix4]
    tcp_pose: Matrix4
    joint_origins_mm: tuple[Vector3, ...]
    joint_axes_world: tuple[Vector3, ...]


@dataclass(frozen=True)
class SingularityAnalysis:
    singular: bool
    sigma_min: float
    sigma_max: float
    condition_number: float
    threshold: float
    source: str = "simulated"
    advisory_only: bool = True
    satisfies_execute_gate: bool = False


def identity() -> Matrix4:
    return ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def multiply(a: Matrix4, b: Matrix4) -> Matrix4:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)) for i in range(4))  # type: ignore[return-value]


def transform_point(matrix: Matrix4, point: Vector3) -> Vector3:
    return tuple(sum(matrix[i][k] * point[k] for k in range(3)) + matrix[i][3] for i in range(3))  # type: ignore[return-value]


def rotate_vector(matrix: Matrix4, vector: Vector3) -> Vector3:
    return tuple(sum(matrix[i][k] * vector[k] for k in range(3)) for i in range(3))  # type: ignore[return-value]


def translation(xyz: Vector3) -> Matrix4:
    x, y, z = xyz
    return ((1.0, 0.0, 0.0, x), (0.0, 1.0, 0.0, y), (0.0, 0.0, 1.0, z), (0.0, 0.0, 0.0, 1.0))


def rpy_matrix(rpy: Vector3) -> Matrix4:
    roll, pitch, yaw = rpy
    cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, 0.0),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, 0.0),
        (-sp, cp * sr, cp * cr, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def axis_rotation(axis: Vector3, angle: float) -> Matrix4:
    x, y, z = axis
    c, s, d = math.cos(angle), math.sin(angle), 1.0 - math.cos(angle)
    return (
        (c + x * x * d, x * y * d - z * s, x * z * d + y * s, 0.0),
        (y * x * d + z * s, c + y * y * d, y * z * d - x * s, 0.0),
        (z * x * d - y * s, z * y * d + x * s, c + z * z * d, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _joint_values(chain: RobotChain, values: Iterable[float]) -> dict[str, float]:
    movable = chain.movable_joints
    supplied = tuple(float(value) for value in islice(values, len(movable) + 1))
    if len(supplied) != len(movable):
        raise ValueError(f"expected {len(movable)} joint values, got {len(supplied)}")
    result = dict(zip((joint.name for joint in movable), supplied))
    for joint in movable:
        value = result[joint.name]
        if not math.isfinite(value):
            raise ValueError(f"joint {joint.name} value is not finite")
        if joint.limit is not None and not joint.limit.lower <= value <= joint.limit.upper:
            raise ValueError(f"joint {joint.name} violates [{joint.limit.lower}, {joint.limit.upper}]")
    return result


def forward_kinematics(chain: RobotChain, values: Iterable[float], tcp: ToolCenterPoint | None = None) -> ForwardKinematics:
    by_name = _joint_values(chain, values)
    current = identity()
    poses: dict[str, Matrix4] = {chain.root_link: current}
    origins: list[Vector3] = []
    axes: list[Vector3] = []
    for joint in chain.joints:
        current = multiply(current, multiply(translation(joint.origin_xyz_mm), rpy_matrix(joint.origin_rpy_rad)))
        if joint.joint_type != "fixed":
            origins.append(transform_point(current, (0.0, 0.0, 0.0)))
            axes.append(rotate_vector(current, joint.axis))
        value = by_name.get(joint.name, 0.0)
        if joint.joint_type in {"revolute", "continuous"}:
            current = multiply(current, axis_rotation(joint.axis, value))
        elif joint.joint_type == "prismatic":
            current = multiply(current, translation(tuple(component * value * 1000.0 for component in joint.axis)))  # type: ignore[arg-type]
        elif joint.joint_type != "fixed":
            raise ValueError(f"unsupported URDF joint type {joint.joint_type!r}")
        poses[joint.child] = current
    tcp_pose = current
    if tcp is not None:
        if tcp.link_id != chain.tip_link:
            raise ValueError(f"TCP link {tcp.link_id!r} is not chain tip {chain.tip_link!r}")
        tcp_pose = multiply(current, multiply(translation(tcp.translation_mm), rpy_matrix(tcp.rotation_rpy_rad)))
    return ForwardKinematics(poses, tcp_pose, tuple(origins), tuple(axes))


def pose_translation(matrix: Matrix4) -> Vector3:
    return (matrix[0][3], matrix[1][3], matrix[2][3])


def geometric_jacobian(chain: RobotChain, values: Iterable[float], tcp: ToolCenterPoint | None = None) -> tuple[tuple[float, ...], ...]:
    fk = forward_kinematics(chain, values, tcp)
    endpoint = pose_translation(fk.tcp_pose)
    columns: list[tuple[float, float, float, float, float, float]] = []
    for joint, origin, axis in zip(chain.movable_joints, fk.joint_origins_mm, fk.joint_axes_world):
        if joint.joint_type == "prismatic":
            linear = tuple(component * 1000.0 for component in axis)
            angular = (0.0, 0.0, 0.0)
        else:
            delta = tuple(endpoint[i] - origin[i] for i in range(3))
            linear = _cross(axis, delta)
            angular = axis
        columns.append(linear + angular)
    return tuple(tuple(column[row] for column in columns) for row in range(6))


def analyze_singularity(chain: RobotChain, values: Iterable[float], *, threshold: float = 1e-6) -> SingularityAnalysis:
    jacobian = geometric_jacobian(chain, values)
    movable = len(chain.movable_joints)
    rows = jacobian[:3] if movable <= 3 else jacobian
    gram = [[sum(row[i] * row[j] for row in rows) for j in range(movable)] for i in range(movable)]
    eigenvalues = _symmetric_eigenvalues(gram)
    singular_values = [math.sqrt(max(0.0, value)) for value in eigenvalues]
    sigma_min = min(singular_values, default=0.0)
    sigma_max = max(singular_values, default=0.0)
    condition = math.inf if sigma_min <= threshold else sigma_max / sigma_min
    return SingularityAnalysis(sigma_min <= threshold, sigma_min, sigma_max, condition, threshold)


def require_reachable(chain: RobotChain, target_mm: Vector3, *, tcp: ToolCenterPoint | None = None) -> dict[str, object]:
    """Conservative reach-envelope refusal before planning or IK."""
    fixed_span = sum(math.sqrt(sum(value * value for value in joint.origin_xyz_mm)) for joint in chain.joints)
    prismatic_extra = sum(
        max(abs(joint.limit.lower), abs(joint.limit.upper)) * 1000.0
        for joint in chain.movable_joints
        if joint.joint_type == "prismatic" and joint.limit is not None
    )
    tcp_span = 0.0 if tcp is None else math.sqrt(sum(value * value for value in tcp.translation_mm))
    max_reach = fixed_span + prismatic_extra + tcp_span
    distance = math.sqrt(sum(value * value for value in target_mm))
    if distance > max_reach + 1e-9:
        raise UnreachableTargetError(f"unreachable_target: distance_mm={distance:.9g} exceeds max_reach_mm={max_reach:.9g}")
    return {
        "reachable": True,
        "distance_mm": distance,
        "max_reach_mm": max_reach,
        "source": "simulated",
        "advisory_only": True,
        "satisfies_execute_gate": False,
    }


def _cross(a: Vector3, b: Vector3) -> Vector3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _symmetric_eigenvalues(matrix: list[list[float]]) -> list[float]:
    """Jacobi eigenvalues for a small real symmetric matrix."""
    size = len(matrix)
    values = [row[:] for row in matrix]
    for _ in range(max(1, 50 * size * size)):
        p, q, magnitude = 0, 0, 0.0
        for i in range(size):
            for j in range(i + 1, size):
                if abs(values[i][j]) > magnitude:
                    p, q, magnitude = i, j, abs(values[i][j])
        if magnitude <= 1e-12:
            break
        angle = 0.5 * math.atan2(2.0 * values[p][q], values[q][q] - values[p][p])
        c, s = math.cos(angle), math.sin(angle)
        app, aqq, apq = values[p][p], values[q][q], values[p][q]
        values[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq
        values[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq
        values[p][q] = values[q][p] = 0.0
        for k in range(size):
            if k in (p, q):
                continue
            aik, akq = values[k][p], values[k][q]
            values[k][p] = values[p][k] = c * aik - s * akq
            values[k][q] = values[q][k] = s * aik + c * akq
    return sorted(values[i][i] for i in range(size))
