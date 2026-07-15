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
import logging
import os
import shutil
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
import webbrowser
from typing import Callable

from .base import Provider
from . import sub_auth

log = logging.getLogger("agent.providers.xai_sub")

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
# Manual fallback verification page when the device-code response omits a URI.
XAI_VERIFICATION_URL_FALLBACK = "https://accounts.x.ai/activate"

API_BASE_URL = "https://api.x.ai/v1"
CHAT_COMPLETIONS_URL = f"{API_BASE_URL}/chat/completions"
MODELS_URL = f"{API_BASE_URL}/models"
PROVIDER_ID = "xai-sub"
DEFAULT_MODEL = "grok-4.5"
# Access tokens are short-lived (~6h); refresh up to 1h early like hermes
# (XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 3600).
REFRESH_SKEW_MS = 60 * 60 * 1000

# Concurrent chat-completion cap (mega-2 fingerprint: provider_read_timeout_storm).
# 5-way SPECIFY fan-out + later pairs hammered xAI with parallel long reads →
# "The read operation timed out" classified as budget death. Bound sibling
# complete_ex calls process-wide. Override via AICAD_XAI_MAX_CONCURRENT (int ≥1).
# mega-5: default raised to 3 so serialization under fan-out is less likely to
# starve long PLAN/BUILD reads; occupancy + queue_wait telemetry is emitted so
# we can tell serialization vs load-induced timeouts.
_DEFAULT_MAX_CONCURRENT = 3
_DEFAULT_READ_TIMEOUT_S = 180.0
# One bounded in-provider retry for retryable read timeouts (mega-4/5 watch-list).
_DEFAULT_READ_TIMEOUT_RETRIES = 1
_sem_lock = threading.Lock()
_request_sem: threading.Semaphore | None = None
_sem_capacity: int = 0
_sem_in_flight: int = 0
_sem_peak_in_flight: int = 0
_sem_wait_count: int = 0
_sem_total_wait_ms: float = 0.0


def _max_concurrent_requests() -> int:
    raw = (
        os.environ.get("AICAD_XAI_MAX_CONCURRENT")
        or os.environ.get("AI_CAD_XAI_MAX_CONCURRENT")
        or ""
    ).strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _DEFAULT_MAX_CONCURRENT


def _read_timeout_s() -> float:
    raw = (
        os.environ.get("AICAD_XAI_READ_TIMEOUT")
        or os.environ.get("AI_CAD_XAI_READ_TIMEOUT")
        or ""
    ).strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            pass
    return _DEFAULT_READ_TIMEOUT_S


def _read_timeout_retries() -> int:
    raw = (
        os.environ.get("AICAD_XAI_READ_TIMEOUT_RETRIES")
        or os.environ.get("AI_CAD_XAI_READ_TIMEOUT_RETRIES")
        or ""
    ).strip()
    if raw:
        try:
            return max(0, min(3, int(raw)))
        except ValueError:
            pass
    return _DEFAULT_READ_TIMEOUT_RETRIES


def _request_semaphore() -> threading.Semaphore:
    """Process-wide semaphore shared by every XAISubProvider instance."""
    global _request_sem, _sem_capacity
    with _sem_lock:
        if _request_sem is None:
            _sem_capacity = _max_concurrent_requests()
            _request_sem = threading.Semaphore(_sem_capacity)
        return _request_sem


def _reset_request_semaphore_for_tests() -> None:
    """Test hook: drop the cached semaphore so env changes take effect."""
    global _request_sem, _sem_capacity, _sem_in_flight, _sem_peak_in_flight
    global _sem_wait_count, _sem_total_wait_ms
    with _sem_lock:
        _request_sem = None
        _sem_capacity = 0
        _sem_in_flight = 0
        _sem_peak_in_flight = 0
        _sem_wait_count = 0
        _sem_total_wait_ms = 0.0


def semaphore_stats() -> dict:
    """Snapshot occupancy / queue telemetry for tests and live audit."""
    with _sem_lock:
        return {
            "capacity": int(_sem_capacity or _max_concurrent_requests()),
            "in_flight": int(_sem_in_flight),
            "peak_in_flight": int(_sem_peak_in_flight),
            "wait_count": int(_sem_wait_count),
            "total_wait_ms": float(_sem_total_wait_ms),
        }


