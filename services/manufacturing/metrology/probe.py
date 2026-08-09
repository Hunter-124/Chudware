"""Probe definitions, typed moves/results, qualification and repeatability studies."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
import math
import statistics
from typing import Iterable, Mapping, Sequence

from .evidence import EvidenceClass
from .limits import MAX_METROLOGY_ITEMS, MetrologyResourceRefusal, bounded_tuple

Vector3 = tuple[float, float, float]
DIRECTIONS = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")


def _finite_vector(value: Sequence[float], name: str) -> Vector3:
    if len(value) != 3 or any(not math.isfinite(component) for component in value):
        raise ValueError(f"{name} must be a finite 3-vector")
    return (float(value[0]), float(value[1]), float(value[2]))


def _digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


class ProbeKind(str, Enum):
    TOUCH_TRIGGER = "touch_trigger"
    TOOL_SETTER = "tool_setter"
    TOOL_LENGTH = "tool_length"
    OHMIC = "ohmic"
    EDGE_FINDER = "edge_finder"


@dataclass(frozen=True, slots=True)
class StylusDefinition:
    ball_d_mm: float
    ball_material: str
    effective_length_mm: float
    shank_d_mm: float
    max_deflection_mm: float

    def __post_init__(self) -> None:
        values = (self.ball_d_mm, self.effective_length_mm, self.shank_d_mm,
                  self.max_deflection_mm)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("stylus dimensions must be finite and positive")


@dataclass(frozen=True, slots=True)
class ProbeMotionProfile:
    search_feed_mm_min: float
    measure_feed_mm_min: float
    retract_mm: float
    max_search_mm: float
    overtravel_mm: float
    protected_rapid_feed_mm_min: float

    def __post_init__(self) -> None:
        if any(not math.isfinite(value) or value <= 0 for value in (
                self.search_feed_mm_min, self.measure_feed_mm_min, self.retract_mm,
                self.max_search_mm, self.overtravel_mm,
                self.protected_rapid_feed_mm_min)):
            raise ValueError("probe motion values must be finite and positive")


@dataclass(frozen=True, slots=True)
class ProbeDefinition:
    id: str
    kind: ProbeKind
    tool_number: int
    stylus: StylusDefinition
    motion: ProbeMotionProfile
    repeat_sigma_mm: float
    status_required: tuple[str, ...] = ("battery_ok", "not_tripped")
    qualification_ref: str | None = None
    directions: frozenset[str] = field(default_factory=lambda: frozenset(("+X", "-X", "+Y", "-Y", "-Z")))

    def __post_init__(self) -> None:
        if not self.id or self.tool_number < 0:
            raise ValueError("probe id and non-negative tool number are required")
        if self.repeat_sigma_mm < 0 or not math.isfinite(self.repeat_sigma_mm):
            raise ValueError("repeat sigma must be finite and non-negative")
        unknown = set(self.directions) - set(DIRECTIONS)
        if unknown:
            raise ValueError(f"unknown probe directions: {sorted(unknown)}")


@dataclass(frozen=True, slots=True)
class ToolSetterDefinition:
    probe: ProbeDefinition
    z_reference_machine_mm: float
    plunger_travel_mm: float
    repeat_sigma_mm: float
    location_machine: Vector3
    approach_clearance_mm: float

    def __post_init__(self) -> None:
        if self.probe.kind is not ProbeKind.TOOL_SETTER:
            raise ValueError("tool setter definition requires a tool_setter probe")
        object.__setattr__(self, "location_machine",
                           _finite_vector(self.location_machine, "location_machine"))
        if any(not math.isfinite(value) for value in (
                self.z_reference_machine_mm, self.plunger_travel_mm,
                self.repeat_sigma_mm, self.approach_clearance_mm)):
            raise ValueError("tool setter values must be finite")
        if min(self.plunger_travel_mm, self.repeat_sigma_mm, self.approach_clearance_mm) < 0:
            raise ValueError("tool setter travel, sigma and clearance must be non-negative")


class ProbeMoveKind(str, Enum):
    PROTECTED_RAPID = "protected_rapid"
    SEARCH = "search"
    MEASURE = "measure"
    RETRACT = "retract"


@dataclass(frozen=True, slots=True)
class ProbeMove:
    kind: ProbeMoveKind
    target_machine: Vector3
    approach_direction: Vector3
    feed_mm_min: float
    max_travel_mm: float

    def __post_init__(self) -> None:
        target = _finite_vector(self.target_machine, "target_machine")
        direction = _finite_vector(self.approach_direction, "approach_direction")
        norm = math.sqrt(math.fsum(component * component for component in direction))
        if not math.isclose(norm, 1.0, abs_tol=1e-9):
            raise ValueError("approach direction must be a unit vector")
        if self.feed_mm_min <= 0 or self.max_travel_mm <= 0:
            raise ValueError("probe feed and max travel must be positive")
        object.__setattr__(self, "target_machine", target)
        object.__setattr__(self, "approach_direction", direction)


class ProbeFault(str, Enum):
    NO_TRIP = "no_trip"
    PRE_TRIPPED = "pre_tripped"
    FALSE_TRIP = "false_trip"
    LOST_FEEDBACK = "lost_feedback"
    OVERTRAVEL = "overtravel"
    UNHOMED = "unhomed"
    STATUS_UNKNOWN = "status_unknown"
    LEASE_LOST = "lease_lost"


class ProbeResultStatus(str, Enum):
    COMPLETED = "completed"
    FAULTED = "faulted"
    ABORTED = "aborted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ProbeTouch:
    seq: int
    move: ProbeMove
    trip_machine: Vector3
    surface_point_machine: Vector3
    ball_radius_mm: float
    pretravel_mm: float
    controller_seq: int
    host_t_mono_ns: int

    def __post_init__(self) -> None:
        if self.seq < 0 or self.controller_seq < 0 or self.host_t_mono_ns < 0:
            raise ValueError("probe sequence/timestamp values must be non-negative")
        object.__setattr__(self, "trip_machine", _finite_vector(self.trip_machine, "trip_machine"))
        object.__setattr__(self, "surface_point_machine",
                           _finite_vector(self.surface_point_machine, "surface_point_machine"))


def compensate_surface_point(trip_stylus_center: Sequence[float],
                             approach_direction: Sequence[float], *,
                             effective_radius_mm: float, pretravel_mm: float) -> Vector3:
    """Apply ``trip + d*(R_eff-pretravel(d))`` with the specification's fixed sign."""
    trip = _finite_vector(trip_stylus_center, "trip_stylus_center")
    direction = _finite_vector(approach_direction, "approach_direction")
    norm = math.sqrt(math.fsum(component * component for component in direction))
    if not math.isclose(norm, 1.0, abs_tol=1e-9):
        raise ValueError("approach direction must be a unit vector")
    if effective_radius_mm <= 0 or pretravel_mm < 0:
        raise ValueError("effective radius must be positive and pretravel non-negative")
    distance = effective_radius_mm - pretravel_mm
    return tuple(trip[i] + direction[i] * distance for i in range(3))  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    cycle_id: str
    status: ProbeResultStatus
    touches: tuple[ProbeTouch, ...]
    passes: int
    pass_agreement_mm: float
    program_hash: str
    derived_from: tuple[str, ...]
    fault: ProbeFault | None = None
    evidence_class: EvidenceClass = field(default=EvidenceClass.QUALIFIED, init=False)
    id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.status is ProbeResultStatus.COMPLETED and self.fault is not None:
            raise ValueError("completed probe result cannot carry a fault")
        if self.status is ProbeResultStatus.FAULTED and self.fault is None:
            raise ValueError("faulted probe result must name its fault")
        if self.passes < 0 or self.pass_agreement_mm < 0:
            raise ValueError("passes and agreement must be non-negative")
        object.__setattr__(self, "id", _digest({"cycle": self.cycle_id, "status": self.status.value,
                                                "touches": self.touches, "passes": self.passes,
                                                "agreement": self.pass_agreement_mm,
                                                "program": self.program_hash,
                                                "derived_from": self.derived_from}))


