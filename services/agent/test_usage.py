"""Subscription usage fetchers (contracts/providers.md §4.5) — unit-level, no network.

Mocks HTTP via usage._http_get; exercises cache TTL, 429 freeze, identity persistence,
and the subscription_usage RPC shape.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from services.agent.providers import sub_auth
from services.agent.providers import subscription_login as sl
from services.agent.providers import usage


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Point the credential store at a throwaway config.json."""
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(sub_auth.paths, "CONFIG", cfg)
    usage.clear_cache()
    return cfg


def _jwt(payload: dict) -> str:
    """Unsigned JWT-shaped token for claim extraction tests."""
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{head}.{body}.sig"


# ── Anthropic ────────────────────────────────────────────────────────────────


def test_anthropic_usage_normalizes_buckets(tmp_config, monkeypatch):
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "atok",
        "refresh_token": "rt",
        "email": "a@c.com",
        "account_id": "acct-a",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="acct-a")

    calls = []

    def fake_http(url, headers=None, timeout=20.0):
        calls.append(url)
        if url.endswith("/usage"):
            return 200, {
                "five_hour": {"utilization": 0.42, "resets_at": "2026-07-14T18:00:00Z"},
                "seven_day": {"utilization": 55, "resets_at": "2026-07-20T00:00:00Z"},
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "percent": 10,
                        "resets_at": "2026-07-20T00:00:00Z",
                        "scope": {"model": {"display_name": "Opus"}},
                        "is_active": True,
                    }
                ],
            }, {}
        if url.endswith("/profile"):
            return 200, {
                "email": "a@c.com",
                "display_name": "Ada",
                "subscriptionType": "max",
                "uuid": "acct-a",
            }, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)
    row = usage.fetch_account_usage("claude", "acct-a", force=True)

    assert row["provider"] == "claude"
    assert row["account_id"] == "acct-a"
    assert row["display_name"] == "Ada"
    assert row["plan"] == "max"
    assert row["source"] == "api"
    windows = {u["window"]: u for u in row["usage"]}
    assert "5h" in windows
    assert abs(windows["5h"]["used_fraction"] - 0.42) < 1e-9
    assert windows["5h"]["status"] == "ok"
    assert windows["5h"]["resets_at"]
    assert abs(windows["7d"]["used_fraction"] - 0.55) < 1e-9
    assert any(u["window"].startswith("7d_") for u in row["usage"])
    # Identity persisted onto the credential store.
    cred = sub_auth.load_credential("anthropic-sub", account_id="acct-a")
    assert cred["display_name"] == "Ada"
    assert cred["plan"] == "max"
    assert any(u.endswith("/usage") for u in calls)


def test_anthropic_429_freezes_cache(tmp_config, monkeypatch):
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "atok", "email": "a@c.com", "account_id": "a1",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="a1")

    # Seed a good cache entry first.
    def ok_http(url, headers=None, timeout=20.0):
        if url.endswith("/usage"):
            return 200, {"five_hour": {"utilization": 0.1}}, {}
        if url.endswith("/profile"):
            return 200, {"email": "a@c.com"}, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "_http_get", ok_http)
    first = usage.fetch_account_usage("claude", "a1", force=True)
    assert first["usage"]

    n = {"n": 0}

    def limited(url, headers=None, timeout=20.0):
        n["n"] += 1
        return 429, {"error": "rate_limit"}, {}

    monkeypatch.setattr(usage, "_http_get", limited)
    # force=True still hits network, then freezes.
    second = usage.fetch_account_usage("claude", "a1", force=True)
    assert second["usage"]  # last-good preserved
    assert n["n"] >= 1

    n["n"] = 0
    # A force refresh cannot override a provider-requested 429 freeze.
    third = usage.fetch_account_usage("claude", "a1", force=True)
    assert n["n"] == 0
    assert third["usage"]


# ── Codex ────────────────────────────────────────────────────────────────────


def test_codex_usage_windows_and_plan(tmp_config, monkeypatch):
    token = _jwt({
        "https://api.openai.com/auth": {"chatgpt_account_id": "acc-1"},
        "https://api.openai.com/profile": {"email": "u@openai.com"},
    })
    sub_auth.save_credential("openai-codex", {
        "access_token": token,
        "refresh_token": "rt",
        "account_id": "acc-1",
        "email": "u@openai.com",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="acc-1")

    def fake_http(url, headers=None, timeout=20.0):
        assert "ChatGPT-Account-Id" in (headers or {})
        assert headers["ChatGPT-Account-Id"] == "acc-1"
        assert url.endswith("/wham/usage")
        return 200, {
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 42.0,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 3600,
                },
                "secondary_window": {
                    "used_percent": 10.0,
                    "limit_window_seconds": 604800,
                    "reset_at": int(time.time()) + 86400,
                },
            },
        }, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)
    row = usage.fetch_account_usage("codex", "acc-1", force=True)
    assert row["provider"] == "codex"
    assert row["plan"] == "plus"
    assert row["display_name"] in ("u@openai.com", "u@openai.com")
    assert len(row["usage"]) == 2
    primary = row["usage"][0]
    assert abs(primary["used_fraction"] - 0.42) < 1e-9
    assert primary["status"] == "ok"
    assert primary["resets_at"]
    # plan persisted
    assert sub_auth.load_credential("openai-codex", "acc-1")["plan"] == "plus"


# ── Cursor ───────────────────────────────────────────────────────────────────


