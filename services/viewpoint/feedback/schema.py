"""Strict, dependency-free schemas for Stage-2 viewpoint feedback."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import re
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "1.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LICENSE_KINDS = frozenset({"owned", "licensed", "synthetic", "third_party"})


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _exact(data: Mapping[str, Any], expected: set[str], name: str) -> None:
    missing = expected - set(data)
    unknown = set(data) - expected
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unknown " + ", ".join(sorted(unknown)))
        raise ValueError(f"invalid {name}: {'; '.join(details)}")


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _sha256(value: Any, name: str) -> str:
    value = _nonempty(value, name)
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _triple(value: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 3:
        raise ValueError(f"{name} must have exactly three coordinates")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} coordinates must be finite")
    return result  # type: ignore[return-value]


def _score(value: Any, name: str, *, allow_zero: bool = True) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= 4:
        raise ValueError(f"{name} must be an integer in [{minimum}, 4]")
    return value


def _optional_bool(value: Any, name: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ValueError(f"{name} must be true, false, or null")
    return value


@dataclass(frozen=True)
class CameraPose:
    eye: tuple[float, float, float]
    target: tuple[float, float, float]
    up: tuple[float, float, float]
    fov_y_degrees: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "eye", _triple(self.eye, "eye"))
        object.__setattr__(self, "target", _triple(self.target, "target"))
        object.__setattr__(self, "up", _triple(self.up, "up"))
        fov = float(self.fov_y_degrees)
        if not math.isfinite(fov) or not 1.0 <= fov < 180.0:
            raise ValueError("fov_y_degrees must be finite and in [1, 180)")
        if self.eye == self.target:
            raise ValueError("camera eye and target must differ")
        if self.up == (0.0, 0.0, 0.0):
            raise ValueError("camera up vector must be non-zero")
        object.__setattr__(self, "fov_y_degrees", fov)

    def to_dict(self) -> dict[str, Any]:
        return {"eye": list(self.eye), "target": list(self.target), "up": list(self.up),
                "fov_y_degrees": self.fov_y_degrees}

    @classmethod
    def from_dict(cls, value: Any) -> "CameraPose":
        data = _mapping(value, "CameraPose")
        _exact(data, {"eye", "target", "up", "fov_y_degrees"}, "CameraPose")
        return cls(_triple(data["eye"], "eye"), _triple(data["target"], "target"),
                   _triple(data["up"], "up"), data["fov_y_degrees"])


@dataclass(frozen=True)
class CandidatePose:
    candidate_id: str
    pose: CameraPose

    def __post_init__(self) -> None:
        _nonempty(self.candidate_id, "candidate_id")
        if not isinstance(self.pose, CameraPose):
            raise ValueError("pose must be a CameraPose")

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "pose": self.pose.to_dict()}

    @classmethod
    def from_dict(cls, value: Any) -> "CandidatePose":
        data = _mapping(value, "CandidatePose")
        _exact(data, {"candidate_id", "pose"}, "CandidatePose")
        return cls(_nonempty(data["candidate_id"], "candidate_id"), CameraPose.from_dict(data["pose"]))


@dataclass(frozen=True)
class CandidateSetRequest:
    request_id: str
    geometry_hash: str
    feature: str
    task_text: str
    candidate_poses: tuple[CandidatePose, ...]
    randomization_seed: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        _nonempty(self.request_id, "request_id")
        _sha256(self.geometry_hash, "geometry_hash")
        _nonempty(self.feature, "feature")
        _nonempty(self.task_text, "task_text")
        object.__setattr__(self, "candidate_poses", tuple(self.candidate_poses))
        if len(self.candidate_poses) < 2:
            raise ValueError("candidate set must contain at least two poses")
        ids = [candidate.candidate_id for candidate in self.candidate_poses]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate_id values must be unique")
        if type(self.randomization_seed) is not int or not 0 <= self.randomization_seed < 2**63:
            raise ValueError("randomization_seed must be an integer in [0, 2^63)")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "request_id": self.request_id,
                "geometry_hash": self.geometry_hash, "feature": self.feature,
                "task_text": self.task_text,
                "candidate_poses": [item.to_dict() for item in self.candidate_poses],
                "randomization_seed": self.randomization_seed}

    @classmethod
    def from_dict(cls, value: Any) -> "CandidateSetRequest":
        data = _mapping(value, "CandidateSetRequest")
        fields = {"schema_version", "request_id", "geometry_hash", "feature", "task_text",
                  "candidate_poses", "randomization_seed"}
        _exact(data, fields, "CandidateSetRequest")
        poses = data["candidate_poses"]
        if not isinstance(poses, list):
            raise ValueError("candidate_poses must be an array")
        return cls(request_id=_nonempty(data["request_id"], "request_id"),
                   geometry_hash=_sha256(data["geometry_hash"], "geometry_hash"),
                   feature=_nonempty(data["feature"], "feature"),
                   task_text=_nonempty(data["task_text"], "task_text"),
                   candidate_poses=tuple(CandidatePose.from_dict(item) for item in poses),
                   randomization_seed=data["randomization_seed"], schema_version=data["schema_version"])

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "CandidateSetRequest":
        return cls.from_dict(json.loads(value))


@dataclass(frozen=True)
class RenderCandidate:
    candidate_id: str
    pose: CameraPose
    render_uri: str
    render_sha256: str

    def __post_init__(self) -> None:
        _nonempty(self.candidate_id, "candidate_id")
        if not isinstance(self.pose, CameraPose):
            raise ValueError("pose must be a CameraPose")
        _nonempty(self.render_uri, "render_uri")
        _sha256(self.render_sha256, "render_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "pose": self.pose.to_dict(),
                "render_uri": self.render_uri, "render_sha256": self.render_sha256}

    @classmethod
    def from_dict(cls, value: Any) -> "RenderCandidate":
        data = _mapping(value, "RenderCandidate")
        _exact(data, {"candidate_id", "pose", "render_uri", "render_sha256"}, "RenderCandidate")
        return cls(_nonempty(data["candidate_id"], "candidate_id"), CameraPose.from_dict(data["pose"]),
                   _nonempty(data["render_uri"], "render_uri"),
                   _sha256(data["render_sha256"], "render_sha256"))


@dataclass(frozen=True)
class CandidateSetResponse:
    request_id: str
    geometry_hash: str
    feature: str
    task_text: str
    canonical_candidates: tuple[RenderCandidate, ...]
    permutation: tuple[int, ...]
    randomization_seed: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        _nonempty(self.request_id, "request_id")
        _sha256(self.geometry_hash, "geometry_hash")
        _nonempty(self.feature, "feature")
        _nonempty(self.task_text, "task_text")
        object.__setattr__(self, "canonical_candidates", tuple(self.canonical_candidates))
        object.__setattr__(self, "permutation", tuple(self.permutation))
        count = len(self.canonical_candidates)
        if count < 2:
            raise ValueError("candidate set must contain at least two renders")
        ids = [item.candidate_id for item in self.canonical_candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate_id values must be unique")
        if self.permutation != tuple(self.permutation):
            raise ValueError("permutation must be a tuple")
        if sorted(self.permutation) != list(range(count)):
            raise ValueError("permutation must contain every canonical candidate index exactly once")
        if type(self.randomization_seed) is not int or not 0 <= self.randomization_seed < 2**63:
            raise ValueError("randomization_seed must be an integer in [0, 2^63)")

    @property
    def ordered_candidates(self) -> tuple[RenderCandidate, ...]:
        return tuple(self.canonical_candidates[index] for index in self.permutation)

    def candidate(self, candidate_id: str) -> RenderCandidate:
        for candidate in self.canonical_candidates:
            if candidate.candidate_id == candidate_id:
                return candidate
        raise KeyError(candidate_id)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "request_id": self.request_id,
                "geometry_hash": self.geometry_hash, "feature": self.feature,
                "task_text": self.task_text,
                "canonical_candidates": [item.to_dict() for item in self.canonical_candidates],
                "permutation": list(self.permutation), "randomization_seed": self.randomization_seed}

    @classmethod
    def from_dict(cls, value: Any) -> "CandidateSetResponse":
        data = _mapping(value, "CandidateSetResponse")
        fields = {"schema_version", "request_id", "geometry_hash", "feature", "task_text",
                  "canonical_candidates", "permutation", "randomization_seed"}
        _exact(data, fields, "CandidateSetResponse")
        candidates = data["canonical_candidates"]
        permutation = data["permutation"]
        if not isinstance(candidates, list) or not isinstance(permutation, list):
            raise ValueError("canonical_candidates and permutation must be arrays")
        if any(type(index) is not int for index in permutation):
            raise ValueError("permutation values must be integers")
        return cls(_nonempty(data["request_id"], "request_id"),
                   _sha256(data["geometry_hash"], "geometry_hash"),
                   _nonempty(data["feature"], "feature"), _nonempty(data["task_text"], "task_text"),
                   tuple(RenderCandidate.from_dict(item) for item in candidates), tuple(permutation),
                   data["randomization_seed"], data["schema_version"])

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "CandidateSetResponse":
        return cls.from_dict(json.loads(value))


@dataclass(frozen=True)
class RubricScore:
    candidate_id: str
    feature_visible: int
    unoccluded: int
    unforeshortened: int
    adequate_scale: int
    task_usefulness: int

    def __post_init__(self) -> None:
        _nonempty(self.candidate_id, "candidate_id")
        for name in ("feature_visible", "unoccluded", "unforeshortened", "adequate_scale",
                     "task_usefulness"):
            _score(getattr(self, name), name)

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "feature_visible": self.feature_visible,
                "unoccluded": self.unoccluded, "unforeshortened": self.unforeshortened,
                "adequate_scale": self.adequate_scale, "task_usefulness": self.task_usefulness}

    @classmethod
    def from_dict(cls, value: Any) -> "RubricScore":
        data = _mapping(value, "RubricScore")
        fields = {"candidate_id", "feature_visible", "unoccluded", "unforeshortened",
                  "adequate_scale", "task_usefulness"}
        _exact(data, fields, "RubricScore")
        return cls(_nonempty(data["candidate_id"], "candidate_id"),
                   *(_score(data[name], name) for name in ("feature_visible", "unoccluded",
                                                           "unforeshortened", "adequate_scale",
                                                           "task_usefulness")))


@dataclass(frozen=True)
class PairwiseScore:
    candidate_a_id: str
    candidate_b_id: str
    preferred_id: str | None
    confidence: int
    abstain_reason: str | None

    def __post_init__(self) -> None:
        _nonempty(self.candidate_a_id, "candidate_a_id")
        _nonempty(self.candidate_b_id, "candidate_b_id")
        if self.candidate_a_id == self.candidate_b_id:
            raise ValueError("pairwise candidates must differ")
        if self.preferred_id is None:
            if not isinstance(self.abstain_reason, str) or not self.abstain_reason.strip():
                raise ValueError("an abstained comparison requires abstain_reason")
            _score(self.confidence, "confidence")
            if self.confidence != 0:
                raise ValueError("an abstained comparison must have zero confidence")
        else:
            if self.preferred_id not in {self.candidate_a_id, self.candidate_b_id}:
                raise ValueError("preferred_id must name one of the compared candidates")
            _score(self.confidence, "confidence", allow_zero=False)
            if self.abstain_reason is not None:
                raise ValueError("a decisive comparison cannot have abstain_reason")

    @property
    def abstained(self) -> bool:
        return self.preferred_id is None

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_a_id": self.candidate_a_id, "candidate_b_id": self.candidate_b_id,
                "preferred_id": self.preferred_id, "confidence": self.confidence,
                "abstain_reason": self.abstain_reason}

    @classmethod
    def from_dict(cls, value: Any) -> "PairwiseScore":
        data = _mapping(value, "PairwiseScore")
        fields = {"candidate_a_id", "candidate_b_id", "preferred_id", "confidence", "abstain_reason"}
        _exact(data, fields, "PairwiseScore")
        preferred = data["preferred_id"]
        abstain = data["abstain_reason"]
        if preferred is not None:
            preferred = _nonempty(preferred, "preferred_id")
        if abstain is not None and not isinstance(abstain, str):
            raise ValueError("abstain_reason must be a string or null")
        return cls(_nonempty(data["candidate_a_id"], "candidate_a_id"),
                   _nonempty(data["candidate_b_id"], "candidate_b_id"), preferred,
                   data["confidence"], abstain)


@dataclass(frozen=True)
class ScoringResponse:
    request_id: str
    rubric_scores: tuple[RubricScore, ...]
    pairwise_scores: tuple[PairwiseScore, ...]
    overall_abstain_reason: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        _nonempty(self.request_id, "request_id")
        object.__setattr__(self, "rubric_scores", tuple(self.rubric_scores))
        object.__setattr__(self, "pairwise_scores", tuple(self.pairwise_scores))
        ids = [score.candidate_id for score in self.rubric_scores]
        if len(ids) != len(set(ids)):
            raise ValueError("rubric candidate_id values must be unique")
        if self.overall_abstain_reason is not None:
            _nonempty(self.overall_abstain_reason, "overall_abstain_reason")
            if self.pairwise_scores:
                raise ValueError("an overall abstention cannot contain pairwise scores")
        elif not self.pairwise_scores:
            raise ValueError("a non-abstained response requires pairwise scores")

    @property
    def abstained(self) -> bool:
        return self.overall_abstain_reason is not None

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "request_id": self.request_id,
                "rubric_scores": [item.to_dict() for item in self.rubric_scores],
                "pairwise_scores": [item.to_dict() for item in self.pairwise_scores],
                "overall_abstain_reason": self.overall_abstain_reason}

    @classmethod
    def from_dict(cls, value: Any) -> "ScoringResponse":
        data = _mapping(value, "ScoringResponse")
        fields = {"schema_version", "request_id", "rubric_scores", "pairwise_scores",
                  "overall_abstain_reason"}
        _exact(data, fields, "ScoringResponse")
        rubric, pairwise = data["rubric_scores"], data["pairwise_scores"]
        if not isinstance(rubric, list) or not isinstance(pairwise, list):
            raise ValueError("rubric_scores and pairwise_scores must be arrays")
        reason = data["overall_abstain_reason"]
        if reason is not None and not isinstance(reason, str):
            raise ValueError("overall_abstain_reason must be a string or null")
        return cls(_nonempty(data["request_id"], "request_id"),
                   tuple(RubricScore.from_dict(item) for item in rubric),
                   tuple(PairwiseScore.from_dict(item) for item in pairwise), reason,
                   data["schema_version"])


@dataclass(frozen=True)
class PublicationConsent:
    license_kind: str
    license_ref: str
    consent_ref: str
    model_training: bool
    public_redistribution: bool
    redaction_applied: bool
    export_restricted: bool

    def __post_init__(self) -> None:
        if self.license_kind not in _LICENSE_KINDS:
            raise ValueError(f"license_kind must be one of {sorted(_LICENSE_KINDS)}")
        _nonempty(self.license_ref, "license_ref")
        _nonempty(self.consent_ref, "consent_ref")
        for name in ("model_training", "public_redistribution", "redaction_applied",
                     "export_restricted"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {"license_kind": self.license_kind, "license_ref": self.license_ref,
                "consent_ref": self.consent_ref, "model_training": self.model_training,
                "public_redistribution": self.public_redistribution,
                "redaction_applied": self.redaction_applied,
                "export_restricted": self.export_restricted}

    @classmethod
    def from_dict(cls, value: Any) -> "PublicationConsent":
        data = _mapping(value, "PublicationConsent")
        fields = {"license_kind", "license_ref", "consent_ref", "model_training",
                  "public_redistribution", "redaction_applied", "export_restricted"}
        _exact(data, fields, "PublicationConsent")
        return cls(data["license_kind"], _nonempty(data["license_ref"], "license_ref"),
                   _nonempty(data["consent_ref"], "consent_ref"), data["model_training"],
                   data["public_redistribution"], data["redaction_applied"],
                   data["export_restricted"])


@dataclass(frozen=True)
class PreferenceRecord:
    record_id: str
    candidate_set: CandidateSetResponse
    scoring: ScoringResponse
    model_id: str
    harness_version: str
    timestamp: str
    answer_correct: bool | None
    publication_consent: PublicationConsent | None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        _nonempty(self.record_id, "record_id")
        if not isinstance(self.candidate_set, CandidateSetResponse):
            raise ValueError("candidate_set must be a CandidateSetResponse")
        if not isinstance(self.scoring, ScoringResponse):
            raise ValueError("scoring must be a ScoringResponse")
        if self.scoring.request_id != self.candidate_set.request_id:
            raise ValueError("scoring request_id does not match candidate set")
        _nonempty(self.model_id, "model_id")
        _nonempty(self.harness_version, "harness_version")
        _optional_bool(self.answer_correct, "answer_correct")
        if self.publication_consent is not None and not isinstance(self.publication_consent,
                                                                   PublicationConsent):
            raise ValueError("publication_consent must be PublicationConsent or null")
        try:
            parsed = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("timestamp must be an ISO-8601 string") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must include a UTC offset")

    @property
    def geometry_hash(self) -> str:
        return self.candidate_set.geometry_hash

    @property
    def feature(self) -> str:
        return self.candidate_set.feature

    @property
    def task_text(self) -> str:
        return self.candidate_set.task_text

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "record_id": self.record_id,
                "candidate_set": self.candidate_set.to_dict(), "scoring": self.scoring.to_dict(),
                "model_id": self.model_id, "harness_version": self.harness_version,
                "timestamp": self.timestamp, "answer_correct": self.answer_correct,
                "publication_consent": (None if self.publication_consent is None
                                         else self.publication_consent.to_dict())}

    @classmethod
    def from_dict(cls, value: Any) -> "PreferenceRecord":
        data = _mapping(value, "PreferenceRecord")
        fields = {"schema_version", "record_id", "candidate_set", "scoring", "model_id",
                  "harness_version", "timestamp", "answer_correct", "publication_consent"}
        _exact(data, fields, "PreferenceRecord")
        consent = data["publication_consent"]
        return cls(_nonempty(data["record_id"], "record_id"),
                   CandidateSetResponse.from_dict(data["candidate_set"]),
                   ScoringResponse.from_dict(data["scoring"]),
                   _nonempty(data["model_id"], "model_id"),
                   _nonempty(data["harness_version"], "harness_version"),
                   _nonempty(data["timestamp"], "timestamp"),
                   _optional_bool(data["answer_correct"], "answer_correct"),
                   None if consent is None else PublicationConsent.from_dict(consent),
                   data["schema_version"])

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> "PreferenceRecord":
        return cls.from_dict(json.loads(value))
