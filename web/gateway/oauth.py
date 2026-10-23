"""Google, GitHub, and Microsoft sign-in, hand-rolled on httpx + PyJWT.

Three providers, one shared shape, ~300 lines. Authlib would add a dependency
to the login path — the single riskiest path in the gateway — to save the code
below, and would still need its own JWKS cache when ``auth.JWKSCache`` already
exists and is already exercised by the Cloudflare Access branch. MSAL would add
a second, much larger one for a single provider.

Google and Microsoft are OIDC: the ``id_token`` they return is an RS256 JWT
verified against published JWKS, so the email arrives signed. GitHub is plain
OAuth2 with no identity token at all, so the email comes from a second
authenticated call to ``GET /user/emails``, filtered to the address GitHub
itself marks primary *and* verified — an unverified GitHub email is a claim,
not a proof. Microsoft needs a further check of its own (see ``nOAuth`` at
``_verify_microsoft_id_token``): in a multi-tenant app the ``email`` claim is
only trustworthy when the tenant has proven it owns the domain.

CSRF on the callback is the ``state`` parameter: a random nonce, HMAC-signed
with the session secret and bound to the provider, stored as a SHA-256 in
``email_tokens`` so redeeming it is atomically single-use, and paired with a
short-lived browser cookie so a stolen state cannot swap another browser's
session. The post-sign-in destination travels the same way: it is covered by
the state signature and carried beside the verifier in that one cookie, so a
desktop sign-in can be resumed at ``/desktop/authorize`` without the redirect
target ever being attacker-controlled.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from html import escape
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from urllib.parse import quote, unquote, urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

try:
    from . import accounts, authn
    from .auth import JWKSCache
    from .config import settings
    from .db import tx
    from .limits import login_by_ip, request_network
except ImportError:  # uvicorn app:app from the package dir
    import accounts  # type: ignore
    import authn  # type: ignore
    from auth import JWKSCache  # type: ignore
    from config import settings  # type: ignore
    from db import tx  # type: ignore
    from limits import login_by_ip, request_network  # type: ignore

import jwt

log = logging.getLogger("chudware-web.oauth")

router = APIRouter(tags=["auth"])

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
# Google has emitted both spellings over the years and still documents both as
# acceptable; PyJWT takes a container for exactly this case.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

# Entra endpoints are tenant-parameterised. Templates, not URLs: the tenant is
# configuration and must be read at request time, never frozen at import.
MICROSOFT_AUTHORITY = "https://login.microsoftonline.com"
MICROSOFT_JWKS_URL_TEMPLATE = f"{MICROSOFT_AUTHORITY}/{{tenant}}/discovery/v2.0/keys"
MICROSOFT_ISSUER_TEMPLATE = f"{MICROSOFT_AUTHORITY}/{{tid}}/v2.0"
# Tenant selectors that are routing aliases rather than a directory: with any
# of these the token's own ``tid`` is the only tenant we learn, so it cannot be
# compared against configuration.
MICROSOFT_MULTI_TENANT = frozenset({"common", "organizations", "consumers"})
# The fixed tenant id every personal Microsoft account (MSA) signs in under.
# It is not a directory anyone administers, which is why it is exempted from
# the xms_edov requirement below.
MICROSOFT_CONSUMERS_TID = "9188040d-6c67-4c5b-b112-36a304b66dad"

_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
OAUTH_TRANSACTION_COOKIE = "chudware_oauth_transaction"
OAUTH_TRANSACTION_MAX_AGE = 10 * 60
# Outstanding states, shared across providers, are bounded in PostgreSQL rather
# than trusting a process limiter whose network keys can be evicted by churn.
OAUTH_STATES_PER_NETWORK = 10


@dataclass(frozen=True)
class _Provider:
    name: str
    label: str
    # URL *templates*. Entra's endpoints carry ``{tenant}``; Google's and
    # GitHub's carry no placeholder, so ``_endpoint`` serves all three and the
    # frozen table never holds a tenant that configuration has since changed.
    authorize_url: str
    token_url: str
    scope: str


PROVIDERS: dict[str, _Provider] = {
    "google": _Provider(
        name="google",
        label="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope="openid email",
    ),
    "github": _Provider(
        name="github",
        label="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scope="read:user user:email",
    ),
    "microsoft": _Provider(
        name="microsoft",
        label="Microsoft",
        authorize_url=f"{MICROSOFT_AUTHORITY}/{{tenant}}/oauth2/v2.0/authorize",
        token_url=f"{MICROSOFT_AUTHORITY}/{{tenant}}/oauth2/v2.0/token",
        scope="openid email profile",
    ),
}

_google_jwks: JWKSCache | None = None
_microsoft_jwks: dict[str, JWKSCache] = {}


class OAuthError(Exception):
    """A callback that cannot be completed. Rendered as an HTML page, never as
    a redirect carrying provider detail back into a URL."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _credentials(provider: str) -> tuple[str, str]:
    if provider == "google":
        return settings.google_client_id, settings.google_client_secret
    if provider == "microsoft":
        return settings.microsoft_client_id, settings.microsoft_client_secret
    return settings.github_client_id, settings.github_client_secret


