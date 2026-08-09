"""Binary structures for the deliberately small read-only OPC UA service set."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .codec import (
    ANONYMOUS_IDENTITY_TOKEN_BINARY,
    AnonymousIdentityToken,
    DataValue,
    DiagnosticInfo,
    ExpandedNodeId,
    ExtensionObject,
    LocalizedText,
    NodeId,
    QualifiedName,
    Reader,
    Writer,
    decode_array,
    decode_bytestring,
    decode_data_value,
    decode_datetime,
    decode_diagnostic_info,
    decode_expanded_node_id,
    decode_extension_object,
    decode_localized_text,
    decode_node_id,
    decode_qualified_name,
    decode_string,
    encode_array,
    encode_bytestring,
    encode_data_value,
    encode_datetime,
    encode_diagnostic_info,
    encode_expanded_node_id,
    encode_extension_object,
    encode_localized_text,
    encode_node_id,
    encode_qualified_name,
    encode_string,
)

SECURITY_POLICY_NONE = "http://opcfoundation.org/UA/SecurityPolicy#None"
VALUE_ATTRIBUTE_ID = 13
GOOD = 0x00000000

# DefaultBinary encoding NodeIds from OPC UA Namespace 0.  This allowlist is the
# entire service surface; the client dispatches nothing outside it.
SERVICE_TYPE_IDS = {
    "ServiceFault": 397,
    "OpenSecureChannelRequest": 446,
    "OpenSecureChannelResponse": 449,
    "CloseSecureChannelRequest": 452,
    "CloseSecureChannelResponse": 455,
    "CreateSessionRequest": 461,
    "CreateSessionResponse": 464,
    "ActivateSessionRequest": 467,
    "ActivateSessionResponse": 470,
    "CloseSessionRequest": 473,
    "CloseSessionResponse": 476,
    "BrowseRequest": 527,
    "BrowseResponse": 530,
    "BrowseNextRequest": 533,
    "BrowseNextResponse": 536,
    "ReadRequest": 631,
    "ReadResponse": 634,
}


@dataclass(frozen=True, slots=True)
class RequestHeader:
    authentication_token: NodeId = NodeId()
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    request_handle: int = 0
    return_diagnostics: int = 0
    audit_entry_id: str | None = None
    timeout_hint: int = 10_000
    additional_header: ExtensionObject = ExtensionObject(NodeId(), None, 0)


@dataclass(frozen=True, slots=True)
class ResponseHeader:
    timestamp: datetime
    request_handle: int
    service_result: int = GOOD
    service_diagnostics: DiagnosticInfo = DiagnosticInfo()
    string_table: tuple[str | None, ...] | None = ()
    additional_header: ExtensionObject = ExtensionObject(NodeId(), None, 0)


@dataclass(frozen=True, slots=True)
class ApplicationDescription:
    application_uri: str | None
    product_uri: str | None
    application_name: LocalizedText
    application_type: int
    gateway_server_uri: str | None = None
    discovery_profile_uri: str | None = None
    discovery_urls: tuple[str | None, ...] | None = ()


@dataclass(frozen=True, slots=True)
class SignatureData:
    algorithm: str | None = None
    signature: bytes | None = None


@dataclass(frozen=True, slots=True)
class SignedSoftwareCertificate:
    certificate_data: bytes | None = None
    signature: bytes | None = None


@dataclass(frozen=True, slots=True)
class UserTokenPolicy:
    policy_id: str | None
    token_type: int
    issued_token_type: str | None = None
    issuer_endpoint_url: str | None = None
    security_policy_uri: str | None = None


@dataclass(frozen=True, slots=True)
class EndpointDescription:
    endpoint_url: str | None
    server: ApplicationDescription
    server_certificate: bytes | None
    security_mode: int
    security_policy_uri: str | None
    user_identity_tokens: tuple[UserTokenPolicy, ...] | None
    transport_profile_uri: str | None
    security_level: int


@dataclass(frozen=True, slots=True)
class ChannelSecurityToken:
    channel_id: int
    token_id: int
    created_at: datetime
    revised_lifetime: int


@dataclass(frozen=True, slots=True)
class OpenSecureChannelRequest:
    header: RequestHeader
    client_protocol_version: int = 0
    request_type: int = 0
    security_mode: int = 1
    client_nonce: bytes | None = None
    requested_lifetime: int = 600_000


@dataclass(frozen=True, slots=True)
class OpenSecureChannelResponse:
    header: ResponseHeader
    server_protocol_version: int
    token: ChannelSecurityToken
    server_nonce: bytes | None


@dataclass(frozen=True, slots=True)
class CreateSessionRequest:
    header: RequestHeader
    client_description: ApplicationDescription
    server_uri: str | None
    endpoint_url: str | None
    session_name: str | None
    client_nonce: bytes | None
    client_certificate: bytes | None
    requested_session_timeout: float
    max_response_message_size: int


@dataclass(frozen=True, slots=True)
class CreateSessionResponse:
    header: ResponseHeader
    session_id: NodeId
    authentication_token: NodeId
    revised_session_timeout: float
    server_nonce: bytes | None
    server_certificate: bytes | None
    server_endpoints: tuple[EndpointDescription, ...] | None
    server_software_certificates: tuple[SignedSoftwareCertificate, ...] | None
    server_signature: SignatureData
    max_request_message_size: int


@dataclass(frozen=True, slots=True)
class ActivateSessionRequest:
    header: RequestHeader
    client_signature: SignatureData = SignatureData()
    client_software_certificates: tuple[SignedSoftwareCertificate, ...] | None = ()
    locale_ids: tuple[str | None, ...] | None = ()
    user_identity_token: ExtensionObject = ExtensionObject(
        ANONYMOUS_IDENTITY_TOKEN_BINARY, AnonymousIdentityToken(None), 1
    )
    user_token_signature: SignatureData = SignatureData()


@dataclass(frozen=True, slots=True)
class ActivateSessionResponse:
    header: ResponseHeader
    server_nonce: bytes | None
    results: tuple[int, ...] | None
    diagnostic_infos: tuple[DiagnosticInfo, ...] | None


@dataclass(frozen=True, slots=True)
class ViewDescription:
    view_id: NodeId = NodeId()
    timestamp: datetime = datetime(1601, 1, 1, tzinfo=timezone.utc)
    view_version: int = 0


@dataclass(frozen=True, slots=True)
class BrowseDescription:
    node_id: NodeId
    browse_direction: int = 0
    reference_type_id: NodeId = NodeId(0, 33)
    include_subtypes: bool = True
    node_class_mask: int = 0
    result_mask: int = 0x3F


@dataclass(frozen=True, slots=True)
class ReferenceDescription:
    reference_type_id: NodeId
    is_forward: bool
    node_id: ExpandedNodeId
    browse_name: QualifiedName
    display_name: LocalizedText
    node_class: int
    type_definition: ExpandedNodeId


@dataclass(frozen=True, slots=True)
class BrowseResult:
    status: int
    continuation_point: bytes | None
    references: tuple[ReferenceDescription, ...] | None


@dataclass(frozen=True, slots=True)
class BrowseRequest:
    header: RequestHeader
    view: ViewDescription
    requested_max_references_per_node: int
    nodes: tuple[BrowseDescription, ...] | None


@dataclass(frozen=True, slots=True)
class BrowseResponse:
    header: ResponseHeader
    results: tuple[BrowseResult, ...] | None
    diagnostic_infos: tuple[DiagnosticInfo, ...] | None


@dataclass(frozen=True, slots=True)
class BrowseNextRequest:
    header: RequestHeader
    release_continuation_points: bool
    continuation_points: tuple[bytes | None, ...] | None


@dataclass(frozen=True, slots=True)
class BrowseNextResponse:
    header: ResponseHeader
    results: tuple[BrowseResult, ...] | None
    diagnostic_infos: tuple[DiagnosticInfo, ...] | None


@dataclass(frozen=True, slots=True)
class ReadValueId:
    node_id: NodeId
    attribute_id: int = VALUE_ATTRIBUTE_ID
    index_range: str | None = None
    data_encoding: QualifiedName = QualifiedName()


@dataclass(frozen=True, slots=True)
class ReadRequest:
    header: RequestHeader
    max_age: float
    timestamps_to_return: int
    nodes: tuple[ReadValueId, ...] | None


@dataclass(frozen=True, slots=True)
class ReadResponse:
    header: ResponseHeader
    results: tuple[DataValue, ...] | None
    diagnostic_infos: tuple[DiagnosticInfo, ...] | None


@dataclass(frozen=True, slots=True)
class CloseSessionRequest:
    header: RequestHeader
    delete_subscriptions: bool = True


@dataclass(frozen=True, slots=True)
class CloseSessionResponse:
    header: ResponseHeader


@dataclass(frozen=True, slots=True)
class CloseSecureChannelRequest:
    header: RequestHeader


@dataclass(frozen=True, slots=True)
class CloseSecureChannelResponse:
    header: ResponseHeader


@dataclass(frozen=True, slots=True)
class ServiceFault:
    header: ResponseHeader


def encode_request_header(out: Writer, value: RequestHeader) -> None:
    encode_node_id(out, value.authentication_token)
    encode_datetime(out, value.timestamp)
    out.pack("I", value.request_handle)
    out.pack("I", value.return_diagnostics)
    encode_string(out, value.audit_entry_id)
    out.pack("I", value.timeout_hint)
    encode_extension_object(out, value.additional_header)


def decode_request_header(source: Reader) -> RequestHeader:
    return RequestHeader(
        decode_node_id(source),
        decode_datetime(source),
        source.unpack("I"),
        source.unpack("I"),
        decode_string(source),
        source.unpack("I"),
        decode_extension_object(source),
    )


def encode_response_header(out: Writer, value: ResponseHeader) -> None:
    encode_datetime(out, value.timestamp)
    out.pack("I", value.request_handle)
    out.pack("I", value.service_result)
    encode_diagnostic_info(out, value.service_diagnostics)
    encode_array(out, value.string_table, encode_string)
    encode_extension_object(out, value.additional_header)




# ResponseHeader cannot be decoded in a constructor expression because the
# string table follows diagnostics.  Keep the explicit wire order here.
def decode_response_header(source: Reader) -> ResponseHeader:
    timestamp = decode_datetime(source)
    handle = source.unpack("I")
    result = source.unpack("I")
    diagnostics = decode_diagnostic_info(source)
    table = decode_array(source, decode_string)
    additional = decode_extension_object(source)
    return ResponseHeader(
        timestamp,
        handle,
        result,
        diagnostics,
        tuple(table) if table is not None else None,
        additional,
    )



def encode_application_description(out: Writer, value: ApplicationDescription) -> None:
    encode_string(out, value.application_uri)
    encode_string(out, value.product_uri)
    encode_localized_text(out, value.application_name)
    out.pack("i", value.application_type)
    encode_string(out, value.gateway_server_uri)
    encode_string(out, value.discovery_profile_uri)
    encode_array(out, value.discovery_urls, encode_string)




# Arrays at the tail make an imperative decoder the clearest wire-order proof.
def decode_application_description(source: Reader) -> ApplicationDescription:
    application_uri = decode_string(source)
    product_uri = decode_string(source)
    application_name = decode_localized_text(source)
    application_type = source.unpack("i")
    gateway = decode_string(source)
    discovery_profile = decode_string(source)
    urls = decode_array(source, decode_string)
    return ApplicationDescription(
        application_uri,
        product_uri,
        application_name,
        application_type,
        gateway,
        discovery_profile,
        tuple(urls) if urls is not None else None,
    )



def encode_signature_data(out: Writer, value: SignatureData) -> None:
    encode_string(out, value.algorithm)
    encode_bytestring(out, value.signature)


def decode_signature_data(source: Reader) -> SignatureData:
    return SignatureData(decode_string(source), decode_bytestring(source))


def encode_signed_certificate(out: Writer, value: SignedSoftwareCertificate) -> None:
    encode_bytestring(out, value.certificate_data)
    encode_bytestring(out, value.signature)


def decode_signed_certificate(source: Reader) -> SignedSoftwareCertificate:
    return SignedSoftwareCertificate(decode_bytestring(source), decode_bytestring(source))


def encode_user_token_policy(out: Writer, value: UserTokenPolicy) -> None:
    encode_string(out, value.policy_id)
    out.pack("i", value.token_type)
    encode_string(out, value.issued_token_type)
    encode_string(out, value.issuer_endpoint_url)
    encode_string(out, value.security_policy_uri)


def decode_user_token_policy(source: Reader) -> UserTokenPolicy:
    return UserTokenPolicy(
        decode_string(source),
        source.unpack("i"),
        decode_string(source),
        decode_string(source),
        decode_string(source),
    )


def encode_endpoint_description(out: Writer, value: EndpointDescription) -> None:
    encode_string(out, value.endpoint_url)
    encode_application_description(out, value.server)
    encode_bytestring(out, value.server_certificate)
    out.pack("i", value.security_mode)
    encode_string(out, value.security_policy_uri)
    encode_array(out, value.user_identity_tokens, encode_user_token_policy)
    encode_string(out, value.transport_profile_uri)
    out.pack("B", value.security_level)


def decode_endpoint_description(source: Reader) -> EndpointDescription:
    endpoint_url = decode_string(source)
    server = decode_application_description(source)
    certificate = decode_bytestring(source)
    mode = source.unpack("i")
    policy = decode_string(source)
    tokens = decode_array(source, decode_user_token_policy)
    profile = decode_string(source)
    level = source.unpack("B")
    return EndpointDescription(
        endpoint_url,
        server,
        certificate,
        mode,
        policy,
        tuple(tokens) if tokens is not None else None,
        profile,
        level,
    )


def encode_channel_token(out: Writer, value: ChannelSecurityToken) -> None:
    out.pack("I", value.channel_id)
    out.pack("I", value.token_id)
    encode_datetime(out, value.created_at)
    out.pack("I", value.revised_lifetime)


def decode_channel_token(source: Reader) -> ChannelSecurityToken:
    return ChannelSecurityToken(
        source.unpack("I"), source.unpack("I"), decode_datetime(source), source.unpack("I")
    )


def encode_view(out: Writer, value: ViewDescription) -> None:
    encode_node_id(out, value.view_id)
    encode_datetime(out, value.timestamp)
    out.pack("I", value.view_version)


def decode_view(source: Reader) -> ViewDescription:
    return ViewDescription(decode_node_id(source), decode_datetime(source), source.unpack("I"))


def encode_browse_description(out: Writer, value: BrowseDescription) -> None:
    encode_node_id(out, value.node_id)
    out.pack("i", value.browse_direction)
    encode_node_id(out, value.reference_type_id)
    out.pack("B", 1 if value.include_subtypes else 0)
    out.pack("I", value.node_class_mask)
    out.pack("I", value.result_mask)


def decode_browse_description(source: Reader) -> BrowseDescription:
    node_id = decode_node_id(source)
    direction = source.unpack("i")
    reference_type = decode_node_id(source)
    include = source.unpack("B")
    if include not in (0, 1):
        raise ValueError("invalid BrowseDescription Boolean")
    return BrowseDescription(
        node_id,
        direction,
        reference_type,
        bool(include),
        source.unpack("I"),
        source.unpack("I"),
    )


def encode_reference(out: Writer, value: ReferenceDescription) -> None:
    encode_node_id(out, value.reference_type_id)
    out.pack("B", 1 if value.is_forward else 0)
    encode_expanded_node_id(out, value.node_id)
    encode_qualified_name(out, value.browse_name)
    encode_localized_text(out, value.display_name)
    out.pack("i", value.node_class)
    encode_expanded_node_id(out, value.type_definition)


def decode_reference(source: Reader) -> ReferenceDescription:
    reference_type = decode_node_id(source)
    forward = source.unpack("B")
    if forward not in (0, 1):
        raise ValueError("invalid ReferenceDescription Boolean")
    return ReferenceDescription(
        reference_type,
        bool(forward),
        decode_expanded_node_id(source),
        decode_qualified_name(source),
        decode_localized_text(source),
        source.unpack("i"),
        decode_expanded_node_id(source),
    )


def encode_browse_result(out: Writer, value: BrowseResult) -> None:
    out.pack("I", value.status)
    encode_bytestring(out, value.continuation_point)
    encode_array(out, value.references, encode_reference)


def decode_browse_result(source: Reader) -> BrowseResult:
    status = source.unpack("I")
    point = decode_bytestring(source)
    references = decode_array(source, decode_reference)
    return BrowseResult(status, point, tuple(references) if references is not None else None)


def encode_read_value_id(out: Writer, value: ReadValueId) -> None:
    encode_node_id(out, value.node_id)
    out.pack("I", value.attribute_id)
    encode_string(out, value.index_range)
    encode_qualified_name(out, value.data_encoding)


def decode_read_value_id(source: Reader) -> ReadValueId:
    return ReadValueId(
        decode_node_id(source),
        source.unpack("I"),
        decode_string(source),
        decode_qualified_name(source),
    )


def _encode_diagnostics(out: Writer, values: tuple[DiagnosticInfo, ...] | None) -> None:
    encode_array(out, values, encode_diagnostic_info)


def _decode_diagnostics(source: Reader) -> tuple[DiagnosticInfo, ...] | None:
    values = decode_array(source, decode_diagnostic_info)
    return tuple(values) if values is not None else None


def encode_service_body(value: object) -> tuple[int, bytes]:
    """Encode exactly one allowlisted service object, returning its type id."""

    out = Writer()
    type_name = type(value).__name__
    try:
        type_id = SERVICE_TYPE_IDS[type_name]
    except KeyError as exc:
        raise TypeError(f"service is outside the read-only allowlist: {type_name}") from exc

    if isinstance(value, OpenSecureChannelRequest):
        encode_request_header(out, value.header)
        out.pack("I", value.client_protocol_version)
        out.pack("i", value.request_type)
        out.pack("i", value.security_mode)
        encode_bytestring(out, value.client_nonce)
        out.pack("I", value.requested_lifetime)
    elif isinstance(value, OpenSecureChannelResponse):
        encode_response_header(out, value.header)
        out.pack("I", value.server_protocol_version)
        encode_channel_token(out, value.token)
        encode_bytestring(out, value.server_nonce)
    elif isinstance(value, CreateSessionRequest):
        encode_request_header(out, value.header)
        encode_application_description(out, value.client_description)
        encode_string(out, value.server_uri)
        encode_string(out, value.endpoint_url)
        encode_string(out, value.session_name)
        encode_bytestring(out, value.client_nonce)
        encode_bytestring(out, value.client_certificate)
        out.pack("d", value.requested_session_timeout)
        out.pack("I", value.max_response_message_size)
    elif isinstance(value, CreateSessionResponse):
        encode_response_header(out, value.header)
        encode_node_id(out, value.session_id)
        encode_node_id(out, value.authentication_token)
        out.pack("d", value.revised_session_timeout)
        encode_bytestring(out, value.server_nonce)
        encode_bytestring(out, value.server_certificate)
        encode_array(out, value.server_endpoints, encode_endpoint_description)
        encode_array(out, value.server_software_certificates, encode_signed_certificate)
        encode_signature_data(out, value.server_signature)
        out.pack("I", value.max_request_message_size)
    elif isinstance(value, ActivateSessionRequest):
        encode_request_header(out, value.header)
        encode_signature_data(out, value.client_signature)
        encode_array(out, value.client_software_certificates, encode_signed_certificate)
        encode_array(out, value.locale_ids, encode_string)
        encode_extension_object(out, value.user_identity_token)
        encode_signature_data(out, value.user_token_signature)
    elif isinstance(value, ActivateSessionResponse):
        encode_response_header(out, value.header)
        encode_bytestring(out, value.server_nonce)
        encode_array(out, value.results, lambda target, item: target.pack("I", item))
        _encode_diagnostics(out, value.diagnostic_infos)
    elif isinstance(value, BrowseRequest):
        encode_request_header(out, value.header)
        encode_view(out, value.view)
        out.pack("I", value.requested_max_references_per_node)
        encode_array(out, value.nodes, encode_browse_description)
    elif isinstance(value, BrowseResponse):
        encode_response_header(out, value.header)
        encode_array(out, value.results, encode_browse_result)
        _encode_diagnostics(out, value.diagnostic_infos)
    elif isinstance(value, BrowseNextRequest):
        encode_request_header(out, value.header)
        out.pack("B", 1 if value.release_continuation_points else 0)
        encode_array(out, value.continuation_points, encode_bytestring)
    elif isinstance(value, BrowseNextResponse):
        encode_response_header(out, value.header)
        encode_array(out, value.results, encode_browse_result)
        _encode_diagnostics(out, value.diagnostic_infos)
    elif isinstance(value, ReadRequest):
        encode_request_header(out, value.header)
        out.pack("d", value.max_age)
        out.pack("i", value.timestamps_to_return)
        encode_array(out, value.nodes, encode_read_value_id)
    elif isinstance(value, ReadResponse):
        encode_response_header(out, value.header)
        encode_array(out, value.results, encode_data_value)
        _encode_diagnostics(out, value.diagnostic_infos)
    elif isinstance(value, CloseSessionRequest):
        encode_request_header(out, value.header)
        out.pack("B", 1 if value.delete_subscriptions else 0)
    elif isinstance(value, (CloseSessionResponse, CloseSecureChannelResponse, ServiceFault)):
        encode_response_header(out, value.header)
    elif isinstance(value, CloseSecureChannelRequest):
        encode_request_header(out, value.header)
    else:  # pragma: no cover - guarded by the type-id lookup above
        raise TypeError(type_name)
    return type_id, out.finish()




def decode_service_body(type_id: int, payload: bytes) -> object:
    """Decode exactly one allowlisted service body by DefaultBinary type id."""

    source = Reader(payload)
    type_name = next((name for name, value in SERVICE_TYPE_IDS.items() if value == type_id), None)
    if type_name is None:
        raise ValueError(f"service type id is outside the read-only allowlist: {type_id}")

    if type_name == "OpenSecureChannelRequest":
        result: object = OpenSecureChannelRequest(
            decode_request_header(source),
            source.unpack("I"),
            source.unpack("i"),
            source.unpack("i"),
            decode_bytestring(source),
            source.unpack("I"),
        )
    elif type_name == "OpenSecureChannelResponse":
        result = OpenSecureChannelResponse(
            decode_response_header(source),
            source.unpack("I"),
            decode_channel_token(source),
            decode_bytestring(source),
        )
    elif type_name == "CreateSessionRequest":
        result = CreateSessionRequest(
            decode_request_header(source),
            decode_application_description(source),
            decode_string(source),
            decode_string(source),
            decode_string(source),
            decode_bytestring(source),
            decode_bytestring(source),
            source.unpack("d"),
            source.unpack("I"),
        )
    elif type_name == "CreateSessionResponse":
        header = decode_response_header(source)
        session_id = decode_node_id(source)
        auth = decode_node_id(source)
        timeout = source.unpack("d")
        nonce = decode_bytestring(source)
        certificate = decode_bytestring(source)
        endpoints = decode_array(source, decode_endpoint_description)
        software = decode_array(source, decode_signed_certificate)
        signature = decode_signature_data(source)
        result = CreateSessionResponse(
            header,
            session_id,
            auth,
            timeout,
            nonce,
            certificate,
            tuple(endpoints) if endpoints is not None else None,
            tuple(software) if software is not None else None,
            signature,
            source.unpack("I"),
        )
    elif type_name == "ActivateSessionRequest":
        certificates = None
        locales = None
        header = decode_request_header(source)
        signature = decode_signature_data(source)
        certificates_raw = decode_array(source, decode_signed_certificate)
        locales_raw = decode_array(source, decode_string)
        certificates = tuple(certificates_raw) if certificates_raw is not None else None
        locales = tuple(locales_raw) if locales_raw is not None else None
        result = ActivateSessionRequest(
            header,
            signature,
            certificates,
            locales,
            decode_extension_object(source),
            decode_signature_data(source),
        )
    elif type_name == "ActivateSessionResponse":
        header = decode_response_header(source)
        nonce = decode_bytestring(source)
        results_raw = decode_array(source, lambda item: item.unpack("I"))
        result = ActivateSessionResponse(
            header,
            nonce,
            tuple(results_raw) if results_raw is not None else None,
            _decode_diagnostics(source),
        )
    elif type_name == "BrowseRequest":
        header = decode_request_header(source)
        view = decode_view(source)
        maximum = source.unpack("I")
        nodes = decode_array(source, decode_browse_description)
        result = BrowseRequest(header, view, maximum, tuple(nodes) if nodes is not None else None)
    elif type_name == "BrowseResponse":
        header = decode_response_header(source)
        rows = decode_array(source, decode_browse_result)
        result = BrowseResponse(
            header, tuple(rows) if rows is not None else None, _decode_diagnostics(source)
        )
    elif type_name == "BrowseNextRequest":
        header = decode_request_header(source)
        release = source.unpack("B")
        if release not in (0, 1):
            raise ValueError("invalid BrowseNext Boolean")
        points = decode_array(source, decode_bytestring)
        result = BrowseNextRequest(
            header, bool(release), tuple(points) if points is not None else None
        )
    elif type_name == "BrowseNextResponse":
        header = decode_response_header(source)
        rows = decode_array(source, decode_browse_result)
        result = BrowseNextResponse(
            header, tuple(rows) if rows is not None else None, _decode_diagnostics(source)
        )
    elif type_name == "ReadRequest":
        header = decode_request_header(source)
        max_age = source.unpack("d")
        timestamps = source.unpack("i")
        nodes = decode_array(source, decode_read_value_id)
        result = ReadRequest(
            header, max_age, timestamps, tuple(nodes) if nodes is not None else None
        )
    elif type_name == "ReadResponse":
        header = decode_response_header(source)
        values = decode_array(source, decode_data_value)
        result = ReadResponse(
            header, tuple(values) if values is not None else None, _decode_diagnostics(source)
        )
    elif type_name == "CloseSessionRequest":
        header = decode_request_header(source)
        flag = source.unpack("B")
        if flag not in (0, 1):
            raise ValueError("invalid CloseSession Boolean")
        result = CloseSessionRequest(header, bool(flag))
    elif type_name == "CloseSessionResponse":
        result = CloseSessionResponse(decode_response_header(source))
    elif type_name == "CloseSecureChannelRequest":
        result = CloseSecureChannelRequest(decode_request_header(source))
    elif type_name == "CloseSecureChannelResponse":
        result = CloseSecureChannelResponse(decode_response_header(source))
    elif type_name == "ServiceFault":
        result = ServiceFault(decode_response_header(source))
    else:  # pragma: no cover - exhaustive over SERVICE_TYPE_IDS
        raise ValueError(type_name)
    source.require_finished()
    return result


__all__ = tuple(
    sorted(
        name
        for name in globals()
        if name.endswith(("Request", "Response", "Description", "Result", "Header"))
        or name
        in {
            "ApplicationDescription",
            "ChannelSecurityToken",
            "GOOD",
            "ReferenceDescription",
            "SECURITY_POLICY_NONE",
            "SERVICE_TYPE_IDS",
            "SignatureData",
            "SignedSoftwareCertificate",
            "UserTokenPolicy",
            "VALUE_ATTRIBUTE_ID",
            "ViewDescription",
            "decode_service_body",
            "encode_service_body",
        }
    )
)
