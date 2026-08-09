from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .client import UaProtocolError, UaTcpClient
from .codec import (
    DOUBLE,
    UINT32,
    NodeId,
    Reader,
    Writer,
    decode_node_id,
    encode_node_id,
)
from .fake_server import BAD_NODE_ID_UNKNOWN, FakeOpcUaServer
from .protocol import (
    SERVICE_TYPE_IDS,
    ReadRequest,
    decode_service_body,
    encode_service_body,
)


EXPECTED_LIFECYCLE = [
    "HEL",
    "OpenSecureChannelRequest",
    "CreateSessionRequest",
    "ActivateSessionRequest",
    "BrowseRequest",
    "BrowseNextRequest",
    "ReadRequest",
    "CloseSessionRequest",
    "CloseSecureChannelRequest",
]

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "mfg" / "monitor" / "opcua"


def test_handshake_session_browse_continuation_scalar_array_and_close() -> None:
    server = FakeOpcUaServer()
    with server:
        with UaTcpClient(server.endpoint) as client:
            first = client.browse([NodeId(0, 85)], max_references_per_node=1)
            assert first[0].references[0].browse_name.name == "ActualSpeed"
            assert first[0].continuation_point == b"page-2"

            second = client.browse_next([first[0].continuation_point])
            assert second[0].references[0].browse_name.name == "Parts"
            assert second[0].continuation_point is None

            scalar, array, bad = client.read(
                [NodeId(2, 1001), NodeId(2, 1002), NodeId(2, 9999)]
            )
            assert scalar.value.type_id == DOUBLE
            assert scalar.value.value == 9000.5
            assert array.value.type_id == UINT32
            assert array.value.is_array is True
            assert array.value.value == [1, 2, 3]
            assert bad.status == BAD_NODE_ID_UNKNOWN
    assert server.events == EXPECTED_LIFECYCLE


def test_chunked_secure_messages_are_reassembled_before_service_decode() -> None:
    server = FakeOpcUaServer(response_fragment_size=32)
    with server:
        with UaTcpClient(server.endpoint) as client:
            value = client.read([NodeId(2, 1001)])[0]
            assert value.value.value == 9000.5
    assert server.events[-2:] == ["CloseSessionRequest", "CloseSecureChannelRequest"]


@pytest.mark.parametrize(
    ("malformed", "message"),
    (("oversized", "exceeds negotiated limit"), ("chunk_type", "invalid OPC UA chunk type")),
)
def test_oversized_and_invalid_chunks_are_refused_before_service_decode(
    malformed: str, message: str
) -> None:
    with FakeOpcUaServer(malformed=malformed) as server:
        with pytest.raises(UaProtocolError, match=message):
            UaTcpClient(server.endpoint).connect()


def test_recorded_read_request_bytes_decode_and_reencode_exactly() -> None:
    text = (FIXTURES / "read_request.hex").read_text(encoding="utf-8")
    recorded = bytes.fromhex("".join(line for line in text.splitlines() if not line.startswith("#")))
    source = Reader(recorded)
    type_node = decode_node_id(source)
    request = decode_service_body(type_node.identifier, source.take(source.remaining))

    assert isinstance(request, ReadRequest)
    assert request.header.authentication_token == NodeId(1, 101)
    assert request.header.request_handle == 42
    assert [item.node_id for item in request.nodes] == [NodeId(2, 1001), NodeId(2, 1002)]

    type_id, body = encode_service_body(request)
    output = Writer()
    encode_node_id(output, NodeId(0, type_id))
    output.put(body)
    assert output.finish() == recorded


def test_client_surface_and_service_registry_structurally_exclude_server_mutation() -> None:
    package = Path(__file__).parent
    forbidden_service_roots = {
        "Write",
        "Call",
        "AddNodes",
        "AddReferences",
        "DeleteNodes",
        "DeleteReferences",
        "HistoryUpdate",
        "RegisterNodes",
        "UnregisterNodes",
    }
    for module_name in ("client.py", "protocol.py"):
        tree = ast.parse((package / module_name).read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert defined.isdisjoint(forbidden_service_roots), module_name

    assert set(SERVICE_TYPE_IDS) == {
        "ServiceFault",
        "OpenSecureChannelRequest",
        "OpenSecureChannelResponse",
        "CloseSecureChannelRequest",
        "CloseSecureChannelResponse",
        "CreateSessionRequest",
        "CreateSessionResponse",
        "ActivateSessionRequest",
        "ActivateSessionResponse",
        "CloseSessionRequest",
        "CloseSessionResponse",
        "BrowseRequest",
        "BrowseResponse",
        "BrowseNextRequest",
        "BrowseNextResponse",
        "ReadRequest",
        "ReadResponse",
    }
    public_surface = {
        name
        for name, value in vars(UaTcpClient).items()
        if not name.startswith("_") and (callable(value) or isinstance(value, property))
    }
    assert public_surface == {"connect", "connected", "browse", "browse_next", "read", "close"}