def test_cursor_usage_and_stripe_plan(tmp_config, monkeypatch):
    sub_auth.save_credential("cursor", {
        "access_token": "ctok",
        "refresh_token": "rt",
        "account_id": "c1",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="c1")

    def fake_http(url, headers=None, timeout=20.0):
        if url.endswith("/auth/usage"):
            return 200, {
                "gpt-4": {"numRequests": 40, "maxRequestUsage": 100},
                "planUsage": {"usdUsed": 5.0, "usdLimit": 20.0},
                "startOfMonth": "2026-07-01T00:00:00Z",
            }, {}
        if url.endswith("/full_stripe_profile"):
            return 200, {
                "membershipType": "pro",
                "email": "dev@cursor.com",
                "name": "Dev",
            }, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)
    row = usage.fetch_account_usage("cursor", "c1", force=True)
    assert row["provider"] == "cursor"
    assert row["plan"] == "pro"
    assert row["display_name"] == "Dev"
    assert row["email"] == "dev@cursor.com"
    assert len(row["usage"]) >= 1
    fracs = [u["used_fraction"] for u in row["usage"] if u["used_fraction"] is not None]
    assert any(abs(f - 0.4) < 1e-9 for f in fracs)
    assert any(abs(f - 0.25) < 1e-9 for f in fracs)


def test_cursor_period_usage_provides_fractions(tmp_config, monkeypatch):
    """When /auth/usage has null maxRequestUsage, dashboard period fills fractions."""
    sub_auth.save_credential("cursor", {
        "access_token": "ctok",
        "refresh_token": "rt",
        "account_id": "c1",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="c1")

    def fake_http(url, headers=None, timeout=20.0):
        if url.endswith("/auth/usage"):
            return 200, {
                "gpt-4": {
                    "numRequests": 0,
                    "maxRequestUsage": None,
                },
                "startOfMonth": "2026-06-19T08:52:01.000Z",
            }, {}
        if url.endswith("/full_stripe_profile"):
            return 200, {
                "membershipType": "pro",
                "email": "dev@cursor.com",
                "name": "Dev",
            }, {}
        return 404, None, {}

    def fake_request(method, url, headers=None, data=None, timeout=20.0):
        if "GetCurrentPeriodUsage" in url:
            return 200, {
                "billingCycleStart": "1781859121000",
                "billingCycleEnd": "1784451121000",
                "planUsage": {
                    "totalSpend": 9288,
                    "includedSpend": 2000,
                    "limit": 2000,
                    "autoPercentUsed": 40,
                    "apiPercentUsed": 100,
                    "totalPercentUsed": 75,
                },
            }, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)
    monkeypatch.setattr(usage, "_http_request", fake_request)
    row = usage.fetch_account_usage("cursor", "c1", force=True)
    fracs = {
        u["window"]: u["used_fraction"]
        for u in row["usage"]
        if u.get("used_fraction") is not None
    }
    assert abs(fracs.get("plan", -1) - 0.75) < 1e-9
    assert abs(fracs.get("auto", -1) - 0.40) < 1e-9
    assert abs(fracs.get("api", -1) - 1.0) < 1e-9
    assert any(u.get("resets_at") for u in row["usage"])


def test_parse_cursor_period_usage_spend_fallback():
    rows = usage._parse_cursor_period_usage({
        "billingCycleEnd": "1784451121000",
        "planUsage": {
            "includedSpend": 500,
            "limit": 2000,
        },
    })
    assert len(rows) == 1
    assert abs(rows[0]["used_fraction"] - 0.25) < 1e-9
    assert rows[0]["window"] == "plan"


# ── xAI degrade ──────────────────────────────────────────────────────────────


def test_xai_jwt_inference_degrade(tmp_config, monkeypatch):
    token = _jwt({
        "email": "g@x.ai",
        "sub": "user-1",
        "plan": "SuperGrok",
    })
    sub_auth.save_credential("xai-sub", {
        "access_token": token,
        "refresh_token": "rt",
        "account_id": "user-1",
        "email": "g@x.ai",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="user-1")

    # No network should be required for jwt_inference.
    def boom(*_a, **_k):
        raise AssertionError("xAI jwt_inference must not hit the network")

    monkeypatch.setattr(usage, "_http_get", boom)
    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_JWT)

    row = usage.fetch_account_usage("xai", "user-1", force=True)
    assert row["provider"] == "xai"
    assert row["plan"] == "SuperGrok"
    assert row["usage"] == []
    assert row["dashboard_url"] == "https://grok.com/?_s=usage"
    assert row["source"] == "jwt_inference"
    assert row["display_name"] in ("g@x.ai", "g@x.ai")


