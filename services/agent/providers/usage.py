"""Subscription usage / identity fetchers (contracts/providers.md §4.5).

Normalizes per-provider product quota endpoints into:

    {
      "display_name": str|None,
      "plan": str|None,
      "usage": [
        {"window", "label", "used_fraction", "resets_at", "status"}
      ],
      # optional extras for UI degrade paths:
      "dashboard_url": str|None,
      "source": "api"|"jwt_inference"|"browser_scrape"|"unavailable"|...,
    }

Poll policy: cache per (provider_store_id, account_id) for CACHE_TTL_S (5 min).
A 429 from a usage endpoint freezes that account's fetch until the cache TTL
elapses (never retry inside the window). Network failures degrade to last-good
cache or an empty usage list — never invent remaining quotas.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from . import sub_auth
from . import subscription_login as sl

log = logging.getLogger("agent.providers.usage")

CACHE_TTL_S = 5 * 60  # contracts/providers.md §4.5: poll <= 5 min

# Friendly provider id -> credential store key (mirrors subscription_login._PROVIDERS).
_FRIENDLY_TO_CRED = {
    "codex": "openai-codex",
    "claude": "anthropic-sub",
    "cursor": "cursor",
    "xai": "xai-sub",
    "kimi": "kimi-sub",
}

DASHBOARD_URLS = {
    "claude": "https://claude.ai/settings/usage",
    "codex": "https://chatgpt.com/",
    "cursor": "https://cursor.com/dashboard/usage",
    "kimi": "https://www.kimi.com/code",
    "xai": "https://grok.com/?_s=usage",
}

# xAI usage sources. Primary path hits the official cli-chat-proxy credits
# endpoint (same as grok-build ``GET /v1/billing?format=credits``) plus
# ``GET /v1/user?include=subscription`` for live tier. Rate-limit headers
# from api.x.ai remain secondary enrichment. JWT has NO email/name claims
# (only sub/tier/team_id); OIDC userinfo fills identity. Browser scrape is a
# degrade path when the billing path returns empty windows.
XAI_SOURCE_JWT = "jwt_inference"
XAI_SOURCE_BROWSER = "browser_scrape"
XAI_SOURCE_API = "api"
# Prefer live billing + userinfo. Override with env AICAD_XAI_USAGE_SOURCE.
XAI_USAGE_SOURCE = XAI_SOURCE_API

XAI_USERINFO_URL = "https://auth.x.ai/oauth2/userinfo"
XAI_ME_URL = "https://api.x.ai/v1/me"
XAI_MODELS_URL = "https://api.x.ai/v1/models"
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
# Official SuperGrok credits source (grok-build extensions/billing.rs).
XAI_CLI_CHAT_PROXY_BASE = "https://cli-chat-proxy.grok.com/v1"
XAI_BILLING_URL = f"{XAI_CLI_CHAT_PROXY_BASE}/billing?format=credits"
XAI_USER_SUB_URL = f"{XAI_CLI_CHAT_PROXY_BASE}/user?include=subscription"
# Token-auth header value from grok-build auth/config.rs ``token_header``.
XAI_TOKEN_AUTH_VALUE = "xai-grok-cli"
# Brief per-process cache for billing GETs (Settings polls; avoid hammering).
XAI_BILLING_CACHE_TTL_S = 60

# Anthropic OAuth product surfaces
_ANTHROPIC_OAUTH_BASE = "https://api.anthropic.com/api/oauth"
_ANTHROPIC_BETA = "oauth-2025-04-20"
_ANTHROPIC_UA = "claude-cli/2.1.165 (external, local-agent)"

# Codex agentic pool
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_CODEX_UA = "codex-cli"

# Cursor personal OAuth
_CURSOR_USAGE_URL = "https://api2.cursor.sh/auth/usage"
_CURSOR_STRIPE_URL = "https://api2.cursor.sh/auth/full_stripe_profile"
# Real period spend / included limits (Connect JSON unary) — /auth/usage often
# returns gpt-4 buckets with maxRequestUsage=null, which leaves Settings empty.
_CURSOR_PERIOD_USAGE_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
)
_CURSOR_PLAN_INFO_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetPlanInfo"
)

# In-process cache: key -> {fetched_at, rate_limited_until, row}
_cache: dict[str, dict[str, Any]] = {}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _cache_key(cred_id: str, account_id: str) -> str:
    return f"{cred_id}::{account_id}"


def _now() -> float:
    return time.time()


def _iso_from_ms(ms: float | int | None) -> str | None:
    if ms is None:
        return None
    try:
        ms_f = float(ms)
    except (TypeError, ValueError):
        return None
    if ms_f <= 0:
        return None
    # Accept seconds or milliseconds.
    if ms_f < 1_000_000_000_000:
        ms_f *= 1000.0
    try:
        return datetime.fromtimestamp(ms_f / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _iso_from_seconds(sec: float | int | None, *, now: float | None = None) -> str | None:
    if sec is None:
        return None
    try:
        sec_f = float(sec)
    except (TypeError, ValueError):
        return None
    if sec_f <= 0:
        return None
    # Absolute unix timestamp vs relative offset heuristic.
    if sec_f > 1_000_000_000:  # absolute
        return _iso_from_ms(sec_f * 1000.0)
    base = now if now is not None else _now()
    return _iso_from_ms((base + sec_f) * 1000.0)


def _parse_iso_to_iso(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    try:
        # Normalize to Zulu if parseable.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return value  # pass through unknown formats


def _clamp_fraction(value: float | int | None) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f > 1.0 and f <= 100.0:
        f = f / 100.0
    if f < 0:
        f = 0.0
    if f > 1.0:
        f = 1.0
    return f


def _status_from_fraction(frac: float | None, *, limit_reached: bool = False) -> str:
    """Map utilization to contract status: ok|warn|blocked|unknown."""
    if limit_reached:
        return "blocked"
    if frac is None:
        return "unknown"
    if frac >= 1.0:
        return "blocked"
    if frac >= 0.9:
        return "warn"
    return "ok"


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value != value:  # NaN
            return None
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _http_get(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = 20.0,
) -> tuple[int, Any, dict[str, str]]:
    """GET *url*; return (status, parsed_json_or_None, response_headers_lower).

    Does not raise on HTTP error statuses — callers decide how to handle 401/403/429.
    Network-level failures raise.
    """
    hdrs = dict(headers or {})
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(getattr(resp, "status", 200) or 200)
            rh = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read() if hasattr(exc, "read") else b""
        rh = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
    body: Any = None
    if raw:
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            body = None
    return status, body, rh


def _http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    *,
    data: bytes | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any, dict[str, str]]:
    """HTTP request; return (status, parsed_json_or_None, response_headers_lower).

    Does not raise on HTTP error statuses. Network-level failures raise.
    """
    hdrs = dict(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(getattr(resp, "status", 200) or 200)
            rh = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read() if hasattr(exc, "read") else b""
        rh = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
    body: Any = None
    if raw:
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            body = None
    return status, body, rh


def _xai_auth_headers(cred: dict, *, proxy: bool = False) -> dict[str, str]:
    """Bearer headers for xAI endpoints.

    *proxy*=True adds the cli-chat-proxy session markers required by
    grok-build (``X-XAI-Token-Auth: xai-grok-cli``, client version/mode,
    ``x-authenticateresponse``). Tokens need ``conversations:read/write``
    scopes in addition to ``grok-cli:access`` or the proxy returns 403.
    """
    token = str(cred.get("access_token") or "").strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if proxy:
        headers["X-XAI-Token-Auth"] = XAI_TOKEN_AUTH_VALUE
        headers["x-grok-client-version"] = "0.1.210"
        headers["x-grok-client-mode"] = "headless"
        headers["x-authenticateresponse"] = "authenticate-response"
        headers["x-grok-client-identifier"] = "billet"
        uid = cred.get("account_id") or cred.get("user_id") or cred.get("sub")
        if uid is not None and str(uid).strip():
            headers["x-userid"] = str(uid).strip()
    return headers


def _usage_from_ratelimit_headers(headers: dict[str, str]) -> list[dict]:
    """Map xAI ``x-ratelimit-*`` response headers into §4.5 usage windows.

    Live chat/completions responses carry:
      x-ratelimit-limit-requests / remaining-requests
      x-ratelimit-limit-tokens / remaining-tokens
    Returns [] when no usable pair is present.
    """
    if not headers:
        return []
    h = {str(k).lower(): str(v) for k, v in headers.items()}

    def _pair(limit_key: str, rem_key: str) -> tuple[float | None, float | None]:
        return _to_number(h.get(limit_key)), _to_number(h.get(rem_key))

    out: list[dict] = []
    lim_r, rem_r = _pair("x-ratelimit-limit-requests", "x-ratelimit-remaining-requests")
    if lim_r is not None and lim_r > 0 and rem_r is not None:
        used = _clamp_fraction((lim_r - rem_r) / lim_r)
        resets = None
        for rk in (
            "x-ratelimit-reset-requests",
            "x-ratelimit-reset",
            "x-ratelimit-reset-tokens",
        ):
            if h.get(rk) is None:
                continue
            n = _to_number(h.get(rk))
            if n is None:
                continue
            # Heuristic: values > 1e12 look like ms, > 1e9 epoch s, else delta s.
            if n > 1e12:
                resets = _iso_from_ms(n)
            elif n > 1e9:
                resets = _iso_from_ms(n * 1000.0)
            else:
                resets = _iso_from_seconds(n)
            break
        out.append(_usage_row("requests", "Request pool", used, resets, None))
    lim_t, rem_t = _pair("x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens")
    if lim_t is not None and lim_t > 0 and rem_t is not None:
        used = _clamp_fraction((lim_t - rem_t) / lim_t)
        out.append(_usage_row("tokens", "Token pool", used, None, None))
    return out


def record_xai_ratelimit_headers(
    cred: dict | None,
    headers: dict[str, str] | None,
    *,
    account_id: str | None = None,
) -> list[dict]:
    """Persist rate-limit headers from a live completion onto the credential.

    Called by :mod:`xai_sub_provider` after each chat round-trip so Settings can
    show rate-limit windows as secondary data alongside billing credits.
    Returns the normalized usage rows (may be empty).
    """
    if not isinstance(cred, dict) or not headers:
        return []
    rows = _usage_from_ratelimit_headers(headers)
    if not rows:
        return []
    aid = (
        account_id
        or cred.get("account_id")
        or sub_auth.get_active_account_id("xai-sub")
    )
    try:
        persist_identity(
            "xai-sub",
            str(aid) if aid else None,
            usage=rows,
            usage_fetched_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("xai: persist rate-limit headers failed: %s", exc)
    return rows


def _usage_row(
    window: str,
    label: str,
    used_fraction: float | None,
    resets_at: str | None,
    status: str | None = None,
) -> dict:
    frac = _clamp_fraction(used_fraction)
    return {
        "window": window,
        "label": label,
        "used_fraction": frac if frac is not None else None,
        "resets_at": resets_at,
        "status": status or _status_from_fraction(frac),
    }


def _base_row(
    provider: str,
    account_id: str,
    *,
    display_name: str | None = None,
    plan: str | None = None,
    usage: list | None = None,
    dashboard_url: str | None = None,
    source: str | None = None,
    email: str | None = None,
) -> dict:
    row = {
        "provider": provider,
        "account_id": account_id,
        "display_name": display_name,
        "plan": plan,
        "usage": list(usage or []),
    }
    if dashboard_url is not None:
        row["dashboard_url"] = dashboard_url
    elif provider in DASHBOARD_URLS:
        row["dashboard_url"] = DASHBOARD_URLS[provider]
    if source is not None:
        row["source"] = source
    if email is not None:
        row["email"] = email
    return row


def _looks_like_uuid(value: str | None) -> bool:
    """True when *value* is a bare UUID (common xAI ``sub`` / account_id)."""
    if not value or not isinstance(value, str):
        return False
    s = value.strip()
    if len(s) != 36:
        return False
    # 8-4-4-4-12 hex with hyphens
    parts = s.split("-")
    if len(parts) != 5 or [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    try:
        int(s.replace("-", ""), 16)
    except ValueError:
        return False
    return True


def _looks_like_acct_slug(value: str | None) -> bool:
    """True for derive_account_id timestamp fallbacks like ``acct-1784192914``."""
    if not value or not isinstance(value, str):
        return False
    s = value.strip()
    if not s.startswith("acct-"):
        return False
    rest = s[5:]
    return bool(rest) and rest.isdigit()


def _short_id(value: str | None, *, n: int = 8) -> str:
    if not value:
        return ""
    s = str(value).strip()
    if _looks_like_uuid(s):
        return s.split("-", 1)[0][:n]
    if _looks_like_acct_slug(s):
        return s[5:][:n]
    return s[:n]


def _merge_identity(row: dict, cred: dict) -> dict:
    """Fill missing display_name/plan/email from the credential store."""
    if not row.get("display_name"):
        row["display_name"] = cred.get("display_name") or cred.get("email")
    # Replace bare UUID / acct-slug labels when the credential has a better name.
    dn = row.get("display_name")
    if isinstance(dn, str) and (_looks_like_uuid(dn) or _looks_like_acct_slug(dn)):
        better = cred.get("display_name") or cred.get("email")
        if isinstance(better, str) and better.strip() and not (
            _looks_like_uuid(better) or _looks_like_acct_slug(better)
        ):
            row["display_name"] = better.strip()
    if not row.get("plan"):
        row["plan"] = cred.get("plan")
    if not row.get("email") and cred.get("email"):
        row["email"] = cred.get("email")
    return row


def persist_identity(
    cred_id: str,
    account_id: str | None,
    *,
    display_name: str | None = None,
    plan: str | None = None,
    email: str | None = None,
    usage: list | None = None,
    usage_fetched_at: float | None = None,
) -> None:
    """Write identity (+ optional last-known usage) onto a stored credential.

    Tokens are never clobbered. Usage snapshots let Settings restore bars after a
    cold restart until the next live fetch succeeds (Claude "Usage unavailable"
    after restart).
    """
    cred = sub_auth.load_credential(cred_id, account_id=account_id)
    if not cred:
        return
    changed = False
    if display_name and not cred.get("display_name"):
        cred["display_name"] = display_name
        changed = True
    elif display_name and cred.get("display_name") != display_name:
        # Prefer human labels over bare UUID / acct-* leftovers.
        prev = cred.get("display_name")
        if (
            _looks_like_uuid(prev)
            or _looks_like_acct_slug(prev)
            or prev != display_name
        ):
            # Don't overwrite a good email/name with a weaker UUID fallback.
            if not (
                (_looks_like_uuid(display_name) or _looks_like_acct_slug(display_name))
                and prev
                and not (_looks_like_uuid(prev) or _looks_like_acct_slug(prev))
            ):
                cred["display_name"] = display_name
                changed = True
    if plan and cred.get("plan") != plan:
        cred["plan"] = plan
        changed = True
    if email and not cred.get("email"):
        cred["email"] = email
        changed = True
    if usage:
        # Only snapshot non-empty windows — empty means "unknown", not "zero".
        cred["last_usage"] = list(usage)
        cred["last_usage_fetched_at"] = float(
            usage_fetched_at if usage_fetched_at is not None else _now()
        )
        changed = True
    if changed:
        aid = account_id or sub_auth.get_active_account_id(cred_id)
        # Identity/usage only — never rewrite access/refresh tokens. A stale
        # load_credential snapshot racing an xAI RT rotation was the invalid_grant
        # footgun (rotated RT clobbered back to the revoked one).
        identity = {
            k: cred[k]
            for k in (
                "display_name",
                "plan",
                "email",
                "last_usage",
                "last_usage_fetched_at",
                "account_id",
            )
            if k in cred
        }
        sub_auth.save_credential(
            cred_id,
            identity,
            account_id=aid,
            set_active=False,
            replace_tokens=False,
        )

# ── Per-provider fetchers ────────────────────────────────────────────────────


def fetch_anthropic_usage(cred: dict) -> dict:
    """Claude Pro/Max: /api/oauth/usage (+ profile for identity)."""
    token = cred.get("access_token") or ""
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": _ANTHROPIC_BETA,
        "Accept": "application/json",
        "User-Agent": _ANTHROPIC_UA,
    }
    status, body, _rh = _http_get(f"{_ANTHROPIC_OAUTH_BASE}/usage", headers)
    if status == 429:
        raise _RateLimited()
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"anthropic usage HTTP {status}")

    usage: list[dict] = []

    def _bucket(key: str, window: str, label: str) -> None:
        b = body.get(key)
        if not isinstance(b, dict):
            return
        util = _to_number(b.get("utilization"))
        # utilization may be 0..1 or 0..100
        frac = _clamp_fraction(util)
        resets = _parse_iso_to_iso(b.get("resets_at") if isinstance(b.get("resets_at"), str) else None)
        if frac is None and resets is None:
            return
        usage.append(_usage_row(window, label, frac, resets))

    _bucket("five_hour", "5h", "5 hour")
    _bucket("seven_day", "7d", "7 day")
    _bucket("seven_day_opus", "7d_opus", "7 day (Opus)")
    _bucket("seven_day_sonnet", "7d_sonnet", "7 day (Sonnet)")

    # Modern model-scoped weekly caps via limits[]
    limits = body.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            if entry.get("is_active") is False:
                continue
            kind = entry.get("kind") or ""
            pct = _to_number(entry.get("percent"))
            frac = _clamp_fraction(pct)
            resets = _parse_iso_to_iso(
                entry.get("resets_at") if isinstance(entry.get("resets_at"), str) else None
            )
            if frac is None and resets is None:
                continue
            display = None
            scope = entry.get("scope")
            if isinstance(scope, dict):
                model = scope.get("model")
                if isinstance(model, dict) and isinstance(model.get("display_name"), str):
                    display = model["display_name"].strip() or None
            if kind == "session" and not any(u["window"] == "5h" for u in usage):
                usage.append(_usage_row("5h", "5 hour", frac, resets))
            elif kind == "weekly_all" and not any(u["window"] == "7d" for u in usage):
                usage.append(_usage_row("7d", "7 day", frac, resets))
            elif kind == "weekly_scoped" and display:
                slug = "".join(ch if ch.isalnum() else "-" for ch in display.lower()).strip("-")
                usage.append(_usage_row(f"7d_{slug or 'scoped'}", f"7 day ({display})", frac, resets))

    display_name = cred.get("display_name")
    plan = cred.get("plan")
    email = cred.get("email")
    account_id = cred.get("account_id")

    # Profile fill (best-effort; never fail the usage row on profile errors).
    try:
        p_status, p_body, _ = _http_get(f"{_ANTHROPIC_OAUTH_BASE}/profile", headers)
        if p_status == 200 and isinstance(p_body, dict):
            email = (
                email
                or (p_body.get("email") if isinstance(p_body.get("email"), str) else None)
                or ((p_body.get("account") or {}).get("email") if isinstance(p_body.get("account"), dict) else None)
            )
            profile_name = None
            if isinstance(p_body.get("display_name"), str) and p_body["display_name"].strip():
                profile_name = p_body["display_name"].strip()
            elif isinstance(p_body.get("name"), str) and p_body["name"].strip():
                profile_name = p_body["name"].strip()
            # Prefer a real profile name over a previously email-only display_name.
            if profile_name:
                display_name = profile_name
            elif not display_name:
                display_name = email
            if not account_id:
                account_id = (
                    p_body.get("uuid")
                    or p_body.get("account_id")
                    or ((p_body.get("account") or {}).get("uuid") if isinstance(p_body.get("account"), dict) else None)
                )
            # subscriptionType / rateLimitTier occasionally appear on profile
            if not plan:
                for k in ("subscriptionType", "subscription_type", "plan", "rateLimitTier", "rate_limit_tier"):
                    v = p_body.get(k)
                    if isinstance(v, str) and v.strip():
                        plan = v.strip()
                        break
                    if isinstance(v, dict):
                        # e.g. rateLimitTier nested
                        for vv in v.values():
                            if isinstance(vv, str) and vv.strip():
                                plan = vv.strip()
                                break
                        if plan:
                            break
    except Exception as exc:  # noqa: BLE001
        log.debug("anthropic profile fetch failed: %s", exc)

    return {
        "display_name": display_name,
        "plan": plan,
        "usage": usage,
        "email": email,
        "account_id": account_id,
        "source": "api",
        "dashboard_url": DASHBOARD_URLS["claude"],
    }


def fetch_codex_usage(cred: dict) -> dict:
    """ChatGPT/Codex Plus/Pro: /backend-api/wham/usage + ChatGPT-Account-Id."""
    token = cred.get("access_token") or ""
    account_id = cred.get("account_id")
    if not account_id and token:
        claims = sub_auth.decode_jwt_payload(token)
        account_id = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": _CODEX_UA,
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = str(account_id)

    status, body, _rh = _http_get(_CODEX_USAGE_URL, headers)
    if status == 429:
        raise _RateLimited()
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"codex usage HTTP {status}")

    plan = body.get("plan_type") if isinstance(body.get("plan_type"), str) else cred.get("plan")
    rate = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}
    limit_reached = bool(rate.get("limit_reached")) if rate else False
    now = _now()
    usage: list[dict] = []

    def _window(key: str, window_id: str, fallback_label: str) -> None:
        w = rate.get(key) if rate else None
        if not isinstance(w, dict):
            return
        used_pct = _to_number(w.get("used_percent"))
        # used_percent is documented as 0..100; also accept 0..1 fractions.
        if used_pct is None:
            frac = None
        elif used_pct <= 1.0:
            frac = used_pct
        else:
            frac = _clamp_fraction(used_pct / 100.0)
        limit_s = _to_number(w.get("limit_window_seconds"))
        if limit_s is not None and limit_s >= 86_400:
            days = max(1, int(round(limit_s / 86_400)))
            label = f"{days} day" if days == 1 else f"{days} days"
            wid = f"{days}d"
        elif limit_s is not None:
            hours = max(1, int(round(limit_s / 3600)))
            label = f"{hours} hour" if hours == 1 else f"{hours} hours"
            wid = f"{hours}h"
        else:
            label = fallback_label
            wid = window_id
        resets = None
        if w.get("reset_at") is not None:
            resets = _iso_from_ms(w.get("reset_at")) if _to_number(w.get("reset_at")) else None
            if resets is None:
                resets = _iso_from_seconds(w.get("reset_at"), now=now)
        if resets is None and w.get("reset_after_seconds") is not None:
            resets = _iso_from_seconds(w.get("reset_after_seconds"), now=now)
        st = _status_from_fraction(frac, limit_reached=limit_reached and key == "primary_window")
        usage.append(_usage_row(window_id, label, frac, resets, st))

    _window("primary_window", "primary", "Primary window")
    _window("secondary_window", "secondary", "Secondary window")

    email = cred.get("email")
    if not email and token:
        claims = sub_auth.decode_jwt_payload(token)
        email = ((claims.get("https://api.openai.com/profile") or {}).get("email") or "").lower().strip() or None

    return {
        "display_name": cred.get("display_name") or email,
        "plan": plan,
        "usage": usage,
        "email": email,
        "account_id": account_id or cred.get("account_id"),
        "source": "api",
        "dashboard_url": DASHBOARD_URLS["codex"],
    }


def _ms_epoch_to_iso(value: Any) -> str | None:
    """Parse Cursor dashboard epoch fields (ms as int/str) to ISO-Z."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().isdigit():
        return _iso_from_ms(int(value.strip()))
    if isinstance(value, (int, float)):
        return _iso_from_ms(value)
    if isinstance(value, str):
        return _parse_iso_to_iso(value)
    return None


