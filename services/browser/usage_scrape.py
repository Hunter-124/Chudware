"""Per-account Grok.com SuperGrok usage scrape via CEF daemon profiles.

Primary path: same-origin ``POST https://grok.com/rest/rate-limits`` under a
per-account CEF cookie jar (never the shared warm profile). Maps remaining
query pools into the providers/usage envelope (``source=browser_scrape``).

Hard rules:
- One daemon profile dir per account: ``{cef_cache_daemon}/accounts/xai/{id}/daemon``
- Lock key ``xai--{id}`` (profiles.profile_lock) — never share warm across accounts
- Detect Cloudflare Turnstile / challenges; never solve them
- 401/redirect → ``needs_login`` + dashboard URL; transport/parse errors raise for
  the caller to fall back to jwt_inference
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import challenge, profiles

log = logging.getLogger("browser.usage_scrape")

DASHBOARD_URL = "https://grok.com/?_s=usage"
RATE_LIMITS_PATH = "/rest/rate-limits"
ORIGIN_URL = "https://grok.com/"
RATE_LIMITS_URL = "https://grok.com/rest/rate-limits"

#: Background cadence floor (seconds). Honored by usage.CACHE_TTL_S; also
#: stamped into profile meta so multi-callers can respect the same floor.
MIN_INTERVAL_S = 5 * 60

DEFAULT_REQUEST_KIND = "DEFAULT"
DEFAULT_PROBE_MODELS: tuple[str, ...] = ("grok-4.5", "grok-4-heavy")

SENTINEL_ID = "__aicad_capture"
_SENTINEL_RE = re.compile(
    rf'id=["\']?{re.escape(SENTINEL_ID)}["\']?[^>]*>(?P<body>.*?)</',
    re.IGNORECASE | re.DOTALL,
)
_SENTINEL_TEXT_RE = re.compile(
    rf'id=["\']?{re.escape(SENTINEL_ID)}["\']?[^>]*>(?P<body>[^<]*)',
    re.IGNORECASE,
)

# Login / auth URL fragments (redirect targets).
_LOGIN_URL_MARKERS: tuple[str, ...] = (
    "/sign-in", "/signin", "/sign_in", "/login", "/log-in",
    "/accounts.x.ai", "/auth", "oauth", "sso",
)


class UsageScrapeError(RuntimeError):
    """Transport or unexpected scrape failure (caller may fall back)."""


# ── Profile paths ────────────────────────────────────────────────────────────


def daemon_cache_root() -> Path:
    """Daemon CEF ``cache_root()`` mirror (Linux layout; env override first).

    Must match ``cef_platform::cache_root()`` when ``set_daemon_process(true)`` so
    profile paths the Python client passes land under the daemon's root_cache_path.
    """
    if ov := os.environ.get("CHUDWARE_CEF_CACHE_ROOT"):
        return Path(ov)
    if xdg := os.environ.get("XDG_DATA_HOME"):
        if xdg.strip():
            return Path(xdg) / "ai-cad" / "cef_cache_daemon"
    home = os.environ.get("HOME") or ""
    if home:
        return Path(home) / ".local" / "share" / "ai-cad" / "cef_cache_daemon"
    return Path.cwd() / "cef_cache_daemon"


def account_site_key(account_id: str) -> str:
    """Lock / meta site key: ``xai--{slug(account_id)}``."""
    return f"xai--{profiles.slug(account_id or 'default')}"


def account_daemon_profile(account_id: str) -> Path:
    """Absolute per-account daemon cookie-jar directory (created if missing)."""
    aid = profiles.slug(account_id or "default")
    p = daemon_cache_root() / "accounts" / "xai" / aid / "daemon"
    p.mkdir(parents=True, exist_ok=True)
    return p


def account_panel_profile(account_id: str) -> Path:
    """Optional per-account interactive panel jar (login UI; import_session → daemon)."""
    aid = profiles.slug(account_id or "default")
    p = daemon_cache_root() / "accounts" / "xai" / aid / "panel"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Dual-schema rate-limit parse ─────────────────────────────────────────────


def _num(value: Any) -> float | None:
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


def _extract_bucket(data: dict) -> dict[str, Any] | None:
    """Pull remaining/total/window from flat or nested rate-limit JSON."""
    if not isinstance(data, dict):
        return None

    remaining = _num(data.get("remainingQueries"))
    total = _num(data.get("totalQueries"))
    window_s = _num(data.get("windowSizeSeconds"))

    # Nested schema: {remaining, limit, windowSizeSeconds} (extension sniffer).
    if remaining is None:
        remaining = _num(data.get("remaining"))
    if total is None:
        total = _num(data.get("limit")) or _num(data.get("total"))
    if window_s is None:
        window_s = _num(data.get("window_size_seconds")) or _num(data.get("windowSeconds"))

    # Nested under rateLimits / rate_limit / DEFAULT etc.
    if remaining is None and total is None:
        for nest_key in ("rateLimits", "rate_limit", "DEFAULT", "default", "data"):
            nested = data.get(nest_key)
            if isinstance(nested, dict):
                got = _extract_bucket(nested)
                if got is not None:
                    return got

    if remaining is None and total is None:
        return None

    out: dict[str, Any] = {
        "remaining": remaining,
        "total": total,
        "window_size_seconds": window_s,
    }
    # Token pools when present (not required for bars).
    rt = _num(data.get("remainingTokens"))
    tt = _num(data.get("totalTokens"))
    if rt is not None:
        out["remaining_tokens"] = rt
    if tt is not None:
        out["total_tokens"] = tt
    return out


def _effort_bucket(data: dict, key: str) -> dict[str, Any] | None:
    block = data.get(key)
    if not isinstance(block, dict):
        # Alternate casings
        for alt in (key, key.replace("RateLimits", "_rate_limits"),
                    key[0].lower() + key[1:] if key else key):
            block = data.get(alt)
            if isinstance(block, dict):
                break
        else:
            return None
    return _extract_bucket(block)


def parse_rate_limit_payload(data: Any) -> dict[str, Any] | None:
    """Parse a rate-limits JSON body (dict or JSON string) into normalized buckets.

    Returns::

        {
          "primary": {remaining, total, window_size_seconds, ...} | None,
          "low_effort": {...} | None,
          "high_effort": {...} | None,
          "raw_keys": [...],
        }

    or ``None`` when nothing usable is present.
    """
    if isinstance(data, str):
        s = data.strip()
        if not s:
            return None
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None

    primary = _extract_bucket(data)
    low = _effort_bucket(data, "lowEffortRateLimits")
    high = _effort_bucket(data, "highEffortRateLimits")

    if primary is None and low is None and high is None:
        return None

    return {
        "primary": primary,
        "low_effort": low,
        "high_effort": high,
        "raw_keys": sorted(str(k) for k in data.keys()),
    }


def _used_fraction(remaining: float | None, total: float | None) -> float | None:
    if remaining is None or total is None or total <= 0:
        return None
    used = (total - remaining) / total
    if used < 0:
        used = 0.0
    if used > 1:
        used = 1.0
    return used


def _status_from_fraction(frac: float | None) -> str:
    if frac is None:
        return "unknown"
    if frac >= 1.0:
        return "exhausted"
    if frac >= 0.9:
        return "warning"
    return "ok"


def _window_id(window_s: float | None) -> tuple[str, str]:
    """Map windowSizeSeconds → (window id, label). Product default is weekly."""
    if window_s is None:
        return "weekly", "Weekly pool"
    try:
        s = float(window_s)
    except (TypeError, ValueError):
        return "weekly", "Weekly pool"
    if s >= 86_400 * 5:  # ~5+ days → weekly
        return "weekly", "Weekly pool"
    if s >= 86_400:
        days = max(1, int(round(s / 86_400)))
        return f"{days}d", f"{days} day" if days == 1 else f"{days} days"
    if s >= 3600:
        hours = max(1, int(round(s / 3600)))
        return f"{hours}h", f"{hours} hour" if hours == 1 else f"{hours} hours"
    return "window", f"{int(s)}s window"


def _usage_row_from_bucket(
    bucket: dict[str, Any],
    *,
    window: str | None = None,
    label: str | None = None,
    row_id: str | None = None,
) -> dict[str, Any]:
    remaining = bucket.get("remaining")
    total = bucket.get("total")
    window_s = bucket.get("window_size_seconds")
    wid, wlabel = _window_id(window_s if isinstance(window_s, (int, float)) else None)
    if window is not None:
        wid = window
    if label is not None:
        wlabel = label
    frac = _used_fraction(
        float(remaining) if remaining is not None else None,
        float(total) if total is not None else None,
    )
    status = _status_from_fraction(frac)
    if remaining is not None and float(remaining) <= 0 and (total is None or float(total) > 0):
        status = "exhausted"
    row: dict[str, Any] = {
        "window": wid,
        "label": wlabel,
        "used_fraction": frac,
        "resets_at": None,
        "status": status,
    }
    if row_id is not None:
        row["id"] = row_id
    if remaining is not None:
        row["remaining"] = remaining
    if total is not None:
        row["limit"] = total
    if window_s is not None:
        row["window_size_seconds"] = window_s
    return row


def buckets_to_usage_rows(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert parse_rate_limit_payload output → usage[] rows for the UI envelope."""
    rows: list[dict[str, Any]] = []
    primary = parsed.get("primary")
    if isinstance(primary, dict):
        rows.append(_usage_row_from_bucket(
            primary, row_id="xai:web:default", label="Grok web",
        ))
    low = parsed.get("low_effort")
    if isinstance(low, dict):
        rows.append(_usage_row_from_bucket(
            low, window="low_effort", label="Low effort", row_id="xai:web:low",
        ))
    high = parsed.get("high_effort")
    if isinstance(high, dict):
        rows.append(_usage_row_from_bucket(
            high, window="high_effort", label="High effort", row_id="xai:web:high",
        ))
    return rows