def test_xai_browser_scrape_uses_scrape_or_falls_back(tmp_config, monkeypatch):
    """browser_scrape source: success maps bars; any scrape failure → jwt_inference."""
    sub_auth.save_credential("xai-sub", {
        "access_token": "t",
        "email": "g@x.ai",
        "account_id": "u1",
        "plan": "Premium+",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="u1")
    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_BROWSER)

    # Failure path: CEF unavailable → jwt_inference fallback (no invented bars).
    def boom(*_a, **_k):
        raise RuntimeError("cef down")

    monkeypatch.setattr(usage, "fetch_xai_usage_browser_scrape", usage.fetch_xai_usage_browser_scrape)
    # Patch the scrape module import target.
    import services.browser.usage_scrape as scrape_mod
    monkeypatch.setattr(scrape_mod, "fetch_grok_usage", boom)

    row = usage.fetch_account_usage("xai", "u1", force=True)
    assert row["source"] == "jwt_inference"
    assert row["dashboard_url"] == "https://grok.com/?_s=usage"
    assert row["plan"] == "Premium+"
    assert row["usage"] == []

    # A live CEF session that needs login is honest about having no quota data:
    # the scraper's placeholder must not become a made-up 0% usage bar.
    usage.clear_cache()

    def needs_login(*_a, **_k):
        return {
            "ok": False,
            "status": "needs_login",
            "source": "browser_scrape",
            "dashboard_url": "https://grok.com/?_s=usage",
            "usage": [{
                "window": "weekly",
                "label": "Weekly pool",
                "used_fraction": None,
                "resets_at": None,
                "status": "needs_login",
            }],
        }

    monkeypatch.setattr(scrape_mod, "fetch_grok_usage", needs_login)
    login_row = usage.fetch_account_usage("xai", "u1", force=True)
    assert login_row["source"] == "browser_scrape"
    assert len(login_row["usage"]) == 1
    assert login_row["usage"][0]["used_fraction"] is None
    assert login_row["usage"][0]["status"] == "needs_login"

    # Success path: scrape returns weekly pool fractions.
    usage.clear_cache()

    def ok_scrape(account_id, cred, **_k):
        return {
            "ok": True,
            "status": "ok",
            "source": "browser_scrape",
            "dashboard_url": "https://grok.com/?_s=usage",
            "usage": [{
                "window": "weekly",
                "label": "Grok web",
                "used_fraction": 0.4,
                "resets_at": None,
                "status": "ok",
                "remaining": 60,
                "limit": 100,
            }],
        }

    monkeypatch.setattr(scrape_mod, "fetch_grok_usage", ok_scrape)
    row2 = usage.fetch_account_usage("xai", "u1", force=True)
    assert row2["source"] == "browser_scrape"
    assert len(row2["usage"]) == 1
    assert abs(row2["usage"][0]["used_fraction"] - 0.4) < 1e-9
    assert row2["usage"][0]["status"] == "ok"


# ── Cache TTL ────────────────────────────────────────────────────────────────


def test_cache_ttl_skips_network(tmp_config, monkeypatch):
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "t", "account_id": "a", "email": "e@c.com",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="a")
    n = {"n": 0}

    def fake_http(url, headers=None, timeout=20.0):
        n["n"] += 1
        if url.endswith("/usage"):
            return 200, {"five_hour": {"utilization": 0.2}}, {}
        return 200, {}, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)
    usage.fetch_account_usage("claude", "a", force=True)
    c1 = n["n"]
    usage.fetch_account_usage("claude", "a", force=False)
    assert n["n"] == c1  # cache hit
    usage.fetch_account_usage("claude", "a", force=True)
    assert n["n"] > c1  # force bypass



def test_get_cached_expires_and_logout_invalidates_account(tmp_config, monkeypatch):
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "t", "account_id": "a", "email": "e@c.com",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="a")
    clock = [1000.0]
    monkeypatch.setattr(usage, "_now", lambda: clock[0])
    monkeypatch.setattr(
        usage,
        "_FETCHERS",
        {"claude": lambda _cred: {
            "display_name": "E",
            "plan": "pro",
            "usage": [{"window": "5h", "label": "Session", "used_fraction": 0.2,
                       "resets_at": None, "status": "ok"}],
        }},
    )

    usage.fetch_account_usage("claude", "a", force=True)
    assert usage.get_cached("claude", "a") is not None
    clock[0] += usage.CACHE_TTL_S + 1
    assert usage.get_cached("claude", "a") is None

    clock[0] += 1
    usage.fetch_account_usage("claude", "a", force=True)
    assert usage.get_cached("claude", "a") is not None
    assert usage.sl.logout("claude", account_id="a")["ok"] is True
    assert usage.get_cached("claude", "a") is None

# ── subscription_usage aggregate + RPC ───────────────────────────────────────


def test_subscription_usage_rows_with_mocked_fetchers(tmp_config, monkeypatch):
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "t1", "email": "a@c.com", "account_id": "a1",
        "display_name": "Ada", "plan": "max",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="a1")
    sub_auth.save_credential("xai-sub", {
        "access_token": _jwt({"email": "g@x.ai", "plan": "SuperGrok", "sub": "x1"}),
        "email": "g@x.ai", "account_id": "x1", "plan": "SuperGrok",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="x1")

    def fake_http(url, headers=None, timeout=20.0):
        if "anthropic" in url and url.endswith("/usage"):
            return 200, {"five_hour": {"utilization": 0.5}}, {}
        if "anthropic" in url and url.endswith("/profile"):
            return 200, {"display_name": "Ada", "subscriptionType": "max"}, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)
    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_JWT)

    out = usage.subscription_usage(force=True)
    assert out["ok"] is True
    providers = {r["provider"] for r in out["rows"]}
    assert "claude" in providers
    assert "xai" in providers
    claude = next(r for r in out["rows"] if r["provider"] == "claude")
    assert claude["plan"] == "max"
    assert claude["usage"]
    assert set(claude["usage"][0]) >= {"window", "label", "used_fraction", "resets_at", "status"}
    xai = next(r for r in out["rows"] if r["provider"] == "xai")
    assert xai["usage"] == []
    assert xai["dashboard_url"] == "https://grok.com/?_s=usage"
    assert xai["plan"] == "SuperGrok"

    # Filter by provider.
    only = usage.subscription_usage("xai", force=True)
    assert only["ok"] is True
    assert all(r["provider"] == "xai" for r in only["rows"])

    bad = usage.subscription_usage("nope")
    assert bad["ok"] is False and bad["rows"] == []


