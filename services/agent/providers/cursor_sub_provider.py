"""Cursor Pro subscription provider — auth + Connect-RPC agent completions.

Auth: browser poll login, token refresh, multi-account credential storage
(matches omp ``packages/ai/src/registry/oauth/cursor.ts``).

Completions: HTTP/2 Connect-RPC protobuf to
``POST https://api2.cursor.sh/agent.v1.AgentService/Run``
(``application/connect+proto``), full-duplex so we can answer mid-stream
``requestContext`` / KV blob handshakes. Framing and message shapes follow
omp ``packages/ai/src/providers/cursor.ts`` + ``agent.proto``.

Usage: ``GET /auth/usage`` (+ stripe profile) via :mod:`usage`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import select
import socket
import ssl
import struct
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import urlopen
import urllib.error
from .base import Provider
from . import sub_auth

log = logging.getLogger("agent.providers.cursor_sub")

LOGIN_URL = "https://cursor.com/loginDeepControl"
POLL_URL = "https://api2.cursor.sh/auth/poll"
REFRESH_URL = "https://api2.cursor.sh/auth/exchange_user_api_key"
CURSOR_API_URL = "https://api2.cursor.sh"
CURSOR_RUN_PATH = "/agent.v1.AgentService/Run"
CURSOR_CLIENT_VERSION = "cli-2026.01.09-231024f"
PROVIDER_ID = "cursor"
DEFAULT_MODEL = "composer-2.5"
POLL_MAX_ATTEMPTS = 150
POLL_BASE_DELAY = 1.0
POLL_MAX_DELAY = 10.0
POLL_BACKOFF = 1.2
REFRESH_SKEW_MS = 5 * 60 * 1000

CONNECT_END_STREAM_FLAG = 0b00000010
_HEARTBEAT_INTERVAL_S = 5.0
_DEFAULT_TIMEOUT_S = 180.0

# Registry / GUI id → (wire model_id, display_name, max_mode)
# Wire ids match Cursor usable-models / omp catalog; ``cursor/`` prefix is stripped.
_CURSOR_MODEL_MAP: dict[str, tuple[str, str, bool]] = {
    "default": ("default", "Cursor Auto", False),
    "auto": ("default", "Cursor Auto", False),
    "cursor-default": ("default", "Cursor Auto", False),
    "composer-2.5": ("composer-2.5", "Cursor Composer 2.5", False),
    "composer-2.5-max": ("composer-2.5", "Cursor Composer 2.5 Max", True),
    "composer-1.5": ("composer-1.5", "Cursor Composer 1.5", False),
    "composer-1": ("composer-1", "Cursor Composer 1", False),
    "cursor/claude-opus-4-8": ("claude-opus-4-8", "Claude Opus 4.8 (Cursor)", False),
    "cursor/claude-sonnet-5": ("claude-sonnet-5", "Claude Sonnet 5 (Cursor)", False),
    "cursor/gpt-5.6-sol": ("gpt-5.6-sol", "GPT-5.6 Sol (Cursor)", False),
    "claude-opus-4-8": ("claude-opus-4-8", "Claude Opus 4.8 (Cursor)", False),
    "claude-sonnet-5": ("claude-sonnet-5", "Claude Sonnet 5 (Cursor)", False),
    "gpt-5.6-sol": ("gpt-5.6-sol", "GPT-5.6 Sol (Cursor)", False),
}


class CursorAuthError(RuntimeError):
    """401 / unauthenticated — re-login required."""


class CursorUsageLimitError(RuntimeError):
    """Account/plan usage limit (soft block)."""


class CursorTransportError(RuntimeError):
    """Connect / HTTP / framing / protocol failure."""


class CursorProviderError(RuntimeError):
    """Upstream provider error with optional machine code."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ── Connect framing ──────────────────────────────────────────────────────────


def frame_connect_message(data: bytes, flags: int = 0) -> bytes:
    """5-byte Connect envelope: flags(1) + length_be(4) + payload."""
    if len(data) > 0xFFFFFFFF:
        raise ValueError("connect frame payload too large")
    return struct.pack("!BI", flags & 0xFF, len(data)) + data


