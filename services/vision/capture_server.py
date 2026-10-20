"""Local HTTP capture server for guided multi-photo ingestion.

Default OFF. Start with start_session(port, token) -> returns QR string (data URL PNG).
Guided shot list: top, front, right, back, left, bottom, 3x angled 45 degrees.
Upload endpoint: POST /upload?token=<tok> with multipart/form-data photo field.
All endpoints check token. The listener binds to one selected LAN interface, not all interfaces.
"""
from __future__ import annotations

import hmac
import io
import secrets
import socket
import tempfile
import threading
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as _EMAIL_POLICY
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import parse_qs, urlparse

try:
    import qrcode  # type: ignore
    import base64 as _b64
    HAVE_QRCODE = True
except Exception:  # noqa: BLE001
    qrcode = None  # type: ignore
    HAVE_QRCODE = False

_HANDSHAKE_TIMEOUT_S = 5.0
_BODY_TIMEOUT_S = 30.0
_MAX_CONNECTIONS = 16
_MAX_HEADER_BYTES = 16 * 1024
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024
_MAX_SESSION_BYTES = 256 * 1024 * 1024
_MAX_UPLOAD_COUNT = 128


def _close_connection(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


class _CaptureServer(ThreadingMixIn, HTTPServer):
    """Bounded concurrent connections, each with an absolute pre-auth deadline."""
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, handler):
        self._slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self._connections = set()
        self._connections_lock = threading.Lock()
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            _close_connection(request)
            return
        request.settimeout(_HANDSHAKE_TIMEOUT_S)
        with self._connections_lock:
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._connections_lock:
                self._connections.discard(request)
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._connections_lock:
                self._connections.discard(request)
            self._slots.release()

    def server_close(self):
        with self._connections_lock:
            for request in self._connections:
                _close_connection(request)
        super().server_close()


class _HeaderReader:
    """Budget the request line and all headers before the token can be checked."""
    def __init__(self, stream):
        self.stream = stream
        self.remaining = _MAX_HEADER_BYTES

    def readline(self, size=-1):
        line = self.stream.readline(min(size if size >= 0 else self.remaining + 1,
                                        self.remaining + 1))
        self.remaining -= len(line)
        if self.remaining < 0:
            raise TimeoutError("capture headers exceed limit")
        return line

    def close(self):
        self.stream.close()

_GUIDED_SHOTS = [
    "top",
    "front",
    "right",
    "back",
    "left",
    "bottom",
    "angle_front_left_45",
    "angle_front_right_45",
    "angle_back_left_45",
    "angle_top_front_45",
    "angle_top_back_45",
]

