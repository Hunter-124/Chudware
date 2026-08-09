"""Deterministic broad-phase collision checks for a simple box cell."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .kinematics import forward_kinematics, pose_translation
from .model import BoxObstacle, CellModel, Vector3

MAX_COLLISION_PAIR_CHECKS = 1_000_000


class CollisionResourceRefusal(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class CollisionContact:
    link_id: str
    obstacle_id: str
    point_mm: Vector3


@dataclass(frozen=True)
class CollisionResult:
    collision: bool
    contacts: tuple[CollisionContact, ...]
    source: str = "simulated"
    advisory_only: bool = True
    satisfies_execute_gate: bool = False


def check_collisions(cell: CellModel, joint_values: Iterable[float]) -> CollisionResult:
    fk = forward_kinematics(cell.chain, joint_values, cell.tcp)
    points = [pose_translation(fk.link_poses[link]) for link in cell.chain.links]
    pair_checks = max(0, len(points) - 1) * len(cell.obstacles)
    if pair_checks > MAX_COLLISION_PAIR_CHECKS:
        raise CollisionResourceRefusal(
            "collision_pair_limit",
            f"collision analysis exceeds the {MAX_COLLISION_PAIR_CHECKS} pair-check limit",
        )
    contacts: list[CollisionContact] = []
    for index, (start, end) in enumerate(zip(points, points[1:])):
        link_id = cell.chain.links[index + 1]
        for obstacle in cell.obstacles:
            hit, fraction = _segment_box(start, end, obstacle, cell.link_radius_mm)
            if hit:
                point = tuple(start[axis] + fraction * (end[axis] - start[axis]) for axis in range(3))
                contacts.append(CollisionContact(link_id, obstacle.id, point))  # type: ignore[arg-type]
    contacts.sort(key=lambda contact: (contact.link_id, contact.obstacle_id, contact.point_mm))
    return CollisionResult(bool(contacts), tuple(contacts))


def _segment_box(start: Vector3, end: Vector3, box: BoxObstacle, radius: float) -> tuple[bool, float]:
    """Slab intersection against an AABB expanded by link radius."""
    low = tuple(value - radius for value in box.minimum_mm)
    high = tuple(value + radius for value in box.maximum_mm)
    enter, leave = 0.0, 1.0
    for axis in range(3):
        delta = end[axis] - start[axis]
        if abs(delta) <= 1e-15:
            if start[axis] < low[axis] or start[axis] > high[axis]:
                return False, 0.0
            continue
        first = (low[axis] - start[axis]) / delta
        second = (high[axis] - start[axis]) / delta
        if first > second:
            first, second = second, first
        enter, leave = max(enter, first), min(leave, second)
        if enter > leave:
            return False, 0.0
    return True, enter
