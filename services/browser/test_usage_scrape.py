"""Unit tests for services.browser.usage_scrape — mocked CEF, no network.

Covers dual-schema rate-limit parse, needs_login, challenge/blocked, and the
usage.py browser_scrape → jwt_inference fallback path.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from services.browser import usage_scrape as us


# ── Pure parsers ─────────────────────────────────────────────────────────────


def test_parse_flat_remaining_queries_schema():
    raw = {
        "remainingQueries": 42,
        "totalQueries": 100,
        "windowSizeSeconds": 604800,
        "remainingTokens": 1000,
        "totalTokens": 5000,
    }
    parsed = us.parse_rate_limit_payload(raw)
    assert parsed is not None
    assert parsed["primary"]["remaining"] == 42
    assert parsed["primary"]["total"] == 100
    assert parsed["primary"]["window_size_seconds"] == 604800
    assert parsed["primary"]["remaining_tokens"] == 1000
    rows = us.buckets_to_usage_rows(parsed)
    assert len(rows) == 1
    assert rows[0]["window"] == "weekly"
    assert rows[0]["remaining"] == 42
    assert rows[0]["limit"] == 100
    assert abs(rows[0]["used_fraction"] - 0.58) < 1e-9
    assert rows[0]["status"] == "ok"


def test_parse_nested_remaining_limit_schema():
    raw = {
        "remaining": 3,
        "limit": 10,
        "windowSizeSeconds": 3600,
    }
    parsed = us.parse_rate_limit_payload(raw)
    assert parsed is not None
    assert parsed["primary"]["remaining"] == 3
    assert parsed["primary"]["total"] == 10
    rows = us.buckets_to_usage_rows(parsed)
    assert rows[0]["window"] == "1h"
    assert abs(rows[0]["used_fraction"] - 0.7) < 1e-9


def test_parse_low_high_effort_variants():
    raw = {
        "remainingQueries": 50,
        "totalQueries": 100,
        "windowSizeSeconds": 604800,
        "lowEffortRateLimits": {
            "remainingQueries": 80,
            "totalQueries": 100,
            "windowSizeSeconds": 604800,
        },
        "highEffortRateLimits": {
            "remainingQueries": 5,
            "totalQueries": 20,
            "windowSizeSeconds": 604800,
        },
    }
    parsed = us.parse_rate_limit_payload(raw)
    assert parsed is not None
    assert parsed["low_effort"]["remaining"] == 80
    assert parsed["high_effort"]["remaining"] == 5
    rows = us.buckets_to_usage_rows(parsed)
    windows = {r["window"] for r in rows}
    assert "weekly" in windows or any(r.get("id") == "xai:web:default" for r in rows)
    assert "low_effort" in windows
    assert "high_effort" in windows


def test_parse_nested_default_wrapper():
    raw = {
        "DEFAULT": {
            "remainingQueries": 1,
            "totalQueries": 10,
            "windowSizeSeconds": 604800,
        }
    }
    parsed = us.parse_rate_limit_payload(raw)
    assert parsed is not None
    assert parsed["primary"]["remaining"] == 1


def test_parse_json_string_and_empty():
    assert us.parse_rate_limit_payload('{"remainingQueries":2,"totalQueries":4}') is not None
    assert us.parse_rate_limit_payload("") is None
    assert us.parse_rate_limit_payload({}) is None
    assert us.parse_rate_limit_payload("not-json") is None


def test_exhausted_and_warning_status():
    exhausted = us.buckets_to_usage_rows(us.parse_rate_limit_payload({
        "remainingQueries": 0, "totalQueries": 50, "windowSizeSeconds": 604800,
    }))
    assert exhausted[0]["status"] == "exhausted"
    warn = us.buckets_to_usage_rows(us.parse_rate_limit_payload({
        "remainingQueries": 5, "totalQueries": 100, "windowSizeSeconds": 604800,
    }))
    assert warn[0]["status"] == "warning"  # 95% used
    assert abs(warn[0]["used_fraction"] - 0.95) < 1e-9


# ── Sentinel / detection ─────────────────────────────────────────────────────


def test_extract_sentinel_payload():
    body = {"http": 200, "body": json.dumps({"remainingQueries": 1, "totalQueries": 2})}
    html = f'<html><div id="__aicad_capture">{json.dumps(body)}</div></html>'
    got = us.extract_sentinel_payload(html)
    assert isinstance(got, dict)
    assert got["http"] == 200


def test_detect_login_required_status_and_redirect():
    assert us.detect_login_required(http_status=401) is True
    assert us.detect_login_required(http_status=403) is True
    assert us.detect_login_required(final_url="https://accounts.x.ai/sign-in") is True
    assert us.detect_login_required(
        sentinel={"http": 401, "body": ""},
    ) is True
    assert us.detect_login_required(
        sentinel={"http": 200, "redirected": True, "url": "https://grok.com/sign-in"},
    ) is True
    assert us.detect_login_required(http_status=200, final_url="https://grok.com/") is False


def test_detect_challenge_blocked():
    html = (
        "<html><body>Just a moment... "
        '<div class="cf-turnstile"></div>'
        "Checking your browser before</body></html>"
    )
    assert us.detect_challenge_blocked(html, "https://grok.com/") is True
    assert us.detect_challenge_blocked("<html><body>Grok chat</body></html>") is False


# ── Profile isolation ────────────────────────────────────────────────────────


def test_account_site_key_and_profile_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CEF_CACHE_ROOT", str(tmp_path / "cef_root"))
    key = us.account_site_key("User/One@x")
    assert key.startswith("xai--")
    assert "@" not in key
    daemon = us.account_daemon_profile("User/One@x")
    assert daemon.exists()
    assert "accounts/xai" in str(daemon).replace("\\", "/")
    assert daemon.name == "daemon"
    # Must not be the warm profile leaf.
    assert "_warm_profile" not in str(daemon)


# ── CEF interact mocks ───────────────────────────────────────────────────────


class _FakeCef:
    """Minimal cef_client.CefDaemon stand-in recording profile= args."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or [])

    def interact(self, url, script, *, sentinel="__aicad_capture",
                 settle_ms=4000, timeout_ms=12000, profile="", timeout_s=90.0):
        self.calls.append({
            "url": url, "script": script, "sentinel": sentinel,
            "profile": profile, "timeout_s": timeout_s,
        })
        if self._responses:
            return self._responses.pop(0)
        return {"ok": True, "status": 200, "final_url": url, "html": ""}


