"""Offline joint trajectory preview, non-executable export, and mock replay."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Iterable

from .model import RobotChain


@dataclass(frozen=True)
class TrajectoryPoint:
    time_s: float
    positions: tuple[float, ...]


@dataclass(frozen=True)
class Trajectory:
    id: str
    joint_names: tuple[str, ...]
    points: tuple[TrajectoryPoint, ...]
    not_for_execution: bool = True


@dataclass(frozen=True)
class MockReplay:
    trajectory_id: str
    controller: str
    samples: tuple[TrajectoryPoint, ...]
    completed: bool
    deterministic_digest: str
    source: str = "simulated"
    advisory_only: bool = True
    satisfies_execute_gate: bool = False


def preview_trajectory(
    chain: RobotChain,
    waypoints: Iterable[Iterable[float]],
    *,
    speed_rad_s: float = 1.0,
    sample_period_s: float = 0.1,
    trajectory_id: str = "offline-preview",
) -> Trajectory:
    if speed_rad_s <= 0.0 or sample_period_s <= 0.0:
        raise ValueError("trajectory speed and sample period must be positive")
    source = tuple(tuple(float(value) for value in waypoint) for waypoint in waypoints)
    joint_names = tuple(joint.name for joint in chain.movable_joints)
    if len(source) < 2:
        raise ValueError("trajectory preview needs at least two waypoints")
    if any(len(waypoint) != len(joint_names) for waypoint in source):
        raise ValueError(f"every waypoint must contain {len(joint_names)} joint values")
    for waypoint_index, waypoint in enumerate(source):
        for joint, value in zip(chain.movable_joints, waypoint):
            if not math.isfinite(value):
                raise ValueError(f"waypoint {waypoint_index} joint {joint.name} is not finite")
            if joint.limit is not None and not joint.limit.lower <= value <= joint.limit.upper:
                raise ValueError(
                    f"waypoint {waypoint_index} joint {joint.name} violates "
                    f"[{joint.limit.lower}, {joint.limit.upper}]"
                )
    points: list[TrajectoryPoint] = [TrajectoryPoint(0.0, source[0])]
    elapsed = 0.0
    for start, end in zip(source, source[1:]):
        duration = max(abs(b - a) for a, b in zip(start, end)) / speed_rad_s
        steps = max(1, int(round(duration / sample_period_s)))
        duration = steps * sample_period_s
        for step in range(1, steps + 1):
            alpha = step / steps
            positions = tuple(a + alpha * (b - a) for a, b in zip(start, end))
            points.append(TrajectoryPoint(round(elapsed + step * sample_period_s, 12), positions))
        elapsed += duration
    return Trajectory(trajectory_id, joint_names, tuple(points))


def export_trajectory(trajectory: Trajectory, path: str | Path, *, format: str | None = None) -> Path:
    target = Path(path)
    selected = (format or target.suffix.lstrip(".")).lower()
    if selected == "json":
        payload = {
            "schema": "chudware.robot.trajectory/1",
            "not_for_execution": True,
            "evidence_class": "advisory",
            "trajectory": asdict(trajectory),
        }
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif selected == "csv":
        stream = io.StringIO(newline="")
        stream.write("# not_for_execution=true; evidence_class=advisory\n")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("time_s",) + trajectory.joint_names)
        for point in trajectory.points:
            writer.writerow((format_float(point.time_s),) + tuple(format_float(value) for value in point.positions))
        target.write_text(stream.getvalue(), encoding="utf-8")
    else:
        raise ValueError(f"unsupported offline trajectory export format {selected!r}")
    return target


def mock_controller_run(trajectory: Trajectory) -> MockReplay:
    """Replay ideal command-to-state mirroring; never contacts a controller."""
    canonical = json.dumps(asdict(trajectory), sort_keys=True, separators=(",", ":")).encode()
    return MockReplay(
        trajectory_id=trajectory.id,
        controller="deterministic_mock_controller",
        samples=trajectory.points,
        completed=True,
        deterministic_digest=hashlib.sha256(canonical).hexdigest(),
    )


def format_float(value: float) -> str:
    return format(value, ".15g")
