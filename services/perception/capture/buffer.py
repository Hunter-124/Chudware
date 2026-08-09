from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

from .types import FrameSample


@dataclass(frozen=True)
class BufferAppendResult:
    accepted: bool
    evicted: int
    reason: str | None = None


class RollingFrameBuffer:
    """Time-bounded frame ring with deterministic retained-memory accounting."""

    def __init__(self, *, memory_ceiling_bytes: int, horizon_ns: int) -> None:
        if memory_ceiling_bytes <= 0:
            raise ValueError("memory ceiling must be positive")
        if horizon_ns <= 0:
            raise ValueError("buffer horizon must be positive")
        self._ceiling = int(memory_ceiling_bytes)
        self._horizon_ns = int(horizon_ns)
        self._frames: deque[FrameSample] = deque()
        self._retained_bytes = 0

    @property
    def memory_ceiling_bytes(self) -> int:
        return self._ceiling

    @property
    def retained_bytes(self) -> int:
        return self._retained_bytes

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    def append(self, sample: FrameSample) -> BufferAppendResult:
        if self._frames and sample.t_mono_ns < self._frames[-1].t_mono_ns:
            return BufferAppendResult(False, 0, "out-of-order frame timestamp")
        size = sample.retained_bytes
        if size > self._ceiling:
            return BufferAppendResult(False, 0, "frame exceeds memory ceiling")

        evicted = 0
        oldest_allowed = sample.t_mono_ns - self._horizon_ns
        while self._frames and self._frames[0].t_mono_ns < oldest_allowed:
            self._remove_oldest()
            evicted += 1
        while self._frames and self._retained_bytes + size > self._ceiling:
            self._remove_oldest()
            evicted += 1

        self._frames.append(sample)
        self._retained_bytes += size
        assert self._retained_bytes <= self._ceiling
        return BufferAppendResult(True, evicted)

    def select(self, start_mono_ns: int, end_mono_ns: int) -> tuple[FrameSample, ...]:
        if end_mono_ns < start_mono_ns:
            raise ValueError("selection interval is invalid")
        return tuple(
            sample
            for sample in self._frames
            if start_mono_ns <= sample.t_mono_ns <= end_mono_ns
        )

    def snapshot(self) -> tuple[FrameSample, ...]:
        return tuple(self._frames)

    def extend(self, samples: Iterable[FrameSample]) -> int:
        accepted = 0
        for sample in samples:
            accepted += int(self.append(sample).accepted)
        return accepted

    def _remove_oldest(self) -> None:
        removed = self._frames.popleft()
        self._retained_bytes -= removed.retained_bytes
