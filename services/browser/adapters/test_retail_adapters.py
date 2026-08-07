"""Tests for the retail adapter wave (Parts Library V2, workstream 2) —
homedepot, lowes, acehardware, grainger, fastenal, harborfreight,
tractorsupply, onlinemetals, metalsdepot — plus the shared ``_retail`` parsing
helpers and the ``services.browser.health`` canary module.

Same offline style as test_adapters.py / test_new_adapters.py: no real
Playwright, no real network — ``services.browser.session``'s
``browser_fetch``/``browser_download`` are monkeypatched directly.
"""
from __future__ import annotations

import importlib

import pytest

from services.browser.adapters._retail import (find_datasheet_url, normalize_machining_params,
                                               parse_jsonld_products, parse_spec_table,
                                               parse_stock_params)


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
    retail_mods = {
        "homedepot": homedepot, "lowes": lowes, "acehardware": acehardware,
        "grainger": grainger, "fastenal": fastenal, "harborfreight": harborfreight,
        "tractorsupply": tractorsupply, "onlinemetals": onlinemetals,
        "metalsdepot": metalsdepot,
    }
    yield adapters_pkg, session, retail_mods
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

def _jsonld_search_page(payload: str) -> str:
    return f'<html><body><script type="application/ld+json">{payload}</script></body></html>'


HOMEDEPOT_SEARCH_HTML = _jsonld_search_page("""
{
  "@context": "https://schema.org",
  "@type": "ItemList",
  "itemListElement": [
    {"@type": "ListItem", "position": 1, "item": {
      "@type": "Product",
      "name": "Deck-Mate #9 x 2-1/2 in. Star Flat-Head Wood Deck Screws (1 lb.)",
      "url": "https://www.homedepot.com/p/Deck-Mate-9-x-2-1-2-in/202097227",
      "sku": "202097227",
      "model": "34842",
      "image": "https://images.thdstatic.com/productImages/deckmate.jpg",
      "brand": {"@type": "Brand", "name": "Deck-Mate"},
      "offers": {"@type": "Offer", "price": "9.47", "priceCurrency": "USD",
                 "availability": "https://schema.org/InStock"}
    }}
  ]
}
""")

CHALLENGE_HTML = ('<html><head><title>Access Denied</title></head><body>'
                  "<h1>Access Denied</h1>You don't have permission to access this page. "
                  "Reference #18.9d4c1502.1719430000.1a2b3c</body></html>")

LOWES_SEARCH_JSONLD_HTML = _jsonld_search_page("""
[{"@type": "Product",
  "name": "2-in x 4-in x 8-ft Whitewood Kiln-Dried Stud",
  "url": "/pd/2x4x8-stud/1000068131",
  "sku": "1000068131",
  "offers": {"@type": "Offer", "price": 3.98, "priceCurrency": "USD"}}]
""")

LOWES_PRELOADED_HTML = """
<html><body><script>
window.__PRELOADED_STATE__ = {"itemList": [
  {"product": {"title": "2-in x 4-in x 8-ft Whitewood Stud",
               "pdURL": "/pd/2x4x8-stud/1000068131",
               "itemNumber": "1000068131",
               "price": 3.98,
               "imageUrl": "https://mobileimages.lowes.com/stud.jpg"}}
]};
</script></body></html>
"""

ACE_SEARCH_HTML = _jsonld_search_page("""
{"@context": "https://schema.org", "@graph": [
  {"@type": "Product", "name": "WD-40 Multi-Use Lubricant 11 oz",
   "url": "https://www.acehardware.com/departments/wd40-11oz/123456",
   "sku": "123456",
   "offers": {"@type": "Offer", "price": "6.99", "priceCurrency": "USD"}}
]}
""")

GRAINGER_SEARCH_HTML = _jsonld_search_page("""
{"@type": "Product",
 "name": "Square End Mill, 1/2 in Milling Dia, 4 Flutes, AlTiN",
 "url": "https://www.grainger.com/product/19LR45",
 "sku": "19LR45",
 "offers": {"@type": "Offer", "price": "58.30", "priceCurrency": "USD"}}
""")

