"""In-process UA-TCP peer for deterministic no-network OPC UA tests."""

from __future__ import annotations

from datetime import datetime, timezone
import socket
import struct
import threading
from typing import Any

from .codec import (
    AnonymousIdentityToken,
    DOUBLE,
    UINT32,
    DataValue,
    ExpandedNodeId,
    LocalizedText,
    NodeId,
    QualifiedName,
    Reader,
    Variant,
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
    ApplicationDescription,
    ActivateSessionResponse,
    BrowseNextRequest,
    BrowseNextResponse,
    BrowseRequest,
    BrowseResponse,
    BrowseResult,
    ChannelSecurityToken,
    CloseSecureChannelRequest,
    CloseSessionRequest,
    CloseSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    EndpointDescription,
    OpenSecureChannelRequest,
    OpenSecureChannelResponse,
    ReadRequest,
    ReadResponse,
    ReferenceDescription,
    ResponseHeader,
    SECURITY_POLICY_NONE,
    SignatureData,
    UserTokenPolicy,
    decode_service_body,
    encode_service_body,
)

STAMP = datetime(2026, 8, 8, 12, 0, 0, 123456, tzinfo=timezone.utc)
BAD_NODE_ID_UNKNOWN = 0x80340000


class FakeOpcUaServer:
    """One-connection anonymous server implementing the client's closed service set."""

    def __init__(
        self,
        *,
        response_fragment_size: int | None = None,
        malformed: str | None = None,
    ) -> None:
        self.response_fragment_size = response_fragment_size
        self.malformed = malformed
        self.events: list[str] = []
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(2.0)
        host, port = self._listener.getsockname()
        self.endpoint = f"opc.tcp://{host}:{port}/machine"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._server_sequence = 100
        self._channel_id = 77
        self._token_id = 88

    def __enter__(self) -> "FakeOpcUaServer":
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._listener.close()
        self._thread.join(timeout=3.0)
        if exc is None and self.error is not None:
            raise AssertionError("fake OPC UA server failed") from self.error
        if exc is None and self._thread.is_alive():
            raise AssertionError("fake OPC UA server did not terminate")

    @staticmethod
    def _exact(connection: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        while size:
            block = connection.recv(size)
            if not block:
                raise EOFError("client closed during frame")
            chunks.append(block)
            size -= len(block)
        return b"".join(chunks)

    @classmethod
    def _frame(cls, connection: socket.socket) -> tuple[bytes, bytes]:
        header = cls._exact(connection, 8)
        size = struct.unpack_from("<I", header, 4)[0]
        if size < 8 or size > 4 * 1024 * 1024:
            raise ValueError("invalid client frame size")
        return header[:4], cls._exact(connection, size - 8)

    @staticmethod
    def _send_frame(connection: socket.socket, kind: bytes, body: bytes) -> None:
        connection.sendall(kind + struct.pack("<I", 8 + len(body)) + body)

    def _serve(self) -> None:
        try:
            connection, _ = self._listener.accept()
            with connection:
                connection.settimeout(2.0)
                kind, hello = self._frame(connection)
                if kind != b"HELF":
                    raise ValueError("expected HEL")
                source = Reader(hello)
                values = [source.unpack("I") for _ in range(5)]
                endpoint = decode_string(source)
                source.require_finished()
                if values[0] != 0 or endpoint != self.endpoint:
                    raise ValueError("invalid HEL body")
                self.events.append("HEL")
                acknowledgement = struct.pack("<IIIII", 0, 65_536, 65_536, 4 * 1024 * 1024, 64)
                self._send_frame(connection, b"ACKF", acknowledgement)

                while True:
                    kind, frame = self._frame(connection)
                    request, request_id, message_type = self._decode_request(kind, frame)
                    self.events.append(type(request).__name__)
                    if isinstance(request, CloseSecureChannelRequest):
                        return
                    response = self._response(request)
                    if self.malformed == "oversized" and isinstance(
                        request, OpenSecureChannelRequest
                    ):
                        connection.sendall(b"OPNF" + struct.pack("<I", 70_000))
                        return
                    if self.malformed == "chunk_type" and isinstance(
                        request, OpenSecureChannelRequest
                    ):
                        self._send_response(connection, response, request_id, message_type, b"X")
                        return
                    self._send_response(connection, response, request_id, message_type)
        except BaseException as exc:  # surfaced by the context manager
            self.error = exc

    def _decode_request(
        self, kind: bytes, frame: bytes
    ) -> tuple[object, int, bytes]:
        message_type, chunk_type = kind[:3], kind[3:4]
        if chunk_type != b"F" or message_type not in (b"OPN", b"MSG", b"CLO"):
            raise ValueError("unexpected client chunk")
        source = Reader(frame)
        source.unpack("I")
        if message_type == b"OPN":
            if decode_string(source) != SECURITY_POLICY_NONE:
                raise ValueError("client did not use SecurityPolicy None")
            decode_bytestring(source)
            decode_bytestring(source)
        else:
            token = source.unpack("I")
            if token != self._token_id:
                raise ValueError("client used unexpected token")
        source.unpack("I")
        request_id = source.unpack("I")
        service_id = decode_node_id(source)
        if service_id.namespace != 0 or not isinstance(service_id.identifier, int):
            raise ValueError("invalid service NodeId")
        request = decode_service_body(service_id.identifier, source.take(source.remaining))
        return request, request_id, message_type

    @staticmethod
    def _header(request: Any) -> ResponseHeader:
        return ResponseHeader(STAMP, request.header.request_handle)

    def _response(self, request: object) -> object:
        if isinstance(request, OpenSecureChannelRequest):
            return OpenSecureChannelResponse(
                self._header(request),
                0,
                ChannelSecurityToken(
                    self._channel_id, self._token_id, STAMP, request.requested_lifetime
                ),
                None,
            )
        if isinstance(request, CreateSessionRequest):
            endpoint = EndpointDescription(
                self.endpoint,
                ApplicationDescription(
                    "urn:fixture:opcua-server",
                    "urn:fixture:opcua",
                    LocalizedText("en-US", "Fixture OPC UA server"),
                    0,
                    discovery_urls=(self.endpoint,),
                ),
                None,
                1,
                SECURITY_POLICY_NONE,
                (
                    UserTokenPolicy(
                        "anonymous", 0, security_policy_uri=SECURITY_POLICY_NONE
                    ),
                ),
                "http://opcfoundation.org/UA-Profile/Transport/uatcp-uasc-uabinary",
                0,
            )
            return CreateSessionResponse(
                self._header(request),
                NodeId(1, 100),
                NodeId(1, 101),
                request.requested_session_timeout,
                None,
                None,
                (endpoint,),
                (),
                SignatureData(),
                4 * 1024 * 1024,
            )
        if isinstance(request, ActivateSessionRequest):
            if not isinstance(
                request.user_identity_token.body, AnonymousIdentityToken
            ) or request.user_identity_token.body.policy_id != "anonymous":
                raise ValueError("client did not activate with anonymous policy")
            return ActivateSessionResponse(self._header(request), None, (), ())
        if isinstance(request, BrowseRequest):
            first = ReferenceDescription(
                NodeId(0, 33),
                True,
                ExpandedNodeId(NodeId(2, 1001)),
                QualifiedName(2, "ActualSpeed"),
                LocalizedText("en-US", "Actual speed"),
                2,
                ExpandedNodeId(NodeId(0, 63)),
            )
            return BrowseResponse(
                self._header(request),
                (BrowseResult(0, b"page-2", (first,)),),
                (),
            )
        if isinstance(request, BrowseNextRequest):
            second = ReferenceDescription(
                NodeId(0, 33),
                True,
                ExpandedNodeId(NodeId(2, 1002)),
                QualifiedName(2, "Parts"),
                LocalizedText(None, "Parts"),
                2,
                ExpandedNodeId(NodeId(0, 63)),
            )
            return BrowseNextResponse(
                self._header(request),
                (BrowseResult(0, None, (second,)),),
                (),
            )
        if isinstance(request, ReadRequest):
            values = []
            for item in request.nodes or ():
                identifier = item.node_id.identifier
                if identifier == 1001:
                    values.append(DataValue(Variant(DOUBLE, 9000.5), 0, STAMP, STAMP))
                elif identifier == 1002:
                    values.append(
                        DataValue(Variant(UINT32, [1, 2, 3], True), 0, STAMP, STAMP)
                    )
                else:
                    values.append(
                        DataValue(Variant(DOUBLE, 123.0), BAD_NODE_ID_UNKNOWN, STAMP, STAMP)
                    )
            return ReadResponse(self._header(request), tuple(values), ())
        if isinstance(request, CloseSessionRequest):
            return CloseSessionResponse(self._header(request))
        raise TypeError(type(request).__name__)

    def _send_response(
        self,
        connection: socket.socket,
        response: object,
        request_id: int,
        message_type: bytes,
        forced_chunk_type: bytes | None = None,
    ) -> None:
        type_id, body = encode_service_body(response)
        payload_writer = Writer()
        encode_node_id(payload_writer, NodeId(0, type_id))
        payload_writer.put(body)
        payload = payload_writer.finish()
        fragment_size = self.response_fragment_size or len(payload) or 1
        fragments = [
            payload[offset : offset + fragment_size]
            for offset in range(0, len(payload), fragment_size)
        ] or [b""]
        for index, fragment in enumerate(fragments):
            chunk_type = forced_chunk_type or (b"F" if index == len(fragments) - 1 else b"C")
            security = Writer()
            if message_type == b"OPN":
                encode_string(security, SECURITY_POLICY_NONE)
                encode_bytestring(security, None)
                encode_bytestring(security, None)
            else:
                security.pack("I", self._token_id)
            security.pack("I", self._server_sequence)
            security.pack("I", request_id)
            security.put(fragment)
            self._server_sequence += 1
            outer = struct.pack("<I", self._channel_id) + security.finish()
            self._send_frame(connection, message_type + chunk_type, outer)


__all__ = ("BAD_NODE_ID_UNKNOWN", "FakeOpcUaServer", "STAMP")
