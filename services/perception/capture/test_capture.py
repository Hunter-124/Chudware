from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from services.perception.config import ModelSpec
from training.perception.data import WINDOW_SCHEMA
from training.perception.manifest import load_manifest

from .alignment import align_lane
from .buffer import RollingFrameBuffer
from .redaction import PassThroughRedactor
from .replay import replay_session
from .session import CaptureSession, NonBlockingCapture
from .types import CaptureBinding, EventInterval, FrameSample, TICK_NS, TelemetrySample


FIXTURE = Path("fixtures/mfg/perception/capture/manifest_template.json")
RIGHTS_FIELDS = {
    "license",
    "license_ref",
    "consent_ref",
    "operator_id_hash",
    "redaction_applied",
    "retention_class",
    "export_restricted",
}


def _spec() -> ModelSpec:
    return ModelSpec(
        cameras=1,
        window=4,
        camera_features=6,
        process_features=2,
        command_features=2,
        latent_features=3,
        sensor_features=2,
        state_labels=3,
        hidden=8,
        horizons=(1, 2),
        dilations=(1, 2),
    )


def _binding() -> CaptureBinding:
    return CaptureBinding(
        run_id="run:capture-test-001",
        device_id="dev:emulated:capturetest01",
        camera_ids=("camera-1",),
        sensor_lanes=("controller",),
        tick_origin_mono_ns=1_000_000_000,
        command_lane="command",
    )


def _template() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _session(tmp_path: Path, *, redactor=None, extractor=None, ceiling=2_000_000) -> CaptureSession:
    return CaptureSession(
        tmp_path,
        binding=_binding(),
        spec=_spec(),
        manifest_templates={"camera-1": _template()},
        redactor=redactor or PassThroughRedactor(),
        memory_ceiling_bytes=ceiling,
        buffer_horizon_ns=20 * TICK_NS,
        pre_roll_ns=2 * TICK_NS,
        post_roll_ns=2 * TICK_NS,
        feature_extractor=extractor,
    )


def _record_event_session(session: CaptureSession) -> None:
    origin = session.binding.tick_origin_mono_ns
    for tick in range(12):
        frame = np.full((8, 8, 3), 40 + tick, dtype=np.uint8)
        assert session.capture_frame("camera-1", origin + tick * TICK_NS, tick, frame).accepted
        assert session.capture_telemetry(
            "controller", origin + tick * TICK_NS, tick, [tick, tick + 0.5]
        ).accepted
        assert session.capture_telemetry(
            "command", origin + tick * TICK_NS, tick, [tick + 1, tick + 2]
        ).accepted
    clips = session.commit_event(
        EventInterval(
            "event-1",
            origin + 4 * TICK_NS,
            origin + 5 * TICK_NS,
            "forecast_divergence",
        )
    )
    assert len(clips) == 1


def test_window_provenance_is_complete_and_missing_rights_are_rejected(tmp_path: Path):
    session = _session(tmp_path / "good")
    _record_event_session(session)
    result = session.finalize()
    assert result.windows
    assert set(result.windows[0]) == set(WINDOW_SCHEMA["required"])
    row = result.provenance[0]
    assert row["src"] == "advisory"
    assert row["run_id"] == session.binding.run_id
    assert row["device_id"] == session.binding.device_id
    manifest = row["manifest_records"][0]
    assert RIGHTS_FIELDS <= set(manifest)
    assert manifest["redaction_applied"] is True
    assert manifest["export_restricted"] is True
    assert load_manifest(tmp_path / "good" / "manifest.json")[0].to_dict() == manifest

    broken = _template()
    broken.pop("consent_ref")
    with pytest.raises(ValueError, match="missing rights fields: consent_ref"):
        CaptureSession(
            tmp_path / "broken",
            binding=_binding(),
            spec=_spec(),
            manifest_templates={"camera-1": broken},
            redactor=PassThroughRedactor(),
            memory_ceiling_bytes=100_000,
        )


def test_ring_buffer_enforces_memory_ceiling_under_overflow():
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    sample = FrameSample("camera-1", 0, 0, frame, (0.0,) * 6)
    ceiling = sample.retained_bytes * 2
    ring = RollingFrameBuffer(memory_ceiling_bytes=ceiling, horizon_ns=100 * TICK_NS)
    evictions = 0
    for index in range(10):
        owned = np.array(frame, copy=True)
        result = ring.append(FrameSample("camera-1", index * TICK_NS, index, owned, (0.0,) * 6))
        assert result.accepted
        evictions += result.evicted
        assert ring.retained_bytes <= ring.memory_ceiling_bytes
    assert ring.frame_count == 2
    assert evictions == 8


