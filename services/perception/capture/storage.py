from __future__ import annotations

from dataclasses import asdict
import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from services.perception.config import ModelSpec
from training.perception.manifest import ManifestRecord, validate_manifest

from .redaction import Redactor
from .types import (
    ADVISORY_SOURCE,
    CaptureBinding,
    CaptureGap,
    EventInterval,
    FrameSample,
    TelemetrySample,
    TICK_NS,
)

_ASSET_FIELDS = {
    "clip_id",
    "uri",
    "sha256",
    "bytes",
    "codec",
    "resolution",
    "fps_nominal",
    "fps_measured",
    "duration_s",
}
_RIGHTS_FIELDS = {
    "license",
    "license_ref",
    "consent_ref",
    "operator_id_hash",
    "redaction_applied",
    "retention_class",
    "export_restricted",
}


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def validate_manifest_template(
    template: Mapping[str, Any], binding: CaptureBinding, camera_id: str
) -> dict[str, Any]:
    missing_rights = sorted(_RIGHTS_FIELDS - set(template))
    if missing_rights:
        raise ValueError(f"capture provenance missing rights fields: {', '.join(missing_rights)}")
    if template.get("run_id") != binding.run_id or template.get("device_id") != binding.device_id:
        raise ValueError("manifest provenance must match the bound run and device")
    if template.get("camera_id") != camera_id:
        raise ValueError("manifest provenance must match its bound camera")

    probe = dict(template)
    probe.update(
        {
            "clip_id": "capture-template-probe",
            "uri": "capture://template-probe",
            "sha256": "0" * 64,
            "bytes": 0,
            "codec": "raw-bgr/base64",
            "resolution": [1, 1],
            "fps_nominal": 4.0,
            "fps_measured": 4.0,
            "duration_s": 0.25,
        }
    )
    ManifestRecord.from_dict(probe)
    return {key: value for key, value in template.items() if key not in _ASSET_FIELDS}


