"""Self-serve authentication: password credentials, server-side sessions, and
the single-use email tokens behind verification and password reset.

``accounts.py`` answers *may this identity use the service* (pending / active /
denied / suspended). Until this module existed nothing in the gateway answered
*is this identity who it claims to be* — every branch in ``auth.py`` deferred
that to an edge IdP. This is the credential store that answers it here.

Three properties are load-bearing and every change to this file has to keep
them:

* **No account-existence oracle.** ``/api/auth/login`` returns one byte-identical
  401 for "no such email" and "wrong password", and burns a real argon2
  verification against a dummy hash on the unknown-email path so the two answers
  also cost the same wall-clock time. ``/api/auth/register`` hashes once and returns
  the same 202 for fresh and taken addresses, notifying the address owner by mail.
* **Sessions are rows, not signed cookies.** Suspending an account has to end its
  live sessions on the very next request; a self-contained cookie cannot do that
  without a revocation table, at which point it is just a worse version of this.
* **Tokens are stored hashed.** ``email_tokens`` holds only SHA-256 digests;
  queued bearer URLs are authenticated ciphertext under an external mail key.
  Verification GETs are inert: inbox ownership requires an explicit confirm POST,
  and neither step establishes or changes the browser's login.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import secrets
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any
from urllib.parse import quote

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

try:
    from . import accounts, mailer
    from .config import settings
    from .db import tx
    from .limits import login_by_email, login_by_ip, request_network
    from .mailer import send as send_mail
except ImportError:  # uvicorn app:app from the package dir
    import accounts  # type: ignore
    import mailer  # type: ignore
    from config import settings  # type: ignore
    from db import tx  # type: ignore
    from limits import login_by_email, login_by_ip, request_network  # type: ignore
    from mailer import send as send_mail  # type: ignore

log = logging.getLogger("chudware-web.authn")

router = APIRouter(tags=["auth"])

SESSION_COOKIE = "chudware_sid"

# Purposes are a closed set; a token minted for one flow must never be
# redeemable in another (a verification link is not a password reset).
PURPOSE_VERIFY = "verify_email"
PURPOSE_RESET = "password_reset"
PURPOSE_OAUTH_STATE = "oauth_state"

VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
OAUTH_STATE_TTL = timedelta(minutes=10)

# Sliding expiry: refresh last_seen_at at most this often. Every authenticated
# request would otherwise be a row rewrite on the session table.
_TOUCH_INTERVAL = timedelta(seconds=60)

MIN_PASSWORD_CHARS = 12
# argon2 hashes its input in one pass, so a huge password is not itself a cost
# amplifier — but there is no reason to accept a megabyte either.
MAX_PASSWORD_CHARS = 1024

# OWASP's argon2id guidance, and the parameters the plan fixes: 64 MiB, three
# passes, four lanes. hashlib is NOT a password KDF and is used nowhere here.
_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

# Verified against on the unknown-email path so that "no such account" and
# "wrong password" cost the same. Hashing a random secret at import means the
# value is not a known-plaintext target either.
_DUMMY_HASH = _HASHER.hash(secrets.token_urlsafe(32))

# Two 64 MiB Argon2 jobs at most, shared by registration, login and recovery.
# Admission never waits and counts queued + running work, not awaiting requests.
# Do not use asyncio.to_thread: its shared executor has an unbounded work queue.
_PASSWORD_WORKERS = 2
_PASSWORD_POOL = ThreadPoolExecutor(
    max_workers=_PASSWORD_WORKERS, thread_name_prefix="authn-password"
)
_PASSWORD_SLOTS = BoundedSemaphore(_PASSWORD_WORKERS)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")

# One body, one status, for every login failure. Built once so that no code path
# can accidentally introduce a distinguishing whitespace or key order.
_LOGIN_FAILED = "invalid email or password"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def normalize_email(raw: Any) -> str:
    return str(raw or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def token_hash(token: str) -> str:
    """SHA-256 hex of a token. This — never the token — is what we persist.

    SHA-256 is correct here and argon2 would be wrong: these tokens are 256 bits
    of ``secrets`` output, so there is no dictionary to slow down, only a
    database-dump exposure to close.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_LOOPBACK = {"127.0.0.1", "::1"}


def client_ip(request: Request) -> str:
    """Best-known exact client address for session and audit metadata.

    In production cloudflared terminates the tunnel on loopback, so the socket
    peer is always 127.0.0.1. Honour ``CF-Connecting-IP`` only from that hop and
    only when it contains a valid address; direct callers cannot forge session
    metadata. Abuse-control keys use limits.request_network(), not this helper.
    """
    peer = (request.client.host if request.client else "") or "unknown"
    if peer in _LOOPBACK:
        forwarded = (request.headers.get("cf-connecting-ip") or "").strip()
        validated = _inet_or_none(forwarded)
        if validated is not None:
            return validated
    return _inet_or_none(peer) or "unknown"


