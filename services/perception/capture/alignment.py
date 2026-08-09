from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .types import TICK_NS, TelemetrySample


@dataclass(frozen=True)
class AlignedSample:
    t_mono_ns: int
    values: tuple[float, ...]
    mask: tuple[int, ...]
    age_ticks: tuple[int, ...]
    gap: bool
    why: str | None
    source_seq: int | None


def align_lane(
    samples: Sequence[TelemetrySample],
    ticks_mono_ns: Sequence[int],
    *,
    width: int,
    max_gap_ns: int = TICK_NS,
) -> tuple[AlignedSample, ...]:
    """Align one lane without interpolation; every discontinuity becomes an explicit gap."""

    if width <= 0 or max_gap_ns <= 0:
        raise ValueError("alignment width and max gap must be positive")
    ordered = sorted(samples, key=lambda item: (item.t_mono_ns, item.seq))
    if any(item.lane != ordered[0].lane for item in ordered[1:]):
        raise ValueError("align_lane accepts exactly one lane")
    if any(right.t_mono_ns <= left.t_mono_ns for left, right in zip(ordered, ordered[1:])):
        raise ValueError("lane samples require strictly increasing timestamps")

    result: list[AlignedSample] = []
    cursor = -1
    for tick in ticks_mono_ns:
        while cursor + 1 < len(ordered) and ordered[cursor + 1].t_mono_ns <= tick:
            cursor += 1
        previous = ordered[cursor] if cursor >= 0 else None
        following = ordered[cursor + 1] if cursor + 1 < len(ordered) else None

        reason: str | None = None
        if previous is None:
            reason = "no sample at or before tick"
        elif tick - previous.t_mono_ns > max_gap_ns:
            reason = f"telemetry age gap {tick - previous.t_mono_ns}ns"
        elif following is not None and previous.t_mono_ns < tick < following.t_mono_ns:
            if following.t_mono_ns - previous.t_mono_ns > max_gap_ns:
                reason = (
                    f"monotonic time gap {previous.t_mono_ns}..{following.t_mono_ns}"
                )
            elif following.seq != previous.seq + 1:
                reason = f"sequence gap {previous.seq + 1}..{following.seq - 1}"
        elif cursor > 0:
            prior = ordered[cursor - 1]
            if previous.seq != prior.seq + 1:
                reason = f"sequence gap {prior.seq + 1}..{previous.seq - 1}"

        if reason is not None:
            result.append(
                AlignedSample(
                    t_mono_ns=tick,
                    values=(0.0,) * width,
                    mask=(0,) * width,
                    age_ticks=(0,) * width,
                    gap=True,
                    why=reason,
                    source_seq=None if previous is None else previous.seq,
                )
            )
            continue

        assert previous is not None
        values = tuple(previous.values[:width])
        actual = len(values)
        if actual < width:
            values += (0.0,) * (width - actual)
        mask = (1,) * actual + (0,) * (width - actual)
        age = max(0, (tick - previous.t_mono_ns + TICK_NS - 1) // TICK_NS)
        age_ticks = (int(age),) * actual + (0,) * (width - actual)
        result.append(
            AlignedSample(
                t_mono_ns=tick,
                values=values,
                mask=mask,
                age_ticks=age_ticks,
                gap=False,
                why=None,
                source_seq=previous.seq,
            )
        )
    return tuple(result)
