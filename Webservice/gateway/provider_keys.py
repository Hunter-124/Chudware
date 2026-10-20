"""Account-scoped, encrypted provider credentials and portal API.

A browser may submit a key only to the authenticated validation endpoint. The
key is accepted by the provider before it is encrypted, and every read/delete
query is scoped by the authenticated account email. Secret material is never
returned or logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException, Request

try:
    from .auth import get_current_user
    from .config import settings
    from .db import tx
    from .deployment_config import ConfigurationError, PROVIDER_KEY, decode_encryption_key
    from .limits import RateLimiter
except ImportError:  # uvicorn app:app from the package directory
    from auth import get_current_user
    from config import settings
    from db import tx
    from deployment_config import ConfigurationError, PROVIDER_KEY, decode_encryption_key
    from limits import RateLimiter

router = APIRouter(prefix="/api/providers", tags=["provider-keys"])

# One validation request performs third-party I/O. This deliberately has a
# tighter independent budget than ordinary portal writes.
provider_key_validations = RateLimiter(5, 60.0)

_ENVELOPE_VERSION = 1
_NONCE_BYTES = 12
_MAX_KEY_BYTES = 8192
_MAX_PROVIDER_ERROR_CHARS = 2048


@dataclass(frozen=True)
class _ValidationSpec:
    url: str
    header: str
    prefix: str = ""


# Fixed HTTPS origins only. The browser cannot choose a base URL, which keeps a
# submitted credential away from attacker-controlled endpoints.
VALIDATION_SPECS: dict[str, _ValidationSpec] = {
    "anthropic": _ValidationSpec("https://api.anthropic.com/v1/models", "x-api-key"),
    "gemini": _ValidationSpec(
        "https://generativelanguage.googleapis.com/v1beta/models", "x-goog-api-key"
    ),
    "moonshot": _ValidationSpec(
        "https://api.moonshot.ai/v1/models", "Authorization", "Bearer "
    ),
    "openai": _ValidationSpec(
        "https://api.openai.com/v1/models", "Authorization", "Bearer "
    ),
    "openrouter": _ValidationSpec(
        "https://openrouter.ai/api/v1/models", "Authorization", "Bearer "
    ),
    "xai": _ValidationSpec("https://api.x.ai/v1/models", "Authorization", "Bearer "),
}


def _email(value: Any) -> str:
    return str(value or "").strip().lower()


def _provider(value: Any) -> str:
    provider = str(value or "").strip().lower()
    if provider not in VALIDATION_SPECS:
        raise HTTPException(400, "unsupported provider")
    return provider


def _key_bytes() -> bytes:
    encoded = settings.provider_key_encryption_key.strip()
    if not encoded:
        raise RuntimeError(
            "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY is required for web BYOK"
        )
    try:
        return decode_encryption_key(encoded, PROVIDER_KEY)
    except ConfigurationError as exc:
        raise RuntimeError(str(exc)) from None


def _aad(email: str, provider: str) -> bytes:
    return b"chudware-provider-key-v1\0" + email.encode() + b"\0" + provider.encode()


def encrypt_key(email: str, provider: str, key: str) -> bytes:
    """Return a versioned AES-256-GCM envelope bound to account and provider."""
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(_key_bytes()).encrypt(
        nonce, key.encode("utf-8"), _aad(_email(email), provider)
    )
    return bytes((_ENVELOPE_VERSION,)) + nonce + ciphertext


def decrypt_key(email: str, provider: str, envelope: bytes) -> str:
    """Decrypt one account-bound envelope without exposing secret material in errors."""
    raw = bytes(envelope)
    if len(raw) <= 1 + _NONCE_BYTES or raw[0] != _ENVELOPE_VERSION:
        raise RuntimeError("provider credential envelope is invalid")
    nonce = raw[1 : 1 + _NONCE_BYTES]
    ciphertext = raw[1 + _NONCE_BYTES :]
    try:
        plaintext = AESGCM(_key_bytes()).decrypt(
            nonce, ciphertext, _aad(_email(email), provider)
        )
        return plaintext.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise RuntimeError("provider credential cannot be decrypted") from exc


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _metadata(row: Any) -> dict[str, Any]:
    return {
        "provider": str(row["provider"]),
        "state": str(row["validation_state"]),
        "hint": str(row["key_hint"]),
        "last_validated_at": _timestamp(row["last_validated_at"]),
        "last_used_at": _timestamp(row["last_used_at"]),
    }


def list_keys(email: str) -> list[dict[str, Any]]:
    with tx() as cur:
        cur.execute(
            """
            SELECT provider, key_hint, validation_state,
                   last_validated_at, last_used_at
              FROM provider_keys
             WHERE email = %s
             ORDER BY provider
            """,
            (_email(email),),
        )
        return [_metadata(row) for row in cur.fetchall()]


def has_validated_key(email: str, provider: str) -> bool:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in VALIDATION_SPECS:
        return False
    with tx() as cur:
        cur.execute(
            """
            SELECT 1 FROM provider_keys
             WHERE email = %s AND provider = %s
               AND validation_state = 'validated'
            """,
            (_email(email), normalized_provider),
        )
        return cur.fetchone() is not None


def read_validated_key(email: str, provider: str) -> str | None:
    """Read the current validated key without locking or writing last-used metadata."""
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in VALIDATION_SPECS:
        return None
    normalized_email = _email(email)
    with tx() as cur:
        cur.execute(
            "SELECT encrypted_key FROM provider_keys WHERE email=%s AND provider=%s "
            "AND validation_state='validated'", (normalized_email, normalized_provider),
        )
        row = cur.fetchone()
    return decrypt_key(normalized_email, normalized_provider, row["encrypted_key"]) if row else None


def resolve_validated_key(email: str, provider: str) -> str | None:
    """Resolve only this account's validated key and record successful use."""
    normalized_email = _email(email)
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in VALIDATION_SPECS:
        return None
    with tx() as cur:
        cur.execute(
            """
            SELECT encrypted_key FROM provider_keys
             WHERE email = %s AND provider = %s
               AND validation_state = 'validated'
             FOR UPDATE
            """,
            (normalized_email, normalized_provider),
        )
        row = cur.fetchone()
        if row is None:
            return None
        key = decrypt_key(normalized_email, normalized_provider, row["encrypted_key"])
        cur.execute(
            """
            UPDATE provider_keys SET last_used_at = now()
             WHERE email = %s AND provider = %s
            """,
            (normalized_email, normalized_provider),
        )
    return key


