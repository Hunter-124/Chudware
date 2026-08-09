from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from .codec import (
    BOOLEAN,
    BYTE,
    BYTESTRING,
    DATETIME,
    DATAVALUE,
    DIAGNOSTICINFO,
    DOUBLE,
    EXPANDED_NODEID,
    EXTENSIONOBJECT,
    FLOAT,
    GUID,
    INT16,
    INT32,
    INT64,
    LOCALIZEDTEXT,
    NODEID,
    NULL,
    QUALIFIEDNAME,
    SBYTE,
    STATUSCODE,
    STRING,
    UINT16,
    UINT32,
    UINT64,
    VARIANT,
    XMLELEMENT,
    CodecError,
    DataValue,
    DiagnosticInfo,
    ExpandedNodeId,
    ExtensionObject,
    LocalizedText,
    NodeId,
    QualifiedName,
    Reader,
    Variant,
    Writer,
    decode_extension_object,
    decode_string,
    dumps,
    encode_extension_object,
    encode_string,
    loads,
    register_extension_type,
)

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "mfg" / "monitor" / "opcua"
STAMP = datetime(2026, 8, 8, 12, 34, 56, 123456, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class NestedBody:
    label: str | None
    child: ExtensionObject


def _encode_nested(out: Writer, value: NestedBody) -> None:
    encode_string(out, value.label)
    encode_extension_object(out, value.child)


def _decode_nested(source: Reader) -> NestedBody:
    return NestedBody(decode_string(source), decode_extension_object(source))


NESTED_BINARY_ID = NodeId(2, 9001)
register_extension_type(NESTED_BINARY_ID, _encode_nested, _decode_nested)
INNER = ExtensionObject(NodeId(3, "opaque"), b"\x00\x01body", 1)
NESTED = ExtensionObject(NESTED_BINARY_ID, NestedBody("outer", INNER), 1)

SAMPLES = {
    NULL: None,
    BOOLEAN: True,
    SBYTE: -7,
    BYTE: 250,
    INT16: -1234,
    UINT16: 65000,
    INT32: -1234567,
    UINT32: 4_000_000_000,
    INT64: -8_000_000_000,
    UINT64: 16_000_000_000,
    FLOAT: 1.5,
    DOUBLE: -3.25,
    STRING: "Mühle 🛠",
    DATETIME: STAMP,
    GUID: UUID("00112233-4455-6677-8899-aabbccddeeff"),
    BYTESTRING: b"\x00\xffpayload",
    XMLELEMENT: "<x a=\"1\"/>",
    NODEID: NodeId(3, "Machine/Axis/X"),
    EXPANDED_NODEID: ExpandedNodeId(NodeId(2, 42), "urn:vendor:machine", 7),
    STATUSCODE: 0x40920000,
    QUALIFIEDNAME: QualifiedName(2, "ActualSpeed"),
    LOCALIZEDTEXT: LocalizedText("de-DE", "Drehzahl"),
    EXTENSIONOBJECT: NESTED,
    DATAVALUE: DataValue(
        Variant(DOUBLE, 9000.5),
        0,
        STAMP,
        STAMP,
        12,
        34,
    ),
    VARIANT: Variant(UINT32, 7),
    DIAGNOSTICINFO: DiagnosticInfo(
        symbolic_id=2,
        additional_info="outer",
        inner_status=0x80010000,
        inner=DiagnosticInfo(additional_info="inner"),
    ),
}


@pytest.mark.parametrize("type_id", tuple(SAMPLES))
def test_every_builtin_type_round_trips(type_id: int) -> None:
    assert loads(type_id, dumps(type_id, SAMPLES[type_id])) == SAMPLES[type_id]


@pytest.mark.parametrize("type_id", tuple(key for key in SAMPLES if key != NULL))
def test_every_non_null_builtin_type_round_trips_in_array_variant(type_id: int) -> None:
    variant = Variant(type_id, [SAMPLES[type_id]], True, (1,))
    assert loads(VARIANT, dumps(VARIANT, variant)) == variant


def test_nullable_strings_bytes_and_arrays_remain_distinct_from_empty() -> None:
    assert loads(STRING, dumps(STRING, None)) is None
    assert loads(STRING, dumps(STRING, "")) == ""
    assert loads(BYTESTRING, dumps(BYTESTRING, None)) is None
    assert loads(BYTESTRING, dumps(BYTESTRING, b"")) == b""
    assert loads(VARIANT, dumps(VARIANT, Variant(UINT32, None, True))) == Variant(
        UINT32, None, True
    )
    assert loads(VARIANT, dumps(VARIANT, Variant(UINT32, [], True))) == Variant(
        UINT32, [], True
    )


def test_all_node_id_encodings_match_recorded_part6_vectors() -> None:
    values = {
        "two_byte_i42": NodeId(0, 42),
        "four_byte_ns2_i500": NodeId(2, 500),
        "numeric_ns300_i70000": NodeId(300, 70_000),
        "string_ns2_axis": NodeId(2, "Axis"),
        "guid_ns1": NodeId(1, UUID("00112233-4455-6677-8899-aabbccddeeff")),
        "bytes_ns4": NodeId(4, b"\x00\x01\xff"),
    }
    recorded = {}
    for line in (FIXTURES / "nodeids.hex").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, encoded = line.split()
            recorded[name] = bytes.fromhex(encoded)
    assert recorded.keys() == values.keys()
    for name, value in values.items():
        assert dumps(NODEID, value) == recorded[name]
        assert loads(NODEID, recorded[name]) == value


def test_nested_extension_object_decodes_registered_body_recursively() -> None:
    assert loads(EXTENSIONOBJECT, dumps(EXTENSIONOBJECT, NESTED)) == NESTED


def test_datavalue_wire_order_and_picosecond_clamp_follow_part6() -> None:
    recorded = bytes.fromhex(
        "3c"
        "0000000000000000" "1027"
        "0000000000000000" "ffff"
    )
    value = loads(DATAVALUE, recorded)
    assert value.source_timestamp.year == value.server_timestamp.year == 1601
    assert value.source_picoseconds == value.server_picoseconds == 9999
    with pytest.raises(CodecError, match="picoseconds"):
        dumps(DATAVALUE, DataValue(source_picoseconds=10_000))


def test_invalid_lengths_masks_dimensions_and_trailing_bytes_are_refused() -> None:
    with pytest.raises(CodecError, match="oversized String"):
        loads(STRING, b"\xff\xff\xff\x7f")
    with pytest.raises(CodecError, match="LocalizedText mask"):
        loads(LOCALIZEDTEXT, b"\x80")
    with pytest.raises(CodecError, match="dimensions"):
        dumps(VARIANT, Variant(UINT32, [1, 2], True, (3,)))
    with pytest.raises(CodecError, match="trailing"):
        loads(UINT32, b"\x01\x00\x00\x00extra")
