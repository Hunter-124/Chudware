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
XAI_OAUTH_SCOPE = (
    "openid profile email offline_access grok-cli:access api:access "
    "conversations:read conversations:write"
)
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
# Access tokens are short-lived (~6h). Client skew: refresh 5 min early
# (matches omp ACCESS_TOKEN_CLIENT_SKEW_MS). The previous 1h skew forced
# refresh while the access JWT was still valid — and a failed refresh
# (rotated RT not persisted by an external probe) killed usable sessions.
REFRESH_SKEW_MS = 5 * 60 * 1000

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
        self.detail = detail or ""


# ── Multi-account rotation (403/429 fall-through) ────────────────────────────
# Process-local: when one SuperGrok account is tier/spending-limit gated or rate-
# limited, try the next signed-in account before failing the whole provider.
_account_block_lock = threading.Lock()
_blocked_accounts: dict[str, float] = {}  # account_id -> unix-seconds unblock time
_DEFAULT_ACCOUNT_BLOCK_S = 15 * 60


def _now_s() -> float:
    return time.time()


def clear_blocked_accounts_for_tests() -> None:
    """Test hook: drop the process-local account block table."""
    with _account_block_lock:
        _blocked_accounts.clear()


def mark_account_blocked(
    account_id: str | None,
    *,
    seconds: float | None = None,
    reason: str = "",
) -> None:
    """Soft-block *account_id* for a while so multi-account rotation skips it."""
    aid = str(account_id or "").strip()
    if not aid:
        return
    ttl = float(seconds if seconds is not None else _DEFAULT_ACCOUNT_BLOCK_S)
    until = _now_s() + max(30.0, ttl)
    with _account_block_lock:
        prev = float(_blocked_accounts.get(aid) or 0.0)
        _blocked_accounts[aid] = max(prev, until)
    log.info(
        "xai_sub: blocked account %s for %.0fs (%s)",
        aid[:12], max(30.0, ttl), (reason or "tier/rate")[:120],
    )


def is_account_blocked(account_id: str | None) -> bool:
    """True when *account_id* is still inside a soft-block window."""
    aid = str(account_id or "").strip()
    if not aid:
        return False
    now = _now_s()
    with _account_block_lock:
        until = float(_blocked_accounts.get(aid) or 0.0)
        if until <= now:
            if aid in _blocked_accounts:
                _blocked_accounts.pop(aid, None)
            return False
        return True


def _cred_account_id(cred: dict | None) -> str | None:
    if not isinstance(cred, dict):
        return None
    for key in ("account_id", "sub", "user_id"):
        v = cred.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _has_usable_tokens(cred: dict | None) -> bool:
    if not isinstance(cred, dict):
        return False
    return bool(cred.get("access_token") or cred.get("refresh_token"))


def list_candidate_accounts(
    *,
    prefer: str | None = None,
    include_blocked: bool = False,
) -> list[tuple[str, dict]]:
    """Ordered (account_id, cred) candidates for chat completion.

    Prefer *prefer* (or the store's active account), then remaining non-blocked
    signed-in accounts. Empty when no credentials are stored.
    """
    accounts = sub_auth.list_accounts(PROVIDER_ID)
    if not accounts:
        return []
    active = prefer or sub_auth.get_active_account_id(PROVIDER_ID)
    ordered: list[str] = []
    if active:
        ordered.append(str(active))
    for a in accounts:
        aid = str(a.get("account_id") or "")
        if aid and aid not in ordered:
            ordered.append(aid)
    out: list[tuple[str, dict]] = []
    for aid in ordered:
        if not include_blocked and is_account_blocked(aid):
            continue
        cred = sub_auth.load_credential(PROVIDER_ID, account_id=aid)
        if not _has_usable_tokens(cred):
            continue
        assert cred is not None
        # Keep account_id on the dict so rotation + status stay aligned.
        if not cred.get("account_id"):
            cred["account_id"] = aid
        out.append((aid, cred))
    return out


