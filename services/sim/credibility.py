"""Fail-closed simulation credibility evidence registry.

The S0--S3 tier is derived from immutable, revision-bound evidence.  Callers do
not set a tier and a result cannot become S2/S3 merely by carrying a label.
This module records internal V&V evidence only; it never represents an external
certification or accreditation.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
_HASH_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_EVIDENCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FORBIDDEN_CLAIM_RE = re.compile(r"\b(?:certif\w*|accredit\w*)\b", re.IGNORECASE)


class CredibilityError(ValueError):
    """Base error for invalid evidence or unsafe credibility claims."""


class RegistryUnavailableError(CredibilityError):
    """Raised when a corrupt registry cannot safely be modified."""


class ClaimLanguageError(CredibilityError):
    """Raised when internal V&V is presented as an external approval."""


class CredibilityTier(str, Enum):
    S0_UNVALIDATED = "S0_unvalidated"
    S1_BENCHMARKED = "S1_benchmarked"
    S2_CROSS_SOLVER = "S2_cross_solver"
    S3_FIELD_CORRELATED = "S3_field_correlated"


class EvidenceKind(str, Enum):
    BENCHMARK = "benchmark"
    CROSS_SOLVER = "cross_solver"
    FIELD_CORRELATION = "field_correlation"


_TIER_LABELS = {
    CredibilityTier.S0_UNVALIDATED: "unvalidated",
    CredibilityTier.S1_BENCHMARKED: "internally benchmarked",
    CredibilityTier.S2_CROSS_SOLVER: "internally cross-solver compared",
    CredibilityTier.S3_FIELD_CORRELATED: "internally field-correlated",
}
_ALLOWED_LABELS = frozenset(_TIER_LABELS.values())


def _parse_time(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CredibilityError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CredibilityError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CredibilityError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_hash(value: str | None, field: str, *, required: bool) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise CredibilityError(f"{field} must be a 40- or 64-character hexadecimal revision hash")


def guard_claim_label(label: str) -> str:
    """Return an allowed internal label, rejecting external-approval language.

    The allow-list is intentional: sanitising an unsafe label would make a claim
    look accepted.  Unknown labels fail rather than being echoed to a UI/report.
    """
    if not isinstance(label, str):
        raise ClaimLanguageError("credibility label must be a string")
    normalized = " ".join(label.strip().split())
    if _FORBIDDEN_CLAIM_RE.search(normalized):
        raise ClaimLanguageError("internal V&V cannot claim certification or accreditation")
    if normalized not in _ALLOWED_LABELS:
        raise ClaimLanguageError(f"unsupported internal credibility label: {normalized!r}")
    return normalized


def claim_label(tier: CredibilityTier | str) -> str:
    try:
        parsed = tier if isinstance(tier, CredibilityTier) else CredibilityTier(tier)
    except (TypeError, ValueError) as exc:
        raise ClaimLanguageError(f"unknown credibility tier: {tier!r}") from exc
    return guard_claim_label(_TIER_LABELS[parsed])


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    family: str
    kind: str
    solver_source: str
    solver_revision_hash: str
    model_revision_hash: str | None
    created_at: str
    expires_at: str
    outcome: str
    depends_on: tuple[str, ...] = ()
    reference_source: str | None = None
    reference_revision_hash: str | None = None
    dataset_id: str | None = None
    dataset_revision_hash: str | None = None
    invalidated_at: str | None = None
    invalidation_reason: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceRecord":
        if not isinstance(value, dict):
            raise CredibilityError("evidence entry must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value) - allowed
        if unknown:
            raise CredibilityError(f"unknown evidence fields: {sorted(unknown)}")
        data = dict(value)
        deps = data.get("depends_on", ())
        if not isinstance(deps, (list, tuple)) or not all(isinstance(v, str) for v in deps):
            raise CredibilityError("depends_on must be a list of evidence ids")
        data["depends_on"] = tuple(deps)
        try:
            record = cls(**data)
        except TypeError as exc:
            raise CredibilityError(f"incomplete evidence entry: {exc}") from exc
        record.validate()
        return record

    def validate(self) -> None:
        if not isinstance(self.evidence_id, str) or not _EVIDENCE_ID_RE.fullmatch(self.evidence_id):
            raise CredibilityError("evidence_id has an invalid format")
        if not isinstance(self.family, str) or not self.family.strip():
            raise CredibilityError("family must be non-empty")
        if not isinstance(self.solver_source, str) or not self.solver_source.strip():
            raise CredibilityError("solver_source must be non-empty")
        try:
            kind = EvidenceKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise CredibilityError(f"unsupported evidence kind: {self.kind!r}") from exc
        if self.outcome not in ("pass", "fail"):
            raise CredibilityError("outcome must be 'pass' or 'fail'")
        _validate_hash(self.solver_revision_hash, "solver_revision_hash", required=True)
        _validate_hash(self.model_revision_hash, "model_revision_hash", required=False)
        created = _parse_time(self.created_at, "created_at")
        expires = _parse_time(self.expires_at, "expires_at")
        if expires <= created:
            raise CredibilityError("expires_at must be later than created_at")
        if (not isinstance(self.depends_on, (tuple, list))
                or not all(isinstance(value, str) for value in self.depends_on)):
            raise CredibilityError("depends_on must contain evidence ids")
        if len(set(self.depends_on)) != len(self.depends_on) or self.evidence_id in self.depends_on:
            raise CredibilityError("depends_on must contain unique ids and cannot contain itself")
        for dependency in self.depends_on:
            if not _EVIDENCE_ID_RE.fullmatch(dependency):
                raise CredibilityError(f"invalid dependency evidence id: {dependency!r}")
        if self.invalidated_at is not None:
            invalidated = _parse_time(self.invalidated_at, "invalidated_at")
            if invalidated < created:
                raise CredibilityError("invalidated_at cannot precede created_at")
            if not isinstance(self.invalidation_reason, str) or not self.invalidation_reason.strip():
                raise CredibilityError("invalidated evidence requires invalidation_reason")
        elif self.invalidation_reason is not None:
            raise CredibilityError("invalidation_reason requires invalidated_at")

        if kind is EvidenceKind.BENCHMARK:
            if self.reference_source or self.reference_revision_hash or self.dataset_id or self.dataset_revision_hash:
                raise CredibilityError("benchmark evidence cannot carry cross-solver or field fields")
        elif kind is EvidenceKind.CROSS_SOLVER:
            if not self.depends_on:
                raise CredibilityError("cross_solver evidence must depend on benchmark evidence")
            if not isinstance(self.reference_source, str) or not self.reference_source.strip():
                raise CredibilityError("cross_solver evidence requires reference_source")
            _validate_hash(self.reference_revision_hash, "reference_revision_hash", required=True)
            if (self.reference_source.strip() == self.solver_source.strip()
                    and self.reference_revision_hash == self.solver_revision_hash):
                raise CredibilityError("cross_solver reference must be independent of the target solver")
            if self.dataset_id or self.dataset_revision_hash:
                raise CredibilityError("cross_solver evidence cannot carry field dataset fields")
        else:
            if not self.depends_on:
                raise CredibilityError("field_correlation evidence must depend on cross-solver evidence")
            if not isinstance(self.dataset_id, str) or not self.dataset_id.strip():
                raise CredibilityError("field_correlation evidence requires dataset_id")
            _validate_hash(self.dataset_revision_hash, "dataset_revision_hash", required=True)
            if self.reference_source or self.reference_revision_hash:
                raise CredibilityError("field_correlation evidence cannot carry cross-solver fields")

    def is_current(self, now: datetime) -> bool:
        return (self.outcome == "pass" and self.invalidated_at is None
                and _parse_time(self.created_at, "created_at") <= now
                and now < _parse_time(self.expires_at, "expires_at"))


@dataclass(frozen=True)
class CredibilityAssessment:
    family: str
    tier: CredibilityTier
    evidence_ids: tuple[str, ...]
    solver_revision_hash: str
    model_revision_hash: str | None
    reason: str

    @property
    def label(self) -> str:
        return claim_label(self.tier)

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "tier": self.tier.value,
            "label": self.label,
            "scope": "internal_vv_only",
            "evidence_ids": list(self.evidence_ids),
            "solver_revision_hash": self.solver_revision_hash,
            "model_revision_hash": self.model_revision_hash,
            "reason": self.reason,
        }


class CredibilityRegistry:
    """JSON-backed evidence registry with exact-revision, fail-closed derivation."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._records: dict[str, EvidenceRecord] = {}
        self._load_error: str | None = None
        self._load()

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            root = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(root, dict) or set(root) != {"schema_version", "evidence"}:
                raise CredibilityError("registry root must contain only schema_version and evidence")
            if root["schema_version"] != SCHEMA_VERSION or not isinstance(root["evidence"], list):
                raise CredibilityError("unsupported registry schema")
            records: dict[str, EvidenceRecord] = {}
            for value in root["evidence"]:
                record = EvidenceRecord.from_dict(value)
                if record.evidence_id in records:
                    raise CredibilityError(f"duplicate evidence id: {record.evidence_id}")
                records[record.evidence_id] = record
            self._records = records
        except (OSError, json.JSONDecodeError, CredibilityError) as exc:
            self._records = {}
            self._load_error = str(exc)

    def _require_writable(self) -> None:
        if self._load_error is not None:
            raise RegistryUnavailableError(
                f"refusing to overwrite unreadable credibility registry: {self._load_error}")

    def _write(self) -> None:
        self._require_writable()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "evidence": [asdict(self._records[key]) for key in sorted(self._records)],
        }
        temporary = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    def register(self, record: EvidenceRecord) -> None:
        self._require_writable()
        record.validate()
        if record.evidence_id in self._records:
            raise CredibilityError(f"evidence id already registered: {record.evidence_id}")
        missing = set(record.depends_on) - set(self._records)
        if missing:
            raise CredibilityError(f"unregistered dependency evidence ids: {sorted(missing)}")
        self._records[record.evidence_id] = record
        try:
            self._write()
        except Exception:
            self._records.pop(record.evidence_id, None)
            raise

    def invalidate(self, evidence_id: str, reason: str, *, at: str | None = None) -> None:
        self._require_writable()
        if evidence_id not in self._records:
            raise CredibilityError(f"unknown evidence id: {evidence_id}")
        if not isinstance(reason, str) or not reason.strip():
            raise CredibilityError("invalidation reason must be non-empty")
        record = self._records[evidence_id]
        if record.invalidated_at is not None:
            raise CredibilityError(f"evidence already invalidated: {evidence_id}")
        timestamp = at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        updated = replace(record, invalidated_at=timestamp, invalidation_reason=reason.strip())
        updated.validate()
        self._records[evidence_id] = updated
        try:
            self._write()
        except Exception:
            self._records[evidence_id] = record
            raise

    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def derive(self, family: str, solver_revision_hash: str,
               model_revision_hash: str | None = None,
               *, now: datetime | None = None) -> CredibilityAssessment:
        try:
            _validate_hash(solver_revision_hash, "solver_revision_hash", required=True)
            _validate_hash(model_revision_hash, "model_revision_hash", required=False)
        except CredibilityError as exc:
            return CredibilityAssessment(
                family, CredibilityTier.S0_UNVALIDATED, (), solver_revision_hash,
                model_revision_hash, f"invalid revision identity: {exc}")
        if self._load_error is not None:
            return CredibilityAssessment(
                family, CredibilityTier.S0_UNVALIDATED, (), solver_revision_hash,
                model_revision_hash, f"registry unavailable: {self._load_error}")
        instant = now or datetime.now(timezone.utc)
        if (not isinstance(instant, datetime)
                or instant.tzinfo is None or instant.utcoffset() is None):
            return CredibilityAssessment(
                family, CredibilityTier.S0_UNVALIDATED, (), solver_revision_hash,
                model_revision_hash, "assessment time has no timezone")
        instant = instant.astimezone(timezone.utc)
        matching = {
            record.evidence_id: record
            for record in self._records.values()
            if (record.family == family
                and record.solver_revision_hash == solver_revision_hash
                and record.model_revision_hash == model_revision_hash
                and record.is_current(instant))
        }

        benchmarks = sorted(
            evidence_id for evidence_id, record in matching.items()
            if record.kind == EvidenceKind.BENCHMARK.value)
        if not benchmarks:
            return CredibilityAssessment(
                family, CredibilityTier.S0_UNVALIDATED, (), solver_revision_hash,
                model_revision_hash, "no current passing benchmark evidence for this exact revision")

        benchmark_ids = set(benchmarks)
        cross_solver = sorted(
            evidence_id for evidence_id, record in matching.items()
            if (record.kind == EvidenceKind.CROSS_SOLVER.value
                and bool(benchmark_ids.intersection(record.depends_on)))
        )
        if not cross_solver:
            return CredibilityAssessment(
                family, CredibilityTier.S1_BENCHMARKED, tuple(benchmarks), solver_revision_hash,
                model_revision_hash, "current registered benchmark evidence")

        cross_ids = set(cross_solver)
        field = sorted(
            evidence_id for evidence_id, record in matching.items()
            if (record.kind == EvidenceKind.FIELD_CORRELATION.value
                and bool(cross_ids.intersection(record.depends_on)))
        )
        if not field:
            return CredibilityAssessment(
                family, CredibilityTier.S2_CROSS_SOLVER,
                tuple(benchmarks + cross_solver), solver_revision_hash,
                model_revision_hash, "current benchmark and independent cross-solver evidence")
        return CredibilityAssessment(
            family, CredibilityTier.S3_FIELD_CORRELATED,
            tuple(benchmarks + cross_solver + field), solver_revision_hash,
            model_revision_hash,
            "current benchmark, independent cross-solver, and field-correlation evidence")


def derive_credibility(registry_path: str | os.PathLike[str], family: str,
                       solver_revision_hash: str, model_revision_hash: str | None = None,
                       *, now: datetime | None = None) -> CredibilityAssessment:
    """One-shot helper for callers that do not need to retain a registry object."""
    return CredibilityRegistry(registry_path).derive(
        family, solver_revision_hash, model_revision_hash, now=now)
