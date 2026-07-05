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
    from services.browser.adapters import amazon, jlcpcb, mcmaster, pcbway
    importlib.reload(amazon)
    importlib.reload(mcmaster)
    importlib.reload(jlcpcb)
    importlib.reload(pcbway)
    yield adapters_pkg, grabcad, traceparts, session, amazon, mcmaster, jlcpcb, pcbway
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

AMAZON_SEARCH_HTML = """
<html><body>
<div data-asin="B00WIDGET1"><a class="a-link-normal" href="/dp/B00WIDGET1"><span class="a-text-normal">Widget Abc</span></a>
<span class="a-offscreen">$12.99</span></div>
<div data-asin="B00BRACKT1"><a class="a-link-normal" href="/dp/B00BRACKT1"><span class="a-text-normal">Bracket Xyz</span></a>
<span class="a-offscreen">$7.49</span></div>
</body></html>
"""

AMAZON_LOGIN_WALL_HTML = '<html><body><form name="signIn"><input id="ap_email"></form></body></html>'

AMAZON_CHALLENGE_HTML = (
    '<html><body>Sorry, we just need to make sure you\'re not a robot. '
    'Enter the characters you see below.</body></html>'
)

AMAZON_DETAIL_HTML = """
<html><body>
<span class="a-offscreen">$12.99</span>
<div id="availability"><span>In Stock.</span></div>
<span aria-label="Amazon Prime">Prime</span>
<script>{"asin":"B00WIDGET1"}</script>
</body></html>
"""

MCMASTER_SEARCH_HTML = """
<html><body>
<a class="product-card" href="/91290A115/">Socket Head Cap Screw <span class="part-number">91290A115</span></a>
</body></html>
"""

MCMASTER_LOGIN_WALL_HTML = '<html><body>Sign In <div id="SignInEmail"></div></body></html>'

MCMASTER_TERMS_HTML = """
<html><body>
<div class="cad-terms">Accept the terms below to continue</div>
</body></html>
"""

MCMASTER_DETAIL_HTML = """
<html><body>
<span class="part-number">91290A115</span>
<span class="price">$8.32</span>
<a href="/download/91290A115.step?token=1">Download STEP</a>
</body></html>
"""

JLCPCB_SEARCH_HTML = """
<html><body>
<a class="part-item" href="/partdetail/Basic_C123456">STM32F103C8T6
<span class="lcsc-number">C123456</span><span class="price">$1.23</span><span class="stock">4,500 in stock</span></a>
</body></html>
"""

JLCPCB_LOGIN_WALL_HTML = '<html><body>Sign In <div id="login-email"></div></body></html>'

JLCPCB_TERMS_HTML = """
<html><body>
<div class="order-terms">You must agree to the Terms to place this order</div>
</body></html>
"""

JLCPCB_DETAIL_HTML = """
<html><body>
<span class="lcsc-number">C123456</span>
<span class="price">$1.23</span>
<span class="stock">4,500 in stock</span>
</body></html>
"""

PCBWAY_SEARCH_HTML = """
<html><body>
<a class="shop-item" href="/project/shopindex/Widget_Abc.html">Widget Abc
<span class="price">$3.50</span><span class="stock">200 in stock</span></a>
</body></html>
"""

PCBWAY_LOGIN_WALL_HTML = '<html><body>Sign In <div id="login-email"></div></body></html>'

PCBWAY_TERMS_HTML = """
<html><body>
<div class="order-terms">You must agree to the Terms to place this order</div>
</body></html>
"""

PCBWAY_DETAIL_HTML = """
<html><body>
<span class="price">$3.50</span>
<span class="stock">200 in stock</span>
</body></html>
"""


# --- registry / envelope helpers -------------------------------------------------

def test_registry_has_both_adapters(adapter_mods):
    adapters_pkg, grabcad, traceparts, _session, amazon, mcmaster, jlcpcb, pcbway = adapter_mods
    assert adapters_pkg.get_adapter("grabcad") is grabcad.ADAPTER
    assert adapters_pkg.get_adapter("traceparts") is traceparts.ADAPTER
    assert adapters_pkg.get_adapter("amazon") is amazon.ADAPTER
    assert adapters_pkg.get_adapter("mcmaster") is mcmaster.ADAPTER
    assert adapters_pkg.get_adapter("jlcpcb") is jlcpcb.ADAPTER
    assert adapters_pkg.get_adapter("pcbway") is pcbway.ADAPTER
    keys = {a.key for a in adapters_pkg.all_adapters()}
    assert {"grabcad", "traceparts", "amazon", "mcmaster", "jlcpcb", "pcbway"} <= keys


