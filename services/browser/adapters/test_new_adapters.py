"""Tests for the second wave of source adapters —
misumi, lcsc, digikey_web, snapeda, content3d.

Same offline style as test_adapters.py: no real Playwright, no real network —
`services.browser.session`'s `browser_fetch`/`browser_download` are
monkeypatched directly (adapters call them via `_session.browser_fetch(...)`,
so patching the session module's attribute is enough).
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def adapter_mods(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    # Reloading the package resets _REGISTRY — re-register EVERY site module
    # (not just the ones under test) so the registry stays whole for later tests.
    from services.browser.adapters import (_retail, acehardware, amazon, content3d,
                                           digikey_web, fastenal, grabcad, grainger,
                                           harborfreight, homedepot, jlcpcb, lcsc, lowes,
                                           mcmaster, metalsdepot, misumi, onlinemetals,
                                           pcbway, snapeda, tractorsupply, traceparts)
    for mod in (amazon, grabcad, jlcpcb, mcmaster, pcbway, traceparts,
                misumi, lcsc, digikey_web, snapeda, content3d, _retail,
                homedepot, lowes, acehardware, grainger, fastenal, harborfreight,
                tractorsupply, onlinemetals, metalsdepot):
        importlib.reload(mod)
    yield adapters_pkg, session, misumi, lcsc, digikey_web, snapeda, content3d
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


# Real live shape (verified 2026-07-07): results are embedded in a Next.js
# `<script id="__NEXT_DATA__">` blob at
# props.pageProps.hawksearchKeywordSearchResponse.Results, each a configurable
# SERIES (empty sku, no price on the results page). See test_misumi_parser.py
# for the full verbatim slice. The old product-name/part-number markup was
# speculative and never matched the live DOM.
MISUMI_SEARCH_HTML = (
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"hawksearchKeywordSearchResponse":{"Results":['
    '{"DocId":"M0101000000_threaded-linear-shafts","Score":100.0,"Document":{'
    '"url_detail":["/vona2/mech/M0100000000/M0101000000/threaded-linear-shafts/"],'
    '"unique_id":["M0101000000_threaded-linear-shafts"],'
    '"name":["Threaded Linear Shafts"],"sku":[""]}}'
    ']}}},"page":"/result"}'
    '</script>'
)

MISUMI_LOGIN_WALL_HTML = '<html><body>Please log in <div id="loginId"></div></body></html>'

MISUMI_DETAIL_HTML = """
<html><body>
<span class="part-number">PSFJ6-100</span>
<span class="price">$4.56</span>
<a href="/cad/PSFJ6-100.step?token=1">Download STEP</a>
</body></html>
"""

# Real live shape (verified 2026-07-11): LCSC's search results are a Nuxt/Vue SSR
# table — rows are JS-clickable <tr>s, NOT <a href="/product-detail/…"> anchors.
# The per-row extraction anchor is `aria-label="<title> detail page" title="C<code>"`.
LCSC_SEARCH_HTML = """
<html><body><table><tr><td>
<span data-v-8adf140e="" aria-label="ST STM32F103C8T6 detail page" title="C8734"
 class="font-Bold-600 major--text"><span>C8734</span></span>
