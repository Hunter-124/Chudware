"""Browser-driven AI subscription sign-in, scoped per account.

`provider_keys.py` stores a customer's own API key. This module stores OAuth
grants against Claude Pro/Max and ChatGPT Plus/Pro subscriptions. A requester's
personal link takes precedence; active, verified trial-domain accounts may use
active admins' Claude links (personal or shared), without metered pool credits.
Only Claude OAuth supports isolated web-worker injection; unsupported provider
injection is refused downstream, never sent to ambient auth.

The scopes identify who linked a grant, not who may spend it:

* ``personal`` — the linking account's own subscription, also eligible as a
  Claude sponsor grant when the owner is an active admin.
* ``shared`` — an admin-managed subscription, eligible only as a Claude sponsor
  grant for verified, active trial-domain users.

Why these flows remain web-service-local rather than using the packaged desktop
Chudcode credential store: every login and grant is tenant-scoped here, while a
single desktop service has one process-wide credential store and loopback
callback lifecycle. Sharing that store across web workers would cross account
boundaries. The wire protocol matches the vendors' first-party clients; only
the multi-tenant transport and storage differ.

Neither flow can use a redirect back to this origin, because the registered
redirect URIs belong to the vendors' own first-party clients. Claude therefore
finishes by having the operator paste the redirect it lands on, and ChatGPT
uses its device-code endpoint, which needs no redirect at all.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException, Request

try:
    from .auth import get_current_user
    from . import deployment_drain
    from .db import tx
    from .credits import is_trial_domain
    from .limits import RateLimiter

    # Deliberately the same key material as BYOK: one operator secret to
    # manage, one failure mode to reason about. Only the AAD domain differs,
    # so a key envelope can never be replayed across the two tables.
    from .provider_keys import _key_bytes
except ImportError:  # uvicorn app:app from the package directory
    from auth import get_current_user  # type: ignore
    import deployment_drain
    from credits import is_trial_domain  # type: ignore
    from db import tx  # type: ignore
    from limits import RateLimiter  # type: ignore
    from provider_keys import _key_bytes  # type: ignore

log = logging.getLogger("chudware-web.provider-accounts")

router = APIRouter(prefix="/api/providers", tags=["provider-accounts"])

# Starting a flow performs third-party I/O for the device grant, so it carries
# its own tight budget. Completing one is polled every 5s by the browser for up
# to the flow's lifetime, which is why its budget is far larger.
provider_oauth_starts = RateLimiter(10, 300.0)
provider_oauth_completions = RateLimiter(240, 900.0)

_ENVELOPE_VERSION = 1
_NONCE_BYTES = 12
_FLOW_TTL_SECONDS = 600
_MAX_CODE_BYTES = 4096
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Refresh this far before the access token actually dies, so a turn that starts
# just under the wire does not expire mid-request.
_REFRESH_SKEW_MS = 5 * 60 * 1000

PERSONAL = "personal"
SHARED = "shared"


@dataclass(frozen=True)
class _SubscriptionProvider:
    provider: str
    label: str
    kind: str
    # Turn-level provider names this subscription can serve. The wire name is
    # what the client selects for a turn ("anthropic"); the subscription id is
    # how the grant is stored ("anthropic-sub").
    wire_names: tuple[str, ...]


SUBSCRIPTION_PROVIDERS: dict[str, _SubscriptionProvider] = {
    "anthropic-sub": _SubscriptionProvider(
        provider="anthropic-sub",
        label="Claude (Pro / Max)",
        kind="pkce_paste",
        wire_names=("anthropic", "anthropic-sub", "claude"),
    ),
    "openai-codex": _SubscriptionProvider(
        provider="openai-codex",
        label="ChatGPT (Plus / Pro)",
        kind="device",
        wire_names=("openai", "openai-codex", "codex", "chatgpt"),
    ),
}

_WIRE_TO_SUBSCRIPTION: dict[str, str] = {
    wire: spec.provider
    for spec in SUBSCRIPTION_PROVIDERS.values()
    for wire in spec.wire_names
}

# --- vendor endpoints -------------------------------------------------------
# Constants mirror services/agent/providers/{anthropic,codex}_sub_provider.py.
# These are the vendors' own first-party client ids, exactly as their published
# CLIs use them; there is no client secret in either flow (both are PKCE).

ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
ANTHROPIC_REDIRECT_URI = "http://localhost:54545/callback"
ANTHROPIC_SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)
_ANTHROPIC_TOKEN_HEADERS = {
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
}

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_DEVICE_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CODEX_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CODEX_DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
CODEX_DEVICE_VERIFICATION_URL = "https://auth.openai.com/codex/device"

ANTHROPIC_INSTRUCTIONS = (
    "Approve access in the tab that just opened. Your browser will finish on a "
    "page that cannot load - that is expected. Copy the whole address out of "
    "the address bar and paste it below."
)
CODEX_INSTRUCTIONS = (
    "Open the link, sign in to ChatGPT, and enter this code. This page finishes "
    "on its own once you approve."
)


class SubscriptionAuthError(Exception):
    """A flow that cannot be completed, with a message safe to show the user."""


# ---------------------------------------------------------------------------
# Normalisation and envelopes
# ---------------------------------------------------------------------------


def _email(value: Any) -> str:
    return str(value or "").strip().lower()


def _provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    if provider not in SUBSCRIPTION_PROVIDERS:
        raise HTTPException(404, "unknown subscription provider")
    return provider


def _scope(value: Any) -> str:
    scope = str(value or PERSONAL).strip().lower()
    if scope not in (PERSONAL, SHARED):
        raise HTTPException(400, "scope must be personal or shared")
    return scope


def _aad(email: str, provider: str, purpose: str) -> bytes:
    return (
        b"chudware-provider-account-v1\0"
        + purpose.encode()
        + b"\0"
        + email.encode()
        + b"\0"
        + provider.encode()
    )


def _seal(email: str, provider: str, purpose: str, payload: dict[str, Any]) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(_key_bytes()).encrypt(
        nonce,
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        _aad(_email(email), provider, purpose),
    )
    return bytes((_ENVELOPE_VERSION,)) + nonce + ciphertext


def _open(email: str, provider: str, purpose: str, envelope: bytes) -> dict[str, Any]:
    raw = bytes(envelope)
    if len(raw) <= 1 + _NONCE_BYTES or raw[0] != _ENVELOPE_VERSION:
        raise RuntimeError("subscription credential envelope is invalid")
    try:
        plaintext = AESGCM(_key_bytes()).decrypt(
            raw[1 : 1 + _NONCE_BYTES],
            raw[1 + _NONCE_BYTES :],
            _aad(_email(email), provider, purpose),
        )
        decoded = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("subscription credential cannot be decrypted") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("subscription credential cannot be decrypted")
    return decoded


def _storage_available() -> None:
    """Fail closed, and without disclosing configuration, when the key is unset."""
    try:
        _key_bytes()
    except RuntimeError:
        raise HTTPException(
            503, "subscription account storage is unavailable"
        ) from None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _jwt_claims(token: str) -> dict[str, Any]:
    """Claims from a JWT we just received over TLS from the token endpoint.

    Deliberately unverified: this is our own freshly issued token, read only to
    label the row in the UI. Nothing here is a trust decision.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _upstream_detail(response: httpx.Response, fallback: str) -> str:
    """The vendor's own rejection, trimmed, so the user sees the real reason."""
    body = (response.text or "").strip()
    if not body:
        return fallback
    try:
        parsed = json.loads(body)
    except ValueError:
        return f"{fallback}: {body[:300]}"
    if isinstance(parsed, dict):
        for field in ("error_description", "detail", "message", "error"):
            value = parsed.get(field)
            if isinstance(value, str) and value.strip():
                return f"{fallback}: {value.strip()[:300]}"
    return f"{fallback}: {body[:300]}"