@dataclass(frozen=True, slots=True)
class RepeatabilityStudy:
    id: str
    direction: str
    samples_mm: tuple[float, ...]
    mean_mm: float
    sample_sigma_mm: float
    two_sigma_mm: float
    expires_at: datetime
    admitted: bool
    derived_from: tuple[str, ...]

    @classmethod
    def run(cls, direction: str, samples_mm: Iterable[float], *,
            max_sigma_mm: float, established_at: datetime | None = None,
            validity_days: int = 30, derived_from: Iterable[str] = ()) -> "RepeatabilityStudy":
        samples = bounded_tuple(
            (float(value) for value in samples_mm),
            label="repeatability_samples",
        )
        if direction not in DIRECTIONS:
            raise ValueError("repeatability direction must be an axis direction")
        if any(not math.isfinite(value) for value in samples):
            raise ValueError("repeatability samples must be finite")
        if max_sigma_mm < 0:
            raise ValueError("max sigma must be non-negative")
        mean = statistics.fmean(samples) if samples else math.nan
        sigma = statistics.stdev(samples) if len(samples) >= 2 else math.inf
        start = established_at or datetime.now(timezone.utc)
        if start.tzinfo is None:
            raise ValueError("established_at must be timezone-aware")
        admitted = len(samples) >= 10 and sigma <= max_sigma_mm
        roots = tuple(dict.fromkeys(bounded_tuple(derived_from, label="repeatability_roots")))
        payload = {"direction": direction, "samples": samples, "max_sigma": max_sigma_mm,
                   "derived_from": roots}
        return cls(_digest(payload), direction, samples, mean, sigma, 2.0 * sigma,
                   start + timedelta(days=validity_days), admitted, roots)


