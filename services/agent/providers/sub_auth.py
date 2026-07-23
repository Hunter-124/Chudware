"""Shared subscription-auth plumbing: PKCE, local callback server, credential store.

Used by the codex/anthropic/cursor/xai subscription providers. Credentials are stored in
config.json under the "subscription_auth" key, keyed by provider_id. Each provider holds
named accounts: token cache is keyed (provider_id, account_id) with one active account per
provider. Legacy single-account entries migrate transparently to account_id ``"default"``.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

from services import paths


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
                    body = (
                        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                        "<title>Chudware — Login failed</title>"
                        "<style>"
                        "*{box-sizing:border-box;margin:0;padding:0}"
                        "body{min-height:100vh;display:flex;align-items:center;justify-content:center;"
                        "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
                        "background:radial-gradient(1200px 600px at 50% -10%,#1e293b 0%,#0b1220 55%,#070b14 100%);"
                        "color:#e2e8f0}"
                        ".card{width:min(420px,92vw);padding:2rem 1.75rem 1.75rem;border-radius:16px;"
                        "background:rgba(15,23,42,.72);border:1px solid rgba(148,163,184,.18);"
                        "box-shadow:0 24px 60px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.04);"
                        "backdrop-filter:blur(12px);text-align:center}"
                        ".brand{font-size:.8rem;letter-spacing:.16em;text-transform:uppercase;"
                        "color:#94a3b8;margin-bottom:1.25rem;font-weight:600}"
                        ".icon{width:56px;height:56px;margin:0 auto 1rem;border-radius:50%;"
                        "display:flex;align-items:center;justify-content:center;font-size:1.75rem;"
                        "background:rgba(239,68,68,.12);color:#f87171;"
                        "border:1px solid rgba(248,113,113,.35)}"
                        "h1{font-size:1.35rem;font-weight:650;color:#f8fafc;margin-bottom:.5rem}"
                        "p{font-size:.95rem;line-height:1.5;color:#94a3b8}"
                        "</style></head><body><div class=\"card\">"
                        "<div class=\"brand\">Chudware</div>"
                        "<div class=\"icon\" aria-hidden=\"true\">✕</div>"
                        "<h1>Login failed</h1>"
                        "<p>Something went wrong during sign-in. You can close this tab and return to Chudware to try again.</p>"
                        "</div></body></html>"
                    ).encode("utf-8")
                elif code:
                    server_ref._result = {"code": code, "state": state}
                    body = (
                        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                        "<title>Chudware — Login successful</title>"
                        "<style>"
                        "*{box-sizing:border-box;margin:0;padding:0}"
                        "body{min-height:100vh;display:flex;align-items:center;justify-content:center;"
                        "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
                        "background:radial-gradient(1200px 600px at 50% -10%,#1e293b 0%,#0b1220 55%,#070b14 100%);"
                        "color:#e2e8f0}"
                        ".card{width:min(420px,92vw);padding:2rem 1.75rem 1.75rem;border-radius:16px;"
                        "background:rgba(15,23,42,.72);border:1px solid rgba(148,163,184,.18);"
                        "box-shadow:0 24px 60px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.04);"
                        "backdrop-filter:blur(12px);text-align:center}"
                        ".brand{font-size:.8rem;letter-spacing:.16em;text-transform:uppercase;"
                        "color:#94a3b8;margin-bottom:1.25rem;font-weight:600}"
                        ".icon{width:56px;height:56px;margin:0 auto 1rem;border-radius:50%;"
                        "display:flex;align-items:center;justify-content:center;font-size:1.75rem;"
                        "background:rgba(34,197,94,.12);color:#4ade80;"
                        "border:1px solid rgba(74,222,128,.35)}"
                        "h1{font-size:1.35rem;font-weight:650;color:#f8fafc;margin-bottom:.5rem}"
                        "p{font-size:.95rem;line-height:1.5;color:#94a3b8}"
                        "</style></head><body><div class=\"card\">"
                        "<div class=\"brand\">Chudware</div>"
                        "<div class=\"icon\" aria-hidden=\"true\">✓</div>"
                        "<h1>Login successful</h1>"
                        "<p>You can close this tab and return to Chudware.</p>"
                        "</div></body></html>"
                    ).encode("utf-8")
                else:
                    server_ref._error = "no code in callback"
                    body = (
                        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                        "<title>Chudware — Missing code</title>"
                        "<style>"
                        "*{box-sizing:border-box;margin:0;padding:0}"
                        "body{min-height:100vh;display:flex;align-items:center;justify-content:center;"
                        "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
                        "background:radial-gradient(1200px 600px at 50% -10%,#1e293b 0%,#0b1220 55%,#070b14 100%);"
                        "color:#e2e8f0}"
                        ".card{width:min(420px,92vw);padding:2rem 1.75rem 1.75rem;border-radius:16px;"
                        "background:rgba(15,23,42,.72);border:1px solid rgba(148,163,184,.18);"
                        "box-shadow:0 24px 60px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.04);"
                        "backdrop-filter:blur(12px);text-align:center}"
                        ".brand{font-size:.8rem;letter-spacing:.16em;text-transform:uppercase;"
                        "color:#94a3b8;margin-bottom:1.25rem;font-weight:600}"
                        ".icon{width:56px;height:56px;margin:0 auto 1rem;border-radius:50%;"
                        "display:flex;align-items:center;justify-content:center;font-size:1.75rem;"
                        "background:rgba(245,158,11,.12);color:#fbbf24;"
                        "border:1px solid rgba(251,191,36,.35)}"
                        "h1{font-size:1.35rem;font-weight:650;color:#f8fafc;margin-bottom:.5rem}"
                        "p{font-size:.95rem;line-height:1.5;color:#94a3b8}"
                        "</style></head><body><div class=\"card\">"
                        "<div class=\"brand\">Chudware</div>"
                        "<div class=\"icon\" aria-hidden=\"true\">!</div>"
                        "<h1>Missing code</h1>"
                        "<p>No authorization code was returned. You can close this tab and return to Chudware to try again.</p>"
                        "</div></body></html>"
                    ).encode("utf-8")
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
# config.json is shared by the GUI worker, Settings usage polls, diag scripts,
# and agent turns. xAI rotates refresh tokens on EVERY exchange — a stale
# read-modify-write of the whole file (e.g. persist_identity on account B while
# refresh persists account A) silently reverts the new RT and the next refresh
# dies with invalid_grant / "Refresh token has been revoked".
#
# All mutations take a process lock + exclusive file lock, re-read disk, and
# patch only the touched account (never rewrite a stale multi-account snapshot).

_AUTH_KEY = "subscription_auth"
DEFAULT_ACCOUNT_ID = "default"
_cfg_lock = threading.RLock()
_LOCK_SUFFIX = ".lock"


def _lock_path():
    return paths.CONFIG.with_name(paths.CONFIG.name + _LOCK_SUFFIX)


def _secure_perms(path) -> None:
    """Best-effort: ensure *path* is owner-only (0600).

    OAuth tokens for every provider live in the credential store and its
    lockfiles — never leave them world/group readable. Some filesystems
    (FUSE mounts, exotic NAS shares) reject chmod; ignore failures rather
    than crash a credential read/write over it.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_cfg() -> dict:
    if paths.CONFIG.exists():
        _secure_perms(paths.CONFIG)
        try:
            return json.loads(paths.CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_cfg(cfg: dict) -> None:
    paths.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so concurrent readers never see a partial JSON document.
    tmp = paths.CONFIG.with_name(paths.CONFIG.name + ".tmp")
    data = json.dumps(cfg, indent=2)
    tmp.write_text(data, encoding="utf-8")
    # OAuth tokens for every provider live in here — never world/group readable.
    _secure_perms(tmp)
    tmp.replace(paths.CONFIG)


def _file_lock(lf):
    """Exclusive flock when available (POSIX); no-op fallback otherwise."""
    try:
        import fcntl  # noqa: PLC0415 — optional on non-POSIX
    except ImportError:  # pragma: no cover
        return None

    class _Fl:
        def __enter__(self_inner):
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            return lf

        def __exit__(self_inner, *exc):
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            return False

    return _Fl()


def _with_cfg_lock(fn):
    """Run *fn* holding the process + file lock. *fn* receives nothing."""
    with _cfg_lock:
        paths.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        lock_p = _lock_path()
        with open(lock_p, "a+", encoding="utf-8") as lf:
            _secure_perms(lock_p)
            fl = _file_lock(lf)
            if fl is None:
                return fn()
            with fl:
                return fn()


def _account_lock_path(provider_id: str, account_id: str):
    """Per-(provider, account) interprocess lockfile path, next to the store."""
    slug = _slug_account_id(f"{provider_id}:{account_id}")
    return paths.CONFIG.with_name(f"{paths.CONFIG.name}.{slug}{_LOCK_SUFFIX}")


def account_lock(provider_id: str, account_id: str):
    """Interprocess exclusive lock scoped to one (provider_id, account_id).

    Serializes read-check-POST-persist token-rotation sequences (e.g. xAI's
    single-use rotating refresh_token) across processes — an in-process
    ``threading.Lock`` only protects threads inside one process, so two
    processes refreshing the same account can otherwise race: the loser
    submits an already-rotated-out token and gets falsely treated as
    revoked. Callers MUST re-read the credential from disk after acquiring
    this lock in case a peer process already rotated it while waiting.
    Best-effort no-op (never blocks) on platforms without ``fcntl``.
    """
    lock_p = _account_lock_path(provider_id, account_id)

    @contextlib.contextmanager
    def _cm():
        paths.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_p, "a+", encoding="utf-8") as lf:
            _secure_perms(lock_p)
            fl = _file_lock(lf)
            if fl is None:
                yield
            else:
                with fl:
                    yield

    return _cm()