def delete_key(email: str, provider: str) -> bool:
    with tx() as cur:
        cur.execute(
            "DELETE FROM provider_keys WHERE email = %s AND provider = %s",
            (_email(email), provider),
        )
        return cur.rowcount == 1


def _safe_provider_error(provider: str, key: str, response: httpx.Response) -> str:
    detail = response.text.strip().replace(key, "[redacted]")
    if len(detail) > _MAX_PROVIDER_ERROR_CHARS:
        detail = detail[:_MAX_PROVIDER_ERROR_CHARS] + "…"
    base = f"{provider} API key validation failed ({response.status_code})"
    return f"{base}: {detail}" if detail else base


async def validate_key(provider: str, key: str) -> None:
    """Ask the provider to accept the key; raise with its real rejection detail."""
    spec = VALIDATION_SPECS[provider]
    headers = {spec.header: spec.prefix + key, "Accept": "application/json"}
    if provider == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.get(spec.url, headers=headers)
    except httpx.HTTPError as exc:
        # URLs are fixed and contain no key; still scrub defensively in case a
        # transport implementation includes request headers in its text.
        detail = str(exc).replace(key, "[redacted]")
        raise HTTPException(502, f"{provider} validation request failed: {detail}") from None
    if not response.is_success:
        raise HTTPException(400, _safe_provider_error(provider, key, response))


def _persist_validated_key(email: str, provider: str, key: str) -> dict[str, Any]:
    encrypted = encrypt_key(email, provider, key)
    hint = key[-4:]
    with tx() as cur:
        cur.execute(
            """
            INSERT INTO provider_keys (
                email, provider, encrypted_key, key_hint, validation_state,
                created_at, last_validated_at, last_used_at
            ) VALUES (%s, %s, %s, %s, 'validated', now(), now(), NULL)
            ON CONFLICT (email, provider) DO UPDATE SET
                encrypted_key = EXCLUDED.encrypted_key,
                key_hint = EXCLUDED.key_hint,
                validation_state = 'validated',
                last_validated_at = now(),
                last_used_at = NULL
            RETURNING provider, key_hint, validation_state,
                      last_validated_at, last_used_at
            """,
            (_email(email), provider, encrypted, hint),
        )
        return _metadata(cur.fetchone())


@router.get("/keys")
async def api_list_keys(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    return {"keys": list_keys(user.email)}


@router.post("/keys")
async def api_add_key(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    user = get_current_user(request)
    provider = _provider(body.get("provider"))
    key_value = body.get("key")
    if not isinstance(key_value, str) or not key_value.strip():
        raise HTTPException(400, "provider key is required")
    key = key_value.strip()
    if len(key.encode("utf-8")) > _MAX_KEY_BYTES:
        raise HTTPException(400, "provider key is too long")
    try:
        _key_bytes()
    except RuntimeError:
        # Fail before spending a third-party validation request, and do not
        # disclose secret configuration details to the account.
        raise HTTPException(503, "provider key storage is unavailable") from None
    provider_key_validations.enforce(user.email, "provider key validations")
    await validate_key(provider, key)
    return _persist_validated_key(user.email, provider, key)


@router.delete("/keys/{provider}")
async def api_delete_key(provider: str, request: Request) -> dict[str, bool]:
    user = get_current_user(request)
    normalized_provider = _provider(provider)
    if not delete_key(user.email, normalized_provider):
        raise HTTPException(404, "provider key not found")
    return {"ok": True}


__all__ = [
    "VALIDATION_SPECS",
    "decrypt_key",
    "encrypt_key",
    "has_validated_key",
    "list_keys",
    "resolve_validated_key",
    "router",
]