def test_monotonic_alignment_marks_injected_gap_without_interpolation():
    samples = [
        TelemetrySample("controller", 0, 1, (1.0,)),
        TelemetrySample("controller", TICK_NS, 2, (2.0,)),
        TelemetrySample("controller", 4 * TICK_NS, 3, (10.0,)),
    ]
    aligned = align_lane(samples, [tick * TICK_NS for tick in range(5)], width=1)
    assert [item.values[0] for item in aligned] == [1.0, 2.0, 0.0, 0.0, 10.0]
    assert [item.gap for item in aligned] == [False, False, True, True, False]
    assert all(item.mask == (0,) and item.why for item in aligned[2:4])
    assert 6.0 not in [item.values[0] for item in aligned]


def test_event_clip_uses_requested_boundaries_and_no_continuous_recording(tmp_path: Path):
    session = _session(tmp_path)
    _record_event_session(session)
    clip_path = next((tmp_path / "clips").glob("*.json"))
    clip = json.loads(clip_path.read_text(encoding="utf-8"))
    origin = session.binding.tick_origin_mono_ns
    assert clip["requested_start_mono_ns"] == origin + 2 * TICK_NS
    assert clip["requested_end_mono_ns"] == origin + 7 * TICK_NS
    assert [item["t_mono_ns"] for item in clip["frames"]] == [
        origin + tick * TICK_NS for tick in range(2, 8)
    ]
    assert clip["truncated"] is False
    session_record = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))
    assert session_record["continuous_recording"] is False
    assert list((tmp_path / "clips").glob("*.json")) == [clip_path]


class _BlackoutRedactor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def redact_frame(self, camera_id, t_mono_ns, frame):
        self.calls.append(("frame", camera_id))
        return np.zeros_like(frame)

    def redact_record(self, kind, record):
        self.calls.append(("record", kind))
        return dict(record)


def test_redaction_happens_before_clip_and_window_write(tmp_path: Path):
    redactor = _BlackoutRedactor()
    session = _session(tmp_path, redactor=redactor)
    _record_event_session(session)
    session.finalize()

    clip = json.loads(next((tmp_path / "clips").glob("*.json")).read_text(encoding="utf-8"))
    for item in clip["frames"]:
        pixels = np.frombuffer(base64.b64decode(item["pixels_b64"]), dtype=np.dtype(item["dtype"]))
        assert np.count_nonzero(pixels) == 0
    assert ("record", "clip") in redactor.calls
    assert ("record", "window") in redactor.calls
    assert redactor.calls.index(("record", "clip")) < redactor.calls.index(("record", "window"))


def test_offline_replay_is_deterministic_and_identical_to_session_windows(tmp_path: Path):
    session = _session(tmp_path)
    _record_event_session(session)
    first = session.finalize()
    second = replay_session(tmp_path)
    third = replay_session(tmp_path)
    assert first == second == third
    persisted = [
        json.loads(line)
        for line in (tmp_path / "windows.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert persisted == list(first.windows)


def test_replay_refuses_clip_whose_bytes_miss_manifest_sha256(tmp_path: Path):
    session = _session(tmp_path)
    _record_event_session(session)
    session.finalize()
    clip_path = next((tmp_path / "clips").glob("*.json"))
    clip = json.loads(clip_path.read_text(encoding="utf-8"))
    clip["event_kind"] = "tampered"
    clip_path.write_bytes(
        json.dumps(clip, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )

    with pytest.raises(ValueError, match=rf"clip {clip_path.name} sha256 mismatch"):
        replay_session(tmp_path)


def test_replay_refuses_clip_whose_canonical_body_misses_clip_id(tmp_path: Path):
    session = _session(tmp_path)
    _record_event_session(session)
    session.finalize()
    clip_path = next((tmp_path / "clips").glob("*.json"))
    clip = json.loads(clip_path.read_text(encoding="utf-8"))
    clip["event_kind"] = "tampered"
    encoded = json.dumps(
        clip, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    clip_path.write_bytes(encoded)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[0]["sha256"] = hashlib.sha256(encoded).hexdigest()
    manifest[0]["bytes"] = len(encoded)
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )

    with pytest.raises(ValueError, match=rf"clip {clip_path.name} clip_id mismatch"):
        replay_session(tmp_path)


class _CrashingExtractor:
    def extract(self, frame):
        raise RuntimeError("simulated encoder crash")


def test_simulated_capture_crash_degrades_to_gap_and_cannot_act(tmp_path: Path):
    session = _session(tmp_path, extractor=_CrashingExtractor())
    async_capture = NonBlockingCapture(session, queue_capacity=2)
    origin = session.binding.tick_origin_mono_ns
    assert async_capture.submit_frame(
        "camera-1", origin, 0, np.ones((4, 4, 3), dtype=np.uint8)
    )
    async_capture.drain()
    async_capture.close()

    gaps = [json.loads(line) for line in (tmp_path / "gaps.jsonl").read_text().splitlines()]
    assert any("simulated encoder crash" in gap["why"] for gap in gaps)
    assert all(gap["src"] == "advisory" for gap in gaps)
    assert session.machine_actions == async_capture.machine_actions == ()
    assert session.retained_frames == 0