GRAINGER_DETAIL_HTML = """
<html><body>
<script type="application/ld+json">
{"@type": "Product", "name": "Square End Mill 1/2 in",
 "url": "https://www.grainger.com/product/19LR45", "sku": "19LR45",
 "offers": {"@type": "Offer", "price": "58.30", "priceCurrency": "USD"}}
</script>
<table>
<tr><th>Milling Dia.</th><td>1/2"</td></tr>
<tr><th>Number of Flutes</th><td>4</td></tr>
<tr><th>Length of Cut</th><td>1"</td></tr>
<tr><th>Shank Diameter</th><td>1/2 in</td></tr>
<tr><th>Coating</th><td>AlTiN</td></tr>
<tr><th>Tool Material</th><td>Carbide</td></tr>
<tr><th>Max. RPM</th><td>12,000</td></tr>
</table>
<a href="/documents/19LR45_datasheet.pdf">Datasheet (PDF)</a>
</body></html>
"""

FASTENAL_SEARCH_HTML = _jsonld_search_page("""
{"@type": "Product", "name": "M8-1.25 x 30mm Class 8.8 Zinc Hex Cap Screw",
 "url": "https://www.fastenal.com/product/details/0142811",
 "sku": "0142811",
 "offers": {"@type": "Offer", "price": "0.21", "priceCurrency": "USD"}}
""")

FASTENAL_DETAIL_HTML = """
<html><body>
<dl>
<dt>Thread Size</dt><dd>M8-1.25</dd>
<dt>Grade</dt><dd>Class 8.8</dd>
<dt>Finish</dt><dd>Zinc Clear</dd>
</dl>
</body></html>
"""

HARBORFREIGHT_SEARCH_HTML = _jsonld_search_page("""
{"@type": "Product", "name": "6 in. Composite Digital Caliper",
 "url": "https://www.harborfreight.com/6-in-composite-digital-caliper-63586.html",
 "sku": "63586",
 "offers": {"@type": "Offer", "price": "12.99", "priceCurrency": "USD"}}
""")

TRACTORSUPPLY_SEARCH_HTML = _jsonld_search_page("""
{"@type": "Product", "name": "CountyLine Hitch Pin, 1/2 in. x 4 in.",
 "url": "https://www.tractorsupply.com/tsc/product/countyline-hitch-pin-12",
 "sku": "1005125",
 "offers": {"@type": "Offer", "price": "3.49", "priceCurrency": "USD"}}
""")

ONLINEMETALS_SEARCH_HTML = _jsonld_search_page("""
{"@type": "Product",
 "name": "6061-T6 Aluminum Round Tube 1\\" OD x 0.049\\" wall",
 "url": "https://www.onlinemetals.com/en/buy/aluminum-round-tube-6061-t6",
 "sku": "1109",
 "offers": {"@type": "Offer", "price": "8.42", "priceCurrency": "USD"}}
""")

METALSDEPOT_SEARCH_HTML = """
<html><body>
<div class="product product-row card-hover-popout">
  <h4><a href="/steel-products/steel-angle/a36-angle">
    6061-T6 Aluminum Angle 2" x 2" x 1/4" thick</a></h4>
  <div class="price"><span>$18.40</span></div>
</div>
</body></html>
"""


# ---------------------------------------------------------------------------
# _retail helper unit tests
# ---------------------------------------------------------------------------

def test_parse_jsonld_single_product():
    html = _jsonld_search_page(
        '{"@type": "Product", "name": "Widget", "url": "/p/widget", "sku": "W1",'
        ' "image": ["https://x/img.jpg"], "brand": {"name": "Acme"},'
        ' "offers": [{"price": 4.99, "priceCurrency": "USD",'
        ' "availability": "https://schema.org/InStock"}]}')
    products = parse_jsonld_products(html)
    assert len(products) == 1
    p = products[0]
    assert p["title"] == "Widget"
    assert p["url"] == "/p/widget"
    assert p["price"] == "4.99"
    assert p["currency"] == "USD"
    assert p["sku"] == "W1"
    assert p["image"] == "https://x/img.jpg"
    assert p["availability"] == "https://schema.org/InStock"
    assert p["brand"] == "Acme"


def test_parse_jsonld_graph_and_itemlist_variants():
    assert len(parse_jsonld_products(HOMEDEPOT_SEARCH_HTML)) == 1   # ItemList/ListItem/item
    assert len(parse_jsonld_products(ACE_SEARCH_HTML)) == 1         # @graph
    assert len(parse_jsonld_products(LOWES_SEARCH_JSONLD_HTML)) == 1  # top-level array