def test_subscription_usage_fetches_accounts_in_parallel(tmp_config, monkeypatch):
    """Multi-account usage must not serialize N network waterfalls."""
    import threading
    import time as _time

    for i in range(6):
        sub_auth.save_credential("xai-sub", {
            "access_token": f"t{i}",
            "refresh_token": f"r{i}",
            "expires_at_ms": (_time.time() + 3600) * 1000,
            "account_id": f"a{i}",
            "email": f"u{i}@x.ai",
            "display_name": f"User {i}",
            "plan": "Grok Pro",
        }, account_id=f"a{i}", set_active=(i == 0))

    barrier = threading.Barrier(6, timeout=5.0)
    peak = {"n": 0}
    lock = threading.Lock()
    inflight = {"n": 0}

    def slow_fetch(cred):
        with lock:
            inflight["n"] += 1
            peak["n"] = max(peak["n"], inflight["n"])
        try:
            barrier.wait()
            return {
                "display_name": cred.get("display_name"),
                "plan": cred.get("plan"),
                "email": cred.get("email"),
                "usage": [{"window": "credits", "label": "Credits",
                           "used_fraction": 0.1, "resets_at": None, "status": "ok"}],
                "source": "api",
            }
        finally:
            with lock:
                inflight["n"] -= 1

    monkeypatch.setitem(usage._FETCHERS, "xai", slow_fetch)
    usage.clear_cache()
    t0 = _time.time()
    out = usage.subscription_usage("xai", force=True)
    elapsed = _time.time() - t0
    assert out["ok"] is True
    assert len(out["rows"]) == 6
    assert peak["n"] >= 4, peak  # truly concurrent, not serial
    assert elapsed < 2.0, elapsed


def test_xai_expired_token_skips_usage_waterfall(tmp_config, monkeypatch):
    """Expired access tokens must not issue billing/userinfo GETs."""
    now = time.time()
    sub_auth.save_credential("xai-sub", {
        "access_token": "dead.jwt.sig",
        "refresh_token": "rt",
        "expires_at_ms": (now - 100) * 1000,
        "account_id": "acct-x",
        "email": "e@x.ai",
        "display_name": "E",
        "plan": "Grok Pro",
        "last_usage": [{"window": "credits", "label": "Credits",
                        "used_fraction": 0.4, "resets_at": None, "status": "ok"}],
        "refresh_revoked": True,
    }, account_id="acct-x")

    calls = {"n": 0}

    def no_http(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("expired/revoked xAI must not hit network")

    monkeypatch.setattr(usage, "_http_get", no_http)
    monkeypatch.setattr(usage, "_http_request", no_http)
    usage.clear_cache()
    row = usage.fetch_account_usage("xai", "acct-x", force=True)
    assert calls["n"] == 0
    assert row["usage"] and row["usage"][0]["used_fraction"] == 0.4
    assert row.get("source") in ("cached", "jwt_inference", "api")



def test_subscription_usage_rpc(tmp_config, monkeypatch):
    from services.agent import worker_rpc as rpc

    sub_auth.save_credential("xai-sub", {
        "access_token": _jwt({"email": "g@x.ai", "plan": "SuperGrok", "sub": "x1"}),
        "email": "g@x.ai", "account_id": "x1", "plan": "SuperGrok",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="x1")
    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_JWT)

    emit = lambda *_a, **_k: None  # noqa: E731
    assert "subscription_usage" in rpc.SUBSCRIPTION_METHODS
    out = rpc.SUBSCRIPTION_METHODS["subscription_usage"]({"provider": "xai"}, emit)
    assert out["ok"] is True
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["provider"] == "xai"
    assert row["plan"] == "SuperGrok"
    assert row["usage"] == []
    assert row.get("dashboard_url") == "https://grok.com/?_s=usage"


def test_status_and_accounts_expose_plan_display_name(tmp_config):
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "t",
        "email": "a@c.com",
        "account_id": "a1",
        "display_name": "Ada",
        "plan": "max",
        "expires_at_ms": 1,
    }, account_id="a1")
    st = sl.status()["claude"]
    assert st["connected"] is True
    assert st["display_name"] == "Ada"
    assert st["plan"] == "max"
    accts = sl.accounts("claude")
    assert accts[0]["display_name"] == "Ada"
    assert accts[0]["plan"] == "max"
    listed = sub_auth.list_accounts("anthropic-sub")
    assert listed[0]["display_name"] == "Ada"
    assert listed[0]["plan"] == "max"