def _load_all() -> dict:
    return _load_cfg().get(_AUTH_KEY, {}) or {}


def _commit_provider_entry(provider_id: str, entry: dict, cfg: dict) -> None:
    """Write one provider's multi-account entry into *cfg* (caller holds lock)."""
    auth = cfg.setdefault(_AUTH_KEY, {})
    if not isinstance(auth, dict):
        auth = {}
        cfg[_AUTH_KEY] = auth
    accounts = entry.get("accounts") or {}
    if not accounts:
        auth.pop(provider_id, None)
        return
    active = entry.get("active")
    if active is None or str(active) not in accounts:
        active = next(iter(accounts))
    auth[provider_id] = {
        "active": str(active),
        "accounts": {str(k): dict(v) for k, v in accounts.items()},
    }


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
    """Write a multi-account entry; drop the provider key when no accounts remain.

    Locked + disk re-read: only *provider_id* is replaced. Other providers keep
    whatever is already on disk (avoids clobbering parallel writes).
    """
    snapshot = {
        "active": entry.get("active"),
        "accounts": {str(k): dict(v) for k, v in (entry.get("accounts") or {}).items()},
    }

    def _do() -> None:
        cfg = _load_cfg()
        _commit_provider_entry(provider_id, snapshot, cfg)
        _write_cfg(cfg)

    _with_cfg_lock(_do)


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