def _inet_or_none(raw: str) -> str | None:
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def _check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at least {MIN_PASSWORD_CHARS} characters",
        )
    if len(password) > MAX_PASSWORD_CHARS:
        raise HTTPException(status_code=400, detail="password is too long")


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def burn_dummy_verification(password: str) -> None:
    """Spend one argon2 verification with no account behind it.

    Called on the unknown-email login path so the response time of "that email
    has no account" matches "that password is wrong".
    """
    try:
        _HASHER.verify(_DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass


async def _password_work(function: Callable[..., Any], *args: Any) -> Any:
    """Admit bounded KDF work without blocking the event loop or queuing callers.

    Release on the concurrent future's completion, never in the awaiter's
    finally: cancellation cannot stop a running Argon2 job. Pending work may be
    cancelled, but a running job retains its slot until it actually finishes.
    Workers receive only hash/password values, never a db.tx() context or cursor.
    """
    loop = asyncio.get_running_loop()
    slots = _PASSWORD_SLOTS
    if not slots.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="password authentication is busy — try again shortly",
            headers={"Retry-After": "1"},
        )
    try:
        future = _PASSWORD_POOL.submit(function, *args)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _done: slots.release())
    return await asyncio.wrap_future(future, loop=loop)


def _verify_login_password(stored_hash: str | None, password: str) -> tuple[bool, str | None]:
    """One worker admission covers comparison and any parameter upgrade."""
    if stored_hash is None:
        burn_dummy_verification(password)
        return False, None
    if not verify_password(stored_hash, password):
        return False, None
    digest = hash_password(password) if _HASHER.check_needs_rehash(stored_hash) else None
    return True, digest


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _lock_account(email: str) -> None:
    """Serialize authentication transitions, including absent credentials.

    Lock the account before credentials, tokens, or sessions. A credential row
    alone cannot protect OAuth-only accounts, and redeeming a token before this
    lock would race OAuth invalidation. Call inside the transition's outer tx().
    """
    with tx() as cur:
        cur.execute("SELECT email FROM accounts WHERE email = %s FOR UPDATE", (email,))
        cur.fetchone()


def get_credential(email: str) -> dict[str, Any] | None:
    with tx() as cur:
        cur.execute(
            "SELECT email, password_hash, email_verified, verified_at"
            "  FROM credentials WHERE email = %s",
            (email,),
        )
        return cur.fetchone()


def set_password(email: str, password: str, *, verified: bool) -> None:
    """Create or replace the credential for ``email``.

    ``verified`` is True only where the caller has just proven inbox control
    (a redeemed reset token); registration always writes False and waits for the
    verification link.
    """
    _store_password(email, hash_password(password), verified=verified)


def _store_password(email: str, digest: str, *, verified: bool) -> None:
    """Persist a precomputed hash without repeating the expensive work."""
    with tx() as cur:
        _lock_account(email)
        cur.execute(
            """
            INSERT INTO credentials (email, password_hash, email_verified,
                                     verified_at, updated_at)
                 VALUES (%s, %s, %s, CASE WHEN %s THEN now() END, now())
            ON CONFLICT (email) DO UPDATE
                    SET password_hash  = EXCLUDED.password_hash,
                        email_verified = credentials.email_verified OR EXCLUDED.email_verified,
                        verified_at    = COALESCE(credentials.verified_at, EXCLUDED.verified_at),
                        updated_at     = now()
            """,
            (email, digest, verified, verified),
        )


def mark_verified(email: str) -> None:
    with tx() as cur:
        _lock_account(email)
        cur.execute(
            "UPDATE credentials"
            "   SET email_verified = TRUE,"
            "       verified_at = COALESCE(verified_at, now()),"
            "       updated_at = now()"
            " WHERE email = %s",
            (email,),
        )


# ---------------------------------------------------------------------------
# Trial activation
#
# accounts.set_plan(email, plan) is frozen at two arguments, so the trial window
# is written here — the one place that owns the trial_ends_at column added by
# 0003_auth.sql. The WHERE clause is the safety property: only a *pending*
# account is promoted, so redeeming a stale verification link can never
# resurrect an account an admin denied or suspended.
# ---------------------------------------------------------------------------


def start_trial(email: str) -> None:
    now = time.time()
    ends = now + max(1, int(settings.trial_days)) * 86_400.0
    with tx() as cur:
        cur.execute(
            """
            UPDATE accounts
               SET status = 'active',
                   plan = 'trial',
                   trial_ends_at = %s,
                   decided_at = %s,
                   decided_by = 'self-serve'
             WHERE email = %s AND status = 'pending'
            """,
            (ends, now, email),
        )


def trial_ends_at(email: str) -> float | None:
    """Epoch seconds, matching every other timestamp on the accounts row."""
    with tx() as cur:
        cur.execute("SELECT trial_ends_at FROM accounts WHERE email = %s", (email,))
        row = cur.fetchone()
    return row["trial_ends_at"] if row else None


def trial_expired(email: str) -> bool:
    ends = trial_ends_at(email)
    return ends is not None and ends <= time.time()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(
    email: str, *, user_agent: str = "", ip: str = "", ttl: timedelta | None = None
) -> str:
    """Mint a bearer session id, persist only its SHA-256 digest, and return the raw id.

    The raw 256-bit value exists only in the cookie. A database dump therefore
    cannot authenticate as a user, while deterministic SHA-256 still permits
    indexed lookup and revocation without a schema change.
    """
    sid = secrets.token_urlsafe(32)
    expires = _now() + (ttl or timedelta(days=max(1, int(settings.session_ttl_days))))
    with tx() as cur:
        cur.execute("SELECT status FROM accounts WHERE email = %s FOR UPDATE", (email,))
        account = cur.fetchone()
        if account is None or account["status"] in ("denied", "suspended"):
            raise HTTPException(status_code=403, detail="account is not permitted to sign in")
        cur.execute(
            "INSERT INTO sessions (sid, email, expires_at, user_agent, ip)"
            " VALUES (%s, %s, %s, %s, %s)",
            (token_hash(sid), email, expires, user_agent[:512], _inet_or_none(ip)),
        )
    return sid


