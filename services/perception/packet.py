from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

SCHEMA = "aicad.perception_event/1"
_ALLOWED_KINDS = {
    "process_deviation",
    "state_change",
    "view_degraded",
    "forecast_divergence",
}


def advisory_value(
    value: float | None,
    *,
    state: str,
    t_mono_ns: int,
    seq: int,
    age_ms: int,
    why: str = "",
    last_ok: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if state == "ok":
        if value is None or not math.isfinite(float(value)):
            raise ValueError("ok advisory values require a finite value")
    elif value is not None:
        raise ValueError("non-ok advisory values must have v=null")
    field: dict[str, Any] = {
        "v": None if value is None else float(value),
        "state": state,
        "t_mono_ns": int(t_mono_ns),
        "seq": int(seq),
        "age_ms": int(age_ms),
        "ttl_ms": None,
        "src": "advisory",
        "why": str(why),
    }
    if last_ok is not None:
        field["last_ok"] = dict(last_ok)
    return field


def _canonical_bytes(packet: Mapping[str, Any]) -> bytes:
    identity = {key: value for key, value in packet.items() if key != "packet_sha256"}
    return json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def build_evidence_packet(
    *,
    event_id: str,
    established_seq: int,
    device_id: str,
    run_id: str,
    job_revision: str,
    artifact_sha256: str,
    camera_ids: Sequence[str],
    interval: Mapping[str, int],
    kind: str,
    surprise_q: float,
    surprise_raw_nll: float,
    anomaly: float,
    importance: float,
    predictive_sigma: float,
    camera_relevance: Sequence[float],
    model_id: str,
    model_sha256: str,
    feature_spec_id: str,
    calibration_id: str,
    thresholds_id: str,
    qualification: Mapping[str, Any],
    t_mono_ns: int,
    age_ms: int = 0,
    evidence: Sequence[Mapping[str, Any]] = (),
    metrology_refs: Sequence[str] = (),
    input_health: Sequence[Mapping[str, Any]] = (),
    transport_secure: bool = True,
    media_id: str = "",
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Build an immutable, advisory-only evidence packet.

    There is deliberately no parameter for an evidence class, accuracy tier, offset,
    gate, or safety authority. Those properties therefore cannot be elevated by a caller.
    """
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"unsupported perception event kind {kind!r}")
    if len(camera_ids) != len(camera_relevance):
        raise ValueError("camera relevance must have one value per camera")
    if established_seq < 1:
        raise ValueError("established_seq must be gateway-issued and positive")
    if set(interval) != {"tick_start", "tick_apex", "tick_end"}:
        raise ValueError("interval must contain tick_start, tick_apex, and tick_end")
    if not interval["tick_start"] <= interval["tick_apex"] <= interval["tick_end"]:
        raise ValueError("event interval ticks are not ordered")
    unit_scores = {
        "surprise_q": surprise_q,
        "anomaly": anomaly,
        "importance": importance,
        **{
            f"camera_relevance[{index}]": value
            for index, value in enumerate(camera_relevance)
        },
    }
    for name, value in unit_scores.items():
        if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
            raise ValueError(f"{name} must be finite and in [0,1]")
    if not math.isfinite(float(surprise_raw_nll)):
        raise ValueError("surprise_raw_nll must be finite")
    if not math.isfinite(float(predictive_sigma)) or predictive_sigma < 0:
        raise ValueError("predictive_sigma must be finite and non-negative")
    required_qualification = {"id", "kind", "established_at", "expires_at", "state"}
    if set(qualification) != required_qualification or qualification["state"] != "live":
        raise ValueError("a live, complete station qualification is required")
    for item in evidence:
        if item.get("synthetic") is not False:
            raise ValueError("synthetic or unmarked media can never become evidence")
    for ref in metrology_refs:
        if not ref.startswith("sha256:"):
            raise ValueError("metrology references must be content-addressed ids")

    def score(value: float) -> dict[str, Any]:
        return advisory_value(
            value,
            state="ok",
            t_mono_ns=t_mono_ns,
            seq=established_seq,
            age_ms=age_ms,
        )

    packet: dict[str, Any] = {
        "schema": SCHEMA,
        "event_id": str(event_id),
        "supersedes": supersedes,
        "established_seq": int(established_seq),
        "device_id": str(device_id),
        "run_id": str(run_id),
        "job_revision": str(job_revision),
        "artifact_sha256": str(artifact_sha256),
        "approval_id": None,
        "evidence_class": "advisory",
        "accuracy_tier": "A2_station",
        "qualification": copy.deepcopy(dict(qualification)),
        "capability": "monitor",
        "authority": {
            "src": "advisory",
            "offset_source": False,
            "gate_input": False,
            "safety_function": False,
        },
        "camera_ids": [str(value) for value in camera_ids],
        "interval": {key: int(value) for key, value in interval.items()},
        "kind": kind,
        "scores": {
            "surprise_q": score(surprise_q),
            "anomaly": score(anomaly),
            "importance": score(importance),
            "surprise_raw_nll": float(surprise_raw_nll),
            "uncertainty": {
                "predictive_sigma": float(predictive_sigma),
                "calibration_id": str(calibration_id),
                "src": "advisory",
            },
        },
        "camera_relevance": [
            {"camera_id": str(camera_id), "relevance": score(relevance)}
            for camera_id, relevance in zip(camera_ids, camera_relevance)
        ],
        "input_health": [copy.deepcopy(dict(item)) for item in input_health],
        "evidence": [copy.deepcopy(dict(item)) for item in evidence],
        "metrology_refs": [str(ref) for ref in metrology_refs],
        "provenance": {
            "src": "advisory",
            "model_id": str(model_id),
            "model_sha256": str(model_sha256),
            "feature_spec_id": str(feature_spec_id),
            "calibration_id": str(calibration_id),
            "thresholds_id": str(thresholds_id),
            "media_id": str(media_id),
            "transport_secure": bool(transport_secure),
        },
    }
    packet["packet_sha256"] = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    validate_evidence_packet(packet)
    return packet


def validate_evidence_packet(packet: Mapping[str, Any]) -> None:
    required = {
        "schema", "event_id", "packet_sha256", "established_seq", "device_id", "run_id",
        "job_revision", "artifact_sha256", "approval_id", "evidence_class", "accuracy_tier",
        "qualification", "capability", "authority", "camera_ids", "interval", "kind", "scores",
        "camera_relevance", "input_health", "evidence", "metrology_refs", "provenance", "supersedes",
    }
    if set(packet) != required:
        raise ValueError(f"packet keys mismatch: {sorted(set(packet) ^ required)}")
    if packet["schema"] != SCHEMA or packet["evidence_class"] != "advisory":
        raise ValueError("packet schema or evidence class is invalid")
    if packet["accuracy_tier"] != "A2_station" or packet["capability"] != "monitor":
        raise ValueError("perception authority may not be elevated")
    authority = packet["authority"]
    if authority != {"src": "advisory", "offset_source": False, "gate_input": False, "safety_function": False}:
        raise ValueError("perception is advisory-only and cannot be an offset, gate, or safety source")
    if packet["approval_id"] is not None:
        raise ValueError("perception packets are never approval-bound")
    for field in ("event_id", "device_id", "run_id", "job_revision", "artifact_sha256"):
        if not isinstance(packet[field], str) or not packet[field]:
            raise ValueError(f"packet field {field} must be a non-empty string")
    if not isinstance(packet["established_seq"], int) or packet["established_seq"] < 1:
        raise ValueError("established_seq must be gateway-issued and positive")
    qualification = packet["qualification"]
    if (
        not isinstance(qualification, Mapping)
        or set(qualification) != {"id", "kind", "established_at", "expires_at", "state"}
        or qualification["state"] != "live"
    ):
        raise ValueError("packet qualification must be a complete live station profile")
    if packet["kind"] not in _ALLOWED_KINDS:
        raise ValueError("packet event kind is invalid")
    expected_hash = hashlib.sha256(_canonical_bytes(packet)).hexdigest()
    if packet["packet_sha256"] != expected_hash:
        raise ValueError("packet_sha256 does not match canonical packet bytes")
    advisory_fields = [
        packet["scores"]["surprise_q"],
        packet["scores"]["anomaly"],
        packet["scores"]["importance"],
        packet["scores"]["uncertainty"],
        packet["provenance"],
        packet["authority"],
        *(item["relevance"] for item in packet["camera_relevance"]),
    ]
    if any(field.get("src") != "advisory" for field in advisory_fields):
        raise ValueError("every emitted perception field must have src=advisory")
    for field in advisory_fields[:3] + advisory_fields[6:]:
        if field.get("state") != "ok" or field.get("ttl_ms") is not None:
            raise ValueError("durable score fields must be non-decaying FreshValue values")
    for item in packet["evidence"]:
        if item.get("synthetic") is not False:
            raise ValueError("synthetic media cannot be packet evidence")
