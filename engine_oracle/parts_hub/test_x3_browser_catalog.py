"""Tests for T-X3: the import_file placer + browser catalog provider hub wiring.

No real core, no real Playwright/network: core_bridge.call and the browser
adapters are monkeypatched/faked directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_SVC_MCP = _REPO / "services" / "mcp"
if str(_SVC_MCP) not in sys.path:
    sys.path.insert(0, str(_SVC_MCP))


# ── _place_import_file (place.py) ────────────────────────────────────────────

def test_place_import_file_missing_path():
    from engine_oracle.parts_hub.place import _place_import_file
    r = _place_import_file({"kind": "import_file"}, None, None)
    assert r["ok"] is False
    assert "path" in r["reason"]


def test_place_import_file_file_not_found():
    from engine_oracle.parts_hub.place import _place_import_file
    r = _place_import_file({"kind": "import_file", "path": "/no/such/file.step"}, None, None)
    assert r["ok"] is False
    assert "not found" in r["reason"]


def test_place_import_file_success(tmp_path, monkeypatch):
    fake_step = tmp_path / "widget.step"
    fake_step.write_text("ISO-10303-21;")

    import core_bridge as cb  # type: ignore
    monkeypatch.setattr(cb, "call", lambda verb, params: {"doc_id": "doc-abc", "body_count": 1})

    from engine_oracle.parts_hub.place import _place_import_file
    r = _place_import_file({"kind": "import_file", "path": str(fake_step)}, None, None)

    assert r["ok"] is True
    assert r["editable"] is True
    assert r["doc_id"] == "doc-abc"
    assert r["entity_id"] == "doc-abc"
    assert "reverse_engineer" in r["detail"]["hint"]


def test_place_import_file_rpc_failure(tmp_path, monkeypatch):
    fake_step = tmp_path / "widget.step"
    fake_step.write_text("ISO-10303-21;")

    import core_bridge as cb  # type: ignore

    def _raise(verb, params):
        raise RuntimeError("no core")

    monkeypatch.setattr(cb, "call", _raise)

    from engine_oracle.parts_hub.place import _place_import_file
    r = _place_import_file({"kind": "import_file", "path": str(fake_step)}, None, None)
    assert r["ok"] is False
    assert "import_file RPC failed" in r["reason"]


def test_place_dispatches_import_file_kind():
    """place.py's public dispatch table must route the new kind."""
    from engine_oracle.parts_hub.place import _PLACERS
    assert "import_file" in _PLACERS


# ── login-degradation seam (walled catalog vendor → actionable next step) ────

class _FakeWalledAdapter:
    """A SourceAdapter whose acquire hits a login/terms/distributor wall."""
    def __init__(self, key, outcome):
        self.key = key
        self._outcome = outcome
    def search(self, query):
        return [{"ok": True, "title": "M8 SHCS", "url": f"https://{self.key}.com/p/84881A248"}]
    def acquire(self, result):
        return dict(self._outcome)


def test_parts_provider_get_enriches_acquire_failure_with_identity():
    """When acquire fails, BrowserCatalogAdapter.get must stamp part_id/site/link-out
    onto the envelope so the downstream seam can act on it."""
    from services.browser.adapters import login_required
    from services.browser.adapters.parts_provider import BrowserCatalogAdapter
    prov = BrowserCatalogAdapter(_FakeWalledAdapter(
        "mcmaster", login_required("mcmaster", "https://mcmaster.com/p/84881A248")))
    # search first so the url is cached under the part_id
    recs = prov.search("m8 shcs")
    pid = recs[0]["part_id"]
    out = prov.get(pid)
    assert out["ok"] is False
    assert out["reason"] == "login_required"
    assert out["part_id"] == pid
    assert out["site"] == "mcmaster"
    assert out["source_url"].startswith("https://mcmaster.com/")
    assert out["ask_user"]["kind"] == "approval"  # adapter's question preserved


def _place_with_record(monkeypatch, record):
    import engine_oracle.parts_hub as ph
    monkeypatch.setattr(ph, "get", lambda pid: record)
    from engine_oracle.parts_hub.place import place
    return place("catalog:mcmaster::https://mcmaster.com/p/84881A248")


