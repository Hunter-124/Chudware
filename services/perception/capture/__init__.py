"""Event-centric, advisory-only perception capture and deterministic replay."""

from .alignment import AlignedSample, align_lane
from .buffer import BufferAppendResult, RollingFrameBuffer
from .features import FrameFeatureExtractor
from .redaction import PassThroughRedactor, Redactor
from .replay import ReplayResult, replay_session
from .session import CaptureSession, NonBlockingCapture
from .types import (
    ADVISORY_SOURCE,
    TICK_HZ,
    TICK_NS,
    CaptureBinding,
    CaptureGap,
    CaptureOutcome,
    EventInterval,
    FrameSample,
    TelemetrySample,
)

__all__ = [
    "ADVISORY_SOURCE",
    "TICK_HZ",
    "TICK_NS",
    "AlignedSample",
    "BufferAppendResult",
    "CaptureBinding",
    "CaptureGap",
    "CaptureOutcome",
    "CaptureSession",
    "EventInterval",
    "FrameFeatureExtractor",
    "FrameSample",
    "NonBlockingCapture",
    "PassThroughRedactor",
    "Redactor",
    "ReplayResult",
    "RollingFrameBuffer",
    "TelemetrySample",
    "align_lane",
    "replay_session",
]
