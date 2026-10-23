"""OpenAI Codex subscription provider.

Authenticates via the user's ChatGPT Plus/Pro subscription using the same
OAuth/PKCE flow as the Codex CLI, then calls the OpenAI Responses API.
No metered API key is needed — billing runs through the subscription.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .base import Provider
from . import sub_auth

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
CODEX_BASE_URL = "https://chatgpt.com/backend-api"
DEVICE_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
DEVICE_AUTH_URL = "https://auth.openai.com/codex/device"
PROVIDER_ID = "openai-codex"
ORIGINATOR = "chudware"
# Codex subscription models come from the Chudcode catalog for online turns.
DEFAULT_MODEL = "gpt-6-sol"


class CodexSubProvider(Provider):
    """Chat completion via OpenAI Codex subscription (ChatGPT Plus/Pro)."""

    name = "codex-sub"
    # Text-only for now: _build_input drops image parts (Responses-API vision is a
    # follow-up), so declaring False keeps the agent loop from sending screenshots the
    # provider would silently discard (frozen §2 semantics).
    supports_vision = False

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._cred = sub_auth.load_credential(PROVIDER_ID)

    # ── Auth flows ───────────────────────────────────────────────────────────

    def login(self) -> dict:
        """Browser-based PKCE login. Opens the user's default browser."""
        verifier = sub_auth.make_verifier()
        challenge = sub_auth.challenge_for(verifier)
        state = sub_auth.random_state()

        params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            # OpenAI-specific flags that the Codex CLI also sends
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": ORIGINATOR,
        })
        url = f"{AUTHORIZE_URL}?{params}"

        import webbrowser
        webbrowser.open(url)

        server = sub_auth.CallbackServer(CALLBACK_PORT, CALLBACK_PATH)
        result = server.wait_for_callback(expected_state=state)

        resp = sub_auth.post_form(TOKEN_URL, {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": result["code"],
            "code_verifier": verifier,
            "redirect_uri": REDIRECT_URI,
        })
        return self._store_tokens(resp)

    def login_device(self) -> dict:
        """Headless device-code login for environments without a browser."""
        import time

        init = sub_auth.post_json(DEVICE_USERCODE_URL, {"client_id": CLIENT_ID})
        device_auth_id = init["device_auth_id"]
        user_code = init["user_code"]
        interval = init.get("interval", 5)

        print(f"Go to {DEVICE_AUTH_URL} and enter code: {user_code}")

        authorization_code = None
        code_verifier = None
        for _ in range(120):
            time.sleep(max(interval, 5) + 1)
            try:
                data = json.dumps({
                    "device_auth_id": device_auth_id,
                    "user_code": user_code,
                }).encode("utf-8")
                req = urllib.request.Request(
                    DEVICE_TOKEN_URL,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                authorization_code = payload["authorization_code"]
                code_verifier = payload["code_verifier"]
                break
            except urllib.error.HTTPError as e:
                # 403/404 means the user hasn't approved yet — keep polling
                if e.code in (403, 404):
                    continue
                raise

        if authorization_code is None:
            raise RuntimeError(
                f"Device-code login timed out — visit {DEVICE_AUTH_URL} "
                f"and enter code {user_code}, then retry."
            )

        resp = sub_auth.post_form(TOKEN_URL, {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": authorization_code,
            "code_verifier": code_verifier,
            "redirect_uri": DEVICE_REDIRECT_URI,
        })
        return self._store_tokens(resp)

    def _store_tokens(self, resp: dict) -> dict:
        """Persist tokens from a token-endpoint response and update self._cred."""
        import time

        access = resp["access_token"]
        refresh = resp.get("refresh_token")
        expires_in = resp.get("expires_in", 3600)

        claims = sub_auth.decode_jwt_payload(access)
        account_id = (claims.get("https://api.openai.com/auth") or {}).get(
            "chatgpt_account_id"
        )
        email = (
            (claims.get("https://api.openai.com/profile") or {}).get("email") or ""
        ).lower().strip()

        if not account_id:
            raise RuntimeError(
                "Codex login failed: no chatgpt_account_id in token — "
                "this subscription may not have Codex access."
            )

        now_ms = time.time() * 1000
        cred = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at_ms": now_ms + expires_in * 1000,
            "account_id": account_id,
            "email": email,
        }
        # Preserve plan across refresh; fill display_name from JWT email.
        if self._cred and self._cred.get("plan"):
            cred["plan"] = self._cred.get("plan")
        try:
            from . import usage as usage_mod
            cred = usage_mod.enrich_codex_identity(cred)
        except Exception:
            if email:
                cred.setdefault("display_name", email)
        sub_auth.save_credential(PROVIDER_ID, cred)
        self._cred = cred
        return cred

    def refresh(self) -> dict:
        """Exchange the stored refresh token for a new access token."""
        resp = sub_auth.post_form(TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": self._cred["refresh_token"],
            "client_id": CLIENT_ID,
        })
        # Some token responses omit refresh_token; preserve the existing one so we
        # can refresh again later without forcing a full re-login.
        resp.setdefault("refresh_token", self._cred["refresh_token"])
        return self._store_tokens(resp)

    def _ensure_auth(self) -> None:
        if not self._cred:
            raise RuntimeError(
                "Not logged in to Codex subscription. "
                "Run login() first (Settings → Connect Codex)."
            )
        if sub_auth.is_expired(self._cred, skew_ms=0):
            self.refresh()

    # ── Request helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        import platform
        return {
            "Authorization": f"Bearer {self._cred['access_token']}",
            "chatgpt-account-id": self._cred.get("account_id") or "",
            "OpenAI-Beta": "responses=experimental",
            "originator": ORIGINATOR,
            "User-Agent": (
                f"chudware/1.0 ({platform.system()} {platform.release()}; "
                f"{platform.machine()})"
            ),
            "Content-Type": "application/json",
        }

    def _build_input(self, system: str, messages: list[dict]) -> list:
        """Convert (system, messages) to the Responses API input array."""
        items: list[dict] = [{"role": "developer", "content": system}]
        for msg in messages:
            text_parts = [
                p["text"]
                for p in msg.get("content", [])
                if p.get("type") == "text"
            ]
            # Image parts are skipped — vision through this endpoint is out of scope
            text = "\n\n".join(text_parts)
            items.append({"role": msg["role"], "content": text})
        return items

    # ── Completion ───────────────────────────────────────────────────────────

    def complete(self, system: str, messages: list[dict],
                 max_tokens: int = 8000) -> str:
        self._ensure_auth()

        url = f"{CODEX_BASE_URL}/responses"
        body = {
            "model": self.model,
            "input": self._build_input(system, messages),
            "stream": False,
        }

        def _post(payload: dict) -> dict:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers=self._headers(), method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise  # caller will refresh and retry
                body_excerpt = e.read()[:400].decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"POST {url} returned HTTP {e.code}: {body_excerpt}"
                ) from e

        try:
            resp = _post(body)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Token may have expired mid-session; refresh once and retry
                self.refresh()
                resp = _post(body)
            else:
                body_excerpt = e.read()[:400].decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"POST {url} returned HTTP {e.code}: {body_excerpt}"
                ) from e

        # Walk the Responses API output array collecting output_text parts
        texts: list[str] = []
        for item in resp.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        texts.append(part["text"])

        if texts:
            return "".join(texts)

        # Flat convenience field present in some response shapes
        return resp.get("output_text", "")

    def complete_ex(self, system: str, messages: list[dict],
                    max_tokens: int = 8000) -> tuple[str, str]:
        return self.complete(system, messages, max_tokens=max_tokens), ""
