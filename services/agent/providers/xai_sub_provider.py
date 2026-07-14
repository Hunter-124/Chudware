"""xAI Grok subscription provider (SuperGrok / X Premium+ OAuth).

Authenticates via the OAuth 2.0 device-code flow against accounts.x.ai / auth.x.ai
— the same flow Hermes Agent uses for SuperGrok. No metered API key required when
the subscription tier is API-enabled. Tokens are stored in config.json and
refreshed automatically before they expire.

Source constants (client id, scopes, endpoints) come from Hermes Agent:
  https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/auth.py
  https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth

Honest 403 surface: xAI has gated OAuth API access to SuperGrok Heavy for some
tiers (hermes-agent issue #26847). On HTTP 403 after a successful login we raise a
contract-shaped RuntimeError pointing at console.x.ai — never a traceback.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import webbrowser

from .base import Provider
from . import sub_auth

# ── Endpoints / client (source: hermes-agent hermes_cli/auth.py) ─────────────
# https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/auth.py
# https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
# Fallback token endpoint when OIDC discovery is unavailable (same issuer path
# hermes discovers at runtime). Prefer discovery when online.
XAI_OAUTH_TOKEN_URL_FALLBACK = f"{XAI_OAUTH_ISSUER}/oauth2/token"

API_BASE_URL = "https://api.x.ai/v1"
CHAT_COMPLETIONS_URL = f"{API_BASE_URL}/chat/completions"
MODELS_URL = f"{API_BASE_URL}/models"
PROVIDER_ID = "xai-sub"
DEFAULT_MODEL = "grok-4"
# Access tokens are short-lived (~6h); refresh up to 1h early like hermes
# (XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 3600).
REFRESH_SKEW_MS = 60 * 60 * 1000

# Contract-shaped message for the Heavy-only OAuth API gate (issue #26847).
TIER_DENIED_REASON = (
    "xAI subscription tier not API-enabled (Heavy-only gating as of 2026-05)"
)
TIER_DENIED_HINT = (
    "Use an API key from console.x.ai and switch provider to 'xai', "
    "or upgrade the SuperGrok tier that includes OAuth API access."
)


class XaiTierDeniedError(RuntimeError):
    """HTTP 403 from xAI OAuth/API — subscription tier not API-enabled."""

    def __init__(self, detail: str = "") -> None:
        msg = TIER_DENIED_REASON
        if detail:
            msg = f"{TIER_DENIED_REASON}: {detail}"
        msg = f"{msg}. Hint: {TIER_DENIED_HINT}"
        super().__init__(msg)
        self.reason = TIER_DENIED_REASON
        self.hint = TIER_DENIED_HINT


class XAISubProvider(Provider):
    """Chat completion via xAI SuperGrok / X Premium+ subscription OAuth token."""

    name = "xai-sub"
    # Grok 4.x is multimodal (image input).
    supports_vision = True

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._cred: dict | None = sub_auth.load_credential(PROVIDER_ID)
        self.base_url = API_BASE_URL

    # ── Auth flows ───────────────────────────────────────────────────────────

    def login(self) -> dict:
        """Device-code login (primary xAI path). Opens the verification URL."""
        return self.login_device(open_browser=True)

    def login_device(self, open_browser: bool = True) -> dict:
        """OAuth 2.0 device-code flow against auth.x.ai.

        Prints the verification URL + user code, optionally opens a browser,
        then polls the token endpoint until approved or timeout.
        """
        token_endpoint = self._discover_token_endpoint()
        device = self._request_device_code()
        verification_url = (
            device.get("verification_uri_complete")
            or device.get("verification_uri")
            or "https://accounts.x.ai/device"
        )
        user_code = str(device.get("user_code") or "")
        expires_in = int(device.get("expires_in") or 900)
        interval = max(1, int(device.get("interval") or 5))
        device_code = str(device["device_code"])

        print(f"Go to {verification_url} and approve access"
              + (f" (code: {user_code})" if user_code else ""))
        if open_browser:
            try:
                webbrowser.open(str(verification_url))
            except Exception:  # noqa: BLE001 — headless / no display is fine
                pass

        payload = self._poll_device_token(
            token_endpoint=token_endpoint,
            device_code=device_code,
            expires_in=expires_in,
            poll_interval=interval,
        )
        return self._store_tokens(payload, token_endpoint=token_endpoint)

    def _discover_token_endpoint(self) -> str:
        """OIDC discovery for the token endpoint; fall back to known path."""
        try:
            data = sub_auth.get_json(
                XAI_OAUTH_DISCOVERY_URL,
                headers={"Accept": "application/json"},
                timeout=15.0,
            )
            endpoint = str(data.get("token_endpoint") or "").strip()
            if endpoint.startswith("https://") and "x.ai" in endpoint:
                return endpoint
        except Exception:  # noqa: BLE001 — offline / discovery down → fallback
            pass
        return XAI_OAUTH_TOKEN_URL_FALLBACK

    def _request_device_code(self) -> dict:
        try:
            return sub_auth.post_form(
                XAI_OAUTH_DEVICE_CODE_URL,
                {
                    "client_id": XAI_OAUTH_CLIENT_ID,
                    "scope": XAI_OAUTH_SCOPE,
                },
                headers={"Accept": "application/json"},
                timeout=30.0,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 403:
                raise XaiTierDeniedError(body) from exc
            raise RuntimeError(
                f"xAI device-code request failed (HTTP {exc.code}): {body}"
            ) from exc

    def _poll_device_token(
        self,
        *,
        token_endpoint: str,
        device_code: str,
        expires_in: int,
        poll_interval: int,
    ) -> dict:
        deadline = time.monotonic() + max(1, expires_in)
        current_interval = max(1, poll_interval)
        while time.monotonic() < deadline:
            try:
                resp = sub_auth.post_form(
                    token_endpoint,
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "client_id": XAI_OAUTH_CLIENT_ID,
                        "device_code": device_code,
                    },
                    headers={"Accept": "application/json"},
                    timeout=30.0,
                )
                if not resp.get("access_token"):
                    raise RuntimeError(
                        "xAI device-code token response missing access_token"
                    )
                if not resp.get("refresh_token"):
                    raise RuntimeError(
                        "xAI device-code token response missing refresh_token"
                    )
                return resp
            except urllib.error.HTTPError as exc:
                try:
                    err = json.loads(exc.read().decode("utf-8", errors="replace"))
                except Exception:  # noqa: BLE001
                    err = {}
                code = str(err.get("error") or "")
                if code == "authorization_pending":
                    time.sleep(current_interval)
                    continue
                if code == "slow_down":
                    current_interval = min(current_interval + 1, 30)
                    time.sleep(current_interval)
                    continue
                if exc.code == 403:
                    detail = err.get("error_description") or err.get("error") or ""
                    raise XaiTierDeniedError(str(detail)) from exc
                detail = err.get("error_description") or err.get("error") or exc.reason
                raise RuntimeError(
                    f"xAI device-code token polling failed: {detail}"
                ) from exc
            time.sleep(current_interval)
        raise RuntimeError(
            "Timed out waiting for xAI device authorization — open the "
            "verification URL, approve access, then try again."
        )

    def _store_tokens(self, resp: dict, *, token_endpoint: str | None = None) -> dict:
        access = resp["access_token"]
        refresh = resp.get("refresh_token")
        expires_in = int(resp.get("expires_in") or 3600)

        claims = sub_auth.decode_jwt_payload(access)
        email = (
            claims.get("email")
            or (claims.get("profile") or {}).get("email")
            or ""
        )
        if isinstance(email, str):
            email = email.lower().strip()
        else:
            email = ""
        account_id = (
            claims.get("sub")
            or claims.get("account_id")
            or claims.get("user_id")
            or None
        )

        now_ms = time.time() * 1000
        cred = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at_ms": now_ms + expires_in * 1000,
            "account_id": account_id,
            "email": email or None,
            "token_endpoint": token_endpoint
            or (self._cred or {}).get("token_endpoint")
            or XAI_OAUTH_TOKEN_URL_FALLBACK,
        }
        if resp.get("id_token"):
            cred["id_token"] = resp["id_token"]
        sub_auth.save_credential(PROVIDER_ID, cred)
        self._cred = cred
        return cred

    def refresh(self) -> dict:
        """Exchange the stored refresh token for a new access token."""
        if not self._cred or not self._cred.get("refresh_token"):
            raise RuntimeError(
                "Not logged in to xAI subscription. "
                "Run login() first (Settings → Sign in with xAI)."
            )
        endpoint = (
            self._cred.get("token_endpoint")
            or XAI_OAUTH_TOKEN_URL_FALLBACK
        )
        try:
            resp = sub_auth.post_form(
                endpoint,
                {
                    "grant_type": "refresh_token",
                    "client_id": XAI_OAUTH_CLIENT_ID,
                    "refresh_token": self._cred["refresh_token"],
                },
                headers={"Accept": "application/json"},
                timeout=30.0,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 403:
                raise XaiTierDeniedError(body) from exc
            if exc.code in (400, 401):
                raise RuntimeError(
                    "xAI OAuth refresh failed — re-authenticate via "
                    "Settings → Sign in with xAI. "
                    f"(HTTP {exc.code}: {body})"
                ) from exc
            raise RuntimeError(
                f"xAI token refresh failed (HTTP {exc.code}): {body}"
            ) from exc
        resp.setdefault("refresh_token", self._cred["refresh_token"])
        return self._store_tokens(resp, token_endpoint=endpoint)

    def _ensure_auth(self) -> None:
        if not self._cred:
            raise RuntimeError(
                "Not logged in to xAI subscription. "
                "Run login() first (Settings → Sign in with xAI)."
            )
        if sub_auth.is_expired(self._cred, skew_ms=REFRESH_SKEW_MS):
            self.refresh()

    # ── Request helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._cred['access_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _content(self, part: dict) -> dict:
        if part["type"] == "text":
            return {"type": "text", "text": part["text"]}
        import base64
        from pathlib import Path

        raw = Path(part["path"]).read_bytes()
        mime = part.get("mime", "image/png")
        data = base64.standard_b64encode(raw).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data}"},
        }

    def _message(self, role: str, parts: list[dict]) -> dict:
        if all(p["type"] == "text" for p in parts):
            return {
                "role": role,
                "content": "\n\n".join(p["text"] for p in parts),
            }
        return {"role": role, "content": [self._content(p) for p in parts]}

    def _post_once(self, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            CHAT_COMPLETIONS_URL,
            data=data,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_excerpt = exc.read().decode("utf-8", errors="replace")[:400]
            if exc.code == 401:
                raise  # caller refreshes + retries
            if exc.code == 403:
                raise XaiTierDeniedError(body_excerpt) from exc
            raise RuntimeError(
                f"POST {CHAT_COMPLETIONS_URL} returned HTTP {exc.code}: "
                f"{body_excerpt}"
            ) from exc

    # ── Completion ───────────────────────────────────────────────────────────

    def complete_ex(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> tuple[str, str]:
        self._ensure_auth()
        msgs = [{"role": "system", "content": system}]
        for m in messages:
            msgs.append(self._message(m["role"], m["content"]))
        body = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": max_tokens,
        }
        try:
            resp = self._post_once(body)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                body_excerpt = exc.read().decode("utf-8", errors="replace")[:400]
                if exc.code == 403:
                    raise XaiTierDeniedError(body_excerpt) from exc
                raise RuntimeError(
                    f"POST {CHAT_COMPLETIONS_URL} returned HTTP {exc.code}: "
                    f"{body_excerpt}"
                ) from exc
            self.refresh()
            resp = self._post_once(body)

        choices = resp.get("choices") or []
        if not choices:
            raise RuntimeError(
                f"xAI endpoint returned no completion (model={self.model!r}). "
                f"Detail: {resp.get('error') or resp}"
            )
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        # Some Grok reasoning variants put CoT in reasoning_content.
        if not content:
            content = (
                msg.get("reasoning_content")
                or msg.get("reasoning")
                or ""
            )
        thinking = msg.get("reasoning_content") or msg.get("reasoning") or ""
        if thinking == content:
            thinking = ""
        return content, thinking

    def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]