def test_enrich_helpers_login_time(monkeypatch):
    def fake_http(url, headers=None, timeout=20.0):
        if url.endswith("/profile"):
            return 200, {
                "email": "p@c.com",
                "display_name": "Pat",
                "subscriptionType": "pro",
                "uuid": "u-1",
            }, {}
        if url.endswith("/full_stripe_profile"):
            return 200, {"membershipType": "pro", "email": "c@cursor.com", "name": "Cy"}, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)

    a = usage.enrich_anthropic_profile({"access_token": "t"})
    assert a["email"] == "p@c.com"
    assert a["display_name"] == "Pat"
    assert a["plan"] == "pro"
    assert a["account_id"] == "u-1"

    c = usage.enrich_cursor_profile({"access_token": "t"})
    assert c["plan"] == "pro"
    assert c["email"] == "c@cursor.com"
    assert c["display_name"] == "Cy"

    codex = usage.enrich_codex_identity({
        "access_token": _jwt({
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc"},
            "https://api.openai.com/profile": {"email": "o@x.com"},
        }),
    })
    assert codex["email"] == "o@x.com"
    assert codex["account_id"] == "acc"
    assert codex["display_name"] == "o@x.com"

    # enrich_xai_identity may hit userinfo; stub offline.
    def fake_xai_http(url, headers=None, timeout=20.0):
        return 404, None, {}
    monkeypatch.setattr(usage, "_http_get", fake_xai_http)
    x = usage.enrich_xai_identity({
        "access_token": _jwt({"email": "g@x.ai", "plan": "SuperGrok"}),
        "email": "g@x.ai",
    })
    assert x["plan"] == "SuperGrok"
    assert x["display_name"] == "g@x.ai"


def test_xai_display_name_never_bare_uuid(tmp_config, monkeypatch):
    """UUID-only xAI accounts get a friendly Grok label, not the raw sub."""
    uuid = "06dc3359-1234-5678-9abc-def012345678"
    token = _jwt({"sub": uuid, "plan": "SuperGrok"})
    sub_auth.save_credential("xai-sub", {
        "access_token": token,
        "refresh_token": "rt",
        "account_id": uuid,
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id=uuid)

    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_JWT)
    # enrich_xai_identity now probes userinfo — keep offline for this unit test.
    monkeypatch.setattr(usage, "_http_get", lambda *a, **k: (404, None, {}))

    row = usage.fetch_account_usage("xai", uuid, force=True)
    assert row["display_name"]
    assert not usage._looks_like_uuid(row["display_name"])
    assert "Grok" in row["display_name"] or "SuperGrok" in (row["display_name"] or "")
    assert row["plan"] == "SuperGrok"
    # Persisted onto the credential for list_accounts / restart.
    cred = sub_auth.load_credential("xai-sub", account_id=uuid)
    assert cred["display_name"]
    assert not usage._looks_like_uuid(cred["display_name"])

    # enrich helper alone never returns bare UUID either.
    bare = usage.enrich_xai_identity({"access_token": token, "account_id": uuid})
    assert bare["display_name"]
    assert not usage._looks_like_uuid(bare["display_name"])


def test_cursor_identity_without_usage_endpoint(tmp_config, monkeypatch):
    """When /auth/usage fails, still surface email/plan — never bare acct-…."""
    sub_auth.save_credential("cursor", {
        "access_token": "ctok",
        "refresh_token": "rt",
        "account_id": "acct-1784192914",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="acct-1784192914")

    def fake_http(url, headers=None, timeout=20.0):
        if url.endswith("/auth/usage"):
            return 401, {"error": "unauthorized"}, {}
        if url.endswith("/full_stripe_profile"):
            return 200, {
                "membershipType": "pro",
                "email": "dev@cursor.com",
                "name": "Dev",
            }, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)
    row = usage.fetch_account_usage("cursor", "acct-1784192914", force=True)
    assert row["display_name"] == "Dev"
    assert row["email"] == "dev@cursor.com"
    assert row["plan"] == "pro"
    assert not usage._looks_like_acct_slug(row["display_name"])
    # Usage empty but source marked unavailable (identity still ok).
    assert row["usage"] == []
    assert row["source"] in ("unavailable", "api")

    # enrich alone with no network still yields a non-acct label.
    offline = usage.enrich_cursor_profile({
        "account_id": "acct-1784192914",
        "plan": "pro",
    })
    assert offline["display_name"]
    assert not usage._looks_like_acct_slug(offline["display_name"])
    assert "Cursor" in offline["display_name"] or offline["display_name"] == "pro"


def test_claude_usage_restored_after_cold_reload(tmp_config, monkeypatch):
    """last_usage on the credential restores bars when in-process cache is empty."""
    windows = [
        {"window": "5h", "label": "5 hour", "used_fraction": 0.33,
         "resets_at": "2026-07-20T00:00:00Z", "status": "ok"},
        {"window": "7d", "label": "7 day", "used_fraction": 0.1,
         "resets_at": None, "status": "ok"},
    ]
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "t",
        "email": "a@c.com",
        "account_id": "acct-a",
        "display_name": "Ada",
        "plan": "max",
        "last_usage": windows,
        "last_usage_fetched_at": time.time(),
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="acct-a")

    # Simulate cold start: empty in-process cache + live fetch failure.
    usage.clear_cache()

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(usage, "_http_get", boom)
    row = usage.fetch_account_usage("claude", "acct-a", force=True)
    assert row["display_name"] == "Ada"
    assert row["usage"], "expected last_usage restored after cold reload"
    assert row["usage"][0]["window"] == "5h"
    assert abs(row["usage"][0]["used_fraction"] - 0.33) < 1e-9
    assert row["source"] == "cached"


