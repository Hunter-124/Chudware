"""Canonical, transport-free normalization for industrial monitor observations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import calendar
import hashlib
import ipaddress
import json
import re
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit

Quality = Literal["good", "uncertain", "bad", "unavailable"]
State = Literal["ok", "unknown"]
Origin = Literal["live", "replay_fixture", "twin_trace", "loopback_emulator"]
SourceTruth = Literal["probed", "simulated"]

_UTC_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]00:00)$"
)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class SourceSchema:
    protocol: str
    protocol_version: str
    schema_version: str
    profile: str
    namespace_uri: str
    device_uuid: str | None = None
    agent_instance_id: str | None = None
    sender: str | None = None
    node_id: str | None = None
    extension_namespaces: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EndpointContext:
    endpoint: str
    configured_label: str
    origin: Origin = "live"
    endpoint_is_physical: bool = False
    manifest_virtual: bool = False

    @property
    def src(self) -> SourceTruth:
        return derive_src(
            endpoint=self.endpoint,
            origin=self.origin,
            endpoint_is_physical=self.endpoint_is_physical,
            manifest_virtual=self.manifest_virtual,
        )

    @property
    def source_id(self) -> str:
        payload = f"mtconnect|{self.endpoint}|{self.configured_label}"
        return "src_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class DataItemDefinition:
    item_key: str
    source_item_id: str
    device_key: str
    canonical_kind: str
    value_kind: str
    unit: str | None
    unit_src: str | None
    sub_type: str | None
    component: str | None
    schema: SourceSchema
    raw_element: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeviceDefinition:
    device_key: str
    name: str
    uuid: str | None
    schema: SourceSchema
    data_items: tuple[DataItemDefinition, ...]
    raw_element: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ObservationRow:
    obs_id: str
    source_id: str
    device_key: str
    item_key: str
    source_item_id: str
    canonical_kind: str
    value_kind: Literal["sample", "event", "asset"]
    v_num: float | None
    v_text: str | None
    v_json: Any | None
    unit: str | None
    unit_src: str | None
    converted_from: Mapping[str, Any] | None
    t_src_utc_ns: int | None
    t_server_utc_ns: int | None
    src_seq: int | None
    seq: None
    quality: Quality
    quality_src: str
    state: State
    why: str | None
    schema: SourceSchema
    raw: Mapping[str, Any]
    raw_element: str
    src: SourceTruth
    origin: Origin
    evidence_eligible: bool
    gap: bool = False
    last_ok: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return _stable_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class ConditionRow:
    cond_id: str
    source_id: str
    device_key: str
    item_key: str
    source_item_id: str
    severity: Literal["normal", "warning", "fault", "unavailable"]
    native_code: str | None
    native_severity: str | None
    qualifier: str | None
    text: str | None
    t_src_utc_ns: int | None
    src_seq: int | None
    seq: None
    quality: Quality
    quality_src: str
    state: State
    why: str | None
    transition: Literal["raised", "changed", "cleared", "unavailable"]
    schema: SourceSchema
    raw: Mapping[str, Any]
    raw_element: str
    src: SourceTruth
    origin: Origin
    gap: bool = False
    last_ok: Mapping[str, Any] | None = None
    epoch_policy: Literal["falsify"] = "falsify"
    retain_as_last_ok: Literal[True] = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return _stable_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class SequenceHeader:
    instance_id: str
    first_sequence: int | None
    last_sequence: int | None
    next_sequence: int | None


@dataclass(frozen=True, slots=True)
class GapNotice:
    source_id: str
    reason: Literal["buffer_roll", "instance_restart", "sequence_regression"]
    why: str
    expected_next_sequence: int | None
    observed_first_sequence: int | None
    previous_instance_id: str | None
    instance_id: str
    invalidation_class: Literal["falsified"] = "falsified"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SequenceTracker:
    """Tracks protocol continuity only; it never creates a durable journal sequence."""

    def __init__(self) -> None:
        self._state: dict[str, tuple[str, int | None]] = {}

    def check(self, source_id: str, header: SequenceHeader) -> GapNotice | None:
        previous = self._state.get(source_id)
        self._state[source_id] = (header.instance_id, header.next_sequence)
        if previous is None:
            return None
        previous_instance, expected = previous
        if previous_instance != header.instance_id:
            return GapNotice(
                source_id,
                "instance_restart",
                f"agent instance changed from {previous_instance} to {header.instance_id}",
                expected,
                header.first_sequence,
                previous_instance,
                header.instance_id,
            )
        if expected is not None and header.first_sequence is not None and header.first_sequence > expected:
            return GapNotice(
                source_id,
                "buffer_roll",
                f"source buffer starts at {header.first_sequence}; expected {expected}",
                expected,
                header.first_sequence,
                previous_instance,
                header.instance_id,
            )
        if expected is not None and header.next_sequence is not None and header.next_sequence < expected:
            return GapNotice(
                source_id,
                "sequence_regression",
                f"nextSequence regressed from {expected} to {header.next_sequence}",
                expected,
                header.first_sequence,
                previous_instance,
                header.instance_id,
            )
        return None


def parse_utc_ns(value: str | None) -> int | None:
    """Parse an explicitly UTC ISO-8601 timestamp without substituting a local clock."""

    if value is None:
        return None
    match = _UTC_TIMESTAMP.fullmatch(value)
    if match is None:
        return None
    date_part, time_part, fraction, _zone = match.groups()
    try:
        parsed = datetime.fromisoformat(f"{date_part}T{time_part}").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    seconds = calendar.timegm(parsed.utctimetuple())
    nanos = int((fraction or "").ljust(9, "0")) if fraction else 0
    return seconds * 1_000_000_000 + nanos


def _is_loopback(endpoint: str) -> bool:
    parsed = urlsplit(endpoint if "://" in endpoint else f"//{endpoint}")
    host = parsed.hostname
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def derive_src(
    *, endpoint: str, origin: Origin, endpoint_is_physical: bool, manifest_virtual: bool = False
) -> SourceTruth:
    """Derive provenance from endpoint physicality; replay and loopback always simulate."""

    if origin != "live" or manifest_virtual or _is_loopback(endpoint):
        return "simulated"
    return "probed" if endpoint_is_physical else "simulated"


def device_key(source_id: str, uuid_or_name: str) -> str:
    payload = f"{source_id}|{uuid_or_name}"
    return "dev_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def item_key(device: str, stable_item_id: str) -> str:
    payload = f"{device}|{stable_item_id}"
    return "itm_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def value_snapshot(row: ObservationRow) -> dict[str, Any]:
    return {
        "v_num": row.v_num,
        "v_text": row.v_text,
        "v_json": row.v_json,
        "t_src_utc_ns": row.t_src_utc_ns,
        "src_seq": row.src_seq,
    }


def condition_snapshot(row: ConditionRow) -> dict[str, Any]:
    return {
        "severity": row.severity,
        "text": row.text,
        "t_src_utc_ns": row.t_src_utc_ns,
        "src_seq": row.src_seq,
    }


def observations_unknown_on_gap(
    rows: Sequence[ObservationRow], gap: GapNotice, last_good: Mapping[str, Mapping[str, Any]] | None = None
) -> tuple[ObservationRow, ...]:
    previous = last_good or {}
    return tuple(
        replace(
            row,
            v_num=None,
            v_text=None,
            v_json=None,
            quality="unavailable",
            quality_src="SEQUENCE_GAP",
            state="unknown",
            why=gap.why,
            evidence_eligible=False,
            gap=True,
            last_ok=previous.get(row.item_key) or (value_snapshot(row) if row.state == "ok" else row.last_ok),
        )
        for row in rows
    )


def conditions_unknown_on_gap(
    rows: Sequence[ConditionRow], gap: GapNotice, last_good: Mapping[str, Mapping[str, Any]] | None = None
) -> tuple[ConditionRow, ...]:
    previous = last_good or {}
    return tuple(
        replace(
            row,
            severity="unavailable",
            quality="unavailable",
            quality_src="SEQUENCE_GAP",
            state="unknown",
            why=gap.why,
            transition="unavailable",
            gap=True,
            last_ok=previous.get(row.item_key) or (condition_snapshot(row) if row.state == "ok" else row.last_ok),
        )
        for row in rows
    )