def lookup_session(sid: str) -> str | None:
    """Email behind a live session, or None. Slides ``last_seen_at``.

    Called from the auth middleware on every request, so the touch is throttled
    to once a minute per session: without that, serving a page of static assets
    would rewrite the same row a dozen times.
    """
    if not sid:
        return None
    with tx() as cur:
        cur.execute(
            "SELECT email, last_seen_at FROM sessions"
            " WHERE sid = %s AND revoked_at IS NULL AND expires_at > now()",
            (token_hash(sid),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        if row["last_seen_at"] <= _now() - _TOUCH_INTERVAL:
            cur.execute(
                "UPDATE sessions SET last_seen_at = now() WHERE sid = %s",
                (token_hash(sid),),
            )
    return row["email"]


def revoke_session(sid: str) -> None:
    if not sid:
        return
    with tx() as cur:
        cur.execute(
            "UPDATE sessions SET revoked_at = now()"
            " WHERE sid = %s AND revoked_at IS NULL",
            (token_hash(sid),),
        )


def revoke_sessions(email: str) -> int:
    """Kill every live session for ``email``. Returns how many were killed."""
    with tx() as cur:
        cur.execute(
            "UPDATE sessions SET revoked_at = now()"
            " WHERE email = %s AND revoked_at IS NULL",
            (email,),
        )
        return cur.rowcount


def delete_sessions(email: str) -> int:
    """Permanently remove every session for an account."""
    with tx() as cur:
        cur.execute("DELETE FROM sessions WHERE email = %s", (email,))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Email tokens
# ---------------------------------------------------------------------------


def store_token(token: str, email: str, purpose: str, ttl: timedelta) -> None:
    """Record the SHA-256 of an ALREADY-CHOSEN token.

    Storing invalidates any outstanding token of the same purpose for the same
    address, so resending verification cannot leave multiple live links behind.
    Account-free OAuth state and its network buckets are managed in oauth.py.
    """
    with tx() as cur:
        if email:
            _lock_account(email)
            cur.execute(
                "UPDATE email_tokens SET used_at = now()"
                " WHERE email = %s AND purpose = %s AND used_at IS NULL",
                (email, purpose),
            )
        cur.execute(
            "INSERT INTO email_tokens (token_hash, email, purpose, expires_at)"
            " VALUES (%s, %s, %s, %s)",
            (token_hash(token), email, purpose, _now() + ttl),
        )


def mint_token(email: str, purpose: str, ttl: timedelta) -> str:
    """Issue a token for ``purpose``; plaintext is returned, never stored here."""
    token = secrets.token_urlsafe(32)
    store_token(token, email, purpose, ttl)
    return token


def _live_token_email(token: str, purpose: str) -> str | None:
    """Inspect a capability without consuming it or holding a lock for a browser."""
    if not token:
        return None
    with tx() as cur:
        cur.execute(
            "SELECT email FROM email_tokens"
            " WHERE token_hash = %s AND purpose = %s"
            " AND used_at IS NULL AND expires_at > now()",
            (token_hash(token), purpose),
        )
        row = cur.fetchone()
    return row["email"] if row else None


def consume_token(token: str, purpose: str) -> str | None:
    """Redeem a token exactly once. Returns the email it was minted for.

    The single-use guarantee is the UPDATE's own ``used_at IS NULL`` predicate,
    so two concurrent redemptions cannot both win.
    """
    if not token:
        return None
    with tx() as cur:
        # Read without a token-row lock, then take locks in account-first order.
        # The UPDATE below rechecks validity after a concurrent OAuth cutover.
        cur.execute(
            "SELECT email FROM email_tokens WHERE token_hash = %s AND purpose = %s",
            (token_hash(token), purpose),
        )
        candidate = cur.fetchone()
        if candidate is None:
            return None
        if candidate["email"]:
            _lock_account(candidate["email"])
        cur.execute(
            """
            UPDATE email_tokens SET used_at = now()
             WHERE token_hash = %s
               AND purpose = %s
               AND used_at IS NULL
               AND expires_at > now()
         RETURNING email
            """,
            (token_hash(token), purpose),
        )
        row = cur.fetchone()
    return row["email"] if row else None


# ---------------------------------------------------------------------------
# OAuth identity links
# ---------------------------------------------------------------------------


def oauth_email(provider: str, subject: str) -> str | None:
    with tx() as cur:
        cur.execute(
            "SELECT email FROM oauth_identities WHERE provider = %s AND subject = %s",
            (provider, subject),
        )
        row = cur.fetchone()
    return row["email"] if row else None


def link_oauth_identity(provider: str, subject: str, email: str) -> None:
    """Bind a stable provider subject once; provider email changes never move it."""
    with tx() as cur:
        cur.execute(
            """
            INSERT INTO oauth_identities (provider, subject, email)
                 VALUES (%s, %s, %s)
            ON CONFLICT (provider, subject) DO NOTHING
            """,
            (provider, subject, email),
        )


def oauth_providers_for(email: str) -> list[str]:
    with tx() as cur:
        cur.execute(
            "SELECT provider FROM oauth_identities WHERE email = %s ORDER BY provider",
            (email,),
        )
        return [row["provider"] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------


def _cookie_secure(request: Request) -> bool:
    """Secure everywhere except plain-http localhost, where a Secure cookie
    would simply never be stored and local development would break."""
    host = (request.headers.get("host") or "").split(":", 1)[0].strip("[]").lower()
    local_http = request.url.scheme == "http" and host in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    return not local_http


def _cookie(name: str, value: str, *, max_age: int, secure: bool) -> str:
    # SameSite=Lax, not Strict: the OAuth callback arrives as a cross-site
    # top-level navigation and Strict would drop the cookie exactly there. Lax
    # still blocks the cross-site POST that CSRF needs.
    attrs = f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
    return attrs + "; Secure" if secure else attrs


def attach_session_cookie(response: Response, request: Request, sid: str) -> None:
    max_age = max(1, int(settings.session_ttl_days)) * 24 * 3600
    response.headers.append(
        "set-cookie", _cookie(SESSION_COOKIE, sid, max_age=max_age, secure=_cookie_secure(request))
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.headers.append(
        "set-cookie", _cookie(SESSION_COOKIE, "", max_age=0, secure=_cookie_secure(request))
    )


# ---------------------------------------------------------------------------
# User-facing pages
# ---------------------------------------------------------------------------


# Brand tokens for the server-rendered unauthenticated pages (pending, denied,
# suspended, link-expired, OAuth failure). Same values as the portal stylesheet
# and the marketing site's global.css; the fonts are the two public
# /brand-assets files (content-hashed — rehash_static.py rewrites these
# references too, so never retype a digest by hand).
#
# A plain constant, not part of the f-string, so the CSS needs no doubled
# braces. app.py's global CSP carries style-src 'unsafe-inline', so an inline
# <style> on these responses is permitted (portal_serve.py's stricter CSP
# applies only to the static portal assets, not to these routes).
_PAGE_STYLE = """
    @font-face { font-family: "Chudware Sans";
      src: url("/brand-assets/Rubik-VF.247dbaa0aa4a.woff2") format("woff2-variations");
      font-style: normal; font-weight: 300 900; font-display: swap; }
    @font-face { font-family: "Chudware Mono";
      src: url("/brand-assets/JetBrainsMono-Regular.8f0f0cc0353e.woff2") format("woff2");
      font-style: normal; font-weight: 400; font-display: swap; }
    :root { color-scheme: dark;
      --bg: #090d12; --panel: #111821; --panel-2: #151e29; --edge: #273342;
      --edge-soft: rgba(255,255,255,.075); --text: #eef4f8; --muted: #8d9aaa;
      --teal: #42ecce; --teal-wash: rgba(66,236,206,.09);
      --ring: 0 0 0 3px rgba(66,236,206,.18); --shadow: 0 22px 60px rgba(0,0,0,.3);
      --sans: "Chudware Sans", "Arial Narrow", sans-serif;
      --mono: "Chudware Mono", ui-monospace, Consolas, monospace;
      --step-0: clamp(1rem, .96rem + .18vw, 1.1rem);
      --step-1: clamp(1.2rem, 1.1rem + .45vw, 1.45rem);
      --s2: .5rem; --s4: 1rem; --s5: 1.5rem; --s6: 2rem;
      --radius: .625rem; --radius-lg: 1rem; }
    body { font-family: var(--sans); color: var(--text);
           background: radial-gradient(900px 540px at 82% -5%, rgba(66,236,206,.08), transparent 62%), var(--bg);
           display: flex; flex-direction: column; min-height: 100vh; min-height: 100dvh;
           align-items: center; justify-content: center; gap: var(--s5);
           margin: 0; padding: max(16px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right))
                    max(16px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
           box-sizing: border-box; -webkit-font-smoothing: antialiased; }
    .brand { display: flex; align-items: center; gap: 12px; color: var(--text);
             font-weight: 720; font-size: .98rem; text-transform: uppercase; letter-spacing: .08em; }
    .brand svg { width: 30px; height: 30px; color: var(--teal);
                 filter: drop-shadow(0 0 10px rgba(66,236,206,.25)); }
    .card { max-width: 32rem; width: 100%; padding: var(--s6); box-sizing: border-box;
            border: 1px solid var(--edge-soft); border-radius: var(--radius-lg);
            background: linear-gradient(145deg, rgba(22,31,42,.96), rgba(13,19,27,.96));
            box-shadow: var(--shadow); }
    h1 { margin: 0 0 var(--s2); font-size: var(--step-1); letter-spacing: -.01em; font-weight: 650; }
    p { margin: var(--s2) 0 0; line-height: 1.55; font-size: var(--step-0); color: var(--muted); }
    code { font-family: var(--mono); font-size: .9em; color: var(--text);
           background: var(--panel-2); border: 1px solid var(--edge);
           padding: .1rem .35rem; border-radius: .25rem; word-break: break-all; }
    a { color: var(--teal); text-decoration: underline; text-underline-offset: 3px; }
    button { display: inline-flex; align-items: center; justify-content: center;
             min-height: 42px; margin-top: var(--s2); padding: 10px 16px;
             border: 1px solid var(--teal); border-radius: var(--radius);
             background: var(--teal); color: #06110f; font: 650 .81rem/1 var(--sans);
             cursor: pointer; transition: .14s ease;
             box-shadow: 0 7px 22px rgba(66,236,206,.15); }
    button:hover:not(:disabled) { background: #68f5da; }
    button:disabled { cursor: wait; opacity: .45; }
    :is(a, button):focus-visible { outline: none; box-shadow: var(--ring); }
    @media (prefers-reduced-motion: reduce) { button { transition: none; } }
    @media (max-width: 480px) {
      .card { padding: var(--s4); }
    }
"""

# The teal tesseract mark — same geometry as the portal sidebar and favicon.
_BRAND_MARK = (
    '<svg viewBox="0 0 64 64" aria-hidden="true"><g fill="none" stroke="currentColor" '
    'stroke-width="3.2"><rect x="9" y="9" width="46" height="46"/>'
    '<rect x="21" y="21" width="22" height="22"/>'
    '<path d="M9 9l12 12M55 9L43 21M55 55L43 43M9 55l12-12"/></g></svg>'
)


def page_html(title: str, heading: str, body_html: str) -> str:
    """The one HTML shell every unauthenticated gateway page uses.

    Inline <style>, but never inline <script>. Interactive callers reference a
    content-hashed file through a public asset route that the CSP permits.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title} — Chudware</title>
  <style>{_PAGE_STYLE}</style>
</head>
<body>
  <div class="brand">{_BRAND_MARK}<span>Chudware</span></div>
  <div class="card">
    <h1>{heading}</h1>
{body_html}
  </div>
</body>
</html>
"""


def _token_page(kind: str) -> HTMLResponse:
    return HTMLResponse(
        page_html(
            "Link expired",
            "That link is no longer valid",
            f"    <p>This {kind} link has already been used, or it expired."
            f" Links are single-use.</p>\n"
            '    <p><a href="/">Back to Chudware</a> and request a new one.</p>',
        ),
        status_code=400,
    )


# ---------------------------------------------------------------------------
# Shared flow pieces
# ---------------------------------------------------------------------------

# Both bodies are constants so no caller can drift them apart.
_ACCEPTED = {"status": "accepted"}


def _base_url() -> str:
    return settings.public_base_url.rstrip("/")


def _verify_url(token: str) -> str:
    return f"{_base_url()}/api/auth/verify?token={token}"


def _reset_url(token: str) -> str:
    return f"{_base_url()}/reset?token={token}"


def _login_url() -> str:
    return f"{_base_url()}/signin"


def begin_session_response(
    request: Request, email: str, payload: dict[str, Any] | None = None
) -> JSONResponse:
    sid = create_session(
        email,
        user_agent=request.headers.get("user-agent", ""),
        ip=client_ip(request),
    )
    account = accounts.get_account(email)
    body: dict[str, Any] = {
        "ok": True,
        "email": email,
        "status": account.status if account else "pending",
        "plan": accounts.effective_plan(account),
    }
    if payload:
        body.update(payload)
    response = JSONResponse(body)
    attach_session_cookie(response, request, sid)
    return response


def complete_verification(email: str) -> None:
    """Verify inbox ownership without creating or replacing a browser session."""
    with tx():
        _lock_account(email)
        mark_verified(email)
        start_trial(email)


def _invalidate_password_capabilities(email: str) -> None:
    """Invalidate verification and recovery links while holding the account lock."""
    with tx() as cur:
        cur.execute(
            "UPDATE email_tokens SET used_at = now()"
            " WHERE email = %s AND purpose IN (%s, %s) AND used_at IS NULL",
            (email, PURPOSE_VERIFY, PURPOSE_RESET),
        )


def complete_oauth(request: Request, email: str) -> str:
    """Finish provider ownership proof without blessing an unverified password.

    Only a password verified independently of this OAuth flow survives. Remove
    preregistration credentials and their capabilities before issuing the OAuth
    session, in the same transaction as account linking and trial activation.
    """
    with tx() as cur:
        _lock_account(email)
        cur.execute(
            "DELETE FROM credentials WHERE email = %s AND NOT email_verified",
            (email,),
        )
        if cur.rowcount:
            revoke_sessions(email)
        _invalidate_password_capabilities(email)
        start_trial(email)
        return create_session(
            email,
            user_agent=request.headers.get("user-agent", ""),
            ip=client_ip(request),
        )


def _issue_token_mail(email: str, purpose: str, ttl: timedelta) -> None:
    token = secrets.token_urlsafe(32)
    expires_at = _now() + ttl
    url_key = "verify_url" if purpose == PURPOSE_VERIFY else "reset_url"
    url = _verify_url(token) if purpose == PURPOSE_VERIFY else _reset_url(token)
    message = mailer.prepare(
        email, purpose,
        {"email": email, url_key: url,
         "expires_hours": str(max(1, int(ttl.total_seconds() // 3600)))},
        expires_at=expires_at,
    )
    if message is None:
        return
    # A savepoint preserves the previous deliverable token if enqueue fails,
    # even when registration/resend already owns the outer account transaction.
    with tx() as cur:
        try:
            with cur.connection.transaction():
                store_token(token, email, purpose, ttl)
                mailer.enqueue(message)
        except Exception:
            log.warning("authn: token mail enqueue failed purpose=%s", purpose)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/auth/register", status_code=202)
async def register(request: Request, body: dict[str, Any]) -> dict[str, str]:
    """Begin registration. Always 202 — see the module docstring.

    An address that is already registered gets the identical response and a
    "someone tried to register your address" mail. Returning 409 here would turn
    the endpoint into a free account-existence scanner.
    """
    login_by_ip.enforce(request_network(request), "authentication attempts")
    email = normalize_email(body.get("email"))
    password = str(body.get("password") or "")
    if not valid_email(email):
        raise HTTPException(status_code=400, detail="enter a valid email address")
    _check_password_policy(password)
    # Exactly one Argon2 hash for every valid, admitted registration outcome.
    digest = await _password_work(hash_password, password)

    with tx() as cur:
        # A row lock cannot serialize a first registration: the account does not
        # exist yet. Lock the normalized address BEFORE lookup/creation instead.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"authn.register:{email}",),
        )
        taken = accounts.get_account(email) is not None
        if not taken:
            # Other creators (OAuth/Access/admin) need not use our advisory lock.
            # Only the INSERT winner owns this new row; never attach a password
            # to the pending account returned by a losing ensure_account call.
            cur.execute(
                "INSERT INTO accounts"
                " (email, display, status, plan, role, created_at, decided_at, decided_by)"
                " VALUES (%s, %s, 'pending', 'trial', 'user', %s, NULL, '')"
                " ON CONFLICT (email) DO NOTHING RETURNING email",
                (email, email.split("@")[0], time.time()),
            )
            taken = cur.fetchone() is None
        if taken:
            send_mail(
                email,
                "register_exists",
                {"email": email, "login_url": _login_url(), "reset_url": f"{_base_url()}/#forgot"},
            )
            return _ACCEPTED

        _store_password(email, digest, verified=False)
        _issue_token_mail(email, PURPOSE_VERIFY, VERIFY_TTL)
    log.info("authn: registration started for %s", email)
    return _ACCEPTED


@router.post("/api/auth/login")
async def login(request: Request, body: dict[str, Any]) -> JSONResponse:
    """Verify a password and open a session.

    The network budget counts all requests; the email+network budget counts only
    wrong passwords. Both are checked before Argon2. Someone on another network
    cannot lock the owner out, and successful sign-ins do not spend a guess.
    """
    network = request_network(request)
    login_by_ip.enforce(network, "sign-in attempts from this network")
    email = normalize_email(body.get("email"))
    password = str(body.get("password") or "")
    failure_key = f"{network}|{email}"
    if not login_by_email.allowed(failure_key):
        raise HTTPException(
            status_code=429,
            detail="too many sign-in attempts for this account from this network — slow down",
            headers={"Retry-After": str(login_by_email.retry_after(failure_key))},
        )

    # db.tx() joins by ContextVar. Snapshot first, do CPU work with no transaction
    # open, then lock and recheck: reset/OAuth/admin may retire this credential
    # while Argon2 runs, and a stale password must never mint a session.
    credential = get_credential(email)
    stored_hash = credential["password_hash"] if credential else None
    valid, upgraded = await _password_work(_verify_login_password, stored_hash, password)
    if not valid:
        login_by_email.check(failure_key)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED)

    with tx() as cur:
        _lock_account(email)
        current = get_credential(email)
        if current is None or current["password_hash"] != stored_hash:
            login_by_email.check(failure_key)
            raise HTTPException(status_code=401, detail=_LOGIN_FAILED)
        login_by_email.clear(failure_key)
        if not current["email_verified"]:
            # Only reachable with the CORRECT password, so this is not an oracle:
            # the caller already knows the account exists.
            raise HTTPException(status_code=403, detail="email address not verified")
        if upgraded is not None:
            cur.execute(
                "UPDATE credentials SET password_hash = %s, updated_at = now()"
                " WHERE email = %s AND password_hash = %s",
                (upgraded, email, stored_hash),
            )

        log.info("authn: login for %s", email)
        return begin_session_response(request, email)


@router.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    revoke_session(request.cookies.get(SESSION_COOKIE, ""))
    response = JSONResponse({"ok": True})
    clear_session_cookie(response, request)
    return response


@router.get("/api/auth/verify", response_model=None)
async def verify(request: Request, token: str = "") -> Response:
    """Show an inert confirmation page, safe for email scanners and prefetchers.

    Mail security products routinely GET links without the recipient's consent.
    A GET must never consume the token, verify an attacker-chosen password, or
    activate the trial. Only the recipient's explicit form POST does that.
    """
    if _live_token_email(token, PURPOSE_VERIFY) is None:
        return _token_page("verification")
    action = f"/api/auth/verify?token={quote(token, safe='')}"
    return HTMLResponse(
        page_html(
            "Confirm email",
            "Confirm your email address",
            "    <p>Continue only if you requested this Chudware account."
            " This confirms its email address and password registration.</p>\n"
            f'    <form method="post" action="{action}">\n'
            '      <button type="submit">Confirm email address</button>\n'
            "    </form>",
        ),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/api/auth/verify", response_model=None)
async def confirm_verification(request: Request, token: str = "") -> Response:
    """Explicitly redeem inbox ownership without opening a browser session."""
    with tx():
        email = consume_token(token, PURPOSE_VERIFY)
        if email is None:
            return _token_page("verification")
        complete_verification(email)
    log.info("authn: verified %s", email)
    return RedirectResponse("/signin?verified=1", status_code=303)


@router.post("/api/auth/resend-verification", status_code=202)
async def resend_verification(request: Request, body: dict[str, Any]) -> dict[str, str]:
    """Re-send a verification link. Always 202, whatever the address is."""
    login_by_ip.enforce(request_network(request), "authentication attempts")
    email = normalize_email(body.get("email"))
    if not valid_email(email):
        return _ACCEPTED
    # Access/OAuth can create a pending account without creating password
    # credentials. The account state, not a credentials row, is the authority
    # for whether this address still needs a verification link.
    with tx():
        _lock_account(email)
        account = accounts.get_account(email)
        if account is not None and account.status == "pending":
            _issue_token_mail(email, PURPOSE_VERIFY, VERIFY_TTL)
    return _ACCEPTED


@router.post("/api/auth/forgot", status_code=202)
async def forgot(request: Request, body: dict[str, Any]) -> dict[str, str]:
    """Begin a password reset. Always 202, whatever the address is.

    An address that exists only in ``oauth_identities`` has no password to
    reset, and must not be convertible into one without re-proving ownership —
    so it gets a "you signed in with Google" mail instead of a reset link.
    """
    login_by_ip.enforce(request_network(request), "authentication attempts")
    email = normalize_email(body.get("email"))
    if not valid_email(email):
        return _ACCEPTED
    with tx():
        _lock_account(email)
        if get_credential(email) is not None:
            _issue_token_mail(email, PURPOSE_RESET, RESET_TTL)
            return _ACCEPTED
        providers = oauth_providers_for(email)
        if providers:
            send_mail(
                email,
                "oauth_only_reset",
                {"email": email, "provider": providers[0], "login_url": _login_url()},
            )
    return _ACCEPTED


@router.post("/api/auth/reset")
async def reset(request: Request, body: dict[str, Any]) -> JSONResponse:
    """Redeem a reset token and set a new password.

    Redeeming proves inbox control, so it also verifies the address and promotes
    a still-pending account — a user who never clicked the verification link is
    not stuck. Every other session for the account is revoked: a password change
    has to evict whoever prompted it.
    """
    login_by_ip.enforce(request_network(request), "authentication attempts")
    password = str(body.get("password") or "")
    _check_password_policy(password)
    token = str(body.get("token") or "")
    if _live_token_email(token, PURPOSE_RESET) is None:
        raise HTTPException(status_code=400, detail="that reset link is no longer valid")
    digest = await _password_work(hash_password, password)
    with tx():
        email = consume_token(token, PURPOSE_RESET)
        if email is None:
            raise HTTPException(status_code=400, detail="that reset link is no longer valid")
        _store_password(email, digest, verified=True)
        start_trial(email)
        revoke_sessions(email)
        log.info("authn: password reset for %s", email)
        return begin_session_response(request, email, {"reset": True})


__all__ = [
    "MIN_PASSWORD_CHARS",
    "OAUTH_STATE_TTL",
    "PURPOSE_OAUTH_STATE",
    "PURPOSE_RESET",
    "PURPOSE_VERIFY",
    "SESSION_COOKIE",
    "attach_session_cookie",
    "begin_session_response",
    "clear_session_cookie",
    "client_ip",
    "complete_oauth",
    "complete_verification",
    "consume_token",
    "create_session",
    "get_credential",
    "hash_password",
    "link_oauth_identity",
    "lookup_session",
    "mint_token",
    "normalize_email",
    "oauth_email",
    "oauth_providers_for",
    "page_html",
    "revoke_session",
    "revoke_sessions",
    "router",
    "set_password",
    "start_trial",
    "store_token",
    "token_hash",
    "trial_ends_at",
    "trial_expired",
    "valid_email",
    "verify_password",
]


# ---------------------------------------------------------------------------
# Ops CLI
# ---------------------------------------------------------------------------


def _cli(argv: list[str] | None = None) -> int:
    """``python -m authn ops-session --email you@example.com``
    ``python -m authn grant-admin --email you@example.com [--oauth-only]``
    ``python -m authn grant-subscription --email you@example.com``

    ``ops-session`` prints one short-lived session id on stdout. Deploy scripts
    that must call an authenticated loopback endpoint — install_webd.sh polls
    /healthz for the active engine-bridge count before it drops users — used to
    do that with the oauth2-proxy shared secret. That secret is gone, and
    inventing a second machine credential to replace it would just be the same
    trusted-string problem under a new name. A real session row is better in
    every way: it expires on its own, it is revocable, and it is the exact
    credential path the middleware already verifies.

    ``grant-admin`` is the operator bootstrap: it registers the address if
    needed, sets a password read from stdin (never argv, which is world-visible
    in /proc), marks it verified, activates it, and promotes it to ``admin``.
    With ``--oauth-only`` every password credential, verification/reset link,
    and existing session is removed or revoked, so an old password cannot become
    a second, weaker way into the admin account.
    Verification is granted by the operator's database access rather than by a
    mailed link, so this works on a deployment whose mail backend is still
    ``console`` — which is exactly the state a first admin is created in. It is
    idempotent, and it is the only writer of the role column.

    ``grant-subscription`` opens the desktop entitlement window for an account
    without going through Stripe, which is how an operator or an internal seat
    gets a working desktop licence.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="authn")
    sub = parser.add_subparsers(dest="command", required=True)
    mint = sub.add_parser("ops-session", help="mint a short-lived session id")
    mint.add_argument("--email", required=True)
    mint.add_argument("--ttl-seconds", type=int, default=900)
    grant = sub.add_parser("grant-admin", help="create/promote an admin account")
    grant.add_argument("--email", required=True)
    credential = grant.add_mutually_exclusive_group()
    credential.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )
    credential.add_argument(
        "--oauth-only",
        action="store_true",
        help="remove password login and old sessions; sign in through an OAuth provider",
    )
    grant_sub = sub.add_parser(
        "grant-subscription",
        help="give an account an effectively unlimited subscription",
    )
    grant_sub.add_argument("--email", required=True)
    grant_sub.add_argument("--plan", default="enterprise")
    grant_sub.add_argument(
        "--years",
        type=int,
        default=100,
        help="entitlement horizon in years (default 100)",
    )
    args = parser.parse_args(argv)

    email = normalize_email(args.email)
    if not valid_email(email):
        print(f"not a usable email address: {email!r}", file=sys.stderr)
        return 1

    if args.command == "grant-admin":
        return _cli_grant_admin(
            email, password_stdin=args.password_stdin, oauth_only=args.oauth_only
        )

    if args.command == "grant-subscription":
        return _cli_grant_subscription(email, plan=args.plan, years=args.years)

    try:
        with tx():
            _lock_account(email)
            account = accounts.get_account(email)
            if account is None or not accounts.is_authorized(account):
                print(f"no active account for {email!r}", file=sys.stderr)
                return 1
            ttl = timedelta(seconds=max(1, args.ttl_seconds))
            sid = create_session(email, user_agent="chudware-ops-cli", ttl=ttl)
    except HTTPException as exc:
        print(str(exc.detail), file=sys.stderr)
        return 1
    print(sid)
    return 0


def _cli_grant_admin(email: str, *, password_stdin: bool, oauth_only: bool) -> int:
    import getpass

    password = ""
    if not oauth_only:
        if password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass(f"password for {email}: ")
            if password != getpass.getpass("confirm: "):
                print("passwords did not match", file=sys.stderr)
                return 1
        if len(password) < MIN_PASSWORD_CHARS:
            print(
                f"password must be at least {MIN_PASSWORD_CHARS} characters",
                file=sys.stderr,
            )
            return 1

    with tx():
        accounts.ensure_account(email)
        _lock_account(email)
        # Database access is the operator's proof of control. In OAuth-only
        # mode it must retire an existing password, not verify it by accident.
        if oauth_only:
            with tx() as cur:
                cur.execute("DELETE FROM credentials WHERE email = %s", (email,))
            _invalidate_password_capabilities(email)
            revoke_sessions(email)
        else:
            set_password(email, password, verified=True)
        accounts.set_status(email, "active", decided_by="chudware-ops-cli")
        account = accounts.set_role(email, "admin")
    if account is None:
        print(f"account vanished while promoting {email!r}", file=sys.stderr)
        return 1
    print(f"{account.email}: status={account.status} role={account.role}")
    return 0


def _cli_grant_subscription(email: str, *, plan: str, years: int) -> int:
    """Open the desktop entitlement window for ``email`` without Stripe.

    There is no "unlimited" sentinel to write, and adding one would mean a new
    branch in every entitlement reader. ``entitlement._entitlement_window``
    requires ``paid_through > now`` and ``subscribed_since > 0`` for any paid
    plan, so "unlimited" is representable only as a far-future ``paid_through``;
    a century is past the life of any signing key or schema this would outlive.
    ``accounts.set_subscription`` only ever advances ``paid_through``, so
    re-running this is idempotent and can never shorten an entitlement. The
    account must also be active for the window to open, which is why the status
    is asserted here rather than assumed.
    """
    try:
        from . import plans
    except ImportError:  # uvicorn app:app from the package dir
        import plans  # type: ignore

    if plan not in plans.PLANS:
        known = ", ".join(sorted(plans.PLANS))
        print(f"unknown plan {plan!r}; known plans: {known}", file=sys.stderr)
        return 1
    if years < 1:
        print("--years must be at least 1", file=sys.stderr)
        return 1
    if accounts.get_account(email) is None:
        print(
            f"no account for {email!r}; run grant-admin or register first",
            file=sys.stderr,
        )
        return 1

    now = int(time.time())
    paid_through = now + years * 365 * 86_400
    accounts.set_plan(email, plan)
    accounts.set_status(email, "active", decided_by="chudware-ops-cli")
    account = accounts.set_subscription(
        email,
        subscription_id="",
        subscription_status="active",
        price_id="",
        subscribed_since=now,
        paid_through=paid_through,
        is_edu=False,
    )
    if account is None:
        print(f"account vanished while granting {email!r}", file=sys.stderr)
        return 1
    through = datetime.fromtimestamp(
        int(account.paid_through or 0), tz=timezone.utc
    ).isoformat()
    print(
        f"{account.email}: plan={account.plan} "
        f"subscription_status={account.subscription_status} paid_through={through}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