def _tenant() -> str:
    """The Entra tenant selector, read fresh on every call."""
    return (settings.microsoft_tenant or "common").strip() or "common"


def _endpoint(template: str) -> str:
    """Resolve a provider URL template against the tenant configured *now*."""
    return template.format(tenant=_tenant())


def configured_providers() -> list[str]:
    """Providers with both halves of their client credentials present."""
    return [p for p in PROVIDERS if all(_credentials(p))]


def _provider_or_error(name: str) -> _Provider:
    provider = PROVIDERS.get(name)
    if provider is None or not all(_credentials(name)):
        raise OAuthError("that sign-in provider is not enabled on this deployment")
    return provider


def _redirect_uri(provider: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/auth/oauth/{provider}/callback"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


# A destination is only ever a path on this origin. Anything else — an absolute
# URL, a scheme-relative "//host", a backslash that some browsers fold into a
# separator, a control character smuggled into a header — collapses to "/".
# These are the same rules ``safeNext()`` applies in static/login*.js, so the
# link the browser builds and the link the server honours cannot disagree.
_UNSAFE_NEXT_RE = re.compile(r"[\x00-\x1f\x7f\\]")
DEFAULT_NEXT = "/"


def _safe_next(value: str) -> str:
    """``value`` if it is a same-origin local path, else ``"/"``."""
    candidate = value or DEFAULT_NEXT
    if not candidate.startswith("/") or candidate.startswith("//"):
        return DEFAULT_NEXT
    if _UNSAFE_NEXT_RE.search(candidate):
        return DEFAULT_NEXT
    return candidate


def _sign_state(provider: str, nonce: str, verifier: str, next_path: str) -> str:
    """Bind the provider state to the verifier held only by its browser, and to
    the destination that state is allowed to land on."""
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        f"{provider}:{nonce}:{verifier}:{next_path}".encode("utf-8"),
        sha256,
    ).hexdigest()


