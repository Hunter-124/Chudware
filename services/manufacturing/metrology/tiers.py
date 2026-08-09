"""Measured admission predicates for the A0-A2 and Q0-Q3 ladders."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Mapping, Sequence

from services.vision import fiducial as vision_fiducial
from services.vision import frames as vision_frames
from services.vision import measure as vision_measure

from .evidence import AdvisoryObservation, AdvisoryTier, DeviceIdentity, QualifiedTier
from .probe import MachineArtifactStudy, RepeatabilityStudy, StylusQualification


@dataclass(frozen=True, slots=True)
class A1Inputs:
    inlier_ratio: float
    residual_rms_px: float
    coverage: float
    planarity_cov: float | None
    endpoints_in_expanded_hull: bool
    hull_growth_board_diagonal: float
    px_per_mm: float
    length_mm: float

    @classmethod
    def from_detection(cls, detection: vision_fiducial.Detection, *, inlier_ratio: float,
                       residual_rms_px: float, coverage: float,
                       endpoints_in_expanded_hull: bool,
                       hull_growth_board_diagonal: float, length_mm: float) -> "A1Inputs":
        """Adapt the existing ChArUco primitive rather than duplicating its calibration math."""
        return cls(inlier_ratio, residual_rms_px, coverage, detection.planarity_cov,
                   endpoints_in_expanded_hull, hull_growth_board_diagonal,
                   detection.px_per_mm, length_mm)


@dataclass(frozen=True, slots=True)
class A2Inputs:
    view_count: int
    reprojection_rms_px: float
    controls_locked: bool
    controls_readback_verified: bool
    station_profile_live: bool
    artifact_verification_live: bool
    artifact_field_errors_mm: tuple[float, ...]
    px_per_mm: float
    image_coverage: float = 0.60
    depth_bands: int = 3


@dataclass(frozen=True, slots=True)
class AdvisoryTierDecision:
    tier: AdvisoryTier
    admitted: bool
    uncertainty_mm: float | None
    reasons: tuple[str, ...]


def a1_uncertainty(inputs: A1Inputs) -> float:
    """Expanded A1 uncertainty from residual, scale and pixel resolution measurements."""
    if inputs.px_per_mm <= 0 or inputs.length_mm < 0:
        raise ValueError("A1 uncertainty needs positive scale and non-negative length")
    residual_rms_mm = inputs.residual_rms_px / inputs.px_per_mm
    return 2.0 * math.sqrt((2.0 * residual_rms_mm) ** 2
                           + (0.02 * inputs.length_mm) ** 2
                           + (1.0 / inputs.px_per_mm) ** 2)


def admits_a1(inputs: A1Inputs) -> bool:
    return (inputs.inlier_ratio >= 0.80
            and inputs.residual_rms_px <= 2.0
            and inputs.coverage >= 0.25
            and inputs.planarity_cov is not None
            and inputs.planarity_cov <= 0.25
            and inputs.endpoints_in_expanded_hull
            and inputs.hull_growth_board_diagonal <= 0.50
            and inputs.px_per_mm > 0)


def _sample_stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise ValueError("sample standard deviation requires at least two values")
    mean = math.fsum(values) / len(values)
    return math.sqrt(math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1))


def a2_uncertainty(inputs: A2Inputs) -> float:
    """Expanded A2 uncertainty from the station's own artifact field study."""
    if len(inputs.artifact_field_errors_mm) < 5:
        raise ValueError("A2 needs artifact verification at five or more field positions")
    if inputs.px_per_mm <= 0:
        raise ValueError("A2 needs positive measured scale")
    s_artifact = _sample_stdev(inputs.artifact_field_errors_mm)
    return max(2.0 * s_artifact, 1.0 / inputs.px_per_mm)


def admits_a2(inputs: A2Inputs) -> bool:
    return (inputs.view_count >= 12
            and inputs.reprojection_rms_px <= 0.5
            and inputs.controls_locked
            and inputs.controls_readback_verified
            and inputs.station_profile_live
            and inputs.artifact_verification_live
            and len(inputs.artifact_field_errors_mm) >= 5
            and inputs.px_per_mm > 0
            and inputs.image_coverage >= 0.60
            and inputs.depth_bands >= 3)


def admits_a0(*, a1: A1Inputs | None = None, a2: A2Inputs | None = None) -> bool:
    """A0 is admitted precisely when neither numeric camera tier is supportable."""
    return not ((a2 is not None and admits_a2(a2)) or (a1 is not None and admits_a1(a1)))


