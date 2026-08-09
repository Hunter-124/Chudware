"""Strictly read-only OPC UA client over UA-TCP and SecurityPolicy None.

Implemented service surface: secure-channel/session establishment, browse with
continuations, attribute reads, and orderly session/channel closure.  There is
no generic service dispatcher: request objects must belong to the explicit
allowlist in :mod:`protocol`.
"""

from __future__ import annotations
import socket
import struct
from typing import Iterable
from urllib.parse import urlsplit

from .codec import (
    ANONYMOUS_IDENTITY_TOKEN_BINARY,
    AnonymousIdentityToken,
    CodecError,
    ExtensionObject,
    NodeId,
    Reader,
    Writer,
    decode_bytestring,
    decode_node_id,
    decode_string,
    encode_bytestring,
    encode_node_id,
    encode_string,
)
from .protocol import (
    ActivateSessionRequest,
    ActivateSessionResponse,
    ApplicationDescription,
    BrowseDescription,
    BrowseNextRequest,
    BrowseNextResponse,
    BrowseRequest,
    BrowseResponse,
    CloseSecureChannelRequest,
    CloseSessionRequest,
    CloseSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    GOOD,
    OpenSecureChannelRequest,
    OpenSecureChannelResponse,
    ReadRequest,
    ReadResponse,
    ReadValueId,
    RequestHeader,
    SECURITY_POLICY_NONE,
    ServiceFault,
    ViewDescription,
    decode_service_body,
    encode_service_body,
    LocalizedText,
)


class UaClientError(RuntimeError):
    """Base class for stable client failures."""


class UaProtocolError(UaClientError):
    """Peer bytes violate UA-TCP framing, negotiated bounds, or typed service shape."""


class UaServiceError(UaClientError):
    """An OPC UA service returned a non-Good StatusCode."""

    def __init__(self, status: int, service: str) -> None:
        self.status = status
        self.service = service
        super().__init__(f"{service} failed with StatusCode 0x{status:08X}")