def mint_state(
    provider: str, next_path: str = DEFAULT_NEXT, *, network: str = "unknown"
) -> tuple[str, str]:
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(32)
    state = f"{nonce}.{_sign_state(provider, nonce, verifier, next_path)}"
    # OAuth has no account yet. Reuse the token table's non-FK email key as a
    # namespaced network bucket; never pass that key to account/token helpers.
    key = f"oauth-network:{network}"
    lock = int.from_bytes(sha256(key.encode()).digest()[:8], "big", signed=True)
    with tx() as cur:
        # A transaction-scoped lock makes the count/insert bound hold across
        # concurrent requests and gateway processes, not just this event loop.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
        cur.execute(
            "DELETE FROM email_tokens WHERE purpose = %s"
            " AND (used_at IS NOT NULL OR expires_at <= now())",
            (authn.PURPOSE_OAUTH_STATE,),
        )
        cur.execute(
            "SELECT count(*) AS n FROM email_tokens"
            " WHERE email = %s AND purpose = %s AND used_at IS NULL",
            (key, authn.PURPOSE_OAUTH_STATE),
        )
        if cur.fetchone()["n"] >= OAUTH_STATES_PER_NETWORK:
            raise HTTPException(
                status_code=429,
                detail="too many pending sign-ins — complete one or try again later",
                headers={"Retry-After": str(OAUTH_TRANSACTION_MAX_AGE)},
            )
        cur.execute(
            "INSERT INTO email_tokens (token_hash, email, purpose, expires_at)"
            " VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
            (authn.token_hash(state), key, authn.PURPOSE_OAUTH_STATE,
             authn.OAUTH_STATE_TTL.total_seconds()),
        )
    return state, verifier


def check_state(
    provider: str, state: str, verifier: str, next_path: str = DEFAULT_NEXT
) -> bool:
    """Require its browser verifier, then atomically redeem the signed state."""
    nonce, separator, signature = (state or "").partition(".")
    if not nonce or not separator or not signature or not verifier:
        return False
    if not hmac.compare_digest(
        signature, _sign_state(provider, nonce, verifier, next_path)
    ):
        return False
    # Delete atomically instead of retaining used rows. The network bucket is
    # not an account email, and callbacks may legitimately change networks.
    # Signature, browser verifier and destination must all match before this
    # deletion, including for states minted before network buckets existed.
    with tx() as cur:
        cur.execute(
            "DELETE FROM email_tokens WHERE token_hash = %s AND purpose = %s"
            " AND used_at IS NULL AND expires_at > now() RETURNING token_hash",
            (authn.token_hash(state), authn.PURPOSE_OAUTH_STATE),
        )
        return cur.fetchone() is not None


def _transaction_value(verifier: str, next_path: str) -> str:
    """One cookie, one value: the verifier, then the percent-encoded
    destination. The verifier is ``token_urlsafe`` and the encoding leaves no
    dot in the destination, so the first dot is an unambiguous separator."""
    return f"{verifier}.{quote(next_path, safe='')}"


def _split_transaction(raw: str) -> tuple[str, str]:
    """(verifier, destination) from a transaction cookie. A malformed value
    yields an empty verifier, which no state can be checked against."""
    verifier, separator, encoded = (raw or "").partition(".")
    if not verifier or not separator:
        return "", DEFAULT_NEXT
    return verifier, _safe_next(unquote(encoded))


def _attach_transaction_cookie(
    response: Response, request: Request, verifier: str, next_path: str
) -> None:
    response.headers.append(
        "set-cookie",
        authn._cookie(
            OAUTH_TRANSACTION_COOKIE,
            _transaction_value(verifier, next_path),
            max_age=OAUTH_TRANSACTION_MAX_AGE,
            secure=authn._cookie_secure(request),
        ),
    )


def _clear_transaction_cookie(response: Response, request: Request) -> None:
    response.headers.append(
        "set-cookie",
        authn._cookie(
            OAUTH_TRANSACTION_COOKIE,
            "",
            max_age=0,
            secure=authn._cookie_secure(request),
        ),
    )


# ---------------------------------------------------------------------------
# Provider calls
# ---------------------------------------------------------------------------


