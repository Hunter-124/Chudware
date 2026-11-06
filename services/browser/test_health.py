"""Offline tests of the vendor canary's public health results."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.browser import embed, health


@pytest.fixture
def canary(monkeypatch):
    monkeypatch.setattr(embed, "get_manager", lambda: SimpleNamespace(wizard_active=lambda: False))
    monkeypatch.setattr(health.profiles, "has_login", lambda _site: True)

    def check(hits):
        adapter = SimpleNamespace(search=lambda _query: hits)
        monkeypatch.setattr(health, "get_adapter", lambda _site: adapter)
        return health.run_health_check(["example"])[0]

    return check


@pytest.mark.parametrize("kind", ["category", "series", "document", "model", "project", "page", None, ""])
def test_non_product_hits_are_not_healthy(canary, kind):
    hit = {"title": "A result", "url": "https://example.test/result"}
    if kind is not None:
        hit["kind"] = kind
    row = canary([hit])
    assert row["ok"] is False
    assert row["hit_count"] == 0
    assert row["error"] == "no product hits parsed from canary search"


def test_only_complete_successful_product_hits_count(canary):
    row = canary([
        {"title": "Product", "url": "https://example.test/a", "kind": "product"},
        {"title": "Another", "url": "https://example.test/b", "kind": "product"},
        {"title": "Category", "url": "https://example.test/c", "kind": "category"},
        {"title": "Missing URL", "kind": "product"},
        {"url": "https://example.test/d", "kind": "product"},
        {"ok": False, "title": "Not a hit", "url": "https://example.test/e",
         "kind": "product", "reason": "blocked"},
    ])
    assert row["ok"] is True
    assert row["hit_count"] == 2
    assert row["error"] is None
    assert row["infrastructure_unavailable"] is False


def test_login_wall_clears_stale_login_and_preserves_details(canary):
    failure = {"ok": False, "site": "example", "op": "search", "reason": "login_required",
               "hint": "Sign in", "url": "https://example.test/signin", "status": 401,
               "retry_after_s": None, "ask_user": {"prompt": "Sign in"}}
    row = canary([failure])
    assert row["ok"] is False
    assert row["error"] == row["reason"] == "login_required"
    assert row["expected"] is True
    assert row["logged_in"] is False
    assert row["infrastructure_unavailable"] is False
    for key in ("op", "hint", "url", "status", "retry_after_s", "ask_user"):
        assert row[key] == failure[key]


@pytest.mark.parametrize("reason", [
    "playwright_not_installed", "browser_helper_missing", "browser_unavailable",
    "browser_fetch_failed", "transport_failed", "timeout",
])
def test_infrastructure_failure_is_not_a_login(canary, reason):
    row = canary([{"ok": False, "reason": reason, "hint": "Browser unavailable",
                   "url": "https://example.test/search", "status": None,
                   "retry_after_s": 12.5}])
    assert row["ok"] is False
    assert row["error"] == row["reason"] == reason
    assert row["infrastructure_unavailable"] is True
    assert not row.get("expected", False)
    assert row["logged_in"] is True
    assert row["hint"] == "Browser unavailable"
    assert row["url"] == "https://example.test/search"
    assert row["status"] is None
    assert row["retry_after_s"] == 12.5


@pytest.mark.parametrize("reason", [
    "blocked", "captcha", "challenge_page", "bot_walled", "terms_required",
    "terms_confirmation_required", "rate_limited", "no_profile", "credentials_required",
])
def test_challenges_and_other_failures_are_not_login_walls(canary, reason):
    row = canary([{"ok": False, "reason": reason}])
    assert row["ok"] is False
    assert row["error"] == row["reason"] == reason
    assert row["infrastructure_unavailable"] is False
    assert not row.get("expected", False)
    assert row["logged_in"] is (reason != "no_profile")


def test_health_rpc_only_prompts_browser_login_for_login_wall(monkeypatch):
    from services.mcp.parts_tools import vendors_health_impl

    monkeypatch.setattr(embed, "get_manager", lambda: SimpleNamespace(wizard_active=lambda: False))
    monkeypatch.setattr(health.profiles, "has_login", lambda _site: False)
    reasons = {"login": "login_required", "captcha": "captcha", "blocked": "blocked",
               "transport": "transport_failed", "timeout": "timeout",
               "keys": "credentials_required"}
    monkeypatch.setattr(health, "get_adapter", lambda site: SimpleNamespace(
        search=lambda _query: [{"ok": False, "reason": reasons[site]}]))

    result = vendors_health_impl(",".join(reasons))
    rows = {row["site"]: row for row in result["sites"]}
    assert result["ok"] is True
    assert result["needs_human_login"] == ["login"]
    assert rows["login"]["needs_human_login"] is True
    for site in ("captcha", "blocked", "transport", "timeout", "keys"):
        assert rows[site]["needs_human_login"] is False
    assert rows["transport"]["infrastructure_unavailable"] is True
    assert rows["timeout"]["infrastructure_unavailable"] is True


def test_default_selection_includes_wizard_and_remaining_canaries(monkeypatch):
    monkeypatch.setattr(embed, "get_manager", lambda: SimpleNamespace(wizard_active=lambda: False))
    monkeypatch.setattr(health.profiles, "has_login", lambda _site: False)
    monkeypatch.setattr(health, "login_wizard_sites", lambda: ["wizard-b", "wizard-a"])
    monkeypatch.setattr(health, "CANARY_QUERIES", {
        "shop": "screw", "wizard-a": "bearing", "catalog": "motor",
    })
    monkeypatch.setattr(health, "get_adapter", lambda _site: SimpleNamespace(
        search=lambda query: [{"title": query, "url": "https://example.test", "kind": "product"}]))

    rows = health.run_health_check()
    assert [row["site"] for row in rows] == ["wizard-b", "wizard-a", "shop", "catalog"]
    assert all(row["ok"] is True for row in rows)


def test_default_selection_uses_registry_when_wizard_list_is_empty(monkeypatch):
    monkeypatch.setattr(embed, "get_manager", lambda: SimpleNamespace(wizard_active=lambda: True))
    monkeypatch.setattr(health, "login_wizard_sites", lambda: [])
    monkeypatch.setattr(health, "all_adapters", lambda: [SimpleNamespace(key="registered")])
    monkeypatch.setattr(health, "CANARY_QUERIES", {"registered": "part", "extra": "part"})
    rows = health.run_health_check()
    assert [row["site"] for row in rows] == ["registered", "extra"]
    assert all(row["deferred"] is True for row in rows)