def test_parse_jsonld_skips_malformed_blocks():
    html = ('<script type="application/ld+json">{not json at all</script>'
            + GRAINGER_SEARCH_HTML)
    products = parse_jsonld_products(html)
    assert len(products) == 1
    assert products[0]["sku"] == "19LR45"


def test_parse_jsonld_ignores_non_products():
    html = _jsonld_search_page('{"@type": "BreadcrumbList", "name": "crumbs"}')
    assert parse_jsonld_products(html) == []


def test_parse_spec_table_th_td_and_dt_dd():
    spec = parse_spec_table(GRAINGER_DETAIL_HTML)
    assert spec["milling_dia"] == '1/2"'
    assert spec["number_of_flutes"] == "4"
    assert spec["coating"] == "AlTiN"
    spec2 = parse_spec_table(FASTENAL_DETAIL_HTML)
    assert spec2["thread_size"] == "M8-1.25"
    assert spec2["grade"] == "Class 8.8"
    assert spec2["finish"] == "Zinc Clear"


def test_normalize_machining_params_end_mill():
    spec = parse_spec_table(GRAINGER_DETAIL_HTML)
    params = normalize_machining_params(spec)
    assert params["tool_diameter_mm"] == pytest.approx(12.7)
    assert params["flutes"] == 4
    assert params["loc_mm"] == pytest.approx(25.4)
    assert params["shank_diameter_mm"] == pytest.approx(12.7)
    assert params["coating"] == "AlTiN"
    assert params["material"] == "Carbide"
    assert params["max_rpm"] == 12000


def test_normalize_machining_params_only_emits_found_keys():
    params = normalize_machining_params({"coating": "TiN"})
    assert params == {"coating": "TiN"}
    assert normalize_machining_params({}) == {}


def test_normalize_machining_params_metric_passthrough():
    params = normalize_machining_params({"cutting_diameter": "6 mm",
                                         "flute_length": "20mm"})
    assert params["tool_diameter_mm"] == pytest.approx(6.0)
    assert params["loc_mm"] == pytest.approx(20.0)


def test_parse_stock_params_from_onlinemetals_title():
    params = parse_stock_params('6061-T6 Aluminum Round Tube 1" OD x 0.049" wall')
    assert params["alloy"] == "6061-T6"
    assert params["profile"] == "round_tube"
    assert params["od_mm"] == pytest.approx(25.4)
    assert params["wall_mm"] == pytest.approx(1.2446)


def test_parse_stock_params_angle_thickness():
    params = parse_stock_params('A36 Steel Angle 2" x 2" x 1/4" thick')
    assert params["alloy"] == "A36"
    assert params["profile"] == "angle"
    assert params["thickness_mm"] == pytest.approx(6.35)


def test_find_datasheet_url_matches_pdf_near_keyword():
    url = find_datasheet_url(GRAINGER_DETAIL_HTML, "https://www.grainger.com")
    assert url == "https://www.grainger.com/documents/19LR45_datasheet.pdf"
    # non-pdf and keyword-less links never match
    assert find_datasheet_url('<a href="/x.pdf">random link</a>', "https://x") is None
    assert find_datasheet_url('<a href="/manual.html">Manual</a>', "https://x") is None


# ---------------------------------------------------------------------------
# adapter registry
# ---------------------------------------------------------------------------

RETAIL_SITES = ("homedepot", "lowes", "acehardware", "grainger", "fastenal",
                "harborfreight", "tractorsupply", "onlinemetals", "metalsdepot")


def test_registry_has_all_retail_adapters(adapter_mods):
    adapters_pkg, _session, retail_mods = adapter_mods
    keys = {a.key for a in adapters_pkg.all_adapters()}
    assert set(RETAIL_SITES) <= keys
    for site in RETAIL_SITES:
        assert adapters_pkg.get_adapter(site) is retail_mods[site].ADAPTER


# ---------------------------------------------------------------------------
# per-adapter search parsing
# ---------------------------------------------------------------------------

_SEARCH_CASES = {
    "homedepot": (HOMEDEPOT_SEARCH_HTML, "Deck Screws", "202097227"),
    "lowes": (LOWES_SEARCH_JSONLD_HTML, "Whitewood", "1000068131"),
    "acehardware": (ACE_SEARCH_HTML, "WD-40", "123456"),
    "grainger": (GRAINGER_SEARCH_HTML, "End Mill", "19LR45"),
    "fastenal": (FASTENAL_SEARCH_HTML, "Hex Cap Screw", "0142811"),
    "harborfreight": (HARBORFREIGHT_SEARCH_HTML, "Digital Caliper", "63586"),
    "tractorsupply": (TRACTORSUPPLY_SEARCH_HTML, "Hitch Pin", "1005125"),
    "onlinemetals": (ONLINEMETALS_SEARCH_HTML, "Round Tube", "1109"),
}