def _resolve_save_account_id(
    provider_id: str,
    cred: dict,
    account_id: str | None,
    entry: dict,
) -> str:
    if account_id is not None and str(account_id).strip():
        return str(account_id).strip()
    raw = cred.get("account_id") if isinstance(cred, dict) else None
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    if entry["accounts"] and entry.get("active") in entry["accounts"]:
        # Refresh path: providers often omit identity; update the active slot
        # instead of spawning a new "default" account.
        return entry["active"]
    return DEFAULT_ACCOUNT_ID


def _merge_saved_cred(prev: dict | None, cred: dict, *, replace_tokens: bool) -> dict:
    """Merge *cred* onto *prev*.

    Token writes (replace_tokens=True) install access/refresh/expiry from *cred*.
    Identity / usage patches (replace_tokens=False) NEVER touch token fields — that
    is the xAI invalid_grant footgun when Settings usage polls race a refresh.
    """
    base = dict(prev or {})
    incoming = dict(cred or {})
    token_keys = (
        "access_token",
        "refresh_token",
        "expires_at_ms",
        "id_token",
        "token_endpoint",
        "refresh_revoked",
        "auth_error",
    )
    if replace_tokens:
        base.update(incoming)
        # A successful token write clears a prior revoked marker unless caller set it.
        if incoming.get("access_token") or incoming.get("refresh_token"):
            if "refresh_revoked" not in incoming:
                base.pop("refresh_revoked", None)
            if "auth_error" not in incoming:
                base.pop("auth_error", None)
        return base
    for k, v in incoming.items():
        if k in token_keys:
            continue
        base[k] = v
    return base


