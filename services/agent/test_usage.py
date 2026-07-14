"""Subscription usage fetchers (contracts/providers.md §4.5) — unit-level, no network.

Mocks HTTP via usage._http_get; exercises cache TTL, 429 freeze, identity persistence,
and the subscription_usage RPC shape.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from agent.providers import sub_auth
from agent.providers import subscription_login as sl
from agent.providers import usage


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
    # Subsequent fetch within TTL must NOT re-hit the network.
    third = usage.fetch_account_usage("claude", "a1", force=False)
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


def test_xai_browser_scrape_stub_seam(tmp_config, monkeypatch):
    sub_auth.save_credential("xai-sub", {
        "access_token": "t",
        "email": "g@x.ai",
        "account_id": "u1",
        "plan": "Premium+",
        "expires_at_ms": int(time.time() * 1000) + 10**9,
    }, account_id="u1")
    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_BROWSER)
    row = usage.fetch_account_usage("xai", "u1", force=True)
    assert row["source"] == "browser_scrape"
    assert row["dashboard_url"] == "https://grok.com/?_s=usage"
    assert row["plan"] == "Premium+"
    assert len(row["usage"]) == 1
    assert row["usage"][0]["status"] == "unknown"
    assert row["usage"][0]["used_fraction"] is None


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


def test_subscription_usage_rpc(tmp_config, monkeypatch):
    from engine_oracle.parts_hub import subscription_rpc as rpc

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

    x = usage.enrich_xai_identity({
        "access_token": _jwt({"email": "g@x.ai", "plan": "SuperGrok"}),
        "email": "g@x.ai",
    })
    assert x["plan"] == "SuperGrok"
    assert x["display_name"] == "g@x.ai"


def test_status_fraction_mapping():
    assert usage._status_from_fraction(0.0) == "ok"
    assert usage._status_from_fraction(0.89) == "ok"
    assert usage._status_from_fraction(0.9) == "warn"
    assert usage._status_from_fraction(1.0) == "blocked"
    assert usage._status_from_fraction(None) == "unknown"
    assert usage._status_from_fraction(0.1, limit_reached=True) == "blocked"