@pytest.mark.parametrize("site", sorted(_SEARCH_CASES))
def test_retail_search_parses_jsonld(adapter_mods, monkeypatch, site):
    _adapters_pkg, session, retail_mods = adapter_mods
    html, title_word, sku = _SEARCH_CASES[site]
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": html,
                                        "session_profile_used": s})

    hits = retail_mods[site].ADAPTER.search("canary query")

    assert len(hits) == 1
    hit = hits[0]
    assert hit["ok"] is True
    assert title_word.lower() in hit["title"].lower()
    assert hit["url"].startswith("https://")
    assert hit["site"] == site
    assert hit["sku"] == sku
    assert hit["price"]


def test_metalsdepot_search_parses_current_product_cards(adapter_mods, monkeypatch):
    _adapters_pkg, session, retail_mods = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": METALSDEPOT_SEARCH_HTML})

    hits = retail_mods["metalsdepot"].ADAPTER.search("aluminum angle")

    assert len(hits) == 1
    assert "6061-T6 Aluminum Angle" in hits[0]["title"]
    assert hits[0]["url"] == "https://www.metalsdepot.com/steel-products/steel-angle/a36-angle"
    assert hits[0]["site"] == "metalsdepot"
    assert hits[0]["price"] == "$18.40"


def test_lowes_search_falls_back_to_preloaded_state(adapter_mods, monkeypatch):
    _adapters_pkg, session, retail_mods = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": LOWES_PRELOADED_HTML})

    hits = retail_mods["lowes"].ADAPTER.search("2x4 lumber")

    assert len(hits) == 1
    assert "Whitewood Stud" in hits[0]["title"]
    assert hits[0]["url"] == "https://www.lowes.com/pd/2x4x8-stud/1000068131"
    assert hits[0]["sku"] == "1000068131"
    assert hits[0]["price"] == "3.98"
    assert hits[0]["thumbnail"].endswith("stud.jpg")


@pytest.mark.parametrize("site", ("homedepot", "lowes"))
def test_akamai_challenge_degrades_to_login_required(adapter_mods, monkeypatch, site):
    """Bot walls are NEVER solved — they surface as the login_required ask."""
    _adapters_pkg, session, retail_mods = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": CHALLENGE_HTML})

    hits = retail_mods[site].ADAPTER.search("anything")
    assert len(hits) == 1
    assert hits[0]["ok"] is False
    assert hits[0]["reason"] == "login_required"
    assert hits[0]["ask_user"]["id"].startswith("browser_login")

    result = retail_mods[site].ADAPTER.acquire({"url": "https://example.com/p/1"})
    assert result["ok"] is False
    assert result["reason"] == "login_required"


def test_retail_search_propagates_fetch_failure(adapter_mods, monkeypatch):
    _adapters_pkg, session, retail_mods = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = retail_mods["grainger"].ADAPTER.search("end mill")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]


# ---------------------------------------------------------------------------
# acquire: spec / params / datasheet
# ---------------------------------------------------------------------------

def test_grainger_acquire_end_mill_params_and_datasheet(adapter_mods, monkeypatch):
    _adapters_pkg, session, retail_mods = adapter_mods

    def fake_download(url, site, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"PDF-BYTES")
        return {"ok": True, "dest": str(dest), "session_profile_used": "grainger"}

    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": GRAINGER_DETAIL_HTML,
                                        "session_profile_used": "grainger"})
    monkeypatch.setattr(session, "browser_download", fake_download)

    result = retail_mods["grainger"].ADAPTER.acquire(
        {"title": "Square End Mill 1/2 in", "url": "https://www.grainger.com/product/19LR45"})

    assert result["ok"] is True
    assert result["source_url"] == "https://www.grainger.com/product/19LR45"
    assert "license_note" in result and result["license_note"]
    assert result["sku"] == "19LR45"
    assert result["price"] == "58.30"
    assert result["spec"]["milling_dia"] == '1/2"'
    assert result["params"]["tool_diameter_mm"] == pytest.approx(12.7)
    assert result["params"]["flutes"] == 4
    assert result["params"]["coating"] == "AlTiN"
    assert result["datasheet_url"].endswith("19LR45_datasheet.pdf")
    assert result["datasheet_path"].endswith("19LR45_datasheet.pdf")
    assert result["artifact_path"] == result["datasheet_path"]


