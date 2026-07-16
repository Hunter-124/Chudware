"""Cursor Pro subscription provider — auth only.

This module implements the full Cursor authentication flow (login via browser poll,
token refresh, credential storage). The chat transport is NOT implemented: Cursor's
agent backend uses a Connect-RPC/protobuf protocol over HTTP/2, not a REST API.
See porting-docs/03-cursor.md §4 for the porting plan.

complete()/complete_ex() raise NotImplementedError with a clear explanation so the
provider can be registered and the auth flow tested without the protobuf client.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlencode
from urllib.request import urlopen
import urllib.error

from .base import Provider
from . import sub_auth


LOGIN_URL = "https://cursor.com/loginDeepControl"
POLL_URL = "https://api2.cursor.sh/auth/poll"
REFRESH_URL = "https://api2.cursor.sh/auth/exchange_user_api_key"
CURSOR_API_URL = "https://api2.cursor.sh"
CURSOR_CLIENT_VERSION = "cli-2026.01.09-231024f"
PROVIDER_ID = "cursor"
DEFAULT_MODEL = "cursor-default"
POLL_MAX_ATTEMPTS = 150
POLL_BASE_DELAY = 1.0
POLL_MAX_DELAY = 10.0
POLL_BACKOFF = 1.2
REFRESH_SKEW_MS = 5 * 60 * 1000


class CursorSubProvider(Provider):
    """Authenticates via the user's Cursor Pro subscription (poll-based, no callback server)."""

    name = "cursor-sub"
    supports_vision = False

    def __init__(self, model: str = "cursor-default") -> None:
        self.model = model
        self._cred = sub_auth.load_credential(PROVIDER_ID)

    def login(self) -> dict:
        """Open the Cursor login page and poll until the user authorizes."""
        verifier = sub_auth.make_verifier()
        challenge = sub_auth.challenge_for(verifier)
        uuid_ = sub_auth.random_uuid()

        params = urlencode({
            "challenge": challenge,
            "uuid": uuid_,
            "mode": "login",
            "redirectTarget": "cli",
        })
        url = f"{LOGIN_URL}?{params}"

        import webbrowser
        webbrowser.open(url)

        data = self._poll(uuid_, verifier)
        return self._store_tokens(data)

    def _poll(self, uuid_: str, verifier: str) -> dict:
        """Poll the Cursor auth endpoint until the user completes the browser login."""
        delay = POLL_BASE_DELAY
        consecutive_errors = 0

        for _ in range(POLL_MAX_ATTEMPTS):
            params = urlencode({"uuid": uuid_, "verifier": verifier})
            req_url = f"{POLL_URL}?{params}"
            try:
                with urlopen(req_url, timeout=30.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("accessToken"):
                    return data
                # 200 but no token yet — keep waiting
                consecutive_errors = 0
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    # Not yet authorized — expected during the polling window
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        raise
            except Exception:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise

            time.sleep(delay)
            delay = min(delay * POLL_BACKOFF, POLL_MAX_DELAY)

        raise RuntimeError(
            "Cursor login timed out — did you complete the browser login?"
        )

    def _store_tokens(self, data: dict) -> dict:
        access = data["accessToken"]
        refresh = data.get("refreshToken")

        claims = sub_auth.decode_jwt_payload(access)
        exp = claims.get("exp")
        if exp:
            expires_at_ms = int(exp) * 1000 - REFRESH_SKEW_MS
        else:
            expires_at_ms = int(time.time() * 1000) + 3600 * 1000

        # Prefer a real identity key over derive_account_id's acct-<timestamp>.
        account_id = None
        for key in ("sub", "user_id", "userId", "id"):
            v = claims.get(key)
            if v is not None and str(v).strip():
                account_id = str(v).strip()
                break
        email = None
        for key in ("email", "preferred_username", "unique_name", "upn"):
            v = claims.get(key)
            if isinstance(v, str) and "@" in v:
                email = v.strip().lower()
                break

        cred = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at_ms": expires_at_ms,
            "account_id": account_id,
            "email": email,
        }
        # Preserve identity + last_usage across refresh when the poll payload omits it.
        if self._cred:
            for k in (
                "email", "display_name", "plan", "account_id",
                "last_usage", "last_usage_fetched_at",
            ):
                if self._cred.get(k) is not None and not cred.get(k):
                    cred[k] = self._cred[k]
        # Best-effort stripe profile for membershipType / email / display_name.
        try:
            from . import usage as usage_mod
            cred = usage_mod.enrich_cursor_profile(cred)
        except Exception:
            # Offline-safe friendly label so list_accounts never shows bare acct-…
            if not cred.get("display_name"):
                if cred.get("email"):
                    cred["display_name"] = cred["email"]
                elif cred.get("plan"):
                    cred["display_name"] = f"Cursor ({cred['plan']})"
                else:
                    cred["display_name"] = "Cursor"
        # If enrich still left account_id empty, prefer email slug over timestamp.
        if not cred.get("account_id") and cred.get("email"):
            cred["account_id"] = sub_auth._slug_account_id(cred["email"])
        sub_auth.save_credential(PROVIDER_ID, cred)
        self._cred = cred
        return cred

    def refresh(self) -> dict:
        """Exchange the refresh token (or access token) for a fresh access token."""
        bearer = self._cred["refresh_token"] or self._cred["access_token"]
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        }
        resp = sub_auth.post_json(REFRESH_URL, {}, headers=headers)

        # Preserve old refresh token if the response omits one
        merged = dict(resp)
        if not merged.get("refreshToken") and self._cred.get("refresh_token"):
            merged["refreshToken"] = self._cred["refresh_token"]

        return self._store_tokens(merged)

    def _ensure_auth(self) -> None:
        if not self._cred:
            raise RuntimeError(
                "Not logged in to Cursor. Run login() first (Settings → Connect Cursor)."
            )
        if sub_auth.is_expired(self._cred, skew_ms=REFRESH_SKEW_MS):
            self.refresh()

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        self._ensure_auth()
        raise NotImplementedError(
            "Cursor auth is wired, but the chat transport is not yet implemented. Cursor's agent "
            "backend is a Connect-RPC/protobuf protocol over HTTP/2 (not REST) — see "
            "porting-docs/03-cursor.md §4. Auth (login/refresh/token storage) works and is testable; "
            "the protobuf client is a documented follow-up."
        )

    def complete_ex(self, system: str, messages: list[dict],
                    max_tokens: int = 8000) -> tuple[str, str]:
        return self.complete(system, messages, max_tokens=max_tokens), ""
