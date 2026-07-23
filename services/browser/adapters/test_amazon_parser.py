"""Tests for the Amazon adapter's search() parser — reverse-engineered against a
REAL fetched search page (https://www.amazon.com/s?k=m3+screw, HTTP 200, no
captcha, saved 2026-07-07).

Unlike the speculative fixtures in test_adapters.py, the ``AMAZON_SEARCH_HTML``
fixture below embeds VERBATIM (byte-for-byte) slices of that saved page: two
real result cards (one sponsored, one organic) — the full opening
``<div … data-asin=… data-component-type="s-search-result">`` tag, the full
``s-line-clamp`` title anchor (real /sspa/click and /…/dp/ hrefs and all), and
the ``<h2>…<span>TITLE</span></h2></a>`` block, all copied unmodified. Only each
card's inner ``a-price`` presentation sub-tree (the ``aria-hidden`` duplicate)
is trimmed after the real ``<span class="a-offscreen">$X.YZ</span>``; the
``a-price``/``a-offscreen`` wrapper itself is verbatim. A third card
(``_CARD_NO_PRICE``) is a CONSTRUCTED edge case — NOT a real slice — carrying a
title but no price span, to exercise leak-protection (the real page had 0
priceless cards, so live data cannot exercise it).

No real Playwright, no real network: ``services.browser.session.browser_fetch``
is monkeypatched (adapters call it as ``_session.browser_fetch(url, site)``, so
patching the session attribute is enough). Mirrors the McMaster test structure.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def amazon_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import amazon
    importlib.reload(amazon)
    yield amazon, session


# --- VERBATIM byte-for-byte slices of the real saved amazon.html (2026-07-07) -
# Card 1: SPONSORED result, ASIN B0G7HV64L6. The title anchor href is a real
# /sspa/click tracking redirect (verbatim) — the parser must ignore it and build
# the canonical /dp/<ASIN> URL instead. Only the inner aria-hidden price sub-tree
# is trimmed after the real a-offscreen span.
_CARD_SPONSORED = (
    '<div role="listitem" data-asin="B0G7HV64L6" data-index="3" data-uuid="2d0b1446-fd25-4ee2-a0bd-404da1bf7679" id="2d0b1446-fd25-4ee2-a0bd-404da1bf7679" data-component-type="s-search-result" class="sg-col-20-of-24 s-result-item s-asin sg-col-0-of-12 sg-col-16-of-20 AdHolder sg-col s-widget-spacing-small gsx-ies-anchor sg-col-12-of-16" data-component-id="14" data-cel-widget="search_result_2">'
    '<a class="a-link-normal s-line-clamp-2 puis-line-clamp-3-for-col-4-and-8 s-link-style a-text-normal" href="/sspa/click?ie=UTF8&amp;spc=MTo1MjgzNDIxOTcwNTkzNzkxOjE3ODM0NDQ2Mjc6c3BfYXRmOjMwMTE5Njc2ODk1MjcwMjo6MDo6&amp;url=%2F1000Pcs-Screws-Assortment-Grade-Printer%2Fdp%2FB0G7HV64L6%2Fref%3Dsr_1_1_sspa%3Fdib%3DeyJ2IjoiMSJ9.cuBJ5G2d98fh1Tef_3PCuEvSr0_7MqTb3URx4CtWOZf20xKXkIRepqRKKuRqmiqFvCZQres3WBNgkvhZuad3IpqeOVJ7OY7Wyuco8EbpBraHrYwOWAVB5X2ym1eUzYZ2xRfo5QxrINHWpZaOCjjbZ0AxpS3ZXi_exze6VSS7hiGoX-XVrpFUKKfkuACQk59t83ydORAPjCdvIuyJJ27I6T60W1QVK8q7pRln-YrVIuQ.5JzjFhpGFNmQTOtoEYgh8YR02AnuXAKCeXCD-SLOxjk%26dib_tag%3Dse%26keywords%3Dm3%2Bscrew%26qid%3D1783444627%26sr%3D8-1-spons%26sp_csd%3Dd2lkZ2V0TmFtZT1zcF9hdGY%26psc%3D1"><h2 aria-label="Sponsored Ad - 1000Pcs M3 Screws Assortment Kit, 12.9 Grade Hex Bolts Set for 3D Printer | M3x6/8/10/12/16/20/25/30mm,12.9 Grade Black Zinc Plated" class="a-size-medium a-spacing-none a-color-base a-text-normal"><span>1000Pcs M3 Screws Assortment Kit, 12.9 Grade Hex Bolts Set for 3D Printer | M3x6/8/10/12/16/20/25/30mm,12.9 Grade Black Zinc Plated</span></h2></a>'
    '<span class="a-price" data-a-size="xl" data-a-color="base"><span class="a-offscreen">$8.99</span></span>'
    '</div>'
)

# Card 2: ORGANIC result, ASIN B0FD9SD33X. Plain /…/dp/<ASIN>/ href (verbatim);
# price is $6.49.
_CARD_ORGANIC = (
    '<div role="listitem" data-asin="B0FD9SD33X" data-index="12" data-uuid="2b98b8ea-888a-4ee8-a35b-55072e17c270" id="2b98b8ea-888a-4ee8-a35b-55072e17c270" data-component-type="s-search-result" class="sg-col-20-of-24 s-result-item s-asin sg-col-0-of-12 sg-col-16-of-20 sg-col s-widget-spacing-small gsx-ies-anchor sg-col-12-of-16" data-component-id="40" data-cel-widget="search_result_14">'
    '<a class="a-link-normal s-line-clamp-2 puis-line-clamp-3-for-col-4-and-8 s-link-style a-text-normal" href="/Machine-Screws-Countersunk-Phillips-Assortment/dp/B0FD9SD33X/ref=sr_1_6?dib=eyJ2IjoiMSJ9.cuBJ5G2d98fh1Tef_3PCuEvSr0_7MqTb3URx4CtWOZf20xKXkIRepqRKKuRqmiqFvCZQres3WBNgkvhZuad3IpqeOVJ7OY7Wyuco8EbpBraHrYwOWAVB5X2ym1eUzYZ2xRfo5QxrINHWpZaOCjjbZ0AxpS3ZXi_exze6VSS7hiGoX-XVrpFUKKfkuACQk59t83ydORAPjCdvIuyJJ27I6T60W1QVK8q7pRln-YrVIuQ.5JzjFhpGFNmQTOtoEYgh8YR02AnuXAKCeXCD-SLOxjk&amp;dib_tag=se&amp;keywords=m3+screw&amp;qid=1783444627&amp;sr=8-6"><h2 aria-label="380 Pcs M3 Black Flat Head Machine Screws and Hex Nuts Kit Carbon Steel Countersunk Head Phillips Screws Bolts Assortment,M3 x 4mm/ 6mm/ 8mm/ 10mm/ 12mm/ 16mm/ 20mm" class="a-size-medium a-spacing-none a-color-base a-text-normal"><span>380 Pcs M3 Black Flat Head Machine Screws and Hex Nuts Kit Carbon Steel Countersunk Head Phillips Screws Bolts Assortment,M3 x 4mm/ 6mm/ 8mm/ 10mm/ 12mm/ 16mm/ 20mm</span></h2></a>'
    '<span class="a-price" data-a-size="xl" data-a-color="base"><span class="a-offscreen">$6.49</span></span>'
    '</div>'
)

# Card 3: CONSTRUCTED edge case (NOT a real slice). A real-shaped card with a
# title but NO price span, proving the parser yields price=None here rather than
# borrowing card 2's $6.49. The real page had 0 priceless cards, so this is the
# only way to exercise leak-protection.
_CARD_NO_PRICE = (
    '<div role="listitem" data-asin="B0FQ3XXN8F" data-index="20" '
    'data-component-type="s-search-result" class="sg-col-20-of-24 s-result-item s-asin">'
    '<a class="a-link-normal s-line-clamp-2 puis-line-clamp-3-for-col-4-and-8 s-link-style a-text-normal" '
    'href="/Kernmax-Countersunk/dp/B0FQ3XXN8F/ref=sr_1_9">'
    '<h2 class="a-size-medium a-spacing-none a-color-base a-text-normal">'
    '<span>Kernmax M3 Countersunk Phillips Screws for Electronics</span></h2></a></div>'
)

AMAZON_SEARCH_HTML = (
    '<div class="s-main-slot s-result-list">'
    + _CARD_SPONSORED + _CARD_ORGANIC + _CARD_NO_PRICE
    + '</div>'
)


def test_amazon_search_parses_real_cards(amazon_mod, monkeypatch):
    amazon, session = amazon_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: {"ok": True, "content": AMAZON_SEARCH_HTML})

    hits = amazon.ADAPTER.search("m3 screw")

    assert len(hits) == 3

    # Card 1 — sponsored: canonical /dp/<ASIN> URL, NOT the /sspa/click href.
    assert "1000Pcs M3 Screws Assortment Kit" in hits[0]["title"]
    assert hits[0]["url"] == "https://www.amazon.com/dp/B0G7HV64L6"
    assert hits[0]["site"] == "amazon"
    assert hits[0]["sku"] == "B0G7HV64L6"
    assert hits[0]["asin"] == "B0G7HV64L6"
    assert hits[0]["price"] == "$8.99"

    # Card 2 — organic.
    assert "380 Pcs M3 Black Flat Head Machine Screws" in hits[1]["title"]
    assert hits[1]["url"] == "https://www.amazon.com/dp/B0FD9SD33X"
    assert hits[1]["sku"] == "B0FD9SD33X"
    assert hits[1]["price"] == "$6.49"

    # Card 3 — no price span: must be None, NOT borrowed from card 2.
    assert hits[2]["sku"] == "B0FQ3XXN8F"
    assert hits[2]["price"] is None


def test_amazon_search_propagates_fetch_failure(amazon_mod, monkeypatch):
    amazon, session = amazon_mod
    failure = {"ok": False, "reason": "fetch_error", "hint": "connection refused"}
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: failure)

    hits = amazon.ADAPTER.search("m3 screw")

    assert hits == [failure]
