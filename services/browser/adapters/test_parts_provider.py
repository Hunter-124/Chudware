"""Tests for services/browser/adapters/parts_provider.py.

No real Playwright/network: a fake SourceAdapter stands in for GrabCAD/TraceParts.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def provider_mods(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles
    importlib.reload(profiles)
    from services.browser.adapters import parts_provider
    importlib.reload(parts_provider)
    yield profiles, parts_provider
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


class _FakeSourceAdapter:
    key = "fakesite"

    def __init__(self):
        self.search_calls = []
        self.acquire_calls = []
        self.search_return = [{"title": "Widget", "url": "https://fakesite.example/widget", "site": "fakesite"}]
        self.acquire_return = {
            "ok": True,
            "source_url": "https://fakesite.example/widget",
            "fetched_ts": "2026-07-04T00:00:00Z",
            "session_profile_used": "fakesite",
            "license_note": "test license note",
            "artifact_path": "/tmp/widget.step",
        }

    def search(self, query):
        self.search_calls.append(query)
        return self.search_return

    def acquire(self, result):
        self.acquire_calls.append(result)
        return self.acquire_return


def test_search_without_profile_reaches_adapter(provider_mods):
    # Anonymous browsing is allowed (2026-07-07): no profile no longer gates
    # search — session.browser_fetch auto-creates one; gated sites degrade to
    # login_required stubs from the adapter itself.
    profiles, parts_provider = provider_mods
    src = _FakeSourceAdapter()
    provider = parts_provider.BrowserCatalogAdapter(src)

    provider.search("widget")

    assert src.search_calls == ["widget"]


def test_search_filters_by_domain(provider_mods):
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    provider = parts_provider.BrowserCatalogAdapter(src)

    assert provider.search("widget", domain="ecad") == []
    assert src.search_calls == []


def test_search_maps_hits_to_partrecords(provider_mods):
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    provider = parts_provider.BrowserCatalogAdapter(src)

    hits = provider.search("widget")

    assert len(hits) == 1
    rec = hits[0]
    assert rec["domain"] == "catalog"
    assert rec["title"] == "Widget"
    assert rec["geometry_ref"] is None
    assert rec["source"] == "live"
    assert rec["part_id"] == "catalog:fakesite::https://fakesite.example/widget"


def test_search_passes_through_ask_user_errors(provider_mods):
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    src.search_return = [{"ok": False, "reason": "login_required", "ask_user": {"kind": "approval"}}]
    provider = parts_provider.BrowserCatalogAdapter(src)

    hits = provider.search("widget")

    assert hits == [{"ok": False, "reason": "login_required", "ask_user": {"kind": "approval"}}]


def test_get_without_profile_reaches_adapter(provider_mods):
    # Anonymous acquire allowed (2026-07-07) — the adapter itself reports
    # login_required when the site actually gates the download.
    _profiles, parts_provider = provider_mods
    src = _FakeSourceAdapter()
    provider = parts_provider.BrowserCatalogAdapter(src)

    provider.get("catalog:fakesite::https://fakesite.example/widget")

    assert len(src.acquire_calls) == 1


def test_get_returns_none_for_other_site_part_id(provider_mods):
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    provider = parts_provider.BrowserCatalogAdapter(src)

    assert provider.get("catalog:othersite::https://x") is None


def test_get_acquires_and_builds_full_record(provider_mods):
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    provider = parts_provider.BrowserCatalogAdapter(src)
    provider.search("widget")  # populate the cache with title

    part_id = "catalog:fakesite::https://fakesite.example/widget"
    rec = provider.get(part_id)

    assert rec["ok"] if "ok" in rec else True
    assert rec["part_id"] == part_id
    assert rec["title"] == "Widget"
    assert rec["geometry_ref"] == {"kind": "import_file", "path": "/tmp/widget.step"}
    assert rec["license_note"] == "test license note"
    assert src.acquire_calls == [{"title": "Widget", "url": "https://fakesite.example/widget", "site": "fakesite"}]


def test_get_without_prior_search_still_recovers_url(provider_mods):
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    provider = parts_provider.BrowserCatalogAdapter(src)

    part_id = "catalog:fakesite::https://fakesite.example/widget"
    provider.get(part_id)

    assert src.acquire_calls == [{"title": "", "url": "https://fakesite.example/widget", "site": "fakesite"}]


def test_get_propagates_and_enriches_acquire_failure(provider_mods):
    """An acquire failure (login_required/terms/…) is propagated, and additively
    enriched with part_id/site/link-out so the downstream place() seam can surface an
    actionable next step. The adapter's own reason/ask_user must survive unchanged."""
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    src.acquire_return = {"ok": False, "reason": "login_required", "ask_user": {"kind": "approval"}}
    provider = parts_provider.BrowserCatalogAdapter(src)

    part_id = "catalog:fakesite::https://fakesite.example/widget"
    rec = provider.get(part_id)

    # original fields untouched
    assert rec["ok"] is False
    assert rec["reason"] == "login_required"
    assert rec["ask_user"] == {"kind": "approval"}
    # additive identity + link-out
    assert rec["part_id"] == part_id
    assert rec["site"] == "fakesite"
    assert rec["url"] == "https://fakesite.example/widget"
    assert rec["source_url"] == "https://fakesite.example/widget"


