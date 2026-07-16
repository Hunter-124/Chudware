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
}

DASHBOARD_URLS = {
    "claude": "https://claude.ai/settings/usage",
    "codex": "https://chatgpt.com/",
    "cursor": "https://cursor.com/dashboard/usage",
    "xai": "https://grok.com/?_s=usage",
}

# xAI usage source enum (wave-2: browser_scrape via CEF per-account profiles).
XAI_SOURCE_JWT = "jwt_inference"
XAI_SOURCE_BROWSER = "browser_scrape"
XAI_USAGE_SOURCE = XAI_SOURCE_BROWSER  # CEF rate-limits scrape; jwt_inference on failure

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
        sub_auth.save_credential(cred_id, cred, account_id=aid, set_active=False)

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


def fetch_cursor_usage(cred: dict) -> dict:
    """Cursor Pro: /auth/usage + optional /auth/full_stripe_profile for plan.

    Usage endpoint needs a live personal OAuth access token:
      GET https://api2.cursor.sh/auth/usage
      Authorization: Bearer <access_token>
      Accept: application/json

    Some tokens (stale refresh, enterprise SSO, CLI-exchanged keys) return 401/403
    on /auth/usage even while /auth/full_stripe_profile still answers — in that
    case we still return a human display_name/plan and an empty usage list so
    Settings never shows a bare ``acct-…`` row.
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


def _infer_xai_plan(cred: dict) -> str | None:
    """Infer SuperGrok / Premium+ tier from stored plan or JWT claims."""
    if cred.get("plan"):
        return str(cred["plan"])
    token = cred.get("access_token") or cred.get("id_token") or ""
    if not token:
        return None
    claims = sub_auth.decode_jwt_payload(token)
    for key in (
        "plan", "tier", "subscription", "subscription_tier", "product",
        "grok_tier", "xai_tier", "account_tier",
    ):
        v = claims.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("name", "type", "id", "tier", "plan"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    # Nested app_metadata / user_metadata common in OIDC
    for nest in ("app_metadata", "user_metadata", "https://x.ai/claims"):
        block = claims.get(nest)
        if not isinstance(block, dict):
            continue
        for key in ("plan", "tier", "subscription", "product"):
            v = block.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def fetch_xai_usage_jwt_inference(cred: dict) -> dict:
    """xAI degrade path: plan from JWT / stored tier; no remaining-quota API."""
    # Re-run identity so cold-start credentials without display_name get a label.
    enriched = enrich_xai_identity(dict(cred))
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


def fetch_xai_usage(cred: dict) -> dict:
    """Dispatch xAI usage by pluggable source enum."""
    if XAI_USAGE_SOURCE == XAI_SOURCE_BROWSER:
        return fetch_xai_usage_browser_scrape(cred)
    return fetch_xai_usage_jwt_inference(cred)


class _RateLimited(Exception):
    """Usage endpoint returned 429 — freeze this account until cache TTL."""


_FETCHERS: dict[str, Callable[[dict], dict]] = {
    "claude": fetch_anthropic_usage,
    "codex": fetch_codex_usage,
    "cursor": fetch_cursor_usage,
    "xai": fetch_xai_usage,
}


# ── Public API ───────────────────────────────────────────────────────────────


def clear_cache() -> None:
    """Drop the in-process usage cache (tests)."""
    _cache.clear()

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

    rows: list[dict] = []
    for friendly, cred_id, _label in targets:
        for acct in sub_auth.list_accounts(cred_id):
            aid = acct["account_id"]
            try:
                row = fetch_account_usage(friendly, aid, force=force)
                # Strip internal-only noise for the RPC surface while keeping extras
                # (dashboard_url, source, email) that the UI can use.
                rows.append({
                    "provider": row.get("provider") or friendly,
                    "account_id": row.get("account_id") or aid,
                    "display_name": row.get("display_name"),
                    "plan": row.get("plan"),
                    "usage": row.get("usage") or [],
                    **({"dashboard_url": row["dashboard_url"]} if row.get("dashboard_url") else {}),
                    **({"source": row["source"]} if row.get("source") else {}),
                    **({"email": row["email"]} if row.get("email") else {}),
                })
            except Exception as exc:  # noqa: BLE001
                log.warning("usage: row build failed for %s/%s: %s", friendly, aid, exc)
                rows.append(_base_row(
                    friendly,
                    aid,
                    display_name=acct.get("display_name") or acct.get("email"),
                    plan=acct.get("plan"),
                    usage=[],
                    source="unavailable",
                    email=acct.get("email"),
                ))
    return {"ok": True, "rows": rows}


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
    """Fill display_name + plan from JWT claims at login.

    Never leaves display_name as a bare UUID (xAI ``sub``). Prefer email, then
    JWT name/preferred_username, then ``Grok (<plan>)`` / short-id fallback.
    """
    token = cred.get("access_token") or cred.get("id_token") or ""
    claims: dict = sub_auth.decode_jwt_payload(token) if token else {}

    # Email from cred or JWT.
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
            # Skip email-shaped preferred_username when we already have email as fallback.
            name = v.strip()
            break
        # Nested profile block
    nested = claims.get("profile") if isinstance(claims.get("profile"), dict) else {}
    if not name:
        for key in ("name", "display_name"):
            v = nested.get(key)
            if isinstance(v, str) and v.strip() and not _looks_like_uuid(v.strip()):
                name = v.strip()
                break

    current = cred.get("display_name")
    if current and not (_looks_like_uuid(current) or _looks_like_acct_slug(current)):
        # Keep a good existing label.
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