def evaluate_advisory_tier(*, a1: A1Inputs | None = None,
                           a2: A2Inputs | None = None) -> AdvisoryTierDecision:
    if a2 is not None and admits_a2(a2):
        return AdvisoryTierDecision(AdvisoryTier.A2, True, a2_uncertainty(a2), ())
    if a1 is not None and admits_a1(a1):
        return AdvisoryTierDecision(AdvisoryTier.A1, True, a1_uncertainty(a1), ())
    return AdvisoryTierDecision(AdvisoryTier.A0, True, None,
                                ("camera evidence does not meet A1 or A2 admission",))


def observation_from_photo(*, frame: vision_frames.FrameRecord,
                           result: vision_measure.MeasureResult,
                           metrics: A1Inputs, device: DeviceIdentity,
                           machine_id: str) -> AdvisoryObservation:
    """Bind the existing photo result to the advisory ladder; never converts it to qualified."""
    if not result.ok or result.value_mm is None:
        raise ValueError(result.reason or "photo measurement failed")
    decision = evaluate_advisory_tier(a1=metrics)
    return AdvisoryObservation(device=device, tier=decision.tier, machine_id=machine_id,
                               value=result.value_mm, uncertainty_mm=decision.uncertainty_mm,
                               derived_from=(frame.path,), raw_ref=frame.path)


def _live(expires_at: datetime, at: datetime | None) -> bool:
    now = at or datetime.now(timezone.utc)
    if now.tzinfo is None or expires_at.tzinfo is None:
        raise ValueError("qualification times must be timezone-aware")
    return expires_at >= now


def admits_q0(qualification: StylusQualification | None, *, at: datetime | None = None) -> bool:
    return qualification is None or not _live(qualification.expires_at, at)


def admits_q1(qualification: StylusQualification | None,
              repeatability: RepeatabilityStudy | None, *, at: datetime | None = None) -> bool:
    return (qualification is not None and qualification.isotropic
            and _live(qualification.expires_at, at)
            and repeatability is not None and repeatability.admitted
            and _live(repeatability.expires_at, at))


def admits_q2(qualification: StylusQualification | None,
              repeatability: RepeatabilityStudy | None, *, required_directions: Sequence[str],
              pass_agreement_limit_mm: float, at: datetime | None = None) -> bool:
    if not admits_q1(qualification, repeatability, at=at) or qualification is None:
        return False
    return (qualification.passes >= 2
            and qualification.pass_agreement_mm <= pass_agreement_limit_mm
            and all(direction in qualification.directional_pretravel_mm
                    for direction in required_directions))


def admits_q3(qualification: StylusQualification | None,
              repeatability: RepeatabilityStudy | None,
              machine_artifact: MachineArtifactStudy | None, *,
              required_directions: Sequence[str], pass_agreement_limit_mm: float,
              at: datetime | None = None) -> bool:
    return (admits_q2(qualification, repeatability,
                      required_directions=required_directions,
                      pass_agreement_limit_mm=pass_agreement_limit_mm, at=at)
            and machine_artifact is not None
            and machine_artifact.admitted
            and machine_artifact.machine_positioning_measured
            and machine_artifact.certificate_uncertainty_mm is not None
            and machine_artifact.environment_recorded
            and _live(machine_artifact.expires_at, at))


def evaluate_probe_tier(qualification: StylusQualification | None,
                        repeatability: RepeatabilityStudy | None,
                        machine_artifact: MachineArtifactStudy | None = None, *,
                        required_directions: Sequence[str] = ("+X", "-X", "+Y", "-Y", "+Z", "-Z"),
                        pass_agreement_limit_mm: float = 0.01,
                        at: datetime | None = None) -> QualifiedTier:
    if admits_q3(qualification, repeatability, machine_artifact,
                 required_directions=required_directions,
                 pass_agreement_limit_mm=pass_agreement_limit_mm, at=at):
        return QualifiedTier.Q3
    if admits_q2(qualification, repeatability, required_directions=required_directions,
                 pass_agreement_limit_mm=pass_agreement_limit_mm, at=at):
        return QualifiedTier.Q2
    if admits_q1(qualification, repeatability, at=at):
        return QualifiedTier.Q1
    return QualifiedTier.Q0
