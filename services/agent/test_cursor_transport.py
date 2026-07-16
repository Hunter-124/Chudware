"""Offline tests for Cursor Connect-RPC framing, model map, and error mapping."""
from __future__ import annotations

import json
import struct

import pytest

from agent.providers import cursor_sub_provider as csp


# ── Framing ──────────────────────────────────────────────────────────────────


def test_frame_connect_message_roundtrip():
    payload = b"hello-proto"
    frame = csp.frame_connect_message(payload)
    assert frame[0] == 0
    assert struct.unpack_from("!I", frame, 1)[0] == len(payload)
    assert frame[5:] == payload

    frames, rest = csp.parse_connect_frames(frame + b"partial")
    assert len(frames) == 1
    assert frames[0] == (0, payload)
    assert rest == b"partial"


def test_frame_end_stream_flag():
    err = json.dumps({"error": {"code": "resource_exhausted", "message": "quota"}}).encode()
    frame = csp.frame_connect_message(err, flags=csp.CONNECT_END_STREAM_FLAG)
    frames, rest = csp.parse_connect_frames(frame)
    assert rest == b""
    flags, data = frames[0]
    assert flags & csp.CONNECT_END_STREAM_FLAG
    exc = csp.parse_connect_end_stream(data)
    assert isinstance(exc, csp.CursorUsageLimitError)


def test_parse_connect_frames_split_across_chunks():
    p1 = csp.frame_connect_message(b"aaa")
    p2 = csp.frame_connect_message(b"bbbb")
    buf = p1 + p2
    # First 3 bytes only
    frames, rem = csp.parse_connect_frames(buf[:3])
    assert frames == []
    frames, rem = csp.parse_connect_frames(rem + buf[3:])
    assert len(frames) == 2
    assert frames[0][1] == b"aaa"
    assert frames[1][1] == b"bbbb"


def test_parse_end_stream_pulls_detail_from_aiserver():
    payload = {
        "error": {
            "code": "resource_exhausted",
            "message": "Error",
            "details": [{
                "type": "aiserver.v1.ErrorDetails",
                "debug": {
                    "error": "ERROR_RATE_LIMITED",
                    "details": {
                        "title": "Increase limits",
                        "detail": "You're out of usage. Switch to Auto.",
                    },
                },
            }],
        }
    }
    exc = csp.parse_connect_end_stream(json.dumps(payload).encode())
    assert isinstance(exc, csp.CursorUsageLimitError)
    assert "out of usage" in str(exc).lower()


# ── Error mapping ────────────────────────────────────────────────────────────


def test_map_auth_401():
    exc = csp.map_cursor_error(status=401, message="bad token")
    assert isinstance(exc, csp.CursorAuthError)


def test_map_usage_limit_from_code():
    exc = csp.map_cursor_error(code="resource_exhausted", message="out of credits")
    assert isinstance(exc, csp.CursorUsageLimitError)


def test_map_usage_limit_from_403_message():
    exc = csp.map_cursor_error(status=403, message="usage limit reached")
    assert isinstance(exc, csp.CursorUsageLimitError)


def test_map_generic_provider():
    exc = csp.map_cursor_error(status=500, message="boom")
    assert isinstance(exc, csp.CursorProviderError)
    assert exc.status == 500


# ── Model id mapping ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "registry_id,wire,max_mode",
    [
        ("composer-2.5", "composer-2.5", False),
        ("cursor-default", "default", False),
        ("default", "default", False),
        ("auto", "default", False),
        ("composer-2.5-max", "composer-2.5", True),
        ("cursor/claude-opus-4-8", "claude-opus-4-8", False),
        ("cursor/claude-sonnet-5", "claude-sonnet-5", False),
        ("cursor/gpt-5.6-sol", "gpt-5.6-sol", False),
        ("claude-sonnet-5", "claude-sonnet-5", False),
    ],
)
def test_map_cursor_model(registry_id, wire, max_mode):
    w, _name, mm = csp.map_cursor_model(registry_id)
    assert w == wire
    assert mm is max_mode