class UaTcpClient:
    """One synchronous anonymous OPC UA session with no mutating surface."""

    __slots__ = (
        "endpoint",
        "timeout",
        "receive_buffer_size",
        "send_buffer_size",
        "max_message_size",
        "max_chunk_count",
        "_sock",
        "_channel_id",
        "_token_id",
        "_authentication_token",
        "_anonymous_policy_id",
        "_sequence_number",
        "_last_server_sequence",
        "_request_id",
        "_request_handle",
        "_connected",
        "_session_open",
    )

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 5.0,
        receive_buffer_size: int = 65_536,
        send_buffer_size: int = 65_536,
        max_message_size: int = 4 * 1024 * 1024,
        max_chunk_count: int = 64,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme.casefold() != "opc.tcp" or not parsed.hostname or parsed.port is None:
            raise ValueError("endpoint must be opc.tcp://host:port[/path]")
        if receive_buffer_size < 8_192 or send_buffer_size < 8_192:
            raise ValueError("UA-TCP buffers must be at least 8192 bytes")
        if max_message_size < receive_buffer_size or max_chunk_count < 1:
            raise ValueError("invalid UA-TCP message bounds")
        self.endpoint = endpoint
        self.timeout = timeout
        self.receive_buffer_size = receive_buffer_size
        self.send_buffer_size = send_buffer_size
        self.max_message_size = max_message_size
        self.max_chunk_count = max_chunk_count
        self._sock: socket.socket | None = None
        self._channel_id = 0
        self._token_id = 0
        self._authentication_token = NodeId()
        self._anonymous_policy_id: str | None = None
        self._sequence_number = 1
        self._last_server_sequence: int | None = None
        self._request_id = 1
        self._request_handle = 1
        self._connected = False
        self._session_open = False

    def __enter__(self) -> "UaTcpClient":
        return self.connect()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._connected and self._sock is not None

    def connect(self) -> "UaTcpClient":
        if self.connected:
            return self
        parsed = urlsplit(self.endpoint)
        assert parsed.hostname is not None and parsed.port is not None
        try:
            self._sock = socket.create_connection(
                (parsed.hostname, parsed.port), timeout=self.timeout
            )
            self._sock.settimeout(self.timeout)
            self._hello()
            self._open_channel()
            self._create_session()
            self._activate_session()
            self._connected = True
            self._session_open = True
            return self
        except (OSError, CodecError, ValueError, UaClientError) as exc:
            self._drop_transport()
            if isinstance(exc, UaClientError):
                raise
            raise UaProtocolError(str(exc)) from exc

    def browse(
        self,
        nodes: Iterable[NodeId | BrowseDescription],
        *,
        max_references_per_node: int = 0,
    ) -> tuple:
        descriptions = tuple(
            item if isinstance(item, BrowseDescription) else BrowseDescription(item)
            for item in nodes
        )
        request = BrowseRequest(
            self._header(), ViewDescription(), max_references_per_node, descriptions
        )
        response = self._exchange(request, BrowseResponse)
        assert isinstance(response, BrowseResponse)
        return response.results or ()

    def browse_next(
        self, continuation_points: Iterable[bytes | None], *, release: bool = False
    ) -> tuple:
        request = BrowseNextRequest(
            self._header(), release, tuple(continuation_points)
        )
        response = self._exchange(request, BrowseNextResponse)
        assert isinstance(response, BrowseNextResponse)
        return response.results or ()

    def read(self, nodes: Iterable[NodeId | ReadValueId]) -> tuple:
        values = tuple(
            item if isinstance(item, ReadValueId) else ReadValueId(item) for item in nodes
        )
        request = ReadRequest(self._header(), 0.0, 2, values)
        response = self._exchange(request, ReadResponse)
        assert isinstance(response, ReadResponse)
        return response.results or ()

    def close(self) -> None:
        transport = self._sock
        if transport is None:
            return
        try:
            if self._session_open:
                request = CloseSessionRequest(self._header(), True)
                response = self._exchange(request, CloseSessionResponse)
                assert isinstance(response, CloseSessionResponse)
                self._session_open = False
            channel_request = CloseSecureChannelRequest(self._header())
            self._send_service(channel_request, b"CLO")
        except (OSError, UaClientError, CodecError, ValueError):
            pass
        finally:
            self._drop_transport()

    def _drop_transport(self) -> None:
        transport, self._sock = self._sock, None
        self._connected = False
        self._session_open = False
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            transport.close()

    def _next_request_id(self) -> int:
        value = self._request_id
        self._request_id = 1 if value == 0xFFFFFFFF else value + 1
        return value

    def _header(self, *, authenticated: bool = True) -> RequestHeader:
        value = RequestHeader(
            authentication_token=(
                self._authentication_token if authenticated else NodeId()
            ),
            request_handle=self._request_handle,
            timeout_hint=max(1, int(self.timeout * 1000)),
        )
        self._request_handle = 1 if self._request_handle == 0xFFFFFFFF else self._request_handle + 1
        return value

    def _hello(self) -> None:
        out = Writer(max_allocation=self.max_message_size)
        out.pack("I", 0)
        out.pack("I", self.receive_buffer_size)
        out.pack("I", self.send_buffer_size)
        out.pack("I", self.max_message_size)
        out.pack("I", self.max_chunk_count)
        encode_string(out, self.endpoint)
        body = out.finish()
        self._send_frame(b"HELF", body)

        message_type, body = self._receive_frame(self.receive_buffer_size)
        if message_type == b"ERRF":
            source = Reader(body)
            status = source.unpack("I")
            reason = decode_string(source)
            raise UaProtocolError(f"UA-TCP Error 0x{status:08X}: {reason or ''}")
        if message_type != b"ACKF" or len(body) != 20:
            raise UaProtocolError("expected a 20-byte UA-TCP ACK body")
        source = Reader(body)
        protocol_version = source.unpack("I")
        remote_receive = source.unpack("I")
        remote_send = source.unpack("I")
        remote_max_message = source.unpack("I")
        remote_max_chunks = source.unpack("I")
        if protocol_version != 0:
            raise UaProtocolError(f"unsupported UA-TCP protocol version {protocol_version}")
        if remote_receive < 8_192 or remote_send < 8_192:
            raise UaProtocolError("peer ACK advertises undersized buffers")
        if remote_max_message:
            self.max_message_size = min(self.max_message_size, remote_max_message)
        if remote_max_chunks:
            self.max_chunk_count = min(self.max_chunk_count, remote_max_chunks)
        self.send_buffer_size = min(self.send_buffer_size, remote_receive)
        self.receive_buffer_size = min(self.receive_buffer_size, remote_send)

    def _open_channel(self) -> None:
        request = OpenSecureChannelRequest(self._header(authenticated=False))
        response = self._exchange(request, OpenSecureChannelResponse, b"OPN")
        assert isinstance(response, OpenSecureChannelResponse)
        if response.token.channel_id == 0 or response.token.token_id == 0:
            raise UaProtocolError("server returned an invalid secure-channel token")
        self._channel_id = response.token.channel_id
        self._token_id = response.token.token_id

    def _create_session(self) -> None:
        description = ApplicationDescription(
            "urn:chudware:manufacturing:readonly-opcua",
            "urn:chudware:manufacturing",
            LocalizedText("en-US", "Chudware read-only monitor"),
            1,
            discovery_urls=(),
        )
        request = CreateSessionRequest(
            self._header(authenticated=False),
            description,
            None,
            self.endpoint,
            "Chudware read-only monitor",
            None,
            None,
            600_000.0,
            self.max_message_size,
        )
        response = self._exchange(request, CreateSessionResponse)
        assert isinstance(response, CreateSessionResponse)
        if response.authentication_token == NodeId():
            raise UaProtocolError("CreateSession returned a null authentication token")
        self._authentication_token = response.authentication_token
        endpoints = response.server_endpoints or ()
        anonymous_policies = [
            token.policy_id
            for endpoint in endpoints
            if endpoint.security_mode == 1
            and endpoint.security_policy_uri == SECURITY_POLICY_NONE
            for token in (endpoint.user_identity_tokens or ())
            if token.token_type == 0
            and token.security_policy_uri in (None, "", SECURITY_POLICY_NONE)
        ]
        if endpoints and not anonymous_policies:
            raise UaProtocolError(
                "CreateSession returned no anonymous SecurityPolicy None endpoint"
            )
        self._anonymous_policy_id = anonymous_policies[0] if anonymous_policies else None

    def _activate_session(self) -> None:
        identity = ExtensionObject(
            ANONYMOUS_IDENTITY_TOKEN_BINARY,
            AnonymousIdentityToken(self._anonymous_policy_id),
            1,
        )
        request = ActivateSessionRequest(
            self._header(), user_identity_token=identity
        )
        response = self._exchange(request, ActivateSessionResponse)
        assert isinstance(response, ActivateSessionResponse)

    def _exchange(
        self,
        request: object,
        expected_type: type,
        message_type: bytes = b"MSG",
    ) -> object:
        request_id = self._send_service(request, message_type)
        response = self._receive_service(request_id, message_type)
        if response.header.request_handle != request.header.request_handle:
            raise UaProtocolError("response request handle does not match request")
        if isinstance(response, ServiceFault):
            raise UaServiceError(response.header.service_result, type(request).__name__)
        if not isinstance(response, expected_type):
            raise UaProtocolError(
                f"expected {expected_type.__name__}, received {type(response).__name__}"
            )
        header = response.header
        if header.service_result != GOOD:
            raise UaServiceError(header.service_result, type(request).__name__)
        return response

    def _send_service(self, request: object, message_type: bytes) -> int:
        type_id, body = encode_service_body(request)
        service = Writer(max_allocation=self.max_message_size)
        encode_node_id(service, NodeId(0, type_id))
        service.put(body)
        payload = service.finish()
        request_id = self._next_request_id()
        overhead = 12 + (12 if message_type == b"OPN" else 4) + 8
        if len(payload) + overhead > self.send_buffer_size:
            raise UaProtocolError("request requires chunking, outside this bounded client scope")
        security = Writer(max_allocation=self.max_message_size)
        if message_type == b"OPN":
            encode_string(security, SECURITY_POLICY_NONE)
            encode_bytestring(security, None)
            encode_bytestring(security, None)
        else:
            security.pack("I", self._token_id)
        security.pack("I", self._sequence_number)
        security.pack("I", request_id)
        security.put(payload)
        self._sequence_number = 1 if self._sequence_number == 0xFFFFFFFF else self._sequence_number + 1
        outer = struct.pack("<I", self._channel_id) + security.finish()
        self._send_frame(message_type + b"F", outer)
        return request_id

    def _receive_service(self, request_id: int, expected_message_type: bytes) -> object:
        fragments: list[bytes] = []
        total = 0
        channel_seen: int | None = None
        for chunk_index in range(self.max_chunk_count):
            frame_type, frame = self._receive_frame(self.receive_buffer_size)
            message_type, chunk_type = frame_type[:3], frame_type[3:4]
            if message_type != expected_message_type:
                raise UaProtocolError(
                    f"expected {expected_message_type.decode()} chunk, got {message_type.decode(errors='replace')}"
                )
            if chunk_type == b"A":
                raise UaProtocolError("server aborted the OPC UA message")
            if chunk_type not in (b"C", b"F"):
                raise UaProtocolError("invalid OPC UA chunk type")
            source = Reader(frame, max_allocation=self.max_message_size)
            channel_id = source.unpack("I")
            if channel_seen is None:
                channel_seen = channel_id
            elif channel_seen != channel_id:
                raise UaProtocolError("secure-channel id changed within a message")
            if message_type != b"OPN" and channel_id != self._channel_id:
                raise UaProtocolError("response used an unexpected secure-channel id")
            if message_type == b"OPN":
                policy = decode_string(source)
                decode_bytestring(source)
                decode_bytestring(source)
                if policy != SECURITY_POLICY_NONE:
                    raise UaProtocolError("server selected a security policy outside #None")
            else:
                token_id = source.unpack("I")
                if token_id != self._token_id:
                    raise UaProtocolError("unexpected secure-channel token id")
            sequence = source.unpack("I")
            received_request_id = source.unpack("I")
            if received_request_id != request_id:
                raise UaProtocolError("response request id does not match request")
            if self._last_server_sequence is not None:
                expected_sequence = (
                    1 if self._last_server_sequence == 0xFFFFFFFF else self._last_server_sequence + 1
                )
                if sequence != expected_sequence:
                    raise UaProtocolError("server sequence number is not contiguous")
            self._last_server_sequence = sequence
            fragment = source.take(source.remaining)
            total += len(fragment)
            if total > self.max_message_size:
                raise UaProtocolError("reassembled OPC UA message exceeds limit")
            fragments.append(fragment)
            if chunk_type == b"F":
                break
        else:
            raise UaProtocolError("OPC UA message exceeds chunk-count limit")

        payload = b"".join(fragments)
        source = Reader(payload, max_allocation=self.max_message_size)
        service_node = decode_node_id(source)
        if service_node.namespace != 0 or not isinstance(service_node.identifier, int):
            raise UaProtocolError("service type id must be a numeric Namespace 0 NodeId")
        body = source.take(source.remaining)
        try:
            return decode_service_body(service_node.identifier, body)
        except (CodecError, ValueError) as exc:
            raise UaProtocolError(str(exc)) from exc

    def _send_frame(self, message_type: bytes, body: bytes) -> None:
        if len(message_type) != 4:
            raise UaProtocolError("UA-TCP message type must be four bytes")
        size = 8 + len(body)
        if size > self.max_message_size:
            raise UaProtocolError("outgoing UA-TCP frame exceeds message limit")
        transport = self._sock
        if transport is None:
            raise UaProtocolError("client is not connected")
        transport.sendall(message_type + struct.pack("<I", size) + body)

    def _receive_frame(self, chunk_limit: int) -> tuple[bytes, bytes]:
        header = self._receive_exact(8)
        message_type = header[:4]
        size = struct.unpack_from("<I", header, 4)[0]
        if size < 8:
            raise UaProtocolError("UA-TCP frame size is smaller than its header")
        if size > chunk_limit or size > self.max_message_size:
            raise UaProtocolError("UA-TCP chunk exceeds negotiated limit")
        return message_type, self._receive_exact(size - 8)

    def _receive_exact(self, size: int) -> bytes:
        transport = self._sock
        if transport is None:
            raise UaProtocolError("client is not connected")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            block = transport.recv(remaining)
            if not block:
                raise UaProtocolError("connection closed during UA-TCP frame")
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)


__all__ = ("UaClientError", "UaProtocolError", "UaServiceError", "UaTcpClient")