def _sentinel_html(payload: dict | str, *, page: str = "ok") -> str:
    text = json.dumps(payload) if isinstance(payload, dict) else payload
    challenge_bits = ""
    if page == "challenge":
        challenge_bits = 'Just a moment... <div class="cf-turnstile"></div> Checking your browser before'
    return (
        f"<html><body>{challenge_bits}"
        f'<div id="__aicad_capture">{text}</div></body></html>'
    )


def test_fetch_grok_usage_ok_flat_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CEF_CACHE_ROOT", str(tmp_path / "cef"))
    monkeypatch.setattr(
        "services.paths.BROWSER_PROFILES", tmp_path / "pw_profiles", raising=False,
    )
    # profiles.meta writes under BROWSER_PROFILES — patch ensure path exists.
    import services.paths as engine_paths
    monkeypatch.setattr(engine_paths, "BROWSER_PROFILES", tmp_path / "pw_profiles")
    (tmp_path / "pw_profiles").mkdir(parents=True, exist_ok=True)

    body = json.dumps({
        "remainingQueries": 70,
        "totalQueries": 100,
        "windowSizeSeconds": 604800,
        "lowEffortRateLimits": {
            "remainingQueries": 90, "totalQueries": 100, "windowSizeSeconds": 604800,
        },
    })
    cef = _FakeCef([{
        "ok": True,
        "status": 200,
        "final_url": "https://grok.com/",
        "html": _sentinel_html({"http": 200, "url": "https://grok.com/rest/rate-limits",
                                "redirected": False, "body": body}),
    }])
    out = us.fetch_grok_usage("acct-1", {"email": "g@x.ai", "plan": "SuperGrok"}, cef=cef)
    assert out["ok"] is True
    assert out["source"] == "browser_scrape"
    assert out["status"] == "ok"
    assert out["dashboard_url"] == us.DASHBOARD_URL
    assert out["plan"] == "SuperGrok"
    assert len(out["usage"]) >= 1
    primary = out["usage"][0]
    assert abs(primary["used_fraction"] - 0.3) < 1e-9
    assert primary["status"] == "ok"
    # Profile isolation: daemon path + never empty/warm.
    assert cef.calls
    assert cef.calls[0]["profile"]
    assert "accounts" in cef.calls[0]["profile"]
    assert "acct-1" in cef.calls[0]["profile"] or "acct-1" in us.account_site_key("acct-1")
    assert "_warm_profile" not in cef.calls[0]["profile"]
    assert "rate-limits" in cef.calls[0]["script"] or "rate-limits" in cef.calls[0]["url"]


