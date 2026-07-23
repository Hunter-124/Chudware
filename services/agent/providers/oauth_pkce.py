"""OpenRouter PKCE session login — the default third-party session auth.

Contract: contracts/providers.md §4.1. "Sign in with OpenRouter" is officially documented
for third-party apps: one login mints a user-scoped API key, billed against the user's
OpenRouter balance across hundreds of models. Subscription-auth providers (§4.4) are a
separate, owner-authorized path for using paid Pro/Max/Plus accounts directly.

Flow (S256 PKCE):
  1. begin_login()  -> {auth_url, code_verifier}; open auth_url in a browser.
  2. user authorizes; OpenRouter redirects to callback_url with ?code=<code>.
  3. complete_login(code, code_verifier) -> {ok, key}; store `key` like any api_key.

Feature-flagged: inert unless config `openrouter_pkce: true` or env
CHUDWARE_ENABLE_OPENROUTER_PKCE=1. The token exchange is transport-injectable, so CI drives
the whole flow with a fake transport and NO network / NO secrets. Every failure path returns
a contract-shaped {"ok": false, "reason", "hint"} — never a traceback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.request
from urllib.parse import urlencode

AUTH_URL = "https://openrouter.ai/auth"
KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
FLAG_ENV = "CHUDWARE_ENABLE_OPENROUTER_PKCE"
FLAG_CFG = "openrouter_pkce"


def pkce_enabled(cfg: dict | None = None) -> bool:
    """True if the OpenRouter PKCE login is enabled (feature flag)."""
    if os.environ.get(FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool((cfg or {}).get(FLAG_CFG, False))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_verifier() -> str:
    """A high-entropy PKCE code_verifier (43–128 url-safe chars, RFC 7636)."""
    return _b64url(secrets.token_bytes(64))


def challenge_for(verifier: str) -> str:
    """S256 code_challenge = BASE64URL(SHA256(verifier))."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _disabled() -> dict:
    return {
        "ok": False,
        "reason": "OpenRouter PKCE login is disabled",
        "hint": (f"Enable it with config '{FLAG_CFG}': true or env {FLAG_ENV}=1. "
                 "For subscription-auth (Claude/Codex/Cursor), see §4.4 providers."),
    }


def begin_login(callback_url: str, cfg: dict | None = None) -> dict:
    """Start a PKCE login. Returns {ok, auth_url, code_verifier} or a disabled/error dict.

    Open ``auth_url`` in the user's browser; when they authorize, OpenRouter redirects to
    ``callback_url`` with a ``code`` query param — pass that + ``code_verifier`` to
    :func:`complete_login`. ``code_verifier`` MUST be kept private and never logged.
    """
    if not pkce_enabled(cfg):
        return _disabled()
    verifier = make_verifier()
    params = {
        "callback_url": callback_url,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
    }
    return {"ok": True, "auth_url": f"{AUTH_URL}?{urlencode(params)}", "code_verifier": verifier}


def _default_transport(url: str, payload: dict, timeout: float) -> dict:
    """POST JSON *payload* to *url*, return the parsed JSON response. Real network path."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https URL)
        return json.loads(resp.read().decode("utf-8"))


def complete_login(code: str, code_verifier: str, cfg: dict | None = None,
                   transport=None, timeout: float = 30.0) -> dict:
    """Exchange an auth *code* + *code_verifier* for an OpenRouter API key.

    Returns {"ok": True, "key": "sk-or-..."} on success, else a contract-shaped
    {"ok": False, "reason", "hint"}. *transport* is an injectable
    ``(url, payload_dict, timeout) -> response_dict`` used by tests to avoid the network.
    """
    if not pkce_enabled(cfg):
        return _disabled()
    if not code or not code_verifier:
        return {"ok": False, "reason": "missing auth code or code_verifier",
                "hint": "Call begin_login() first, then pass the callback ?code= and the "
                        "returned code_verifier to complete_login()."}
    send = transport or _default_transport
    payload = {"code": code, "code_verifier": code_verifier, "code_challenge_method": "S256"}
    try:
        data = send(KEYS_URL, payload, timeout)
    except urllib.error.HTTPError as e:  # pragma: no cover - network path
        detail = e.read().decode("utf-8", "replace")[:400]
        return {"ok": False, "reason": f"OpenRouter key exchange failed (HTTP {e.code})",
                "hint": f"Endpoint detail: {detail}"}
    except Exception as e:  # noqa: BLE001  # pragma: no cover - network path
        return {"ok": False, "reason": f"OpenRouter key exchange failed: {e}",
                "hint": "Check network connectivity, then retry the login."}
    key = (data or {}).get("key")
    if not key:
        return {"ok": False, "reason": "OpenRouter returned no key",
                "hint": f"Unexpected response: {json.dumps(data)[:300]}"}
    return {"ok": True, "key": key}
