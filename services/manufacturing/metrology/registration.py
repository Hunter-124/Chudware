"""Probe-established stock/workspace registration with advisory-only seeding."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Iterable, Mapping, Sequence

from .evidence import AdvisoryObservation, QualifiedObservation, QualifiedTier
from .limits import bounded_tuple
from .transform import Pose

Vector3 = tuple[float, float, float]


def _require_qualified(observations: Iterable[QualifiedObservation]) -> tuple[QualifiedObservation, ...]:
    values = bounded_tuple(observations, label="registration_observations")
    if not values:
        raise ValueError("registration requires qualified observations")
    if any(type(value) is not QualifiedObservation for value in values):
        raise TypeError("registration accepts QualifiedObservation only")
    return values


@dataclass(frozen=True, slots=True)
class AdvisorySeed:
    expected_position_machine: Vector3
    seeded_dimension_mm: float
    observation: AdvisoryObservation

    def __post_init__(self) -> None:
        if len(self.expected_position_machine) != 3:
            raise ValueError("seed position must have three coordinates")
        if self.seeded_dimension_mm <= 0:
            raise ValueError("seeded dimension must be positive")


def seed_disagreement_limit(seeded_dimension_mm: float) -> float:
    return max(5.0, 0.25 * seeded_dimension_mm)


def verify_seed(seed: AdvisorySeed, probed_position_machine: Sequence[float]) -> None:
    if len(probed_position_machine) != 3:
        raise ValueError("probed position must have three coordinates")
    disagreement = math.sqrt(math.fsum(
        (float(probed_position_machine[i]) - seed.expected_position_machine[i]) ** 2
        for i in range(3)))
    if disagreement > seed_disagreement_limit(seed.seeded_dimension_mm):
        raise ValueError("advisory seed disagreement requires fresh approval")


class RegistrationVerdict(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class StockRegistration:
    stock_id: str
    frame: str
    pose_machine: Pose
    measured_extents_mm: Vector3
    skew_mm: float
    cam_bounds_deviation_mm: Vector3
    verdict: RegistrationVerdict
    evidence_ids: tuple[str, ...]
    derived_from: tuple[str, ...]


def register_stock(*, stock_id: str, observations: Sequence[QualifiedObservation],
                   pose_machine: Pose, measured_extents_mm: Sequence[float],
                   cam_extents_mm: Sequence[float], max_skew_mm: float,
                   measured_skew_mm: float, seed: AdvisorySeed | None = None) -> StockRegistration:
    evidence = _require_qualified(observations)
    if any(item.tier not in (QualifiedTier.Q1, QualifiedTier.Q2, QualifiedTier.Q3)
           for item in evidence):
        raise ValueError("stock registration requires Q1 or better")
    if len(measured_extents_mm) != 3 or len(cam_extents_mm) != 3:
        raise ValueError("stock extents must be 3-vectors")
    if seed is not None:
        verify_seed(seed, pose_machine.translation_mm)
    measured = tuple(float(v) for v in measured_extents_mm)
    deviation = tuple(measured[i] - float(cam_extents_mm[i]) for i in range(3))
    verdict = (RegistrationVerdict.ACCEPTED if abs(measured_skew_mm) <= max_skew_mm
               else RegistrationVerdict.REJECTED)
    roots = tuple(dict.fromkeys(root for item in evidence for root in (item.id, *item.derived_from)))
    return StockRegistration(stock_id, f"stock:{stock_id}", pose_machine, measured,
                             measured_skew_mm, deviation, verdict,
                             tuple(item.id for item in evidence), roots)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class KeepOutVolume:
    id: str
    minimum_machine: Vector3
    maximum_machine: Vector3

    def __post_init__(self) -> None:
        if len(self.minimum_machine) != 3 or len(self.maximum_machine) != 3:
            raise ValueError("keep-out bounds must be 3-vectors")
        if any(self.minimum_machine[i] >= self.maximum_machine[i] for i in range(3)):
            raise ValueError("keep-out minimum must be below maximum")


@dataclass(frozen=True, slots=True)
class WorkspaceRegistration:
    workspace_id: str
    fixture_frames: Mapping[str, Pose]
    keep_out_volumes: tuple[KeepOutVolume, ...]
    evidence_ids: tuple[str, ...]
    derived_from: tuple[str, ...]


def register_workspace(*, workspace_id: str,
                       observations: Sequence[QualifiedObservation],
                       fixture_frames: Mapping[str, Pose],
                       keep_out_volumes: Iterable[KeepOutVolume]) -> WorkspaceRegistration:
    evidence = _require_qualified(observations)
    roots = tuple(dict.fromkeys(root for item in evidence for root in (item.id, *item.derived_from)))
    frames = dict(bounded_tuple(fixture_frames.items(), label="fixture_frames"))
    volumes = bounded_tuple(keep_out_volumes, label="keep_out_volumes")
    return WorkspaceRegistration(workspace_id, frames, volumes,
                                 tuple(item.id for item in evidence), roots)
