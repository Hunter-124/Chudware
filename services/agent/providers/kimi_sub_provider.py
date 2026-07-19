"""Moonshot Kimi Code subscription provider (Kimi For Coding OAuth).

Authenticates via the OAuth 2.0 device authorization grant against auth.kimi.com
— the same flow oh-my-pi (``packages/ai/src/registry/oauth/kimi.ts``) and the
Kimi CLI use for Kimi For Coding subscriptions. No metered API key is required;
billing runs through the subscription. Tokens are stored in config.json (via
:mod:`sub_auth`) and refreshed automatically before they expire. Python is the
single refresh writer.

Completions use the Anthropic-compatible endpoint at
``https://api.kimi.com/coding/v1/messages`` (ported from omp's
``providers/kimi.ts`` + ``openai-anthropic-shim.ts``): Bearer access token,
``anthropic-version`` header, the X-Msh-* Kimi CLI device headers, a required
``max_tokens``, and adaptive thinking. Usage probes hit
``GET /coding/v1/usages`` (see :mod:`usage`).

Cloud provider: NEVER offline-safe.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import platform
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from engine import paths

from .base import Provider
from . import sub_auth

log = logging.getLogger("agent.providers.kimi_sub")

PROVIDER_ID = "kimi-sub"
# Subscription endpoint only accepts "kimi-for-coding". GUI catalog uses kimi-k3 /
# kimi-k2.7 (K3 is the CAD-modeling priority model); normalize before the wire call.
DEFAULT_MODEL = "kimi-for-coding"
_WIRE_ALIASES = {
    "kimi-k3": "kimi-for-coding",
    "kimi-k2.7": "kimi-for-coding",
    "kimi-k2.7-code": "kimi-for-coding",
    "kimi-for-coding": "kimi-for-coding",
}


def wire_model(model: str | None) -> str:
    """Map a GUI/catalog model id to the id the Kimi Code subscription accepts."""
    m = (model or DEFAULT_MODEL).strip()
    return _WIRE_ALIASES.get(m, DEFAULT_MODEL)

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
DEFAULT_OAUTH_HOST = "https://auth.kimi.com"
DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
ANTHROPIC_VERSION = "2023-06-01"
# Version reported in the Kimi CLI headers (omp packages/ai package.json at port time).
KIMI_CLI_VERSION = "17.0.5"

DEVICE_ID_FILENAME = "kimi-device-id"
DEFAULT_POLL_INTERVAL_S = 5
DEFAULT_DEVICE_FLOW_TTL_S = 15 * 60
OAUTH_EXPIRY_SKEW_MS = 5 * 60 * 1000
REFRESH_SKEW_MS = OAUTH_EXPIRY_SKEW_MS

ProgressCb = Callable[[dict], None]


def _oauth_host() -> str:
    return (
        os.environ.get("KIMI_CODE_OAUTH_HOST")
        or os.environ.get("KIMI_OAUTH_HOST")
        or DEFAULT_OAUTH_HOST
    ).rstrip("/")


def _base_url() -> str:
    return (os.environ.get("KIMI_CODE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


# ── Kimi CLI device headers ──────────────────────────────────────────────────


def _sanitize_header(value: str, fallback: str = "") -> str:
    sanitized = "".join(ch for ch in str(value) if 0x20 <= ord(ch) <= 0x7E).strip()
    return sanitized or fallback


def _device_model() -> str:
    system = platform.system()
    label = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(system, system)
    return _sanitize_header(
        " ".join(p for p in (label, platform.release(), platform.machine()) if p),
        "unknown",
    )


def _get_device_id() -> str:
    """Install-stable device id, persisted next to config.json (best-effort).

    A missing/unwritable config dir must never break header construction —
    fall back to a per-process ephemeral id (mirrors omp getDeviceId).
    """
    device_id_path = paths.CONFIG.parent / DEVICE_ID_FILENAME
    try:
        existing = device_id_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except Exception:  # noqa: BLE001 — unreadable file: regenerate below
        pass
    device_id = uuid.uuid4().hex
    try:
        device_id_path.parent.mkdir(parents=True, exist_ok=True)
        device_id_path.write_text(f"{device_id}\n", encoding="utf-8")
        os.chmod(device_id_path, 0o600)
    except Exception:  # noqa: BLE001 — persist failure → ephemeral id for this process
        pass
    return device_id


_COMMON_HEADERS_CACHE: dict | None = None


def get_kimi_common_headers() -> dict:
    """Kimi CLI identification headers, cached per-process (omp getKimiCommonHeaders)."""
    global _COMMON_HEADERS_CACHE
    if _COMMON_HEADERS_CACHE is None:
        _COMMON_HEADERS_CACHE = {
            "User-Agent": f"KimiCLI/{KIMI_CLI_VERSION}",
            "X-Msh-Platform": "kimi_cli",
            "X-Msh-Version": KIMI_CLI_VERSION,
            "X-Msh-Device-Name": _sanitize_header(platform.node(), "unknown"),
            "X-Msh-Device-Model": _device_model(),
            "X-Msh-Os-Version": _sanitize_header(platform.version(), "unknown"),
            "X-Msh-Device-Id": _sanitize_header(_get_device_id(), "unknown"),
        }
    return dict(_COMMON_HEADERS_CACHE)


def provider_is_usable() -> bool:
    """True when a signed-in Kimi credential with tokens exists."""
    cred = sub_auth.load_credential(PROVIDER_ID)
    return bool(cred and (cred.get("access_token") or cred.get("refresh_token")))


class KimiSubProvider(Provider):
    """Anthropic-shaped chat completion via a Kimi For Coding subscription token."""

    name = "kimi-sub"
    supports_vision = True

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = wire_model(model)
        self._cred = sub_auth.load_credential(PROVIDER_ID)
        self.base_url = _base_url()

    # ── Auth: OAuth 2.0 device authorization grant ───────────────────────────

    def login(self, progress: ProgressCb | None = None) -> dict:
        """Device-code login (only Kimi path). Opens the verification URL."""
        return self.login_device(open_browser=True, progress=progress)

    def login_device(
        self,
        open_browser: bool = True,
        progress: ProgressCb | None = None,
    ) -> dict:
        """OAuth 2.0 device authorization grant against auth.kimi.com.

        Requests a device code, surfaces the verification URL + user code via
        *progress* (same payload shape as the xAI device flow so the Settings
        device-code UX works unchanged), optionally opens a browser, then polls
        the token endpoint until approved, denied, or expired.
        """
        def _progress(payload: dict) -> None:
            if progress is None:
                return
            try:
                progress(payload)
            except Exception as exc:  # noqa: BLE001 — UI callback must not abort login
                log.warning("kimi oauth: progress callback failed: %s", exc)

        log.info("kimi oauth: start (open_browser=%s)", open_browser)
        _progress({"stage": "start", "browser_opened": False})

        try:
            device = self._request_device_authorization()
        except Exception as exc:
            log.error("kimi oauth: device authorization failed: %s", exc)
            _progress({"stage": "device_code", "error": str(exc)})
            raise

        verification_uri = str(device.get("verification_uri") or "")
        verification_url = str(
            device.get("verification_uri_complete") or verification_uri
        )
        user_code = str(device.get("user_code") or "")
        expires_in = int(device.get("expires_in") or DEFAULT_DEVICE_FLOW_TTL_S)
        interval = max(1, int(device.get("interval") or DEFAULT_POLL_INTERVAL_S))
        device_code = str(device["device_code"])

        log.info(
            "kimi oauth: device code issued (user_code=%s url=%s expires_in=%s interval=%s)",
            user_code, verification_url, expires_in, interval,
        )
        print(
            f"Go to {verification_url} and approve access"
            + (f" (code: {user_code})" if user_code else "")
        )

        browser_opened = False
        if open_browser:
            from .xai_sub_provider import open_verification_url

            browser_opened = open_verification_url(verification_url)
            log.info("kimi oauth: browser_opened=%s", browser_opened)

        _progress({
            "stage": "awaiting_approval",
            "user_code": user_code,
            "verification_url": verification_url,
            "verification_uri": verification_uri,
            "browser_opened": browser_opened,
            "expires_in": expires_in,
            "interval": interval,
        })

        try:
            payload = self._poll_for_token(
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        except Exception as exc:
            log.error("kimi oauth: token poll failed: %s", exc)
            _progress({"stage": "poll", "error": str(exc)})
            raise

        try:
            cred = self._store_tokens(payload)
        except Exception as exc:
            log.error("kimi oauth: store tokens failed: %s", exc)
            _progress({"stage": "exchange", "error": str(exc)})
            raise

        log.info(
            "kimi oauth: login ok (email=%s account_id=%s)",
            cred.get("email"), cred.get("account_id"),
        )
        _progress({
            "stage": "connected",
            "user_code": user_code,
            "verification_url": verification_url,
            "browser_opened": browser_opened,
            "email": cred.get("email"),
            "account_id": cred.get("account_id"),
        })
        return cred

    def _request_device_authorization(self) -> dict:
        try:
            payload = sub_auth.post_form(
                f"{_oauth_host()}/api/oauth/device_authorization",
                {"client_id": CLIENT_ID},
                headers={"Accept": "application/json", **get_kimi_common_headers()},
                timeout=30.0,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(
                f"Kimi device authorization failed (HTTP {exc.code}): {body}"
            ) from exc
        if not all(payload.get(k) for k in ("user_code", "device_code", "verification_uri")):
            raise RuntimeError(
                "Kimi device authorization response missing required fields"
            )
        return payload

    def _poll_for_token(
        self,
        *,
        device_code: str,
        expires_in: int,
        poll_interval: int,
    ) -> dict:
        deadline = time.monotonic() + max(1, expires_in)
        wait_s = max(1, poll_interval)
        while time.monotonic() < deadline:
            try:
                payload = sub_auth.post_form(
                    f"{_oauth_host()}/api/oauth/token",
                    {
                        "client_id": CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={"Accept": "application/json", **get_kimi_common_headers()},
                    timeout=30.0,
                )
                if payload.get("access_token"):
                    return payload
                # 200-with-error-body is unusual but treat it like the error path.
                err = payload
            except urllib.error.HTTPError as exc:
                try:
                    err = json.loads(exc.read().decode("utf-8", errors="replace"))
                except Exception:  # noqa: BLE001
                    err = {}

            code = str(err.get("error") or "")
            if code == "authorization_pending":
                time.sleep(wait_s)
                continue
            if code == "slow_down":
                wait_s += 5
                retry_after = err.get("interval")
                if isinstance(retry_after, (int, float)) and retry_after > wait_s:
                    wait_s = int(retry_after)
                time.sleep(wait_s)
                continue
            if code == "expired_token":
                raise RuntimeError("Kimi device authorization expired")
            if code == "access_denied":
                raise RuntimeError("Kimi device authorization denied")
            detail = err.get("error_description") or code or "unknown error"
            raise RuntimeError(f"Kimi device flow failed: {detail}")
        raise RuntimeError(
            "Timed out waiting for Kimi device authorization — open the "
            "verification URL, approve access, then try again."
        )

    @staticmethod
    def _parse_token_payload(payload: dict, refresh_fallback: str | None = None) -> dict:
        access = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not access or not isinstance(expires_in, (int, float)):
            raise RuntimeError("Kimi token response missing required fields")
        refresh = payload.get("refresh_token") or refresh_fallback
        if not refresh:
            raise RuntimeError("Kimi token response missing refresh token")
        now_ms = time.time() * 1000
        return {
            "access_token": access,
            "refresh_token": refresh,
            # Skew subtracted at store time (omp parseTokenPayload) so a token
            # expiring within the skew window is already treated as expired.
            "expires_at_ms": now_ms + int(expires_in) * 1000 - OAUTH_EXPIRY_SKEW_MS,
        }

    def _store_tokens(self, payload: dict, refresh_fallback: str | None = None) -> dict:
        cred = self._parse_token_payload(payload, refresh_fallback)
        claims = sub_auth.decode_jwt_payload(cred["access_token"])
        email = claims.get("email") or (claims.get("profile") or {}).get("email") or ""
        if isinstance(email, str):
            email = email.lower().strip()
        else:
            email = ""
        cred["account_id"] = (
            claims.get("sub") or claims.get("account_id") or claims.get("user_id") or None
        )
        cred["email"] = email or None
        # Preserve identity/usage metadata across refresh.
        if self._cred:
            for k in ("plan", "display_name", "last_usage", "last_usage_fetched_at"):
                if self._cred.get(k) is not None and not cred.get(k):
                    cred[k] = self._cred[k]
        sub_auth.save_credential(PROVIDER_ID, cred)
        self._cred = cred
        return cred

    def refresh(self) -> dict:
        """Exchange the stored refresh token for a new access token."""
        if not self._cred or not self._cred.get("refresh_token"):
            raise RuntimeError(
                "Not logged in to Kimi subscription. "
                "Run login() first (Settings → Accounts)."
            )
        old_refresh = self._cred["refresh_token"]
        try:
            payload = sub_auth.post_form(
                f"{_oauth_host()}/api/oauth/token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": old_refresh,
                    "client_id": CLIENT_ID,
                },
                headers={"Accept": "application/json", **get_kimi_common_headers()},
                timeout=30.0,
            )
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = err.get("error_description") or err.get("error") or ""
            except Exception:  # noqa: BLE001
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"Kimi token refresh failed (HTTP {exc.code}){suffix}"
            ) from exc
        # Kimi omits refresh_token on refresh responses — keep the old one.
        return self._store_tokens(payload, refresh_fallback=old_refresh)

    # ── Completion: Anthropic-compatible /coding/v1/messages ────────────────

    def _ensure_auth(self) -> None:
        if not self._cred:
            self._cred = sub_auth.load_credential(PROVIDER_ID)
        if not self._cred:
            raise RuntimeError(
                "Not logged in to Kimi subscription. "
                "Run login() first (Settings → Accounts)."
            )
        if sub_auth.is_expired(self._cred, skew_ms=REFRESH_SKEW_MS):
            self.refresh()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._cred['access_token']}",
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            **get_kimi_common_headers(),
        }

    def _block(self, part: dict) -> dict:
        if part["type"] == "text":
            return {"type": "text", "text": part["text"]}
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

    def _post_once(self, body: dict) -> dict:
        url = f"{_base_url()}/messages"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise  # caller refreshes + retries once
            body_excerpt = exc.read().decode("utf-8", errors="replace")[:400]
            err = RuntimeError(f"POST {url} returned HTTP {exc.code}: {body_excerpt}")
            err.status_code = int(exc.code)  # type: ignore[attr-defined]
            raise err from exc

    def complete_ex(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> tuple[str, str]:
        self._ensure_auth()
        api_messages = [
            {"role": m["role"], "content": [self._block(p) for p in m["content"]]}
            for m in messages
        ]
        body = {
            "model": self.model,
            # max_tokens is REQUIRED by the Anthropic-compatible endpoint.
            "max_tokens": max_tokens,
            "system": system,
            "messages": api_messages,
            # omp anthropicThinkingMode "anthropic-adaptive" for kimi thinking models.
            "thinking": {"type": "adaptive"},
        }
        try:
            resp = self._post_once(body)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            self.refresh()
            resp = self._post_once(body)

        if resp.get("type") == "error":
            raise RuntimeError(
                "Kimi API error: " + (resp.get("error") or {}).get("message", "unknown")
            )
        content = resp.get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        thinking = "".join(
            b.get("thinking", "") for b in content if b.get("type") == "thinking"
        )
        try:
            from . import token_usage as _tu  # noqa: PLC0415

            extract = getattr(_tu, "extract_anthropic_usage", None)
            u = extract(resp) if extract else (resp.get("usage") or {})
            _tu.record_usage(
                provider=self.name,
                model=str(self.model or ""),
                prompt_tokens=u.get("input_tokens"),
                completion_tokens=u.get("output_tokens"),
                total_tokens=(u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
                if isinstance(u, dict)
                else None,
                source="api",
            )
        except Exception:  # noqa: BLE001 — usage metering must never fail chat
            pass
        return text, thinking

    def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]