def test_place_surfaces_login_required_actionably(monkeypatch):
    from services.browser.adapters import login_required
    rec = dict(login_required("mcmaster", "https://mcmaster.com/p/84881A248"),
               site="mcmaster", source_url="https://mcmaster.com/p/84881A248")
    r = _place_with_record(monkeypatch, rec)
    assert r["ok"] is False
    assert r["reason"] == "Cannot place 'catalog:mcmaster::https://mcmaster.com/p/84881A248': login_required."
    assert "vendor_login('mcmaster')" in r["hint"]
    assert "retry part_place" in r["hint"]
    # the sign-in question + link-out survive for the caller to route to the user
    assert r["detail"]["ask_user"]["id"] == "browser_login.mcmaster"
    assert r["detail"]["source_url"] == "https://mcmaster.com/p/84881A248"


def test_place_surfaces_distributor_only(monkeypatch):
    from services.browser.adapters import distributor_only
    rec = dict(distributor_only("hydraforce", "https://hydraforce.com/valve",
                                distributors_url="https://hydraforce.com/distributors"),
               site="hydraforce")
    r = _place_with_record(monkeypatch, rec)
    assert r["ok"] is False
    assert "distributor_only" in r["reason"]
    assert "distributors" in r["hint"]
    assert r["detail"]["distributors_url"] == "https://hydraforce.com/distributors"


def test_place_surfaces_terms_confirmation(monkeypatch):
    from services.browser.adapters import terms_confirmation_required
    rec = dict(terms_confirmation_required("traceparts", "https://traceparts.com/p/1",
                                           "per-download license"),
               site="traceparts", source_url="https://traceparts.com/p/1")
    r = _place_with_record(monkeypatch, rec)
    assert r["ok"] is False
    assert "terms_confirmation_required" in r["reason"]
    assert "traceparts.com/p/1" in r["hint"]
    assert r["detail"]["ask_user"]["id"] == "browser_terms.traceparts"


def test_place_still_reports_plain_missing_geometry(monkeypatch):
    """A genuine PartRecord with no geometry_ref (not a failure envelope) keeps the
    original 'no geometry_ref' error — the seam must not swallow that case."""
    rec = {"part_id": "catalog:x::y", "domain": "catalog", "geometry_ref": None}
    import engine_oracle.parts_hub as ph
    monkeypatch.setattr(ph, "get", lambda pid: rec)
    from engine_oracle.parts_hub.place import place
    r = place("catalog:x::y")
    assert r["ok"] is False
    assert "no geometry_ref" in r["reason"]


# ── hub wiring (bootstrap registers browser catalog providers) ──────────────

def _make_hub():
    from engine_oracle.parts_hub.hub import PartsHub
    hub = PartsHub()
    hub.bootstrap()
    return hub


def test_bootstrap_never_raises_when_browser_package_present():
    """bootstrap() must succeed even though services.browser has no saved profiles here."""
    hub = _make_hub()
    assert hub is not None


def test_bootstrap_registers_browser_catalog_providers():
    hub = _make_hub()
    provider_ids = {getattr(p, "provider_id", None) for p in hub._registry.providers()}
    assert "browser_grabcad" in provider_ids
    assert "browser_traceparts" in provider_ids


@pytest.mark.live
def test_zero_profile_search_contributes_nothing(tmp_path, monkeypatch):
    """DoD: no saved browser profile anywhere -> catalog providers contribute zero
    results, silently -- part_search behavior for an otherwise-empty query is
    unaffected by T-X3 (same shape as before this task: an empty live_results list
    from these providers, no ask_user noise).

    LIVE-NETWORK (gated 2026-07-07): this test makes a real vendor search, so it
    belongs behind the `live` marker like the other live-adapter tests — it was
    running in the deterministic suite and doing a ~2min Amazon round-trip.
    FLAG for the parts team: the ``catalog_results == []`` assertion is now
    OUTDATED. Two design changes invalidate it: (1) anonymous public-catalog
    browsing is allowed (profiles auto-created; live-probe 2026-07-07), and (2)
    browser_fetch now waits for the SPA DOM to render before snapshotting
    (session.py DEFAULT_RENDER_SETTLE_S), so Amazon returns filler/suggestion
    cards even for a nonsense query. Redefine the intended behavior — likely
    "no ask_user noise / no error" rather than "zero results" — then re-enable."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import importlib
    from engine import paths as engine_paths
    importlib.reload(engine_paths)

    hub = _make_hub()
    results = hub.search("zzz_definitely_not_a_real_part_zzz")
    catalog_results = [r for r in results if isinstance(r, dict) and r.get("domain") == "catalog"]
    assert catalog_results == []

    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)
