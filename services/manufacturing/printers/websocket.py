"""Minimal RFC 6455 JSON text client for Moonraker subscriptions."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
from typing import Any, Mapping
from urllib.parse import urlsplit


from .http_client import _connect_authorized
from .model import ProtocolError, Redactor, _commissioned_address_class

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_MESSAGE = 4 * 1024 * 1024
_MAX_FRAMES_PER_MESSAGE = 256


class JsonWebSocket:
    def __init__(self, base_url: str, path: str, headers: Mapping[str, str], timeout_s: float,
                 redactor: Redactor, *, address_class: str | None = None) -> None:
        parsed = urlsplit(base_url)
        self._secure = parsed.scheme == "https"
        self._host = parsed.hostname or ""
        self._address_class = _commissioned_address_class(self._host, address_class)
        self._port = parsed.port or (443 if self._secure else 80)
        prefix = parsed.path.rstrip("/")
        self._path = prefix + "/" + path.lstrip("/")
        self._headers = dict(headers)
        self._timeout_s = timeout_s
        self._redactor = redactor
        self._socket: socket.socket | ssl.SSLSocket | None = None
        self._receive_buffer = bytearray()

    def __enter__(self) -> "JsonWebSocket":
        raw = _connect_authorized(self._host, self._port, self._timeout_s, self._address_class)
        if self._secure:
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=self._host)
        raw.settimeout(self._timeout_s)
        self._socket = raw
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host = self._host if self._port in {80, 443} else f"{self._host}:{self._port}"
        lines = [
            f"GET {self._path} HTTP/1.1",
            f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines.extend(f"{name}: {value}" for name, value in self._headers.items())
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        header = self._recv_until(b"\r\n\r\n", 64 * 1024)
        text = header.decode("iso-8859-1")
        first, *rest = text.split("\r\n")
        if " 101 " not in f" {first} ":
            raise ProtocolError("Moonraker websocket upgrade was unavailable")
        response_headers: dict[str, str] = {}
        for line in rest:
            if ":" in line:
                name, value = line.split(":", 1)
                response_headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        if response_headers.get("sec-websocket-accept") != expected:
            raise ProtocolError("Moonraker websocket returned an invalid accept key")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._socket is not None:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
            self._socket.close()
            self._socket = None

    def send_json(self, value: Any) -> None:
        self._send_frame(0x1, json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

    def recv_json(self) -> Any:
        fragments = bytearray()
        expecting_continuation = False
        frame_count = 0
        while True:
            final, opcode, payload = self._recv_frame()
            frame_count += 1
            if frame_count > _MAX_FRAMES_PER_MESSAGE:
                raise ProtocolError("Moonraker websocket message exceeded frame limit")
            if opcode == 0x8:
                raise ProtocolError("Moonraker websocket closed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                if expecting_continuation:
                    raise ProtocolError("Moonraker websocket interleaved a data frame")
                fragments = bytearray(payload)
                expecting_continuation = not final
            elif opcode == 0x0 and expecting_continuation:
                fragments.extend(payload)
                expecting_continuation = not final
            else:
                raise ProtocolError("Moonraker websocket returned an unsupported frame")
            if len(fragments) > _MAX_MESSAGE:
                raise ProtocolError("Moonraker websocket message exceeded size limit")
            if final:
                try:
                    value = json.loads(fragments.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError("Moonraker websocket returned malformed JSON") from exc
                return self._redactor.value(value)

    def _recv_until(self, marker: bytes, limit: int) -> bytes:
        if self._socket is None:
            raise ProtocolError("websocket is not connected")
        data = bytearray(self._receive_buffer)
        self._receive_buffer.clear()
        while marker not in data:
            block = self._socket.recv(4096)
            if not block:
                raise ProtocolError("Moonraker websocket closed during handshake")
            data.extend(block)
            if len(data) > limit:
                raise ProtocolError("Moonraker websocket handshake exceeded size limit")
        end = data.index(marker) + len(marker)
        self._receive_buffer.extend(data[end:])
        return bytes(data[:end])

    def _recv_exact(self, length: int) -> bytes:
        if self._socket is None:
            raise ProtocolError("websocket is not connected")
        data = bytearray()
        if self._receive_buffer:
            take = min(length, len(self._receive_buffer))
            data.extend(self._receive_buffer[:take])
            del self._receive_buffer[:take]
        while len(data) < length:
            block = self._socket.recv(length - len(data))
            if not block:
                raise ProtocolError("Moonraker websocket closed mid-frame")
            data.extend(block)
        return bytes(data)

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._recv_exact(2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if masked:
            raise ProtocolError("server websocket frames must not be masked")
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if length > _MAX_MESSAGE:
            raise ProtocolError("Moonraker websocket frame exceeded size limit")
        return final, opcode, self._recv_exact(length)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._socket is None:
            raise ProtocolError("websocket is not connected")
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + mask + masked)