def test_claude_usage_persisted_on_successful_fetch(tmp_config, monkeypatch):
    """Successful live fetch writes last_usage onto the credential."""
    sub_auth.save_credential("anthropic-sub", {
        "access_token": "t",
        "email": "a@c.com",
        "account_id": "acct-a",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="acct-a")

    def fake_http(url, headers=None, timeout=20.0):
        if url.endswith("/usage"):
            return 200, {
                "five_hour": {"utilization": 0.5, "resets_at": "2026-07-20T00:00:00Z"},
                "seven_day": {"utilization": 0.2, "resets_at": None},
            }, {}
        if url.endswith("/profile"):
            return 200, {
                "email": "a@c.com",
                "display_name": "Ada",
                "subscriptionType": "max",
                "uuid": "acct-a",
            }, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)
    usage.clear_cache()
    row = usage.fetch_account_usage("claude", "acct-a", force=True)
    assert row["usage"]
    cred = sub_auth.load_credential("anthropic-sub", account_id="acct-a")
    assert isinstance(cred.get("last_usage"), list) and cred["last_usage"]
    assert cred["last_usage"][0]["window"] in ("5h", "7d")
    assert cred.get("last_usage_fetched_at")



def test_status_fraction_mapping():
    assert usage._status_from_fraction(0.0) == "ok"
    assert usage._status_from_fraction(0.89) == "ok"
    assert usage._status_from_fraction(0.9) == "warn"
    assert usage._status_from_fraction(1.0) == "blocked"
    assert usage._status_from_fraction(None) == "unknown"
    assert usage._status_from_fraction(0.1, limit_reached=True) == "blocked"


def test_xai_numeric_jwt_tier_maps_to_plan(tmp_config, monkeypatch):
    """xAI OAuth JWTs carry tier:1 — map to SuperGrok, not leave plan empty."""
    token = _jwt({
        "sub": "06dc3359-2d20-4cd7-bd84-8c8db560cd4e",
        "tier": 1,
        "scope": "openid profile email offline_access grok-cli:access api:access",
    })
    sub_auth.save_credential("xai-sub", {
        "access_token": token,
        "refresh_token": "rt",
        "account_id": "06dc3359-2d20-4cd7-bd84-8c8db560cd4e",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="06dc3359-2d20-4cd7-bd84-8c8db560cd4e")

    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_JWT)
    monkeypatch.setattr(usage, "_http_get", lambda *a, **k: (404, None, {}))
    usage.clear_cache()
    row = usage.fetch_account_usage(
        "xai", "06dc3359-2d20-4cd7-bd84-8c8db560cd4e", force=True
    )
    assert row["plan"] == "SuperGrok"
    assert row["display_name"]
    assert not usage._looks_like_uuid(row["display_name"])

    # Persist path also stores the mapped plan for cold restore.
    cred = sub_auth.load_credential(
        "xai-sub", account_id="06dc3359-2d20-4cd7-bd84-8c8db560cd4e"
    )
    assert cred.get("plan") == "SuperGrok"


def test_xai_last_usage_cold_restore(tmp_config, monkeypatch):
    """Persisted last_usage restores bars when in-process cache is empty (xAI)."""
    windows = [
        {"window": "2h", "label": "Grok web", "used_fraction": 0.0,
         "resets_at": None, "status": "ok"},
    ]
    sub_auth.save_credential("xai-sub", {
        "access_token": _jwt({"sub": "x1", "tier": 1, "email": "g@x.ai"}),
        "account_id": "x1",
        "email": "g@x.ai",
        "plan": "SuperGrok",
        "display_name": "g@x.ai",
        "last_usage": windows,
        "last_usage_fetched_at": time.time(),
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="x1")

    # Force jwt path; it should still surface last_usage from the credential.
    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_JWT)
    usage.clear_cache()
    row = usage.fetch_account_usage("xai", "x1", force=True)
    assert row["usage"], "expected last_usage restored for xAI cold path"
    assert row["usage"][0]["window"] == "2h"
    assert row["plan"] == "SuperGrok"


def test_xai_api_userinfo_and_ratelimit_headers(tmp_config, monkeypatch):
    """When billing is empty, API path falls back to rate-limit headers."""
    token = _jwt({"sub": "u-api-1", "tier": 1})
    sub_auth.save_credential("xai-sub", {
        "access_token": token,
        "refresh_token": "rt",
        "account_id": "u-api-1",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="u-api-1")

    def fake_http(url, headers=None, timeout=20.0):
        if "userinfo" in url:
            return 200, {
                "sub": "u-api-1",
                "name": "Ada Lovelace",
                "email": "ada@example.com",
                "email_verified": True,
            }, {}
        if "billing" in url:
            return 404, None, {}
        if "include=subscription" in url or url.rstrip("/").endswith("/user"):
            return 404, None, {}
        if url.rstrip("/").endswith("/me"):
            return 200, {
                "user_id": "u-api-1",
                "team_id": "team-1",
                "team_blocked": False,
            }, {}
        if url.rstrip("/").endswith("/models"):
            return 200, {"data": []}, {
                "x-ratelimit-limit-requests": "1000",
                "x-ratelimit-remaining-requests": "250",
                "x-ratelimit-limit-tokens": "100000",
                "x-ratelimit-remaining-tokens": "80000",
            }
        return 404, None, {}

    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_API)
    monkeypatch.setattr(usage, "_http_get", fake_http)
    monkeypatch.delenv("CHUDWARE_XAI_USAGE_PROBE", raising=False)
    usage.clear_cache()

    row = usage.fetch_account_usage("xai", "u-api-1", force=True)
    assert row["source"] == "api"
    assert row["email"] == "ada@example.com"
    assert row["display_name"] == "Ada Lovelace"
    assert row["plan"] == "SuperGrok"
    assert len(row["usage"]) >= 1
    req = next(u for u in row["usage"] if u["window"] == "requests")
    assert abs(req["used_fraction"] - 0.75) < 1e-9
    assert req["status"] in ("ok", "warn", "blocked")

    # Identity persisted for Settings restart.
    cred = sub_auth.load_credential("xai-sub", account_id="u-api-1")
    assert cred.get("email") == "ada@example.com"
    assert cred.get("display_name") == "Ada Lovelace"
    assert cred.get("last_usage")


