"""In-process Moonraker, OctoPrint, and PrusaLink virtual printer APIs."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import json
import re
import threading
import struct
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

_FILENAME = re.compile(br'filename="([^"\r\n]+)"')


class _Server(ThreadingHTTPServer):
    daemon_threads = True


class VirtualPrinter:
    def __init__(self, kind: str, *, api_key: str | None = None) -> None:
        if kind not in {"moonraker", "octoprint", "prusalink"}:
            raise ValueError("unknown virtual printer kind")
        self.kind = kind
        self.api_key = api_key
        self.state = "standby" if kind == "moonraker" else ("Operational" if kind == "octoprint" else "READY")
        self.remote_name = "shared.gcode"
        self.selected_name: str | None = None
        self.telemetry_gap = False
        self.malformed_paths: set[str] = set()
        self.html_error_paths: set[str] = set()
        self.timeout_paths: set[str] = set()
        self.timeout_delay_s = 0.25
        self.oversized_paths: set[str] = set()
        self.raw_blob_bytes = 0
        self.octoprint_upload_path_override: str | None = None
        self.log_lines: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.prusa_job_endpoint = False
        self.prusa_storage_endpoint = False
        self.moonraker_websocket = False
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("virtual printer is not running")
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "VirtualPrinter":
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ChudwareVirtualPrinter/1"

            def log_message(self, fmt: str, *args: Any) -> None:
                # Never copy request headers or bodies into fixture logs.
                fixture.log_lines.append(f"{self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}".strip())

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

            def do_OPTIONS(self) -> None:
                self._dispatch("OPTIONS")

            def _dispatch(self, method: str) -> None:
                parsed = urlsplit(self.path)
                path = parsed.path
                fixture.requests.append({"method": method, "path": path})
                if path in fixture.timeout_paths:
                    time.sleep(fixture.timeout_delay_s)
                if path in fixture.html_error_paths:
                    self._send_bytes(500, b"<html>vendor failed</html>", "text/html")
                    return
                if path in fixture.malformed_paths:
                    self._send_bytes(200, b'{"broken":', "application/json")
                    return
                if path in fixture.oversized_paths:
                    self._send_bytes(
                        200,
                        b'{"blob":"' + (b"x" * ((1 << 20) + 32)) + b'"}',
                        "application/json",
                    )
                    return
                if fixture.api_key and self.headers.get("X-Api-Key") != fixture.api_key:
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    if fixture.kind == "prusalink":
                        self.send_header("WWW-Authenticate", 'Digest realm="virtual-printer", nonce="fixed"')
                    body = b'{"error":"authentication required"}'
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
                if fixture.kind == "moonraker":
                    self._moonraker(method, path, parsed.query, body)
                elif fixture.kind == "octoprint":
                    self._octoprint(method, path, body)
                else:
                    self._prusalink(method, path, body)

            def _moonraker(self, method: str, path: str, query: str, body: bytes) -> None:
                if method == "GET" and path == "/printer/info":
                    self._json(200, {"result": {"state": "ready", "software_version": "v0.13.0", "hostname": "virtual"}})
                    return
                if method == "GET" and path == "/printer/objects/query":
                    objects: dict[str, Any] = {
                        "print_stats": {"state": fixture.state, "filename": fixture.remote_name},
                        "virtual_sdcard": {"progress": 0.5},
                        "extruder": {"temperature": 210.0, "target": 215.0},
                        "heater_bed": {"temperature": 59.0, "target": 60.0},
                        "toolhead": {"position": [1.0, 2.0, 3.0]},
                        "fan": {"speed": 0.5},
                    }
                    if fixture.raw_blob_bytes:
                        objects["vendor_blob"] = "x" * fixture.raw_blob_bytes
                    if fixture.telemetry_gap:
                        objects.pop("virtual_sdcard")
                        objects.pop("extruder")
                    self._json(200, {"result": {"eventtime": 42.0, "status": objects}})
                    return
                if method == "GET" and path == "/websocket":
                    if not fixture.moonraker_websocket:
                        self._json(404, {"error": "websocket disabled by fixture"})
                        return
                    key = self.headers.get("Sec-WebSocket-Key", "")
                    accept = base64.b64encode(
                        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                    ).decode("ascii")
                    self.send_response(101, "Switching Protocols")
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self._read_websocket_frame()
                    self._send_websocket_json(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {
                                "status": {
                                    "print_stats": {"state": fixture.state, "filename": fixture.remote_name},
                                    "virtual_sdcard": {"progress": 0.5},
                                    "extruder": {"temperature": 210.0, "target": 215.0},
                                    "heater_bed": {"temperature": 59.0, "target": 60.0},
                                }
                            },
                        }
                    )
                    return
                if method == "POST" and path == "/server/files/upload":
                    fixture.remote_name = self._multipart_filename(body) or fixture.remote_name
                    self._json(201, {"result": {"item": {"path": fixture.remote_name}}})
                    return
                commands = {
                    "/printer/print/start": "printing",
                    "/printer/print/pause": "paused",
                    "/printer/print/resume": "printing",
                    "/printer/print/cancel": "cancelled",
                }
                if method == "POST" and path in commands:
                    fixture.state = commands[path]
                    if path == "/printer/print/start":
                        requested = parse_qs(query).get("filename", [None])[0]
                        if requested:
                            fixture.remote_name = requested
                    self._json(200, {"result": "ok"})
                    return
                self._json(404, {"error": "not found"})

            def _octoprint(self, method: str, path: str, body: bytes) -> None:
                if method == "GET" and path == "/api/version":
                    self._json(200, {"api": "0.1", "server": "1.11.8", "text": "OctoPrint 1.11.8"})
                    return
                if method == "GET" and path == "/api/printer":
                    self._json(
                        200,
                        {
                            "state": {"text": fixture.state},
                            "temperature": {
                                "tool0": {"actual": 210.0, "target": 215.0},
                                "bed": {"actual": 59.0, "target": 60.0},
                            },
                        },
                    )
                    return
                if method == "GET" and path == "/api/job":
                    self._json(
                        200,
                        {
                            "state": fixture.state,
                            "job": {"file": {"name": fixture.remote_name}},
                            "progress": {"completion": 50.0},
                        },
                    )
                    return
                if method == "POST" and path == "/api/files/local":
                    fixture.remote_name = self._multipart_filename(body) or fixture.remote_name
                    remote_path = fixture.octoprint_upload_path_override or fixture.remote_name
                    self._json(201, {"files": {"local": {"name": fixture.remote_name, "path": remote_path}}})
                    return
                if method == "POST" and path.startswith("/api/files/local/"):
                    payload = self._body_json(body)
                    if payload.get("command") == "select":
                        fixture.selected_name = unquote(path.removeprefix("/api/files/local/"))
                    self._json(200, {})
                    return
                if method == "POST" and path == "/api/job":
                    payload = self._body_json(body)
                    command = payload.get("command")
                    action = payload.get("action")
                    if command == "start":
                        fixture.state = "Printing"
                    elif command == "cancel":
                        fixture.state = "Cancelled"
                    elif command == "pause" and action == "pause":
                        fixture.state = "Paused"
                    elif command == "pause" and action == "resume":
                        fixture.state = "Printing"
                    self._json(200, {})
                    return
                self._json(404, {"error": "not found"})

            def _prusalink(self, method: str, path: str, body: bytes) -> None:
                if method == "GET" and path == "/api/version":
                    capabilities = {"upload-by-put": True} if fixture.prusa_storage_endpoint else {}
                    self._json(
                        200,
                        {
                            "api": "1.0.0",
                            "version": "0.8.2",
                            "printer": "virtual",
                            "text": "PrusaLink virtual",
                            "firmware": "6.6.3",
                            "capabilities": capabilities,
                        },
                    )
                    return
                if method == "GET" and path == "/api/v1/status":
                    payload: dict[str, Any] = {
                        "printer": {
                            "state": fixture.state,
                            "temp_nozzle": 210.0,
                            "target_nozzle": 215.0,
                            "temp_bed": 59.0,
                            "target_bed": 60.0,
                        }
                    }
                    if fixture.prusa_job_endpoint:
                        payload["job"] = {
                            "id": 7,
                            "progress": 50.0,
                            "file": {"display_name": fixture.remote_name},
                        }
                    self._json(200, payload)
                    return
                if method == "GET" and path == "/api/v1/job" and fixture.prusa_job_endpoint:
                    self._json(200, {"id": 7, "state": "PRINTING", "progress": 50.0, "time_printing": 10})
                    return
                if method == "GET" and path == "/api/v1/storage" and fixture.prusa_storage_endpoint:
                    self._json(200, {"storage_list": [{"available": True, "read_only": False, "path": "/local", "type": "LOCAL"}]})
                    return
                if method == "OPTIONS" and fixture.prusa_storage_endpoint and path.startswith("/api/v1/files/local/"):
                    self._empty(204, allow="GET, HEAD, PUT, POST, DELETE")
                    return
                if method == "OPTIONS" and fixture.prusa_job_endpoint and path.endswith("/pause"):
                    self._empty(204, allow="PUT")
                    return
                if method == "OPTIONS" and fixture.prusa_job_endpoint and path.endswith("/resume"):
                    self._empty(204, allow="PUT")
                    return
                if method == "OPTIONS" and fixture.prusa_job_endpoint and re.fullmatch(r"/api/v1/job/\d+", path):
                    self._empty(204, allow="GET, DELETE")
                    return
                if fixture.prusa_storage_endpoint and path.startswith("/api/v1/files/local/"):
                    if method == "PUT":
                        fixture.remote_name = unquote(path.rsplit("/", 1)[-1])
                        self._empty(201)
                        return
                    if method == "POST":
                        fixture.state = "PRINTING"
                        self._empty(204)
                        return
                if fixture.prusa_job_endpoint and path.endswith("/pause") and method == "PUT":
                    fixture.state = "PAUSED"
                    self._empty(204)
                    return
                if fixture.prusa_job_endpoint and path.endswith("/resume") and method == "PUT":
                    fixture.state = "PRINTING"
                    self._empty(204)
                    return
                if fixture.prusa_job_endpoint and re.fullmatch(r"/api/v1/job/\d+", path) and method == "DELETE":
                    fixture.state = "STOPPED"
                    self._empty(204)
                    return
                self._json(404, {"error": "not found"})

            @staticmethod
            def _multipart_filename(body: bytes) -> str | None:
                match = _FILENAME.search(body)
                return match.group(1).decode("utf-8") if match else None

            @staticmethod
            def _body_json(body: bytes) -> dict[str, Any]:
                value = json.loads(body.decode("utf-8")) if body else {}
                return value if isinstance(value, dict) else {}

            def _read_websocket_frame(self) -> bytes:
                first = self.rfile.read(2)
                if len(first) != 2:
                    return b""
                length = first[1] & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self.rfile.read(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self.rfile.read(8))[0]
                mask = self.rfile.read(4) if first[1] & 0x80 else b""
                payload = self.rfile.read(length)
                if mask:
                    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                return payload

            def _send_websocket_json(self, value: Any) -> None:
                payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
                header = bytearray([0x81])
                if len(payload) < 126:
                    header.append(len(payload))
                elif len(payload) <= 0xFFFF:
                    header.append(126)
                    header.extend(struct.pack("!H", len(payload)))
                else:
                    header.append(127)
                    header.extend(struct.pack("!Q", len(payload)))
                self.wfile.write(bytes(header) + payload)
                self.wfile.flush()

            def _json(self, status: int, payload: Any) -> None:
                self._send_bytes(status, json.dumps(payload, separators=(",", ":")).encode("utf-8"), "application/json")

            def _empty(self, status: int, *, allow: str | None = None) -> None:
                self.send_response(status)
                if allow:
                    self.send_header("Allow", allow)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._server = _Server(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name=f"virtual-{self.kind}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