_GUIDED_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chudware Photo Capture</title>
<style>
  body {{ font-family: sans-serif; max-width: 480px; margin: 0 auto; padding: 1em; }}
  h2 {{ color: #333; }}
  .shot {{ margin: 0.5em 0; padding: 0.75em; border: 1px solid #ddd; border-radius: 6px; }}
  .shot.done {{ background: #eaffea; border-color: #6c6; }}
  .shot.current {{ background: #fff8e6; border-color: #ca0; font-weight: bold; }}
  input[type=file] {{ display: block; margin: 1em 0; }}
  button {{ background: #0070d2; color: white; border: none; padding: 0.5em 1.5em;
            border-radius: 4px; font-size: 1em; cursor: pointer; }}
</style>
</head>
<body>
<h2>Chudware Photo Capture</h2>
<p>Upload photos for each angle. After all shots, close this page.</p>
<p>Received: <strong>{received_count}</strong> / {total_count}</p>
<ul>
{shot_list}
</ul>
<form method="POST" action="/upload?token={token}" enctype="multipart/form-data">
  <label>Choose photo:</label>
  <input type="file" name="photo" accept="image/*" capture="environment" required>
  <button type="submit">Upload Photo</button>
</form>
</body>
</html>
"""


def _lan_ip() -> str:
    """Return the LAN IP address (not 0.0.0.0/localhost) so the phone can reach it."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


def _make_qr_data_url(url: str) -> str:
    """Return a PNG data URL of the QR code, or just the URL if qrcode is absent."""
    if not HAVE_QRCODE:
        return url
    try:
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:  # noqa: BLE001
        return url


@dataclass
class CaptureSession:
    port: int
    token: str
    shot_names: list
    received: list = field(default_factory=list)
    # A plain persistent directory (tempfile.mkdtemp) — it must OUTLIVE the session so the
    # reconstructed mesh can be handed to reverse_engineer(source=...) after stop_session.
    # The caller owns cleanup.
    work_dir: Optional[str] = field(default=None, repr=False)
    _server: Optional[HTTPServer] = field(default=None, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    bind_host: str = "127.0.0.1"
    _upload_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _received_bytes: int = field(default=0, repr=False)

    @property
    def upload_url(self) -> str:
        ip = self.bind_host
        return f"http://{ip}:{self.port}/upload?token={self.token}"

    @property
    def qr(self) -> str:
        """QR data URL (PNG) or the raw URL if qrcode is not installed."""
        return _make_qr_data_url(self.upload_url)


def _parse_multipart_photo(headers, rfile):
    """Extract (filename, bytes) of the 'photo' field from a multipart/form-data body.

    Stdlib-only replacement for cgi.FieldStorage (cgi was removed in Python 3.13): the raw
    body is re-wrapped as a MIME document and split with email.parser.
    Returns (None, None) when the body has no usable photo part.
    """
    ctype = headers.get("Content-Type", "")
    if headers.get("Transfer-Encoding") is not None:
        raise ValueError("transfer encoding is not supported")
    lengths = headers.get_all("Content-Length", []) if hasattr(headers, "get_all") else [headers.get("Content-Length", "0")]
    if len(lengths) != 1:
        raise ValueError("exactly one Content-Length is required")
    try:
        length = int(lengths[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Content-Length") from exc
    if length > _MAX_UPLOAD_BYTES:
        raise ValueError("photo exceeds upload limit")
    if "multipart/form-data" not in ctype or length <= 0:
        return None, None
    body = rfile.read(length)
    if len(body) != length:
        raise ValueError("incomplete photo upload")
    doc = (b"Content-Type: " + ctype.encode("latin-1") +
           b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    msg = BytesParser(policy=_EMAIL_POLICY).parsebytes(doc)
    if not msg.is_multipart():
        return None, None
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == "photo":
            payload = part.get_payload(decode=True)
            if payload:
                return part.get_filename(), payload
    return None, None


def _make_handler(session: CaptureSession):
    """Build a request handler class bound to the given session."""

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self._body_stream = self.rfile
            self.rfile = _HeaderReader(self.rfile)
            self._deadline = threading.Timer(_HANDSHAKE_TIMEOUT_S, _close_connection,
                                             args=(self.connection,))
            self._deadline.daemon = True
            self._deadline.start()

        def finish(self):
            self._deadline.cancel()
            self.rfile = self._body_stream
            super().finish()

        def log_message(self, fmt, *args):  # suppress default stderr logging
            pass

        def _check_token(self) -> bool:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            tok = (qs.get("token") or [""])[0]
            authorized = hmac.compare_digest(tok, session.token)
            if authorized:
                self._deadline.cancel()
                self.rfile = self._body_stream
                self.connection.settimeout(_BODY_TIMEOUT_S)
                self._deadline = threading.Timer(_BODY_TIMEOUT_S, _close_connection,
                                                 args=(self.connection,))
                self._deadline.daemon = True
                self._deadline.start()
            return authorized

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path not in ("/", "/upload"):
                self.send_response(404)
                self.end_headers()
                return
            if not self._check_token():
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"401 Unauthorized")
                return
            # Render the guided page
            shot_items = []
            for i, name in enumerate(session.shot_names):
                # Label received shots by matching filename prefix
                received_count_so_far = len([p for p in session.received
                                              if f"_{name}" in Path(p).stem or Path(p).stem.endswith(name)])
                done = received_count_so_far > 0
                current = (i == len(session.received)) and not done
                cls = "done" if done else ("current" if current else "shot")
                shot_items.append(f'<li class="{cls}">{name}{"  ✓" if done else ""}</li>')
            page = _GUIDED_PAGE_TEMPLATE.format(
                received_count=len(session.received),
                total_count=len(session.shot_names),
                shot_list="\n".join(shot_items),
                token=session.token,
            )
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/upload":
                self.send_response(404)
                self.end_headers()
                return
            if not self._check_token():
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"401 Unauthorized")
                return
            # Parse multipart form data with the stdlib email parser (the cgi module was
            # REMOVED in Python 3.13 — do not reintroduce it).
            try:
                filename, data = _parse_multipart_photo(self.headers, self.rfile)
                if data is None:
                    raise ValueError("no 'photo' field in multipart body")
                ext = ".jpg"
                if filename:
                    fn_ext = Path(filename).suffix.lower()
                    if fn_ext in (".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"):
                        ext = fn_ext
                with session._upload_lock:
                    idx = len(session.received)
                    if idx >= _MAX_UPLOAD_COUNT or session._received_bytes + len(data) > _MAX_SESSION_BYTES:
                        raise ValueError("capture session upload limit reached")
                    shot_name = (session.shot_names[idx] if idx < len(session.shot_names)
                                 else f"extra_{idx}")
                    work_path = Path(session.work_dir)
                    dest = work_path / f"{idx:03d}_{shot_name}{ext}"
                    dest.write_bytes(data)
                    session.received.append(str(dest))
                    session._received_bytes += len(data)
                body = b"OK"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # noqa: BLE001
                body = f"Upload error: {exc}".encode("utf-8")
                self.send_response(400 if isinstance(exc, (ValueError, TimeoutError)) else 500)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    return Handler


def get_guided_shot_names() -> list:
    """Return the 11 standard guided shot names."""
    return list(_GUIDED_SHOTS)


def start_session(port: int = 8742, token: Optional[str] = None,
                  bind_host: Optional[str] = None) -> CaptureSession:
    """Start a local capture HTTP server; return a CaptureSession with QR info.

    The session's .qr property returns a PNG data URL (or raw URL if qrcode is absent).
    By default binds only to the selected LAN interface. Pass a specific local interface
    (e.g. 127.0.0.1 for local-only use). Wildcard binds are refused.
    """
    if token is None:
        token = secrets.token_urlsafe(16)
    bind_host = bind_host or _lan_ip()
    if bind_host in {"0.0.0.0", "::", ""}:
        raise ValueError("capture requires a specific local interface")
    work_dir = tempfile.mkdtemp(prefix="chudware_capture_")   # persists past stop_session
    session = CaptureSession(
        port=port,
        token=token,
        shot_names=list(_GUIDED_SHOTS),
        work_dir=work_dir,
        bind_host=bind_host,
    )
    handler_class = _make_handler(session)
    server = _CaptureServer((bind_host, port), handler_class)
    session.port = server.server_address[1]                # resolves port=0 -> real port
    session._server = server
    t = threading.Thread(target=server.serve_forever, name=f"chudware-capture-{port}", daemon=True)
    t.start()
    session._thread = t
    return session


def stop_session(session: CaptureSession) -> None:
    """Shut down the HTTP server. The caller is responsible for session.work_dir cleanup."""
    if session._server is not None:
        session._server.shutdown()
        session._server.server_close()
        session._server = None
