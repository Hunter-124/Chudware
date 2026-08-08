import copy

import pytest

from services.perception.packet import build_evidence_packet, validate_evidence_packet


def _packet(**overrides):
    values = {
        "event_id": "event-001",
        "established_seq": 42,
        "device_id": "device-001",
        "run_id": "run-001",
        "job_revision": "rev-3",
        "artifact_sha256": "a" * 64,
        "camera_ids": ["cam-a", "cam-b"],
        "interval": {"tick_start": 100, "tick_apex": 104, "tick_end": 108},
        "kind": "forecast_divergence",
        "surprise_q": 0.94,
        "surprise_raw_nll": 12.5,
        "anomaly": 0.88,
        "importance": 0.71,
        "predictive_sigma": 0.31,
        "camera_relevance": [0.8, 0.2],
        "model_id": "mldp-fixture",
        "model_sha256": "b" * 64,
        "feature_spec_id": "c" * 64,
        "calibration_id": "cal-1",
        "thresholds_id": "thr-1",
        "qualification": {
            "id": "station-1", "kind": "station_profile",
            "established_at": "2026-08-08T00:00:00Z",
            "expires_at": "2027-08-08T00:00:00Z", "state": "live",
        },
        "t_mono_ns": 123_000_000,
        "evidence": [{"kind": "frame", "uri": "fixture://frame", "sha256": "d" * 64, "synthetic": False}],
        "metrology_refs": ["sha256:" + "e" * 64],
        "media_id": "media-1",
    }
    values.update(overrides)
    return build_evidence_packet(**values)


def test_packet_schema_is_canonical_and_advisory_only():
    packet = _packet()
    validate_evidence_packet(packet)
    assert packet["evidence_class"] == "advisory"
    assert packet["accuracy_tier"] == "A2_station"
    assert packet["approval_id"] is None
    assert packet["authority"] == {
        "src": "advisory", "offset_source": False, "gate_input": False, "safety_function": False,
    }
    fields = [packet["scores"][name] for name in ("surprise_q", "anomaly", "importance")]
    fields += [item["relevance"] for item in packet["camera_relevance"]]
    assert all(field["src"] == "advisory" and field["ttl_ms"] is None for field in fields)
    assert _packet()["packet_sha256"] == packet["packet_sha256"]


def test_packet_rejects_synthetic_evidence_and_provenance_tampering():
    with pytest.raises(ValueError, match="Synthetic|synthetic"):
        _packet(evidence=[{"kind": "frame", "synthetic": True}])
    packet = _packet()
    changed = copy.deepcopy(packet)
    changed["scores"]["anomaly"]["src"] = "probed"
    with pytest.raises(ValueError, match="packet_sha256|advisory"):
        validate_evidence_packet(changed)
