"""Pure stdlib MTConnect Probe/Current/Sample XML decoding.

The module accepts bytes already obtained by a read-only transport.  It owns no
network client and exposes no machine command surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import re
from typing import Any, Iterable, Mapping
import xml.etree.ElementTree as ET

from .normalize import (
    ConditionRow,
    DataItemDefinition,
    DeviceDefinition,
    EndpointContext,
    ObservationRow,
    SequenceHeader,
    SourceSchema,
    device_key,
    item_key,
    parse_utc_ns,
)

MAX_XML_BYTES = 4 * 1024 * 1024
MAX_ELEMENTS = 100_000
SUPPORTED_VERSIONS = frozenset({"1.7", "1.8", "2.0", "2.1", "2.2", "2.3"})
_VERSION_AT_END = re.compile(r"(?:[:_/])(\d+\.\d+)(?:\.xsd)?$")


class MonitoringRefusal(ValueError):
    """Base typed refusal carrying a stable code and operator-readable reason."""

    code = "monitor_refused"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason}


class MalformedDocumentRefusal(MonitoringRefusal):
    code = "malformed_document"


class UnsupportedVersionRefusal(MonitoringRefusal):
    code = "unsupported_version"


class AgentDocumentRefusal(MonitoringRefusal):
    code = "agent_error_document"


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    document_kind: str
    schema: SourceSchema
    header: SequenceHeader | None
    devices: tuple[DeviceDefinition, ...] = ()
    data_items: tuple[DataItemDefinition, ...] = ()
    observations: tuple[ObservationRow, ...] = ()
    conditions: tuple[ConditionRow, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def data_item_map(self) -> dict[str, DataItemDefinition]:
        return {item.source_item_id: item for item in self.data_items}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") and "}" in tag else ""


def _xml_text(payload: bytes | str) -> str:
    if isinstance(payload, bytes):
        if len(payload) > MAX_XML_BYTES:
            raise MalformedDocumentRefusal(f"document exceeds {MAX_XML_BYTES} byte limit")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedDocumentRefusal(f"document is not UTF-8: {exc}") from exc
    else:
        text = payload
        if len(text.encode("utf-8")) > MAX_XML_BYTES:
            raise MalformedDocumentRefusal(f"document exceeds {MAX_XML_BYTES} byte limit")
    folded = text.casefold()
    if "<!doctype" in folded or "<!entity" in folded:
        raise MalformedDocumentRefusal("DTD and entity declarations are forbidden")
    return text


def _decode(payload: bytes | str) -> tuple[ET.Element, str, str, tuple[str, ...]]:
    text = _xml_text(payload)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise MalformedDocumentRefusal(f"XML parse error: {exc}") from exc
    elements = tuple(root.iter())
    if len(elements) > MAX_ELEMENTS:
        raise MalformedDocumentRefusal(f"document exceeds {MAX_ELEMENTS} element limit")
    namespace_uri = _namespace(root.tag)
    match = _VERSION_AT_END.search(namespace_uri)
    if match is None:
        raise UnsupportedVersionRefusal(
            f"MTConnect namespace does not declare a supported version: {namespace_uri or '<none>'}"
        )
    version = match.group(1)
    if version not in SUPPORTED_VERSIONS:
        raise UnsupportedVersionRefusal(
            f"MTConnect version {version} is unsupported; supported versions are {', '.join(sorted(SUPPORTED_VERSIONS))}"
        )
    extension_namespaces = tuple(
        sorted(
            {
                candidate
                for element in elements
                for candidate in (
                    _namespace(element.tag),
                    *(_namespace(attribute) for attribute in element.attrib),
                )
                if candidate not in {"", namespace_uri}
            }
        )
    )
    return root, namespace_uri, version, extension_namespaces


def _find_child(root: ET.Element, local_name: str) -> ET.Element | None:
    return next((element for element in root if _local(element.tag) == local_name), None)


def _header(root: ET.Element) -> ET.Element:
    header = _find_child(root, "Header")
    if header is None:
        raise MalformedDocumentRefusal("MTConnect document has no Header")
    return header


def _nonnegative_int(value: str | None, field: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise MalformedDocumentRefusal(f"Header@{field} is not an integer: {value!r}") from exc
    if parsed < 0:
        raise MalformedDocumentRefusal(f"Header@{field} must be non-negative")
    return parsed


def _schema(
    *,
    namespace_uri: str,
    version: str,
    extension_namespaces: tuple[str, ...],
    header: ET.Element,
    profile: str,
    node_id: str | None = None,
    device_uuid: str | None = None,
) -> SourceSchema:
    return SourceSchema(
        protocol="mtconnect",
        protocol_version=version,
        schema_version=version,
        profile=profile,
        namespace_uri=namespace_uri,
        device_uuid=device_uuid,
        agent_instance_id=header.attrib.get("instanceId"),
        sender=header.attrib.get("sender"),
        node_id=node_id,
        extension_namespaces=extension_namespaces,
    )


def _sequence_header(header: ET.Element) -> SequenceHeader:
    instance_id = header.attrib.get("instanceId")
    if not instance_id:
        raise MalformedDocumentRefusal("Header@instanceId is required")
    return SequenceHeader(
        instance_id=instance_id,
        first_sequence=_nonnegative_int(header.attrib.get("firstSequence"), "firstSequence"),
        last_sequence=_nonnegative_int(header.attrib.get("lastSequence"), "lastSequence"),
        next_sequence=_nonnegative_int(header.attrib.get("nextSequence"), "nextSequence"),
    )


def _raw(element: ET.Element) -> tuple[dict[str, Any], str]:
    raw_element = ET.tostring(element, encoding="unicode", short_empty_elements=True)
    return (
        {
            "tag": element.tag,
            "local_name": _local(element.tag),
            "attributes": dict(sorted(element.attrib.items())),
            "text": element.text or "",
        },
        raw_element,
    )


def _canonical_kind(item_type: str, component: str | None) -> str:
    token = item_type.replace("_", "").replace("-", "").casefold()
    component_token = (component or "").casefold()
    if token == "position":
        return "axis_position"
    if token in {"pathfeedrate", "feedrate"}:
        return "axis_feedrate"
    if token == "load":
        return "spindle_load" if "rotary" in component_token or "spindle" in component_token else "axis_load"
    if token in {"rotaryvelocity", "spindlespeed"}:
        return "spindle_speed"
    mapping = {
        "execution": "execution_state",
        "controllermode": "controller_mode",
        "program": "program_name",
        "block": "program_block",
        "programcomment": "program_comment",
        "toolnumber": "tool_id",
        "toollife": "tool_life",
        "partcount": "part_count",
        "temperature": "temperature",
        "powerstate": "power",
        "power": "power",
        "availability": "availability",
        "doorstate": "door_state",
        "coolant": "coolant_state",
        "emergencystop": "emergency_stop_state",
        "alarm": "alarm",
        "assetchanged": "asset",
        "assetremoved": "asset",
    }
    return mapping.get(token, "other")


def _unit(unit_src: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if unit_src is None:
        return None, None
    token = unit_src.strip().upper()
    direct = {
        "MILLIMETER": "mm",
        "MILLIMETERS": "mm",
        "MILLIMETER/SECOND": "mm/s",
        "MILLIMETER/SECOND^2": "mm/s^2",
        "DEGREE": "deg",
        "REVOLUTION/MINUTE": "rpm",
        "PERCENT": "%",
        "CELSIUS": "degC",
        "WATT": "W",
        "COUNT": "count",
    }
    if token in direct:
        return direct[token], None
    if token in {"INCH", "INCHES"}:
        return "mm", {"unit": unit_src, "factor": 25.4, "offset": 0.0}
    return None, None


def _value_kind(category: str | None) -> str:
    token = (category or "EVENT").casefold()
    return token if token in {"sample", "event", "condition", "asset"} else "event"


def parse_probe(payload: bytes | str, context: EndpointContext) -> ParsedDocument:
    root, namespace_uri, version, extensions = _decode(payload)
    if _local(root.tag) != "MTConnectDevices":
        raise MalformedDocumentRefusal(f"expected MTConnectDevices, got {_local(root.tag)}")
    header = _header(root)
    base_schema = _schema(
        namespace_uri=namespace_uri,
        version=version,
        extension_namespaces=extensions,
        header=header,
        profile=f"MTConnect Devices {version}",
    )
    devices_parent = next((element for element in root.iter() if _local(element.tag) == "Devices"), None)
    if devices_parent is None:
        raise MalformedDocumentRefusal("Probe document has no Devices element")

    devices: list[DeviceDefinition] = []
    all_items: list[DataItemDefinition] = []
    for device in (element for element in devices_parent if _local(element.tag) == "Device"):
        name = device.attrib.get("name") or device.attrib.get("id")
        if not name:
            raise MalformedDocumentRefusal("Probe Device requires name or id")
        uuid = device.attrib.get("uuid")
        canonical_device = device_key(context.source_id, uuid or name)
        device_schema = replace(base_schema, device_uuid=uuid)
        items: list[DataItemDefinition] = []

        def collect(element: ET.Element, component: str | None = None) -> None:
            local = _local(element.tag)
            next_component = component
            if local not in {"DataItems", "DataItem", "Components", "Device"}:
                next_component = element.attrib.get("name") or element.attrib.get("id") or local
            if local == "DataItem":
                source_item_id = element.attrib.get("id") or element.attrib.get("name")
                if not source_item_id:
                    raise MalformedDocumentRefusal("Probe DataItem requires id or name")
                item_type = element.attrib.get("type") or "OTHER"
                unit_src = element.attrib.get("nativeUnits") or element.attrib.get("units")
                canonical_unit, _conversion = _unit(unit_src)
                _raw_dict, raw_element = _raw(element)
                item_schema = replace(device_schema, node_id=source_item_id)
                items.append(
                    DataItemDefinition(
                        item_key=item_key(canonical_device, source_item_id),
                        source_item_id=source_item_id,
                        device_key=canonical_device,
                        canonical_kind=_canonical_kind(item_type, component),
                        value_kind=_value_kind(element.attrib.get("category")),
                        unit=canonical_unit,
                        unit_src=unit_src,
                        sub_type=element.attrib.get("subType"),
                        component=component,
                        schema=item_schema,
                        raw_element=raw_element,
                    )
                )
            for child in element:
                collect(child, next_component)

        collect(device)
        _raw_dict, device_raw = _raw(device)
        item_tuple = tuple(items)
        all_items.extend(item_tuple)
        devices.append(DeviceDefinition(canonical_device, name, uuid, device_schema, item_tuple, device_raw))
    return ParsedDocument("probe", base_schema, _sequence_header(header), tuple(devices), tuple(all_items))


def _definition_map(
    definitions: Mapping[str, DataItemDefinition] | Iterable[DataItemDefinition] | ParsedDocument | None,
) -> dict[str, DataItemDefinition]:
    if definitions is None:
        return {}
    if isinstance(definitions, ParsedDocument):
        return definitions.data_item_map()
    if isinstance(definitions, Mapping):
        return dict(definitions)
    return {item.source_item_id: item for item in definitions}


def _condition_severity(local_name: str) -> tuple[str, str, str, str | None]:
    token = local_name.casefold()
    if token == "normal":
        return "normal", "good", "cleared", None
    if token == "warning":
        return "warning", "uncertain", "raised", "source reported warning condition"
    if token == "fault":
        return "fault", "bad", "raised", "source reported fault condition"
    return "unavailable", "unavailable", "unavailable", "source reported unavailable condition"


def _convert_number(text: str, conversion: Mapping[str, Any] | None) -> float | None:
    try:
        number = float(text)
    except ValueError:
        return None
    if conversion is not None:
        number = number * float(conversion["factor"]) + float(conversion["offset"])
    return number


def parse_streams(
    payload: bytes | str,
    context: EndpointContext,
    definitions: Mapping[str, DataItemDefinition] | Iterable[DataItemDefinition] | ParsedDocument | None = None,
    *,
    document_kind: str = "current",
) -> ParsedDocument:
    root, namespace_uri, version, extensions = _decode(payload)
    root_kind = _local(root.tag)
    if root_kind == "MTConnectError":
        errors = [
            f"{element.attrib.get('errorCode', 'UNKNOWN')}: {(element.text or '').strip()}"
            for element in root.iter()
            if _local(element.tag) == "Error"
        ]
        raise AgentDocumentRefusal("; ".join(errors) or "MTConnect agent returned an error document")
    if root_kind != "MTConnectStreams":
        raise MalformedDocumentRefusal(f"expected MTConnectStreams, got {root_kind}")
    header_element = _header(root)
    header = _sequence_header(header_element)
    base_schema = _schema(
        namespace_uri=namespace_uri,
        version=version,
        extension_namespaces=extensions,
        header=header_element,
        profile=f"MTConnect Streams {version}",
    )
    item_definitions = _definition_map(definitions)
    observations: list[ObservationRow] = []
    conditions: list[ConditionRow] = []

    for device_stream in (element for element in root.iter() if _local(element.tag) == "DeviceStream"):
        fallback_name = device_stream.attrib.get("uuid") or device_stream.attrib.get("name") or "unknown-device"
        fallback_device = device_key(context.source_id, fallback_name)
        for component_stream in (element for element in device_stream if _local(element.tag) == "ComponentStream"):
            component = component_stream.attrib.get("component") or component_stream.attrib.get("name")
            for group in component_stream:
                group_name = _local(group.tag)
                if group_name not in {"Samples", "Events", "Condition"}:
                    continue
                for element in group:
                    local_name = _local(element.tag)
                    source_item_id = element.attrib.get("dataItemId") or element.attrib.get("name")
                    if not source_item_id:
                        raise MalformedDocumentRefusal(f"{local_name} stream element requires dataItemId or name")
                    definition = item_definitions.get(source_item_id)
                    canonical_device = definition.device_key if definition else fallback_device
                    canonical_item = definition.item_key if definition else item_key(canonical_device, source_item_id)
                    schema = replace(
                        definition.schema if definition else base_schema,
                        agent_instance_id=header.instance_id,
                        sender=header_element.attrib.get("sender"),
                        node_id=source_item_id,
                    )
                    raw, raw_element = _raw(element)
                    src_seq = _nonnegative_int(element.attrib.get("sequence"), "sequence")
                    timestamp_text = element.attrib.get("timestamp")
                    timestamp_ns = parse_utc_ns(timestamp_text)
                    text = element.text or ""

                    if group_name == "Condition":
                        severity, quality, transition, why = _condition_severity(local_name)
                        if timestamp_ns is None:
                            quality = "unavailable"
                            why = "missing or invalid source timestamp"
                        state = "ok" if quality == "good" and timestamp_ns is not None else "unknown"
                        conditions.append(
                            ConditionRow(
                                cond_id=f"{context.source_id}:{canonical_device}:{canonical_item}:{src_seq}",
                                source_id=context.source_id,
                                device_key=canonical_device,
                                item_key=canonical_item,
                                source_item_id=source_item_id,
                                severity=severity,
                                native_code=element.attrib.get("nativeCode"),
                                native_severity=element.attrib.get("nativeSeverity"),
                                qualifier=element.attrib.get("qualifier"),
                                text=text,
                                t_src_utc_ns=timestamp_ns,
                                src_seq=src_seq,
                                seq=None,
                                quality=quality,
                                quality_src=f"Condition/{local_name}",
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

                    canonical_kind = (
                        definition.canonical_kind if definition else _canonical_kind(local_name, component)
                    )
                    value_kind = definition.value_kind if definition else _value_kind(group_name[:-1])
                    if value_kind not in {"sample", "event", "asset"}:
                        value_kind = "event"
                    unit_src = definition.unit_src if definition else element.attrib.get("nativeUnits")
                    canonical_unit, conversion = _unit(unit_src)
                    if definition and definition.unit is not None:
                        canonical_unit = definition.unit
                    unavailable = text.strip().upper() == "UNAVAILABLE"
                    quality = "unavailable" if unavailable or timestamp_ns is None else "good"
                    why = None
                    quality_src = "GOOD"
                    if unavailable:
                        why = "source reported UNAVAILABLE"
                        quality_src = "UNAVAILABLE"
                    elif timestamp_ns is None:
                        why = "missing or invalid source timestamp"
                        quality_src = "MISSING_OR_INVALID_TIMESTAMP"
                    number = None if quality != "good" else _convert_number(text.strip(), conversion)
                    value_text = None if quality != "good" or number is not None else text
                    observations.append(
                        ObservationRow(
                            obs_id=f"{context.source_id}:{canonical_device}:{canonical_item}:{src_seq}",
                            source_id=context.source_id,
                            device_key=canonical_device,
                            item_key=canonical_item,
                            source_item_id=source_item_id,
                            canonical_kind=canonical_kind,
                            value_kind=value_kind,
                            v_num=number,
                            v_text=value_text,
                            v_json=None,
                            unit=canonical_unit,
                            unit_src=unit_src,
                            converted_from=conversion,
                            t_src_utc_ns=timestamp_ns,
                            t_server_utc_ns=None,
                            src_seq=src_seq,
                            seq=None,
                            quality=quality,
                            quality_src=quality_src,
                            state="ok" if quality == "good" else "unknown",
                            why=why,
                            schema=schema,
                            raw=raw,
                            raw_element=raw_element,
                            src=context.src,
                            origin=context.origin,
                            evidence_eligible=(
                                context.src == "probed" and quality == "good" and timestamp_ns is not None
                            ),
                        )
                    )
    return ParsedDocument(
        document_kind,
        base_schema,
        header,
        observations=tuple(observations),
        conditions=tuple(conditions),
    )


def parse_current(
    payload: bytes | str,
    context: EndpointContext,
    definitions: Mapping[str, DataItemDefinition] | Iterable[DataItemDefinition] | ParsedDocument | None = None,
) -> ParsedDocument:
    return parse_streams(payload, context, definitions, document_kind="current")


def parse_sample(
    payload: bytes | str,
    context: EndpointContext,
    definitions: Mapping[str, DataItemDefinition] | Iterable[DataItemDefinition] | ParsedDocument | None = None,
) -> ParsedDocument:
    return parse_streams(payload, context, definitions, document_kind="sample")


def parse_mtconnect(
    payload: bytes | str,
    context: EndpointContext,
    definitions: Mapping[str, DataItemDefinition] | Iterable[DataItemDefinition] | ParsedDocument | None = None,
    *,
    document_kind: str | None = None,
) -> ParsedDocument:
    """Decode a document after inferring its read-only MTConnect document family."""

    root, _namespace_uri, _version, _extensions = _decode(payload)
    local_name = _local(root.tag)
    if local_name == "MTConnectDevices":
        return parse_probe(payload, context)
    if local_name in {"MTConnectStreams", "MTConnectError"}:
        return parse_streams(payload, context, definitions, document_kind=document_kind or "current")
    raise MalformedDocumentRefusal(f"unsupported MTConnect root element: {local_name}")
