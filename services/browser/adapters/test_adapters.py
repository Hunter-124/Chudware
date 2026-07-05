"""Tests for services/browser/adapters — T-X2.

No real Playwright, no real network: `services.browser.session`'s
`browser_fetch`/`browser_download` are monkeypatched directly (adapters call
them via `_session.browser_fetch(...)`, so patching the session module's
attribute is enough — no adapter talks to Playwright itself).
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def adapter_mods(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import grabcad, traceparts
    importlib.reload(grabcad)
    importlib.reload(traceparts)
    yield adapters_pkg, grabcad, traceparts, session
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


GRABCAD_SEARCH_HTML = """
<html><body>
<a class="card-title result" href="/library/widget-abc">Widget <b>Abc</b></a>
<a class="card-title result" href="/library/bracket-xyz">Bracket Xyz</a>
</body></html>
"""

GRABCAD_DETAIL_HTML = """
<html><body>
<a href="/downloads/widget-abc.step?token=1">Download STEP</a>
</body></html>
"""

GRABCAD_LOGIN_WALL_HTML = '<html><body>Log in to GrabCAD <div id="user_email"></div></body></html>'

TRACEPARTS_TERMS_HTML = """
<html><body>
<div class="supplier-terms">Accept the terms below to continue</div>
</body></html>
"""


# --- registry / envelope helpers -------------------------------------------------

def test_registry_has_both_adapters(adapter_mods):
    adapters_pkg, grabcad, traceparts, _session = adapter_mods
    assert adapters_pkg.get_adapter("grabcad") is grabcad.ADAPTER
    assert adapters_pkg.get_adapter("traceparts") is traceparts.ADAPTER
    keys = {a.key for a in adapters_pkg.all_adapters()}
    assert {"grabcad", "traceparts"} <= keys


def test_provenance_envelope_has_required_fields(adapter_mods):
    adapters_pkg, _grabcad, _traceparts, _session = adapter_mods
    env = adapters_pkg.provenance_envelope(
        source_url="https://grabcad.com/library/widget-abc",
        session_profile_used="grabcad",
        license_note="test note",
        artifact_path="/tmp/x.step",
    )
    assert env["ok"] is True
    for field in ("source_url", "fetched_ts", "session_profile_used", "license_note"):
        assert field in env
    assert env["artifact_path"] == "/tmp/x.step"


def test_login_required_shape(adapter_mods):
    adapters_pkg, _grabcad, _traceparts, _session = adapter_mods
    q = adapters_pkg.login_required("grabcad", "https://grabcad.com/library/x")
    assert q["ok"] is False
    assert q["reason"] == "login_required"
    ask = q["ask_user"]
    assert ask["kind"] == "approval"
    assert ask["required"] is True
    assert ask["suggested"] is True


def test_terms_confirmation_required_shape(adapter_mods):
    adapters_pkg, _grabcad, _traceparts, _session = adapter_mods
    q = adapters_pkg.terms_confirmation_required("traceparts", "https://x", "reseller terms")
    assert q["ok"] is False
    assert q["reason"] == "terms_confirmation_required"
    assert q["ask_user"]["kind"] == "approval"


def test_download_dir_is_created_under_browser_downloads(adapter_mods):
    adapters_pkg, _grabcad, _traceparts, _session = adapter_mods
    from engine import paths
    d = adapters_pkg.download_dir("GrabCAD")
    assert d.exists()
    assert d == paths.BROWSER_DOWNLOADS / "grabcad"


# --- GrabCAD ----------------------------------------------------------------------

def test_grabcad_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, grabcad, _traceparts, session = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": GRABCAD_SEARCH_HTML})

    hits = grabcad.ADAPTER.search("widget")

    assert len(hits) == 2
    assert hits[0]["title"] == "Widget Abc"
    assert hits[0]["url"] == "https://grabcad.com/library/widget-abc"
    assert hits[0]["site"] == "grabcad"


def test_grabcad_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, grabcad, _traceparts, session = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": GRABCAD_LOGIN_WALL_HTML})

    hits = grabcad.ADAPTER.search("widget")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_grabcad_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, grabcad, _traceparts, session = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = grabcad.ADAPTER.search("widget")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_grabcad_acquire_downloads_and_returns_provenance(adapter_mods, monkeypatch, tmp_path):
    _adapters_pkg, grabcad, _traceparts, session = adapter_mods

    def fake_fetch(url, site):
        return {"ok": True, "content": GRABCAD_DETAIL_HTML}

    def fake_download(url, site, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"STEP-FILE-BYTES")
        return {"ok": True, "dest": str(dest), "session_profile_used": "grabcad"}

    monkeypatch.setattr(session, "browser_fetch", fake_fetch)
    monkeypatch.setattr(session, "browser_download", fake_download)

    result = grabcad.ADAPTER.acquire({"title": "Widget Abc", "url": "https://grabcad.com/library/widget-abc"})

    assert result["ok"] is True
    assert result["source_url"] == "https://grabcad.com/library/widget-abc"
    assert result["session_profile_used"] == "grabcad"
    assert "license_note" in result and result["license_note"]
    assert result["artifact_path"].endswith("widget-abc.step")


def test_grabcad_acquire_login_wall_on_detail_page(adapter_mods, monkeypatch):
    _adapters_pkg, grabcad, _traceparts, session = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": GRABCAD_LOGIN_WALL_HTML})

    result = grabcad.ADAPTER.acquire({"url": "https://grabcad.com/library/widget-abc"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_grabcad_acquire_no_download_link(adapter_mods, monkeypatch):
    _adapters_pkg, grabcad, _traceparts, session = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": "<html>nothing here</html>"})

    result = grabcad.ADAPTER.acquire({"url": "https://grabcad.com/library/widget-abc"})
    assert result["ok"] is False
    assert result["reason"] == "no_download_link"


def test_grabcad_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, grabcad, _traceparts, _session = adapter_mods
    result = grabcad.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


# --- TraceParts ---------------------------------------------------------------------

def test_traceparts_acquire_surfaces_terms_confirmation(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, traceparts, session = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": TRACEPARTS_TERMS_HTML})

    result = traceparts.ADAPTER.acquire({"url": "https://www.traceparts.com/en/product/x"})

    assert result["ok"] is False
    assert result["reason"] == "terms_confirmation_required"
    assert result["ask_user"]["kind"] == "approval"


def test_traceparts_never_auto_accepts_terms(adapter_mods, monkeypatch):
    """A terms wall must never fall through to a download attempt."""
    _adapters_pkg, _grabcad, traceparts, session = adapter_mods
    called = {"download": False}

    def fake_download(url, site, dest):
        called["download"] = True
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": TRACEPARTS_TERMS_HTML})
    monkeypatch.setattr(session, "browser_download", fake_download)

    traceparts.ADAPTER.acquire({"url": "https://www.traceparts.com/en/product/x"})
    assert called["download"] is False