async def _exchange_code(provider: _Provider, code: str) -> dict[str, Any]:
    client_id, client_secret = _credentials(provider.name)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": _redirect_uri(provider.name),
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(
            _endpoint(provider.token_url),
            data=form,
            headers={"Accept": "application/json"},
        )
    if response.status_code != 200:
        # The provider's own error code is the only thing that distinguishes a
        # stale code from a rejected credential or a misconfigured redirect, and
        # it is a diagnostic rather than a secret. The code and the client
        # secret stay out of the log; the description is truncated because
        # providers put whole paragraphs in it.
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = " ".join(
                    str(body.get(field, ""))
                    for field in ("error", "error_description")
                ).strip()[:200]
        except ValueError:
            detail = ""
        log.warning(
            "oauth: %s token exchange failed (%s) %s",
            provider.name,
            response.status_code,
            detail,
        )
        raise OAuthError("the sign-in provider rejected that request")
    try:
        payload = response.json()
    except ValueError as exc:
        raise OAuthError("the sign-in provider returned an unreadable response") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise OAuthError("the sign-in provider rejected that request")
    return payload


def _verify_google_id_token(id_token: str) -> tuple[str, str]:
    global _google_jwks
    if _google_jwks is None:
        _google_jwks = JWKSCache(GOOGLE_JWKS_URL)
    client_id, _ = _credentials("google")
    try:
        key = _google_jwks.get_signing_key(id_token)
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=GOOGLE_ISSUERS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        log.warning("oauth: google id_token verification failed: %s", exc)
        raise OAuthError("could not verify the response from Google") from exc

    email = authn.normalize_email(claims.get("email"))
    if not claims.get("email_verified") or not authn.valid_email(email):
        raise OAuthError("Google did not supply a verified email address")
    return str(claims["sub"]), email


def _microsoft_jwks_for(tenant: str) -> JWKSCache:
    cache = _microsoft_jwks.get(tenant)
    if cache is None:
        cache = JWKSCache(MICROSOFT_JWKS_URL_TEMPLATE.format(tenant=tenant))
        _microsoft_jwks[tenant] = cache
    return cache