def test_provenance_envelope_has_required_fields(adapter_mods):
    adapters_pkg, _grabcad, _traceparts, _session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
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
    adapters_pkg, _grabcad, _traceparts, _session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    q = adapters_pkg.login_required("grabcad", "https://grabcad.com/library/x")
    assert q["ok"] is False
    assert q["reason"] == "login_required"
    ask = q["ask_user"]
    assert ask["kind"] == "approval"
    assert ask["required"] is True
    assert ask["suggested"] is True


def test_terms_confirmation_required_shape(adapter_mods):
    adapters_pkg, _grabcad, _traceparts, _session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    q = adapters_pkg.terms_confirmation_required("traceparts", "https://x", "reseller terms")
    assert q["ok"] is False
    assert q["reason"] == "terms_confirmation_required"
    assert q["ask_user"]["kind"] == "approval"


def test_download_dir_is_created_under_browser_downloads(adapter_mods):
    adapters_pkg, _grabcad, _traceparts, _session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    from engine import paths
    d = adapters_pkg.download_dir("GrabCAD")
    assert d.exists()
    assert d == paths.BROWSER_DOWNLOADS / "grabcad"


# --- GrabCAD ----------------------------------------------------------------------

def test_grabcad_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": GRABCAD_SEARCH_HTML})

    hits = grabcad.ADAPTER.search("widget")

    assert len(hits) == 2
    assert hits[0]["title"] == "Widget Abc"
    assert hits[0]["url"] == "https://grabcad.com/library/widget-abc"
    assert hits[0]["site"] == "grabcad"