def parse_pasted_code(raw: str) -> tuple[str, str]:
    """(code, state) from a pasted redirect URL, ``code#state``, or bare code."""
    value = (raw or "").strip()
    if not value:
        raise SubscriptionAuthError("Paste the address you were redirected to.")
    if "://" in value:
        query = parse_qs(urlsplit(value).query)
        error = (query.get("error") or [""])[0]
        if error:
            description = (query.get("error_description") or [""])[0]
            raise SubscriptionAuthError(
                description.strip() or f"Sign-in was refused: {error}"
            )
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code:
            raise SubscriptionAuthError(
                "That address has no authorization code in it. Copy the full "
                "address you landed on, including everything after the '?'."
            )
        # Anthropic appends the state to the code as a fragment on some paths.
        if "#" in code:
            code, _, fragment = code.partition("#")
            state = state or fragment
        return code, state
    if "#" in value:
        code, _, state = value.partition("#")
        return code.strip(), state.strip()
    return value, ""


# ---------------------------------------------------------------------------
# Vendor calls (synchronous: shared by the async routes via asyncio.to_thread
# and by the turn-resolution path, which already runs off the event loop)
# ---------------------------------------------------------------------------


def _post(
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        return client.post(url, json=json_body, data=form_body, headers=headers)


def _anthropic_authorize_url(challenge: str, state: str) -> str:
    params = urlencode(
        {
            "code": "true",
            "client_id": ANTHROPIC_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": ANTHROPIC_REDIRECT_URI,
            "scope": ANTHROPIC_SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{ANTHROPIC_AUTHORIZE_URL}?{params}"


def _anthropic_exchange(code: str, state: str, verifier: str) -> dict[str, Any]:
    response = _post(
        ANTHROPIC_TOKEN_URL,
        json_body={
            "grant_type": "authorization_code",
            "client_id": ANTHROPIC_CLIENT_ID,
            "code": code,
            "state": state,
            "redirect_uri": ANTHROPIC_REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers=_ANTHROPIC_TOKEN_HEADERS,
    )
    if response.status_code >= 400:
        raise SubscriptionAuthError(
            _upstream_detail(response, "Claude rejected the authorization code")
        )
    payload = response.json()
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    organization = (
        payload.get("organization")
        if isinstance(payload.get("organization"), dict)
        else {}
    )
    return {
        "tokens": _token_payload(payload),
        "account_key": str(
            account.get("uuid") or account.get("email_address") or "default"
        ),
        "label": str(account.get("email_address") or organization.get("name") or ""),
        "plan": str(organization.get("name") or ""),
    }


def _anthropic_refresh(refresh_token: str) -> dict[str, Any]:
    response = _post(
        ANTHROPIC_TOKEN_URL,
        json_body={
            "grant_type": "refresh_token",
            "client_id": ANTHROPIC_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        headers=_ANTHROPIC_TOKEN_HEADERS,
    )
    if response.status_code >= 400:
        raise SubscriptionAuthError(
            _upstream_detail(response, "Claude refused to refresh this connection")
        )
    return _token_payload(response.json(), fallback_refresh=refresh_token)


def _codex_begin_device() -> dict[str, Any]:
    response = _post(
        CODEX_DEVICE_USERCODE_URL, json_body={"client_id": CODEX_CLIENT_ID}
    )
    if response.status_code >= 400:
        raise SubscriptionAuthError(
            _upstream_detail(response, "ChatGPT would not start a sign-in")
        )
    payload = response.json()
    device_auth_id = str(payload.get("device_auth_id") or "")
    user_code = str(payload.get("user_code") or "")
    if not device_auth_id or not user_code:
        raise SubscriptionAuthError("ChatGPT returned an unusable sign-in code.")
    return {"device_auth_id": device_auth_id, "user_code": user_code}


def _codex_poll_device(device_auth_id: str, user_code: str) -> dict[str, Any] | None:
    """One poll. ``None`` while the user has not approved yet."""
    response = _post(
        CODEX_DEVICE_TOKEN_URL,
        json_body={"device_auth_id": device_auth_id, "user_code": user_code},
    )
    # The device endpoint answers 403/404 for the entire pre-approval window.
    if response.status_code in (403, 404):
        return None
    if response.status_code >= 400:
        raise SubscriptionAuthError(
            _upstream_detail(response, "ChatGPT rejected this sign-in")
        )
    payload = response.json()
    authorization_code = str(payload.get("authorization_code") or "")
    code_verifier = str(payload.get("code_verifier") or "")
    if not authorization_code or not code_verifier:
        return None
    return _codex_exchange(authorization_code, code_verifier)


def _codex_exchange(code: str, verifier: str) -> dict[str, Any]:
    response = _post(
        CODEX_TOKEN_URL,
        form_body={
            "grant_type": "authorization_code",
            "client_id": CODEX_CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": CODEX_DEVICE_REDIRECT_URI,
        },
    )
    if response.status_code >= 400:
        raise SubscriptionAuthError(
            _upstream_detail(response, "ChatGPT rejected the authorization code")
        )
    payload = response.json()
    tokens = _token_payload(payload)
    claims = _jwt_claims(tokens["access_token"])
    auth_claim = claims.get("https://api.openai.com/auth")
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
    profile_claim = claims.get("https://api.openai.com/profile")
    profile_claim = profile_claim if isinstance(profile_claim, dict) else {}
    account_key = str(auth_claim.get("chatgpt_account_id") or "")
    if not account_key:
        raise SubscriptionAuthError(
            "That ChatGPT account has no Codex access, so it cannot be connected."
        )
    return {
        "tokens": tokens,
        "account_key": account_key,
        "label": str(profile_claim.get("email") or "").strip().lower(),
        "plan": str(auth_claim.get("chatgpt_plan_type") or ""),
    }


def _codex_refresh(refresh_token: str) -> dict[str, Any]:
    response = _post(
        CODEX_TOKEN_URL,
        form_body={
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    if response.status_code >= 400:
        raise SubscriptionAuthError(
            _upstream_detail(response, "ChatGPT refused to refresh this connection")
        )
    return _token_payload(response.json(), fallback_refresh=refresh_token)


def _token_payload(
    payload: dict[str, Any], fallback_refresh: str = ""
) -> dict[str, Any]:
    access = str(payload.get("access_token") or "")
    if not access:
        raise SubscriptionAuthError("The provider returned no access token.")
    refresh = str(payload.get("refresh_token") or fallback_refresh)
    try:
        expires_in = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at_ms": int(time.time() * 1000) + expires_in * 1000,
    }


# ---------------------------------------------------------------------------
# Flow store
# ---------------------------------------------------------------------------


def _sweep_expired_flows(cur: Any) -> None:
    cur.execute("DELETE FROM provider_oauth_flows WHERE expires_at < now()")


def _create_flow(
    email: str, provider: str, scope: str, kind: str, secret: dict[str, Any]
) -> str:
    flow_id = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_FLOW_TTL_SECONDS)
    envelope = _seal(email, provider, "flow", secret)
    with tx() as cur:
        _sweep_expired_flows(cur)
        cur.execute(
            """
            INSERT INTO provider_oauth_flows (
                flow_id, email, provider, scope, kind, encrypted_secret, expires_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (flow_id, _email(email), provider, scope, kind, envelope, expires_at),
        )
    return flow_id


def _claim_flow(email: str, provider: str, flow_id: str) -> dict[str, Any]:
    """The caller's own live flow. Raises 410 once it has expired or been used."""
    with tx() as cur:
        cur.execute(
            """
            SELECT scope, kind, encrypted_secret, expires_at
              FROM provider_oauth_flows
             WHERE flow_id = %s AND email = %s AND provider = %s
             FOR UPDATE
            """,
            (flow_id, _email(email), provider),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(410, "this sign-in has expired - start it again")
        if row["expires_at"] <= datetime.now(timezone.utc):
            cur.execute(
                "DELETE FROM provider_oauth_flows WHERE flow_id = %s", (flow_id,)
            )
            raise HTTPException(410, "this sign-in has expired - start it again")
        return {
            "scope": row["scope"],
            "kind": row["kind"],
            "secret": _open(email, provider, "flow", row["encrypted_secret"]),
        }


def _drop_flow(flow_id: str) -> None:
    with tx() as cur:
        cur.execute("DELETE FROM provider_oauth_flows WHERE flow_id = %s", (flow_id,))


# ---------------------------------------------------------------------------
# Account store
# ---------------------------------------------------------------------------


def _row_metadata(row: Any) -> dict[str, Any]:
    expires_at = row["access_expires_at"]
    return {
        "provider": row["provider"],
        "account_key": row["account_key"],
        "scope": row["scope"],
        "label": row["label"],
        "plan": row["plan"],
        "created_at": _timestamp(row["created_at"]),
        "last_used_at": _timestamp(row["last_used_at"]),
        "expires_at": _timestamp(expires_at),
        "expired": bool(
            expires_at is not None and expires_at <= datetime.now(timezone.utc)
        ),
    }


def list_accounts(email: str, *, include_shared: bool) -> list[dict[str, Any]]:
    with tx() as cur:
        if include_shared:
            cur.execute(
                """
                SELECT provider, account_key, scope, label, plan, created_at,
                       last_used_at, access_expires_at
                  FROM provider_accounts
                 WHERE (email = %s AND scope = 'personal') OR scope = 'shared'
                 ORDER BY scope, provider, created_at
                """,
                (_email(email),),
            )
        else:
            cur.execute(
                """
                SELECT provider, account_key, scope, label, plan, created_at,
                       last_used_at, access_expires_at
                  FROM provider_accounts
                 WHERE email = %s AND scope = 'personal'
                 ORDER BY provider, created_at
                """,
                (_email(email),),
            )
        return [_row_metadata(row) for row in cur.fetchall()]


def _persist_account(
    email: str, provider: str, scope: str, connected: dict[str, Any]
) -> dict[str, Any]:
    account_key = connected["account_key"]
    tokens = connected["tokens"]
    envelope = _seal(email, provider, "tokens", tokens)
    expires_at = datetime.fromtimestamp(tokens["expires_at_ms"] / 1000, tz=timezone.utc)
    with tx() as cur:
        cur.execute(
            """
            INSERT INTO provider_accounts (
                email, provider, account_key, scope, encrypted_tokens,
                label, plan, access_expires_at,
                created_at, last_refreshed_at, last_used_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now(), NULL)
            ON CONFLICT (email, provider, account_key) DO UPDATE SET
                scope = EXCLUDED.scope,
                encrypted_tokens = EXCLUDED.encrypted_tokens,
                label = EXCLUDED.label,
                plan = EXCLUDED.plan,
                access_expires_at = EXCLUDED.access_expires_at,
                last_refreshed_at = now()
            RETURNING provider, account_key, scope, label, plan, created_at,
                      last_used_at, access_expires_at
            """,
            (
                _email(email),
                provider,
                account_key,
                scope,
                envelope,
                connected["label"],
                connected["plan"],
                expires_at,
            ),
        )
        return _row_metadata(cur.fetchone())


def delete_account(
    email: str, provider: str, account_key: str, *, is_admin: bool
) -> bool:
    """Unlink. A non-admin can only ever remove their own personal link."""
    with tx() as cur:
        if is_admin:
            cur.execute(
                """
                DELETE FROM provider_accounts
                 WHERE provider = %s AND account_key = %s
                   AND (scope = 'shared' OR email = %s)
                """,
                (provider, account_key, _email(email)),
            )
        else:
            cur.execute(
                """
                DELETE FROM provider_accounts
                 WHERE email = %s AND provider = %s AND account_key = %s
                   AND scope = 'personal'
                """,
                (_email(email), provider, account_key),
            )
        return cur.rowcount >= 1


# ---------------------------------------------------------------------------
# Turn-time resolution
# ---------------------------------------------------------------------------


def _refresh_for(provider: str, refresh_token: str) -> dict[str, Any]:
    if provider == "anthropic-sub":
        return _anthropic_refresh(refresh_token)
    return _codex_refresh(refresh_token)


def _usable_access_token(row: Any, owner_email: str) -> str | None:
    """Row's access token, refreshed under its own lock when it is stale.

    Anthropic rotates the refresh token on every exchange, so two workers
    refreshing the same row concurrently can revoke a live grant. The row lock
    plus the post-lock re-check means the loser of the race adopts the winner's
    tokens instead of spending the rotated one a second time.
    """
    provider = row["provider"]
    account_key = row["account_key"]
    tokens = _open(owner_email, provider, "tokens", row["encrypted_tokens"])
    now_ms = int(time.time() * 1000)
    if tokens.get("expires_at_ms", 0) - _REFRESH_SKEW_MS > now_ms:
        return str(tokens.get("access_token") or "") or None

    with tx() as cur:
        cur.execute(
            """
            SELECT encrypted_tokens FROM provider_accounts
             WHERE email = %s AND provider = %s AND account_key = %s
             FOR UPDATE
            """,
            (owner_email, provider, account_key),
        )
        locked = cur.fetchone()
        if locked is None:
            return None
        current = _open(owner_email, provider, "tokens", locked["encrypted_tokens"])
        if current.get("expires_at_ms", 0) - _REFRESH_SKEW_MS > int(time.time() * 1000):
            # Another worker refreshed while we waited for the lock.
            return str(current.get("access_token") or "") or None
        refresh_token = str(current.get("refresh_token") or "")
        if not refresh_token:
            return None
        try:
            fresh = _refresh_for(provider, refresh_token)
        except SubscriptionAuthError as exc:
            log.warning(
                "subscription refresh failed provider=%s account=%s: %s",
                provider,
                account_key[:8],
                exc,
            )
            return None
        cur.execute(
            """
            UPDATE provider_accounts
               SET encrypted_tokens = %s, access_expires_at = %s, last_refreshed_at = now()
             WHERE email = %s AND provider = %s AND account_key = %s
            """,
            (
                _seal(owner_email, provider, "tokens", fresh),
                datetime.fromtimestamp(fresh["expires_at_ms"] / 1000, tz=timezone.utc),
                owner_email,
                provider,
                account_key,
            ),
        )
        return str(fresh.get("access_token") or "") or None


def resolve_subscription_token(
    email: str, wire_provider: str, *, trial_provider_override: bool = False
) -> dict[str, Any] | None:
    """A usable own Claude subscription, or an active admin's Claude grant for
    a verified trial-domain account. Own personal links win; sponsored grants
    rotate least-recently-used across active admin owners and bypass pool billing.
    For browser turns naming a non-Claude provider, the gateway may request a
    trial-only Anthropic override: validate eligibility before looking up links.
    Codex OAuth cannot run in an isolated web worker.
    """
    provider = _WIRE_TO_SUBSCRIPTION.get(str(wire_provider or "").strip().lower())
    if provider != "anthropic-sub":
        return None
    normalized_email = _email(email)
    eligible = is_trial_domain(normalized_email) if trial_provider_override else None
    if trial_provider_override and not eligible:
        return None
    with tx() as cur:
        cur.execute(
            """
            SELECT email, provider, account_key, scope, encrypted_tokens
              FROM provider_accounts
             WHERE provider = %s
               AND email = %s AND scope = 'personal'
             ORDER BY last_used_at NULLS FIRST
            """,
            (provider, normalized_email),
        )
        candidates = cur.fetchall()

    def usable(rows: Any) -> dict[str, Any] | None:
        for row in rows:
            owner_email = row["email"]
            try:
                access = _usable_access_token(row, owner_email)
            except RuntimeError:
                # An unreadable envelope cannot silently become ambient auth.
                log.warning(
                    "subscription envelope unreadable provider=%s scope=%s",
                    row["provider"],
                    row["scope"],
                )
                continue
            if not access:
                continue
            with tx() as cur:
                cur.execute(
                    """
                    UPDATE provider_accounts SET last_used_at = now()
                     WHERE email = %s AND provider = %s AND account_key = %s
                    """,
                    (owner_email, row["provider"], row["account_key"]),
                )
            return {
                "provider": row["provider"],
                "account_key": row["account_key"],
                "scope": row["scope"],
                "access_token": access,
                "sponsored": owner_email != normalized_email,
                "owner_email": owner_email,
            }
        return None

    own = usable(candidates)
    if own is not None:
        return own
    if eligible is None and not is_trial_domain(normalized_email):
        return None
    with tx() as cur:
        cur.execute(
            """
            SELECT pa.email, pa.provider, pa.account_key, pa.scope, pa.encrypted_tokens
              FROM provider_accounts pa JOIN accounts a ON a.email = pa.email
             WHERE pa.provider = 'anthropic-sub'
               AND pa.scope IN ('personal', 'shared')
               AND a.role = 'admin' AND a.status = 'active'
             ORDER BY pa.last_used_at NULLS FIRST, pa.email, pa.account_key
            """
        )
        sponsors = cur.fetchall()
    return usable(sponsors)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _is_admin(user: Any) -> bool:
    return getattr(user, "role", "") == "admin"


def _provider_catalogue(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    connected = {row["provider"] for row in accounts}
    return [
        {
            "provider": spec.provider,
            "label": spec.label,
            "kind": spec.kind,
            "connected": spec.provider in connected,
        }
        for spec in SUBSCRIPTION_PROVIDERS.values()
    ]


@router.get("/accounts")
async def api_list_accounts(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    admin = _is_admin(user)
    accounts = list_accounts(user.email, include_shared=admin)
    return {
        "providers": _provider_catalogue(accounts),
        "accounts": accounts,
        "can_manage_shared": admin,
    }


@router.post("/accounts/{provider}/begin")
async def api_begin(
    provider: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    user = get_current_user(request)
    normalized = _provider(provider)
    scope = _scope((body or {}).get("scope"))
    if scope == SHARED and not _is_admin(user):
        raise HTTPException(403, "only an administrator can connect a shared account")
    _storage_available()
    provider_oauth_starts.enforce(user.email, "subscription sign-ins")

    spec = SUBSCRIPTION_PROVIDERS[normalized]
    if spec.kind == "pkce_paste":
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        flow_id = _create_flow(
            user.email,
            normalized,
            scope,
            spec.kind,
            {"verifier": verifier, "state": state},
        )
        return {
            "flow_id": flow_id,
            "kind": spec.kind,
            "expires_in": _FLOW_TTL_SECONDS,
            "authorize_url": _anthropic_authorize_url(challenge, state),
            "instructions": ANTHROPIC_INSTRUCTIONS,
        }

    try:
        device = await _run(_codex_begin_device)
    except SubscriptionAuthError as exc:
        raise HTTPException(502, str(exc)) from None
    flow_id = _create_flow(user.email, normalized, scope, spec.kind, device)
    return {
        "flow_id": flow_id,
        "kind": spec.kind,
        "expires_in": _FLOW_TTL_SECONDS,
        "verification_url": CODEX_DEVICE_VERIFICATION_URL,
        "user_code": device["user_code"],
        "instructions": CODEX_INSTRUCTIONS,
    }


@router.post("/accounts/{provider}/complete")
async def api_complete(
    provider: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    user = get_current_user(request)
    normalized = _provider(provider)
    payload = body or {}
    flow_id = str(payload.get("flow_id") or "").strip()
    if not flow_id:
        raise HTTPException(400, "flow_id is required")
    _storage_available()
    provider_oauth_completions.enforce(user.email, "subscription sign-in checks")

    flow = _claim_flow(user.email, normalized, flow_id)
    scope = flow["scope"]
    if scope == SHARED and not _is_admin(user):
        raise HTTPException(403, "only an administrator can connect a shared account")
    secret = flow["secret"]

    try:
        if flow["kind"] == "pkce_paste":
            raw_code = payload.get("code")
            if not isinstance(raw_code, str) or not raw_code.strip():
                raise SubscriptionAuthError("Paste the address you were redirected to.")
            if len(raw_code.encode("utf-8")) > _MAX_CODE_BYTES:
                raise SubscriptionAuthError(
                    "That value is too long to be a redirect address."
                )
            code, pasted_state = parse_pasted_code(raw_code)
            state = pasted_state or secret["state"]
            if pasted_state and pasted_state != secret["state"]:
                raise SubscriptionAuthError(
                    "That address belongs to a different sign-in. Start again."
                )
            connected = await _run(_anthropic_exchange, code, state, secret["verifier"])
        else:
            connected = await _run(
                _codex_poll_device, secret["device_auth_id"], secret["user_code"]
            )
            if connected is None:
                return {
                    "status": "pending",
                    "detail": "Waiting for you to approve the code.",
                }
    except SubscriptionAuthError as exc:
        # A failed paste is recoverable — the operator can paste again — but a
        # spent authorization code is not, so the flow dies either way and the
        # UI tells them to restart.
        _drop_flow(flow_id)
        raise HTTPException(400, str(exc)) from None

    account = _persist_account(user.email, normalized, scope, connected)
    _drop_flow(flow_id)
    return {"status": "connected", "account": account}


@router.delete("/accounts/{provider}/{account_key}")
async def api_delete_account(
    provider: str, account_key: str, request: Request
) -> dict[str, bool]:
    user = get_current_user(request)
    normalized = _provider(provider)
    if not delete_account(
        user.email, normalized, account_key, is_admin=_is_admin(user)
    ):
        raise HTTPException(404, "subscription account not found")
    return {"ok": True}


async def _run(fn: Any, *args: Any) -> Any:
    """Run one blocking vendor call without stalling the event loop."""
    return await deployment_drain.run_blocking(fn, *args)


__all__ = [
    "SUBSCRIPTION_PROVIDERS",
    "SubscriptionAuthError",
    "delete_account",
    "list_accounts",
    "parse_pasted_code",
    "resolve_subscription_token",
    "router",
]