class CaptureStorage:
    """Local event-centric store. Frames have no write path outside ``commit_event``."""

    def __init__(
        self,
        root: str | Path,
        *,
        binding: CaptureBinding,
        spec: ModelSpec,
        manifest_templates: Mapping[str, Mapping[str, Any]],
        redactor: Redactor,
        pre_roll_ns: int,
        post_roll_ns: int,
    ) -> None:
        self._binding = binding
        self._spec = spec
        self._redactor = redactor
        if set(manifest_templates) != set(binding.camera_ids):
            raise ValueError("manifest provenance must exist for exactly the bound cameras")
        self._templates = {
            camera_id: validate_manifest_template(manifest_templates[camera_id], binding, camera_id)
            for camera_id in binding.camera_ids
        }
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clips_dir = self.root / "clips"
        self.clips_dir.mkdir(exist_ok=True)
        self._manifest_records: list[dict[str, Any]] = []
        self._pre_roll_ns = pre_roll_ns
        self._post_roll_ns = post_roll_ns

        session = {
            "schema": "aicad.perception_capture_session/1",
            "src": ADVISORY_SOURCE,
            "binding": asdict(binding),
            "model_spec": asdict(spec),
            "feature_spec_id": spec.feature_spec_id,
            "pre_roll_ns": pre_roll_ns,
            "post_roll_ns": post_roll_ns,
            "continuous_recording": False,
            "manifest_templates": self._templates,
        }
        self._write_json(self.root / "session.json", "session", session)
        for name in ("telemetry.jsonl", "gaps.jsonl", "windows.jsonl", "window_provenance.jsonl"):
            (self.root / name).touch(exist_ok=True)
        self._write_manifest()

    def append_telemetry(self, sample: TelemetrySample) -> None:
        self._append_jsonl(self.root / "telemetry.jsonl", "telemetry", sample.to_record())

    def append_gap(self, gap: CaptureGap) -> None:
        self._append_jsonl(self.root / "gaps.jsonl", "gap", gap.to_record())

    def commit_event(
        self, event: EventInterval, frames: Sequence[FrameSample]
    ) -> tuple[dict[str, Any], ...]:
        requested_start = max(0, event.start_mono_ns - self._pre_roll_ns)
        requested_end = event.end_mono_ns + self._post_roll_ns
        committed: list[dict[str, Any]] = []
        for camera_id in self._binding.camera_ids:
            selected = [
                sample
                for sample in frames
                if sample.camera_id == camera_id
                and requested_start <= sample.t_mono_ns <= requested_end
            ]
            if not selected:
                self.append_gap(
                    CaptureGap(
                        t_mono_ns=event.start_mono_ns,
                        lane=f"camera:{camera_id}",
                        why=f"event {event.event_id} has no retained frames",
                    )
                )
                continue
            committed.append(self._commit_camera_clip(event, camera_id, selected, requested_start, requested_end))
        return tuple(committed)

    def replace_windows(
        self, windows: Sequence[Mapping[str, Any]], provenance: Sequence[Mapping[str, Any]]
    ) -> None:
        if len(windows) != len(provenance):
            raise ValueError("every window requires one provenance row")
        redacted_windows = [self._redact_mapping("window", record) for record in windows]
        redacted_provenance = [self._redact_mapping("window_provenance", record) for record in provenance]
        self._write_jsonl(self.root / "windows.jsonl", redacted_windows)
        self._write_jsonl(self.root / "window_provenance.jsonl", redacted_provenance)

    def _commit_camera_clip(
        self,
        event: EventInterval,
        camera_id: str,
        selected: Sequence[FrameSample],
        requested_start: int,
        requested_end: int,
    ) -> dict[str, Any]:
        encoded_frames: list[dict[str, Any]] = []
        for sample in selected:
            redacted = np.asarray(
                self._redactor.redact_frame(camera_id, sample.t_mono_ns, sample.frame)
            )
            if redacted.shape != sample.frame.shape:
                raise ValueError("redactor must preserve frame shape")
            contiguous = np.ascontiguousarray(redacted)
            encoded_frames.append(
                {
                    "camera_id": camera_id,
                    "t_mono_ns": sample.t_mono_ns,
                    "seq": sample.seq,
                    "shape": list(contiguous.shape),
                    "dtype": contiguous.dtype.str,
                    "pixels_b64": base64.b64encode(contiguous.tobytes()).decode("ascii"),
                }
            )

        body: dict[str, Any] = {
            "schema": "aicad.perception_capture_clip/1",
            "src": ADVISORY_SOURCE,
            "event_id": event.event_id,
            "event_kind": event.kind,
            "run_id": self._binding.run_id,
            "device_id": self._binding.device_id,
            "camera_id": camera_id,
            "requested_start_mono_ns": requested_start,
            "requested_end_mono_ns": requested_end,
            "actual_start_mono_ns": selected[0].t_mono_ns,
            "actual_end_mono_ns": selected[-1].t_mono_ns,
            "truncated": selected[0].t_mono_ns > requested_start or selected[-1].t_mono_ns < requested_end,
            "frames": encoded_frames,
        }
        body = self._redact_mapping("clip", body)
        clip_id = hashlib.sha256(_canonical_bytes(body)).hexdigest()
        body["clip_id"] = clip_id
        encoded = _canonical_bytes(body)
        path = self.clips_dir / f"{clip_id}.json"
        path.write_bytes(encoded)

        shape = selected[0].frame.shape
        height, width = int(shape[0]), int(shape[1])
        duration_s = max(TICK_NS, selected[-1].t_mono_ns - selected[0].t_mono_ns + TICK_NS) / 1e9
        record = dict(self._templates[camera_id])
        record.update(
            {
                "clip_id": clip_id,
                "uri": path.relative_to(self.root).as_posix(),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "bytes": len(encoded),
                "codec": "raw-bgr/base64",
                "resolution": [width, height],
                "fps_nominal": float(record.get("fps_nominal", 4.0)),
                "fps_measured": len(selected) / duration_s,
                "duration_s": duration_s,
                "redaction_applied": True,
            }
        )
        record = self._redact_mapping("manifest", record)
        ManifestRecord.from_dict(record)
        self._manifest_records.append(record)
        validate_manifest(self._manifest_records)
        self._write_manifest()
        return body

    def _redact_mapping(self, kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
        redacted = self._redactor.redact_record(kind, value)
        if not isinstance(redacted, Mapping):
            raise ValueError("redactor must return a mapping")
        return dict(redacted)

    def _write_json(self, path: Path, kind: str, value: Mapping[str, Any]) -> None:
        redacted = self._redact_mapping(kind, value)
        path.write_bytes(_canonical_bytes(redacted))

    def _write_manifest(self) -> None:
        wrapper = self._redact_mapping(
            "manifest_collection", {"records": [dict(record) for record in self._manifest_records]}
        )
        records = wrapper.get("records")
        if not isinstance(records, list) or any(not isinstance(record, Mapping) for record in records):
            raise ValueError("manifest collection redactor must preserve the records list")
        normalized = [dict(record) for record in records]
        validate_manifest(normalized)
        self._manifest_records = normalized
        payload = json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        (self.root / "manifest.json").write_bytes(payload)

    def _append_jsonl(self, path: Path, kind: str, value: Mapping[str, Any]) -> None:
        redacted = self._redact_mapping(kind, value)
        with path.open("ab") as handle:
            handle.write(_canonical_bytes(redacted) + b"\n")

    @staticmethod
    def _write_jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
        payload = b"".join(_canonical_bytes(value) + b"\n" for value in values)
        path.write_bytes(payload)
