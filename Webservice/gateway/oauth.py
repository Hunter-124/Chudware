"""Google and GitHub sign-in, hand-rolled on httpx + PyJWT.

Two providers, one shared shape, ~200 lines. Authlib would add a dependency to
the login path — the single riskiest path in the gateway — to save the code
below, and would still need its own JWKS cache when ``auth.JWKSCache`` already
exists and is already exercised by the Cloudflare Access branch.

Google is OIDC: the ``id_token`` it returns is an RS256 JWT verified against
Google's published JWKS, so the email arrives signed. GitHub is plain OAuth2
with no identity token at all, so the email comes from a second authenticated
call to ``GET /user/emails``, filtered to the address GitHub itself marks
primary *and* verified — an unverified GitHub email is a claim, not a proof.

CSRF on the callback is the ``state`` parameter: a random nonce, HMAC-signed
with the session secret and bound to the provider, stored as a SHA-256 in
``email_tokens`` so redeeming it is atomically single-use, and expiring after
ten minutes.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

try:
    from . import accounts, authn
    from .auth import JWKSCache
    from .config import settings
    from .limits import login_by_ip
except ImportError:  # uvicorn app:app from the package dir
    import accounts  # type: ignore
    import authn  # type: ignore
    from auth import JWKSCache  # type: ignore
    from config import settings  # type: ignore
    from limits import login_by_ip  # type: ignore

import jwt

log = logging.getLogger("chudware-web.oauth")

router = APIRouter(tags=["auth"])

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
# Google has emitted both spellings over the years and still documents both as
# acceptable; PyJWT takes a container for exactly this case.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@dataclass(frozen=True)
class _Provider:
    name: str
    label: str
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
}

_google_jwks: JWKSCache | None = None


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
    return settings.github_client_id, settings.github_client_secret


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


def _sign_state(provider: str, nonce: str) -> str:
    """HMAC over provider *and* nonce, so a state minted for Google's callback
    cannot be replayed at GitHub's."""
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        f"{provider}:{nonce}".encode("utf-8"),
        sha256,
    ).hexdigest()


def mint_state(provider: str) -> str:
    nonce = secrets.token_urlsafe(24)
    state = f"{nonce}.{_sign_state(provider, nonce)}"
    # store_token, not mint_token: the value is ours, signature and all.
    authn.store_token(state, "", authn.PURPOSE_OAUTH_STATE, authn.OAUTH_STATE_TTL)
    return state


def check_state(provider: str, state: str) -> bool:
    """Signature first (free), then the single-use database redemption."""
    nonce, _, signature = (state or "").partition(".")
    if not nonce or not signature:
        return False
    if not hmac.compare_digest(signature, _sign_state(provider, nonce)):
        return False
    # consume_token returns the (empty) email it was minted against, so success
    # is "not None" rather than truthiness.
    return authn.consume_token(state, authn.PURPOSE_OAUTH_STATE) is not None


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
            provider.token_url, data=form, headers={"Accept": "application/json"}
        )
    if response.status_code != 200:
        log.warning(
            "oauth: %s token exchange failed (%s)", provider.name, response.status_code
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
    if provider.name == "google":
        id_token = str(token_payload.get("id_token") or "")
        if not id_token:
            raise OAuthError("Google did not return an identity token")
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
    existing = authn.oauth_email(provider, subject)
    if existing:
        # Already linked. The provider-side email may have changed since; the
        # account it points at deliberately does not follow, because the subject
        # is the stable identity and the account owns the data.
        return existing
    accounts.ensure_account(email, display=email.split("@")[0])
    authn.link_oauth_identity(provider, subject, email)
    log.info("oauth: linked %s identity %s to %s", provider, subject, email)
    return email


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _error_page(message: str) -> Response:
    return HTMLResponse(
        authn.page_html(
            "Sign-in failed",
            "That sign-in could not be completed",
            f"    <p>{message}.</p>\n"
            '    <p><a href="/">Back to Chudware</a> and try again.</p>',
        ),
        status_code=400,
    )


@router.get("/api/auth/oauth/{provider}/start", response_model=None)
async def oauth_start(provider: str, request: Request) -> Response:
    login_by_ip.enforce(authn.client_ip(request), "authentication attempts")
    try:
        entry = _provider_or_error(provider)
    except OAuthError as exc:
        return _error_page(exc.message)
    query = urlencode(
        {
            "client_id": _credentials(entry.name)[0],
            "redirect_uri": _redirect_uri(entry.name),
            "response_type": "code",
            "scope": entry.scope,
            "state": mint_state(entry.name),
            # Google only returns a refresh token with consent, which we do not
            # want or store; select_account makes account switching possible.
            **({"prompt": "select_account"} if entry.name == "google" else {}),
        }
    )
    return RedirectResponse(f"{entry.authorize_url}?{query}", status_code=302)


@router.get("/api/auth/oauth/{provider}/callback", response_model=None)
async def oauth_callback(
    provider: str, request: Request, code: str = "", state: str = "", error: str = ""
) -> Response:
    login_by_ip.enforce(authn.client_ip(request), "authentication attempts")
    try:
        entry = _provider_or_error(provider)
        if error:
            raise OAuthError(f"{entry.label} reported: {error}")
        if not code:
            raise OAuthError("that sign-in response carried no authorization code")
        if not check_state(entry.name, state):
            raise OAuthError("that sign-in link expired or was already used")
        subject, email = await resolve_identity(entry, code)
    except OAuthError as exc:
        return _error_page(exc.message)

    account_email = link_account(entry.name, subject, email)
    # The provider just proved control of the address, so this is a full
    # verification: it marks any password credential verified and promotes a
    # pending account onto the trial, exactly as clicking a verify link would.
    sid = authn.complete_verification(request, account_email)
    log.info("oauth: %s sign-in for %s", entry.name, account_email)
    response = RedirectResponse("/", status_code=303)
    authn.attach_session_cookie(response, request, sid)
    return response


__all__ = [
    "GOOGLE_ISSUERS",
    "GOOGLE_JWKS_URL",
    "OAuthError",
    "PROVIDERS",
    "check_state",
    "configured_providers",
    "link_account",
    "mint_state",
    "resolve_identity",
    "router",
]