@dataclass(frozen=True, slots=True)
class StylusQualification:
    id: str
    effective_radius_mm: float
    isotropic: bool
    residual_rms_mm: float
    directional_pretravel_mm: Mapping[str, float]
    passes: int
    pass_agreement_mm: float
    artifact_ref: str
    expires_at: datetime
    derived_from: tuple[str, ...]

    @classmethod
    def fit(cls, *, certified_diameter_mm: float, fitted_diameter_mm: float,
            nominal_ball_radius_mm: float, residual_rms_mm: float,
            residual_limit_mm: float, directional_pretravel_mm: Mapping[str, float],
            passes: int, pass_agreement_mm: float, artifact_ref: str,
            established_at: datetime | None = None, validity_days: int = 30,
            derived_from: Iterable[str] = ()) -> "StylusQualification":
        if len(directional_pretravel_mm) > MAX_METROLOGY_ITEMS:
            raise MetrologyResourceRefusal(
                "directional_pretravel_limit",
                f"directional pretravel exceeds the {MAX_METROLOGY_ITEMS} item limit",
            )
        if certified_diameter_mm <= 0 or fitted_diameter_mm <= 0 or nominal_ball_radius_mm <= 0:
            raise ValueError("qualification dimensions must be positive")
        if residual_rms_mm < 0 or residual_rms_mm > residual_limit_mm:
            raise ValueError("stylus qualification residual exceeds limit")
        if any(direction not in DIRECTIONS or value < 0 or not math.isfinite(value)
               for direction, value in directional_pretravel_mm.items()):
            raise ValueError("directional pretravel must use axis directions and finite values")
        start = established_at or datetime.now(timezone.utc)
        if start.tzinfo is None:
            raise ValueError("established_at must be timezone-aware")
        # The fitted-vs-certified radial bias is measured and applied to the nominal tip radius.
        effective_radius = nominal_ball_radius_mm + (certified_diameter_mm - fitted_diameter_mm) / 2.0
        if effective_radius <= 0:
            raise ValueError("qualification produced a non-positive effective radius")
        # Directional pretravel is expected to vary.  Isotropy is established by the fitted
        # spherical/ring artifact residual, not by pretending directional pretravel is equal.
        isotropic = True
        roots = tuple(
            dict.fromkeys(
                (artifact_ref, *bounded_tuple(derived_from, label="qualification_roots"))
            )
        )
        payload = {"certified": certified_diameter_mm, "fitted": fitted_diameter_mm,
                   "nominal_radius": nominal_ball_radius_mm, "residual": residual_rms_mm,
                   "pretravel": dict(directional_pretravel_mm), "passes": passes,
                   "agreement": pass_agreement_mm, "artifact": artifact_ref,
                   "derived_from": roots}
        return cls(_digest(payload), effective_radius, isotropic, residual_rms_mm,
                   dict(directional_pretravel_mm), passes, pass_agreement_mm,
                   artifact_ref, start + timedelta(days=validity_days), roots)


@dataclass(frozen=True, slots=True)
class MachineArtifactStudy:
    id: str
    measured_errors_mm: tuple[float, ...]
    orientations: int
    machine_positioning_measured: bool
    certificate_uncertainty_mm: float | None
    environment_recorded: bool
    expires_at: datetime
    admitted: bool
    derived_from: tuple[str, ...]

    @classmethod
    def run(cls, errors_mm: Iterable[float], *, orientations: int,
            certificate_uncertainty_mm: float | None, environment_recorded: bool,
            max_abs_error_mm: float, established_at: datetime | None = None,
            validity_days: int = 90, derived_from: Iterable[str] = ()) -> "MachineArtifactStudy":
        errors = bounded_tuple(
            (float(value) for value in errors_mm),
            label="artifact_errors",
        )
        if any(not math.isfinite(value) for value in errors):
            raise ValueError("artifact errors must be finite")
        start = established_at or datetime.now(timezone.utc)
        if start.tzinfo is None:
            raise ValueError("established_at must be timezone-aware")
        measured = bool(errors) and orientations >= 3
        admitted = (measured and certificate_uncertainty_mm is not None
                    and certificate_uncertainty_mm >= 0 and environment_recorded
                    and max(abs(value) for value in errors) <= max_abs_error_mm)
        roots = tuple(dict.fromkeys(bounded_tuple(derived_from, label="artifact_roots")))
        return cls(_digest({"errors": errors, "orientations": orientations,
                            "certificate_uncertainty": certificate_uncertainty_mm,
                            "environment": environment_recorded,
                            "derived_from": roots}),
                   errors, orientations, measured, certificate_uncertainty_mm,
                   environment_recorded, start + timedelta(days=validity_days), admitted,
                   roots)
