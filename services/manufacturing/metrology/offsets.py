"""Pure WCS/tool-offset proposal math.  This module has no apply or transport API."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import math
from typing import Iterable, Mapping, Sequence

from .evidence import QualifiedObservation, QualifiedTier

Vector3 = tuple[float, float, float]


class OffsetTargetKind(str, Enum):
    WCS_REGISTER = "wcs_register"
    TOOL_LENGTH_DYNAMIC = "tool_length_dynamic"
    TOOL_TABLE = "tool_table"


class LocalityUse(str, Enum):
    THIS_SETUP = "this_setup"
    DATUM_TRANSFER = "datum_transfer"
    SECOND_SIDE = "second_side"
    CONFORMANCE = "conformance"
    Q3_TRACEABLE_CLAIM = "q3_traceable_claim"


@dataclass(frozen=True, slots=True)
class LocalityBlock:
    datum_location_machine: Vector3
    cancellation_envelope_mm: Vector3
    valid_for: frozenset[LocalityUse] = frozenset((LocalityUse.THIS_SETUP,))
    basis: str = "machine_positioning:assumed"

    def __post_init__(self) -> None:
        if len(self.datum_location_machine) != 3 or len(self.cancellation_envelope_mm) != 3:
            raise ValueError("locality datum and envelope must be 3-vectors")
        if any(not math.isfinite(value) for value in self.datum_location_machine):
            raise ValueError("datum location must be finite")
        if any(not math.isfinite(value) or value <= 0 for value in self.cancellation_envelope_mm):
            raise ValueError("cancellation envelope must be finite and positive")
        if not self.valid_for:
            raise ValueError("locality validity scope must not be empty")

    def contains(self, point_machine: Sequence[float]) -> bool:
        if len(point_machine) != 3:
            raise ValueError("locality point must be a 3-vector")
        return all(abs(float(point_machine[i]) - self.datum_location_machine[i])
                   <= self.cancellation_envelope_mm[i] for i in range(3))


@dataclass(frozen=True, slots=True)
class OffsetTarget:
    kind: OffsetTargetKind
    axes: tuple[str, ...]
    register: int | None = None
    sub: int = 0
    tool_number: int | None = None

    def __post_init__(self) -> None:
        if not self.axes or any(axis not in ("X", "Y", "Z") for axis in self.axes):
            raise ValueError("offset axes must be a non-empty X/Y/Z subset")
        if self.kind is OffsetTargetKind.WCS_REGISTER and self.register is None:
            raise ValueError("WCS target requires a register")
        if self.kind is not OffsetTargetKind.WCS_REGISTER and self.tool_number is None:
            raise ValueError("tool offset target requires a tool number")


@dataclass(frozen=True, slots=True)
class OffsetProposal:
    id: str
    machine_id: str
    job_id: str
    setup_id: str
    setup_revision: int
    target: OffsetTarget
    current_mm: Mapping[str, float]
    proposed_mm: Mapping[str, float]
    delta_mm: Mapping[str, float]
    evidence_ids: tuple[str, ...]
    uncertainty_mm: float
    locality: LocalityBlock
    intended_use: LocalityUse
    derived_from: tuple[str, ...]


def _qualified_only(observations: Iterable[QualifiedObservation]) -> tuple[QualifiedObservation, ...]:
    result = tuple(observations)
    if not result:
        raise ValueError("offset proposal requires qualified observations")
    # Exact nominal type deliberately refuses AdvisoryObservation and user-defined lookalikes.
    if any(type(observation) is not QualifiedObservation for observation in result):
        raise TypeError("offset proposals accept QualifiedObservation only")
    return result


def guard_locality(*, evidence_tier: QualifiedTier, locality: LocalityBlock,
                   intended_use: LocalityUse,
                   use_location_machine: Sequence[float] | None = None) -> None:
    """Refuse Q2 datum transfer, second-side use, and any Q3 conformance claim."""
    if evidence_tier is QualifiedTier.Q2:
        if intended_use is not LocalityUse.THIS_SETUP or intended_use not in locality.valid_for:
            raise ValueError("Q2 locality_scope_exceeded: datum transfer/flip/conformance is forbidden")
        if use_location_machine is not None and not locality.contains(use_location_machine):
            raise ValueError("Q2 locality_scope_exceeded: outside cancellation envelope")
    elif intended_use is LocalityUse.Q3_TRACEABLE_CLAIM and evidence_tier is not QualifiedTier.Q3:
        raise ValueError("Q3 conformance claim requires Q3 traceable evidence")
    elif intended_use not in locality.valid_for:
        raise ValueError("requested offset use is outside the proposal validity scope")


def propose_offset(*, observations: Sequence[QualifiedObservation], machine_id: str,
                   job_id: str, setup_id: str, setup_revision: int,
                   target: OffsetTarget, current_mm: Mapping[str, float],
                   proposed_mm: Mapping[str, float], locality: LocalityBlock,
                   intended_use: LocalityUse = LocalityUse.THIS_SETUP,
                   use_location_machine: Sequence[float] | None = None,
                   max_delta_mm: float | None = None) -> OffsetProposal:
    """Compute immutable proposal data awaiting arbiter approval; performs no controller I/O."""
    evidence = _qualified_only(observations)
    if any(item.tier not in (QualifiedTier.Q2, QualifiedTier.Q3) for item in evidence):
        raise ValueError("tier_below_q2")
    if any(item.machine_id != machine_id for item in evidence):
        raise ValueError("evidence_cross_machine")
    evidence_tier = (QualifiedTier.Q3 if all(item.tier is QualifiedTier.Q3 for item in evidence)
                     else QualifiedTier.Q2)
    guard_locality(evidence_tier=evidence_tier, locality=locality,
                   intended_use=intended_use, use_location_machine=use_location_machine)
    if set(current_mm) != set(proposed_mm) or set(proposed_mm) != set(target.axes):
        raise ValueError("current/proposed axes must exactly match the target")
    delta = {axis: float(proposed_mm[axis]) - float(current_mm[axis]) for axis in target.axes}
    if any(not math.isfinite(value) for value in (*current_mm.values(), *proposed_mm.values())):
        raise ValueError("offset values must be finite")
    if max_delta_mm is not None and any(abs(value) > max_delta_mm for value in delta.values()):
        raise ValueError("delta_exceeds_limit")
    expanded = math.sqrt(math.fsum(item.uncertainty.expanded_mm ** 2 for item in evidence))
    roots = tuple(dict.fromkeys(root for item in evidence for root in (item.id, *item.derived_from)))
    payload = {"machine": machine_id, "job": job_id, "setup": setup_id,
               "revision": setup_revision, "target": target.kind.value,
               "current": dict(current_mm), "proposed": dict(proposed_mm),
               "evidence": [item.id for item in evidence], "locality": locality,
               "intended_use": intended_use.value}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    proposal_id = "sha256:" + sha256(encoded.encode("utf-8")).hexdigest()
    return OffsetProposal(proposal_id, machine_id, job_id, setup_id, setup_revision,
                          target, dict(current_mm), dict(proposed_mm), delta,
                          tuple(item.id for item in evidence), expanded, locality,
                          intended_use, roots)


def propose_wcs_offset(*, observations: Sequence[QualifiedObservation], machine_id: str,
                       job_id: str, setup_id: str, setup_revision: int, register: int,
                       current_mm: Mapping[str, float], proposed_mm: Mapping[str, float],
                       locality: LocalityBlock,
                       intended_use: LocalityUse = LocalityUse.THIS_SETUP,
                       use_location_machine: Sequence[float] | None = None,
                       max_delta_mm: float = 1.0) -> OffsetProposal:
    target = OffsetTarget(OffsetTargetKind.WCS_REGISTER, tuple(proposed_mm), register=register)
    return propose_offset(observations=observations, machine_id=machine_id, job_id=job_id,
                          setup_id=setup_id, setup_revision=setup_revision, target=target,
                          current_mm=current_mm, proposed_mm=proposed_mm, locality=locality,
                          intended_use=intended_use, use_location_machine=use_location_machine,
                          max_delta_mm=max_delta_mm)


def propose_tool_offset(*, observations: Sequence[QualifiedObservation], machine_id: str,
                        job_id: str, setup_id: str, setup_revision: int, tool_number: int,
                        current_length_mm: float, proposed_length_mm: float,
                        locality: LocalityBlock, max_delta_mm: float = 0.5) -> OffsetProposal:
    target = OffsetTarget(OffsetTargetKind.TOOL_LENGTH_DYNAMIC, ("Z",),
                          tool_number=tool_number)
    return propose_offset(observations=observations, machine_id=machine_id, job_id=job_id,
                          setup_id=setup_id, setup_revision=setup_revision, target=target,
                          current_mm={"Z": current_length_mm},
                          proposed_mm={"Z": proposed_length_mm}, locality=locality,
                          max_delta_mm=max_delta_mm)
