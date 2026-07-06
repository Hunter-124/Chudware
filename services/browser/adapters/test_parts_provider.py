"""Tests for services/browser/adapters/parts_provider.py — T-X3.

No real Playwright/network: a fake SourceAdapter stands in for GrabCAD/TraceParts.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def provider_mods(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
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


def test_search_returns_empty_without_profile(provider_mods):
    profiles, parts_provider = provider_mods
    src = _FakeSourceAdapter()
    provider = parts_provider.BrowserCatalogAdapter(src)

    hits = provider.search("widget")

    assert hits == []
    assert src.search_calls == []  # never even called the underlying adapter


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


def test_get_returns_none_without_profile(provider_mods):
    _profiles, parts_provider = provider_mods
    src = _FakeSourceAdapter()
    provider = parts_provider.BrowserCatalogAdapter(src)

    assert provider.get("catalog:fakesite::https://fakesite.example/widget") is None
    assert src.acquire_calls == []


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


def test_get_propagates_acquire_failure(provider_mods):
    profiles, parts_provider = provider_mods
    profiles.profile_dir("fakesite").mkdir(parents=True)
    src = _FakeSourceAdapter()
    src.acquire_return = {"ok": False, "reason": "login_required", "ask_user": {"kind": "approval"}}
    provider = parts_provider.BrowserCatalogAdapter(src)

    rec = provider.get("catalog:fakesite::https://fakesite.example/widget")

    assert rec == {"ok": False, "reason": "login_required", "ask_user": {"kind": "approval"}}


def test_get_folds_vendor_extras_into_params(provider_mods):
    """T-X7 item 3: acquire-envelope extras (e.g. McMaster sku + price) land in params
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