def _is_read_timeout(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return (
        "timed out" in text
        or "timeout" in text
        or "read operation timed out" in text
        or isinstance(exc, TimeoutError)
    )

# Contract-shaped message for the Heavy-only OAuth API gate (issue #26847).
TIER_DENIED_REASON = (
    "xAI subscription tier not API-enabled (Heavy-only gating as of 2026-05)"
)
TIER_DENIED_HINT = (
    "Use an API key from console.x.ai and switch provider to 'xai', "
    "or upgrade the SuperGrok tier that includes OAuth API access."
)

# progress callback payload keys (also mirrored into subscription_status)
ProgressCb = Callable[[dict], None]


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


def open_verification_url(url: str) -> bool:
    """Open *url* in the user's default browser.

    Tries ``webbrowser.open`` first (works for a desktop GUI process), then
    ``xdg-open`` / ``open`` / ``start`` as a fallback. Returns True if a
    launcher reported success. Never raises — headless envs just return False
    so the GUI can show the URL + user_code for manual open.
    """
    if not url:
        return False
    try:
        if webbrowser.open(str(url)):
            log.info("xai oauth: opened browser via webbrowser.open (%s)", url)
            return True
    except Exception as exc:  # noqa: BLE001 — headless / no display is fine
        log.info("xai oauth: webbrowser.open failed: %s", exc)

    # Fallbacks for environments where webbrowser has no graphical controller.
    candidates: list[list[str]] = []
    if sys.platform.startswith("linux"):
        for bin_name in ("xdg-open", "gio"):
            path = shutil.which(bin_name)
            if path:
                candidates.append(
                    [path, "open", url] if bin_name == "gio" else [path, url]
                )
    elif sys.platform == "darwin":
        path = shutil.which("open")
        if path:
            candidates.append([path, url])
    elif sys.platform == "win32":
        # ``start`` is a shell builtin; use os.startfile when available.
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            log.info("xai oauth: opened browser via os.startfile (%s)", url)
            return True
        except Exception as exc:  # noqa: BLE001
            log.info("xai oauth: os.startfile failed: %s", exc)

    for cmd in candidates:
        try:
            subprocess.Popen(  # noqa: S603 — fixed launcher + user-supplied URL
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log.info("xai oauth: opened browser via %s (%s)", cmd[0], url)
            return True
        except Exception as exc:  # noqa: BLE001
            log.info("xai oauth: launcher %s failed: %s", cmd[0], exc)
    log.warning("xai oauth: could not open a browser for %s (headless?)", url)
    return False


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

    def login(self, progress: ProgressCb | None = None) -> dict:
        """Device-code login (primary xAI path). Opens the verification URL."""
        return self.login_device(open_browser=True, progress=progress)

    def login_device(
        self,
        open_browser: bool = True,
        progress: ProgressCb | None = None,
    ) -> dict:
        """OAuth 2.0 device-code flow against auth.x.ai.

        Prints the verification URL + user code, optionally opens a browser,
        then polls the token endpoint until approved or timeout.

        *progress* (if set) is invoked with a dict of UI-facing fields as soon
        as the device code is issued so the Settings panel can show the code
        and a fallback link while polling continues.
        """
        def _progress(payload: dict) -> None:
            if progress is None:
                return
            try:
                progress(payload)
            except Exception as exc:  # noqa: BLE001 — UI callback must not abort login
                log.warning("xai oauth: progress callback failed: %s", exc)

        log.info("xai oauth: start (open_browser=%s)", open_browser)
        _progress({"stage": "start", "browser_opened": False})

        try:
            token_endpoint = self._discover_token_endpoint()
        except Exception as exc:
            log.error("xai oauth: discovery failed: %s", exc)
            _progress({"stage": "discovery", "error": str(exc)})
            raise

        try:
            device = self._request_device_code()
        except XaiTierDeniedError:
            log.error("xai oauth: device-code request denied (403-tier)")
            _progress({"stage": "device_code", "error": TIER_DENIED_REASON})
            raise
        except Exception as exc:
            log.error("xai oauth: device-code request failed: %s", exc)
            _progress({"stage": "device_code", "error": str(exc)})
            raise

        verification_url = (
            device.get("verification_uri_complete")
            or device.get("verification_uri")
            or XAI_VERIFICATION_URL_FALLBACK
        )
        user_code = str(device.get("user_code") or "")
        expires_in = int(device.get("expires_in") or 900)
        interval = max(1, int(device.get("interval") or 5))
        device_code = str(device["device_code"])

        log.info(
            "xai oauth: device code issued (user_code=%s url=%s expires_in=%s interval=%s)",
            user_code, verification_url, expires_in, interval,
        )
        print(
            f"Go to {verification_url} and approve access"
            + (f" (code: {user_code})" if user_code else "")
        )

        browser_opened = False
        if open_browser:
            browser_opened = open_verification_url(str(verification_url))
            log.info("xai oauth: browser_opened=%s", browser_opened)

        _progress({
            "stage": "awaiting_approval",
            "user_code": user_code,
            "verification_url": str(verification_url),
            "verification_uri": str(
                device.get("verification_uri") or XAI_VERIFICATION_URL_FALLBACK
            ),
            "browser_opened": browser_opened,
            "expires_in": expires_in,
            "interval": interval,
        })

        try:
            payload = self._poll_device_token(
                token_endpoint=token_endpoint,
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        except XaiTierDeniedError:
            log.error("xai oauth: token poll denied (403-tier)")
            _progress({"stage": "poll", "error": TIER_DENIED_REASON})
            raise
        except Exception as exc:
            log.error("xai oauth: token poll/exchange failed: %s", exc)
            _progress({"stage": "poll", "error": str(exc)})
            raise

        try:
            cred = self._store_tokens(payload, token_endpoint=token_endpoint)
        except Exception as exc:
            log.error("xai oauth: store tokens failed: %s", exc)
            _progress({"stage": "exchange", "error": str(exc)})
            raise

        log.info(
            "xai oauth: login ok (email=%s account_id=%s)",
            cred.get("email"), cred.get("account_id"),
        )
        _progress({
            "stage": "connected",
            "user_code": user_code,
            "verification_url": str(verification_url),
            "browser_opened": browser_opened,
            "email": cred.get("email"),
            "account_id": cred.get("account_id"),
        })
        return cred

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
                log.info("xai oauth: discovery token_endpoint=%s", endpoint)
                return endpoint
            log.warning(
                "xai oauth: discovery returned unusable token_endpoint=%r; using fallback",
                endpoint,
            )
        except Exception as exc:  # noqa: BLE001 — offline / discovery down → fallback
            log.warning("xai oauth: discovery failed (%s); using fallback", exc)
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
                log.info("xai oauth: token exchange succeeded")
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
        # Preserve plan across refresh; infer plan/display_name from JWT.
        if self._cred and self._cred.get("plan"):
            cred["plan"] = self._cred.get("plan")
        if self._cred and self._cred.get("display_name") and not cred.get("display_name"):
            cred["display_name"] = self._cred.get("display_name")
        try:
            from . import usage as usage_mod
            cred = usage_mod.enrich_xai_identity(cred)
        except Exception:
            if email:
                cred.setdefault("display_name", email)
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
        log.info("xai oauth: refresh via %s", endpoint)
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
                log.error("xai oauth: refresh 403-tier: %s", body)
                raise XaiTierDeniedError(body) from exc
            if exc.code in (400, 401):
                log.error("xai oauth: refresh failed HTTP %s: %s", exc.code, body)
                raise RuntimeError(
                    "xAI OAuth refresh failed — re-authenticate via "
                    "Settings → Sign in with xAI. "
                    f"(HTTP {exc.code}: {body})"
                ) from exc
            log.error("xai oauth: refresh failed HTTP %s: %s", exc.code, body)
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

    def _message(self, role: str, parts) -> dict:
        if isinstance(parts, str):
            return {"role": role, "content": parts}
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
            with urllib.request.urlopen(req, timeout=_read_timeout_s()) as resp:
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
        # Bound concurrent sibling DL calls (mega-2 timeout storm under fan-out).
        # mega-5: record queue_wait + occupancy so timeouts can be attributed to
        # serialization vs remote load; one bounded retry on read timeout.
        sem = _request_semaphore()
        attempts = 1 + _read_timeout_retries()
        last_exc: BaseException | None = None
        resp: dict | None = None
        for attempt in range(attempts):
            wait_t0 = time.monotonic()
            acquired = sem.acquire()
            wait_ms = (time.monotonic() - wait_t0) * 1000.0
            global _sem_in_flight, _sem_peak_in_flight, _sem_wait_count, _sem_total_wait_ms
            with _sem_lock:
                if wait_ms > 1.0:
                    _sem_wait_count += 1
                    _sem_total_wait_ms += wait_ms
                if acquired:
                    _sem_in_flight += 1
                    if _sem_in_flight > _sem_peak_in_flight:
                        _sem_peak_in_flight = _sem_in_flight
            try:
                if wait_ms > 1.0:
                    log.info(
                        "xai_sub semaphore queue_wait_ms=%.1f attempt=%d "
                        "in_flight=%d capacity=%d",
                        wait_ms, attempt + 1,
                        semaphore_stats()["in_flight"],
                        semaphore_stats()["capacity"],
                    )
                try:
                    resp = self._post_once(body)
                    last_exc = None
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code != 401:
                        body_excerpt = exc.read().decode(
                            "utf-8", errors="replace")[:400]
                        if exc.code == 403:
                            raise XaiTierDeniedError(body_excerpt) from exc
                        raise RuntimeError(
                            f"POST {CHAT_COMPLETIONS_URL} returned HTTP "
                            f"{exc.code}: {body_excerpt}"
                        ) from exc
                    self.refresh()
                    try:
                        resp = self._post_once(body)
                        last_exc = None
                        break
                    except Exception as exc2:  # noqa: BLE001
                        last_exc = exc2
                        if not _is_read_timeout(exc2) or attempt + 1 >= attempts:
                            raise
                        log.warning(
                            "xai_sub read timeout after refresh; retry %d/%d",
                            attempt + 1, attempts - 1,
                        )
                        continue
                except Exception as exc:  # noqa: BLE001 — retry only read timeouts
                    last_exc = exc
                    if not _is_read_timeout(exc) or attempt + 1 >= attempts:
                        raise
                    log.warning(
                        "xai_sub read timeout; retry %d/%d (queue_wait_ms=%.1f)",
                        attempt + 1, attempts - 1, wait_ms,
                    )
                    continue
            finally:
                if acquired:
                    with _sem_lock:
                        _sem_in_flight = max(0, _sem_in_flight - 1)
                    sem.release()
        if last_exc is not None:
            raise last_exc
        if resp is None:
            raise RuntimeError("xAI complete_ex produced no response")

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
