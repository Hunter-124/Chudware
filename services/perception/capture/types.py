from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Any, Mapping

import numpy as np


ADVISORY_SOURCE = "advisory"
TICK_HZ = 4
TICK_NS = 1_000_000_000 // TICK_HZ


@dataclass(frozen=True)
class CaptureBinding:
    """Immutable identity and lane binding for one manufacturing run."""

    run_id: str
    device_id: str
    camera_ids: tuple[str, ...]
    sensor_lanes: tuple[str, ...]
    tick_origin_mono_ns: int
    command_lane: str | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.device_id:
            raise ValueError("capture binding requires run_id and device_id")
        if not self.camera_ids or any(not value for value in self.camera_ids):
            raise ValueError("capture binding requires non-empty camera ids")
        if len(set(self.camera_ids)) != len(self.camera_ids):
            raise ValueError("camera ids must be unique")
        if any(not value for value in self.sensor_lanes):
            raise ValueError("sensor lane ids must be non-empty")
        if len(set(self.sensor_lanes)) != len(self.sensor_lanes):
            raise ValueError("sensor lane ids must be unique")
        if self.command_lane is not None and not self.command_lane:
            raise ValueError("command lane must be non-empty when supplied")
        if self.tick_origin_mono_ns < 0:
            raise ValueError("tick origin must be non-negative")


@dataclass(frozen=True)
class FrameSample:
    camera_id: str
    t_mono_ns: int
    seq: int
    frame: np.ndarray
    features: tuple[float, ...]

    @property
    def retained_bytes(self) -> int:
        # The array is made owning/contiguous at ingest, so getsizeof includes its data.
        return (
            sys.getsizeof(self)
            + sys.getsizeof(self.camera_id)
            + sys.getsizeof(self.frame)
            + sys.getsizeof(self.features)
            + sum(sys.getsizeof(value) for value in self.features)
        )


@dataclass(frozen=True)
class TelemetrySample:
    lane: str
    t_mono_ns: int
    seq: int
    values: tuple[float, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "schema": "aicad.perception_capture_telemetry/1",
            "src": ADVISORY_SOURCE,
            "lane": self.lane,
            "t_mono_ns": self.t_mono_ns,
            "seq": self.seq,
            "values": list(self.values),
        }

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> "TelemetrySample":
        return cls(
            lane=str(raw["lane"]),
            t_mono_ns=int(raw["t_mono_ns"]),
            seq=int(raw["seq"]),
            values=tuple(float(value) for value in raw["values"]),
        )


@dataclass(frozen=True)
class EventInterval:
    event_id: str
    start_mono_ns: int
    end_mono_ns: int
    kind: str = "detected_event"

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event id must be non-empty")
        if self.start_mono_ns < 0 or self.end_mono_ns < self.start_mono_ns:
            raise ValueError("event interval is invalid")


@dataclass(frozen=True)
class CaptureGap:
    t_mono_ns: int
    lane: str
    why: str
    expected_seq: int | None = None
    observed_seq: int | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "schema": "aicad.perception_capture_gap/1",
            "src": ADVISORY_SOURCE,
            "t_mono_ns": self.t_mono_ns,
            "lane": self.lane,
            "why": self.why,
            "expected_seq": self.expected_seq,
            "observed_seq": self.observed_seq,
        }


@dataclass(frozen=True)
class CaptureOutcome:
    accepted: bool
    gap: CaptureGap | None = None
    evicted_frames: int = 0
    # This is deliberately always empty: capture has no machine-action vocabulary.
    machine_actions: tuple[object, ...] = ()
