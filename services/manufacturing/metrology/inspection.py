"""Immutable inspection plans and guardbanded qualified conformance results."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
import math
from typing import Iterable, Sequence

from .evidence import AdvisoryObservation, QualifiedObservation, QualifiedTier
from .limits import MAX_METROLOGY_ITEMS, MetrologyResourceRefusal, bounded_tuple


class InspectionMethod(str, Enum):
    PROBE = "probe"
    CAMERA = "camera"
    MANUAL = "manual"


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"
    NOT_MEASURED = "not_measured"


class DecisionMode(str, Enum):
    GUARDBAND_ISO14253 = "guardband_iso14253"
    SIMPLE_ACCEPTANCE = "simple_acceptance"
    RELAXED = "relaxed"


@dataclass(frozen=True, slots=True)
class Tolerance:
    lower_mm: float
    upper_mm: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.lower_mm) or not math.isfinite(self.upper_mm):
            raise ValueError("tolerance limits must be finite")
        if self.lower_mm >= self.upper_mm:
            raise ValueError("lower tolerance must be below upper tolerance")


@dataclass(frozen=True, slots=True)
class DecisionRule:
    mode: DecisionMode = DecisionMode.GUARDBAND_ISO14253
    k: float = 2.0
    acknowledgement: str | None = None
    relaxed_guardband_mm: float = 0.0

    def __post_init__(self) -> None:
        if self.k <= 0:
            raise ValueError("coverage factor must be positive")
        if self.mode is not DecisionMode.GUARDBAND_ISO14253 and not self.acknowledgement:
            raise ValueError("non-default decision rule requires a risk acknowledgement")
        if self.relaxed_guardband_mm < 0:
            raise ValueError("relaxed guardband must be non-negative")


@dataclass(frozen=True, slots=True)
class Characteristic:
    id: str
    feature: str
    kind: str
    nominal_mm: float
    tolerance: Tolerance
    method: InspectionMethod
    required_tier: QualifiedTier = QualifiedTier.Q2
    decision_rule: DecisionRule = DecisionRule()


@dataclass(frozen=True, slots=True)
class InspectionPlan:
    id: str
    revision: int
    part_ref: str
    setup_id: str
    characteristics: tuple[Characteristic, ...]

    def __post_init__(self) -> None:
        if self.revision < 0 or not self.characteristics:
            raise ValueError("inspection plan needs a revision and characteristics")
        ids = [item.id for item in self.characteristics]
        if len(ids) != len(set(ids)):
            raise ValueError("characteristic ids must be unique")


@dataclass(frozen=True, slots=True)
class CharacteristicResult:
    characteristic_id: str
    observation_id: str
    measured_mm: float
    deviation_mm: float
    uncertainty_mm: float
    verdict: Verdict
    uncertainty_basis: str
    why: str
    derived_from: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InspectionResult:
    id: str
    plan_id: str
    plan_revision: int
    characteristics: tuple[CharacteristicResult, ...]
    verdict: Verdict
    evidence_bundle_hash: str
    derived_from: tuple[str, ...]


def _tier_at_least(actual: QualifiedTier, required: QualifiedTier) -> bool:
    order = {QualifiedTier.Q0: 0, QualifiedTier.Q1: 1,
             QualifiedTier.Q2: 2, QualifiedTier.Q3: 3}
    return order[actual] >= order[required]


def guardbanded_verdict(*, measured_mm: float, nominal_mm: float,
                        tolerance: Tolerance, expanded_uncertainty_mm: float) -> Verdict:
    """ISO 14253 interval decision with inclusive pass and strict fail boundaries."""
    if not all(math.isfinite(value) for value in
               (measured_mm, nominal_mm, expanded_uncertainty_mm)):
        raise ValueError("guardband inputs must be finite")
    if expanded_uncertainty_mm < 0:
        raise ValueError("expanded uncertainty must be non-negative")
    lower_limit = nominal_mm + tolerance.lower_mm
    upper_limit = nominal_mm + tolerance.upper_mm
    interval_lower = measured_mm - expanded_uncertainty_mm
    interval_upper = measured_mm + expanded_uncertainty_mm
    if interval_lower >= lower_limit and interval_upper <= upper_limit:
        return Verdict.PASS
    if interval_upper < lower_limit or interval_lower > upper_limit:
        return Verdict.FAIL
    return Verdict.INDETERMINATE


def evaluate_conformance(characteristic: Characteristic,
                         observation: QualifiedObservation) -> CharacteristicResult:
    """Evaluate physical conformance; nominal typing excludes advisory observations."""
    if type(observation) is not QualifiedObservation:
        raise TypeError("conformance evaluation accepts QualifiedObservation only")
    if characteristic.method is not InspectionMethod.PROBE:
        raise ValueError("qualified conformance requires a probe characteristic")
    if not _tier_at_least(observation.tier, characteristic.required_tier):
        raise ValueError("observation tier is below the characteristic requirement")
    try:
        measured = float(observation.value)
    except (TypeError, ValueError) as exc:
        raise ValueError("scalar characteristic requires a scalar observation") from exc
    uncertainty = observation.uncertainty.expanded_mm
    rule = characteristic.decision_rule
    if rule.mode is DecisionMode.GUARDBAND_ISO14253:
        verdict = guardbanded_verdict(measured_mm=measured,
                                      nominal_mm=characteristic.nominal_mm,
                                      tolerance=characteristic.tolerance,
                                      expanded_uncertainty_mm=uncertainty)
    elif rule.mode is DecisionMode.SIMPLE_ACCEPTANCE:
        deviation = measured - characteristic.nominal_mm
        verdict = (Verdict.PASS if characteristic.tolerance.lower_mm <= deviation
                   <= characteristic.tolerance.upper_mm else Verdict.FAIL)
    else:
        widened = Tolerance(characteristic.tolerance.lower_mm - rule.relaxed_guardband_mm,
                            characteristic.tolerance.upper_mm + rule.relaxed_guardband_mm)
        verdict = guardbanded_verdict(measured_mm=measured,
                                      nominal_mm=characteristic.nominal_mm,
                                      tolerance=widened,
                                      expanded_uncertainty_mm=uncertainty)
    deviation = measured - characteristic.nominal_mm
    why = ("measurement interval lies inside tolerance" if verdict is Verdict.PASS else
           "measurement interval lies outside tolerance" if verdict is Verdict.FAIL else
           "measurement uncertainty overlaps a tolerance boundary")
    roots = tuple(dict.fromkeys((observation.id, *observation.derived_from)))
    return CharacteristicResult(characteristic.id, observation.id, measured, deviation,
                                uncertainty, verdict, observation.uncertainty.basis.value,
                                why, roots)


def screen_advisory(characteristic: Characteristic,
                    observation: AdvisoryObservation) -> CharacteristicResult:
    """Record camera screening without accepting or rejecting physical conformance."""
    if type(observation) is not AdvisoryObservation:
        raise TypeError("advisory screening accepts AdvisoryObservation only")
    measured = float(observation.value)
    uncertainty = observation.uncertainty_mm or math.inf
    roots = tuple(dict.fromkeys((observation.id, *observation.derived_from)))
    return CharacteristicResult(characteristic.id, observation.id, measured,
                                measured - characteristic.nominal_mm, uncertainty,
                                Verdict.INDETERMINATE, "advisory",
                                "camera evidence is advisory and cannot accept a part", roots)


def overall_verdict(results: Iterable[CharacteristicResult]) -> Verdict:
    values = bounded_tuple(results, label="inspection_results")
    if not values:
        raise ValueError("inspection result needs characteristic results")
    if any(item.verdict is Verdict.FAIL for item in values):
        return Verdict.FAIL
    if any(item.verdict in (Verdict.INDETERMINATE, Verdict.NOT_MEASURED) for item in values):
        return Verdict.INDETERMINATE
    return Verdict.PASS


def build_inspection_result(plan: InspectionPlan,
                            results: Sequence[CharacteristicResult]) -> InspectionResult:
    if len(results) > MAX_METROLOGY_ITEMS or len(plan.characteristics) > MAX_METROLOGY_ITEMS:
        raise MetrologyResourceRefusal(
            "inspection_result_limit",
            f"inspection exceeds the {MAX_METROLOGY_ITEMS} result limit",
        )
    if {item.id for item in plan.characteristics} != {item.characteristic_id for item in results}:
        raise ValueError("inspection results must exactly cover the plan")
    ordered = tuple(sorted(results, key=lambda item: item.characteristic_id))
    roots = tuple(dict.fromkeys(root for item in ordered for root in item.derived_from))
    bundle_payload = [{"characteristic": item.characteristic_id,
                       "observation": item.observation_id,
                       "verdict": item.verdict.value} for item in ordered]
    bundle_text = json.dumps(bundle_payload, sort_keys=True, separators=(",", ":"))
    bundle_hash = "sha256:" + sha256(bundle_text.encode("utf-8")).hexdigest()
    result_payload = {"plan": plan.id, "revision": plan.revision, "bundle": bundle_hash}
    result_id = "sha256:" + sha256(json.dumps(result_payload, sort_keys=True).encode("utf-8")).hexdigest()
    return InspectionResult(result_id, plan.id, plan.revision, ordered,
                            overall_verdict(ordered), bundle_hash, roots)
