"""Shared subscription-auth plumbing: PKCE, local callback server, credential store.

Used by the codex/anthropic/cursor/xai subscription providers. Credentials are stored in
config.json under the "subscription_auth" key, keyed by provider_id. Each provider holds
named accounts: token cache is keyed (provider_id, account_id) with one active account per
provider. Legacy single-account entries migrate transparently to account_id ``"default"``.
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
DEFAULT_ACCOUNT_ID = "default"


def _load_cfg() -> dict:
    if paths.CONFIG.exists():
        try:
            return json.loads(paths.CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_cfg(cfg: dict) -> None:
    paths.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths.CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _load_all() -> dict:
    return _load_cfg().get(_AUTH_KEY, {}) or {}


def _is_multi_entry(entry) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("accounts"), dict)


def _normalize_entry(entry) -> dict:
    """Return multi-account shape ``{active, accounts}`` from legacy or multi storage.

    Does not persist. Legacy flat credential dicts become a single account under
    ``DEFAULT_ACCOUNT_ID`` (or the cred's own ``account_id`` when present).
    """
    if not entry or not isinstance(entry, dict):
        return {"active": DEFAULT_ACCOUNT_ID, "accounts": {}}
    if _is_multi_entry(entry):
        accounts = {str(k): dict(v) for k, v in entry["accounts"].items() if isinstance(v, dict)}
        active = entry.get("active")
        if active is None or str(active) not in accounts:
            active = next(iter(accounts), DEFAULT_ACCOUNT_ID)
        else:
            active = str(active)
        return {"active": active, "accounts": accounts}
    # Legacy single-account credential (flat token dict).
    raw_aid = entry.get("account_id")
    if raw_aid is not None and str(raw_aid).strip():
        aid = str(raw_aid).strip()
    else:
        aid = DEFAULT_ACCOUNT_ID
    return {"active": aid, "accounts": {aid: dict(entry)}}


def _persist_entry(provider_id: str, entry: dict) -> None:
    """Write a multi-account entry; drop the provider key when no accounts remain."""
    cfg = _load_cfg()
    auth = cfg.setdefault(_AUTH_KEY, {})
    accounts = entry.get("accounts") or {}
    if not accounts:
        auth.pop(provider_id, None)
    else:
        active = entry.get("active")
        if active is None or str(active) not in accounts:
            active = next(iter(accounts))
        auth[provider_id] = {
            "active": str(active),
            "accounts": {str(k): dict(v) for k, v in accounts.items()},
        }
    _write_cfg(cfg)


def _slug_account_id(value: str) -> str:
    """Filesystem/json-safe account id from an identity string."""
    s = "".join(ch if ch.isalnum() or ch in "._@+-" else "-" for ch in value.strip())
    s = s.strip("-._")
    return (s[:96] if s else DEFAULT_ACCOUNT_ID) or DEFAULT_ACCOUNT_ID


def derive_account_id(cred: dict | None) -> str:
    """Pick a stable account_id from token metadata, else a timestamp slug."""
    if not cred:
        return f"acct-{int(time.time())}"
    for key in ("account_id", "email", "sub", "user_id"):
        val = cred.get(key)
        if val is not None and str(val).strip():
            return _slug_account_id(str(val))
    return f"acct-{int(time.time())}"


def load_credential(provider_id: str, account_id: str | None = None) -> dict | None:
    """Load a credential for *provider_id*.

    When *account_id* is None, returns the ACTIVE account's token — this is the seam
    every ``*_sub_provider`` already uses, so multi-account is transparent to them.
    Migrates legacy flat cache entries on first read (in-memory only until next write).
    """
    entry = _normalize_entry(_load_all().get(provider_id))
    accounts = entry["accounts"]
    if not accounts:
        return None
    if account_id is not None:
        cred = accounts.get(str(account_id))
        return dict(cred) if cred else None
    active = entry["active"]
    cred = accounts.get(active)
    if cred is None:
        cred = accounts.get(DEFAULT_ACCOUNT_ID) or next(iter(accounts.values()), None)
    return dict(cred) if cred else None


def save_credential(
    provider_id: str,
    cred: dict,
    account_id: str | None = None,
    *,
    set_active: bool = True,
) -> str:
    """Save *cred* under (provider_id, account_id). Returns the account_id used.

    *account_id* defaults to ``cred['account_id']`` then ``DEFAULT_ACCOUNT_ID``.
    When *set_active* is True (default), that account becomes the provider's active one.
    Transparent migration: a legacy flat entry is rewritten as multi-account on save.
    """
    entry = _normalize_entry(_load_all().get(provider_id))
    if account_id is not None and str(account_id).strip():
        aid = str(account_id).strip()
    else:
        raw = cred.get("account_id") if isinstance(cred, dict) else None
        if raw is not None and str(raw).strip():
            aid = str(raw).strip()
        elif entry["accounts"] and entry.get("active") in entry["accounts"]:
            # Refresh path: providers often omit identity; update the active slot
            # instead of spawning a new "default" account.
            aid = entry["active"]
        else:
            aid = DEFAULT_ACCOUNT_ID
    stored = dict(cred)
    # Keep the identity field in sync with the cache key when missing and non-default.
    if not stored.get("account_id") and aid != DEFAULT_ACCOUNT_ID:
        stored["account_id"] = aid
    entry["accounts"][aid] = stored
    if set_active:
        entry["active"] = aid
    elif entry.get("active") not in entry["accounts"]:
        entry["active"] = aid
    _persist_entry(provider_id, entry)
    return aid


def clear_credential(provider_id: str, account_id: str | None = None) -> None:
    """Remove one account (active when *account_id* is None). Drops the provider when empty."""
    raw = _load_all().get(provider_id)
    if raw is None:
        return
    entry = _normalize_entry(raw)
    accounts = entry["accounts"]
    if not accounts:
        _persist_entry(provider_id, {"active": DEFAULT_ACCOUNT_ID, "accounts": {}})
        return
    if account_id is None:
        target = entry.get("active") or DEFAULT_ACCOUNT_ID
        if target not in accounts:
            target = next(iter(accounts))
    else:
        target = str(account_id)
    if target not in accounts:
        return
    del accounts[target]
    if entry.get("active") == target:
        entry["active"] = next(iter(accounts), DEFAULT_ACCOUNT_ID)
    entry["accounts"] = accounts
    _persist_entry(provider_id, entry)


def list_accounts(provider_id: str) -> list[dict]:
    """List stored accounts for *provider_id* (no tokens). Empty when disconnected.

    Each item: account_id, email, label, active, connected, expires_at_ms, plus
    optional identity fields ``display_name`` / ``plan`` when previously persisted
    (contracts/providers.md §4.5).
    """
    entry = _normalize_entry(_load_all().get(provider_id))
    active = entry["active"]
    out: list[dict] = []
    for aid, cred in entry["accounts"].items():
        email = cred.get("email")
        display_name = cred.get("display_name")
        plan = cred.get("plan")
        label = display_name or email or aid
        item = {
            "account_id": aid,
            "email": email,
            "label": label,
            "active": aid == active,
            "connected": True,
            "expires_at_ms": cred.get("expires_at_ms"),
        }
        if display_name:
            item["display_name"] = display_name
        if plan:
            item["plan"] = plan
        out.append(item)
    # Stable order: active first, then account_id.
    out.sort(key=lambda a: (not a["active"], a["account_id"] or ""))
    return out


def get_active_account_id(provider_id: str) -> str | None:
    """Return the active account_id, or None when the provider has no credentials."""
    entry = _normalize_entry(_load_all().get(provider_id))
    if not entry["accounts"]:
        return None
    return entry["active"]


def set_active_account(provider_id: str, account_id: str) -> bool:
    """Point the active account pointer at *account_id*. Returns False if unknown."""
    entry = _normalize_entry(_load_all().get(provider_id))
    aid = str(account_id)
    if aid not in entry["accounts"]:
        return False
    entry["active"] = aid
    _persist_entry(provider_id, entry)
    return True


def migrate_legacy_provider(provider_id: str) -> bool:
    """Persist multi-account shape for a legacy flat entry. Returns True if rewritten."""
    raw = _load_all().get(provider_id)
    if raw is None or _is_multi_entry(raw):
        return False
    if not isinstance(raw, dict):
        return False
    # Only migrate credential-looking dicts.
    if "access_token" not in raw and "refresh_token" not in raw:
        return False
    entry = _normalize_entry(raw)
    _persist_entry(provider_id, entry)
    return True

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
