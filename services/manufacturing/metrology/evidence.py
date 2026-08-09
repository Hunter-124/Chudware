"""Nominal metrology evidence types and measured uncertainty budgets.

Evidence class is derived from a closed device-kind table.  It is intentionally absent
from every public constructor: callers cannot relabel camera evidence as probe evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
import json
import math
from typing import Any, ClassVar, Iterable


class EvidenceClass(str, Enum):
    ADVISORY = "advisory"
    QUALIFIED = "qualified"
    SIMULATED = "simulated"


class AdvisoryTier(str, Enum):
    A0 = "A0_indicative"
    A1 = "A1_scaled"
    A2 = "A2_station"


class QualifiedTier(str, Enum):
    Q0 = "Q0_unqualified"
    Q1 = "Q1_repeatable"
    Q2 = "Q2_qualified"
    Q3 = "Q3_traceable"


_DEVICE_EVIDENCE_CLASS: dict[str, EvidenceClass] = {
    "camera": EvidenceClass.ADVISORY,
    "fixed_camera": EvidenceClass.ADVISORY,
    "touch_probe": EvidenceClass.QUALIFIED,
    "tool_setter": EvidenceClass.QUALIFIED,
    "tool_length_probe": EvidenceClass.QUALIFIED,
    "ohmic_probe": EvidenceClass.QUALIFIED,
    "edge_finder": EvidenceClass.QUALIFIED,
    "twin_probe": EvidenceClass.SIMULATED,
}


def evidence_class_for_device_kind(kind: str) -> EvidenceClass:
    """Return the immutable class for a known device kind; unknown kinds fail closed."""
    try:
        return _DEVICE_EVIDENCE_CLASS[kind]
    except KeyError as exc:
        raise ValueError(f"unknown metrology device kind: {kind}") from exc


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    kind: str
    id: str
    serial: str | None = None
    firmware: str | None = None

    def __post_init__(self) -> None:
        evidence_class_for_device_kind(self.kind)
        if not self.id:
            raise ValueError("device id must not be empty")


class ComponentSource(str, Enum):
    MEASURED = "measured"
    ASSUMED = "assumed"


@dataclass(frozen=True, slots=True)
class UncertaintyComponent:
    name: str
    u_mm: float
    source: ComponentSource
    ref: str | None = None
    distribution: str = "normal"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("uncertainty component name must not be empty")
        if not math.isfinite(self.u_mm) or self.u_mm < 0:
            raise ValueError("standard uncertainty must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class UncertaintyBudget:
    components: tuple[UncertaintyComponent, ...]
    k: float = 2.0
    u_combined_mm: float = field(init=False)
    expanded_mm: float = field(init=False)

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError("qualified uncertainty requires measured or assumed components")
        if not math.isfinite(self.k) or self.k <= 0:
            raise ValueError("coverage factor k must be positive")
        combined = math.sqrt(math.fsum(c.u_mm * c.u_mm for c in self.components))
        object.__setattr__(self, "u_combined_mm", combined)
        object.__setattr__(self, "expanded_mm", self.k * combined)

    @classmethod
    def measured(cls, components: Iterable[UncertaintyComponent], *, k: float = 2.0) -> "UncertaintyBudget":
        """Compute the GUM root-sum-square from input measurements; no tier table is used."""
        return cls(tuple(components), k)

    @property
    def basis(self) -> ComponentSource:
        return (ComponentSource.MEASURED if all(c.source is ComponentSource.MEASURED for c in self.components)
                else ComponentSource.ASSUMED)

    def component(self, name: str) -> UncertaintyComponent | None:
        return next((component for component in self.components if component.name == name), None)


def _content_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + sha256(encoded.encode("utf-8")).hexdigest()


def _roots(derived_from: Iterable[str]) -> tuple[str, ...]:
    roots = tuple(dict.fromkeys(derived_from))
    if any(not root for root in roots):
        raise ValueError("derived_from roots must not be empty strings")
    return roots


@dataclass(frozen=True, slots=True, init=False)
class AdvisoryObservation:
    """Camera-derived evidence.  This type has no qualified fields or conversion path."""
    EVIDENCE_CLASS: ClassVar[EvidenceClass] = EvidenceClass.ADVISORY
    id: str
    device: DeviceIdentity
    tier: AdvisoryTier
    machine_id: str
    value: Any
    units: str
    uncertainty_mm: float | None
    derived_from: tuple[str, ...]
    raw_ref: str | None
    _evidence_class: EvidenceClass

    def __init__(self, *, device: DeviceIdentity, tier: AdvisoryTier, machine_id: str,
                 value: Any, units: str = "mm", uncertainty_mm: float | None = None,
                 derived_from: Iterable[str] = (), raw_ref: str | None = None) -> None:
        evidence_class = evidence_class_for_device_kind(device.kind)
        if evidence_class is not EvidenceClass.ADVISORY:
            raise TypeError(f"{device.kind} cannot construct AdvisoryObservation")
        if tier is AdvisoryTier.A0 and uncertainty_mm is not None:
            raise ValueError("A0 refuses a numeric uncertainty")
        if (tier is AdvisoryTier.A0 and isinstance(value, (int, float))
                and not isinstance(value, bool)):
            raise ValueError("A0 refuses a numeric measurement value")
        if tier is not AdvisoryTier.A0 and (uncertainty_mm is None or uncertainty_mm < 0):
            raise ValueError("A1/A2 require a measured uncertainty")
        roots = _roots(derived_from)
        payload = {"device": device.id, "tier": tier.value, "machine": machine_id,
                   "value": value, "units": units, "uncertainty": uncertainty_mm,
                   "derived_from": roots, "raw_ref": raw_ref}
        object.__setattr__(self, "id", _content_id(payload))
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "tier", tier)
        object.__setattr__(self, "machine_id", machine_id)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "units", units)
        object.__setattr__(self, "uncertainty_mm", uncertainty_mm)
        object.__setattr__(self, "derived_from", roots)
        object.__setattr__(self, "raw_ref", raw_ref)
        object.__setattr__(self, "_evidence_class", evidence_class)

    @property
    def evidence_class(self) -> EvidenceClass:
        return self._evidence_class

    @property
    def src(self) -> str:
        return "advisory"


@dataclass(frozen=True, slots=True, init=False)
class QualifiedObservation:
    """Probe-derived evidence with a complete uncertainty budget and qualification binding."""
    EVIDENCE_CLASS: ClassVar[EvidenceClass] = EvidenceClass.QUALIFIED
    id: str
    device: DeviceIdentity
    tier: QualifiedTier
    machine_id: str
    value: Any
    units: str
    uncertainty: UncertaintyBudget
    qualification_id: str
    derived_from: tuple[str, ...]
    frame: str
    frame_graph_revision: int
    _evidence_class: EvidenceClass

    def __init__(self, *, device: DeviceIdentity, tier: QualifiedTier, machine_id: str,
                 value: Any, uncertainty: UncertaintyBudget, qualification_id: str,
                 derived_from: Iterable[str], units: str = "mm", frame: str = "machine",
                 frame_graph_revision: int = 0) -> None:
        evidence_class = evidence_class_for_device_kind(device.kind)
        if evidence_class is not EvidenceClass.QUALIFIED:
            raise TypeError(f"{device.kind} cannot construct QualifiedObservation")
        if tier is QualifiedTier.Q0:
            raise ValueError("Q0 cannot emit a qualified measurement")
        if not qualification_id:
            raise ValueError("qualified observation requires a qualification id")
        roots = _roots(derived_from)
        payload = {"device": device.id, "tier": tier.value, "machine": machine_id,
                   "value": value, "units": units, "U": uncertainty.expanded_mm,
                   "qualification": qualification_id, "derived_from": roots,
                   "frame": frame, "frame_graph_revision": frame_graph_revision}
        object.__setattr__(self, "id", _content_id(payload))
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "tier", tier)
        object.__setattr__(self, "machine_id", machine_id)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "units", units)
        object.__setattr__(self, "uncertainty", uncertainty)
        object.__setattr__(self, "qualification_id", qualification_id)
        object.__setattr__(self, "derived_from", roots)
        object.__setattr__(self, "frame", frame)
        object.__setattr__(self, "frame_graph_revision", frame_graph_revision)
        object.__setattr__(self, "_evidence_class", evidence_class)

    @property
    def evidence_class(self) -> EvidenceClass:
        return self._evidence_class

    @property
    def src(self) -> str:
        return "probed"


@dataclass(frozen=True, slots=True, init=False)
class SimulatedObservation:
    """Twin context, disjoint from both physical evidence ladders."""
    EVIDENCE_CLASS: ClassVar[EvidenceClass] = EvidenceClass.SIMULATED
    device: DeviceIdentity
    value: Any
    derived_from: tuple[str, ...]
    _evidence_class: EvidenceClass

    def __init__(self, *, device: DeviceIdentity, value: Any,
                 derived_from: Iterable[str] = ()) -> None:
        evidence_class = evidence_class_for_device_kind(device.kind)
        if evidence_class is not EvidenceClass.SIMULATED:
            raise TypeError(f"{device.kind} cannot construct SimulatedObservation")
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "derived_from", _roots(derived_from))
        object.__setattr__(self, "_evidence_class", evidence_class)

    @property
    def evidence_class(self) -> EvidenceClass:
        return self._evidence_class

    @property
    def src(self) -> str:
        return "simulated"