def _parse_cursor_period_usage(body: dict) -> list[dict]:
    """Map DashboardService/GetCurrentPeriodUsage JSON → §4.5 usage windows.

    Live fields (2026): planUsage.totalPercentUsed / autoPercentUsed /
    apiPercentUsed (0..100), planUsage.includedSpend vs limit (cents),
    billingCycleEnd as epoch-ms string.
    """
    if not isinstance(body, dict):
        return []
    resets = (
        _ms_epoch_to_iso(body.get("billingCycleEnd"))
        or _ms_epoch_to_iso(body.get("billingCycleStart"))
    )
    plan_usage = body.get("planUsage") if isinstance(body.get("planUsage"), dict) else {}
    rows: list[dict] = []

    def _pct_field(key: str, window: str, label: str) -> None:
        pct = _to_number(plan_usage.get(key))
        if pct is None:
            return
        # Server sends 0..100 percentages (sometimes already 0..1).
        frac = pct / 100.0 if pct > 1.0 else pct
        rows.append(_usage_row(window, label, _clamp_fraction(frac), resets))

    _pct_field("totalPercentUsed", "plan", "Plan usage")
    _pct_field("autoPercentUsed", "auto", "Auto models")
    _pct_field("apiPercentUsed", "api", "API / premium models")

    # Spend cents when percent fields absent: includedSpend / limit.
    if not rows:
        used_cents = (
            _to_number(plan_usage.get("includedSpend"))
            or _to_number(plan_usage.get("totalSpend"))
        )
        limit_cents = _to_number(plan_usage.get("limit"))
        if used_cents is not None and limit_cents and limit_cents > 0:
            rows.append(
                _usage_row(
                    "plan",
                    "Plan usage",
                    _clamp_fraction(used_cents / limit_cents),
                    resets,
                )
            )

    # Optional on-demand / spend-limit bucket.
    spend = body.get("spendLimitUsage") if isinstance(body.get("spendLimitUsage"), dict) else {}
    if spend:
        used = (
            _to_number(spend.get("used"))
            or _to_number(spend.get("spendUsed"))
            or _to_number(spend.get("totalSpend"))
        )
        limit = (
            _to_number(spend.get("limit"))
            or _to_number(spend.get("spendLimit"))
            or _to_number(spend.get("amountLimit"))
        )
        if used is not None and limit and limit > 0:
            rows.append(
                _usage_row(
                    "spend_limit",
                    "Spend limit",
                    _clamp_fraction(used / limit),
                    resets,
                )
            )

    return rows