def _edov_is_true(raw: Any) -> bool:
    """``xms_edov`` reaches us as a JSON boolean from some tenants and as the
    string "1" or "true" from others. Everything else, absence included, is
    false."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true"}
    return False


def _verify_microsoft_id_token(id_token: str) -> tuple[str, str]:
    tenant = _tenant()
    client_id, _ = _credentials("microsoft")
    try:
        key = _microsoft_jwks_for(tenant).get_signing_key(id_token)
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256"],
            audience=client_id,
            # verify_iss is off ON PURPOSE, and the issuer is not going
            # unchecked. With tenant "common" the issuer is per-directory —
            # https://login.microsoftonline.com/<tid>/v2.0 — so there is no
            # single string PyJWT could be handed up front. PyJWT's check is
            # therefore compensated below by an exact comparison against the
            # issuer the token's own ``tid`` implies, plus a tid bound. Both
            # are required, so this is stricter than a static issuer, not
            # looser.
            options={
                "require": ["exp", "iss", "aud", "sub", "tid"],
                "verify_iss": False,
            },
        )
    except jwt.PyJWTError as exc:
        log.warning("oauth: microsoft id_token verification failed: %s", exc)
        raise OAuthError("could not verify the response from Microsoft") from exc

    tid = str(claims.get("tid") or "")
    issuer = str(claims.get("iss") or "")
    if not tid or not hmac.compare_digest(
        issuer, MICROSOFT_ISSUER_TEMPLATE.format(tid=tid)
    ):
        raise OAuthError("that Microsoft token came from an unexpected issuer")
    # A deployment pinned to one directory gets that pin enforced: a token from
    # any other tenant is the wrong directory even when it is internally
    # consistent.
    if tenant not in MICROSOFT_MULTI_TENANT and not hmac.compare_digest(tid, tenant):
        raise OAuthError("that Microsoft account is not in this deployment's directory")

    # nOAuth. link_account() links a provider identity to an existing account
    # BY EMAIL, and in a multi-tenant Entra app any tenant administrator can
    # set an arbitrary, unverified ``email`` on a user in their own directory —
    # including an address at a domain they do not own. Trusting the bare claim
    # hands them the matching Chudware account; that is the published nOAuth
    # account-takeover vector. ``xms_edov`` is Microsoft asserting the domain
    # is verified as owned by the issuing tenant, so it is the only condition
    # under which the claim is proof rather than tenant-controlled input.
    # Personal accounts are the exception, and only because the premise above
    # does not hold for them: the consumers tenant has no administrator who can
    # mint users, Microsoft owns the address space, and xms_edov is an Entra-only
    # claim that MSA never emits. Requiring it there would refuse every personal
    # Microsoft account instead of stopping an attack.
    if tid != MICROSOFT_CONSUMERS_TID and not _edov_is_true(claims.get("xms_edov")):
        raise OAuthError(
            "that Microsoft tenant has not proven it owns that email address, so "
            "we cannot sign you in with it — use another sign-in method, or ask "
            "your administrator to emit the xms_edov optional claim"
        )
    email = authn.normalize_email(claims.get("email"))
    # No preferred_username fallback on purpose: it is a display hint the
    # tenant also controls, so falling back to it would reopen nOAuth by
    # another door.
    if not authn.valid_email(email):
        raise OAuthError("Microsoft did not supply a usable email address")

    # oid + tid rather than sub: ``sub`` is a pairwise pseudonym scoped to this
    # application as well as the user, so recreating the app registration
    # rotates it and every linked account would orphan. ``oid`` is the
    # immutable directory object id, unique only within its tenant, so the
    # tenant-qualified pair is the durable global identity.
    oid = str(claims.get("oid") or "").strip()
    subject = f"{tid}.{oid}" if oid else str(claims["sub"])
    return subject, email


async def _github_identity(access_token: str) -> tuple[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=headers) as client:
        profile = await client.get("https://api.github.com/user")
        emails = await client.get("https://api.github.com/user/emails")
    if profile.status_code != 200 or emails.status_code != 200:
        raise OAuthError("GitHub would not tell us who you are")

    subject = str((profile.json() or {}).get("id") or "").strip()
    rows = emails.json()
    if not subject or not isinstance(rows, list):
        raise OAuthError("GitHub returned an unreadable profile")

    # Primary AND verified. A verified-but-secondary address is not the identity
    # GitHub considers canonical, and an unverified one is just user input.
    chosen = ""
    for row in rows:
        if not isinstance(row, dict) or not row.get("verified"):
            continue
        candidate = authn.normalize_email(row.get("email"))
        if not authn.valid_email(candidate):
            continue
        if row.get("primary"):
            chosen = candidate
            break
    if not chosen:
        raise OAuthError(
            "no verified primary email on that GitHub account — verify one and try again"
        )
    return subject, chosen


async def resolve_identity(provider: _Provider, code: str) -> tuple[str, str]:
    """(subject, email) proven by ``provider`` for this authorization code."""
    token_payload = await _exchange_code(provider, code)
    if provider.name in ("google", "microsoft"):
        id_token = str(token_payload.get("id_token") or "")
        if not id_token:
            raise OAuthError(f"{provider.label} did not return an identity token")
        if provider.name == "microsoft":
            return _verify_microsoft_id_token(id_token)
        return _verify_google_id_token(id_token)
    access_token = str(token_payload.get("access_token") or "")
    if not access_token:
        raise OAuthError("GitHub did not return an access token")
    return await _github_identity(access_token)


# ---------------------------------------------------------------------------
# Account linking
# ---------------------------------------------------------------------------


def link_account(provider: str, subject: str, email: str) -> str:
    """Account this identity signs into, creating or linking as needed.

    A provider identity whose email already has an account — password or not —
    LINKS to it. Creating a second account for the same address would fork the
    user's documents and billing in half the first time they clicked the other
    button.
    """
    with tx():
        existing = authn.oauth_email(provider, subject)
        if existing:
            # The stable subject owns the account even if its email changed.
            return existing
        accounts.ensure_account(email, display=email.split("@")[0])
        authn._lock_account(email)
        authn.link_oauth_identity(provider, subject, email)
        # Concurrent callbacks can first link the same subject. The immutable
        # binding wins; never return the loser's provider email as its account.
        linked = authn.oauth_email(provider, subject)
        assert linked is not None
    log.info("oauth: linked %s identity %s to %s", provider, subject, linked)
    return linked


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _error_page(message: str) -> Response:
    return HTMLResponse(
        authn.page_html(
            "Sign-in failed",
            "That sign-in could not be completed",
            f"    <p>{escape(message)}.</p>\n"
            '    <p><a href="/">Back to Chudware</a> and try again.</p>',
        ),
        status_code=400,
    )


@router.get("/api/auth/oauth/{provider}/start", response_model=None)
async def oauth_start(provider: str, request: Request, next: str = "") -> Response:
    network = request_network(request)
    login_by_ip.enforce(network, "authentication attempts")
    try:
        entry = _provider_or_error(provider)
    except OAuthError as exc:
        return _error_page(exc.message)
    next_path = _safe_next(next)
    state, verifier = mint_state(entry.name, next_path, network=network)
    query = urlencode(
        {
            "client_id": _credentials(entry.name)[0],
            "redirect_uri": _redirect_uri(entry.name),
            "response_type": "code",
            "scope": entry.scope,
            "state": state,
            # Neither Google nor Microsoft returns a refresh token without a
            # consent prompt, which we do not want or store; select_account is
            # what makes account switching possible on both.
            **(
                {"prompt": "select_account"}
                if entry.name in ("google", "microsoft")
                else {}
            ),
        }
    )
    response = RedirectResponse(
        f"{_endpoint(entry.authorize_url)}?{query}", status_code=302
    )
    _attach_transaction_cookie(response, request, verifier, next_path)
    return response


@router.get("/api/auth/oauth/{provider}/callback", response_model=None)
async def oauth_callback(
    provider: str, request: Request, code: str = "", state: str = "", error: str = ""
) -> Response:
    login_by_ip.enforce(request_network(request), "authentication attempts")
    verifier, next_path = _split_transaction(
        request.cookies.get(OAUTH_TRANSACTION_COOKIE, "")
    )
    try:
        entry = _provider_or_error(provider)
        if error:
            raise OAuthError(f"{entry.label} reported: {error}")
        if not code:
            raise OAuthError("that sign-in response carried no authorization code")
        if not check_state(entry.name, state, verifier, next_path):
            raise OAuthError("that sign-in link expired or was already used")
        subject, email = await resolve_identity(entry, code)
    except OAuthError as exc:
        response = _error_page(exc.message)
        _clear_transaction_cookie(response, request)
        return response

    try:
        with tx():
            account_email = link_account(entry.name, subject, email)
            sid = authn.complete_oauth(request, account_email)
    except HTTPException as exc:
        response = _error_page(str(exc.detail))
        response.status_code = exc.status_code
        _clear_transaction_cookie(response, request)
        return response
    log.info("oauth: %s sign-in for %s", entry.name, account_email)
    response = RedirectResponse(next_path, status_code=303)
    authn.attach_session_cookie(response, request, sid)
    _clear_transaction_cookie(response, request)
    return response


__all__ = [
    "DEFAULT_NEXT",
    "GOOGLE_ISSUERS",
    "GOOGLE_JWKS_URL",
    "MICROSOFT_AUTHORITY",
    "MICROSOFT_ISSUER_TEMPLATE",
    "MICROSOFT_JWKS_URL_TEMPLATE",
    "MICROSOFT_MULTI_TENANT",
    "OAuthError",
    "OAUTH_TRANSACTION_COOKIE",
    "PROVIDERS",
    "check_state",
    "configured_providers",
    "link_account",
    "mint_state",
    "resolve_identity",
    "router",
]