def pick_usable_account(prefer: str | None = None) -> tuple[str, dict] | None:
    """First non-blocked signed-in account, or None."""
    cands = list_candidate_accounts(prefer=prefer, include_blocked=False)
    return cands[0] if cands else None


def provider_is_usable() -> bool:
    """True when at least one signed-in xAI account has tokens (Python usable gate).

    Mirrors the GUI ``providerUsable`` idea for subscription xAI: connected
    credentials make the provider selectable even without a classic API key.
    Soft-blocked accounts still count as signed-in (rotation may clear).
    """
    if list_candidate_accounts(include_blocked=True):
        return True
    # Cold path: legacy flat entry that list_accounts would also see after normalize.
    cred = sub_auth.load_credential(PROVIDER_ID)
    return _has_usable_tokens(cred)


def _is_rate_limit_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "429" in text or "rate limit" in text or "rate_limit" in text:
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        return int(code) == 429
    except (TypeError, ValueError):
        return False


def _is_rotatable_account_error(exc: BaseException) -> bool:
    """Per-account failures that should fall through to the next signed-in account.

    Includes: 403 tier/spending/credits, 429 rate limits, usage-limit phrasing.
    Read timeouts are handled separately: same-account retry first (in
    ``_complete_once_with_retries``), then rotation in ``complete_ex``.
    """
    if isinstance(exc, XaiTierDeniedError):
        return True
    if _is_rate_limit_error(exc):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "tier not api-enabled",
        "personal-team-blocked",
        "spending-limit",
        "spending limit",
        "run out of credits",
        "out of credits",
        "need a grok subscription",
        "usage limit",
        "usage_limit",
        "quota",
        "team_blocked",
        "team blocked",
        "http 403",
        "http 429",
        "http 503",
        "http 502",
        "http 504",
    )
    return any(m in text for m in markers)



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
        self.model = model or DEFAULT_MODEL
        # Prefer a non-blocked signed-in account (multi-account cold restore).
        picked = pick_usable_account()
        if picked is not None:
            aid, cred = picked
            # Align store active pointer when we skip a soft-blocked active account.
            active = sub_auth.get_active_account_id(PROVIDER_ID)
            if active and active != aid:
                try:
                    sub_auth.set_active_account(PROVIDER_ID, aid)
                except Exception:  # noqa: BLE001 — best-effort pointer fix
                    pass
            self._cred = cred
        else:
            # Fall back to whatever is active (may be blocked / expired); complete_ex
            # will surface a clear multi-account error if nothing works.
            self._cred = sub_auth.load_credential(PROVIDER_ID)
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
        # Preserve plan/display_name/last_usage across refresh.
        if self._cred:
            for k in ("plan", "display_name", "last_usage", "last_usage_fetched_at"):
                if self._cred.get(k) is not None and not cred.get(k):
                    cred[k] = self._cred[k]
        try:
            from . import usage as usage_mod
            cred = usage_mod.enrich_xai_identity(cred)
        except Exception:
            if email:
                cred.setdefault("display_name", email)
            elif not cred.get("display_name"):
                # Never leave the UI with a bare UUID account_id as the only label.
                plan = cred.get("plan")
                short = (str(account_id).split("-")[0][:8] if account_id else "")
                if plan and short:
                    cred["display_name"] = f"Grok ({plan} · {short})"
                elif plan:
                    cred["display_name"] = f"Grok ({plan})"
                elif short:
                    cred["display_name"] = f"Grok ({short})"
                else:
                    cred["display_name"] = "Grok"
        sub_auth.save_credential(PROVIDER_ID, cred)
        # Fresh tokens mean this account is usable again.
        aid = _cred_account_id(cred) or account_id
        if aid:
            with _account_block_lock:
                _blocked_accounts.pop(str(aid), None)
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
        aid = _cred_account_id(self._cred)
        log.info("xai oauth: refresh via %s (account=%s)", endpoint, (aid or "")[:12])
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
                mark_account_blocked(aid, reason=f"refresh 403: {body[:80]}")
                raise XaiTierDeniedError(body) from exc
            if exc.code in (400, 401):
                log.error("xai oauth: refresh failed HTTP %s: %s", exc.code, body)
                mark_account_blocked(
                    aid, seconds=5 * 60, reason=f"refresh {exc.code}"
                )
                raise RuntimeError(
                    "xAI OAuth refresh failed — re-authenticate via "
                    "Settings → Sign in with xAI. "
                    f"(HTTP {exc.code}: {body})"
                ) from exc
            if exc.code == 429:
                mark_account_blocked(aid, seconds=5 * 60, reason="refresh 429")
                raise RuntimeError(
                    f"xAI token refresh rate-limited (HTTP 429): {body}"
                ) from exc
            log.error("xai oauth: refresh failed HTTP %s: %s", exc.code, body)
            raise RuntimeError(
                f"xAI token refresh failed (HTTP {exc.code}): {body}"
            ) from exc
        resp.setdefault("refresh_token", self._cred["refresh_token"])
        return self._store_tokens(resp, token_endpoint=endpoint)

    def _bind_account(self, account_id: str, cred: dict) -> None:
        """Switch in-memory + store active account to *account_id*/*cred*."""
        self._cred = dict(cred)
        if not self._cred.get("account_id"):
            self._cred["account_id"] = account_id
        try:
            if sub_auth.get_active_account_id(PROVIDER_ID) != account_id:
                sub_auth.set_active_account(PROVIDER_ID, account_id)
        except Exception:  # noqa: BLE001
            pass

    def _access_jwt_still_valid(self, *, skew_ms: int = 60_000) -> bool:
        """True when the access token's JWT ``exp`` is still in the future."""
        if not self._cred:
            return False
        token = str(self._cred.get("access_token") or "")
        if token.count(".") < 2:
            return False
        claims = sub_auth.decode_jwt_payload(token)
        exp = claims.get("exp")
        try:
            exp_f = float(exp)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return exp_f * 1000.0 > (time.time() * 1000.0 + skew_ms)

    def _ensure_auth(self) -> None:
        """Ensure *self._cred* is present and not expired (refresh if needed).

        On cold restore / multi-account, prefers a non-blocked signed-in account
        over a soft-blocked active pointer. Refresh failures that look
        per-account (403/429) rotate before giving up. If refresh fails with
        invalid_grant but the access JWT is still live, keep using it.
        """
        if not self._cred or not _has_usable_tokens(self._cred):
            picked = pick_usable_account()
            if picked is None:
                raise RuntimeError(
                    "Not logged in to xAI subscription. "
                    "Run login() first (Settings → Sign in with xAI)."
                )
            self._bind_account(picked[0], picked[1])
        elif is_account_blocked(_cred_account_id(self._cred)):
            # Active account soft-blocked — rotate before the request.
            picked = pick_usable_account(prefer=None)
            if picked is None:
                raise RuntimeError(
                    "All signed-in xAI accounts are temporarily blocked "
                    "(tier-gate 403 or rate-limit 429). Sign in another SuperGrok "
                    "account or wait and retry. Hint: " + TIER_DENIED_HINT
                )
            self._bind_account(picked[0], picked[1])

        if not sub_auth.is_expired(self._cred, skew_ms=REFRESH_SKEW_MS):
            return

        # Within skew but JWT may still be fully valid — try refresh, fall back.
        try:
            self.refresh()
            return
        except Exception as exc:  # noqa: BLE001
            if self._access_jwt_still_valid():
                log.warning(
                    "xai oauth: refresh failed but access JWT still valid; "
                    "continuing without refresh: %s",
                    exc,
                )
                return
            if not _is_rotatable_account_error(exc):
                # Try rotating on invalid_grant too (per-account RT death).
                text = f"{type(exc).__name__}: {exc}".lower()
                if (
                    "invalid_grant" in text
                    or "revoked" in text
                    or "re-authenticate" in text
                ):
                    failed = _cred_account_id(self._cred)
                    mark_account_blocked(
                        failed,
                        seconds=30 * 60,
                        reason=f"refresh dead: {exc}"[:120],
                    )
                    if self._activate_next_account(failed_id=failed):
                        return
                raise
            failed = _cred_account_id(self._cred)
            mark_account_blocked(failed, reason=f"ensure_auth refresh: {exc}"[:120])
            if not self._activate_next_account(failed_id=failed):
                raise

    def _activate_next_account(self, *, failed_id: str | None) -> bool:
        """Mark *failed_id* blocked and switch to the next candidate. True if switched."""
        if failed_id:
            mark_account_blocked(failed_id, reason="complete_ex rotate")
        for aid, cred in list_candidate_accounts(include_blocked=False):
            if failed_id and aid == failed_id:
                continue
            log.info(
                "xai_sub: rotating account %s → %s after per-account failure",
                (failed_id or "?")[:12], aid[:12],
            )
            self._bind_account(aid, cred)
            try:
                if sub_auth.is_expired(self._cred, skew_ms=REFRESH_SKEW_MS):
                    self.refresh()
            except Exception as exc:  # noqa: BLE001 — try next account
                log.warning(
                    "xai_sub: refresh on rotated account %s failed: %s",
                    aid[:12], exc,
                )
                mark_account_blocked(aid, reason=f"rotate-refresh: {exc}"[:120])
                continue
            return True
        return False

    # ── Request helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        if not self._cred or not self._cred.get("access_token"):
            raise RuntimeError(
                "Not logged in to xAI subscription. "
                "Run login() first (Settings → Sign in with xAI)."
            )
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
                raw = resp.read().decode("utf-8")
                rh = {
                    k.lower(): v
                    for k, v in (resp.headers.items() if resp.headers else [])
                }
                parsed = json.loads(raw)
                # Stash rate-limit headers for Settings usage bars (§4.5).
                try:
                    from . import usage as usage_mod
                    usage_mod.record_xai_ratelimit_headers(
                        self._cred,
                        rh,
                        account_id=_cred_account_id(self._cred),
                    )
                except Exception:  # noqa: BLE001 — never fail chat on usage persist
                    pass
                return parsed
        except urllib.error.HTTPError as exc:
            body_excerpt = exc.read().decode("utf-8", errors="replace")[:400]
            # Attach status for rotation classifiers (urllib HTTPError has .code).
            if exc.code == 401:
                raise  # caller refreshes + retries same account
            if exc.code == 403:
                raise XaiTierDeniedError(body_excerpt) from exc
            if exc.code == 429:
                err = RuntimeError(
                    f"POST {CHAT_COMPLETIONS_URL} returned HTTP 429: {body_excerpt}"
                )
                err.status_code = 429  # type: ignore[attr-defined]
                raise err from exc
            # Timeouts / 5xx / empty can mean pool exhaustion on SuperGrok —
            # surface status so multi-account rotation can decide.
            err = RuntimeError(
                f"POST {CHAT_COMPLETIONS_URL} returned HTTP {exc.code}: "
                f"{body_excerpt}"
            )
            err.status_code = int(exc.code)  # type: ignore[attr-defined]
            raise err from exc

    def _complete_once_with_retries(
        self,
        body: dict,
        *,
        attempts: int,
    ) -> dict:
        """POST chat completions for the CURRENT account with timeout/401 retries."""
        sem = _request_semaphore()
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
                        # _post_once already re-raises non-401 as RuntimeError /
                        # XaiTierDeniedError; this path is defensive if a bare
                        # HTTPError bubbles (e.g. tests monkeypatch).
                        body_excerpt = ""
                        try:
                            body_excerpt = exc.read().decode(
                                "utf-8", errors="replace")[:400]
                        except Exception:  # noqa: BLE001
                            body_excerpt = str(exc)
                        if exc.code == 403:
                            raise XaiTierDeniedError(body_excerpt) from exc
                        if exc.code == 429:
                            err = RuntimeError(
                                f"POST {CHAT_COMPLETIONS_URL} returned HTTP "
                                f"429: {body_excerpt}"
                            )
                            err.status_code = 429  # type: ignore[attr-defined]
                            raise err from exc
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
                        if _is_rotatable_account_error(exc2):
                            raise
                        if not _is_read_timeout(exc2) or attempt + 1 >= attempts:
                            raise
                        log.warning(
                            "xai_sub read timeout after refresh; retry %d/%d",
                            attempt + 1, attempts - 1,
                        )
                        continue
                except Exception as exc:  # noqa: BLE001 — retry only read timeouts
                    last_exc = exc
                    if _is_rotatable_account_error(exc):
                        raise
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
        return resp

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
        # Normalize stale hyphen model ids (catalog / gym) to dotted API id.
        model = str(self.model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        if model == "grok-4-5":
            model = "grok-4.5"
            self.model = model
        body = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
        }
        # Bound concurrent sibling DL calls (mega-2 timeout storm under fan-out).
        # mega-5: record queue_wait + occupancy so timeouts can be attributed to
        # serialization vs remote load; one bounded retry on read timeout.
        # Multi-account: on per-account 403/429 fall through to the next signed-in
        # account before failing the provider.
        attempts = 1 + _read_timeout_retries()
        tried: list[str] = []
        last_exc: BaseException | None = None
        resp: dict | None = None
        max_account_hops = max(1, len(list_candidate_accounts(include_blocked=True)) or 1)

        for _hop in range(max_account_hops):
            aid = _cred_account_id(self._cred) or sub_auth.get_active_account_id(PROVIDER_ID) or "?"
            if aid not in tried:
                tried.append(str(aid))
            try:
                resp = self._complete_once_with_retries(body, attempts=attempts)
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                # Same-account retries already exhausted inside
                # _complete_once_with_retries. Rotate on per-account limits OR
                # read timeouts (SuperGrok often hangs instead of returning 429).
                rotatable = _is_rotatable_account_error(exc) or _is_read_timeout(exc)
                if not rotatable:
                    raise
                if _is_rate_limit_error(exc) or _is_read_timeout(exc):
                    block_s = 5 * 60
                else:
                    block_s = None  # default 15 min for tier/spend
                mark_account_blocked(
                    aid,
                    seconds=block_s,
                    reason=str(exc)[:120],
                )
                if not self._activate_next_account(failed_id=str(aid) if aid != "?" else None):
                    break
                continue

        if last_exc is not None and resp is None:
            if _is_rotatable_account_error(last_exc) or _is_read_timeout(last_exc):
                n = len(tried)
                detail = str(last_exc)
                raise XaiTierDeniedError(
                    f"all {n} signed-in account(s) failed "
                    f"(tried {', '.join(t[:8] for t in tried)}): {detail}"
                ) from last_exc
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
        # mega-6: capture token usage from OpenAI-shaped response body.
        try:
            from . import token_usage as _tu  # noqa: PLC0415
            u = _tu.extract_openai_usage(resp)
            _tu.record_usage(
                provider=self.name,
                model=str(self.model or ""),
                prompt_tokens=u.get("prompt_tokens"),
                completion_tokens=u.get("completion_tokens"),
                total_tokens=u.get("total_tokens"),
                source="api",
            )
        except Exception:  # noqa: BLE001
            pass
        return content, thinking

    def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> str:
        return self.complete_ex(system, messages, max_tokens=max_tokens)[0]
