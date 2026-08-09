from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from queue import Full, Queue
from threading import Thread
from typing import Any, Mapping, Protocol

import numpy as np

from services.perception.config import ModelSpec

from .buffer import RollingFrameBuffer
from .features import FrameFeatureExtractor
from .redaction import Redactor
from .replay import ReplayResult, replay_session
from .storage import CaptureStorage
from .types import (
    CaptureBinding,
    CaptureGap,
    CaptureOutcome,
    EventInterval,
    FrameSample,
    TelemetrySample,
)


class FeatureExtractor(Protocol):
    def extract(self, frame: np.ndarray) -> tuple[float, ...]: ...


class CaptureSession:
    """Advisory-only capture pipeline isolated from machine execution."""

    def __init__(
        self,
        root: str | Path,
        *,
        binding: CaptureBinding,
        spec: ModelSpec,
        manifest_templates: Mapping[str, Mapping[str, Any]],
        redactor: Redactor,
        memory_ceiling_bytes: int,
        buffer_horizon_ns: int = 90_000_000_000,
        pre_roll_ns: int = 8_000_000_000,
        post_roll_ns: int = 4_000_000_000,
        feature_extractor: FeatureExtractor | None = None,
    ) -> None:
        if pre_roll_ns < 0 or post_roll_ns < 0:
            raise ValueError("event roll intervals must be non-negative")
        self.binding = binding
        self.spec = spec
        self._redactor = redactor
        self._extractor = feature_extractor or FrameFeatureExtractor(spec)
        self._buffer = RollingFrameBuffer(
            memory_ceiling_bytes=memory_ceiling_bytes,
            horizon_ns=buffer_horizon_ns,
        )
        self._storage = CaptureStorage(
            root,
            binding=binding,
            spec=spec,
            manifest_templates=manifest_templates,
            redactor=redactor,
            pre_roll_ns=pre_roll_ns,
            post_roll_ns=post_roll_ns,
        )
        self._last_frame_seq: dict[str, int] = {}
        self._last_lane_seq: dict[str, int] = {}

    @property
    def retained_bytes(self) -> int:
        return self._buffer.retained_bytes

    @property
    def retained_frames(self) -> int:
        return self._buffer.frame_count

    @property
    def memory_ceiling_bytes(self) -> int:
        return self._buffer.memory_ceiling_bytes

    @property
    def machine_actions(self) -> tuple[object, ...]:
        return ()

    def capture_frame(
        self, camera_id: str, t_mono_ns: int, seq: int, frame: np.ndarray
    ) -> CaptureOutcome:
        lane = f"camera:{camera_id}"
        try:
            if camera_id not in self.binding.camera_ids:
                raise ValueError("camera is not bound to this capture session")
            if t_mono_ns < 0 or seq < 0:
                raise ValueError("frame timestamp and sequence must be non-negative")
            previous = self._last_frame_seq.get(camera_id)
            if previous is not None and seq != previous + 1:
                self._record_gap(
                    CaptureGap(
                        t_mono_ns=t_mono_ns,
                        lane=lane,
                        why=f"sequence gap {previous + 1}..{seq - 1}",
                        expected_seq=previous + 1,
                        observed_seq=seq,
                    )
                )
            self._last_frame_seq[camera_id] = seq

            redacted = np.asarray(self._redactor.redact_frame(camera_id, t_mono_ns, frame))
            if redacted.shape != np.asarray(frame).shape:
                raise ValueError("redactor must preserve frame shape")
            owned = np.array(redacted, copy=True, order="C")
            features = self._extractor.extract(owned)
            if len(features) != self.spec.camera_features:
                raise ValueError("feature extractor returned the wrong camera feature width")
            sample = FrameSample(camera_id, t_mono_ns, seq, owned, tuple(float(v) for v in features))
            append = self._buffer.append(sample)
            if not append.accepted:
                gap = CaptureGap(t_mono_ns=t_mono_ns, lane=lane, why=append.reason or "frame dropped")
                self._record_gap(gap)
                return CaptureOutcome(False, gap, append.evicted)
            return CaptureOutcome(True, None, append.evicted)
        except Exception as exc:
            gap = CaptureGap(
                t_mono_ns=max(0, int(t_mono_ns)),
                lane=lane,
                why=f"capture failure: {type(exc).__name__}: {exc}",
            )
            self._record_gap(gap)
            return CaptureOutcome(False, gap)

    def capture_telemetry(
        self, lane: str, t_mono_ns: int, seq: int, values: tuple[float, ...] | list[float]
    ) -> CaptureOutcome:
        try:
            allowed = set(self.binding.sensor_lanes)
            if self.binding.command_lane:
                allowed.add(self.binding.command_lane)
            if lane not in allowed:
                raise ValueError("telemetry lane is not bound to this capture session")
            numeric = tuple(float(value) for value in values)
            if not numeric or not np.isfinite(numeric).all():
                raise ValueError("telemetry values must be finite and non-empty")
            previous = self._last_lane_seq.get(lane)
            if previous is not None and seq != previous + 1:
                self._record_gap(
                    CaptureGap(
                        t_mono_ns=t_mono_ns,
                        lane=lane,
                        why=f"sequence gap {previous + 1}..{seq - 1}",
                        expected_seq=previous + 1,
                        observed_seq=seq,
                    )
                )
            self._last_lane_seq[lane] = seq
            self._storage.append_telemetry(TelemetrySample(lane, t_mono_ns, seq, numeric))
            return CaptureOutcome(True)
        except Exception as exc:
            gap = CaptureGap(
                t_mono_ns=max(0, int(t_mono_ns)),
                lane=lane,
                why=f"telemetry capture failure: {type(exc).__name__}: {exc}",
            )
            self._record_gap(gap)
            return CaptureOutcome(False, gap)

    def commit_event(self, event: EventInterval) -> tuple[dict[str, Any], ...]:
        try:
            return self._storage.commit_event(event, self._buffer.snapshot())
        except Exception as exc:
            self._record_gap(
                CaptureGap(
                    t_mono_ns=event.start_mono_ns,
                    lane="persistence",
                    why=f"event persistence failure: {type(exc).__name__}: {exc}",
                )
            )
            return ()

    def finalize(self, *, stride_ticks: int = 1) -> ReplayResult:
        result = replay_session(self._storage.root, stride_ticks=stride_ticks)
        self._storage.replace_windows(result.windows, result.provenance)
        return result

    def _record_gap(self, gap: CaptureGap) -> None:
        try:
            self._storage.append_gap(gap)
        except Exception:
            # Capture is never allowed to escape into the machine lane. A failed local
            # evidence store cannot honestly promise a durable gap, but it still cannot act.
            pass


