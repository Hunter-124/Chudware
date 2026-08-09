from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from services.perception.config import ModelSpec
from training.perception.data import WINDOW_SCHEMA, WindowBatch
from training.perception.manifest import validate_manifest

from .alignment import align_lane
from .features import FrameFeatureExtractor
from .types import ADVISORY_SOURCE, CaptureBinding, FrameSample, TICK_NS, TelemetrySample


@dataclass(frozen=True)
class ReplayResult:
    windows: tuple[dict[str, Any], ...]
    provenance: tuple[dict[str, Any], ...]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw["records"] if isinstance(raw, dict) else raw
    return [record.to_dict() for record in validate_manifest(records)]


def _load_clip(path: Path, extractor: FrameFeatureExtractor) -> tuple[dict[str, Any], tuple[FrameSample, ...]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("src") != ADVISORY_SOURCE:
        raise ValueError("capture clips must be advisory")
    samples: list[FrameSample] = []
    for item in raw["frames"]:
        shape = tuple(int(value) for value in item["shape"])
        dtype = np.dtype(item["dtype"])
        pixels = np.frombuffer(base64.b64decode(item["pixels_b64"], validate=True), dtype=dtype)
        frame = np.array(pixels.reshape(shape), copy=True, order="C")
        samples.append(
            FrameSample(
                camera_id=str(item["camera_id"]),
                t_mono_ns=int(item["t_mono_ns"]),
                seq=int(item["seq"]),
                frame=frame,
                features=extractor.extract(frame),
            )
        )
    return raw, tuple(samples)


def replay_session(root: str | Path, *, stride_ticks: int = 1) -> ReplayResult:
    """Rebuild model-ready windows only from persisted, already-redacted session data."""

    if stride_ticks <= 0:
        raise ValueError("replay stride must be positive")
    root_path = Path(root)
    session = json.loads((root_path / "session.json").read_text(encoding="utf-8"))
    if session.get("src") != ADVISORY_SOURCE or session.get("continuous_recording") is not False:
        raise ValueError("capture session must be advisory and event-centric")

    binding_raw = session["binding"]
    binding = CaptureBinding(
        run_id=str(binding_raw["run_id"]),
        device_id=str(binding_raw["device_id"]),
        camera_ids=tuple(binding_raw["camera_ids"]),
        sensor_lanes=tuple(binding_raw["sensor_lanes"]),
        tick_origin_mono_ns=int(binding_raw["tick_origin_mono_ns"]),
        command_lane=binding_raw.get("command_lane"),
    )
    spec_raw = session["model_spec"]
    spec = ModelSpec(
        cameras=int(spec_raw["cameras"]),
        window=int(spec_raw["window"]),
        camera_features=int(spec_raw["camera_features"]),
        process_features=int(spec_raw["process_features"]),
        command_features=int(spec_raw["command_features"]),
        latent_features=int(spec_raw["latent_features"]),
        sensor_features=int(spec_raw["sensor_features"]),
        state_labels=int(spec_raw["state_labels"]),
        hidden=int(spec_raw["hidden"]),
        horizons=tuple(int(value) for value in spec_raw["horizons"]),
        dilations=tuple(int(value) for value in spec_raw["dilations"]),
    )
    if spec.feature_spec_id != session["feature_spec_id"]:
        raise ValueError("recorded feature spec id does not match the model spec")
    if len(binding.camera_ids) > spec.cameras:
        raise ValueError("capture binds more cameras than the model accepts")

    manifest = _load_manifest(root_path / "manifest.json")
    manifest_by_clip = {record["clip_id"]: record for record in manifest}
    extractor = FrameFeatureExtractor(spec)
    grouped: dict[str, list[tuple[dict[str, Any], tuple[FrameSample, ...]]]] = {}
    for clip_path in sorted((root_path / "clips").glob("*.json")):
        clip, samples = _load_clip(clip_path, extractor)
        if clip["clip_id"] not in manifest_by_clip:
            raise ValueError(f"clip {clip['clip_id']} is absent from the landed manifest")
        grouped.setdefault(str(clip["event_id"]), []).append((clip, samples))

    telemetry = [TelemetrySample.from_record(raw) for raw in _read_jsonl(root_path / "telemetry.jsonl")]
    windows: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for event_id in sorted(grouped):
        event_windows, event_provenance = _windows_for_event(
            event_id,
            grouped[event_id],
            manifest_by_clip,
            telemetry,
            binding,
            spec,
            stride_ticks,
        )
        windows.extend(event_windows)
        provenance.extend(event_provenance)

    _validate_window_batch(windows, spec)
    return ReplayResult(tuple(windows), tuple(provenance))


def _windows_for_event(
    event_id: str,
    clips: Sequence[tuple[dict[str, Any], tuple[FrameSample, ...]]],
    manifest_by_clip: Mapping[str, Mapping[str, Any]],
    telemetry: Sequence[TelemetrySample],
    binding: CaptureBinding,
    spec: ModelSpec,
    stride_ticks: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    frame_by_camera_tick: dict[tuple[str, int], FrameSample] = {}
    clip_ids: list[str] = []
    event_start = min(int(clip["requested_start_mono_ns"]) for clip, _ in clips)
    event_end = max(int(clip["requested_end_mono_ns"]) for clip, _ in clips)
    all_ticks: list[int] = []
    for clip, samples in clips:
        clip_ids.append(str(clip["clip_id"]))
        for sample in samples:
            tick = round((sample.t_mono_ns - binding.tick_origin_mono_ns) / TICK_NS)
            canonical_time = binding.tick_origin_mono_ns + tick * TICK_NS
            if abs(sample.t_mono_ns - canonical_time) > TICK_NS // 2:
                continue
            frame_by_camera_tick[(sample.camera_id, tick)] = sample
            all_ticks.append(tick)
    if not all_ticks:
        return [], []

    first_tick = min(all_ticks)
    last_tick = max(all_ticks)
    first_t0 = first_tick + spec.window - 1
    last_t0 = last_tick - spec.max_horizon
    if first_t0 > last_t0:
        return [], []

    all_context_times = [
        binding.tick_origin_mono_ns + tick * TICK_NS for tick in range(first_tick, last_tick + 1)
    ]
    lane_aligned: dict[str, dict[int, Any]] = {}
    for lane in binding.sensor_lanes:
        lane_samples = [sample for sample in telemetry if sample.lane == lane]
        lane_width = max((len(sample.values) for sample in lane_samples), default=1)
        aligned = align_lane(lane_samples, all_context_times, width=lane_width)
        lane_aligned[lane] = {
            first_tick + index: value for index, value in enumerate(aligned)
        }

    command_aligned: dict[int, Any] = {}
    if binding.command_lane:
        command_samples = [sample for sample in telemetry if sample.lane == binding.command_lane]
        command_ticks = [
            binding.tick_origin_mono_ns + tick * TICK_NS
            for tick in range(first_tick, last_tick + 1)
        ]
        command_aligned = {
            first_tick + index: value
            for index, value in enumerate(
                align_lane(command_samples, command_ticks, width=spec.command_features)
            )
        }

    result: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    sorted_clip_ids = sorted(set(clip_ids))
    primary_clip_id = sorted_clip_ids[0]
    for t0 in range(first_t0, last_t0 + 1, stride_ticks):
        cam_feat = np.zeros((spec.cameras, spec.window, spec.camera_features), dtype=np.float32)
        cam_mask = np.zeros((spec.cameras, spec.window), dtype=np.int8)
        cam_age = np.zeros((spec.cameras, spec.window), dtype=np.float32)
        for camera_slot, camera_id in enumerate(binding.camera_ids):
            last_seen: int | None = None
            for offset, tick in enumerate(range(t0 - spec.window + 1, t0 + 1)):
                sample = frame_by_camera_tick.get((camera_id, tick))
                if sample is not None:
                    cam_feat[camera_slot, offset] = sample.features
                    cam_mask[camera_slot, offset] = 1
                    cam_age[camera_slot, offset] = 0
                    last_seen = tick
                elif last_seen is not None:
                    cam_age[camera_slot, offset] = tick - last_seen

        proc_feat = np.zeros((spec.window, spec.process_features), dtype=np.float32)
        proc_mask = np.zeros((spec.window, spec.process_features), dtype=np.int8)
        proc_age = np.zeros((spec.window, spec.process_features), dtype=np.float32)
        for offset, tick in enumerate(range(t0 - spec.window + 1, t0 + 1)):
            column = 0
            for lane in binding.sensor_lanes:
                aligned = lane_aligned[lane][tick]
                for value, mask, age in zip(aligned.values, aligned.mask, aligned.age_ticks):
                    if column >= spec.process_features:
                        break
                    proc_feat[offset, column] = value
                    proc_mask[offset, column] = mask
                    proc_age[offset, column] = age
                    column += 1
                if column >= spec.process_features:
                    break

        cmd_future = np.zeros((spec.max_horizon, spec.command_features), dtype=np.float32)
        cmd_mask = np.zeros((spec.max_horizon,), dtype=np.int8)
        for index, tick in enumerate(range(t0 + 1, t0 + spec.max_horizon + 1)):
            aligned = command_aligned.get(tick)
            if aligned is not None and not aligned.gap:
                cmd_future[index] = aligned.values
                cmd_mask[index] = int(any(aligned.mask))

        target_latent = np.zeros(
            (len(spec.horizons), spec.cameras, spec.latent_features), dtype=np.float32
        )
        for horizon_slot, horizon in enumerate(spec.horizons):
            for camera_slot, camera_id in enumerate(binding.camera_ids):
                future = frame_by_camera_tick.get((camera_id, t0 + horizon))
                if future is not None:
                    width = min(spec.latent_features, len(future.features))
                    target_latent[horizon_slot, camera_slot, :width] = future.features[:width]

        t0_ns = binding.tick_origin_mono_ns + t0 * TICK_NS
        identity = {
            "clip_ids": sorted_clip_ids,
            "event_id": event_id,
            "feature_spec_id": spec.feature_spec_id,
            "t0_tick": t0,
        }
        window_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        record: dict[str, Any] = {
            "window_id": window_id,
            "feature_spec_id": spec.feature_spec_id,
            "clip_id": primary_clip_id,
            "split": manifest_by_clip[primary_clip_id]["split"],
            "t0_tick": t0,
            "cam_feat": cam_feat.tolist(),
            "cam_mask": cam_mask.tolist(),
            "cam_age": cam_age.tolist(),
            "proc_feat": proc_feat.tolist(),
            "proc_mask": proc_mask.tolist(),
            "proc_age": proc_age.tolist(),
            "cmd_future": cmd_future.tolist(),
            "cmd_mask": cmd_mask.tolist(),
            "target_latent": target_latent.tolist(),
            "event_label": event_start <= t0_ns <= event_end,
        }
        expected_keys = set(WINDOW_SCHEMA["required"])
        if set(record) != expected_keys:
            raise ValueError("capture window does not match the landed window schema")
        result.append(record)
        provenance.append(
            {
                "schema": "aicad.perception_window_provenance/1",
                "src": ADVISORY_SOURCE,
                "window_id": window_id,
                "run_id": binding.run_id,
                "device_id": binding.device_id,
                "camera_ids": list(binding.camera_ids),
                "sensor_lanes": list(binding.sensor_lanes),
                "manifest_records": [manifest_by_clip[clip_id] for clip_id in sorted_clip_ids],
            }
        )
    return result, provenance


def _validate_window_batch(windows: Sequence[Mapping[str, Any]], spec: ModelSpec) -> None:
    if not windows:
        return
    batch = WindowBatch(
        cam_feat=np.asarray([record["cam_feat"] for record in windows], dtype=np.float32),
        cam_mask=np.asarray([record["cam_mask"] for record in windows], dtype=np.int8),
        cam_age=np.asarray([record["cam_age"] for record in windows], dtype=np.float32),
        proc_feat=np.asarray([record["proc_feat"] for record in windows], dtype=np.float32),
        proc_mask=np.asarray([record["proc_mask"] for record in windows], dtype=np.int8),
        proc_age=np.asarray([record["proc_age"] for record in windows], dtype=np.float32),
        cmd_future=np.asarray([record["cmd_future"] for record in windows], dtype=np.float32),
        cmd_mask=np.asarray([record["cmd_mask"] for record in windows], dtype=np.int8),
        target_latent=np.asarray([record["target_latent"] for record in windows], dtype=np.float32),
        event_label=np.asarray([record["event_label"] for record in windows], dtype=np.bool_),
        window_ids=tuple(str(record["window_id"]) for record in windows),
    )
    batch.validate(spec)
