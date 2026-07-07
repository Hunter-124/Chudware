"""Shared subscription-auth plumbing: PKCE, local callback server, credential store.

Used by the codex/anthropic/cursor subscription providers. Credentials are stored in
config.json under the "subscription_auth" key, keyed by provider_id.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

from engine import paths


# ── PKCE (96-byte verifier to match reference clients) ──────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_verifier() -> str:
    return _b64url(secrets.token_bytes(96))


def challenge_for(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def random_state() -> str:
    return secrets.token_urlsafe(32)


def random_uuid() -> str:
    return str(uuid.uuid4())


# ── Local OAuth callback server ─────────────────────────────────────────────

class CallbackServer:
    """Single-use HTTP server that waits for an OAuth redirect on a fixed port."""

    def __init__(self, port: int, path: str, timeout: float = 120.0):
        self.port = port
        self.path = path.rstrip("/")
        self.timeout = timeout
        self._result: dict | None = None
        self._error: str | None = None
        self._event = threading.Event()
        self._httpd: http.server.HTTPServer | None = None

    def _handler_factory(self):
        server_ref = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path.rstrip("/") != server_ref.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                params = parse_qs(parsed.query)
                code = (params.get("code") or [None])[0]
                state = (params.get("state") or [None])[0]
                error = (params.get("error") or [None])[0]
                if error:
                    server_ref._error = error
                    body = b"<html><body><h2>Login failed</h2><p>You can close this tab.</p></body></html>"
                elif code:
                    server_ref._result = {"code": code, "state": state}
                    body = b"<html><body><h2>Login successful</h2><p>You can close this tab.</p></body></html>"
                else:
                    server_ref._error = "no code in callback"
                    body = b"<html><body><h2>Missing code</h2></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                server_ref._event.set()

            def log_message(self, format, *args):
                pass  # silence request logging

        return Handler

    def _make_server(self) -> http.server.HTTPServer:
        """Bind the listener on whatever `localhost` resolves to (IPv4 or IPv6).

        The vendor redirect URIs use the host ``localhost``. If the browser resolves that
        to ``::1`` while we listen only on ``127.0.0.1`` (or vice-versa), the callback never
        lands and we'd eat the full timeout. Resolving here and matching the address family
        keeps both IPv4-only and IPv6-first machines working.
        """
        import socket

        infos = socket.getaddrinfo("localhost", self.port, 0, socket.SOCK_STREAM)
        # Prefer IPv4 (127.0.0.1) when available — the most common localhost mapping —
        # then fall back to whatever family resolved (e.g. ::1 on IPv6-only boxes).
        infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
        family, _stype, _proto, _canon, sockaddr = infos[0]

        class _Server(http.server.HTTPServer):
            address_family = family

        return _Server((sockaddr[0], self.port), self._handler_factory())

    def wait_for_callback(self, expected_state: str | None = None) -> dict:
        """Block until the OAuth redirect arrives. Returns {"code": ...} or raises."""
        try:
            self._httpd = self._make_server()
        except OSError as e:
            raise RuntimeError(
                f"Cannot bind port {self.port} for OAuth callback — is another process "
                f"using it? Free the port or use the device-code flow. ({e})"
            ) from e

        thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        thread.start()
        try:
            if not self._event.wait(timeout=self.timeout):
                raise RuntimeError(
                    f"OAuth callback timed out after {self.timeout:.0f}s — "
                    f"did you complete the login in your browser?"
                )
        finally:
            self._httpd.shutdown()
            thread.join(timeout=5)

        if self._error:
            raise RuntimeError(f"OAuth login failed: {self._error}")
        if not self._result:
            raise RuntimeError("OAuth callback received but no code was captured")
        if expected_state and self._result.get("state") != expected_state:
            raise RuntimeError("OAuth state mismatch — possible CSRF attack")
        return self._result


# ── Credential store (config.json) ──────────────────────────────────────────

_AUTH_KEY = "subscription_auth"


def _load_all() -> dict:
    if paths.CONFIG.exists():
        try:
            cfg = json.loads(paths.CONFIG.read_text(encoding="utf-8"))
            return cfg.get(_AUTH_KEY, {})
        except Exception:
            return {}
    return {}


def load_credential(provider_id: str) -> dict | None:
    creds = _load_all()
    return creds.get(provider_id)


def save_credential(provider_id: str, cred: dict) -> None:
    if paths.CONFIG.exists():
        try:
            cfg = json.loads(paths.CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}
    auth = cfg.setdefault(_AUTH_KEY, {})
    auth[provider_id] = cred
    paths.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths.CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def clear_credential(provider_id: str) -> None:
    if not paths.CONFIG.exists():
        return
    try:
        cfg = json.loads(paths.CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return
    auth = cfg.get(_AUTH_KEY, {})
    if provider_id in auth:
        del auth[provider_id]
        paths.CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Token lifecycle helpers ─────────────────────────────────────────────────

def is_expired(cred: dict, skew_ms: int = 0) -> bool:
    expires = cred.get("expires_at_ms", 0)
    return time.time() * 1000 >= (expires - skew_ms)


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload without signature verification (client-side claim extraction)."""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = 4 - len(payload) % 4
    if padding != 4:
        payload += "=" * padding
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# ── HTTP helpers (no extra dependencies) ────────────────────────────────────

def post_json(url: str, body: dict, headers: dict | None = None,
              timeout: float = 30.0) -> dict:
    import urllib.request
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_form(url: str, fields: dict, headers: dict | None = None,
              timeout: float = 30.0) -> dict:
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode(fields).encode("utf-8")
    hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str, headers: dict | None = None, timeout: float = 30.0) -> dict:
    import urllib.request
    hdrs = headers or {}
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