@dataclass(frozen=True)
class _FrameWork:
    camera_id: str
    t_mono_ns: int
    seq: int
    frame: np.ndarray


@dataclass(frozen=True)
class _TelemetryWork:
    lane: str
    t_mono_ns: int
    seq: int
    values: tuple[float, ...]


class NonBlockingCapture:
    """Bounded, best-effort ingress; submitting from a run uses only ``put_nowait``."""

    _STOP = object()

    def __init__(self, session: CaptureSession, *, queue_capacity: int = 32) -> None:
        if queue_capacity <= 0:
            raise ValueError("capture queue capacity must be positive")
        self._session = session
        self._queue: Queue[object] = Queue(maxsize=queue_capacity)
        self._dropped = 0
        self._thread = Thread(target=self._run, name="perception-capture", daemon=True)
        self._thread.start()

    @property
    def machine_actions(self) -> tuple[object, ...]:
        return ()

    def submit_frame(self, camera_id: str, t_mono_ns: int, seq: int, frame: np.ndarray) -> bool:
        try:
            self._queue.put_nowait(_FrameWork(camera_id, t_mono_ns, seq, np.asarray(frame)))
            return True
        except Full:
            self._dropped += 1
            return False

    def submit_telemetry(
        self, lane: str, t_mono_ns: int, seq: int, values: tuple[float, ...] | list[float]
    ) -> bool:
        try:
            self._queue.put_nowait(_TelemetryWork(lane, t_mono_ns, seq, tuple(values)))
            return True
        except Full:
            self._dropped += 1
            return False

    def drain(self) -> None:
        self._queue.join()
        self._flush_queue_gap()

    def close(self) -> None:
        self.drain()
        self._queue.put(self._STOP)
        self._thread.join()

    def _run(self) -> None:
        while True:
            work = self._queue.get()
            try:
                if work is self._STOP:
                    return
                if isinstance(work, _FrameWork):
                    self._session.capture_frame(work.camera_id, work.t_mono_ns, work.seq, work.frame)
                elif isinstance(work, _TelemetryWork):
                    self._session.capture_telemetry(work.lane, work.t_mono_ns, work.seq, work.values)
            finally:
                self._queue.task_done()

    def _flush_queue_gap(self) -> None:
        if self._dropped:
            dropped, self._dropped = self._dropped, 0
            self._session._record_gap(
                CaptureGap(
                    t_mono_ns=self._session.binding.tick_origin_mono_ns,
                    lane="capture_queue",
                    why=f"capture queue overflow dropped {dropped} samples",
                )
            )