def save_credential(
    provider_id: str,
    cred: dict,
    account_id: str | None = None,
    *,
    set_active: bool = True,
    replace_tokens: bool | None = None,
) -> str:
    """Save *cred* under (provider_id, account_id). Returns the account_id used.

    *account_id* defaults to ``cred['account_id']`` then ``DEFAULT_ACCOUNT_ID``.
    When *set_active* is True (default), that account becomes the provider's active one.
    Transparent migration: a legacy flat entry is rewritten as multi-account on save.

    Under lock, only this one account slot is patched against a fresh disk read —
    parallel writers cannot clobber other accounts' rotated refresh tokens.

    *replace_tokens*:
      - True  → install token fields from *cred* (login / refresh path)
      - False → identity/usage patch; token fields on disk are preserved
      - None  → True when *cred* carries access_token or refresh_token, else False
    """
    if replace_tokens is None:
        replace_tokens = bool(
            isinstance(cred, dict)
            and (cred.get("access_token") or cred.get("refresh_token")
                 or cred.get("expires_at_ms") is not None)
        )
    # Heuristic: callers that pass set_active=False with a full loaded cred (usage
    # persist_identity) must NOT rewind tokens. Force non-token merge unless the
    # payload looks like a dedicated token rotation (explicit replace_tokens=True).
    if replace_tokens and not set_active:
        # Still allow token writes with set_active=False when explicitly rotating
        # off the active pointer — but identity-only payloads often include stale
        # token copies from load_credential. Detect pure identity intent:
        # if caller didn't change tokens vs disk we keep disk tokens via merge
        # keyed on replace_tokens flag from caller. Default path below re-reads.
        pass

    result: dict[str, str] = {}

    def _do() -> None:
        cfg = _load_cfg()
        auth = cfg.get(_AUTH_KEY) or {}
        entry = _normalize_entry(auth.get(provider_id) if isinstance(auth, dict) else None)
        aid = _resolve_save_account_id(provider_id, cred, account_id, entry)
        prev = entry["accounts"].get(aid)
        # Auto-demote to identity merge when set_active=False and incoming tokens
        # are older/equal than disk — protects persist_identity races.
        do_tokens = bool(replace_tokens)
        if do_tokens and not set_active and prev:
            try:
                prev_exp = float(prev.get("expires_at_ms") or 0)
                new_exp = float((cred or {}).get("expires_at_ms") or 0)
            except (TypeError, ValueError):
                prev_exp, new_exp = 0.0, 0.0
            prev_rt = prev.get("refresh_token") or ""
            new_rt = (cred or {}).get("refresh_token") or ""
            prev_at = prev.get("access_token") or ""
            new_at = (cred or {}).get("access_token") or ""
            # Stale snapshot of the same slot (common usage-poll path): keep disk tokens.
            if new_exp <= prev_exp and (new_rt == prev_rt or not new_rt) and (
                new_at == prev_at or not new_at or new_exp < prev_exp
            ):
                do_tokens = False
        stored = _merge_saved_cred(prev, dict(cred or {}), replace_tokens=do_tokens)
        if not stored.get("account_id") and aid != DEFAULT_ACCOUNT_ID:
            stored["account_id"] = aid
        entry["accounts"][aid] = stored
        if set_active:
            entry["active"] = aid
        elif entry.get("active") not in entry["accounts"]:
            entry["active"] = aid
        _commit_provider_entry(provider_id, entry, cfg)
        _write_cfg(cfg)
        result["aid"] = aid

    _with_cfg_lock(_do)
    return result["aid"]