# ── Sentinel / login / challenge helpers ─────────────────────────────────────


def extract_sentinel_payload(html: str, sentinel: str = SENTINEL_ID) -> Any | None:
    """Pull JSON textContent from the interact sentinel div."""
    if not html:
        return None
    # Prefer attribute-free text body match.
    for rx in (_SENTINEL_TEXT_RE, _SENTINEL_RE):
        if sentinel != SENTINEL_ID:
            rx = re.compile(
                rf'id=["\']?{re.escape(sentinel)}["\']?[^>]*>(?P<body>[^<]*)',
                re.IGNORECASE,
            )
        m = rx.search(html)
        if not m:
            continue
        raw = (m.group("body") or "").strip()
        if not raw:
            continue
        # HTML entity decode minimal
        raw = raw.replace("&quot;", '"').replace("&#34;", '"').replace("&amp;", "&")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    # Fallback: find sentinel id then nearest {...}
    idx = html.find(sentinel)
    if idx < 0:
        return None
    brace = html.find("{", idx)
    if brace < 0:
        return None
    depth = 0
    for i in range(brace, min(len(html), brace + 200_000)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = html[brace : i + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    return None
    return None


def _looks_like_login_url(url: str | None) -> bool:
    if not url:
        return False
    u = url.lower()
    if "grok.com" in u and any(m in u for m in ("/sign", "/login", "accounts.")):
        return True
    return any(m in u for m in _LOGIN_URL_MARKERS)


def detect_login_required(
    *,
    http_status: int | None = None,
    final_url: str | None = None,
    html: str | None = None,
    sentinel: Any | None = None,
) -> bool:
    """True when the session is missing / expired (401, auth redirect, login CTA)."""
    if http_status in (401, 403):
        return True
    if isinstance(sentinel, dict):
        sh = sentinel.get("http")
        if sh in (401, 403):
            return True
        if sentinel.get("redirected") and _looks_like_login_url(str(sentinel.get("url") or "")):
            return True
        if _looks_like_login_url(str(sentinel.get("url") or "")):
            return True
    if _looks_like_login_url(final_url):
        return True
    hay = (html or "").lower()
    if hay and len(hay) < 80_000:
        # Small pages with explicit sign-in CTAs.
        if ("sign in" in hay or "log in" in hay) and (
            "grok" in hay or "x.ai" in hay or "continue with" in hay
        ):
            # Avoid flagging the logged-in usage SPA which may still mention sign-in in footers.
            if "remainingqueries" in hay or "rate-limits" in hay:
                return False
            if "sign in to grok" in hay or "log in to continue" in hay or 'href="/sign-in"' in hay:
                return True
    return False


def detect_challenge_blocked(html: str | None, url: str = "") -> bool:
    """True on Cloudflare Turnstile / captcha interstitial — detect only, never solve."""
    if not html:
        return False
    if challenge.is_challenge(html, url):
        return True
    hay = html.lower()
    # Extra grok-specific human-verify loops reported in the field.
    if "cf-turnstile" in hay or "challenges.cloudflare.com" in hay:
        if "verify you are human" in hay or "just a moment" in hay or "cf-challenge" in hay:
            return True
    return False


def _rate_limits_script(request_kind: str, model_name: str, sentinel: str = SENTINEL_ID) -> str:
    """Page JS: POST /rest/rate-limits with credentials; publish JSON to sentinel."""
    # Keep this as a single expression-friendly IIFE; no template literals (CEF/old).
    kind_js = json.dumps(request_kind)
    model_js = json.dumps(model_name)
    sid_js = json.dumps(sentinel)
    path_js = json.dumps(RATE_LIMITS_PATH)
    return (
        "(function(){"
        f"var SID={sid_js};var KIND={kind_js};var MODEL={model_js};var PATH={path_js};"
        "function pub(obj){"
        "var d=document.createElement('div');d.id=SID;"
        "try{d.textContent=JSON.stringify(obj);}catch(e){d.textContent='{\"error\":\"stringify\"}';}"
        "document.documentElement.appendChild(d);}"
        "try{"
        "fetch(PATH,{"
        "method:'POST',credentials:'include',"
        "headers:{'Content-Type':'application/json','Accept':'application/json'},"
        "body:JSON.stringify({requestKind:KIND,modelName:MODEL})"
        "}).then(function(r){"
        "return r.text().then(function(t){"
        "pub({http:r.status,url:String(r.url||''),redirected:!!r.redirected,body:t});"
        "});"
        "}).catch(function(e){pub({error:String(e&&e.message||e)});});"
        "}catch(e){pub({error:String(e&&e.message||e)});}"
        "})();"
    )


# ── Core scrape ──────────────────────────────────────────────────────────────


def _needs_login_result(account_id: str, cred: dict | None = None) -> dict[str, Any]:
    cred = cred or {}
    return {
        "ok": False,
        "status": "needs_login",
        "source": "browser_scrape",
        "dashboard_url": DASHBOARD_URL,
        "account_id": account_id or cred.get("account_id"),
        "email": cred.get("email"),
        "display_name": cred.get("display_name") or cred.get("email"),
        "plan": cred.get("plan"),
        "usage": [{
            "window": "weekly",
            "label": "Weekly pool",
            "used_fraction": None,
            "resets_at": None,
            "status": "needs_login",
        }],
        "fetched_at": int(time.time() * 1000),
    }


def _blocked_result(account_id: str, cred: dict | None = None, *, reason: str = "challenge") -> dict[str, Any]:
    cred = cred or {}
    return {
        "ok": False,
        "status": "blocked",
        "source": "browser_scrape",
        "dashboard_url": DASHBOARD_URL,
        "account_id": account_id or cred.get("account_id"),
        "email": cred.get("email"),
        "display_name": cred.get("display_name") or cred.get("email"),
        "plan": cred.get("plan"),
        "usage": [{
            "window": "weekly",
            "label": "Weekly pool",
            "used_fraction": None,
            "resets_at": None,
            "status": "blocked",
        }],
        "details": [f"challenge:{reason}"],
        "fetched_at": int(time.time() * 1000),
    }


def _ok_result(
    account_id: str,
    usage_rows: list[dict[str, Any]],
    cred: dict | None = None,
    *,
    details: list[str] | None = None,
    raw: Any = None,
) -> dict[str, Any]:
    cred = cred or {}
    out: dict[str, Any] = {
        "ok": True,
        "status": "ok",
        "source": "browser_scrape",
        "dashboard_url": DASHBOARD_URL,
        "account_id": account_id or cred.get("account_id"),
        "email": cred.get("email"),
        "display_name": cred.get("display_name") or cred.get("email"),
        "plan": cred.get("plan"),
        "usage": usage_rows,
        "fetched_at": int(time.time() * 1000),
    }
    if details:
        out["details"] = details
    if raw is not None and os.environ.get("CHUDWARE_USAGE_SCRAPE_RAW"):
        out["raw"] = raw
    # Promote exhausted/warning if any row is depleted.
    for row in usage_rows:
        st = row.get("status")
        if st == "exhausted":
            out["status"] = "exhausted"
            break
        if st == "warning" and out["status"] == "ok":
            out["status"] = "warning"
    return out


def fetch_grok_usage(
    account_id: str,
    cred: dict | None = None,
    *,
    cef: Any | None = None,
    probe_models: Iterable[str] | None = None,
    request_kind: str = DEFAULT_REQUEST_KIND,
    timeout_s: float = 45.0,
) -> dict[str, Any]:
    """Scrape SuperGrok web pool remaining for *account_id* under its CEF profile.

    Returns a usage envelope::

        {
          ok, status, source=browser_scrape, dashboard_url, usage: [...],
          account_id, email, plan, display_name, fetched_at, ...
        }

    Structured statuses (not exceptions):
    - ``needs_login`` — 401/auth redirect; UI should CTA the dashboard / panel login
    - ``blocked`` — Turnstile/challenge detected (never auto-solved)

    Raises :class:`UsageScrapeError` on daemon/transport/empty-parse failures so
    ``fetch_xai_usage_browser_scrape`` can fall back to jwt_inference.
    """
    cred = dict(cred or {})
    aid = str(account_id or cred.get("account_id") or "").strip() or "default"
    site_key = account_site_key(aid)
    profile_dir = str(account_daemon_profile(aid))
    models = tuple(probe_models) if probe_models is not None else DEFAULT_PROBE_MODELS
    if not models:
        models = DEFAULT_PROBE_MODELS

    lock = profiles.profile_lock(site_key)
    if not lock.acquire(blocking=False):
        raise UsageScrapeError(f"profile busy for {site_key}")

    try:
        if cef is None:
            from . import cef_client  # lazy: keep import cheap for pure unit tests

            cef = cef_client.client()

        last_html = ""
        last_final_url = ORIGIN_URL
        last_status: int | None = None
        collected: list[dict[str, Any]] = []
        details: list[str] = []

        for model in models:
            script = _rate_limits_script(request_kind, model)
            try:
                r = cef.interact(
                    ORIGIN_URL,
                    script,
                    sentinel=SENTINEL_ID,
                    settle_ms=1500,
                    timeout_ms=int(max(5.0, timeout_s - 5.0) * 1000),
                    profile=profile_dir,
                    timeout_s=timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 — daemon spawn/connect/protocol
                raise UsageScrapeError(f"CEF interact failed: {exc}") from exc

            if not isinstance(r, dict) or not r.get("ok"):
                raise UsageScrapeError(f"CEF interact not ok for model={model}")

            html = r.get("html") or ""
            final_url = r.get("final_url") or ORIGIN_URL
            http_status = r.get("status")
            last_html = html
            last_final_url = final_url
            last_status = http_status if isinstance(http_status, int) else None

            if detect_challenge_blocked(html, final_url):
                log.info("grok usage scrape blocked by challenge account=%s", aid)
                return _blocked_result(aid, cred, reason="captcha")

            if detect_login_required(
                http_status=last_status, final_url=final_url, html=html,
            ):
                log.info("grok usage scrape needs_login (page) account=%s", aid)
                return _needs_login_result(aid, cred)

            payload = extract_sentinel_payload(html, SENTINEL_ID)
            if payload is None:
                # No sentinel — page may still be a challenge/login without markers.
                if detect_challenge_blocked(html, final_url):
                    return _blocked_result(aid, cred, reason="captcha")
                if detect_login_required(http_status=last_status, final_url=final_url, html=html):
                    return _needs_login_result(aid, cred)
                details.append(f"no_sentinel:{model}")
                continue

            if isinstance(payload, dict) and payload.get("error"):
                details.append(f"js_error:{model}:{payload.get('error')}")
                continue

            if isinstance(payload, dict):
                sh = payload.get("http")
                if sh in (401, 403):
                    return _needs_login_result(aid, cred)
                if payload.get("redirected") and _looks_like_login_url(str(payload.get("url") or "")):
                    return _needs_login_result(aid, cred)
                body = payload.get("body")
            else:
                body = payload

            # Body may itself be HTML (challenge / login) rather than JSON.
            if isinstance(body, str) and body.lstrip().startswith("<"):
                if detect_challenge_blocked(body, RATE_LIMITS_URL):
                    return _blocked_result(aid, cred, reason="captcha")
                if detect_login_required(html=body, final_url=str(
                    payload.get("url") if isinstance(payload, dict) else final_url
                )):
                    return _needs_login_result(aid, cred)
                details.append(f"html_body:{model}")
                continue

            parsed = parse_rate_limit_payload(body)
            if parsed is None and isinstance(payload, dict):
                # Some builds return the rate-limit object at the top of the sentinel.
                parsed = parse_rate_limit_payload(payload)
            if parsed is None:
                details.append(f"unparsed:{model}")
                continue

            rows = buckets_to_usage_rows(parsed)
            if not rows:
                details.append(f"empty_rows:{model}")
                continue

            # Tag model on primary row for multi-probe merges.
            for row in rows:
                row.setdefault("model", model)
            collected.extend(rows)
            # First successful primary is enough for the weekly bar; still record
            # effort splits from that same payload.
            if parsed.get("primary") is not None:
                break

        if not collected:
            # Final challenge/login check on last page.
            if detect_challenge_blocked(last_html, last_final_url):
                return _blocked_result(aid, cred, reason="captcha")
            if detect_login_required(
                http_status=last_status, final_url=last_final_url, html=last_html,
            ):
                return _needs_login_result(aid, cred)
            raise UsageScrapeError(
                f"no rate-limit buckets for account={aid} details={details!r}"
            )

        # De-dupe by (window, label) keeping first (primary probe wins).
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for row in collected:
            key = (str(row.get("window")), str(row.get("label")))
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)

        try:
            profiles.save_meta(
                site_key,
                last_usage_fetch_ts=time.time(),
                last_used_ts=time.time(),
            )
        except Exception:  # noqa: BLE001 — meta is best-effort
            pass

        return _ok_result(aid, unique, cred, details=details or None)

    finally:
        lock.release()


def fetch_grok_usage_safe(
    account_id: str,
    cred: dict | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Like :func:`fetch_grok_usage` but never raises — returns ``status=error`` envelope."""
    try:
        return fetch_grok_usage(account_id, cred, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_grok_usage_safe: %s", exc)
        cred = cred or {}
        return {
            "ok": False,
            "status": "error",
            "source": "browser_scrape",
            "dashboard_url": DASHBOARD_URL,
            "account_id": account_id or cred.get("account_id"),
            "email": cred.get("email"),
            "usage": [{
                "window": "weekly",
                "label": "Weekly pool",
                "used_fraction": None,
                "resets_at": None,
                "status": "unknown",
            }],
            "error": str(exc),
            "fetched_at": int(time.time() * 1000),
        }
