"""Canonical offline robot-cell records (millimetres, radians)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

Vector3: TypeAlias = tuple[float, float, float]
Matrix4: TypeAlias = tuple[tuple[float, float, float, float], tuple[float, float, float, float], tuple[float, float, float, float], tuple[float, float, float, float]]


@dataclass(frozen=True)
class JointLimit:
    lower: float
    upper: float
    velocity: float | None = None


@dataclass(frozen=True)
class RobotJoint:
    name: str
    joint_type: str
    parent: str
    child: str
    origin_xyz_mm: Vector3 = (0.0, 0.0, 0.0)
    origin_rpy_rad: Vector3 = (0.0, 0.0, 0.0)
    axis: Vector3 = (1.0, 0.0, 0.0)
    limit: JointLimit | None = None


@dataclass(frozen=True)
class RobotChain:
    name: str
    root_link: str
    tip_link: str
    links: tuple[str, ...]
    joints: tuple[RobotJoint, ...]

    @property
    def movable_joints(self) -> tuple[RobotJoint, ...]:
        return tuple(joint for joint in self.joints if joint.joint_type != "fixed")


@dataclass(frozen=True)
class SrdfModel:
    name: str
    groups: dict[str, tuple[str, str]] = field(default_factory=dict)
    group_states: dict[str, dict[str, float]] = field(default_factory=dict)
    end_effectors: dict[str, tuple[str, str]] = field(default_factory=dict)
    disabled_collisions: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class Frame:
    id: str
    parent_id: str
    translation_mm: Vector3
    rotation_rpy_rad: Vector3 = (0.0, 0.0, 0.0)
    source: str = "declared"


@dataclass(frozen=True)
class ToolCenterPoint:
    id: str
    link_id: str
    translation_mm: Vector3
    rotation_rpy_rad: Vector3 = (0.0, 0.0, 0.0)
    source: str = "declared"


@dataclass(frozen=True)
class Payload:
    id: str
    mass_kg: float
    center_of_mass_mm: Vector3
    source: str = "declared"


@dataclass(frozen=True)
class BoxObstacle:
    id: str
    minimum_mm: Vector3
    maximum_mm: Vector3

    def __post_init__(self) -> None:
        if any(lo > hi for lo, hi in zip(self.minimum_mm, self.maximum_mm)):
            raise ValueError("obstacle minimum must not exceed maximum")


@dataclass(frozen=True)
class CellModel:
    id: str
    chain: RobotChain
    frames: tuple[Frame, ...] = ()
    tcp: ToolCenterPoint | None = None
    payload: Payload | None = None
    obstacles: tuple[BoxObstacle, ...] = ()
    link_radius_mm: float = 10.0
