"""Offline joint trajectory preview, non-executable export, and mock replay."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
import hashlib
import io
from itertools import islice
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterable

from .model import RobotChain

MAX_WAYPOINTS = 10_000
MAX_TRAJECTORY_POINTS = 100_000
MAX_TRAJECTORY_VALUES = 1_000_000
MAX_ROBOT_JOINTS = 1_024


class RobotResourceRefusal(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _safe_output_path(path: str | Path, root_dir: str | Path | None) -> Path:
    raw = Path(path).expanduser()
    if ".." in raw.parts:
        raise RobotResourceRefusal(
            "trajectory_path_traversal",
            "trajectory output path may not contain parent traversal",
        )
    root = None
    if root_dir is not None:
        root_raw = Path(root_dir).expanduser()
        root = root_raw if root_raw.is_absolute() else Path.cwd() / root_raw
        root = Path(os.path.abspath(root))
    target = raw if raw.is_absolute() else (root or Path.cwd()) / raw
    target = Path(os.path.abspath(target))
    if root is not None and not target.is_relative_to(root):
        raise RobotResourceRefusal(
            "trajectory_path_escape",
            "trajectory output path is outside the allowed root",
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        current = Path(target.parent.anchor)
        for component in target.parent.parts[1:]:
            current /= component
            if stat.S_ISLNK(current.lstat().st_mode):
                raise RobotResourceRefusal(
                    "trajectory_path_symlink",
                    "trajectory output path may not use symlink directories",
                )
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(mode):
                raise RobotResourceRefusal(
                    "trajectory_path_symlink",
                    "trajectory output may not follow a symlink",
                )
            if not stat.S_ISREG(mode):
                raise RobotResourceRefusal(
                    "trajectory_path_not_regular",
                    "trajectory output must be a regular file",
                )
    except RobotResourceRefusal:
        raise
    except OSError:
        raise RobotResourceRefusal(
            "trajectory_path_unavailable",
            "trajectory output path could not be inspected",
        ) from None
    return target


def _atomic_output(path: Path, payload: str) -> None:
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise RobotResourceRefusal(
            "trajectory_output_unavailable",
            "trajectory output could not be written",
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    if (
        not math.isfinite(speed_rad_s)
        or not math.isfinite(sample_period_s)
        or speed_rad_s <= 0.0
        or sample_period_s <= 0.0
    ):
        raise ValueError("trajectory speed and sample period must be positive finite values")
    joint_names = tuple(joint.name for joint in chain.movable_joints)
    if len(joint_names) > MAX_ROBOT_JOINTS:
        raise RobotResourceRefusal(
            "trajectory_joint_limit",
            f"trajectory exceeds the {MAX_ROBOT_JOINTS} joint limit",
        )
    raw_waypoints = tuple(islice(waypoints, MAX_WAYPOINTS + 1))
    if len(raw_waypoints) > MAX_WAYPOINTS:
        raise RobotResourceRefusal(
            "trajectory_waypoint_limit",
            f"trajectory exceeds the {MAX_WAYPOINTS} waypoint limit",
        )
    if len(raw_waypoints) * max(1, len(joint_names)) > MAX_TRAJECTORY_VALUES:
        raise RobotResourceRefusal(
            "trajectory_value_limit",
            f"trajectory exceeds the {MAX_TRAJECTORY_VALUES} waypoint-value limit",
        )
    source = tuple(
        tuple(float(value) for value in islice(waypoint, len(joint_names) + 1))
        for waypoint in raw_waypoints
    )
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
        requested_steps = duration / sample_period_s
        if not math.isfinite(requested_steps):
            raise RobotResourceRefusal(
                "trajectory_point_limit",
                f"trajectory exceeds the {MAX_TRAJECTORY_POINTS} point limit",
            )
        steps = max(1, int(round(requested_steps)))
        if len(points) + steps > MAX_TRAJECTORY_POINTS:
            raise RobotResourceRefusal(
                "trajectory_point_limit",
                f"trajectory exceeds the {MAX_TRAJECTORY_POINTS} point limit",
            )
        if (len(points) + steps) * max(1, len(joint_names)) > MAX_TRAJECTORY_VALUES:
            raise RobotResourceRefusal(
                "trajectory_value_limit",
                f"trajectory exceeds the {MAX_TRAJECTORY_VALUES} sampled-value limit",
            )
        duration = steps * sample_period_s
        for step in range(1, steps + 1):
            alpha = step / steps
            positions = tuple(a + alpha * (b - a) for a, b in zip(start, end))
            points.append(TrajectoryPoint(round(elapsed + step * sample_period_s, 12), positions))
        elapsed += duration
    return Trajectory(trajectory_id, joint_names, tuple(points))


def _validate_trajectory_size(trajectory: Trajectory) -> None:
    if len(trajectory.points) > MAX_TRAJECTORY_POINTS:
        raise RobotResourceRefusal(
            "trajectory_point_limit",
            f"trajectory exceeds the {MAX_TRAJECTORY_POINTS} point limit",
        )
    value_count = sum(len(point.positions) for point in trajectory.points)
    if value_count > MAX_TRAJECTORY_VALUES:
        raise RobotResourceRefusal(
            "trajectory_value_limit",
            f"trajectory exceeds the {MAX_TRAJECTORY_VALUES} sampled-value limit",
        )


def export_trajectory(
    trajectory: Trajectory,
    path: str | Path,
    *,
    format: str | None = None,
    root_dir: str | Path | None = None,
) -> Path:
    target = _safe_output_path(path, root_dir)
    _validate_trajectory_size(trajectory)
    selected = (format or target.suffix.lstrip(".")).lower()
    if selected == "json":
        payload = {
            "schema": "chudware.robot.trajectory/1",
            "not_for_execution": True,
            "evidence_class": "advisory",
            "trajectory": asdict(trajectory),
        }
        _atomic_output(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    elif selected == "csv":
        stream = io.StringIO(newline="")
        stream.write("# not_for_execution=true; evidence_class=advisory\n")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("time_s",) + trajectory.joint_names)
        for point in trajectory.points:
            writer.writerow((format_float(point.time_s),) + tuple(format_float(value) for value in point.positions))
        _atomic_output(target, stream.getvalue())
    else:
        raise ValueError(f"unsupported offline trajectory export format {selected!r}")
    return target


def mock_controller_run(trajectory: Trajectory) -> MockReplay:
    """Replay ideal command-to-state mirroring; never contacts a controller."""
    _validate_trajectory_size(trajectory)
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