def test_get_folds_vendor_extras_into_params(provider_mods):
    """Acquire-envelope extras (e.g. McMaster sku + price) land in params
    alongside site/url/fetched_ts, so vendor/SKU/URL/fetched-at ride on the part record."""
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    src.acquire_return = {
        "ok": True, "source_url": "https://fakesite.example/widget",
        "fetched_ts": "2026-07-04T00:00:00Z", "session_profile_used": "fakesite",
        "license_note": "n", "artifact_path": "/tmp/widget.step",
        "sku": "91290A115", "price": "$12.34",
    }
    provider = parts_provider.BrowserCatalogAdapter(src)
    rec = provider.get("catalog:fakesite::https://fakesite.example/widget")
    assert rec["params"]["site"] == "fakesite"
    assert rec["params"]["url"] == "https://fakesite.example/widget"
    assert rec["params"]["fetched_ts"] == "2026-07-04T00:00:00Z"
    assert rec["params"]["sku"] == "91290A115"
    assert rec["params"]["price"] == "$12.34"
    assert rec["geometry_ref"] == {"kind": "import_file", "path": "/tmp/widget.step"}


def test_get_no_cad_vendor_has_null_geometry_and_price_in_params(provider_mods):
    """Commerce/parts vendors (Amazon/JLCPCB/PCBWay) return artifact_path=None: the record
    must carry geometry_ref=None (no importable file) and the price/availability in params,
    and must NOT crash on the missing artifact_path (provenance_envelope omits it when None)."""
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    src.acquire_return = {
        "ok": True, "source_url": "https://fakesite.example/widget",
        "fetched_ts": "2026-07-04T00:00:00Z", "session_profile_used": "fakesite",
        "license_note": "n",  # no artifact_path key at all (no CAD file)
        "price": "$9.99", "availability": "In Stock", "asin": "B00ABCDE12",
    }
    provider = parts_provider.BrowserCatalogAdapter(src)
    rec = provider.get("catalog:fakesite::https://fakesite.example/widget")
    assert rec["geometry_ref"] is None
    assert rec["params"]["price"] == "$9.99"
    assert rec["params"]["availability"] == "In Stock"
    assert rec["params"]["asin"] == "B00ABCDE12"


def test_count_and_iter_records_raise_not_implemented(provider_mods):
    _profiles, parts_provider = provider_mods
    provider = parts_provider.BrowserCatalogAdapter(_FakeSourceAdapter())

    with pytest.raises(NotImplementedError):
        provider.count()
    with pytest.raises(NotImplementedError):
        next(iter(provider.iter_records()))


def test_health_wizard_active_is_deferred_not_all_clear(monkeypatch):
    """Active login wizard must yield deferred/needs_human_login rows — never []."""
    from services.browser import health as h

    class _Mgr:
        def wizard_active(self):
            return True

    monkeypatch.setattr("services.browser.embed.get_manager", lambda: _Mgr())
    rows = h.run_health_check(["mcmaster", "grabcad"])
    assert rows, "wizard-active health returned empty all-clear"
    assert len(rows) == 2
    for r in rows:
        assert r["ok"] is False
        assert r.get("deferred") is True
        assert r["error"] == "needs_human_login"
        assert r.get("expected") is True


def test_health_default_sites_are_login_wizard_sites(monkeypatch):
    """Default run_health_check (sites=None) covers every login-wall adapter."""
    from services.browser import adapters, health as h

    class _Mgr:
        def wizard_active(self):
            return True

    monkeypatch.setattr("services.browser.embed.get_manager", lambda: _Mgr())
    rows = h.run_health_check(None)
    wizard = set(adapters.login_wizard_sites())
    assert wizard, "login_wizard_sites() must be non-empty"
    row_sites = {r["site"] for r in rows}
    # Every login-wall adapter must appear; health may also include canary leftovers.
    assert wizard.issubset(row_sites), f"missing login-wall sites: {wizard - row_sites}"
    for r in rows:
        assert set(r) >= {"site", "ok", "latency_ms", "logged_in", "hit_count", "error"}
        if r["site"] in wizard:
            assert r["ok"] is False
            assert r["error"] == "needs_human_login"
            assert r.get("deferred") is True
            assert r.get("expected") is True


def test_health_caps_concurrency_for_cef_reliability(monkeypatch):
    """The health sweep must not overload CEF and turn slow pages into false failures."""
    import threading
    import time

    from services.browser import health as h

    class _Mgr:
        def wizard_active(self):
            return False

    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_check(site):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"site": site, "ok": True, "latency_ms": 30.0, "logged_in": False,
                "hit_count": 1, "error": None}

    monkeypatch.setattr("services.browser.embed.get_manager", lambda: _Mgr())
    monkeypatch.setattr(h, "_check_site", fake_check)

    rows = h.run_health_check([f"site-{i}" for i in range(9)])

    assert len(rows) == 9
    assert max_active == h._MAX_CONCURRENT_CANARIES == 3


def test_health_row_shape_on_login_required(monkeypatch):
    """login_required canary failures keep the required status-board fields."""
    from services.browser import health as h
    from services.browser import session

    class _Mgr:
        def wizard_active(self):
            return False

    monkeypatch.setattr("services.browser.embed.get_manager", lambda: _Mgr())
    monkeypatch.setattr(
        session, "browser_fetch",
        lambda url, site: {
            "ok": True,
            "content": '<html><body><form class="login-form"><h1>Sign In</h1>'
                       '<input id="signInName"/></form></body></html>',
        })
    # digikey is registered and has a canary; login wall → expected failure.
    rows = h.run_health_check(["digikey"])
    assert len(rows) == 1
    r = rows[0]
    assert set(r) >= {"site", "ok", "latency_ms", "logged_in", "hit_count", "error"}
    assert r["site"] == "digikey"
    assert r["ok"] is False
    assert r["error"] in ("login_required", "needs_human_login")
    assert r.get("expected") is True
    assert r["logged_in"] is False
