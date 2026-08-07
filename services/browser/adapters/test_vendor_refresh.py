"""Regression coverage for vendor markup and routes live-verified in August 2026."""
from __future__ import annotations

import importlib

import pytest

from services.browser import session


@pytest.mark.parametrize(
    ("module_name", "query", "content", "title", "url", "request_fragment"),
    [
        (
            "parker",
            "hydraulic valve",
            '<a class="nav-link-text" href="/us/en/product/valve">'
            '<div class="SelectorContainerInner-1">Parker D1VW Valve</div></a>',
            "Parker D1VW Valve",
            "https://www.parker.com/us/en/product/valve",
            "/us/en/search?searchbox=hydraulic+valve&selectCategory=products",
        ),
        (
            "hydraforce",
            "solenoid valve",
            '<h4 class="result-title">SV07-35</h4>'
            '<div class="result-links"><a href="/products/sv07-35/?src=search">View</a></div>',
            "SV07-35",
            "https://www.hydraforce.com/products/sv07-35/",
            "/search/?q=solenoid+valve",
        ),
        (
            "boschrexroth",
            "linear guide",
            '<a class="item-headline" href="/en/us/product/runner-block/">Ball Rail Runner Block</a>',
            "Ball Rail Runner Block",
            "https://www.boschrexroth.com/en/us/product/runner-block/",
            "/en/us/search?q=linear+guide",
        ),
        (
            "thomson",
            "linear actuator",
            '<a href="/en/product/pc-series" class="product-box__title">PC-Series Actuator</a>',
            "PC-Series Actuator",
            "https://www.thomsonlinear.com/en/product/pc-series",
            "/en/search?q=linear+actuator",
        ),
        (
            "gbstandards",
            "GB/T 70.1",
            '<a onclick="showInfo(\'A1B2C3\');">GB/T 70.1-2008</a>'
            '<td class="mytxt"><a>Hexagon socket head cap screws</a></td>',
            "GB/T 70.1-2008 — Hexagon socket head cap screws",
            "http://openstd.samr.gov.cn/bzgk/gb/newGbInfo?hcno=A1B2C3",
            "/std_list?p.p2=GB/T+70.1",
        ),
        (
            "pcbway",
            "esp32",
            '<a href="/project/shareproject/ESP32_Controller.html?from=search" '
            'class="info-title">ESP32 Controller</a>',
            "ESP32 Controller",
            "https://www.pcbway.com/project/shareproject/ESP32_Controller.html",
            "/project/projectsearch?q=esp32&t=1",
        ),
        (
            "professionalplastics",
            "acrylic sheet",
            '<a href="/AcrylicSheet?campaign=search" class="standLink">Acrylic Sheet</a>',
            "Acrylic Sheet",
            "https://www.professionalplastics.com/AcrylicSheet",
            "/cgi-bin/pp.pl?pgm=co_search&func=process&strfnbr=3&terms=acrylic%20sheet&type=1",
        ),
    ],
)
def test_current_vendor_search_markup(
    monkeypatch, module_name, query, content, title, url, request_fragment
):
    requested: list[tuple[str, str]] = []

    def fake_fetch(fetch_url: str, site: str) -> dict:
        requested.append((fetch_url, site))
        return {"ok": True, "content": content, "url": fetch_url}

    monkeypatch.setattr(session, "browser_fetch", fake_fetch)
    adapter = importlib.import_module(f"services.browser.adapters.{module_name}").ADAPTER

    hits = adapter.search(query)

    assert requested and request_fragment in requested[0][0]
    assert requested[0][1] == module_name
    assert hits and hits[0]["title"] == title
    assert hits[0]["url"] == url
    assert hits[0]["site"] == module_name


def test_futureelectronics_uses_same_origin_autocomplete(monkeypatch):
    requested: list[str] = []
    content = '<pre>{"suggestions":[{"term":"ATMEGA328P-AU"}]}</pre>'

    def fake_fetch(url: str, site: str) -> dict:
        requested.append(url)
        assert site == "futureelectronics"
        return {"ok": True, "content": content, "url": url}

    monkeypatch.setattr(session, "browser_fetch", fake_fetch)
    adapter = importlib.import_module(
        "services.browser.adapters.futureelectronics"
    ).ADAPTER

    hits = adapter.search("atmega328p")

    assert requested == [
        "https://www.futureelectronics.com/search/autocomplete/SearchBox_GC?term=atmega328p"
    ]
    assert hits == [
        {
            "ok": True,
            "title": "ATMEGA328P-AU",
            "sku": "ATMEGA328P-AU",
            "url": "https://www.futureelectronics.com/search?text=ATMEGA328P-AU",
            "site": "futureelectronics",
        }
    ]


def test_hiwin_filters_cached_public_sitemap(monkeypatch):
    requested: list[str] = []
    content = """
    <table>
      <tr><td><a href="https://hiwin.com/products/linear-guideways/">Linear Guideways</a></td></tr>
      <tr><td><a href="https://hiwin.com/company/distributors/">Distributors</a></td></tr>
    </table>
    """

    def fake_fetch(url: str, site: str) -> dict:
        requested.append(url)
        assert site == "hiwin"
        return {"ok": True, "content": content, "url": url}

    monkeypatch.setattr(session, "browser_fetch", fake_fetch)
    adapter = importlib.import_module("services.browser.adapters.hiwin").ADAPTER

    hits = adapter.search("linear guide")

    assert requested == ["https://hiwin.com/page-sitemap.xml"]
    assert hits == [
        {
            "title": "Linear Guideways",
            "url": "https://hiwin.com/products/linear-guideways/",
            "site": "hiwin",
            "manufacturer": "HIWIN",
        }
    ]