def _fetch_cursor_period_usage(token: str) -> list[dict]:
    """POST DashboardService/GetCurrentPeriodUsage (Connect JSON unary)."""
    if not token:
        return []
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connect-Protocol-Version": "1",
        "x-cursor-client-version": "cli-2026.01.09-231024f",
        "x-cursor-client-type": "cli",
    }
    try:
        status, body, _ = _http_request(
            "POST",
            _CURSOR_PERIOD_USAGE_URL,
            headers,
            data=b"{}",
            timeout=20.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("cursor period usage POST failed: %s", exc)
        return []
    if status != 200 or not isinstance(body, dict):
        log.debug("cursor period usage HTTP %s", status)
        return []
    return _parse_cursor_period_usage(body)


def fetch_cursor_usage(cred: dict) -> dict:
    """Cursor Pro usage for Settings.

    Primary quota: POST DashboardService/GetCurrentPeriodUsage (real
    total/auto/api percent used + billing cycle end).

    Secondary: GET /auth/usage request buckets when they include limits.

    Identity: GET /auth/full_stripe_profile for plan/email/display_name.

    Some tokens return 401/403 on one endpoint while another still answers —
    we still return a human display_name/plan so Settings never shows bare
    ``acct-…``.
    """
    token = cred.get("access_token") or ""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    plan = cred.get("plan")
    email = cred.get("email")
    display_name = cred.get("display_name") or email
    account_id = cred.get("account_id")
    usage: list[dict] = []
    source = "api"

    # JWT claims first (no network) — often carries email/sub even when profile fails.
    if token:
        claims = sub_auth.decode_jwt_payload(token)
        if not email:
            for key in ("email", "preferred_username", "unique_name", "upn"):
                v = claims.get(key)
                if isinstance(v, str) and "@" in v:
                    email = v.strip().lower()
                    break
            nested = claims.get("profile") if isinstance(claims.get("profile"), dict) else {}
            if not email and isinstance(nested.get("email"), str):
                email = nested["email"].strip().lower()
        if not account_id:
            for key in ("sub", "user_id", "userId", "id"):
                v = claims.get(key)
                if v is not None and str(v).strip():
                    account_id = str(v).strip()
                    break
        if not display_name:
            for key in ("name", "display_name", "displayName", "preferred_username"):
                v = claims.get(key)
                if isinstance(v, str) and v.strip():
                    display_name = v.strip()
                    break

    usage_error: str | None = None
    try:
        status, body, _rh = _http_get(_CURSOR_USAGE_URL, headers)
        if status == 429:
            raise _RateLimited()
        if status != 200 or not isinstance(body, dict):
            usage_error = f"cursor usage HTTP {status}"
            body = {}
        else:
            # Nested quota objects: each key → {numRequests|used|..., maxRequestUsage|limit|...}
            # plus startOfMonth / billingCycleEnd / resetsAt at top or per bucket.
            global_reset = None
            for rk in ("billingCycleEnd", "resetsAt", "startOfMonth"):
                if body.get(rk) is not None:
                    if rk == "startOfMonth":
                        # +1 month approximate — prefer explicit reset fields
                        continue
                    global_reset = (
                        _parse_iso_to_iso(body[rk]) if isinstance(body[rk], str)
                        else _iso_from_ms(body[rk]) if _to_number(body[rk]) is not None
                        else None
                    )
                    if global_reset:
                        break
            if not global_reset and isinstance(body.get("startOfMonth"), str):
                try:
                    start = datetime.fromisoformat(body["startOfMonth"].replace("Z", "+00:00"))
                    # naive +31d
                    month = start.month + 1
                    year = start.year + (1 if month > 12 else 0)
                    month = month if month <= 12 else 1
                    end = start.replace(year=year, month=month)
                    global_reset = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                except (ValueError, TypeError, OverflowError):
                    pass

            def _add_bucket(name: str, bucket: dict) -> None:
                used = (
                    _to_number(bucket.get("numRequests"))
                    or _to_number(bucket.get("used"))
                    or _to_number(bucket.get("amountUsed"))
                    or _to_number(bucket.get("usdUsed"))
                )
                limit = (
                    _to_number(bucket.get("maxRequestUsage"))
                    or _to_number(bucket.get("limit"))
                    or _to_number(bucket.get("amountLimit"))
                    or _to_number(bucket.get("usdLimit"))
                )
                frac = None
                if used is not None and limit and limit > 0:
                    frac = _clamp_fraction(used / limit)
                elif _to_number(bucket.get("used_fraction")) is not None:
                    frac = _clamp_fraction(bucket.get("used_fraction"))
                elif _to_number(bucket.get("percent")) is not None:
                    frac = _clamp_fraction(bucket.get("percent"))
                resets = None
                for rk in ("resetsAt", "billingCycleEnd", "reset_at"):
                    if bucket.get(rk) is not None:
                        v = bucket[rk]
                        resets = _parse_iso_to_iso(v) if isinstance(v, str) else _iso_from_ms(v)
                        if resets:
                            break
                if not resets:
                    resets = global_reset
                if frac is None and resets is None and used is None:
                    return
                label = name.replace("_", " ").strip() or "usage"
                if label.lower() == "planusage" or label.lower() == "plan usage":
                    label = "Plan usage"
                usage.append(_usage_row(name, label, frac, resets))

            for key, val in body.items():
                if key in ("startOfMonth", "billingCycleEnd", "resetsAt", "email", "membershipType", "plan"):
                    continue
                if isinstance(val, dict) and any(
                    k in val for k in (
                        "numRequests", "used", "amountUsed", "usdUsed",
                        "maxRequestUsage", "limit", "amountLimit", "usdLimit",
                        "used_fraction", "percent",
                    )
                ):
                    _add_bucket(str(key), val)

            # Top-level plan/email fallthroughs on usage payload itself.
            if not plan and isinstance(body.get("membershipType"), str):
                plan = body["membershipType"]
            if not plan and isinstance(body.get("plan"), str):
                plan = body["plan"]
            if not email and isinstance(body.get("email"), str):
                email = body["email"]
    except _RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001
        usage_error = str(exc)
        log.debug("cursor usage fetch failed: %s", exc)

    # Prefer Dashboard period usage — /auth/usage often returns gpt-4 with
    # maxRequestUsage=null (no fraction → Settings "usage unavailable").
    period_rows: list[dict] = []
    try:
        period_rows = _fetch_cursor_period_usage(token)
    except Exception as exc:  # noqa: BLE001
        log.debug("cursor period usage failed: %s", exc)
    if period_rows:
        # Keep any /auth/usage buckets that already have fractions, then add
        # period windows (period wins on window id collision).
        by_window: dict[str, dict] = {}
        for row in usage:
            if isinstance(row, dict) and row.get("used_fraction") is not None:
                by_window[str(row.get("window") or row.get("label") or "")] = row
        for row in period_rows:
            if isinstance(row, dict):
                by_window[str(row.get("window") or row.get("label") or "")] = row
        usage = [r for r in by_window.values() if r]
        usage_error = None
        source = "api"

    # Plan/identity from stripe profile (best-effort, unstable internal endpoint).
    try:
        s_status, s_body, _ = _http_get(_CURSOR_STRIPE_URL, headers)
        if s_status == 200 and isinstance(s_body, dict):
            mt = s_body.get("membershipType") or s_body.get("membership_type") or s_body.get("plan")
            if isinstance(mt, str) and mt.strip():
                plan = mt.strip()
            if not email and isinstance(s_body.get("email"), str):
                email = s_body["email"].strip().lower()
            name = s_body.get("name") or s_body.get("displayName") or s_body.get("display_name")
            if isinstance(name, str) and name.strip():
                display_name = name.strip()
            if not account_id:
                for k in ("userId", "user_id", "id", "customerId"):
                    v = s_body.get(k)
                    if v is not None and str(v).strip():
                        account_id = str(v).strip()
                        break
    except Exception as exc:  # noqa: BLE001
        log.debug("cursor stripe profile failed: %s", exc)

    if not display_name:
        display_name = email
    if not display_name or _looks_like_uuid(display_name) or _looks_like_acct_slug(display_name):
        # Friendly fallback so the Accounts row is never just acct-…
        if plan:
            display_name = f"Cursor ({plan})"
        elif account_id and not _looks_like_acct_slug(str(account_id)):
            display_name = f"Cursor ({_short_id(str(account_id))})"
        else:
            display_name = "Cursor"

    if usage_error and not usage:
        source = "unavailable"
        log.info("cursor usage unavailable (%s); identity-only row", usage_error)

    return {
        "display_name": display_name,
        "plan": plan,
        "usage": usage,
        "email": email,
        "account_id": account_id or cred.get("account_id"),
        "source": source,
        "dashboard_url": DASHBOARD_URLS["cursor"],
    }


# xAI OAuth JWT ``tier`` is often a bare int (1=SuperGrok, 2=Heavy, …).
# Live ``/user?include=subscription`` returns strings like SuperGrokPro
# (subscription_check.rs QUALIFYING_TIERS).
_XAI_TIER_PLAN: dict[str, str] = {
    "0": "Free",
    "1": "SuperGrok",
    "2": "SuperGrok Heavy",
    "3": "Premium+",
    "free": "Free",
    "supergrok": "SuperGrok",
    "supergrokpro": "SuperGrok",
    "supergroklite": "SuperGrok Lite",
    "supergrokheavy": "SuperGrok Heavy",
    "supergrok heavy": "SuperGrok Heavy",
    "grokpro": "Grok Pro",
    "heavy": "SuperGrok Heavy",
    "premium": "Premium+",
    "premium+": "Premium+",
    "xpremiumplus": "Premium+",
    "xpremium": "X Premium",
    "xbasic": "X Basic",
    "x_premium_plus": "Premium+",
    "x_premium": "X Premium",
    "x_basic": "X Basic",
}


def _map_xai_tier_claim(value: Any) -> str | None:
    """Map JWT/store/subscription-API tier claim to a human plan label."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        key = str(int(value))
        return _XAI_TIER_PLAN.get(key) or f"tier-{key}"
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        low = s.lower()
        compact = low.replace(" ", "").replace("_", "").replace("-", "")
        if low in _XAI_TIER_PLAN:
            return _XAI_TIER_PLAN[low]
        if compact in _XAI_TIER_PLAN:
            return _XAI_TIER_PLAN[compact]
        if s.isdigit():
            return _XAI_TIER_PLAN.get(s) or f"tier-{s}"
        # Already a label (SuperGrok / Premium+ / SuperGrok Heavy / …).
        return s
    return None


def _infer_xai_plan(cred: dict) -> str | None:
    """Infer SuperGrok / Premium+ tier from stored plan or JWT claims."""
    mapped = _map_xai_tier_claim(cred.get("plan"))
    if mapped:
        return mapped
    # Numeric tier stored alongside plan on some cold-restore paths.
    mapped = _map_xai_tier_claim(cred.get("tier"))
    if mapped:
        return mapped
    token = cred.get("access_token") or cred.get("id_token") or ""
    if not token:
        return None
    claims = sub_auth.decode_jwt_payload(token)
    for key in (
        "plan", "tier", "subscription", "subscription_tier", "product",
        "grok_tier", "xai_tier", "account_tier",
    ):
        v = claims.get(key)
        mapped = _map_xai_tier_claim(v)
        if mapped:
            return mapped
        if isinstance(v, dict):
            for kk in ("name", "type", "id", "tier", "plan"):
                mapped = _map_xai_tier_claim(v.get(kk))
                if mapped:
                    return mapped
    # Nested app_metadata / user_metadata common in OIDC
    for nest in ("app_metadata", "user_metadata", "https://x.ai/claims"):
        block = claims.get(nest)
        if not isinstance(block, dict):
            continue
        for key in ("plan", "tier", "subscription", "product"):
            mapped = _map_xai_tier_claim(block.get(key))
            if mapped:
                return mapped
    return None


def fetch_xai_usage_jwt_inference(cred: dict, *, network: bool = True) -> dict:
    """xAI degrade path: plan from JWT / stored tier; no remaining-quota API.

    *network*=False skips OIDC userinfo (expired/revoked bulk polls).
    """
    # Re-run identity so cold-start credentials without display_name get a label.
    if network:
        enriched = enrich_xai_identity(dict(cred))
    else:
        enriched = dict(cred)
        # Offline label fill — mirror enrich_xai_identity's non-network branch.
        plan_l = _infer_xai_plan(enriched)
        if plan_l:
            enriched.setdefault("plan", plan_l)
        dn = enriched.get("display_name")
        if not dn or _looks_like_uuid(dn) or _looks_like_acct_slug(dn):
            if enriched.get("email"):
                enriched["display_name"] = enriched["email"]
            elif plan_l:
                short = _short_id(enriched.get("account_id"))
                enriched["display_name"] = (
                    f"Grok ({plan_l})" if not short else f"Grok ({plan_l} · {short})"
                )
            else:
                short = _short_id(enriched.get("account_id"))
                enriched["display_name"] = f"Grok ({short})" if short else "Grok"
    email = enriched.get("email") or cred.get("email")
    plan = enriched.get("plan") or _infer_xai_plan(cred)
    display_name = enriched.get("display_name") or email
    return {
        "display_name": display_name,
        "plan": plan,
        "usage": list(cred.get("last_usage") or []),
        "email": email,
        "account_id": enriched.get("account_id") or cred.get("account_id"),
        "source": XAI_SOURCE_JWT,
        "dashboard_url": DASHBOARD_URLS["xai"],
    }


def fetch_xai_usage_browser_scrape(cred: dict) -> dict:
    """CEF per-account scrape of grok.com SuperGrok web pool remaining.

    Lazy-imports ``services.browser.usage_scrape`` so non-browser test paths stay
    light. On ANY failure (daemon down, parse empty, timeout, import error) falls
    back to :func:`fetch_xai_usage_jwt_inference` so Settings still shows plan +
    dashboard link. Structured ``needs_login`` / ``blocked`` results keep
    ``source=browser_scrape`` so the UI can CTA re-login without inventing bars.
    """
    base = fetch_xai_usage_jwt_inference(cred)
    account_id = str(cred.get("account_id") or cred.get("sub") or cred.get("email") or "default")
    try:
        from services.browser import usage_scrape  # lazy: CEF only when scraping
    except Exception as exc:  # noqa: BLE001
        log.warning("xAI browser_scrape import failed; jwt_inference fallback: %s", exc)
        return base

    try:
        scraped = usage_scrape.fetch_grok_usage(
            account_id,
            cred,
            timeout_s=45.0,
        )
    except Exception as exc:  # noqa: BLE001 — transport / parse / busy
        log.warning("xAI browser_scrape failed; jwt_inference fallback: %s", exc)
        return base

    if not isinstance(scraped, dict):
        return base

    status = scraped.get("status")
    # A login/challenge result is useful provenance, but it is not quota data.
    # Preserve only rows the scraper actually returned, retaining a null fraction
    # so consumers can render the status without inventing a 0% bar.
    if status in ("needs_login", "blocked"):
        out = dict(base)
        out["source"] = XAI_SOURCE_BROWSER
        out["dashboard_url"] = scraped.get("dashboard_url") or DASHBOARD_URLS["xai"]
        out["usage"] = [
            _usage_row(
                str(r.get("window") or "unknown"),
                str(r.get("label") or "Usage"),
                r.get("used_fraction"),
                r.get("resets_at") if isinstance(r.get("resets_at"), str) else None,
                str(r.get("status") or status),
            )
            for r in (scraped.get("usage") or [])
            if isinstance(r, dict)
        ]
        if scraped.get("details"):
            out["details"] = scraped["details"]
        return out

    if not scraped.get("ok"):
        # error / empty — fall back so we don't ship unknown bars without a scrape win
        log.info("xAI browser_scrape not ok (status=%s); jwt_inference fallback", status)
        return base

    usage_rows = scraped.get("usage") or []
    if not usage_rows:
        return base

    out = dict(base)
    out["source"] = XAI_SOURCE_BROWSER
    out["dashboard_url"] = scraped.get("dashboard_url") or DASHBOARD_URLS["xai"]
    # Prefer scrape identity when present; JWT plan/email remain the baseline.
    if scraped.get("email"):
        out["email"] = scraped["email"]
    if scraped.get("display_name"):
        out["display_name"] = scraped["display_name"]
    if scraped.get("plan"):
        out["plan"] = scraped["plan"]

    normalized: list[dict] = []
    for r in usage_rows:
        if not isinstance(r, dict):
            continue
        frac = r.get("used_fraction")
        # Prefer remaining/limit when fraction absent.
        if frac is None:
            rem = _to_number(r.get("remaining"))
            lim = _to_number(r.get("limit"))
            if rem is not None and lim and lim > 0:
                frac = _clamp_fraction((lim - rem) / lim)
        st = r.get("status")
        # Map scrape vocabulary → contract status (ok|warn|blocked|unknown).
        if st == "exhausted":
            st = "blocked"
        elif st == "warning":
            st = "warn"
        elif st in ("needs_login", "blocked"):
            pass
        elif st == "ok" or st is None:
            st = None  # let _usage_row derive from fraction
        normalized.append(
            _usage_row(
                str(r.get("window") or "weekly"),
                str(r.get("label") or "Weekly pool"),
                frac,
                r.get("resets_at") if isinstance(r.get("resets_at"), str) else None,
                st if isinstance(st, str) else None,
            )
        )
    if not normalized:
        return base
    out["usage"] = normalized
    if scraped.get("details"):
        out["details"] = scraped["details"]
    return out


# Process-local cache for cli-chat-proxy billing/user GETs (~60s).
_xai_billing_cache: dict[str, dict[str, Any]] = {}


def _cent_val(value: Any) -> float | None:
    """Extract a numeric amount from a Cent ``{val: N}`` or bare number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        return _to_number(value.get("val") if "val" in value else value.get("value"))
    return _to_number(value)


def _period_field(period: dict | None, *keys: str) -> Any:
    if not isinstance(period, dict):
        return None
    for k in keys:
        if k in period and period[k] is not None:
            return period[k]
    return None


def _period_type_label(raw: Any) -> str:
    """Map USAGE_PERIOD_TYPE_* (or free text) to a short window/label stem."""
    if raw is None:
        return "credits"
    s = str(raw).strip()
    if not s:
        return "credits"
    up = s.upper()
    if "WEEK" in up:
        return "weekly"
    if "MONTH" in up:
        return "monthly"
    if "DAY" in up or "DAILY" in up:
        return "daily"
    # Already a short word.
    low = s.lower()
    if low in ("weekly", "monthly", "daily", "credits"):
        return low
    return "credits"


def _parse_xai_billing_config(body: Any) -> dict[str, Any]:
    """Normalize cli-chat-proxy billing JSON → usage windows + extras.

    Accepts either the full ``BillingConfigResponse`` (``{config: …}``) or a
    bare ``BillingConfig``. Field names may be camelCase (serde) or snake_case.
    """
    empty: dict[str, Any] = {
        "usage": [],
        "plan": None,
        "prepaid_balance": None,
        "credit_usage_percent": None,
    }
    if not isinstance(body, dict):
        return empty

    cfg = body.get("config") if isinstance(body.get("config"), dict) else body
    if not isinstance(cfg, dict):
        return empty

    # Prefer creditUsagePercent; fall back to used/monthlyLimit.
    pct = _to_number(
        cfg.get("creditUsagePercent")
        if cfg.get("creditUsagePercent") is not None
        else cfg.get("credit_usage_percent")
    )
    if pct is None:
        used = _cent_val(cfg.get("used"))
        limit = _cent_val(
            cfg.get("monthlyLimit")
            if cfg.get("monthlyLimit") is not None
            else cfg.get("monthly_limit")
        )
        if used is not None and limit is not None and limit > 0:
            pct = min(100.0, max(0.0, (used / limit) * 100.0))

    period = (
        cfg.get("currentPeriod")
        if isinstance(cfg.get("currentPeriod"), dict)
        else cfg.get("current_period")
        if isinstance(cfg.get("current_period"), dict)
        else None
    )
    period_type = _period_field(period, "type", "periodType", "period_type")
    period_end = _period_field(period, "end") or (
        cfg.get("billingPeriodEnd")
        if cfg.get("billingPeriodEnd") is not None
        else cfg.get("billing_period_end")
    )
    resets_at = _parse_iso_to_iso(str(period_end)) if period_end else None

    window_stem = _period_type_label(period_type)
    # Settings window key: "credits" (task) with a human label that may
    # mention the period (Weekly credits / Monthly credits).
    if window_stem in ("weekly", "monthly", "daily"):
        label = f"{window_stem.capitalize()} credits"
    else:
        label = "Credits"

    usage: list[dict] = []
    # grok-build credit_balance_from_config: absent percent + no limit → 0.0.
    # Live SuperGrok/GrokPro responses often omit creditUsagePercent when the
    # weekly pool is untouched — still a real "0% used" bar for Settings.
    if pct is None and isinstance(cfg, dict) and cfg:
        markers = (
            "currentPeriod", "current_period",
            "prepaidBalance", "prepaid_balance",
            "onDemandCap", "on_demand_cap",
            "isUnifiedBillingUser", "is_unified_billing_user",
            "billingPeriodEnd", "billing_period_end",
            "monthlyLimit", "monthly_limit", "used",
        )
        if any(k in cfg for k in markers):
            pct = 0.0
    if pct is not None:
        frac = _clamp_fraction(pct / 100.0)
        usage.append(_usage_row("credits", label, frac, resets_at, None))

    prepaid = _cent_val(
        cfg.get("prepaidBalance")
        if cfg.get("prepaidBalance") is not None
        else cfg.get("prepaid_balance")
    )
    # Surface prepaid as a secondary detail row only when positive and we
    # already have a primary credits bar (don't invent a 0% bar from prepaid).
    if prepaid is not None and prepaid > 0 and usage:
        # Encode remaining prepaid dollars (Cent.val is USD cents) into the
        # label; fraction stays None so UI doesn't treat it as a second bar.
        dollars = prepaid / 100.0
        usage.append(
            {
                "window": "prepaid",
                "label": f"Prepaid ${dollars:.2f}",
                "used_fraction": None,
                "resets_at": None,
                "status": "ok",
                "prepaid_balance_cents": prepaid,
            }
        )

    plan = None
    for key in (
        "subscriptionTier",
        "subscription_tier",
        "tier",
        "plan",
    ):
        # Top-level BillingConfigResponse may carry subscription_tier.
        v = body.get(key)
        if v is None and cfg is not body:
            v = cfg.get(key)
        mapped = _map_xai_tier_claim(v)
        if mapped:
            plan = mapped
            break

    return {
        "usage": usage,
        "plan": plan,
        "prepaid_balance": prepaid,
        "credit_usage_percent": pct,
    }


def _parse_xai_user_subscription(body: Any) -> dict[str, Any]:
    """Pull plan + identity from ``GET /user?include=subscription``."""
    out: dict[str, Any] = {}
    if not isinstance(body, dict):
        return out
    for key in (
        "subscriptionTier",
        "subscription_tier",
        "tier",
        "plan",
    ):
        mapped = _map_xai_tier_claim(body.get(key))
        if mapped:
            out["plan"] = mapped
            break
    email = body.get("email")
    if isinstance(email, str) and "@" in email:
        out["email"] = email.strip().lower()
    first = body.get("firstName") or body.get("first_name")
    last = body.get("lastName") or body.get("last_name")
    parts = [p for p in (first, last) if isinstance(p, str) and p.strip()]
    if parts:
        name = " ".join(p.strip() for p in parts)
        if name and not _looks_like_uuid(name):
            out["display_name"] = name
    uid = body.get("userId") or body.get("user_id")
    if uid is not None and str(uid).strip():
        out["account_id"] = str(uid).strip()
    return out


def _xai_billing_cache_key(cred: dict) -> str:
    aid = (
        cred.get("account_id")
        or cred.get("user_id")
        or cred.get("sub")
        or ""
    )
    # Token suffix so a re-login for the same sub doesn't reuse stale bars.
    tok = str(cred.get("access_token") or "")
    tail = tok[-12:] if tok else ""
    return f"{aid}::{tail}"


def _get_cached_xai_billing(cred: dict) -> dict[str, Any] | None:
    key = _xai_billing_cache_key(cred)
    entry = _xai_billing_cache.get(key)
    if not entry:
        return None
    if (_now() - float(entry.get("fetched_at") or 0)) > XAI_BILLING_CACHE_TTL_S:
        return None
    return dict(entry.get("data") or {})


def _set_cached_xai_billing(cred: dict, data: dict[str, Any]) -> None:
    key = _xai_billing_cache_key(cred)
    _xai_billing_cache[key] = {"fetched_at": _now(), "data": dict(data)}


def fetch_xai_billing(cred: dict, *, force: bool = False) -> dict[str, Any]:
    """GET cli-chat-proxy billing + subscription tier for one credential.

    Returns ``{usage, plan, prepaid_balance, credit_usage_percent, source}``.
    Network errors raise; HTTP 429 raises ``_RateLimited``.
    """
    if not force:
        cached = _get_cached_xai_billing(cred)
        if cached is not None:
            return cached

    proxy_headers = _xai_auth_headers(cred, proxy=True)
    result: dict[str, Any] = {
        "usage": [],
        "plan": None,
        "prepaid_balance": None,
        "credit_usage_percent": None,
        "source": "billing",
    }

    st, body, _rh = _http_get(XAI_BILLING_URL, headers=proxy_headers, timeout=15.0)
    if st == 429:
        raise _RateLimited("xAI billing 429")
    if st == 200:
        parsed = _parse_xai_billing_config(body)
        result.update(parsed)
    else:
        log.info("xAI billing HTTP %s", st)

    try:
        st_u, user_body, _ = _http_get(
            XAI_USER_SUB_URL, headers=proxy_headers, timeout=10.0
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("xAI /user?include=subscription failed: %s", exc)
        st_u, user_body = -1, None
    if st_u == 429:
        raise _RateLimited("xAI user subscription 429")
    if st_u == 200 and isinstance(user_body, dict):
        sub = _parse_xai_user_subscription(user_body)
        if sub.get("plan"):
            result["plan"] = sub["plan"]
        for k in ("email", "display_name", "account_id"):
            if sub.get(k):
                result[k] = sub[k]

    if result.get("usage") or result.get("plan") is not None:
        _set_cached_xai_billing(cred, result)
    return result

def fetch_xai_usage_api(cred: dict) -> dict:
    """Live xAI identity + SuperGrok credits from official billing proxy.

    Primary (grok-build /usage path):
    - ``GET https://cli-chat-proxy.grok.com/v1/billing?format=credits``
      → ``credit_usage_percent``, ``prepaid_balance``, period reset
    - ``GET …/v1/user?include=subscription`` → live ``subscription_tier``

    Auth for those: ``Authorization: Bearer`` + ``X-XAI-Token-Auth: xai-grok-cli``.

    Identity still comes from OIDC userinfo (JWT has no email/name).
    Rate-limit headers (api.x.ai chat / last_usage) are secondary enrichment
    appended only when billing did not return a credits window.

    Dead/expired tokens skip the multi-GET waterfall entirely — Settings multi-
    account polls otherwise spend minutes serial-timing-out 30+ accounts.
    """
    token = str(cred.get("access_token") or "").strip()
    # Dead/expired tokens skip the multi-GET waterfall entirely — Settings multi-
    # account polls otherwise spend minutes serial-timing-out 30+ accounts.
    # Use offline jwt_inference (no userinfo) so bulk polls stay local.
    if not token:
        return fetch_xai_usage_jwt_inference(cred, network=False)
    if cred.get("refresh_revoked") and sub_auth.is_expired(cred, skew_ms=0):
        out = fetch_xai_usage_jwt_inference(cred, network=False)
        if out.get("usage"):
            out["source"] = "cached"
        return out
    if sub_auth.is_expired(cred, skew_ms=0):
        out = fetch_xai_usage_jwt_inference(cred, network=False)
        if out.get("usage"):
            out["source"] = "cached"
        return out

    base = fetch_xai_usage_jwt_inference(cred)
    headers = _xai_auth_headers(cred)
    out = dict(base)
    out["source"] = XAI_SOURCE_API
    out["dashboard_url"] = DASHBOARD_URLS["xai"]

    # ── Identity: OIDC userinfo (the only reliable name/email source) ────────
    try:
        st, body, _rh = _http_get(XAI_USERINFO_URL, headers=headers, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("xAI userinfo failed: %s", exc)
        st, body = -1, None
    if st == 429:
        raise _RateLimited("xAI userinfo 429")
    if st == 200 and isinstance(body, dict):
        email = body.get("email")
        if isinstance(email, str) and "@" in email:
            out["email"] = email.strip().lower()
        name = body.get("name") or body.get("preferred_username")
        if isinstance(name, str) and name.strip() and not _looks_like_uuid(name.strip()):
            out["display_name"] = name.strip()
        elif out.get("email"):
            out["display_name"] = out["email"]
        sub = body.get("sub")
        if sub is not None and str(sub).strip() and not out.get("account_id"):
            out["account_id"] = str(sub).strip()

    # ── Team block flag from /v1/me ──────────────────────────────────────────
    try:
        st_me, me_body, _ = _http_get(XAI_ME_URL, headers=headers, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        log.debug("xAI /v1/me failed: %s", exc)
        st_me, me_body = -1, None
    team_blocked = False
    if st_me == 200 and isinstance(me_body, dict):
        team_blocked = bool(me_body.get("team_blocked"))
        if me_body.get("user_id") and not out.get("account_id"):
            out["account_id"] = str(me_body["user_id"])

    # ── Primary: SuperGrok credits via cli-chat-proxy billing ────────────────
    usage_rows: list[dict] = []
    billing_ok = False
    try:
        bill_cred = dict(cred)
        if out.get("account_id") and not bill_cred.get("account_id"):
            bill_cred["account_id"] = out["account_id"]
        billing = fetch_xai_billing(bill_cred)
    except _RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("xAI billing fetch failed: %s", exc)
        billing = {}

    if isinstance(billing, dict):
        if billing.get("plan"):
            out["plan"] = billing["plan"]
        for k in ("email", "display_name", "account_id"):
            if billing.get(k) and (
                not out.get(k)
                or (k == "display_name" and _looks_like_uuid(out.get(k)))
            ):
                out[k] = billing[k]
        b_usage = billing.get("usage") or []
        if isinstance(b_usage, list) and b_usage:
            credits = [
                r for r in b_usage
                if isinstance(r, dict) and r.get("window") == "credits"
            ]
            other = [
                r for r in b_usage
                if isinstance(r, dict) and r.get("window") != "credits"
            ]
            usage_rows = list(credits or b_usage)
            for r in other:
                if r not in usage_rows:
                    usage_rows.append(r)
            billing_ok = any(
                isinstance(r, dict) and r.get("used_fraction") is not None
                for r in credits
            )
        if billing.get("credit_usage_percent") is not None:
            out["credit_usage_percent"] = billing["credit_usage_percent"]
        if billing.get("prepaid_balance") is not None:
            out["prepaid_balance"] = billing["prepaid_balance"]

    # ── Secondary: rate-limit headers (live / last chat) ─────────────────────
    rate_rows: list[dict] = []
    if not billing_ok:
        try:
            st_m, _body_m, rh_m = _http_get(
                XAI_MODELS_URL, headers=headers, timeout=20.0
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("xAI /v1/models for rate headers failed: %s", exc)
            st_m, rh_m = -1, {}
        if st_m == 429:
            raise _RateLimited("xAI models 429")
        if st_m == 200:
            rate_rows = _usage_from_ratelimit_headers(rh_m or {})

        if (
            not rate_rows
            and isinstance(cred.get("last_usage"), list)
            and cred["last_usage"]
        ):
            has_frac = any(
                isinstance(r, dict) and r.get("used_fraction") is not None
                for r in cred["last_usage"]
            )
            if has_frac:
                liveish = [
                    r for r in cred["last_usage"]
                    if isinstance(r, dict)
                    and r.get("window") in ("requests", "tokens", "credits")
                ]
                rate_rows = list(liveish or cred["last_usage"])

        import os as _os
        probe = str(_os.environ.get("AICAD_XAI_USAGE_PROBE") or "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if not rate_rows and not usage_rows and probe:
            try:
                payload = json.dumps({
                    "model": "grok-4.5",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                }).encode("utf-8")
                st_c, _body_c, rh_c = _http_request(
                    "POST",
                    XAI_CHAT_URL,
                    headers={**headers, "Content-Type": "application/json"},
                    data=payload,
                    timeout=45.0,
                )
                if st_c == 429:
                    raise _RateLimited("xAI chat probe 429")
                if st_c == 403:
                    rate_rows = [
                        _usage_row("requests", "Request pool", 1.0, None, "blocked"),
                    ]
                elif st_c == 200:
                    rate_rows = _usage_from_ratelimit_headers(rh_c or {})
            except _RateLimited:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("xAI usage probe failed: %s", exc)

    # Merge: credits first, then secondary rate-limit windows (no overwrite).
    if rate_rows:
        seen = {r.get("window") for r in usage_rows if isinstance(r, dict)}
        for r in rate_rows:
            if isinstance(r, dict) and r.get("window") not in seen:
                usage_rows.append(r)
                seen.add(r.get("window"))

    if team_blocked and not usage_rows:
        usage_rows = [
            _usage_row("credits", "Credits", 1.0, None, "blocked"),
        ]
    elif team_blocked and usage_rows:
        usage_rows = [
            {
                **r,
                "status": "blocked",
                "used_fraction": (
                    r.get("used_fraction")
                    if r.get("used_fraction") is not None
                    else 1.0
                ),
            }
            for r in usage_rows
        ]

    if usage_rows:
        out["usage"] = usage_rows
    elif not out.get("usage"):
        out["usage"] = list(cred.get("last_usage") or [])

    # Prefer real human name / email over bare UUID or "Grok (uuid)" leftovers.
    dn = out.get("display_name")
    if (
        not dn
        or _looks_like_uuid(dn)
        or _looks_like_acct_slug(dn)
        or (isinstance(dn, str) and dn.startswith("Grok ("))
    ):
        if isinstance(dn, str) and " " in dn and not dn.startswith("Grok"):
            pass
        elif out.get("email"):
            out["display_name"] = out["email"]
    return out


def fetch_xai_usage(cred: dict) -> dict:
    """Dispatch xAI usage by pluggable source enum.

    Default is live API (cli-chat-proxy billing + userinfo). Env
    ``AICAD_XAI_USAGE_SOURCE`` may force ``api`` / ``browser_scrape`` /
    ``jwt_inference``.
    """
    import os as _os
    src = (
        str(_os.environ.get("AICAD_XAI_USAGE_SOURCE") or "").strip().lower()
        or str(XAI_USAGE_SOURCE or "").strip().lower()
        or XAI_SOURCE_API
    )
    if src in (XAI_SOURCE_BROWSER, "browser", "scrape"):
        return fetch_xai_usage_browser_scrape(cred)
    if src in (XAI_SOURCE_JWT, "jwt", "jwt_inference"):
        return fetch_xai_usage_jwt_inference(cred)
    # Default + "api"
    try:
        return fetch_xai_usage_api(cred)
    except _RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001 — degrade to jwt + last_usage
        log.warning("xAI api usage fetch failed; jwt_inference fallback: %s", exc)
        return fetch_xai_usage_jwt_inference(cred)


# ── Kimi (Kimi For Coding) usage ─────────────────────────────────────────────
# Port of omp ``packages/ai/src/usage/kimi.ts``: GET {base}/usages with the
# OAuth Bearer + Kimi CLI headers; payload carries a `usage` summary row and a
# `limits[]` array with per-window detail/window blocks.


def _kimi_duration_label(duration: float | None, time_unit: str) -> str | None:
    if duration is None or not time_unit:
        return None
    upper = time_unit.upper()
    d = int(duration)
    if "MINUTE" in upper:
        if d >= 60 and d % 60 == 0:
            return f"{d // 60}h limit"
        return f"{d}m limit"
    if "HOUR" in upper:
        return f"{d}h limit"
    if "DAY" in upper:
        return f"{d}d limit"
    if "SECOND" in upper:
        return f"{d}s limit"
    return None


def _kimi_reset_iso(data: dict, now_ms: float) -> str | None:
    """reset_at/resetAt/reset_time/resetTime (ISO str or epoch s/ms) or reset_in/ttl/window seconds."""
    from datetime import datetime, timezone

    for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            iso = _parse_iso_to_iso(value.strip())
            if iso:
                return iso
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            ms = value if value > 1_000_000_000_000 else value * 1000
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    for key in ("reset_in", "resetIn", "ttl", "window"):
        seconds = _to_number(data.get(key))
        if seconds is not None:
            return datetime.fromtimestamp(
                (now_ms + seconds * 1000) / 1000, tz=timezone.utc
            ).isoformat()
    return None


def _kimi_window_id(duration: float | None, time_unit: str) -> str:
    """Short window id from a {duration, timeUnit} block: 300 minutes -> '5h'."""
    if duration is None or not time_unit:
        return "default"
    upper = time_unit.upper()
    d = int(duration)
    if "MINUTE" in upper:
        return f"{d // 60}h" if (d >= 60 and d % 60 == 0) else f"{d}m"
    if "HOUR" in upper:
        return f"{d}h"
    if "DAY" in upper:
        return f"{d}d"
    if "SECOND" in upper:
        return f"{d}s"
    return "default"


def _kimi_usage_row(data: dict, default_label: str, now_ms: float) -> dict | None:
    limit = _to_number(data.get("limit"))
    used = _to_number(data.get("used"))
    remaining = _to_number(data.get("remaining"))
    if used is None and remaining is not None and limit is not None:
        used = limit - remaining
    if used is None and limit is None:
        return None
    frac = _clamp_fraction(used / limit) if (used is not None and limit) else None
    label = data.get("name") or data.get("title") or default_label
    return _usage_row(
        str(data.get("window_id") or "default"),
        str(label),
        frac,
        _kimi_reset_iso(data, now_ms),
    )


def _parse_kimi_usage_payload(payload: Any) -> list[dict]:
    """Map the /usages payload to harness usage rows (omp parseUsagePayload)."""
    if not isinstance(payload, dict):
        return []
    now_ms = _now() * 1000
    rows: list[dict] = []
    summary = payload.get("usage")
    if isinstance(summary, dict):
        row = _kimi_usage_row(summary, "Total quota", now_ms)
        if row:
            row["window"] = "total"
            rows.append(row)
    limits = payload.get("limits")
    if isinstance(limits, list):
        for idx, item in enumerate(limits):
            if not isinstance(item, dict):
                continue
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else item
            window_data = item.get("window") if isinstance(item.get("window"), dict) else {}
            duration = _to_number(window_data.get("duration"))
            time_unit = str(window_data.get("timeUnit") or "")
            label = (
                item.get("name")
                or item.get("title")
                or item.get("scope")
                or detail.get("name")
                or detail.get("title")
                or _kimi_duration_label(duration, time_unit)
                or f"Limit #{idx + 1}"
            )
            row = _kimi_usage_row(detail, str(label), now_ms)
            if row is None:
                continue
            if duration is not None and time_unit:
                row["window"] = _kimi_window_id(duration, time_unit)
                row["label"] = str(label)
            # Kimi puts resetTime on the limit detail, not on window — fall back
            # to the row-level reset so the UI can render "resets in …".
            if not row.get("resets_at"):
                row["resets_at"] = _kimi_reset_iso(window_data, now_ms)
            rows.append(row)
    parallel = payload.get("parallel")
    if isinstance(parallel, dict):
        # {limit: "30", details: [active session ids]} — used = len(details).
        details = parallel.get("details")
        if isinstance(details, list) and "used" not in parallel:
            parallel = {**parallel, "used": len(details)}
        row = _kimi_usage_row(parallel, "Concurrent sessions", now_ms)
        if row:
            row["window"] = "parallel"
            rows.append(row)
    return rows


def _kimi_identity(payload: Any, cred: dict) -> dict:
    """Plan + display label from the /usages `user` block.

    Kimi's OAuth JWT carries only sub/user_id/device_id (no email/name), so the
    membership level is the only human identity available — mirror the xAI
    degrade path's honest fallback labels.
    """
    out: dict[str, Any] = {}
    user = payload.get("user") if isinstance(payload, dict) else None
    plan = None
    uid = None
    if isinstance(user, dict):
        uid = user.get("userId") or user.get("user_id")
        membership = user.get("membership") if isinstance(user.get("membership"), dict) else {}
        level = membership.get("level") or user.get("level")
        if isinstance(level, str) and level.strip():
            # LEVEL_ADVANCED -> Advanced
            plan = level.strip().removeprefix("LEVEL_").replace("_", " ").title()
    if plan:
        out["plan"] = plan
    short = (str(uid).split("-")[0][:8] if uid else "") or (
        str(cred.get("account_id") or "").split("-")[0][:8]
    )
    if plan and short:
        out["display_name"] = f"Kimi {plan} ({short})"
    elif plan:
        out["display_name"] = f"Kimi {plan}"
    elif short:
        out["display_name"] = f"Kimi ({short})"
    return out


def fetch_kimi_usage(cred: dict) -> dict:
    """Kimi For Coding usage: GET {base}/usages with Bearer + Kimi CLI headers.

    Refreshes an expired token inline (Python owns refresh; Settings may poll
    long before any agent run calls ensure_fresh), then maps the usage/limits/
    parallel blocks and the user membership level into rows + identity.
    """
    from . import kimi_sub_provider as ksp

    if sub_auth.is_expired(cred, skew_ms=0) and cred.get("refresh_token"):
        try:
            prov = ksp.KimiSubProvider()
            prov._cred = cred
            cred = prov.refresh()
        except Exception as exc:  # noqa: BLE001 — fall through with stale token
            log.info("kimi usage: inline refresh failed: %s", exc)
    token = cred.get("access_token") or ""
    if not token:
        return {"usage": [], "plan": cred.get("plan"), "source": "unavailable"}

    url = f"{ksp._base_url()}/usages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        **ksp.get_kimi_common_headers(),
    }
    status, payload, _rh = _http_get(url, headers, timeout=12.0)
    if status == 429:
        raise _RateLimited()
    if status == 401 and cred.get("refresh_token"):
        # Token rejected despite a fresh-looking expiry — refresh once and retry.
        prov = ksp.KimiSubProvider()
        prov._cred = cred
        cred = prov.refresh()
        headers["Authorization"] = f"Bearer {cred['access_token']}"
        status, payload, _rh = _http_get(url, headers, timeout=12.0)
    if status != 200:
        raise RuntimeError(f"kimi usages returned HTTP {status}")
    rows = _parse_kimi_usage_payload(payload)
    identity = _kimi_identity(payload, cred)
    return {
        "usage": rows,
        "plan": identity.get("plan") or cred.get("plan"),
        "display_name": identity.get("display_name") or cred.get("display_name"),
        "source": "api" if rows else "unavailable",
        "dashboard_url": DASHBOARD_URLS.get("kimi"),
    }


class _RateLimited(Exception):
    """Usage endpoint returned 429 — freeze this account until cache TTL."""


_FETCHERS: dict[str, Callable[[dict], dict]] = {
    "claude": fetch_anthropic_usage,
    "codex": fetch_codex_usage,
    "cursor": fetch_cursor_usage,
    "xai": fetch_xai_usage,
    "kimi": fetch_kimi_usage,
}


# ── Public API ───────────────────────────────────────────────────────────────


def clear_cache() -> None:
    """Drop the in-process usage + billing caches (tests)."""
    _cache.clear()
    _xai_billing_cache.clear()

def invalidate_account(provider: str, account_id: str | None = None) -> None:
    """Drop cached usage for one account, or every account for a provider."""
    resolved = sl._resolve(provider)
    if resolved is None:
        return
    _friendly, _name, cred_id, _label = resolved
    if account_id is not None:
        _cache.pop(_cache_key(cred_id, str(account_id)), None)
        return
    prefix = f"{cred_id}::"
    for key in [key for key in _cache if key.startswith(prefix)]:
        _cache.pop(key, None)


def get_cached(provider: str, account_id: str) -> dict | None:
    """Return a cached row for (friendly provider, account_id) if still fresh."""
    resolved = sl._resolve(provider)
    if resolved is None:
        return None
    _friendly, _name, cred_id, _label = resolved
    entry = _cache.get(_cache_key(cred_id, account_id))
    if not entry:
        return None
    now = _now()
    rate_until = float(entry.get("rate_limited_until") or 0)
    fetched_at = float(entry.get("fetched_at") or 0)
    if not (rate_until and now < rate_until):
        if not fetched_at or (now - fetched_at) >= CACHE_TTL_S:
            return None
    return dict(entry.get("row") or {})


def fetch_account_usage(
    provider: str,
    account_id: str | None = None,
    *,
    force: bool = False,
    cred: dict | None = None,
) -> dict:
    """Fetch (or return cached) usage for one account.

    *provider* is a friendly id (codex|claude|cursor|xai) or provider name.
    Returns a contract row dict always (degrades on failure).
    """
    resolved = sl._resolve(provider)
    if resolved is None:
        return {
            "ok": False,
            "provider": provider,
            "account_id": account_id,
            "display_name": None,
            "plan": None,
            "usage": [],
            "source": "unavailable",
            "reason": f"unknown subscription provider {provider!r}",
        }
    friendly, _name, cred_id, _label = resolved
    stored = cred if cred is not None else sub_auth.load_credential(cred_id, account_id=account_id)
    if not stored:
        return _base_row(
            friendly,
            account_id or sub_auth.DEFAULT_ACCOUNT_ID,
            source="unavailable",
        )

    aid = (
        account_id
        or stored.get("account_id")
        or sub_auth.get_active_account_id(cred_id)
        or sub_auth.DEFAULT_ACCOUNT_ID
    )
    key = _cache_key(cred_id, str(aid))
    now = _now()
    cached = _cache.get(key)

    if cached:
        # A server-requested freeze is stronger than force=True: once a provider
        # returns 429, no caller may hammer that account again for five minutes.
        rate_until = float(cached.get("rate_limited_until") or 0)
        if rate_until and now < rate_until:
            row = dict(cached.get("row") or {})
            row.setdefault("provider", friendly)
            row.setdefault("account_id", str(aid))
            return _merge_identity(row, stored)
        fetched_at = float(cached.get("fetched_at") or 0)
        if not force and fetched_at and (now - fetched_at) < CACHE_TTL_S:
            row = dict(cached.get("row") or {})
            row.setdefault("provider", friendly)
            row.setdefault("account_id", str(aid))
            return _merge_identity(row, stored)

    fetcher = _FETCHERS.get(friendly)
    if fetcher is None:
        row = _base_row(friendly, str(aid), source="unavailable")
        return _merge_identity(row, stored)

    # Revoked/expired-token accounts (any provider) can never complete a live
    # fetch — skip the network entirely instead of re-hitting it every
    # cache-TTL cycle. A credential whose access token is still live is left
    # to the normal fetcher path (it may yet succeed until that token dies).
    if stored.get("refresh_revoked") and sub_auth.is_expired(stored, skew_ms=0):
        log.info("usage: %s/%s revoked+expired; skipping live fetch", friendly, aid)
        last_usage = []
        if cached and cached.get("row") and cached["row"].get("usage"):
            last_usage = list(cached["row"]["usage"])
        elif isinstance(stored.get("last_usage"), list):
            last_usage = list(stored["last_usage"])
        row = _merge_identity(
            _base_row(
                friendly,
                str(aid),
                display_name=stored.get("display_name") or stored.get("email"),
                plan=stored.get("plan"),
                usage=last_usage,
                source="cached" if last_usage else "unavailable",
                email=stored.get("email"),
            ),
            stored,
        )
        _cache[key] = {"fetched_at": now, "rate_limited_until": 0, "row": row}
        return row

    try:
        result = fetcher(stored)
    except _RateLimited:
        log.info("usage: %s/%s rate-limited; freezing for %ss", friendly, aid, CACHE_TTL_S)
        last_usage = []
        if cached and cached.get("row") and cached["row"].get("usage"):
            last_usage = list(cached["row"]["usage"])
        elif isinstance(stored.get("last_usage"), list):
            last_usage = list(stored["last_usage"])
        row = _merge_identity(
            _base_row(
                friendly,
                str(aid),
                display_name=stored.get("display_name") or stored.get("email"),
                plan=stored.get("plan"),
                usage=last_usage,
                source="api",
                email=stored.get("email"),
            ),
            stored,
        )
        # Preserve last-good identity fields if we had a cached row.
        if cached and cached.get("row"):
            prev = cached["row"]
            if prev.get("usage") and not row.get("usage"):
                row["usage"] = list(prev["usage"])
            if prev.get("plan") and not row.get("plan"):
                row["plan"] = prev["plan"]
            if prev.get("display_name") and not row.get("display_name"):
                row["display_name"] = prev["display_name"]
        _cache[key] = {
            "fetched_at": now,
            "rate_limited_until": now + CACHE_TTL_S,
            "row": row,
        }
        return row
    except Exception as exc:  # noqa: BLE001 — degrade, never invent quotas
        log.warning("usage: %s/%s fetch failed: %s", friendly, aid, exc)
        if cached and cached.get("row"):
            row = dict(cached["row"])
            row.setdefault("provider", friendly)
            row.setdefault("account_id", str(aid))
            return _merge_identity(row, stored)
        # xAI always has a JWT degrade path — re-run jwt inference on hard fail
        if friendly == "xai":
            result = fetch_xai_usage_jwt_inference(stored)
        else:
            # Cold-start restore: last live snapshot written to the credential.
            last_usage = stored.get("last_usage") if isinstance(stored.get("last_usage"), list) else []
            row = _base_row(
                friendly,
                str(aid),
                display_name=stored.get("display_name") or stored.get("email"),
                plan=stored.get("plan"),
                usage=list(last_usage or []),
                source="cached" if last_usage else "unavailable",
                email=stored.get("email"),
            )
            return _merge_identity(row, stored)

    display_name = result.get("display_name") or stored.get("display_name") or stored.get("email")
    plan = result.get("plan") or stored.get("plan")
    email = result.get("email") or stored.get("email")
    usage_rows = list(result.get("usage") or [])
    # Prefer live usage; fall back to last-known snapshot when the fetch returned empty
    # but we previously persisted windows (e.g. transient network blip).
    if not usage_rows and isinstance(stored.get("last_usage"), list) and stored["last_usage"]:
        usage_rows = list(stored["last_usage"])
    source = result.get("source") or "api"
    if usage_rows and not result.get("usage") and source == "unavailable":
        source = "cached"

    row = _base_row(
        friendly,
        str(aid),
        display_name=display_name,
        plan=plan,
        usage=usage_rows,
        dashboard_url=result.get("dashboard_url") or DASHBOARD_URLS.get(friendly),
        source=source,
        email=email,
    )
    # Ensure xAI/cursor never surface a bare UUID / acct-slug as the only label.
    row = _merge_identity(row, stored)
    if (
        not row.get("display_name")
        or _looks_like_uuid(row.get("display_name"))
        or _looks_like_acct_slug(row.get("display_name"))
    ):
        if friendly == "xai":
            fixed = enrich_xai_identity(dict(stored))
            row["display_name"] = fixed.get("display_name") or row.get("display_name")
            if fixed.get("plan") and not row.get("plan"):
                row["plan"] = fixed["plan"]
            if fixed.get("email") and not row.get("email"):
                row["email"] = fixed["email"]
        elif friendly == "cursor":
            plan_lbl = row.get("plan") or stored.get("plan")
            if stored.get("email"):
                row["display_name"] = stored["email"]
            elif plan_lbl:
                row["display_name"] = f"Cursor ({plan_lbl})"
            else:
                row["display_name"] = "Cursor"

    display_name = row.get("display_name")
    plan = row.get("plan")
    email = row.get("email")

    # Persist identity + last-known usage for cold-start restore.
    try:
        persist_identity(
            cred_id,
            str(aid),
            display_name=display_name if isinstance(display_name, str) else None,
            plan=plan if isinstance(plan, str) else None,
            email=email if isinstance(email, str) else None,
            usage=usage_rows if usage_rows else None,
            usage_fetched_at=now if usage_rows else None,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("usage: persist identity failed: %s", exc)

    _cache[key] = {"fetched_at": now, "rate_limited_until": 0, "row": row}
    return row


def subscription_usage(provider: str | None = None, *, force: bool = False) -> dict:
    """Collect usage rows for all (or one) connected subscription accounts.

    Returns ``{ok: True, rows: [...]}`` — contract-shaped, never a traceback.

    Multi-account pools (especially xAI) fetch in parallel so Settings does not
    wait on N serial billing/userinfo waterfalls. Cache / disk last_usage still
    short-circuit inside :func:`fetch_account_usage`.
    """
    targets: list[tuple[str, str, str]] = []  # friendly, cred_id, label
    if provider:
        resolved = sl._resolve(provider)
        if resolved is None:
            return {
                "ok": False,
                "reason": f"unknown subscription provider {provider!r}",
                "hint": "Use one of: codex, claude, cursor, xai.",
                "rows": [],
            }
        friendly, _name, cred_id, label = resolved
        targets.append((friendly, cred_id, label))
    else:
        for friendly, (_name, cred_id, label) in sl._PROVIDERS.items():
            targets.append((friendly, cred_id, label))

    jobs: list[tuple[str, str, dict]] = []  # friendly, aid, acct_meta
    for friendly, cred_id, _label in targets:
        for acct in sub_auth.list_accounts(cred_id):
            aid = str(acct.get("account_id") or "") or sub_auth.DEFAULT_ACCOUNT_ID
            jobs.append((friendly, aid, dict(acct)))

    if not jobs:
        return {"ok": True, "rows": []}

    def _one(job: tuple[str, str, dict]) -> dict:
        friendly, aid, acct = job
        try:
            row = fetch_account_usage(friendly, aid, force=force)
            return {
                "provider": row.get("provider") or friendly,
                "account_id": row.get("account_id") or aid,
                "display_name": row.get("display_name"),
                "plan": row.get("plan"),
                "usage": row.get("usage") or [],
                **({"dashboard_url": row["dashboard_url"]} if row.get("dashboard_url") else {}),
                **({"source": row["source"]} if row.get("source") else {}),
                **({"email": row["email"]} if row.get("email") else {}),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("usage: row build failed for %s/%s: %s", friendly, aid, exc)
            return _base_row(
                friendly,
                aid,
                display_name=acct.get("display_name") or acct.get("email"),
                plan=acct.get("plan"),
                usage=[],
                source="unavailable",
                email=acct.get("email"),
            )

    # Keep order stable (provider targets × account list order) for UI merge.
    rows: list[dict | None] = [None] * len(jobs)
    # Bound workers: enough to hide RTT, not enough to hammer every vendor.
    max_workers = min(12, max(1, len(jobs)))
    # Single-account path stays inline (tests + simple providers).
    if len(jobs) == 1:
        rows[0] = _one(jobs[0])
    else:
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sub-usage") as pool:
            fut_map = {pool.submit(_one, job): idx for idx, job in enumerate(jobs)}
            for fut in as_completed(fut_map):
                idx = fut_map[fut]
                try:
                    rows[idx] = fut.result()
                except Exception as exc:  # noqa: BLE001 — should be rare; _one already catches
                    friendly, aid, acct = jobs[idx]
                    log.warning("usage: parallel row failed for %s/%s: %s", friendly, aid, exc)
                    rows[idx] = _base_row(
                        friendly,
                        aid,
                        display_name=acct.get("display_name") or acct.get("email"),
                        plan=acct.get("plan"),
                        usage=[],
                        source="unavailable",
                        email=acct.get("email"),
                    )

    return {"ok": True, "rows": [r for r in rows if isinstance(r, dict)]}


# Login-time profile helpers (called from providers after token store) ─────────


def enrich_anthropic_profile(cred: dict) -> dict:
    """Best-effort GET /api/oauth/profile; mutates and returns *cred*.

    Also best-effort seeds ``last_usage`` from /api/oauth/usage so a cold restart
    can restore bars before the next live fetch (Settings "Usage unavailable").
    """
    token = cred.get("access_token")
    if not token:
        return cred
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": _ANTHROPIC_BETA,
        "Accept": "application/json",
        "User-Agent": _ANTHROPIC_UA,
    }
    try:
        status, body, _ = _http_get(f"{_ANTHROPIC_OAUTH_BASE}/profile", headers, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        log.debug("anthropic login profile failed: %s", exc)
        body = None
        status = 0
    if status == 200 and isinstance(body, dict):
        email = body.get("email")
        if not email and isinstance(body.get("account"), dict):
            email = body["account"].get("email")
        if isinstance(email, str) and email.strip():
            cred["email"] = email.strip().lower()
        name = body.get("display_name") or body.get("name")
        if isinstance(name, str) and name.strip():
            cred["display_name"] = name.strip()
        elif cred.get("email"):
            cred.setdefault("display_name", cred["email"])
        for k in ("subscriptionType", "subscription_type", "plan"):
            v = body.get(k)
            if isinstance(v, str) and v.strip():
                cred["plan"] = v.strip()
                break
        uuid_ = body.get("uuid") or body.get("account_id")
        if not uuid_ and isinstance(body.get("account"), dict):
            uuid_ = body["account"].get("uuid") or body["account"].get("id")
        if uuid_ and not cred.get("account_id"):
            cred["account_id"] = str(uuid_)

    # Seed last_usage at login / load so cold restart has something to restore.
    if not cred.get("last_usage"):
        try:
            fetched = fetch_anthropic_usage(cred)
            windows = fetched.get("usage") or []
            if windows:
                cred["last_usage"] = list(windows)
                cred["last_usage_fetched_at"] = _now()
            # Prefer profile/plan from the usage path when still missing.
            if fetched.get("plan") and not cred.get("plan"):
                cred["plan"] = fetched["plan"]
            if fetched.get("display_name") and (
                not cred.get("display_name")
                or _looks_like_uuid(cred.get("display_name"))
            ):
                cred["display_name"] = fetched["display_name"]
            if fetched.get("email") and not cred.get("email"):
                cred["email"] = fetched["email"]
        except _RateLimited:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("anthropic login usage seed failed: %s", exc)
    return cred


def enrich_cursor_profile(cred: dict) -> dict:
    """Best-effort full_stripe_profile at login for plan/email.

    Always leaves a human ``display_name`` (email / plan / "Cursor") so Settings
    never falls back to a bare ``acct-…`` account_id slug.
    """
    token = cred.get("access_token")
    if not token:
        # Offline identity fallback from whatever we already have.
        if not cred.get("display_name") or _looks_like_acct_slug(cred.get("display_name")):
            if cred.get("email"):
                cred["display_name"] = cred["email"]
            elif cred.get("plan"):
                cred["display_name"] = f"Cursor ({cred['plan']})"
            else:
                cred["display_name"] = "Cursor"
        return cred
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    # JWT claims first (no network).
    claims = sub_auth.decode_jwt_payload(token)
    if not cred.get("email"):
        for key in ("email", "preferred_username", "unique_name", "upn"):
            v = claims.get(key)
            if isinstance(v, str) and "@" in v:
                cred["email"] = v.strip().lower()
                break
    if not cred.get("account_id"):
        for key in ("sub", "user_id", "userId", "id"):
            v = claims.get(key)
            if v is not None and str(v).strip() and not _looks_like_acct_slug(str(v)):
                # Keep numeric/user ids; timestamp acct-* is not a real id.
                cred["account_id"] = str(v).strip()
                break
    if not cred.get("display_name"):
        for key in ("name", "display_name", "displayName"):
            v = claims.get(key)
            if isinstance(v, str) and v.strip():
                cred["display_name"] = v.strip()
                break
    try:
        status, body, _ = _http_get(_CURSOR_STRIPE_URL, headers, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        log.debug("cursor login profile failed: %s", exc)
        body = None
        status = 0
    if status == 200 and isinstance(body, dict):
        mt = body.get("membershipType") or body.get("membership_type") or body.get("plan")
        if isinstance(mt, str) and mt.strip():
            cred["plan"] = mt.strip()
        if isinstance(body.get("email"), str) and body["email"].strip():
            cred["email"] = body["email"].strip().lower()
        name = body.get("name") or body.get("displayName") or body.get("display_name")
        if isinstance(name, str) and name.strip():
            cred["display_name"] = name.strip()
        if not cred.get("account_id"):
            for k in ("userId", "user_id", "id", "customerId"):
                v = body.get(k)
                if v is not None and str(v).strip():
                    cred["account_id"] = str(v).strip()
                    break
    # Final friendly label — never leave bare acct-* or empty.
    dn = cred.get("display_name")
    if not dn or _looks_like_uuid(dn) or _looks_like_acct_slug(dn):
        if cred.get("email"):
            cred["display_name"] = cred["email"]
        elif cred.get("plan"):
            cred["display_name"] = f"Cursor ({cred['plan']})"
        else:
            cred["display_name"] = "Cursor"
    return cred


def enrich_codex_identity(cred: dict) -> dict:
    """Fill display_name from JWT email (plan arrives later via wham/usage)."""
    if cred.get("email") and not cred.get("display_name"):
        cred["display_name"] = cred["email"]
    token = cred.get("access_token") or ""
    if token and (not cred.get("email") or not cred.get("account_id")):
        claims = sub_auth.decode_jwt_payload(token)
        if not cred.get("account_id"):
            aid = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
            if aid:
                cred["account_id"] = aid
        if not cred.get("email"):
            email = ((claims.get("https://api.openai.com/profile") or {}).get("email") or "").lower().strip()
            if email:
                cred["email"] = email
                cred.setdefault("display_name", email)
    return cred


def enrich_xai_identity(cred: dict) -> dict:
    """Fill display_name + plan + email for an xAI credential.

    xAI OAuth JWTs only carry ``sub`` / ``tier`` / ``team_id`` — never email or
    name. Live identity comes from ``GET auth.x.ai/oauth2/userinfo`` (OIDC).
    Falls back to JWT claims / Grok (plan · short-id) when offline.
    """
    token = cred.get("access_token") or cred.get("id_token") or ""
    claims: dict = sub_auth.decode_jwt_payload(token) if token else {}

    # Live OIDC userinfo — the only reliable name/email source.
    # Skip obviously-fake unit-test tokens (no JWT shape) to keep offline tests fast.
    if token and token.count(".") >= 2 and len(token) > 40:
        try:
            st, body, _rh = _http_get(
                XAI_USERINFO_URL,
                headers=_xai_auth_headers(cred),
                timeout=12.0,
            )
            if st == 200 and isinstance(body, dict):
                email_u = body.get("email")
                if isinstance(email_u, str) and "@" in email_u:
                    cred["email"] = email_u.strip().lower()
                name_u = body.get("name") or body.get("preferred_username")
                if isinstance(name_u, str) and name_u.strip() and not _looks_like_uuid(name_u.strip()):
                    cred["display_name"] = name_u.strip()
                sub_u = body.get("sub")
                if sub_u is not None and str(sub_u).strip() and not cred.get("account_id"):
                    cred["account_id"] = str(sub_u).strip()
        except Exception as exc:  # noqa: BLE001 — offline / network; JWT fallback
            log.debug("xAI userinfo enrich failed: %s", exc)

    # Email from cred or JWT (usually absent on xAI access tokens).
    email = cred.get("email")
    if not email:
        for key in ("email", "preferred_username", "unique_name"):
            v = claims.get(key)
            if isinstance(v, str) and "@" in v:
                email = v.strip().lower()
                break
        if not email:
            nested = claims.get("profile") if isinstance(claims.get("profile"), dict) else {}
            if isinstance(nested.get("email"), str) and "@" in nested["email"]:
                email = nested["email"].strip().lower()
        if email:
            cred["email"] = email

    # Plan from JWT / stored.
    plan = _infer_xai_plan(cred)
    if plan:
        cred["plan"] = plan

    # account_id from sub when missing.
    if not cred.get("account_id"):
        sub = claims.get("sub") or claims.get("account_id") or claims.get("user_id")
        if sub is not None and str(sub).strip():
            cred["account_id"] = str(sub).strip()

    # Human display_name — never a bare UUID.
    name = None
    for key in ("name", "display_name", "preferred_username", "given_name"):
        v = claims.get(key)
        if isinstance(v, str) and v.strip() and not _looks_like_uuid(v.strip()):
            name = v.strip()
            break
    nested = claims.get("profile") if isinstance(claims.get("profile"), dict) else {}
    if not name:
        for key in ("name", "display_name"):
            v = nested.get(key)
            if isinstance(v, str) and v.strip() and not _looks_like_uuid(v.strip()):
                name = v.strip()
                break

    current = cred.get("display_name")
    if current and not (
        _looks_like_uuid(current)
        or _looks_like_acct_slug(current)
        or (isinstance(current, str) and current.startswith("Grok ("))
    ):
        # Keep a good existing label (real name or email).
        pass
    elif name and not (_looks_like_uuid(name) or (name == cred.get("account_id"))):
        cred["display_name"] = name
    elif email:
        cred["display_name"] = email
    elif plan:
        short = _short_id(cred.get("account_id"))
        cred["display_name"] = f"Grok ({plan})" if not short else f"Grok ({plan} · {short})"
    else:
        short = _short_id(cred.get("account_id"))
        cred["display_name"] = f"Grok ({short})" if short else "Grok"
    return cred
