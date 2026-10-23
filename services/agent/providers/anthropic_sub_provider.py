"""Anthropic Claude subscription provider.

Authenticates using the user's Claude Pro/Max subscription via the same OAuth
PKCE flow the Claude Code CLI uses — no metered API key required. Tokens are
stored in config.json and refreshed automatically before they expire.
"""
from __future__ import annotations

import base64
import hashlib
import json
import platform
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .base import Provider
from . import sub_auth


CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
CALLBACK_PORT = 54545
CALLBACK_PATH = "/callback"
REDIRECT_URI = "http://localhost:54545/callback"
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
API_URL = "https://api.anthropic.com/v1/messages"
PROVIDER_ID = "anthropic-sub"
CLAUDE_CODE_VERSION = "2.1.165"
CLAUDE_AGENT_SDK_VERSION = "0.3.165"
CLAUDE_CLIENT_VERSION = "1.11187.4"
CCH_SEED = 0x4D659218E32A3268
DEFAULT_MODEL = "claude-opus-5-5"
REFRESH_SKEW_MS = 5 * 60 * 1000

_TOKEN_HEADERS = {
    "anthropic-beta": "oauth-2025-04-20",
    "User-Agent": "anthropic-sdk-typescript/0.94.0 userOAuthProvider",
}


class AnthropicSubProvider(Provider):
    """Anthropic Messages API backed by a Claude Pro/Max subscription OAuth token."""

    name = "anthropic-sub"
    supports_vision = True

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._cred: dict | None = sub_auth.load_credential(PROVIDER_ID)

    # ── Authentication ───────────────────────────────────────────────────────

    def login(self) -> dict:
        """Open the browser to authorize and exchange the code for tokens."""
        verifier = sub_auth.make_verifier()
        challenge = sub_auth.challenge_for(verifier)
        state = sub_auth.random_state()

        params = urllib.parse.urlencode({
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        })
        url = f"{AUTHORIZE_URL}?{params}"

        import webbrowser
        webbrowser.open(url)

        server = sub_auth.CallbackServer(CALLBACK_PORT, CALLBACK_PATH)
        result = server.wait_for_callback(expected_state=state)

        resp = sub_auth.post_json(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": result["code"],
                "state": state,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
            headers=_TOKEN_HEADERS,
        )

        now_ms = time.time() * 1000
        cred = {
            "access_token": resp["access_token"],
            "refresh_token": resp["refresh_token"],
            "expires_at_ms": now_ms + resp["expires_in"] * 1000,
            "account_id": None,
            "email": None,
        }
        # Best-effort profile + usage seed (user:profile scope).
        try:
            from . import usage as usage_mod
            cred = usage_mod.enrich_anthropic_profile(cred)
        except Exception:
            pass
        sub_auth.save_credential(PROVIDER_ID, cred)
        self._cred = cred
        return cred

    def refresh(self) -> dict:
        """Exchange a refresh token for a new access token."""
        resp = sub_auth.post_json(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": self._cred["refresh_token"],
                "client_id": CLIENT_ID,
            },
            headers=_TOKEN_HEADERS,
        )

        now_ms = time.time() * 1000
        cred = {
            "access_token": resp["access_token"],
            # keep old refresh_token if the server didn't issue a new one
            "refresh_token": resp.get("refresh_token", self._cred["refresh_token"]),
            "expires_at_ms": now_ms + resp["expires_in"] * 1000 - REFRESH_SKEW_MS,
            "account_id": self._cred.get("account_id"),
            "email": self._cred.get("email"),
            "display_name": self._cred.get("display_name"),
            "plan": self._cred.get("plan"),
        }
        # Preserve last-known usage windows across refresh so cold-start restore works.
        for k in ("last_usage", "last_usage_fetched_at"):
            if self._cred.get(k) is not None:
                cred[k] = self._cred[k]
        sub_auth.save_credential(PROVIDER_ID, cred)
        self._cred = cred
        return cred

    def _ensure_auth(self) -> None:
        if not self._cred:
            raise RuntimeError(
                "Not logged in to Claude subscription. "
                "Run login() first (Settings → Connect Claude)."
            )
        if sub_auth.is_expired(self._cred, skew_ms=REFRESH_SKEW_MS):
            self.refresh()

    # ── CCH integrity token ──────────────────────────────────────────────────

    def _compute_cch(self, body_bytes: bytes) -> str:
        # CCH is a truncated xxhash64 of the serialized request body with the
        # placeholder already in place; Anthropic uses it as a lightweight
        # integrity/billing consistency check on subscription requests.
        import xxhash
        h = xxhash.xxh64(body_bytes, seed=CCH_SEED).intdigest()
        return format(h & 0xFFFFF, "05x")

    def _billing_suffix(self, first_user_msg: str) -> str:
        def _ch(i: int) -> str:
            return first_user_msg[i] if i < len(first_user_msg) else "0"

        k = _ch(4) + _ch(7) + _ch(20)
        return hashlib.sha256(
            ("59cf53e54c78" + k + CLAUDE_CODE_VERSION).encode()
        ).hexdigest()[:3]

    # ── Message conversion ───────────────────────────────────────────────────

    def _block(self, part: dict) -> dict:
        if part["type"] == "text":
            return {"type": "text", "text": part["text"]}
        # image part — read file from disk and encode as base64
        raw = Path(part["path"]).read_bytes()
        mime = part.get("mime", "image/png")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }

    # ── Request headers ──────────────────────────────────────────────────────

    def _headers(self, cch: str) -> dict:
        # cch is embedded inside the system prompt billing block, not a header;
        # the parameter is kept for call-site symmetry and future use.
        return {
            "Authorization": f"Bearer {self._cred['access_token']}",
            # Do NOT advertise gzip/br/zstd: urllib does not auto-decompress when
            # Accept-Encoding is set manually, which produced UnicodeDecodeError
            # on 0x8b (gzip magic) in complete_ex. Identity keeps the body UTF-8 JSON.
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-dangerous-direct-browser-access": "true",
            "anthropic-beta": "interleaved-thinking-2025-05-14,oauth-2025-04-20",
            "x-app": "cli",
            "User-Agent": (
                f"claude-cli/{CLAUDE_CODE_VERSION} "
                f"(external, local-agent, agent-sdk/{CLAUDE_AGENT_SDK_VERSION})"
            ),
            "X-Stainless-Retry-Count": "0",
            "X-Stainless-Runtime-Version": "v24.3.0",
            "X-Stainless-Package-Version": "0.94.0",
            "X-Stainless-Runtime": "node",
            "X-Stainless-Lang": "js",
            "X-Stainless-Arch": platform.machine(),
            "X-Stainless-OS": platform.system(),
            "X-Stainless-Timeout": "900",
            "anthropic-client-platform": "desktop_app",
            "anthropic-client-version": CLAUDE_CLIENT_VERSION,
        }

    # ── Completion ───────────────────────────────────────────────────────────

    def _post_once(self, body_str: str, cch: str) -> dict:
        """Send one attempt; raises RuntimeError on non-401 HTTP errors."""
        data = body_str.encode("utf-8")
        req = urllib.request.Request(
            API_URL,
            data=data,
            headers=self._headers(cch),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise  # caller handles 401 with token refresh
            body_excerpt = exc.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(
                f"POST {API_URL} returned HTTP {exc.code}: {body_excerpt}"
            ) from exc

    def complete_ex(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> tuple[str, str]:
        self._ensure_auth()

        api_messages = [
            {
                "role": msg["role"],
                "content": [self._block(p) for p in msg["content"]],
            }
            for msg in messages
        ]

        # Gather first user message text for the billing suffix derivation.
        first_user_msg = ""
        for msg in messages:
            if msg["role"] == "user":
                first_user_msg = "".join(
                    p["text"] for p in msg["content"] if p["type"] == "text"
                )
                break

        suffix = self._billing_suffix(first_user_msg)

        # Anthropic OAuth requires a specific system prompt scaffolding.
        # system[0] carries the billing header (with CCH placeholder filled in
        # after the body is serialised); system[1] declares the agent identity.
        system_array = [
            {
                "type": "text",
                "text": (
                    f"x-anthropic-billing-header: "
                    f"cc_version={CLAUDE_CODE_VERSION}.{suffix}; "
                    f"cc_entrypoint=local-agent; cch=00000;"
                ),
            },
            {
                "type": "text",
                "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
            },
            {
                "type": "text",
                "text": system,
            },
        ]

        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system_array,
            "messages": api_messages,
            # Adaptive thinking, NOT {"type":"enabled","budget_tokens":…}: on Opus 4.8
            # `budget_tokens` is removed and 400s (see anthropic_provider.py). Adaptive is
            # the shape the working API-key provider uses.
            "thinking": {"type": "adaptive"},
        }

        # Compute CCH over the body with the placeholder, then splice it in. Compact
        # separators match the reference client's JSON.stringify byte-for-byte, so if
        # Anthropic re-serializes to validate the hash, ours agrees.
        body_json_str = json.dumps(body, separators=(",", ":"))
        cch = self._compute_cch(body_json_str.encode("utf-8"))
        patched = body_json_str.replace("cch=00000;", f"cch={cch};", 1)

        try:
            resp = self._post_once(patched, cch)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                body_excerpt = exc.read().decode("utf-8", errors="replace")[:400]
                raise RuntimeError(
                    f"POST {API_URL} returned HTTP {exc.code}: {body_excerpt}"
                ) from exc
            # Refresh once and retry on 401.
            self.refresh()
            resp = self._post_once(patched, cch)

        if resp.get("type") == "error":
            raise RuntimeError(
                f"Anthropic API error from {API_URL}: "
                + resp["error"].get("message", "unknown")
            )

        content = resp.get("content", [])
        text = "".join(b["text"] for b in content if b.get("type") == "text")
        thinking = "".join(
            b.get("thinking", "") for b in content if b.get("type") == "thinking"
        )
        return text, thinking

    def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]