def test_xai_billing_credits_primary(tmp_config, monkeypatch):
    """Official cli-chat-proxy billing fills credits window + plan from tier."""
    token = _jwt({"sub": "u-bill-1", "tier": 1})
    sub_auth.save_credential("xai-sub", {
        "access_token": token,
        "refresh_token": "rt",
        "account_id": "u-bill-1",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="u-bill-1")

    seen_headers: list[dict] = []

    def fake_http(url, headers=None, timeout=20.0):
        seen_headers.append(dict(headers or {}))
        if "userinfo" in url:
            return 200, {
                "sub": "u-bill-1",
                "name": "Bill User",
                "email": "bill@example.com",
            }, {}
        if "billing" in url and "format=credits" in url:
            return 200, {
                "config": {
                    "creditUsagePercent": 42.5,
                    "currentPeriod": {
                        "type": "USAGE_PERIOD_TYPE_WEEKLY",
                        "start": "2026-07-01T00:00:00Z",
                        "end": "2026-07-08T00:00:00Z",
                    },
                    "prepaidBalance": {"val": 150},
                    "isUnifiedBillingUser": True,
                },
                "subscriptionTier": "SuperGrok",
            }, {}
        if "include=subscription" in url:
            return 200, {
                "userId": "u-bill-1",
                "email": "bill@example.com",
                "firstName": "Bill",
                "lastName": "User",
                "subscriptionTier": "SuperGrokPro",
            }, {}
        if url.rstrip("/").endswith("/me"):
            return 200, {"user_id": "u-bill-1", "team_blocked": False}, {}
        # Models should not be required once billing succeeds.
        if url.rstrip("/").endswith("/models"):
            return 200, {"data": []}, {
                "x-ratelimit-limit-requests": "1000",
                "x-ratelimit-remaining-requests": "1",
            }
        return 404, None, {}

    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_API)
    monkeypatch.setattr(usage, "_http_get", fake_http)
    monkeypatch.delenv("CHUDWARE_XAI_USAGE_PROBE", raising=False)
    usage.clear_cache()

    row = usage.fetch_account_usage("xai", "u-bill-1", force=True)
    assert row["source"] == "api"
    assert row["email"] == "bill@example.com"
    assert row["plan"] == "SuperGrok"
    credits = next(u for u in row["usage"] if u["window"] == "credits")
    assert abs(credits["used_fraction"] - 0.425) < 1e-9
    assert credits["status"] in ("ok", "warn", "blocked")
    assert credits.get("resets_at")
    # Rate-limit secondary must NOT replace primary credits when billing works.
    assert not any(u.get("window") == "requests" for u in row["usage"])
    # Proxy auth header present on billing/user calls.
    proxy_calls = [
        h for h in seen_headers
        if h.get("X-XAI-Token-Auth") == "xai-grok-cli"
        or h.get("x-xai-token-auth") == "xai-grok-cli"
    ]
    # Our headers use exact case from _xai_auth_headers.
    assert any(h.get("X-XAI-Token-Auth") == usage.XAI_TOKEN_AUTH_VALUE for h in seen_headers)

    cred = sub_auth.load_credential("xai-sub", account_id="u-bill-1")
    assert cred.get("last_usage")
    assert any(u.get("window") == "credits" for u in (cred.get("last_usage") or []))


def test_parse_xai_billing_config_snake_and_legacy():
    """Parser accepts camelCase credits shape and legacy limit/used."""
    modern = usage._parse_xai_billing_config({
        "config": {
            "credit_usage_percent": 10.0,
            "current_period": {
                "period_type": "USAGE_PERIOD_TYPE_MONTHLY",
                "end": "2026-08-01T00:00:00Z",
            },
            "prepaid_balance": {"val": 0},
        }
    })
    assert abs(modern["credit_usage_percent"] - 10.0) < 1e-9
    assert modern["usage"][0]["window"] == "credits"
    assert "Monthly" in modern["usage"][0]["label"]

    legacy = usage._parse_xai_billing_config({
        "config": {
            "monthly_limit": {"val": 1000},
            "used": {"val": 250},
            "billing_period_end": "2026-08-01T00:00:00Z",
        }
    })
    assert abs(legacy["credit_usage_percent"] - 25.0) < 1e-9
    assert abs(legacy["usage"][0]["used_fraction"] - 0.25) < 1e-9