def test_grainger_acquire_datasheet_failure_degrades_to_record(adapter_mods, monkeypatch):
    _adapters_pkg, session, retail_mods = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": GRAINGER_DETAIL_HTML})
    monkeypatch.setattr(session, "browser_download",
                        lambda url, site, dest: {"ok": False, "reason": "no_profile", "hint": "x"})

    result = retail_mods["grainger"].ADAPTER.acquire(
        {"url": "https://www.grainger.com/product/19LR45"})

    assert result["ok"] is True
    assert result["datasheet_path"] is None
    assert result.get("artifact_path") is None
    assert result["params"]["tool_diameter_mm"] == pytest.approx(12.7)


def test_fastenal_acquire_surfaces_spec_table(adapter_mods, monkeypatch):
    _adapters_pkg, session, retail_mods = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": FASTENAL_DETAIL_HTML})

    result = retail_mods["fastenal"].ADAPTER.acquire(
        {"title": "M8 Hex Cap Screw", "url": "https://www.fastenal.com/product/details/0142811"})

    assert result["ok"] is True
    assert result["spec"] == {"thread_size": "M8-1.25", "grade": "Class 8.8",
                              "finish": "Zinc Clear"}


def test_onlinemetals_acquire_derives_stock_params(adapter_mods, monkeypatch):
    _adapters_pkg, session, retail_mods = adapter_mods
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": "<html><body>plain</body></html>"})

    result = retail_mods["onlinemetals"].ADAPTER.acquire(
        {"title": '6061-T6 Aluminum Round Tube 1" OD x 0.049" wall',
         "url": "https://www.onlinemetals.com/en/buy/aluminum-round-tube-6061-t6"})

    assert result["ok"] is True
    assert result["params"]["alloy"] == "6061-T6"
    assert result["params"]["profile"] == "round_tube"
    assert result["params"]["od_mm"] == pytest.approx(25.4)
    assert result["params"]["wall_mm"] == pytest.approx(1.2446)


def test_retail_acquire_rejects_missing_url(adapter_mods):
    _adapters_pkg, _session, retail_mods = adapter_mods
    result = retail_mods["homedepot"].ADAPTER.acquire({"title": "no url here"})
    assert result["ok"] is False
    assert result["reason"] == "bad_result"


# ---------------------------------------------------------------------------
# health module
# ---------------------------------------------------------------------------

def test_health_canaries_cover_every_registered_site(adapter_mods):
    adapters_pkg, _session, _retail_mods = adapter_mods
    import services.browser.health as health
    importlib.reload(health)
    registered = {a.key for a in adapters_pkg.all_adapters()}
    assert registered <= set(health.CANARY_QUERIES), (
        f"canary missing for: {registered - set(health.CANARY_QUERIES)}")


def test_health_check_result_shape_and_classification(adapter_mods, monkeypatch):
    _adapters_pkg, session, _retail_mods = adapter_mods
    import services.browser.health as health
    importlib.reload(health)

    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": GRAINGER_SEARCH_HTML})
    results = health.run_health_check(["grainger"])
    assert len(results) == 1
    r = results[0]
    assert set(r) >= {"site", "ok", "latency_ms", "logged_in", "hit_count", "error"}
    assert r["site"] == "grainger"
    assert r["ok"] is True
    assert r["hit_count"] == 1
    assert isinstance(r["latency_ms"], float)
    assert r["error"] is None

    # login wall → expected failure, never a crash
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": CHALLENGE_HTML})
    r = health.run_health_check(["homedepot"])[0]
    assert r["ok"] is False
    assert r["error"] == "login_required"
    assert r["expected"] is True

    # exception → captured, never raised
    def boom(url, s):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(session, "browser_fetch", boom)
    r = health.run_health_check(["fastenal"])[0]
    assert r["ok"] is False
    assert "kaboom" in r["error"]

    # unknown site → structured error
    r = health.run_health_check(["not-a-site"])[0]
    assert r["ok"] is False
    assert "no adapter" in r["error"]