def parse_connect_frames(buffer: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Decode complete Connect frames from *buffer*; return (frames, remainder)."""
    frames: list[tuple[int, bytes]] = []
    i = 0
    n = len(buffer)
    while i + 5 <= n:
        flags = buffer[i]
        msg_len = struct.unpack_from("!I", buffer, i + 1)[0]
        if i + 5 + msg_len > n:
            break
        payload = buffer[i + 5 : i + 5 + msg_len]
        frames.append((flags, payload))
        i += 5 + msg_len
    return frames, buffer[i:]


def parse_connect_end_stream(data: bytes) -> Exception | None:
    """Parse Connect end-stream JSON error payload (flag bit 0b10)."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return CursorTransportError("Failed to parse Connect end stream")
    error = payload.get("error") if isinstance(payload, dict) else None
    if not error:
        return None
    if not isinstance(error, dict):
        return CursorProviderError(f"Connect error: {error!r}", code="unknown")
    code = error.get("code") if isinstance(error.get("code"), str) else "unknown"
    message = error.get("message") if isinstance(error.get("message"), str) else "Unknown error"
    # Prefer human detail from aiserver.v1.ErrorDetails debug payload.
    details = error.get("details")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            debug = item.get("debug") if isinstance(item.get("debug"), dict) else {}
            nested = debug.get("details") if isinstance(debug.get("details"), dict) else {}
            detail = nested.get("detail") or nested.get("title")
            err_name = debug.get("error")
            if isinstance(detail, str) and detail.strip():
                message = detail.strip()
            if isinstance(err_name, str) and err_name.strip():
                code = err_name.strip()
            break
    return map_cursor_error(code=code, message=message, status=None)


def map_cursor_error(
    *,
    code: str | None = None,
    message: str = "",
    status: int | None = None,
) -> Exception:
    """Map HTTP / Connect error codes to typed exceptions."""
    code_l = (code or "").lower()
    msg_l = (message or "").lower()
    combined = f"{code_l} {msg_l}"

    if status == 401 or code_l in ("unauthenticated", "unauth", "permission_denied"):
        return CursorAuthError(
            f"Cursor auth failed ({status or code or 'unauthenticated'}): "
            f"{message or 're-login required (Settings → Connect Cursor)'}"
        )
    if status == 403 and any(
        s in combined
        for s in ("usage", "quota", "limit", "rate", "exhausted", "spend", "billing")
    ):
        return CursorUsageLimitError(
            f"Cursor usage limit: {message or code or 'quota exceeded'}"
        )
    if any(
        s in combined
        for s in (
            "resource_exhausted",
            "usage limit",
            "usage_limit",
            "out of credits",
            "rate_limit",
            "ratelimit",
            "quota",
        )
    ):
        return CursorUsageLimitError(
            f"Cursor usage limit: {message or code or 'quota exceeded'}"
        )
    if status is not None and status >= 400:
        return CursorProviderError(
            f"Cursor HTTP {status}: {message or code or 'error'}",
            code=code,
            status=status,
        )
    return CursorProviderError(
        f"Connect error {code or 'unknown'}: {message or 'Unknown error'}",
        code=code,
        status=status,
    )


def map_cursor_model(model_id: str) -> tuple[str, str, bool]:
    """Map registry/GUI model id → (wire_model_id, display_name, max_mode)."""
    mid = (model_id or "").strip() or DEFAULT_MODEL
    if mid in _CURSOR_MODEL_MAP:
        return _CURSOR_MODEL_MAP[mid]
    if mid.startswith("cursor/"):
        wire = mid[len("cursor/") :].strip() or DEFAULT_MODEL
        max_mode = wire.endswith("-max")
        if max_mode:
            wire = wire[: -len("-max")]
        return wire, mid, max_mode
    if mid.endswith("-max"):
        base = mid[: -len("-max")]
        return base, mid, True
    return mid, mid, False


# ── Minimal protobuf wire codec (subset of agent.v1) ─────────────────────────


def _encode_varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _decode_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated protobuf varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("protobuf varint too long")


def _encode_key(field: int, wire: int) -> bytes:
    return _encode_varint((field << 3) | wire)


def _encode_bytes_field(field: int, data: bytes) -> bytes:
    return _encode_key(field, 2) + _encode_varint(len(data)) + data


def _encode_string_field(field: int, s: str) -> bytes:
    return _encode_bytes_field(field, s.encode("utf-8"))


def _encode_bool_field(field: int, v: bool) -> bytes:
    return _encode_key(field, 0) + _encode_varint(1 if v else 0)


def _encode_uint32_field(field: int, v: int) -> bytes:
    return _encode_key(field, 0) + _encode_varint(int(v) & 0xFFFFFFFF)


def _encode_msg_field(field: int, msg: bytes) -> bytes:
    return _encode_bytes_field(field, msg)


def _iter_fields(data: bytes):
    i = 0
    n = len(data)
    while i < n:
        key, i = _decode_varint(data, i)
        field = key >> 3
        wire = key & 7
        if wire == 0:
            val, i = _decode_varint(data, i)
            yield field, wire, val
        elif wire == 2:
            ln, i = _decode_varint(data, i)
            if i + ln > n:
                raise ValueError("truncated length-delimited field")
            yield field, wire, data[i : i + ln]
            i += ln
        elif wire == 1:
            if i + 8 > n:
                raise ValueError("truncated fixed64")
            yield field, wire, data[i : i + 8]
            i += 8
        elif wire == 5:
            if i + 4 > n:
                raise ValueError("truncated fixed32")
            yield field, wire, data[i : i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _field_bytes(data: bytes, field_no: int) -> bytes | None:
    for f, w, v in _iter_fields(data):
        if f == field_no and w == 2 and isinstance(v, (bytes, bytearray)):
            return bytes(v)
    return None


def _field_varint(data: bytes, field_no: int) -> int | None:
    for f, w, v in _iter_fields(data):
        if f == field_no and w == 0 and isinstance(v, int):
            return v
    return None


def _field_string(data: bytes, field_no: int) -> str | None:
    raw = _field_bytes(data, field_no)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


def create_blob_id(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def build_agent_run_request_bytes(
    *,
    wire_model_id: str,
    display_model_id: str,
    display_name: str,
    max_mode: bool,
    conversation_id: str,
    user_text: str,
    system_prompt: str,
    blob_store: dict[str, bytes],
    custom_system_prompt: str | None = None,
) -> bytes:
    """Build AgentClientMessage { run_request } protobuf bytes."""

    def store_blob(payload: bytes) -> bytes:
        bid = create_blob_id(payload)
        blob_store[bid.hex()] = payload
        return bid

    sys_json = json.dumps(
        {"role": "system", "content": system_prompt or "You are a helpful assistant."},
        separators=(",", ":"),
    )
    root_blob = store_blob(sys_json.encode("utf-8"))

    # ConversationStateStructure { root_prompt_messages_json = 1 (repeated bytes) }
    conv_state = _encode_bytes_field(1, root_blob)

    # UserMessage { text=1, message_id=2 }
    user_msg = _encode_string_field(1, user_text) + _encode_string_field(
        2, str(uuid.uuid4())
    )
    # UserMessageAction { user_message=1 }
    user_action = _encode_msg_field(1, user_msg)
    # ConversationAction { user_message_action=1 }
    action = _encode_msg_field(1, user_action)

    # ModelDetails { model_id=1, display_model_id=3, display_name=4, max_mode=7 }
    model_details = (
        _encode_string_field(1, wire_model_id)
        + _encode_string_field(3, display_model_id)
        + _encode_string_field(4, display_name)
    )
    if max_mode:
        model_details += _encode_bool_field(7, True)

    # RequestedModel { model_id=1, max_mode=2 }
    requested = _encode_string_field(1, wire_model_id)
    if max_mode:
        requested += _encode_bool_field(2, True)
    else:
        requested += _encode_bool_field(2, False)

    # AgentRunRequest
    run_req = (
        _encode_msg_field(1, conv_state)
        + _encode_msg_field(2, action)
        + _encode_msg_field(3, model_details)
        + _encode_string_field(5, conversation_id)
        + _encode_msg_field(9, requested)
    )
    if custom_system_prompt:
        run_req += _encode_string_field(8, custom_system_prompt)

    # AgentClientMessage { run_request = 1 }
    return _encode_msg_field(1, run_req)


def build_client_heartbeat_bytes() -> bytes:
    # AgentClientMessage { client_heartbeat = 7 }  (empty ClientHeartbeat)
    return _encode_msg_field(7, b"")


def build_kv_get_blob_result(kv_id: int, blob_data: bytes | None) -> bytes:
    # GetBlobResult { blob_data = 1 optional }
    result = b""
    if blob_data is not None:
        result = _encode_bytes_field(1, blob_data)
    # KvClientMessage { id=1, get_blob_result=2 }
    kv = _encode_uint32_field(1, kv_id) + _encode_msg_field(2, result)
    # AgentClientMessage { kv_client_message = 3 }
    return _encode_msg_field(3, kv)


def build_kv_set_blob_result(kv_id: int) -> bytes:
    # SetBlobResult {} empty
    kv = _encode_uint32_field(1, kv_id) + _encode_msg_field(3, b"")
    return _encode_msg_field(3, kv)


def build_request_context_result(exec_id_num: int, exec_id_str: str) -> bytes:
    # RequestContext empty
    req_ctx = b""
    # RequestContextSuccess { request_context = 1 }
    success = _encode_msg_field(1, req_ctx)
    # RequestContextResult { success = 1 }
    result = _encode_msg_field(1, success)
    # ExecClientMessage { id=1, request_context_result=10, exec_id=15 }
    exec_msg = _encode_uint32_field(1, exec_id_num) + _encode_msg_field(10, result)
    if exec_id_str:
        exec_msg += _encode_string_field(15, exec_id_str)
    # AgentClientMessage { exec_client_message = 2 }
    return _encode_msg_field(2, exec_msg)


def build_exec_reject(
    exec_id_num: int,
    exec_id_str: str,
    result_field: int,
    reject_field: int,
    reason: str,
) -> bytes:
    """Generic ExecClientMessage with Result { rejected = N { reason/error } }."""
    # Most *Rejected messages use field 1 as string reason.
    rejected = _encode_string_field(1, reason)
    result = _encode_msg_field(reject_field, rejected)
    exec_msg = (
        _encode_uint32_field(1, exec_id_num)
        + _encode_msg_field(result_field, result)
    )
    if exec_id_str:
        exec_msg += _encode_string_field(15, exec_id_str)
    return _encode_msg_field(2, exec_msg)


# ExecServer oneof field → (ExecClient result field, rejected subfield)
# Rejected subfield is usually 3 (rejected) on Result oneofs; shell uses 3 too.
_EXEC_REJECT_MAP: dict[int, tuple[int, int]] = {
    2: (2, 3),   # shell_args → shell_result.rejected
    3: (3, 3),   # write
    4: (4, 3),   # delete
    5: (5, 3),   # grep
    7: (7, 3),   # read
    8: (8, 3),   # ls
    9: (9, 3),   # diagnostics
    11: (11, 3), # mcp
    14: (2, 3),  # shell_stream_args → shell_result-ish; use shell_result
    16: (16, 2), # background_shell — error field often 2
    20: (20, 2), # fetch
}


def _extract_user_text(messages: list[dict]) -> str:
    """Flatten the last user message to plain text."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
                elif isinstance(p, str):
                    parts.append(p)
            return "\n".join(parts).strip()
    return ""


def _prior_root_prompt_jsons(system: str, messages: list[dict]) -> list[str]:
    """System + prior turns (excluding active user) as Cursor root-prompt JSON blobs."""
    out = [
        json.dumps(
            {"role": "system", "content": system or "You are a helpful assistant."},
            separators=(",", ":"),
        )
    ]
    # Exclude last user message (sent via action).
    hist = list(messages)
    if hist and hist[-1].get("role") == "user":
        hist = hist[:-1]
    for msg in hist:
        role = msg.get("role") or "user"
        if role not in ("user", "assistant", "system"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(p.get("text") or "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            text = str(content or "")
        text = text.strip()
        if not text:
            continue
        out.append(json.dumps({"role": role, "content": text}, separators=(",", ":")))
    return out


def build_agent_run_request_bytes_multi(
    *,
    wire_model_id: str,
    display_model_id: str,
    display_name: str,
    max_mode: bool,
    conversation_id: str,
    user_text: str,
    system_prompt: str,
    messages: list[dict],
    blob_store: dict[str, bytes],
) -> bytes:
    """Like :func:`build_agent_run_request_bytes` but multi-turn root prompts."""

    def store_blob(payload: bytes) -> bytes:
        bid = create_blob_id(payload)
        blob_store[bid.hex()] = payload
        return bid

    root_ids = [
        store_blob(j.encode("utf-8"))
        for j in _prior_root_prompt_jsons(system_prompt, messages)
    ]
    conv_state = b"".join(_encode_bytes_field(1, bid) for bid in root_ids)

    user_msg = _encode_string_field(1, user_text) + _encode_string_field(
        2, str(uuid.uuid4())
    )
    user_action = _encode_msg_field(1, user_msg)
    action = _encode_msg_field(1, user_action)

    model_details = (
        _encode_string_field(1, wire_model_id)
        + _encode_string_field(3, display_model_id)
        + _encode_string_field(4, display_name)
    )
    if max_mode:
        model_details += _encode_bool_field(7, True)

    requested = _encode_string_field(1, wire_model_id) + _encode_bool_field(2, max_mode)

    run_req = (
        _encode_msg_field(1, conv_state)
        + _encode_msg_field(2, action)
        + _encode_msg_field(3, model_details)
        + _encode_string_field(5, conversation_id)
        + _encode_msg_field(9, requested)
    )
    return _encode_msg_field(1, run_req)


def parse_server_message(
    data: bytes,
) -> dict[str, Any]:
    """Decode AgentServerMessage into a small dict for the run loop.

    Returns one of:
      {"kind": "interaction", "case": "text_delta"|"thinking_delta"|"turn_ended"|..., "text": str}
      {"kind": "kv", "id": int, "case": "get_blob"|"set_blob", "blob_id": bytes, "blob_data": bytes|None}
      {"kind": "exec", "id": int, "exec_id": str, "case_field": int}
      {"kind": "checkpoint"}
      {"kind": "other", "field": int}
    """
    # AgentServerMessage oneof:
    # 1 interaction_update, 2 exec_server_message, 3 conversation_checkpoint_update,
    # 4 kv_server_message, 7 interaction_query
    for f, w, v in _iter_fields(data):
        if w != 2 or not isinstance(v, (bytes, bytearray)):
            continue
        payload = bytes(v)
        if f == 1:
            return _parse_interaction_update(payload)
        if f == 2:
            return _parse_exec_server(payload)
        if f == 3:
            return {"kind": "checkpoint"}
        if f == 4:
            return _parse_kv_server(payload)
        if f == 7:
            return {"kind": "interaction_query"}
        return {"kind": "other", "field": f}
    return {"kind": "empty"}


def _parse_interaction_update(data: bytes) -> dict[str, Any]:
    # text_delta=1, thinking_delta=4, turn_ended=14, ...
    for f, w, v in _iter_fields(data):
        if f == 1 and w == 2 and isinstance(v, (bytes, bytearray)):
            text = _field_string(bytes(v), 1) or ""
            return {"kind": "interaction", "case": "text_delta", "text": text}
        if f == 4 and w == 2 and isinstance(v, (bytes, bytearray)):
            text = _field_string(bytes(v), 1) or ""
            return {"kind": "interaction", "case": "thinking_delta", "text": text}
        if f == 14:
            return {"kind": "interaction", "case": "turn_ended", "text": ""}
        if f == 5:
            return {"kind": "interaction", "case": "thinking_completed", "text": ""}
        if f == 13:
            return {"kind": "interaction", "case": "heartbeat", "text": ""}
        if f == 8:
            return {"kind": "interaction", "case": "token_delta", "text": ""}
    return {"kind": "interaction", "case": "other", "text": ""}


def _parse_kv_server(data: bytes) -> dict[str, Any]:
    kv_id = _field_varint(data, 1) or 0
    for f, w, v in _iter_fields(data):
        if f == 2 and w == 2 and isinstance(v, (bytes, bytearray)):
            # GetBlobArgs { blob_id = 1 }
            bid = _field_bytes(bytes(v), 1) or b""
            return {
                "kind": "kv",
                "id": kv_id,
                "case": "get_blob",
                "blob_id": bid,
                "blob_data": None,
            }
        if f == 3 and w == 2 and isinstance(v, (bytes, bytearray)):
            # SetBlobArgs { blob_id=1, blob_data=2 }
            inner = bytes(v)
            bid = _field_bytes(inner, 1) or b""
            bdata = _field_bytes(inner, 2)
            return {
                "kind": "kv",
                "id": kv_id,
                "case": "set_blob",
                "blob_id": bid,
                "blob_data": bdata,
            }
    return {
        "kind": "kv",
        "id": kv_id,
        "case": "other",
        "blob_id": b"",
        "blob_data": None,
    }


def _parse_exec_server(data: bytes) -> dict[str, Any]:
    exec_num = _field_varint(data, 1) or 0
    exec_id = _field_string(data, 15) or ""
    case_field = 0
    for f, w, v in _iter_fields(data):
        if w == 2 and f not in (19,):  # skip span_context
            # oneof message fields
            if f in (2, 3, 4, 5, 7, 8, 9, 10, 11, 14, 16, 17, 18, 20, 21, 22, 23):
                case_field = f
                break
    return {
        "kind": "exec",
        "id": exec_num,
        "exec_id": exec_id,
        "case_field": case_field,
    }



# ── HTTP/2 Connect client (full-duplex) ──────────────────────────────────────


def _require_h2():
    try:
        from h2.config import H2Configuration  # noqa: F401
        from h2.connection import H2Connection  # noqa: F401
        from h2.events import (  # noqa: F401
            ConnectionTerminated,
            DataReceived,
            ResponseReceived,
            StreamEnded,
            StreamReset,
            WindowUpdated,
        )
    except ImportError as exc:
        raise CursorTransportError(
            "Cursor completions require the 'h2' package for HTTP/2 "
            "(pip install h2). Auth and usage work without it."
        ) from exc


def run_cursor_agent(
    *,
    access_token: str,
    request_bytes: bytes,
    blob_store: dict[str, bytes],
    base_url: str = CURSOR_API_URL,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    on_write: Callable[[bytes], None] | None = None,
) -> tuple[str, str, str]:
    """POST AgentService/Run over HTTP/2 Connect; return (text, thinking, finish_reason).

    Full-duplex: answers KV get/set blob and requestContext mid-stream.
    """
    _require_h2()
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import (
        ConnectionTerminated,
        DataReceived,
        ResponseReceived,
        StreamEnded,
        StreamReset,
        WindowUpdated,
    )
    from h2.exceptions import ProtocolError

    base = (base_url or CURSOR_API_URL).rstrip("/")
    if "://" in base:
        host = base.split("://", 1)[1].split("/", 1)[0]
    else:
        host = base.split("/", 1)[0]
    port = 443

    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2"])
    raw = socket.create_connection((host, port), timeout=min(30.0, timeout_s))
    try:
        raw.settimeout(timeout_s)
        sock = ctx.wrap_socket(raw, server_hostname=host)
    except Exception:
        raw.close()
        raise
    if sock.selected_alpn_protocol() != "h2":
        sock.close()
        raise CursorTransportError(
            f"TLS ALPN negotiated {sock.selected_alpn_protocol()!r}, need 'h2'"
        )

    config = H2Configuration(client_side=True, header_encoding="utf-8")
    conn = H2Connection(config=config)
    conn.initiate_connection()

    stream_id = conn.get_next_available_stream_id()
    request_id = str(uuid.uuid4())
    headers = [
        (":method", "POST"),
        (":authority", host),
        (":scheme", "https"),
        (":path", CURSOR_RUN_PATH),
        ("content-type", "application/connect+proto"),
        ("connect-protocol-version", "1"),
        ("te", "trailers"),
        ("authorization", f"Bearer {access_token}"),
        ("x-ghost-mode", "true"),
        ("x-cursor-client-version", CURSOR_CLIENT_VERSION),
        ("x-cursor-client-type", "cli"),
        ("x-request-id", request_id),
    ]
    conn.send_headers(stream_id, headers)

    framed = frame_connect_message(request_bytes)
    if on_write:
        on_write(framed)
    # Keep stream open for bidi (heartbeats + exec/kv replies). Do NOT end_stream.
    _h2_send_data(conn, sock, stream_id, framed, end_stream=False)

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    finish_reason = "stop"
    pending = b""
    status: int | None = None
    turn_ended = False
    deadline = time.monotonic() + timeout_s
    last_heartbeat = time.monotonic()
    end_error: Exception | None = None

    try:
        while time.monotonic() < deadline:
            # Heartbeat
            now = time.monotonic()
            if now - last_heartbeat >= _HEARTBEAT_INTERVAL_S and not turn_ended:
                try:
                    hb = frame_connect_message(build_client_heartbeat_bytes())
                    _h2_send_data(conn, sock, stream_id, hb, end_stream=False)
                    last_heartbeat = now
                except Exception as exc:  # noqa: BLE001
                    log.debug("cursor heartbeat failed: %s", exc)

            timeout = max(0.05, min(1.0, deadline - time.monotonic()))
            ready, _, _ = select.select([sock], [], [], timeout)
            if not ready:
                if turn_ended:
                    break
                continue

            try:
                data = sock.recv(65535)
            except socket.timeout:
                if turn_ended:
                    break
                continue
            if not data:
                break

            events = conn.receive_data(data)
            for event in events:
                if isinstance(event, ResponseReceived):
                    hdrs = dict(event.headers)
                    try:
                        status = int(hdrs.get(":status", "0"))
                    except (TypeError, ValueError):
                        status = 0
                    if status and status >= 400:
                        # Still drain body for Connect error envelope.
                        pass
                elif isinstance(event, DataReceived):
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    pending += event.data
                    frames, pending = parse_connect_frames(pending)
                    for flags, payload in frames:
                        if flags & CONNECT_END_STREAM_FLAG:
                            err = parse_connect_end_stream(payload)
                            if err is not None:
                                end_error = err
                            continue
                        try:
                            msg = parse_server_message(payload)
                        except Exception as exc:  # noqa: BLE001
                            log.debug("cursor parse server msg: %s", exc)
                            continue
                        kind = msg.get("kind")
                        if kind == "interaction":
                            case = msg.get("case")
                            if case == "text_delta":
                                text_parts.append(msg.get("text") or "")
                            elif case == "thinking_delta":
                                thinking_parts.append(msg.get("text") or "")
                            elif case == "turn_ended":
                                turn_ended = True
                                finish_reason = "stop"
                        elif kind == "kv":
                            reply = _handle_kv(msg, blob_store)
                            if reply is not None:
                                _h2_send_data(
                                    conn,
                                    sock,
                                    stream_id,
                                    frame_connect_message(reply),
                                    end_stream=False,
                                )
                        elif kind == "exec":
                            reply = _handle_exec(msg)
                            if reply is not None:
                                _h2_send_data(
                                    conn,
                                    sock,
                                    stream_id,
                                    frame_connect_message(reply),
                                    end_stream=False,
                                )
                elif isinstance(event, StreamEnded):
                    turn_ended = True
                elif isinstance(event, StreamReset):
                    end_error = CursorTransportError(
                        f"HTTP/2 stream reset error_code={event.error_code}"
                    )
                    turn_ended = True
                elif isinstance(event, ConnectionTerminated):
                    end_error = CursorTransportError(
                        f"HTTP/2 connection terminated error_code={event.error_code}"
                    )
                    turn_ended = True
                elif isinstance(event, WindowUpdated):
                    pass

            out = conn.data_to_send()
            if out:
                sock.sendall(out)

            if end_error is not None:
                break
            if turn_ended and status is not None and status < 400:
                # Brief drain for trailing frames.
                if time.monotonic() + 0.2 >= deadline:
                    break

        if status is not None and status >= 400 and end_error is None:
            end_error = map_cursor_error(
                status=status,
                message="".join(text_parts) or f"HTTP {status}",
            )
        if end_error is not None and not "".join(text_parts):
            raise end_error

        text = "".join(text_parts)
        thinking = "".join(thinking_parts)
        if not text and not thinking:
            if end_error is not None:
                raise end_error
            if status is not None and status >= 400:
                raise map_cursor_error(status=status, message=f"HTTP {status}")
            if time.monotonic() >= deadline:
                raise CursorTransportError(
                    f"Cursor agent run timed out after {timeout_s:.0f}s"
                )
            raise CursorTransportError("Cursor agent returned empty response")
        return text, thinking, finish_reason
    except ProtocolError as exc:
        raise CursorTransportError(f"HTTP/2 protocol error: {exc}") from exc
    finally:
        try:
            conn.close_connection()
            leftover = conn.data_to_send()
            if leftover:
                sock.sendall(leftover)
        except Exception:  # noqa: BLE001
            pass
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


def _h2_send_data(conn, sock, stream_id: int, data: bytes, *, end_stream: bool) -> None:
    """Write *data* respecting h2 flow-control windows."""
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        window = conn.local_flow_control_window(stream_id)
        max_size = conn.max_outbound_frame_size
        chunk = min(window, max_size, len(view) - offset)
        if chunk <= 0:
            # Wait for window update
            ready, _, _ = select.select([sock], [], [], 5.0)
            if not ready:
                raise CursorTransportError("HTTP/2 flow-control stall")
            incoming = sock.recv(65535)
            if not incoming:
                raise CursorTransportError("HTTP/2 connection closed during send")
            events = conn.receive_data(incoming)
            # Process only window / connection events here; data is rare mid-send.
            from h2.events import DataReceived, WindowUpdated, ConnectionTerminated

            for event in events:
                if isinstance(event, DataReceived):
                    # Unexpected early response data — buffer via side channel not available;
                    # re-inject by acknowledging; caller loop will not see it. Avoid this path.
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, ConnectionTerminated):
                    raise CursorTransportError("HTTP/2 connection terminated during send")
                elif isinstance(event, WindowUpdated):
                    pass
            out = conn.data_to_send()
            if out:
                sock.sendall(out)
            continue
        end = end_stream and (offset + chunk >= len(view))
        conn.send_data(stream_id, view[offset : offset + chunk].tobytes(), end_stream=end)
        offset += chunk
        out = conn.data_to_send()
        if out:
            sock.sendall(out)


def _handle_kv(msg: dict[str, Any], blob_store: dict[str, bytes]) -> bytes | None:
    kv_id = int(msg.get("id") or 0)
    case = msg.get("case")
    if case == "get_blob":
        bid = msg.get("blob_id") or b""
        key = bytes(bid).hex() if isinstance(bid, (bytes, bytearray)) else ""
        data = blob_store.get(key)
        return build_kv_get_blob_result(kv_id, data)
    if case == "set_blob":
        bid = msg.get("blob_id") or b""
        bdata = msg.get("blob_data")
        if isinstance(bid, (bytes, bytearray)) and isinstance(bdata, (bytes, bytearray)):
            blob_store[bytes(bid).hex()] = bytes(bdata)
        return build_kv_set_blob_result(kv_id)
    return None


def _handle_exec(msg: dict[str, Any]) -> bytes | None:
    exec_num = int(msg.get("id") or 0)
    exec_id = str(msg.get("exec_id") or "")
    case_field = int(msg.get("case_field") or 0)
    if case_field == 10:  # request_context_args
        return build_request_context_result(exec_num, exec_id)
    mapped = _EXEC_REJECT_MAP.get(case_field)
    if mapped:
        result_field, reject_field = mapped
        return build_exec_reject(
            exec_num,
            exec_id,
            result_field,
            reject_field,
            "tools not enabled in this client",
        )
    # Unknown exec — no reply (may hang); log.
    log.warning("cursor unhandled exec case_field=%s id=%s", case_field, exec_num)
    return None


# ── Provider ─────────────────────────────────────────────────────────────────


class CursorSubProvider(Provider):
    """Cursor Pro subscription: auth + Connect-RPC agent completions + usage."""

    name = "cursor-sub"
    supports_vision = False

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model or DEFAULT_MODEL
        self._cred = sub_auth.load_credential(PROVIDER_ID)

    # ── Auth ────────────────────────────────────────────────────────────────

    def login(self) -> dict:
        """Open the Cursor login page and poll until the user authorizes."""
        verifier = sub_auth.make_verifier()
        challenge = sub_auth.challenge_for(verifier)
        uuid_ = sub_auth.random_uuid()

        params = urlencode({
            "challenge": challenge,
            "uuid": uuid_,
            "mode": "login",
            "redirectTarget": "cli",
        })
        url = f"{LOGIN_URL}?{params}"

        import webbrowser
        webbrowser.open(url)

        data = self._poll(uuid_, verifier)
        return self._store_tokens(data)

    def _poll(self, uuid_: str, verifier: str) -> dict:
        """Poll the Cursor auth endpoint until the user completes the browser login."""
        delay = POLL_BASE_DELAY
        consecutive_errors = 0

        for _ in range(POLL_MAX_ATTEMPTS):
            params = urlencode({"uuid": uuid_, "verifier": verifier})
            req_url = f"{POLL_URL}?{params}"
            try:
                with urlopen(req_url, timeout=30.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("accessToken"):
                    return data
                consecutive_errors = 0
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        raise
            except Exception:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise

            time.sleep(delay)
            delay = min(delay * POLL_BACKOFF, POLL_MAX_DELAY)

        raise RuntimeError(
            "Cursor login timed out — did you complete the browser login?"
        )

    def _store_tokens(self, data: dict) -> dict:
        access = data["accessToken"]
        refresh = data.get("refreshToken")

        claims = sub_auth.decode_jwt_payload(access)
        exp = claims.get("exp")
        if exp:
            expires_at_ms = int(exp) * 1000 - REFRESH_SKEW_MS
        else:
            expires_at_ms = int(time.time() * 1000) + 3600 * 1000

        account_id = None
        for key in ("sub", "user_id", "userId", "id"):
            v = claims.get(key)
            if v is not None and str(v).strip():
                account_id = str(v).strip()
                break
        email = None
        for key in ("email", "preferred_username", "unique_name", "upn"):
            v = claims.get(key)
            if isinstance(v, str) and "@" in v:
                email = v.strip().lower()
                break

        cred = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at_ms": expires_at_ms,
            "account_id": account_id,
            "email": email,
        }
        if self._cred:
            for k in (
                "email", "display_name", "plan", "account_id",
                "last_usage", "last_usage_fetched_at",
            ):
                if self._cred.get(k) is not None and not cred.get(k):
                    cred[k] = self._cred[k]
        try:
            from . import usage as usage_mod
            cred = usage_mod.enrich_cursor_profile(cred)
        except Exception:
            if not cred.get("display_name"):
                if cred.get("email"):
                    cred["display_name"] = cred["email"]
                elif cred.get("plan"):
                    cred["display_name"] = f"Cursor ({cred['plan']})"
                else:
                    cred["display_name"] = "Cursor"
        if not cred.get("account_id") and cred.get("email"):
            cred["account_id"] = sub_auth._slug_account_id(cred["email"])
        sub_auth.save_credential(PROVIDER_ID, cred)
        self._cred = cred
        return cred

    def refresh(self) -> dict:
        """Exchange the refresh token (or access token) for a fresh access token."""
        if not self._cred:
            raise RuntimeError("Not logged in to Cursor.")
        bearer = self._cred.get("refresh_token") or self._cred.get("access_token")
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        }
        resp = sub_auth.post_json(REFRESH_URL, {}, headers=headers)

        merged = dict(resp)
        if not merged.get("refreshToken") and self._cred.get("refresh_token"):
            merged["refreshToken"] = self._cred["refresh_token"]

        return self._store_tokens(merged)

    def _ensure_auth(self) -> None:
        if not self._cred:
            self._cred = sub_auth.load_credential(PROVIDER_ID)
        if not self._cred:
            raise RuntimeError(
                "Not logged in to Cursor. Run login() first (Settings → Connect Cursor)."
            )
        if sub_auth.is_expired(self._cred, skew_ms=REFRESH_SKEW_MS):
            self.refresh()

    def _access_token(self) -> str:
        self._ensure_auth()
        assert self._cred is not None
        tok = self._cred.get("access_token") or ""
        if not tok:
            raise RuntimeError("Cursor credential missing access_token")
        return tok

    # ── Usage ───────────────────────────────────────────────────────────────

    def fetch_usage(self, *, force: bool = True) -> dict:
        """Live ``GET /auth/usage`` (+ stripe plan) through the shared usage module.

        Returns the contract row: plan, usage windows (used/limit/resets), identity.
        """
        self._ensure_auth()
        from . import usage as usage_mod

        aid = None
        if self._cred:
            aid = self._cred.get("account_id")
        row = usage_mod.fetch_account_usage("cursor", aid, force=force)
        # Stash on credential for cold Settings restore.
        if self._cred is not None and isinstance(row.get("usage"), list) and row["usage"]:
            try:
                self._cred["last_usage"] = list(row["usage"])
                self._cred["last_usage_fetched_at"] = time.time()
                if row.get("plan"):
                    self._cred["plan"] = row["plan"]
                if row.get("display_name"):
                    self._cred["display_name"] = row["display_name"]
                if row.get("email"):
                    self._cred["email"] = row["email"]
                sub_auth.save_credential(PROVIDER_ID, self._cred)
            except Exception:  # noqa: BLE001
                log.debug("cursor stash last_usage failed", exc_info=True)
        return row

    # ── Completions ─────────────────────────────────────────────────────────

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        text, _thinking = self.complete_ex(system, messages, max_tokens=max_tokens)
        return text

    def complete_ex(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> tuple[str, str]:
        """Run a no-tools AgentService/Run turn; return (text, thinking).

        ``max_tokens`` is accepted for Provider API parity (Cursor agent server
        owns output limits; not forwarded on the wire today).
        """
        del max_tokens  # server-controlled
        token = self._access_token()
        wire_id, display_name, max_mode = map_cursor_model(self.model)
        display_model_id = (self.model or wire_id).strip() or wire_id

        user_text = _extract_user_text(messages)
        if not user_text:
            raise ValueError("Cursor complete_ex requires a non-empty user message")

        blob_store: dict[str, bytes] = {}
        conversation_id = str(uuid.uuid4())
        request_bytes = build_agent_run_request_bytes_multi(
            wire_model_id=wire_id,
            display_model_id=display_model_id,
            display_name=display_name,
            max_mode=max_mode,
            conversation_id=conversation_id,
            user_text=user_text,
            system_prompt=system or "You are a helpful assistant.",
            messages=messages,
            blob_store=blob_store,
        )

        try:
            text, thinking, _finish = run_cursor_agent(
                access_token=token,
                request_bytes=request_bytes,
                blob_store=blob_store,
            )
        except CursorAuthError:
            # One refresh retry on auth failure.
            try:
                self.refresh()
                token = self._access_token()
                text, thinking, _finish = run_cursor_agent(
                    access_token=token,
                    request_bytes=request_bytes,
                    blob_store=blob_store,
                )
            except Exception:
                raise
        except CursorUsageLimitError:
            raise
        except CursorProviderError as exc:
            if exc.status == 401 or (exc.code or "").lower() in (
                "unauthenticated",
                "permission_denied",
            ):
                try:
                    self.refresh()
                    token = self._access_token()
                    text, thinking, _finish = run_cursor_agent(
                        access_token=token,
                        request_bytes=request_bytes,
                        blob_store=blob_store,
                    )
                except Exception as retry_exc:
                    raise CursorAuthError(str(retry_exc)) from retry_exc
            else:
                raise

        # Best-effort usage refresh so Settings bars update after a chat.
        try:
            self.fetch_usage(force=True)
        except Exception:  # noqa: BLE001
            log.debug("cursor post-complete usage refresh failed", exc_info=True)

        return text, thinking