def test_grabcad_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": GRABCAD_LOGIN_WALL_HTML})

    hits = grabcad.ADAPTER.search("widget")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_grabcad_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = grabcad.ADAPTER.search("widget")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_grabcad_acquire_downloads_and_returns_provenance(adapter_mods, monkeypatch, tmp_path):
    _adapters_pkg, grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods

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
    _adapters_pkg, grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": GRABCAD_LOGIN_WALL_HTML})

    result = grabcad.ADAPTER.acquire({"url": "https://grabcad.com/library/widget-abc"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_grabcad_acquire_no_download_link(adapter_mods, monkeypatch):
    _adapters_pkg, grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": "<html>nothing here</html>"})

    result = grabcad.ADAPTER.acquire({"url": "https://grabcad.com/library/widget-abc"})
    assert result["ok"] is False
    assert result["reason"] == "no_download_link"


def test_grabcad_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, grabcad, _traceparts, _session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    result = grabcad.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


# --- TraceParts ---------------------------------------------------------------------

def test_traceparts_acquire_surfaces_terms_confirmation(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, traceparts, session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": TRACEPARTS_TERMS_HTML})

    result = traceparts.ADAPTER.acquire({"url": "https://www.traceparts.com/en/product/x"})

    assert result["ok"] is False
    assert result["reason"] == "terms_confirmation_required"
    assert result["ask_user"]["kind"] == "approval"


def test_traceparts_never_auto_accepts_terms(adapter_mods, monkeypatch):
    """A terms wall must never fall through to a download attempt."""
    _adapters_pkg, _grabcad, traceparts, session, _amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    called = {"download": False}

    def fake_download(url, site, dest):
        called["download"] = True
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": TRACEPARTS_TERMS_HTML})
    monkeypatch.setattr(session, "browser_download", fake_download)

    traceparts.ADAPTER.acquire({"url": "https://www.traceparts.com/en/product/x"})
    assert called["download"] is False


# --- Amazon -------------------------------------------------------------------------

def test_amazon_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": AMAZON_SEARCH_HTML})

    hits = amazon.ADAPTER.search("widget")

    assert len(hits) == 2
    assert hits[0]["title"] == "Widget Abc"
    assert hits[0]["url"] == "https://www.amazon.com/dp/B00WIDGET1"
    assert hits[0]["site"] == "amazon"
    assert hits[0]["asin"] == "B00WIDGET1"
    assert hits[0]["price"] == "$12.99"


def test_amazon_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": AMAZON_LOGIN_WALL_HTML})

    hits = amazon.ADAPTER.search("widget")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_amazon_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = amazon.ADAPTER.search("widget")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_amazon_acquire_returns_price_record_no_artifact(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": AMAZON_DETAIL_HTML})

    result = amazon.ADAPTER.acquire({"url": "https://www.amazon.com/dp/B00WIDGET1"})

    assert result["ok"] is True
    assert result["source_url"] == "https://www.amazon.com/dp/B00WIDGET1"
    assert "license_note" in result and result["license_note"]
    assert result.get("artifact_path") is None
    assert result["price"] == "$12.99"
    assert result["availability"] == "In Stock."
    assert result["asin"] == "B00WIDGET1"
    assert result["prime"] is True
    assert result["currency"] == "USD"


def test_amazon_acquire_login_wall(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": AMAZON_LOGIN_WALL_HTML})

    result = amazon.ADAPTER.acquire({"url": "https://www.amazon.com/dp/B00WIDGET1"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_amazon_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _grabcad, _traceparts, _session, amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    result = amazon.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


def test_amazon_acquire_hits_bot_challenge_never_bypasses(adapter_mods, monkeypatch):
    """A CAPTCHA/bot-challenge page must degrade to login_required, never be solved."""
    _adapters_pkg, _grabcad, _traceparts, session, amazon, _mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": AMAZON_CHALLENGE_HTML})

    result = amazon.ADAPTER.acquire({"url": "https://www.amazon.com/dp/B00WIDGET1"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


# --- McMaster-Carr ------------------------------------------------------------------

def test_mcmaster_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MCMASTER_SEARCH_HTML})

    hits = mcmaster.ADAPTER.search("socket head cap screw")

    assert len(hits) == 1
    assert "Socket Head Cap Screw" in hits[0]["title"]
    assert hits[0]["url"] == "https://www.mcmaster.com/91290A115/"
    assert hits[0]["site"] == "mcmaster"
    assert hits[0]["sku"] == "91290A115"


def test_mcmaster_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MCMASTER_LOGIN_WALL_HTML})

    hits = mcmaster.ADAPTER.search("screw")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_mcmaster_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = mcmaster.ADAPTER.search("screw")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_mcmaster_acquire_downloads_and_returns_provenance_with_sku(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, mcmaster, _jlcpcb, _pcbway = adapter_mods

    def fake_fetch(url, site):
        return {"ok": True, "content": MCMASTER_DETAIL_HTML}

    def fake_download(url, site, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"STEP-FILE-BYTES")
        return {"ok": True, "dest": str(dest), "session_profile_used": "mcmaster"}

    monkeypatch.setattr(session, "browser_fetch", fake_fetch)
    monkeypatch.setattr(session, "browser_download", fake_download)

    result = mcmaster.ADAPTER.acquire({"title": "Socket Head Cap Screw", "sku": "91290A115",
                                        "url": "https://www.mcmaster.com/91290A115/"})

    assert result["ok"] is True
    assert result["source_url"] == "https://www.mcmaster.com/91290A115/"
    assert result["session_profile_used"] == "mcmaster"
    assert "license_note" in result and result["license_note"]
    assert result["artifact_path"].endswith("91290A115.step")
    assert result["sku"] == "91290A115"
    assert result["price"] == "$8.32"


def test_mcmaster_acquire_login_wall(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MCMASTER_LOGIN_WALL_HTML})

    result = mcmaster.ADAPTER.acquire({"url": "https://www.mcmaster.com/91290A115/"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_mcmaster_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _grabcad, _traceparts, _session, _amazon, mcmaster, _jlcpcb, _pcbway = adapter_mods
    result = mcmaster.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


def test_mcmaster_acquire_surfaces_terms_confirmation(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, mcmaster, _jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MCMASTER_TERMS_HTML})

    result = mcmaster.ADAPTER.acquire({"url": "https://www.mcmaster.com/91290A115/"})

    assert result["ok"] is False
    assert result["reason"] == "terms_confirmation_required"
    assert result["ask_user"]["kind"] == "approval"


def test_mcmaster_never_auto_accepts_terms(adapter_mods, monkeypatch):
    """A terms wall must never fall through to a download attempt."""
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, mcmaster, _jlcpcb, _pcbway = adapter_mods
    called = {"download": False}

    def fake_download(url, site, dest):
        called["download"] = True
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MCMASTER_TERMS_HTML})
    monkeypatch.setattr(session, "browser_download", fake_download)

    mcmaster.ADAPTER.acquire({"url": "https://www.mcmaster.com/91290A115/"})
    assert called["download"] is False


# --- JLCPCB -------------------------------------------------------------------------

def test_jlcpcb_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": JLCPCB_SEARCH_HTML})

    hits = jlcpcb.ADAPTER.search("stm32f103c8t6")

    assert len(hits) == 1
    assert "STM32F103C8T6" in hits[0]["title"]
    assert hits[0]["url"] == "https://jlcpcb.com/partdetail/Basic_C123456"
    assert hits[0]["site"] == "jlcpcb"
    assert hits[0]["lcsc"] == "C123456"
    assert hits[0]["price"] == "$1.23"
    assert hits[0]["stock"] == "4,500"


def test_jlcpcb_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": JLCPCB_LOGIN_WALL_HTML})

    hits = jlcpcb.ADAPTER.search("stm32")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_jlcpcb_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = jlcpcb.ADAPTER.search("stm32")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_jlcpcb_acquire_returns_structured_record(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": JLCPCB_DETAIL_HTML})

    result = jlcpcb.ADAPTER.acquire({"url": "https://jlcpcb.com/partdetail/Basic_C123456"})

    assert result["ok"] is True
    assert result["source_url"] == "https://jlcpcb.com/partdetail/Basic_C123456"
    assert "license_note" in result and result["license_note"]
    assert result.get("artifact_path") is None
    assert result["lcsc"] == "C123456"
    assert result["price"] == "$1.23"
    assert result["stock"] == "4,500"


def test_jlcpcb_acquire_login_wall(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": JLCPCB_LOGIN_WALL_HTML})

    result = jlcpcb.ADAPTER.acquire({"url": "https://jlcpcb.com/partdetail/Basic_C123456"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_jlcpcb_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _grabcad, _traceparts, _session, _amazon, _mcmaster, jlcpcb, _pcbway = adapter_mods
    result = jlcpcb.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


def test_jlcpcb_acquire_surfaces_terms_confirmation(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": JLCPCB_TERMS_HTML})

    result = jlcpcb.ADAPTER.acquire({"url": "https://jlcpcb.com/partdetail/Basic_C123456"})

    assert result["ok"] is False
    assert result["reason"] == "terms_confirmation_required"
    assert result["ask_user"]["kind"] == "approval"


def test_jlcpcb_never_auto_accepts_terms(adapter_mods, monkeypatch):
    """A terms wall must never fall through to a would-be order/quote action."""
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, jlcpcb, _pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": JLCPCB_TERMS_HTML})

    result = jlcpcb.ADAPTER.acquire({"url": "https://jlcpcb.com/partdetail/Basic_C123456"})
    assert result["reason"] == "terms_confirmation_required"
    assert result.get("artifact_path") is None


# --- PCBWay -------------------------------------------------------------------------

def test_pcbway_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": PCBWAY_SEARCH_HTML})

    hits = pcbway.ADAPTER.search("widget")

    assert len(hits) == 1
    assert hits[0]["title"] == "Widget Abc"
    assert hits[0]["url"] == "https://www.pcbway.com/project/shopindex/Widget_Abc.html"
    assert hits[0]["site"] == "pcbway"
    assert hits[0]["price"] == "$3.50"
    assert hits[0]["stock"] == "200"


def test_pcbway_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": PCBWAY_LOGIN_WALL_HTML})

    hits = pcbway.ADAPTER.search("widget")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_pcbway_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = pcbway.ADAPTER.search("widget")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_pcbway_acquire_returns_structured_record(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": PCBWAY_DETAIL_HTML})

    result = pcbway.ADAPTER.acquire({"url": "https://www.pcbway.com/project/shopindex/Widget_Abc.html"})

    assert result["ok"] is True
    assert result["source_url"] == "https://www.pcbway.com/project/shopindex/Widget_Abc.html"
    assert "license_note" in result and result["license_note"]
    assert result.get("artifact_path") is None
    assert result["price"] == "$3.50"
    assert result["stock"] == "200"


def test_pcbway_acquire_login_wall(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": PCBWAY_LOGIN_WALL_HTML})

    result = pcbway.ADAPTER.acquire({"url": "https://www.pcbway.com/project/shopindex/Widget_Abc.html"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_pcbway_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _grabcad, _traceparts, _session, _amazon, _mcmaster, _jlcpcb, pcbway = adapter_mods
    result = pcbway.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


def test_pcbway_acquire_surfaces_terms_confirmation(adapter_mods, monkeypatch):
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": PCBWAY_TERMS_HTML})

    result = pcbway.ADAPTER.acquire({"url": "https://www.pcbway.com/project/shopindex/Widget_Abc.html"})

    assert result["ok"] is False
    assert result["reason"] == "terms_confirmation_required"
    assert result["ask_user"]["kind"] == "approval"


def test_pcbway_never_auto_accepts_terms(adapter_mods, monkeypatch):
    """A terms wall must never fall through to a would-be order/quote action."""
    _adapters_pkg, _grabcad, _traceparts, session, _amazon, _mcmaster, _jlcpcb, pcbway = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": PCBWAY_TERMS_HTML})

    result = pcbway.ADAPTER.acquire({"url": "https://www.pcbway.com/project/shopindex/Widget_Abc.html"})
    assert result["reason"] == "terms_confirmation_required"
    assert result.get("artifact_path") is None
