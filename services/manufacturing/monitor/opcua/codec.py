"""Bounded OPC UA Binary Encoding codec used by the read-only client and tests.

The codec implements the OPC UA built-in type table, not a Python-specific wire
format.  Length-prefixed values are bounded before allocation and recursive
values have a depth limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import struct
from typing import Any, Callable, Iterable
from uuid import UUID


UA_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
MAX_ALLOCATION = 4 * 1024 * 1024
MAX_ARRAY_LENGTH = 100_000
MAX_RECURSION = 32

NULL = 0
BOOLEAN = 1
SBYTE = 2
BYTE = 3
INT16 = 4
UINT16 = 5
INT32 = 6
UINT32 = 7
INT64 = 8
UINT64 = 9
FLOAT = 10
DOUBLE = 11
STRING = 12
DATETIME = 13
GUID = 14
BYTESTRING = 15
XMLELEMENT = 16
NODEID = 17
EXPANDED_NODEID = 18
STATUSCODE = 19
QUALIFIEDNAME = 20
LOCALIZEDTEXT = 21
EXTENSIONOBJECT = 22
DATAVALUE = 23
VARIANT = 24
DIAGNOSTICINFO = 25

_BUILTIN_NAMES = {
    NULL: "Null",
    BOOLEAN: "Boolean",
    SBYTE: "SByte",
    BYTE: "Byte",
    INT16: "Int16",
    UINT16: "UInt16",
    INT32: "Int32",
    UINT32: "UInt32",
    INT64: "Int64",
    UINT64: "UInt64",
    FLOAT: "Float",
    DOUBLE: "Double",
    STRING: "String",
    DATETIME: "DateTime",
    GUID: "Guid",
    BYTESTRING: "ByteString",
    XMLELEMENT: "XmlElement",
    NODEID: "NodeId",
    EXPANDED_NODEID: "ExpandedNodeId",
    STATUSCODE: "StatusCode",
    QUALIFIEDNAME: "QualifiedName",
    LOCALIZEDTEXT: "LocalizedText",
    EXTENSIONOBJECT: "ExtensionObject",
    DATAVALUE: "DataValue",
    VARIANT: "Variant",
    DIAGNOSTICINFO: "DiagnosticInfo",
}


class CodecError(ValueError):
    """Malformed, unsupported, or resource-exhausting OPC UA binary value."""


@dataclass(frozen=True, slots=True)
class NodeId:
    namespace: int = 0
    identifier: int | str | UUID | bytes = 0

    def __post_init__(self) -> None:
        if not 0 <= self.namespace <= 0xFFFF:
            raise CodecError("NodeId namespace is outside UInt16 range")
        if isinstance(self.identifier, int) and not 0 <= self.identifier <= 0xFFFFFFFF:
            raise CodecError("numeric NodeId is outside UInt32 range")
        if not isinstance(self.identifier, (int, str, UUID, bytes)):
            raise CodecError("unsupported NodeId identifier kind")

    def __str__(self) -> str:
        marker = "i" if isinstance(self.identifier, int) else "s"
        if isinstance(self.identifier, UUID):
            marker = "g"
        elif isinstance(self.identifier, bytes):
            marker = "b"
        value = self.identifier.hex() if isinstance(self.identifier, bytes) else str(self.identifier)
        return f"ns={self.namespace};{marker}={value}"


@dataclass(frozen=True, slots=True)
class ExpandedNodeId:
    node_id: NodeId
    namespace_uri: str | None = None
    server_index: int | None = None


@dataclass(frozen=True, slots=True)
class QualifiedName:
    namespace: int = 0
    name: str | None = None


@dataclass(frozen=True, slots=True)
class LocalizedText:
    locale: str | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class ExtensionObject:
    type_id: NodeId
    body: Any = None
    encoding: int = 1

    def __post_init__(self) -> None:
        if self.encoding not in (0, 1, 2):
            raise CodecError("ExtensionObject encoding must be 0, 1, or 2")


@dataclass(frozen=True, slots=True)
class Variant:
    type_id: int = NULL
    value: Any = None
    is_array: bool = False
    dimensions: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if self.type_id not in _BUILTIN_NAMES:
            raise CodecError(f"unsupported Variant built-in type id {self.type_id}")
        if self.dimensions is not None and not self.is_array:
            raise CodecError("Variant dimensions require an array")


@dataclass(frozen=True, slots=True)
class DataValue:
    value: Variant | None = None
    status: int | None = None
    source_timestamp: datetime | None = None
    server_timestamp: datetime | None = None
    source_picoseconds: int | None = None
    server_picoseconds: int | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticInfo:
    symbolic_id: int | None = None
    namespace_uri: int | None = None
    localized_text: int | None = None
    locale: int | None = None
    additional_info: str | None = None
    inner_status: int | None = None
    inner: "DiagnosticInfo | None" = None


@dataclass(frozen=True, slots=True)
class AnonymousIdentityToken:
    policy_id: str | None = None


class Writer:
    __slots__ = ("_parts", "_size", "max_allocation", "max_array_length")

    def __init__(
        self,
        *,
        max_allocation: int = MAX_ALLOCATION,
        max_array_length: int = MAX_ARRAY_LENGTH,
    ) -> None:
        self._parts: list[bytes] = []
        self._size = 0
        self.max_allocation = max_allocation
        self.max_array_length = max_array_length

    def put(self, payload: bytes) -> None:
        if self._size + len(payload) > self.max_allocation:
            raise CodecError("encoded value exceeds allocation limit")
        self._parts.append(payload)
        self._size += len(payload)

    def pack(self, form: str, value: Any) -> None:
        try:
            self.put(struct.pack("<" + form, value))
        except (struct.error, OverflowError) as exc:
            raise CodecError(f"value cannot be encoded as {form}") from exc

    def finish(self) -> bytes:
        return b"".join(self._parts)


class Reader:
    __slots__ = ("_view", "offset", "max_allocation", "max_array_length", "depth")

    def __init__(
        self,
        payload: bytes | bytearray | memoryview,
        *,
        max_allocation: int = MAX_ALLOCATION,
        max_array_length: int = MAX_ARRAY_LENGTH,
        depth: int = 0,
    ) -> None:
        if len(payload) > max_allocation:
            raise CodecError("encoded value exceeds allocation limit")
        self._view = memoryview(payload)
        self.offset = 0
        self.max_allocation = max_allocation
        self.max_array_length = max_array_length
        self.depth = depth

    @property
    def remaining(self) -> int:
        return len(self._view) - self.offset

    def take(self, size: int) -> bytes:
        if size < 0 or size > self.remaining:
            raise CodecError("truncated OPC UA binary value")
        start = self.offset
        self.offset += size
        return self._view[start : start + size].tobytes()

    def unpack(self, form: str) -> Any:
        size = struct.calcsize("<" + form)
        try:
            value = struct.unpack_from("<" + form, self._view, self.offset)[0]
        except struct.error as exc:
            raise CodecError("truncated OPC UA binary scalar") from exc
        self.offset += size
        return value

    def child(self, payload: bytes) -> "Reader":
        if self.depth >= MAX_RECURSION:
            raise CodecError("OPC UA value nesting exceeds recursion limit")
        return Reader(
            payload,
            max_allocation=self.max_allocation,
            max_array_length=self.max_array_length,
            depth=self.depth + 1,
        )

    def require_finished(self) -> None:
        if self.remaining:
            raise CodecError(f"{self.remaining} trailing bytes after OPC UA value")


ExtensionEncoder = Callable[[Writer, Any], None]
ExtensionDecoder = Callable[[Reader], Any]
_EXTENSION_TYPES: dict[NodeId, tuple[ExtensionEncoder, ExtensionDecoder]] = {}


def register_extension_type(
    type_id: NodeId, encoder: ExtensionEncoder, decoder: ExtensionDecoder
) -> None:
    """Register the binary body codec for one DefaultBinary encoding NodeId."""

    if type_id in _EXTENSION_TYPES:
        raise CodecError(f"ExtensionObject type already registered: {type_id}")
    _EXTENSION_TYPES[type_id] = (encoder, decoder)


def _encode_string(writer: Writer, value: str | None) -> None:
    if value is None:
        writer.pack("i", -1)
        return
    payload = value.encode("utf-8")
    if len(payload) > writer.max_allocation:
        raise CodecError("String exceeds allocation limit")
    writer.pack("i", len(payload))
    writer.put(payload)


def _decode_string(reader: Reader) -> str | None:
    size = reader.unpack("i")
    if size == -1:
        return None
    if size < -1 or size > reader.max_allocation:
        raise CodecError("invalid or oversized String length")
    try:
        return reader.take(size).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CodecError("String is not valid UTF-8") from exc


def _encode_bytes(writer: Writer, value: bytes | None) -> None:
    if value is None:
        writer.pack("i", -1)
        return
    if len(value) > writer.max_allocation:
        raise CodecError("ByteString exceeds allocation limit")
    writer.pack("i", len(value))
    writer.put(value)


def _decode_bytes(reader: Reader) -> bytes | None:
    size = reader.unpack("i")
    if size == -1:
        return None
    if size < -1 or size > reader.max_allocation:
        raise CodecError("invalid or oversized ByteString length")
    return reader.take(size)


def encode_array(
    writer: Writer,
    values: Iterable[Any] | None,
    encoder: Callable[[Writer, Any], None],
) -> None:
    if values is None:
        writer.pack("i", -1)
        return
    sequence = values if isinstance(values, (tuple, list)) else tuple(values)
    if len(sequence) > writer.max_array_length:
        raise CodecError("array exceeds element limit")
    writer.pack("i", len(sequence))
    for value in sequence:
        encoder(writer, value)


def decode_array(reader: Reader, decoder: Callable[[Reader], Any]) -> list[Any] | None:
    count = reader.unpack("i")
    if count == -1:
        return None
    if count < -1 or count > reader.max_array_length:
        raise CodecError("invalid or oversized array length")
    return [decoder(reader) for _ in range(count)]


def encode_node_id(writer: Writer, value: NodeId) -> None:
    identifier = value.identifier
    namespace = value.namespace
    if isinstance(identifier, int):
        if namespace == 0 and identifier <= 0xFF:
            writer.pack("B", 0)
            writer.pack("B", identifier)
        elif namespace <= 0xFF and identifier <= 0xFFFF:
            writer.pack("B", 1)
            writer.pack("B", namespace)
            writer.pack("H", identifier)
        else:
            writer.pack("B", 2)
            writer.pack("H", namespace)
            writer.pack("I", identifier)
    elif isinstance(identifier, str):
        writer.pack("B", 3)
        writer.pack("H", namespace)
        _encode_string(writer, identifier)
    elif isinstance(identifier, UUID):
        writer.pack("B", 4)
        writer.pack("H", namespace)
        writer.put(identifier.bytes_le)
    else:
        writer.pack("B", 5)
        writer.pack("H", namespace)
        _encode_bytes(writer, identifier)


def _decode_node_id_with_encoding(reader: Reader, encoding: int) -> NodeId:
    kind = encoding & 0x3F
    if kind == 0:
        return NodeId(0, reader.unpack("B"))
    if kind == 1:
        return NodeId(reader.unpack("B"), reader.unpack("H"))
    if kind == 2:
        return NodeId(reader.unpack("H"), reader.unpack("I"))
    if kind == 3:
        namespace = reader.unpack("H")
        value = _decode_string(reader)
        if value is None:
            raise CodecError("String NodeId cannot contain a null identifier")
        return NodeId(namespace, value)
    if kind == 4:
        return NodeId(reader.unpack("H"), UUID(bytes_le=reader.take(16)))
    if kind == 5:
        namespace = reader.unpack("H")
        value = _decode_bytes(reader)
        if value is None:
            raise CodecError("ByteString NodeId cannot contain a null identifier")
        return NodeId(namespace, value)
    raise CodecError(f"unsupported NodeId encoding {kind}")


def decode_node_id(reader: Reader) -> NodeId:
    encoding = reader.unpack("B")
    if encoding & 0xC0:
        raise CodecError("expanded flags are invalid on a NodeId")
    return _decode_node_id_with_encoding(reader, encoding)


def encode_expanded_node_id(writer: Writer, value: ExpandedNodeId) -> None:
    inner = Writer(max_allocation=writer.max_allocation, max_array_length=writer.max_array_length)
    encode_node_id(inner, value.node_id)
    encoded = bytearray(inner.finish())
    if value.namespace_uri is not None:
        encoded[0] |= 0x80
    if value.server_index is not None:
        encoded[0] |= 0x40
    writer.put(bytes(encoded))
    if value.namespace_uri is not None:
        _encode_string(writer, value.namespace_uri)
    if value.server_index is not None:
        writer.pack("I", value.server_index)


def decode_expanded_node_id(reader: Reader) -> ExpandedNodeId:
    encoding = reader.unpack("B")
    node_id = _decode_node_id_with_encoding(reader, encoding)
    namespace_uri = _decode_string(reader) if encoding & 0x80 else None
    server_index = reader.unpack("I") if encoding & 0x40 else None
    return ExpandedNodeId(node_id, namespace_uri, server_index)


def encode_qualified_name(writer: Writer, value: QualifiedName) -> None:
    writer.pack("H", value.namespace)
    _encode_string(writer, value.name)


def decode_qualified_name(reader: Reader) -> QualifiedName:
    return QualifiedName(reader.unpack("H"), _decode_string(reader))


def encode_localized_text(writer: Writer, value: LocalizedText) -> None:
    mask = (1 if value.locale is not None else 0) | (2 if value.text is not None else 0)
    writer.pack("B", mask)
    if value.locale is not None:
        _encode_string(writer, value.locale)
    if value.text is not None:
        _encode_string(writer, value.text)


def decode_localized_text(reader: Reader) -> LocalizedText:
    mask = reader.unpack("B")
    if mask & ~0x03:
        raise CodecError("invalid LocalizedText mask")
    locale = _decode_string(reader) if mask & 1 else None
    text = _decode_string(reader) if mask & 2 else None
    return LocalizedText(locale, text)


def _datetime_to_ticks(value: datetime) -> int:
    if value.tzinfo is None:
        raise CodecError("DateTime must be timezone-aware")
    utc = value.astimezone(timezone.utc)
    delta = utc - UA_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 10_000_000 + delta.microseconds * 10


def _ticks_to_datetime(value: int) -> datetime:
    seconds, remainder = divmod(value, 10_000_000)
    try:
        return UA_EPOCH + timedelta(seconds=seconds, microseconds=remainder // 10)
    except OverflowError as exc:
        raise CodecError("DateTime is outside Python datetime range") from exc


def encode_extension_object(writer: Writer, value: ExtensionObject) -> None:
    encode_node_id(writer, value.type_id)
    writer.pack("B", value.encoding)
    if value.encoding == 0:
        return
    if value.encoding == 2:
        if value.body is not None and not isinstance(value.body, str):
            raise CodecError("XML ExtensionObject body must be str or null")
        _encode_string(writer, value.body)
        return
    if value.body is None or isinstance(value.body, bytes):
        _encode_bytes(writer, value.body)
        return
    registered = _EXTENSION_TYPES.get(value.type_id)
    if registered is None:
        raise CodecError(f"no binary body codec registered for {value.type_id}")
    body_writer = Writer(
        max_allocation=writer.max_allocation, max_array_length=writer.max_array_length
    )
    registered[0](body_writer, value.body)
    _encode_bytes(writer, body_writer.finish())


def decode_extension_object(reader: Reader) -> ExtensionObject:
    type_id = decode_node_id(reader)
    encoding = reader.unpack("B")
    if encoding == 0:
        return ExtensionObject(type_id, None, 0)
    if encoding == 2:
        return ExtensionObject(type_id, _decode_string(reader), 2)
    if encoding != 1:
        raise CodecError(f"unsupported ExtensionObject encoding {encoding}")
    payload = _decode_bytes(reader)
    if payload is None:
        return ExtensionObject(type_id, None, 1)
    registered = _EXTENSION_TYPES.get(type_id)
    if registered is None:
        return ExtensionObject(type_id, payload, 1)
    child = reader.child(payload)
    body = registered[1](child)
    child.require_finished()
    return ExtensionObject(type_id, body, 1)


def encode_variant(writer: Writer, value: Variant) -> None:
    if value.type_id == NULL:
        if value.is_array or value.dimensions is not None or value.value is not None:
            raise CodecError("Null Variant cannot carry a value or array flags")
        writer.pack("B", 0)
        return
    if value.dimensions is not None and value.value is not None:
        product = 1
        for dimension in value.dimensions:
            if dimension < 0:
                raise CodecError("Variant dimension cannot be negative")
            product *= dimension
        if product != len(value.value):
            raise CodecError("Variant dimensions do not match array length")
    mask = value.type_id | (0x80 if value.is_array else 0)
    if value.dimensions is not None:
        mask |= 0x40
    writer.pack("B", mask)
    if value.is_array:
        encode_array(writer, value.value, lambda out, item: encode_builtin(out, value.type_id, item))
    else:
        encode_builtin(writer, value.type_id, value.value)
    if value.dimensions is not None:
        encode_array(writer, value.dimensions, lambda out, item: out.pack("i", item))


def decode_variant(reader: Reader) -> Variant:
    mask = reader.unpack("B")
    type_id = mask & 0x3F
    is_array = bool(mask & 0x80)
    has_dimensions = bool(mask & 0x40)
    if type_id == NULL:
        if is_array or has_dimensions:
            raise CodecError("Null Variant has invalid array flags")
        return Variant()
    if type_id not in _BUILTIN_NAMES:
        raise CodecError(f"unsupported Variant built-in type id {type_id}")
    if is_array:
        value = decode_array(reader, lambda source: decode_builtin(source, type_id))
    else:
        value = decode_builtin(reader, type_id)
    dimensions_raw = (
        decode_array(reader, lambda source: source.unpack("i")) if has_dimensions else None
    )
    dimensions = tuple(dimensions_raw or ()) if has_dimensions else None
    if dimensions is not None and value is not None:
        product = 1
        for dimension in dimensions:
            if dimension < 0:
                raise CodecError("Variant dimension cannot be negative")
            product *= dimension
        if product != len(value):
            raise CodecError("Variant dimensions do not match array length")
    return Variant(type_id, value, is_array, dimensions)


def encode_data_value(writer: Writer, value: DataValue) -> None:
    for picoseconds in (value.source_picoseconds, value.server_picoseconds):
        if picoseconds is not None and not 0 <= picoseconds < 10_000:
            raise CodecError("DataValue picoseconds must be in [0, 10000)")
    mask = 0
    mask |= 0x01 if value.value is not None else 0
    mask |= 0x02 if value.status is not None else 0
    mask |= 0x04 if value.source_timestamp is not None else 0
    mask |= 0x08 if value.server_timestamp is not None else 0
    mask |= 0x10 if value.source_picoseconds is not None else 0
    mask |= 0x20 if value.server_picoseconds is not None else 0
    writer.pack("B", mask)
    if value.value is not None:
        encode_variant(writer, value.value)
    if value.status is not None:
        writer.pack("I", value.status)
    if value.source_timestamp is not None:
        writer.pack("q", _datetime_to_ticks(value.source_timestamp))
    if value.source_picoseconds is not None:
        writer.pack("H", value.source_picoseconds)
    if value.server_timestamp is not None:
        writer.pack("q", _datetime_to_ticks(value.server_timestamp))
    if value.server_picoseconds is not None:
        writer.pack("H", value.server_picoseconds)


def decode_data_value(reader: Reader) -> DataValue:
    mask = reader.unpack("B")
    if mask & ~0x3F:
        raise CodecError("invalid DataValue mask")
    value = decode_variant(reader) if mask & 0x01 else None
    status = reader.unpack("I") if mask & 0x02 else None
    source_timestamp = _ticks_to_datetime(reader.unpack("q")) if mask & 0x04 else None
    source_picoseconds_raw = reader.unpack("H") if mask & 0x10 else None
    source_picoseconds = (
        min(source_picoseconds_raw, 9999)
        if source_picoseconds_raw is not None
        else None
    )
    server_timestamp = _ticks_to_datetime(reader.unpack("q")) if mask & 0x08 else None
    server_picoseconds_raw = reader.unpack("H") if mask & 0x20 else None
    server_picoseconds = (
        min(server_picoseconds_raw, 9999)
        if server_picoseconds_raw is not None
        else None
    )
    return DataValue(
        value,
        status,
        source_timestamp,
        server_timestamp,
        source_picoseconds,
        server_picoseconds,
    )


def encode_diagnostic_info(writer: Writer, value: DiagnosticInfo) -> None:
    fields = (
        value.symbolic_id,
        value.namespace_uri,
        value.localized_text,
        value.locale,
        value.additional_info,
        value.inner_status,
        value.inner,
    )
    mask = sum((1 << index) for index, item in enumerate(fields) if item is not None)
    writer.pack("B", mask)
    for item in fields[:4]:
        if item is not None:
            writer.pack("i", item)
    if value.additional_info is not None:
        _encode_string(writer, value.additional_info)
    if value.inner_status is not None:
        writer.pack("I", value.inner_status)
    if value.inner is not None:
        encode_diagnostic_info(writer, value.inner)


def decode_diagnostic_info(reader: Reader) -> DiagnosticInfo:
    mask = reader.unpack("B")
    if mask & ~0x7F:
        raise CodecError("invalid DiagnosticInfo mask")
    return DiagnosticInfo(
        symbolic_id=reader.unpack("i") if mask & 0x01 else None,
        namespace_uri=reader.unpack("i") if mask & 0x02 else None,
        localized_text=reader.unpack("i") if mask & 0x04 else None,
        locale=reader.unpack("i") if mask & 0x08 else None,
        additional_info=_decode_string(reader) if mask & 0x10 else None,
        inner_status=reader.unpack("I") if mask & 0x20 else None,
        inner=decode_diagnostic_info(reader) if mask & 0x40 else None,
    )


def encode_builtin(writer: Writer, type_id: int, value: Any) -> None:
    if type_id == NULL:
        if value is not None:
            raise CodecError("Null built-in value must be None")
    elif type_id == BOOLEAN:
        writer.pack("B", 1 if value else 0)
    elif type_id == SBYTE:
        writer.pack("b", value)
    elif type_id == BYTE:
        writer.pack("B", value)
    elif type_id == INT16:
        writer.pack("h", value)
    elif type_id == UINT16:
        writer.pack("H", value)
    elif type_id == INT32:
        writer.pack("i", value)
    elif type_id == UINT32 or type_id == STATUSCODE:
        writer.pack("I", value)
    elif type_id == INT64:
        writer.pack("q", value)
    elif type_id == UINT64:
        writer.pack("Q", value)
    elif type_id == FLOAT:
        writer.pack("f", value)
    elif type_id == DOUBLE:
        writer.pack("d", value)
    elif type_id == STRING or type_id == XMLELEMENT:
        _encode_string(writer, value)
    elif type_id == DATETIME:
        writer.pack("q", _datetime_to_ticks(value))
    elif type_id == GUID:
        writer.put(value.bytes_le)
    elif type_id == BYTESTRING:
        _encode_bytes(writer, value)
    elif type_id == NODEID:
        encode_node_id(writer, value)
    elif type_id == EXPANDED_NODEID:
        encode_expanded_node_id(writer, value)
    elif type_id == QUALIFIEDNAME:
        encode_qualified_name(writer, value)
    elif type_id == LOCALIZEDTEXT:
        encode_localized_text(writer, value)
    elif type_id == EXTENSIONOBJECT:
        encode_extension_object(writer, value)
    elif type_id == DATAVALUE:
        encode_data_value(writer, value)
    elif type_id == VARIANT:
        encode_variant(writer, value)
    elif type_id == DIAGNOSTICINFO:
        encode_diagnostic_info(writer, value)
    else:
        raise CodecError(f"unsupported built-in type id {type_id}")


def decode_builtin(reader: Reader, type_id: int) -> Any:
    if type_id == NULL:
        return None
    if type_id == BOOLEAN:
        value = reader.unpack("B")
        if value not in (0, 1):
            raise CodecError("Boolean byte must be zero or one")
        return bool(value)
    forms = {
        SBYTE: "b",
        BYTE: "B",
        INT16: "h",
        UINT16: "H",
        INT32: "i",
        UINT32: "I",
        INT64: "q",
        UINT64: "Q",
        FLOAT: "f",
        DOUBLE: "d",
        STATUSCODE: "I",
    }
    if type_id in forms:
        return reader.unpack(forms[type_id])
    if type_id == STRING or type_id == XMLELEMENT:
        return _decode_string(reader)
    if type_id == DATETIME:
        return _ticks_to_datetime(reader.unpack("q"))
    if type_id == GUID:
        return UUID(bytes_le=reader.take(16))
    if type_id == BYTESTRING:
        return _decode_bytes(reader)
    if type_id == NODEID:
        return decode_node_id(reader)
    if type_id == EXPANDED_NODEID:
        return decode_expanded_node_id(reader)
    if type_id == QUALIFIEDNAME:
        return decode_qualified_name(reader)
    if type_id == LOCALIZEDTEXT:
        return decode_localized_text(reader)
    if type_id == EXTENSIONOBJECT:
        return decode_extension_object(reader)
    if type_id == DATAVALUE:
        return decode_data_value(reader)
    if type_id == VARIANT:
        return decode_variant(reader)
    if type_id == DIAGNOSTICINFO:
        return decode_diagnostic_info(reader)
    raise CodecError(f"unsupported built-in type id {type_id}")


def dumps(type_id: int, value: Any) -> bytes:
    writer = Writer()
    encode_builtin(writer, type_id, value)
    return writer.finish()


def loads(type_id: int, payload: bytes) -> Any:
    reader = Reader(payload)
    value = decode_builtin(reader, type_id)
    reader.require_finished()
    return value


def _encode_anonymous(writer: Writer, value: AnonymousIdentityToken) -> None:
    _encode_string(writer, value.policy_id)


def _decode_anonymous(reader: Reader) -> AnonymousIdentityToken:
    return AnonymousIdentityToken(_decode_string(reader))


ANONYMOUS_IDENTITY_TOKEN_BINARY = NodeId(0, 321)
register_extension_type(
    ANONYMOUS_IDENTITY_TOKEN_BINARY, _encode_anonymous, _decode_anonymous
)

# Public aliases for service-structure codecs.  Keeping string/bytes primitives
# here avoids every service module inventing subtly different nullable handling.
encode_string = _encode_string
decode_string = _decode_string
encode_bytestring = _encode_bytes
decode_bytestring = _decode_bytes
encode_datetime = lambda writer, value: writer.pack("q", _datetime_to_ticks(value))
decode_datetime = lambda reader: _ticks_to_datetime(reader.unpack("q"))


__all__ = (
    "ANONYMOUS_IDENTITY_TOKEN_BINARY",
    "AnonymousIdentityToken",
    "BOOLEAN",
    "BYTE",
    "BYTESTRING",
    "CodecError",
    "DATETIME",
    "DATAVALUE",
    "DIAGNOSTICINFO",
    "DOUBLE",
    "DataValue",
    "DiagnosticInfo",
    "EXPANDED_NODEID",
    "EXTENSIONOBJECT",
    "ExpandedNodeId",
    "ExtensionObject",
    "FLOAT",
    "GUID",
    "INT16",
    "INT32",
    "INT64",
    "LOCALIZEDTEXT",
    "LocalizedText",
    "NODEID",
    "NULL",
    "NodeId",
    "QUALIFIEDNAME",
    "QualifiedName",
    "Reader",
    "SBYTE",
    "STATUSCODE",
    "STRING",
    "UINT16",
    "UINT32",
    "UINT64",
    "VARIANT",
    "Variant",
    "Writer",
    "XMLELEMENT",
    "decode_array",
    "decode_builtin",
    "decode_bytestring",
    "decode_data_value",
    "decode_datetime",
    "decode_diagnostic_info",
    "decode_expanded_node_id",
    "decode_extension_object",
    "decode_localized_text",
    "decode_node_id",
    "decode_qualified_name",
    "decode_string",
    "decode_variant",
    "dumps",
    "encode_array",
    "encode_builtin",
    "encode_bytestring",
    "encode_data_value",
    "encode_datetime",
    "encode_diagnostic_info",
    "encode_expanded_node_id",
    "encode_extension_object",
    "encode_localized_text",
    "encode_node_id",
    "encode_qualified_name",
    "encode_string",
    "encode_variant",
    "loads",
    "register_extension_type",
)