</td></tr></table></body></html>
"""

LCSC_LOGIN_WALL_HTML = '<html><body>Sign In <div id="login-email"></div></body></html>'

LCSC_DETAIL_HTML = """
<html><body>
<span class="lcsc-number">C8734</span>
<span class="price">$2.10</span>
<span class="stock">12,000 in stock</span>
<a href="/datasheet/STM32F103C8T6.pdf?v=2">Datasheet</a>
</body></html>
"""

# Real live shape (verified 2026-07-07): a keyword search lands on DigiKey's
# category-disambiguation page — results are embedded in a Next.js
# `<script id="__NEXT_DATA__">` blob at props.pageProps.envelope.data.topResults,
# each a CATEGORY tile (categoryName/categoryUrl/parentCategory/productCount/id),
# NOT a product. Same case McMaster handles. See test_digikey_parser.py for the
# full slice. The old product-title/digikey-part-number markup was speculative.
DIGIKEY_SEARCH_HTML = (
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"envelope":{"data":{"topResults":['
    '{"categoryName":"Chip Resistor - Surface Mount",'
    '"categoryUrl":"/en/products/filter/chip-resistor-surface-mount/52",'
    '"parentCategory":"Resistors","productCount":"1,204,216","id":"52"}'
    ']}}}}}'
    '</script>'
)

DIGIKEY_LOGIN_WALL_HTML = '<html><body>Sign In <div id="signInName"></div></body></html>'

DIGIKEY_DETAIL_HTML = """
<html><body>
<span class="digikey-part-number">497-6063-ND</span>
<span class="price">$3.75</span>
<span class="stock">8,200 in stock</span>
<a href="https://www.st.com/resource/en/datasheet/stm32f103c8.pdf">Datasheet</a>
<a href="https://www.snapeda.com/parts/STM32F103C8T6/STMicroelectronics/view-part/">ECAD Model</a>
</body></html>
"""

SNAPEDA_SEARCH_HTML = """
<html><body>
<a class="part-title" href="/parts/STM32F103C8T6/STMicroelectronics/view-part/">STM32F103C8T6
<span class="manufacturer">STMicroelectronics</span></a>
</body></html>
"""

SNAPEDA_LOGIN_WALL_HTML = ('<html><body>Sign up to download '
                           '<div id="id_username"></div></body></html>')

SNAPEDA_DETAIL_HTML = """
<html><body>
<span class="manufacturer">STMicroelectronics</span>
<a href="/api/download/STM32F103C8T6-kicad.zip?token=1">Download KiCad</a>
</body></html>
"""

CONTENT3D_SEARCH_HTML = """
<html><body>
<a class="model-title" href="/parts/supplier/widget-bearing-608zz">608ZZ Bearing
<span class="supplier">SKF</span></a>
</body></html>
"""

CONTENT3D_LOGIN_WALL_HTML = ('<html><body>You must be logged in to download '
                             '<div id="ctl00_LoginName"></div></body></html>')

CONTENT3D_DETAIL_HTML = """
<html><body>
<span class="supplier">SKF</span>
<a href="/download/608zz.step?token=1">Download STEP</a>
</body></html>
"""


# --- registry -------------------------------------------------------------------

def test_registry_has_all_new_adapters(adapter_mods):
    adapters_pkg, _session, misumi, lcsc, digikey_web, snapeda, content3d = adapter_mods
    assert adapters_pkg.get_adapter("misumi") is misumi.ADAPTER
    assert adapters_pkg.get_adapter("lcsc") is lcsc.ADAPTER
    assert adapters_pkg.get_adapter("digikey") is digikey_web.ADAPTER
    assert adapters_pkg.get_adapter("snapeda") is snapeda.ADAPTER
    assert adapters_pkg.get_adapter("3dcontentcentral") is content3d.ADAPTER
    keys = {a.key for a in adapters_pkg.all_adapters()}
    assert {"misumi", "lcsc", "digikey", "snapeda", "3dcontentcentral"} <= keys


def test_content3d_site_key_slugs_cleanly(adapter_mods):
    """Digits are legal in profile slugs — the '3dcontentcentral' key must round-trip."""
    _adapters_pkg, _session, _misumi, _lcsc, _digikey_web, _snapeda, content3d = adapter_mods
    from services.browser import profiles
    assert profiles.slug(content3d.SITE) == "3dcontentcentral"


# --- MISUMI ---------------------------------------------------------------------

def test_misumi_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, session, misumi, _lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MISUMI_SEARCH_HTML})

    hits = misumi.ADAPTER.search("linear shaft")

    assert len(hits) == 1
    assert "Linear Shaft" in hits[0]["title"]
    assert hits[0]["url"] == (
        "https://us.misumi-ec.com/vona2/mech/M0100000000/M0101000000/threaded-linear-shafts/")
    assert hits[0]["site"] == "misumi"
    assert hits[0]["part_number"] == "M0101000000_threaded-linear-shafts"
    assert hits[0]["kind"] == "series"
    # empty sku on the real results page → no invented value emitted
    assert "sku" not in hits[0]


def test_misumi_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, session, misumi, _lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MISUMI_LOGIN_WALL_HTML})

    hits = misumi.ADAPTER.search("shaft")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_misumi_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, session, misumi, _lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = misumi.ADAPTER.search("shaft")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_misumi_acquire_downloads_and_returns_provenance(adapter_mods, monkeypatch):
    _adapters_pkg, session, misumi, _lcsc, _digikey_web, _snapeda, _content3d = adapter_mods

    def fake_fetch(url, site):
        return {"ok": True, "content": MISUMI_DETAIL_HTML}

    def fake_download(url, site, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"STEP-FILE-BYTES")
        return {"ok": True, "dest": str(dest), "session_profile_used": "misumi"}

    monkeypatch.setattr(session, "browser_fetch", fake_fetch)
    monkeypatch.setattr(session, "browser_download", fake_download)

    result = misumi.ADAPTER.acquire({"title": "Linear Shaft",
                                     "url": "https://us.misumi-ec.com/vona2/detail/110302634310/"})

    assert result["ok"] is True
    assert result["source_url"] == "https://us.misumi-ec.com/vona2/detail/110302634310/"
    assert result["session_profile_used"] == "misumi"
    assert "license_note" in result and result["license_note"]
    assert result["artifact_path"].endswith("PSFJ6-100.step")
    assert result["part_number"] == "PSFJ6-100"
    assert result["price"] == "$4.56"


def test_misumi_acquire_login_wall(adapter_mods, monkeypatch):
    _adapters_pkg, session, misumi, _lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": MISUMI_LOGIN_WALL_HTML})

    result = misumi.ADAPTER.acquire({"url": "https://us.misumi-ec.com/vona2/detail/x/"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_misumi_acquire_no_download_link(adapter_mods, monkeypatch):
    _adapters_pkg, session, misumi, _lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": "<html>nothing here</html>"})

    result = misumi.ADAPTER.acquire({"url": "https://us.misumi-ec.com/vona2/detail/x/"})
    assert result["ok"] is False
    assert result["reason"] == "no_download_link"


def test_misumi_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _session, misumi, _lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    result = misumi.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


# --- LCSC -----------------------------------------------------------------------

def test_lcsc_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": LCSC_SEARCH_HTML})

    hits = lcsc.ADAPTER.search("stm32f103c8t6")

    assert len(hits) == 1
    assert "STM32F103C8T6" in hits[0]["title"]
    # URL is derived from the LCSC code (no anchor href in the SSR table).
    assert hits[0]["url"] == "https://www.lcsc.com/product-detail/C8734.html"
    assert hits[0]["site"] == "lcsc"
    assert hits[0]["lcsc"] == "C8734"
    # Price/stock are not associated from the fragile results table; acquire() reads them.
    assert hits[0]["price"] is None
    assert hits[0]["stock"] is None


def test_lcsc_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": LCSC_LOGIN_WALL_HTML})

    hits = lcsc.ADAPTER.search("stm32")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_lcsc_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = lcsc.ADAPTER.search("stm32")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_lcsc_acquire_returns_structured_record_with_datasheet(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, lcsc, _digikey_web, _snapeda, _content3d = adapter_mods

    def fake_download(url, site, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PDF-BYTES")
        return {"ok": True, "dest": str(dest), "session_profile_used": "lcsc"}

    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": LCSC_DETAIL_HTML})
    monkeypatch.setattr(session, "browser_download", fake_download)

    result = lcsc.ADAPTER.acquire(
        {"url": "https://www.lcsc.com/product-detail/STM32F103C8T6_C8734.html"})

    assert result["ok"] is True
    assert "license_note" in result and result["license_note"]
    assert result["lcsc"] == "C8734"
    assert result["price"] == "$2.10"
    assert result["stock"] == "12,000"
    assert result["datasheet_url"] == "https://www.lcsc.com/datasheet/STM32F103C8T6.pdf?v=2"
    assert result["artifact_path"].endswith("STM32F103C8T6.pdf")


def test_lcsc_acquire_datasheet_download_failure_degrades_to_record(adapter_mods, monkeypatch):
    """A failed datasheet download must not fail the structured record."""
    _adapters_pkg, session, _misumi, lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": LCSC_DETAIL_HTML})
    monkeypatch.setattr(session, "browser_download",
                        lambda url, site, dest: {"ok": False, "reason": "no_profile", "hint": "x"})

    result = lcsc.ADAPTER.acquire(
        {"url": "https://www.lcsc.com/product-detail/STM32F103C8T6_C8734.html"})

    assert result["ok"] is True
    assert result.get("artifact_path") is None
    assert result["lcsc"] == "C8734"


def test_lcsc_acquire_login_wall(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": LCSC_LOGIN_WALL_HTML})

    result = lcsc.ADAPTER.acquire({"url": "https://www.lcsc.com/product-detail/x.html"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_lcsc_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _session, _misumi, lcsc, _digikey_web, _snapeda, _content3d = adapter_mods
    result = lcsc.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


# --- DigiKey (website fallback) ---------------------------------------------------

def test_digikey_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": DIGIKEY_SEARCH_HTML})

    hits = digikey_web.ADAPTER.search("resistor")

    assert len(hits) == 1
    assert hits[0]["title"] == "Chip Resistor - Surface Mount"
    assert hits[0]["url"] == ("https://www.digikey.com/en/products/filter/"
                              "chip-resistor-surface-mount/52")
    assert hits[0]["site"] == "digikey"
    assert hits[0]["kind"] == "category"
    assert hits[0]["parent_category"] == "Resistors"
    assert hits[0]["category_id"] == "52"


def test_digikey_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": DIGIKEY_LOGIN_WALL_HTML})

    hits = digikey_web.ADAPTER.search("stm32")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_digikey_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = digikey_web.ADAPTER.search("stm32")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_digikey_acquire_returns_structured_record_with_links(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": DIGIKEY_DETAIL_HTML})

    result = digikey_web.ADAPTER.acquire(
        {"url": "https://www.digikey.com/en/products/detail/stmicroelectronics/STM32F103C8T6/1646338"})

    assert result["ok"] is True
    assert "license_note" in result and result["license_note"]
    assert result.get("artifact_path") is None
    assert result["digikey_pn"] == "497-6063-ND"
    assert result["price"] == "$3.75"
    assert result["stock"] == "8,200"
    assert result["datasheet_url"] == "https://www.st.com/resource/en/datasheet/stm32f103c8.pdf"
    assert result["ecad_url"].startswith("https://www.snapeda.com/parts/STM32F103C8T6")


def test_digikey_acquire_login_wall(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, digikey_web, _snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": DIGIKEY_LOGIN_WALL_HTML})

    result = digikey_web.ADAPTER.acquire({"url": "https://www.digikey.com/en/products/detail/x"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_digikey_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _session, _misumi, _lcsc, digikey_web, _snapeda, _content3d = adapter_mods
    result = digikey_web.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


# --- SnapEDA --------------------------------------------------------------------

def test_snapeda_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": SNAPEDA_SEARCH_HTML})

    hits = snapeda.ADAPTER.search("stm32f103c8t6")

    assert len(hits) == 1
    assert "STM32F103C8T6" in hits[0]["title"]
    assert hits[0]["url"] == ("https://www.snapeda.com/parts/STM32F103C8T6/"
                              "STMicroelectronics/view-part/")
    assert hits[0]["site"] == "snapeda"
    assert hits[0]["manufacturer"] == "STMicroelectronics"


def test_snapeda_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": SNAPEDA_LOGIN_WALL_HTML})

    hits = snapeda.ADAPTER.search("stm32")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_snapeda_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = snapeda.ADAPTER.search("stm32")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_snapeda_acquire_downloads_and_returns_provenance(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, snapeda, _content3d = adapter_mods

    def fake_fetch(url, site):
        return {"ok": True, "content": SNAPEDA_DETAIL_HTML}

    def fake_download(url, site, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ZIP-BYTES")
        return {"ok": True, "dest": str(dest), "session_profile_used": "snapeda"}

    monkeypatch.setattr(session, "browser_fetch", fake_fetch)
    monkeypatch.setattr(session, "browser_download", fake_download)

    result = snapeda.ADAPTER.acquire(
        {"title": "STM32F103C8T6",
         "url": "https://www.snapeda.com/parts/STM32F103C8T6/STMicroelectronics/view-part/"})

    assert result["ok"] is True
    assert result["session_profile_used"] == "snapeda"
    assert "license_note" in result and result["license_note"]
    assert result["artifact_path"].endswith("STM32F103C8T6-kicad.zip")
    assert result["manufacturer"] == "STMicroelectronics"


def test_snapeda_acquire_login_wall(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": SNAPEDA_LOGIN_WALL_HTML})

    result = snapeda.ADAPTER.acquire({"url": "https://www.snapeda.com/parts/x/y/view-part/"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_snapeda_acquire_no_download_link(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, snapeda, _content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": "<html>nothing here</html>"})

    result = snapeda.ADAPTER.acquire({"url": "https://www.snapeda.com/parts/x/y/view-part/"})
    assert result["ok"] is False
    assert result["reason"] == "no_download_link"


def test_snapeda_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _session, _misumi, _lcsc, _digikey_web, snapeda, _content3d = adapter_mods
    result = snapeda.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


# --- 3D ContentCentral ------------------------------------------------------------

def test_content3d_search_parses_result_cards(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, _snapeda, content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": CONTENT3D_SEARCH_HTML})

    hits = content3d.ADAPTER.search("608zz bearing")

    assert len(hits) == 1
    assert "608ZZ Bearing" in hits[0]["title"]
    assert hits[0]["url"] == "https://www.3dcontentcentral.com/parts/supplier/widget-bearing-608zz"
    assert hits[0]["site"] == "3dcontentcentral"
    assert hits[0]["supplier"] == "SKF"


def test_content3d_search_login_wall_surfaces_ask_user(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, _snapeda, content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": CONTENT3D_LOGIN_WALL_HTML})

    hits = content3d.ADAPTER.search("bearing")

    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"


def test_content3d_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, _snapeda, content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = content3d.ADAPTER.search("bearing")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


def test_content3d_acquire_downloads_and_returns_provenance(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, _snapeda, content3d = adapter_mods

    def fake_fetch(url, site):
        return {"ok": True, "content": CONTENT3D_DETAIL_HTML}

    def fake_download(url, site, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"STEP-FILE-BYTES")
        return {"ok": True, "dest": str(dest), "session_profile_used": "3dcontentcentral"}

    monkeypatch.setattr(session, "browser_fetch", fake_fetch)
    monkeypatch.setattr(session, "browser_download", fake_download)

    result = content3d.ADAPTER.acquire(
        {"title": "608ZZ Bearing",
         "url": "https://www.3dcontentcentral.com/parts/supplier/widget-bearing-608zz"})

    assert result["ok"] is True
    assert result["session_profile_used"] == "3dcontentcentral"
    assert "license_note" in result and result["license_note"]
    assert result["artifact_path"].endswith("608zz.step")
    assert result["supplier"] == "SKF"


def test_content3d_acquire_login_wall(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, _snapeda, content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": CONTENT3D_LOGIN_WALL_HTML})

    result = content3d.ADAPTER.acquire(
        {"url": "https://www.3dcontentcentral.com/parts/supplier/x"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_content3d_acquire_no_download_link(adapter_mods, monkeypatch):
    _adapters_pkg, session, _misumi, _lcsc, _digikey_web, _snapeda, content3d = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": "<html>nothing here</html>"})

    result = content3d.ADAPTER.acquire(
        {"url": "https://www.3dcontentcentral.com/parts/supplier/x"})
    assert result["ok"] is False
    assert result["reason"] == "no_download_link"


def test_content3d_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _session, _misumi, _lcsc, _digikey_web, _snapeda, content3d = adapter_mods
    result = content3d.ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"