def patch_credential(
    provider_id: str,
    account_id: str | None,
    fields: dict,
) -> bool:
    """Patch non-token (or explicit) fields on one account under lock. False if missing."""
    if not isinstance(fields, dict) or not fields:
        return False
    ok = {"v": False}

    def _do() -> None:
        cfg = _load_cfg()
        auth = cfg.get(_AUTH_KEY) or {}
        entry = _normalize_entry(auth.get(provider_id) if isinstance(auth, dict) else None)
        if not entry["accounts"]:
            return
        if account_id is None:
            aid = entry.get("active") or next(iter(entry["accounts"]))
        else:
            aid = str(account_id)
        if aid not in entry["accounts"]:
            return
        prev = dict(entry["accounts"][aid])
        # Default: identity patch. Token keys only applied when explicitly present
        # AND caller is marking revocation / endpoint metadata.
        token_allow = {
            "refresh_revoked",
            "auth_error",
            "token_endpoint",
            "refresh_token",
            "access_token",
            "expires_at_ms",
            "id_token",
        }
        for k, v in fields.items():
            if k in ("access_token", "refresh_token", "expires_at_ms", "id_token"):
                # Explicit token surgery (e.g. clear RT on invalid_grant).
                if v is None:
                    prev.pop(k, None)
                else:
                    prev[k] = v
            elif k in token_allow or k not in (
                "access_token", "refresh_token", "expires_at_ms", "id_token"
            ):
                if v is None:
                    prev.pop(k, None)
                else:
                    prev[k] = v
        entry["accounts"][aid] = prev
        _commit_provider_entry(provider_id, entry, cfg)
        _write_cfg(cfg)
        ok["v"] = True

    _with_cfg_lock(_do)
    return bool(ok["v"])


def clear_credential(provider_id: str, account_id: str | None = None) -> None:
    """Remove one account (active when *account_id* is None). Drops the provider when empty."""

    def _do() -> None:
        cfg = _load_cfg()
        auth = cfg.get(_AUTH_KEY) or {}
        if not isinstance(auth, dict) or provider_id not in auth:
            return
        entry = _normalize_entry(auth.get(provider_id))
        accounts = entry["accounts"]
        if not accounts:
            _commit_provider_entry(
                provider_id, {"active": DEFAULT_ACCOUNT_ID, "accounts": {}}, cfg
            )
            _write_cfg(cfg)
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
        _commit_provider_entry(provider_id, entry, cfg)
        _write_cfg(cfg)

    _with_cfg_lock(_do)


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
    ok = {"v": False}

    def _do() -> None:
        cfg = _load_cfg()
        auth = cfg.get(_AUTH_KEY) or {}
        entry = _normalize_entry(auth.get(provider_id) if isinstance(auth, dict) else None)
        aid = str(account_id)
        if aid not in entry["accounts"]:
            return
        entry["active"] = aid
        _commit_provider_entry(provider_id, entry, cfg)
        _write_cfg(cfg)
        ok["v"] = True

    _with_cfg_lock(_do)
    return bool(ok["v"])


def migrate_legacy_provider(provider_id: str) -> bool:
    """Persist multi-account shape for a legacy flat entry. Returns True if rewritten."""
    ok = {"v": False}

    def _do() -> None:
        cfg = _load_cfg()
        auth = cfg.get(_AUTH_KEY) or {}
        raw = auth.get(provider_id) if isinstance(auth, dict) else None
        if raw is None or _is_multi_entry(raw):
            return
        if not isinstance(raw, dict):
            return
        if "access_token" not in raw and "refresh_token" not in raw:
            return
        entry = _normalize_entry(raw)
        _commit_provider_entry(provider_id, entry, cfg)
        _write_cfg(cfg)
        ok["v"] = True

    _with_cfg_lock(_do)
    return bool(ok["v"])

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
