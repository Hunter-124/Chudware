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
  also cost the same wall-clock time. ``/api/auth/register`` returns the same 202
  for a fresh address and a taken one, and tells the *address owner* by mail.
* **Sessions are rows, not signed cookies.** Suspending an account has to end its
  live sessions on the very next request; a self-contained cookie cannot do that
  without a revocation table, at which point it is just a worse version of this.
* **Tokens are stored hashed.** ``email_tokens`` holds a SHA-256 and nothing
  else, so a database dump never yields a usable verification or reset link.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

try:
    from . import accounts
    from .config import settings
    from .db import tx
    from .limits import login_by_email, login_by_ip
    from .mailer import send as send_mail
except ImportError:  # uvicorn app:app from the package dir
    import accounts  # type: ignore
    from config import settings  # type: ignore
    from db import tx  # type: ignore
    from limits import login_by_email, login_by_ip  # type: ignore
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
    """Best-known client address, used only as a rate-limit key.

    In production cloudflared terminates the tunnel on loopback, so the socket
    peer is always 127.0.0.1 and keying on it would put the entire internet in
    one bucket. ``CF-Connecting-IP`` is therefore honoured — but ONLY when the
    peer really is loopback, so a direct caller cannot rotate the header to
    escape its own budget.
    """
    peer = (request.client.host if request.client else "") or "unknown"
    if peer in _LOOPBACK:
        forwarded = (request.headers.get("cf-connecting-ip") or "").strip()
        if forwarded:
            return forwarded
    return peer


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


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


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
    digest = hash_password(password)
    with tx() as cur:
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


def _rehash_if_needed(email: str, stored_hash: str, password: str) -> None:
    """Upgrade a credential in place when the cost parameters have moved on."""
    if not _HASHER.check_needs_rehash(stored_hash):
        return
    with tx() as cur:
        cur.execute(
            "UPDATE credentials SET password_hash = %s, updated_at = now()"
            " WHERE email = %s AND password_hash = %s",
            (hash_password(password), email, stored_hash),
        )