def test_xai_billing_cache_ttl(tmp_config, monkeypatch):
    """Billing GETs are cached ~60s so force path does not hammer the proxy."""
    token = _jwt({"sub": "u-cache", "tier": 1})
    sub_auth.save_credential("xai-sub", {
        "access_token": token,
        "account_id": "u-cache",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="u-cache")

    n = {"billing": 0}

    def fake_http(url, headers=None, timeout=20.0):
        if "userinfo" in url:
            return 200, {"sub": "u-cache", "email": "c@x.ai", "name": "C"}, {}
        if "billing" in url:
            n["billing"] += 1
            return 200, {"config": {"creditUsagePercent": 5.0}}, {}
        if "include=subscription" in url:
            return 200, {"subscriptionTier": "SuperGrok", "userId": "u-cache"}, {}
        if url.rstrip("/").endswith("/me"):
            return 200, {"user_id": "u-cache", "team_blocked": False}, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_API)
    monkeypatch.setattr(usage, "_http_get", fake_http)
    usage.clear_cache()

    # Bypass outer 5-min cache with force; inner billing cache still applies.
    r1 = usage.fetch_account_usage("xai", "u-cache", force=True)
    r2 = usage.fetch_account_usage("xai", "u-cache", force=True)
    assert r1["usage"][0]["window"] == "credits"
    assert r2["usage"][0]["window"] == "credits"
    assert n["billing"] == 1


def test_parse_xai_billing_absent_percent_is_zero():
    """Live SuperGrok configs omit creditUsagePercent when unused → 0% bar."""
    parsed = usage._parse_xai_billing_config({
        "config": {
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "end": "2026-07-20T00:58:27.738321+00:00",
            },
            "prepaidBalance": {"val": 0},
            "isUnifiedBillingUser": True,
        }
    })
    assert parsed["credit_usage_percent"] == 0.0
    assert parsed["usage"][0]["window"] == "credits"
    assert abs(parsed["usage"][0]["used_fraction"] - 0.0) < 1e-9


def test_xai_record_ratelimit_headers_from_chat(tmp_config):
    """Chat response headers seed last_usage without a usage endpoint."""
    sub_auth.save_credential("xai-sub", {
        "access_token": "t",
        "account_id": "u2",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="u2")
    rows = usage.record_xai_ratelimit_headers(
        {"access_token": "t", "account_id": "u2"},
        {
            "x-ratelimit-limit-requests": "8300",
            "x-ratelimit-remaining-requests": "8300",
            "x-ratelimit-limit-tokens": "53000000",
            "x-ratelimit-remaining-tokens": "53000000",
        },
        account_id="u2",
    )
    assert len(rows) == 2
    assert rows[0]["window"] == "requests"
    assert abs(rows[0]["used_fraction"] - 0.0) < 1e-9
    cred = sub_auth.load_credential("xai-sub", account_id="u2")
    assert cred.get("last_usage")


def test_xai_enrich_userinfo_live_shape(monkeypatch):
    """enrich_xai_identity uses OIDC userinfo when JWT lacks email/name."""
    def fake_http(url, headers=None, timeout=20.0):
        if "userinfo" in url:
            return 200, {
                "sub": "uuid-1",
                "name": "Hunter Edmonds",
                "email": "h@example.com",
            }, {}
        return 404, None, {}

    monkeypatch.setattr(usage, "_http_get", fake_http)
    cred = usage.enrich_xai_identity({
        "access_token": _jwt({"sub": "uuid-1", "tier": 1}),
        "account_id": "uuid-1",
    })
    assert cred["email"] == "h@example.com"
    assert cred["display_name"] == "Hunter Edmonds"
    assert cred["plan"] == "SuperGrok"


def test_revoked_expired_credential_skips_live_fetch_any_provider(tmp_config, monkeypatch):
    """Generalized skip (not xAI-only): any provider's revoked+expired stored
    credential must not reach its live fetcher, even past cache TTL — matches
    the xAI-specific disk-only skip in fetch_xai_usage_api, but for everyone."""
    now = time.time()
    sub_auth.save_credential("openai-codex", {
        "access_token": "dead-at",
        "refresh_token": "dead-rt",
        "expires_at_ms": (now - 100) * 1000,
        "account_id": "acct-c",
        "email": "c@openai.com",
        "display_name": "C",
        "plan": "plus",
        "last_usage": [{"window": "5h", "label": "5h", "used_fraction": 0.2,
                        "resets_at": None, "status": "ok"}],
        "refresh_revoked": True,
    }, account_id="acct-c")

    def boom(_cred):
        raise AssertionError("revoked/expired credential must not reach the live fetcher")

    monkeypatch.setitem(usage._FETCHERS, "codex", boom)
    usage.clear_cache()
    row = usage.fetch_account_usage("codex", "acct-c", force=True)
    assert row["usage"] and row["usage"][0]["used_fraction"] == 0.2
    assert row["source"] == "cached"


def test_live_access_token_still_tries_fetcher_despite_prior_revoke_marker(tmp_config, monkeypatch):
    """A stale 'refresh_revoked' marker must not skip the fetch while the
    access token itself is still unexpired — only revoked+expired skips."""
    now = time.time()
    sub_auth.save_credential("openai-codex", {
        "access_token": "live-at",
        "refresh_token": "rt",
        "expires_at_ms": (now + 3600) * 1000,
        "account_id": "acct-d",
        "refresh_revoked": True,
    }, account_id="acct-d")

    calls = {"n": 0}

    def fetcher(_cred):
        calls["n"] += 1
        return {"usage": [], "plan": "plus"}

    monkeypatch.setitem(usage._FETCHERS, "codex", fetcher)
    usage.clear_cache()
    usage.fetch_account_usage("codex", "acct-d", force=True)
    assert calls["n"] == 1
