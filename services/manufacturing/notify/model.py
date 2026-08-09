from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SEVERITIES = ("info", "warning", "critical")
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    channel: str
    attempt: int
    status: str
    seq: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "channel": self.channel,
            "attempt": self.attempt,
            "status": self.status,
            "seq": self.seq,
        }
        if self.error:
            value["error"] = self.error
        return value


@dataclass(slots=True)
class AttentionItem:
    id: str
    severity: str
    device: str
    run: str
    reason: str
    why: str
    raised_seq: int
    acknowledged_by: str | None = None
    acknowledged_seq: int | None = None
    resolved_seq: int | None = None
    occurrence_count: int = 1
    deliveries: list[DeliveryRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_RANK:
            raise ValueError(f"unsupported attention severity: {self.severity}")
        if not self.id or not self.reason or not self.why:
            raise ValueError("attention id, reason, and why must be non-empty")
        if self.raised_seq <= 0:
            raise ValueError("raised_seq must be positive")

    @property
    def open(self) -> bool:
        return self.acknowledged_seq is None and self.resolved_seq is None

    @property
    def undelivered_channels(self) -> list[str]:
        latest: dict[str, str] = {}
        for record in self.deliveries:
            latest[record.channel] = record.status
        return sorted(
            channel
            for channel, status in latest.items()
            if status in {"failed", "rate_limited"}
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "severity": self.severity,
            "device": self.device,
            "run": self.run,
            "reason": self.reason,
            "why": self.why,
            "raised_seq": self.raised_seq,
            "occurrence_count": self.occurrence_count,
            "deliveries": [record.to_dict() for record in self.deliveries],
            "undelivered_channels": self.undelivered_channels,
        }
        if self.acknowledged_by is not None:
            value["acknowledged_by"] = self.acknowledged_by
        if self.acknowledged_seq is not None:
            value["acknowledged_seq"] = self.acknowledged_seq
        if self.resolved_seq is not None:
            value["resolved_seq"] = self.resolved_seq
        return value


def more_severe(left: str, right: str) -> str:
    if left not in _SEVERITY_RANK or right not in _SEVERITY_RANK:
        raise ValueError("unsupported attention severity")
    return left if _SEVERITY_RANK[left] >= _SEVERITY_RANK[right] else right