def mark_verified(email: str) -> None:
    with tx() as cur:
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
    """Mint a session row and return its id.

    256 bits from ``secrets`` — the id is the entire bearer credential, so it
    gets the full width rather than a truncated uuid. ``ttl`` overrides the
    configured absolute expiry; only the ops CLI below passes it.
    """
    sid = secrets.token_urlsafe(32)
    expires = _now() + (ttl or timedelta(days=max(1, int(settings.session_ttl_days))))
    with tx() as cur:
        cur.execute(
            "INSERT INTO sessions (sid, email, expires_at, user_agent, ip)"
            " VALUES (%s, %s, %s, %s, %s)",
            (sid, email, expires, user_agent[:512], _inet_or_none(ip)),
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
            (sid,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        if row["last_seen_at"] <= _now() - _TOUCH_INTERVAL:
            cur.execute(
                "UPDATE sessions SET last_seen_at = now() WHERE sid = %s", (sid,)
            )
    return row["email"]


def revoke_session(sid: str) -> None:
    if not sid:
        return
    with tx() as cur:
        cur.execute(
            "UPDATE sessions SET revoked_at = now()"
            " WHERE sid = %s AND revoked_at IS NULL",
            (sid,),
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


# ---------------------------------------------------------------------------
# Email tokens
# ---------------------------------------------------------------------------


def store_token(token: str, email: str, purpose: str, ttl: timedelta) -> None:
    """Record the SHA-256 of an ALREADY-CHOSEN token.

    ``oauth.py`` needs this: its state parameter carries an HMAC signature it
    builds itself, so the value cannot be generated here. Storing invalidates
    any outstanding token of the same purpose for the same address, so
    "resend verification" cannot leave a fan of live links behind. Skipped for
    an empty address, which is what an OAuth state row has before the callback
    resolves who is signing in.
    """
    with tx() as cur:
        if email:
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
    """Issue a token for ``purpose`` and return its PLAINTEXT (the only copy)."""
    token = secrets.token_urlsafe(32)
    store_token(token, email, purpose, ttl)
    return token


def consume_token(token: str, purpose: str) -> str | None:
    """Redeem a token exactly once. Returns the email it was minted for.

    The single-use guarantee is the UPDATE's own ``used_at IS NULL`` predicate,
    so two concurrent redemptions cannot both win.
    """
    if not token:
        return None
    with tx() as cur:
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
    """Bind (provider, subject) to an account, following a provider-side email
    change rather than forking a second account for it."""
    with tx() as cur:
        cur.execute(
            """
            INSERT INTO oauth_identities (provider, subject, email)
                 VALUES (%s, %s, %s)
            ON CONFLICT (provider, subject) DO UPDATE
                    SET email = EXCLUDED.email, linked_at = now()
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


def page_html(title: str, heading: str, body_html: str) -> str:
    """The one HTML shell every unauthenticated gateway page uses.

    Inline <style> only — the CSP forbids inline <script>, and these pages are
    served on paths where the static mount may not even be reachable yet.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title} — Chudware</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background: #0f1419; color: #e7ecf1;
           display: flex; min-height: 100vh; min-height: 100dvh; align-items: center; justify-content: center;
           margin: 0; padding: max(16px, env(safe-area-inset-top)) max(16px, env(safe-area-inset-right))
                    max(16px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left));
           box-sizing: border-box; }}
    .card {{ max-width: 32rem; width: 100%; padding: 2rem; border-radius: 12px; background: #1a2332;
             border: 1px solid #2a3a4e; box-shadow: 0 8px 32px rgba(0,0,0,.4); box-sizing: border-box; }}
    h1 {{ margin: 0 0 .75rem; font-size: 1.35rem; }}
    p {{ line-height: 1.5; color: #b0bcc8; }}
    code {{ background: #0f1419; padding: .1rem .35rem; border-radius: 4px; word-break: break-all; }}
    a {{ color: #6cb6ff; }}
    @media (max-width: 480px) {{
      .card {{ padding: 1.25rem; }}
      h1 {{ font-size: 1.2rem; }}
    }}
  </style>
</head>
<body>
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
    return f"{_base_url()}/api/auth/reset?token={token}"


def _login_url() -> str:
    return f"{_base_url()}/"


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


def complete_verification(request: Request, email: str) -> str:
    """Mark the address verified, promote a pending account onto the trial, and
    return a fresh session id. Shared by the verify link and the OAuth callback,
    both of which are proofs of address ownership."""
    mark_verified(email)
    start_trial(email)
    return create_session(
        email,
        user_agent=request.headers.get("user-agent", ""),
        ip=client_ip(request),
    )


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
    login_by_ip.enforce(client_ip(request), "authentication attempts")
    email = normalize_email(body.get("email"))
    password = str(body.get("password") or "")
    if not valid_email(email):
        raise HTTPException(status_code=400, detail="enter a valid email address")
    _check_password_policy(password)

    taken = get_credential(email) is not None or bool(oauth_providers_for(email))
    if taken:
        send_mail(
            email,
            "register_exists",
            {"email": email, "login_url": _login_url(), "reset_url": f"{_base_url()}/#forgot"},
        )
        return _ACCEPTED

    accounts.ensure_account(email, display=email.split("@")[0])
    set_password(email, password, verified=False)
    token = mint_token(email, PURPOSE_VERIFY, VERIFY_TTL)
    send_mail(
        email,
        PURPOSE_VERIFY,
        {
            "email": email,
            "verify_url": _verify_url(token),
            "expires_hours": str(int(VERIFY_TTL.total_seconds() // 3600)),
        },
    )
    log.info("authn: registration started for %s", email)
    return _ACCEPTED


@router.post("/api/auth/login")
async def login(request: Request, body: dict[str, Any]) -> JSONResponse:
    """Verify a password and open a session.

    Both limiters are enforced BEFORE any hashing: an attacker must not be able
    to spend the server's argon2 budget for free, and the per-email budget is
    what actually stops a targeted guess.
    """
    login_by_ip.enforce(client_ip(request), "sign-in attempts from this address")
    email = normalize_email(body.get("email"))
    password = str(body.get("password") or "")
    login_by_email.enforce(email, "sign-in attempts for this account")

    credential = get_credential(email)
    if credential is None:
        burn_dummy_verification(password)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED)
    if not verify_password(credential["password_hash"], password):
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED)
    _rehash_if_needed(email, credential["password_hash"], password)

    if not credential["email_verified"]:
        # Only reachable with the CORRECT password, so this is not an oracle:
        # the caller already knows the account exists.
        raise HTTPException(status_code=403, detail="email address not verified")

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
    """Redeem a verification link: verify the address, start the trial, sign in."""
    email = consume_token(token, PURPOSE_VERIFY)
    if email is None:
        return _token_page("verification")
    sid = complete_verification(request, email)
    log.info("authn: verified %s", email)
    response = RedirectResponse("/", status_code=303)
    attach_session_cookie(response, request, sid)
    return response


@router.post("/api/auth/resend-verification", status_code=202)
async def resend_verification(request: Request, body: dict[str, Any]) -> dict[str, str]:
    """Re-send a verification link. Always 202, whatever the address is."""
    login_by_ip.enforce(client_ip(request), "authentication attempts")
    email = normalize_email(body.get("email"))
    if not valid_email(email):
        return _ACCEPTED
    credential = get_credential(email)
    if credential is not None and not credential["email_verified"]:
        token = mint_token(email, PURPOSE_VERIFY, VERIFY_TTL)
        send_mail(
            email,
            PURPOSE_VERIFY,
            {
                "email": email,
                "verify_url": _verify_url(token),
                "expires_hours": str(int(VERIFY_TTL.total_seconds() // 3600)),
            },
        )
    return _ACCEPTED


@router.post("/api/auth/forgot", status_code=202)
async def forgot(request: Request, body: dict[str, Any]) -> dict[str, str]:
    """Begin a password reset. Always 202, whatever the address is.

    An address that exists only in ``oauth_identities`` has no password to
    reset, and must not be convertible into one without re-proving ownership —
    so it gets a "you signed in with Google" mail instead of a reset link.
    """
    login_by_ip.enforce(client_ip(request), "authentication attempts")
    email = normalize_email(body.get("email"))
    if not valid_email(email):
        return _ACCEPTED
    if get_credential(email) is not None:
        token = mint_token(email, PURPOSE_RESET, RESET_TTL)
        send_mail(
            email,
            PURPOSE_RESET,
            {
                "email": email,
                "reset_url": _reset_url(token),
                "expires_hours": str(max(1, int(RESET_TTL.total_seconds() // 3600))),
            },
        )
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
    login_by_ip.enforce(client_ip(request), "authentication attempts")
    password = str(body.get("password") or "")
    _check_password_policy(password)
    email = consume_token(str(body.get("token") or ""), PURPOSE_RESET)
    if email is None:
        raise HTTPException(status_code=400, detail="that reset link is no longer valid")
    set_password(email, password, verified=True)
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

    Prints one short-lived session id on stdout. Deploy scripts that must call
    an authenticated loopback endpoint — install_webd.sh polls /healthz for the
    active engine-bridge count before it drops users — used to do that with the
    oauth2-proxy shared secret. That secret is gone, and inventing a second
    machine credential to replace it would just be the same trusted-string
    problem under a new name. A real session row is better in every way: it
    expires on its own, it is revocable, and it is the exact credential path
    the middleware already verifies.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="authn")
    sub = parser.add_subparsers(dest="command", required=True)
    mint = sub.add_parser("ops-session", help="mint a short-lived session id")
    mint.add_argument("--email", required=True)
    mint.add_argument("--ttl-seconds", type=int, default=900)
    args = parser.parse_args(argv)

    email = normalize_email(args.email)
    account = accounts.get_account(email)
    if account is None or not accounts.is_authorized(account):
        print(f"no active account for {email!r}", file=sys.stderr)
        return 1
    ttl = timedelta(seconds=max(1, args.ttl_seconds))
    print(create_session(email, user_agent="chudware-ops-cli", ttl=ttl))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
