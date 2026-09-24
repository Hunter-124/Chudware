"""Typed OPC UA Machine Tools companion-profile subset to canonical monitor rows.

This is intentionally a closed mapping table.  Unknown browse paths are not
best-effort guessed; extending coverage requires adding a reviewed shape here.
Raw node values are retained even when quality or timestamp rules make the
canonical current value unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
from typing import Any, Mapping
from uuid import UUID

from ..normalize import (
    ConditionRow,
    EndpointContext,
    ObservationRow,
    SourceSchema,
    device_key,
    item_key,
)
from .codec import BOOLEAN, NULL, DataValue, NodeId, Variant

MACHINE_TOOLS_NAMESPACE = "http://opcfoundation.org/UA/MachineTool/"
PROFILE_NAME = "OPC UA for Machine Tools 1.01 closed read-only subset"


@dataclass(frozen=True, slots=True)
class MachineToolShape:
    browse_path: str
    canonical_kind: str
    value_kind: str
    unit: str | None = None
    unit_src: str | None = None
    condition: bool = False


# OPC 40501-1 shapes supported by this adapter.  Browse paths, not namespace
# indexes or vendor-assigned numeric NodeIds, are the stable identities.
SUPPORTED_MACHINE_TOOL_SHAPES = (
    MachineToolShape(
        "/MachineTool/Monitoring/MachineOperationMode",
        "controller_mode",
        "event",
    ),
    MachineToolShape(
        "/MachineTool/Monitoring/ExecutionState",
        "execution_state",
        "event",
    ),
    MachineToolShape(
        "/MachineTool/Monitoring/Spindle/ActualSpeed",
        "spindle_speed",
        "sample",
        "rpm",
        "REVOLUTION/MINUTE",
    ),
    MachineToolShape(
        "/MachineTool/Monitoring/PartCount",
        "part_count",
        "sample",
        "count",
        "COUNT",
    ),
    MachineToolShape(
        "/MachineTool/Monitoring/DoorState",
        "door_state",
        "event",
    ),
    MachineToolShape(
        "/MachineTool/Monitoring/PowerState",
        "power",
        "event",
    ),
    MachineToolShape(
        "/MachineTool/Monitoring/EmergencyStop",
        "emergency_stop_state",
        "condition",
        condition=True,
    ),
)


@dataclass(frozen=True, slots=True)
class NodeReading:
    node_id: NodeId
    data_value: DataValue


@dataclass(frozen=True, slots=True)
class MachineToolRows:
    observations: tuple[ObservationRow, ...]
    conditions: tuple[ConditionRow, ...]


_STATUS_REASONS = {
    0x80340000: "node is absent from the server address space (BadNodeIdUnknown)",
    0x803A0000: "node does not permit reading (BadNotReadable)",
    0x808D0000: "node is out of service (BadOutOfService)",
    0x80320000: "server is waiting for the initial value (BadWaitingForInitialData)",
    0x801F0000: "anonymous identity is not permitted to read this node (BadUserAccessDenied)",
    0x40920000: "server reported an initial uncertain value (UncertainInitialValue)",
    0x40900000: "server reported the last usable value (UncertainLastUsableValue)",
    0x40910000: "server reported a substituted value (UncertainSubstituteValue)",
}


def _source_id(context: EndpointContext) -> str:
    payload = f"opcua|{context.endpoint}|{context.configured_label}"
    return "src_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _timestamp_ns(value: datetime | None) -> int | None:
    if value is None or value.tzinfo is None:
        return None
    utc = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (UUID, NodeId)):
        return str(value)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if is_dataclass(value):
        return {
            descriptor.name: _json_value(getattr(value, descriptor.name))
            for descriptor in fields(value)
        }
    return repr(value)


def _variant_raw(value: Variant | None) -> Any:
    if value is None:
        return None
    return {
        "builtin_type": value.type_id,
        "is_array": value.is_array,
        "dimensions": list(value.dimensions) if value.dimensions is not None else None,
        "value": _json_value(value.value),
    }


def _quality(status: int | None) -> tuple[str, str, str | None]:
    actual = 0 if status is None else status
    source = f"0x{actual:08X}" + (" (implicit Good)" if status is None else "")
    severity = actual & 0xC0000000
    if severity == 0:
        return "good", source, None
    why = _STATUS_REASONS.get(actual)
    if severity == 0x40000000:
        return "uncertain", source, why or f"server returned uncertain StatusCode 0x{actual:08X}"
    return "bad", source, why or f"server returned bad StatusCode 0x{actual:08X}"


def _raw(
    shape: MachineToolShape,
    node_id: NodeId | None,
    data_value: DataValue | None,
) -> tuple[dict[str, Any], str]:
    raw = {
        "browse_path": shape.browse_path,
        "node_id": str(node_id) if node_id is not None else None,
        "status_code": (
            f"0x{data_value.status:08X}"
            if data_value is not None and data_value.status is not None
            else None
        ),
        "value": _variant_raw(data_value.value) if data_value is not None else None,
        "source_timestamp": _json_value(data_value.source_timestamp) if data_value else None,
        "server_timestamp": _json_value(data_value.server_timestamp) if data_value else None,
    }
    return raw, json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _current_value(data_value: DataValue | None, usable: bool) -> tuple[Any, Any, Any]:
    if not usable or data_value is None or data_value.value is None:
        return None, None, None
    value = data_value.value.value
    if data_value.value.is_array:
        return None, None, _json_value(value)
    if isinstance(value, bool):
        return None, "true" if value else "false", None
    if isinstance(value, (int, float)):
        return float(value), None, None
    if isinstance(value, str):
        return None, value, None
    return None, None, _json_value(value)


def map_machine_tools(
    readings: Mapping[str, NodeReading | DataValue | None],
    context: EndpointContext,
    *,
    device_name: str,
    device_uuid: str | None = None,
    shapes: tuple[MachineToolShape, ...] = SUPPORTED_MACHINE_TOOL_SHAPES,
) -> MachineToolRows:
    """Map the closed profile subset; every declared shape produces one row.

    Missing entries deliberately produce unknown rows.  Extra paths are ignored
    rather than heuristically mapped.
    """

    source_id = _source_id(context)
    canonical_device = device_key(source_id, device_uuid or device_name)
    observations: list[ObservationRow] = []
    conditions: list[ConditionRow] = []

    for shape in shapes:
        supplied = readings.get(shape.browse_path)
        node_id: NodeId | None = None
        data_value: DataValue | None
        if isinstance(supplied, NodeReading):
            node_id = supplied.node_id
            data_value = supplied.data_value
        else:
            data_value = supplied
        canonical_item = item_key(canonical_device, shape.browse_path)
        schema = SourceSchema(
            protocol="opcua",
            protocol_version="1.04",
            schema_version="OPC 40501-1:1.01",
            profile=PROFILE_NAME,
            namespace_uri=MACHINE_TOOLS_NAMESPACE,
            device_uuid=device_uuid,
            node_id=str(node_id) if node_id is not None else None,
        )
        raw, raw_element = _raw(shape, node_id, data_value)
        if data_value is None:
            quality, quality_src, why = (
                "unavailable",
                "ABSENT_NODE",
                "typed profile node is absent",
            )
            source_ns = server_ns = None
        else:
            quality, quality_src, why = _quality(data_value.status)
            source_ns = _timestamp_ns(data_value.source_timestamp)
            server_ns = _timestamp_ns(data_value.server_timestamp)
            if source_ns is None and quality == "good":
                quality = "unavailable"
                quality_src = "MISSING_SOURCE_TIMESTAMP"
                why = "missing source timestamp"
        usable = quality == "good" and source_ns is not None
        if usable:
            variant = data_value.value
            if variant is None or variant.type_id == NULL or variant.value is None:
                quality = "unavailable"
                quality_src = "MISSING_TYPED_VALUE"
                why = "missing typed value"
                usable = False
            elif shape.condition and (
                variant.type_id != BOOLEAN or variant.is_array or type(variant.value) is not bool
            ):
                quality = "unavailable"
                quality_src = "INVALID_EMERGENCY_STOP_TYPE"
                why = "EmergencyStop requires a Boolean scalar"
                usable = False

        if shape.condition:
            active = data_value.value.value if usable else None
            if active is None:
                severity = "unavailable"
                transition = "unavailable"
                state = "unknown"
            elif active:
                severity = "fault"
                transition = "raised"
                quality = "bad"
                state = "unknown"
                why = "source reported active emergency-stop condition"
            else:
                severity = "normal"
                transition = "cleared"
                state = "ok"
            conditions.append(
                ConditionRow(
                    cond_id=f"{source_id}:{canonical_device}:{canonical_item}:{source_ns}",
                    source_id=source_id,
                    device_key=canonical_device,
                    item_key=canonical_item,
                    source_item_id=shape.browse_path,
                    severity=severity,
                    native_code=quality_src,
                    native_severity=severity,
                    qualifier=None,
                    text=(
                        "Emergency stop active"
                        if active is True
                        else "Emergency stop clear" if active is False else None
                    ),
                    t_src_utc_ns=source_ns,
                    src_seq=None,
                    seq=None,
                    quality=quality,
                    quality_src=quality_src,
                    state=state,
                    why=why,
                    transition=transition,
                    schema=schema,
                    raw=raw,
                    raw_element=raw_element,
                    src=context.src,
                    origin=context.origin,
                )
            )
            continue

        v_num, v_text, v_json = _current_value(data_value, usable)
        observations.append(
            ObservationRow(
                obs_id=f"{source_id}:{canonical_device}:{canonical_item}:{source_ns}",
                source_id=source_id,
                device_key=canonical_device,
                item_key=canonical_item,
                source_item_id=shape.browse_path,
                canonical_kind=shape.canonical_kind,
                value_kind=shape.value_kind,  # type: ignore[arg-type]
                v_num=v_num,
                v_text=v_text,
                v_json=v_json,
                unit=shape.unit,
                unit_src=shape.unit_src,
                converted_from=None,
                t_src_utc_ns=source_ns,
                t_server_utc_ns=server_ns,
                src_seq=None,
                seq=None,
                quality=quality,
                quality_src=quality_src,
                state="ok" if usable else "unknown",
                why=why,
                schema=schema,
                raw=raw,
                raw_element=raw_element,
                src=context.src,
                origin=context.origin,
                evidence_eligible=context.src == "probed" and usable,
            )
        )
    return MachineToolRows(tuple(observations), tuple(conditions))


__all__ = (
    "MACHINE_TOOLS_NAMESPACE",
    "MachineToolRows",
    "MachineToolShape",
    "NodeReading",
    "PROFILE_NAME",
    "SUPPORTED_MACHINE_TOOL_SHAPES",
    "map_machine_tools",
)