def test_map_cursor_model_strips_cursor_prefix():
    w, name, mm = csp.map_cursor_model("cursor/composer-1.5")
    assert w == "composer-1.5"
    assert mm is False
    assert "cursor/" in name or name == "cursor/composer-1.5"


# ── Protobuf encode / decode ─────────────────────────────────────────────────


def test_build_run_request_contains_model_and_user():
    store: dict[str, bytes] = {}
    raw = csp.build_agent_run_request_bytes(
        wire_model_id="composer-2.5",
        display_model_id="composer-2.5",
        display_name="Composer",
        max_mode=False,
        conversation_id="conv-1",
        user_text="ping",
        system_prompt="sys",
        blob_store=store,
    )
    assert isinstance(raw, (bytes, bytearray))
    assert len(raw) > 20
    # AgentClientMessage field 1 = run_request
    assert store  # system blob stored
    # User text and model id appear as UTF-8 substrings in the protobuf.
    assert b"ping" in raw
    assert b"composer-2.5" in raw
    assert b"conv-1" in raw


def test_build_run_request_max_mode_flag():
    store: dict[str, bytes] = {}
    raw = csp.build_agent_run_request_bytes(
        wire_model_id="composer-2.5",
        display_model_id="composer-2.5-max",
        display_name="Max",
        max_mode=True,
        conversation_id="c",
        user_text="hi",
        system_prompt="s",
        blob_store=store,
    )
    assert b"composer-2.5" in raw


def test_parse_interaction_text_delta():
    # InteractionUpdate { text_delta = 1 { text = 1 "ab" } }
    text_delta = csp._encode_string_field(1, "ab")
    interaction = csp._encode_msg_field(1, text_delta)
    server = csp._encode_msg_field(1, interaction)
    msg = csp.parse_server_message(server)
    assert msg["kind"] == "interaction"
    assert msg["case"] == "text_delta"
    assert msg["text"] == "ab"


def test_parse_turn_ended():
    # InteractionUpdate { turn_ended = 14 {} }
    interaction = csp._encode_msg_field(14, b"")
    server = csp._encode_msg_field(1, interaction)
    msg = csp.parse_server_message(server)
    assert msg["case"] == "turn_ended"


def test_parse_kv_get_blob():
    # KvServerMessage { id=1:7, get_blob_args=2 { blob_id=1: <32 bytes> } }
    bid = b"\x01" * 32
    args = csp._encode_bytes_field(1, bid)
    kv = csp._encode_uint32_field(1, 7) + csp._encode_msg_field(2, args)
    server = csp._encode_msg_field(4, kv)
    msg = csp.parse_server_message(server)
    assert msg["kind"] == "kv"
    assert msg["case"] == "get_blob"
    assert msg["id"] == 7
    assert msg["blob_id"] == bid


def test_kv_get_blob_result_roundtrip_store():
    store = {"aa" * 16: b"payload"}
    # Use real sha hex key
    data = b"payload"
    bid = csp.create_blob_id(data)
    store[bid.hex()] = data
    reply = csp.build_kv_get_blob_result(3, store[bid.hex()])
    # AgentClientMessage field 3 = kv_client_message
    assert reply  # non-empty
    # Decode: should contain payload bytes
    assert b"payload" in reply


def test_request_context_result_bytes():
    raw = csp.build_request_context_result(9, "exec-1")
    assert raw
    assert b"exec-1" in raw


def test_heartbeat_bytes():
    hb = csp.build_client_heartbeat_bytes()
    # field 7 empty message
    assert hb


# ── extract user text ────────────────────────────────────────────────────────


def test_extract_user_text_parts():
    msgs = [
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        {"role": "user", "content": [{"type": "text", "text": "ping"}]},
    ]
    assert csp._extract_user_text(msgs) == "ping"


def test_require_h2_error_message():
    # When h2 is installed (dev/CI), helper is a no-op; when missing, CursorTransportError.
    try:
        csp._require_h2()
    except csp.CursorTransportError as exc:
        assert "h2" in str(exc).lower()
