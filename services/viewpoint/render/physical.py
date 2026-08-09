"""Map a CAD candidate pose into the metrology machine-frame transform graph."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Sequence

from services.manufacturing.metrology.transform import Pose, TransformGraph
from services.viewpoint.geometry import CameraPose

Vector3 = tuple[float, float, float]


def _vector(values: Sequence[float]) -> Vector3:
    if len(values) != 3:
        raise ValueError("vector must have three coordinates")
    result = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in result):
        raise ValueError("vector coordinates must be finite")
    return result  # type: ignore[return-value]


def _sub(a: Vector3, b: Vector3) -> Vector3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _norm(value: Vector3) -> float:
    return math.sqrt(math.fsum(component * component for component in value))


def _unit(value: Vector3) -> Vector3:
    length = _norm(value)
    if length <= 1e-12:
        raise ValueError("direction must be non-zero")
    return tuple(component / length for component in value)  # type: ignore[return-value]


def _rotate(pose: Pose, value: Sequence[float]) -> Vector3:
    moved = pose.apply(_vector(value))
    return _sub(moved, pose.translation_mm)


def _quaternion_from_axes(right: Vector3, up: Vector3, backward: Vector3) -> tuple[float, float, float, float]:
    """Quaternion for the rotation whose columns are camera +X, +Y, +Z axes."""
    m00, m01, m02 = right[0], up[0], backward[0]
    m10, m11, m12 = right[1], up[1], backward[1]
    m20, m21, m22 = right[2], up[2], backward[2]
    trace = m00 + m11 + m22
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        q = ((m21 - m12) / scale, (m02 - m20) / scale,
             (m10 - m01) / scale, 0.25 * scale)
    elif m00 > m11 and m00 > m22:
        scale = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        q = (0.25 * scale, (m01 + m10) / scale,
             (m02 + m20) / scale, (m21 - m12) / scale)
    elif m11 > m22:
        scale = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        q = ((m01 + m10) / scale, 0.25 * scale,
             (m12 + m21) / scale, (m02 - m20) / scale)
    else:
        scale = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        q = ((m02 + m20) / scale, (m12 + m21) / scale,
             0.25 * scale, (m10 - m01) / scale)
    return q


@dataclass(frozen=True, slots=True)
class PhysicalCameraInstruction:
    """Advisory placement/aim instruction expressed in a metrology machine frame.

    ``camera_pose_machine`` uses a right-handed camera frame: +X right, +Y image-up, and
    -Z along the optical axis. It is a metrology ``Pose`` and therefore composes with the same
    frame graph as fixture, stock, station, and probe registrations.
    """

    fixture_frame: str
    machine_frame: str
    camera_pose_machine: Pose
    target_machine_mm: Vector3
    aim_direction_machine: Vector3
    up_direction_machine: Vector3
    working_distance_mm: float
    vertical_fov_deg: float
    image_size_px: tuple[int, int]
    transform_edge_ids: tuple[str, ...]
    transform_uncertainty_unknown: bool

    @property
    def position_machine_mm(self) -> Vector3:
        return self.camera_pose_machine.translation_mm



def pose_to_physical_instruction(camera_pose: CameraPose, *, transforms: TransformGraph,
                                 fixture_frame: str, units_to_mm: float,
                                 machine_frame: str = "machine",
                                 at: datetime | None = None) -> PhysicalCameraInstruction:
    """Transform an analytic fixture-frame pose into a physical machine-frame instruction.

    ``units_to_mm`` is mandatory because the Stage-1 analytic corpus is scale-normalized and does
    not itself prove that one coordinate unit is one millimetre. A CAD document in native mm passes
    ``1.0``; any other value must come from the fixture/CAD registration, not this bridge.
    """
    if not isinstance(camera_pose, CameraPose):
        raise ValueError("camera_pose must be services.viewpoint.geometry.CameraPose")
    if not isinstance(transforms, TransformGraph):
        raise ValueError("transforms must be services.manufacturing.metrology.TransformGraph")
    if not isinstance(fixture_frame, str) or not fixture_frame.strip():
        raise ValueError("fixture_frame must be a non-empty string")
    if not isinstance(machine_frame, str) or not machine_frame.strip():
        raise ValueError("machine_frame must be a non-empty string")
    scale = float(units_to_mm)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("units_to_mm must be finite and positive")

    resolved = transforms.resolve(fixture_frame, machine_frame, at=at)
    eye_fixture_mm = tuple(float(value) * scale for value in camera_pose.position)
    target_fixture_mm = tuple(float(value) * scale for value in camera_pose.target)
    eye_machine = resolved.pose.apply(eye_fixture_mm)
    target_machine = resolved.pose.apply(target_fixture_mm)
    forward_machine = _unit(_sub(target_machine, eye_machine))
    right_machine = _unit(_rotate(resolved.pose, camera_pose.right))
    up_machine = _unit(_rotate(resolved.pose, camera_pose.true_up))
    backward_machine = tuple(-value for value in forward_machine)
    rotation = _quaternion_from_axes(right_machine, up_machine, backward_machine)  # type: ignore[arg-type]
    camera_frame = Pose(eye_machine, rotation)

    return PhysicalCameraInstruction(
        fixture_frame=fixture_frame,
        machine_frame=machine_frame,
        camera_pose_machine=camera_frame,
        target_machine_mm=target_machine,
        aim_direction_machine=forward_machine,
        up_direction_machine=up_machine,
        working_distance_mm=_norm(_sub(target_machine, eye_machine)),
        vertical_fov_deg=camera_pose.vertical_fov_deg,
        image_size_px=(camera_pose.image_width_px, camera_pose.image_height_px),
        transform_edge_ids=resolved.edge_ids,
        transform_uncertainty_unknown=resolved.uncertainty_unknown,
    )