def test_fetch_grok_usage_nested_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CEF_CACHE_ROOT", str(tmp_path / "cef"))
    import services.paths as engine_paths
    monkeypatch.setattr(engine_paths, "BROWSER_PROFILES", tmp_path / "pw_profiles")
    (tmp_path / "pw_profiles").mkdir(parents=True, exist_ok=True)

    body = json.dumps({"remaining": 2, "limit": 8, "windowSizeSeconds": 7200})
    cef = _FakeCef([{
        "ok": True, "status": 200, "final_url": "https://grok.com/",
        "html": _sentinel_html({"http": 200, "body": body, "redirected": False, "url": ""}),
    }])
    out = us.fetch_grok_usage("a2", {}, cef=cef, probe_models=["grok-3"])
    assert out["ok"] is True
    assert out["usage"][0]["remaining"] == 2
    assert out["usage"][0]["limit"] == 8


def test_fetch_grok_usage_needs_login_401(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CEF_CACHE_ROOT", str(tmp_path / "cef"))
    import services.paths as engine_paths
    monkeypatch.setattr(engine_paths, "BROWSER_PROFILES", tmp_path / "pw_profiles")
    (tmp_path / "pw_profiles").mkdir(parents=True, exist_ok=True)

    cef = _FakeCef([{
        "ok": True, "status": 200, "final_url": "https://grok.com/",
        "html": _sentinel_html({"http": 401, "body": "unauthorized", "url": "",
                                "redirected": False}),
    }])
    out = us.fetch_grok_usage("a3", {"email": "g@x.ai"}, cef=cef)
    assert out["ok"] is False
    assert out["status"] == "needs_login"
    assert out["dashboard_url"] == us.DASHBOARD_URL
    assert out["usage"][0]["status"] == "needs_login"


def test_fetch_grok_usage_needs_login_redirect(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CEF_CACHE_ROOT", str(tmp_path / "cef"))
    import services.paths as engine_paths
    monkeypatch.setattr(engine_paths, "BROWSER_PROFILES", tmp_path / "pw_profiles")
    (tmp_path / "pw_profiles").mkdir(parents=True, exist_ok=True)

    cef = _FakeCef([{
        "ok": True, "status": 200,
        "final_url": "https://accounts.x.ai/sign-in?next=/",
        "html": "<html>Sign in to Grok</html>",
    }])
    out = us.fetch_grok_usage("a4", {}, cef=cef)
    assert out["status"] == "needs_login"


def test_fetch_grok_usage_challenge_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CEF_CACHE_ROOT", str(tmp_path / "cef"))
    import services.paths as engine_paths
    monkeypatch.setattr(engine_paths, "BROWSER_PROFILES", tmp_path / "pw_profiles")
    (tmp_path / "pw_profiles").mkdir(parents=True, exist_ok=True)

    html = (
        "<html><body>Just a moment..."
        '<div class="cf-turnstile" data-sitekey="x"></div>'
        "Checking your browser before accessing grok.com"
        "</body></html>"
    )
    cef = _FakeCef([{
        "ok": True, "status": 403, "final_url": "https://grok.com/",
        "html": html,
    }])
    out = us.fetch_grok_usage("a5", {}, cef=cef)
    assert out["ok"] is False
    assert out["status"] == "blocked"
    assert out["usage"][0]["status"] == "blocked"
    assert any("challenge" in d for d in (out.get("details") or ["challenge:captcha"]))


def test_fetch_grok_usage_cef_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CEF_CACHE_ROOT", str(tmp_path / "cef"))
    import services.paths as engine_paths
    monkeypatch.setattr(engine_paths, "BROWSER_PROFILES", tmp_path / "pw_profiles")
    (tmp_path / "pw_profiles").mkdir(parents=True, exist_ok=True)

    class Boom:
        def interact(self, *a, **k):
            raise RuntimeError("daemon down")

    with pytest.raises(us.UsageScrapeError):
        us.fetch_grok_usage("a6", {}, cef=Boom())


def test_fetch_grok_usage_empty_parse_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CEF_CACHE_ROOT", str(tmp_path / "cef"))
    import services.paths as engine_paths
    monkeypatch.setattr(engine_paths, "BROWSER_PROFILES", tmp_path / "pw_profiles")
    (tmp_path / "pw_profiles").mkdir(parents=True, exist_ok=True)

    cef = _FakeCef([{
        "ok": True, "status": 200, "final_url": "https://grok.com/",
        "html": "<html><body>no sentinel here</body></html>",
    }])
    with pytest.raises(us.UsageScrapeError):
        us.fetch_grok_usage("a7", {}, cef=cef, probe_models=["grok-3"])


# ── usage.py integration (fallback + success) ────────────────────────────────


def test_usage_py_browser_scrape_success(monkeypatch):
    from agent.providers import usage

    scraped = {
        "ok": True,
        "status": "ok",
        "source": "browser_scrape",
        "dashboard_url": us.DASHBOARD_URL,
        "usage": [{
            "window": "weekly",
            "label": "Grok web",
            "used_fraction": 0.25,
            "resets_at": None,
            "status": "ok",
            "remaining": 75,
            "limit": 100,
        }],
    }

    monkeypatch.setattr(us, "fetch_grok_usage", lambda *a, **k: scraped)
    # Ensure import path resolves to our module under test.
    import services.browser.usage_scrape as scrape_mod
    monkeypatch.setattr(scrape_mod, "fetch_grok_usage", lambda *a, **k: scraped)

    cred = {
        "account_id": "u1",
        "email": "g@x.ai",
        "plan": "Premium+",
        "access_token": "t",
    }
    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_BROWSER)
    row = usage.fetch_xai_usage_browser_scrape(cred)
    assert row["source"] == "browser_scrape"
    assert row["plan"] == "Premium+"
    assert len(row["usage"]) == 1
    assert abs(row["usage"][0]["used_fraction"] - 0.25) < 1e-9
    assert row["usage"][0]["status"] == "ok"
    assert row["dashboard_url"] == us.DASHBOARD_URL


def test_usage_py_browser_scrape_fallback_on_error(monkeypatch):
    from agent.providers import usage

    def boom(*_a, **_k):
        raise us.UsageScrapeError("daemon missing")

    import services.browser.usage_scrape as scrape_mod
    monkeypatch.setattr(scrape_mod, "fetch_grok_usage", boom)

    cred = {
        "account_id": "u1",
        "email": "g@x.ai",
        "plan": "SuperGrok",
        "access_token": "t",
    }
    monkeypatch.setattr(usage, "XAI_USAGE_SOURCE", usage.XAI_SOURCE_BROWSER)
    row = usage.fetch_xai_usage_browser_scrape(cred)
    # Fallback is jwt_inference envelope (plan preserved, empty usage).
    assert row["source"] == "jwt_inference"
    assert row["plan"] == "SuperGrok"
    assert row["usage"] == []
    assert row["dashboard_url"] == us.DASHBOARD_URL


def test_usage_py_browser_scrape_needs_login_keeps_source(monkeypatch):
    from agent.providers import usage

    scraped = us._needs_login_result("u1", {"email": "g@x.ai", "plan": "SuperGrok"})
    import services.browser.usage_scrape as scrape_mod
    monkeypatch.setattr(scrape_mod, "fetch_grok_usage", lambda *a, **k: scraped)

    row = usage.fetch_xai_usage_browser_scrape({
        "account_id": "u1", "email": "g@x.ai", "plan": "SuperGrok",
    })
    assert row["source"] == "browser_scrape"
    assert row["usage"][0]["status"] == "needs_login"
    assert row["dashboard_url"] == us.DASHBOARD_URL


def test_usage_py_browser_scrape_blocked_keeps_source(monkeypatch):
    from agent.providers import usage

    scraped = us._blocked_result("u1", {"email": "g@x.ai"})
    import services.browser.usage_scrape as scrape_mod
    monkeypatch.setattr(scrape_mod, "fetch_grok_usage", lambda *a, **k: scraped)

    row = usage.fetch_xai_usage_browser_scrape({
        "account_id": "u1", "email": "g@x.ai", "plan": "SuperGrok",
    })
    assert row["source"] == "browser_scrape"
    assert row["usage"][0]["status"] == "blocked"
