"""Offline search contract fixtures for servocity."""
import json

from services.browser import session
from services.browser.adapters import servocity as adapter_module


def test_stencil_search_contract(monkeypatch):
    adapter = adapter_module.ADAPTER
    urls = []
    html = ('<li class="product"><a class="card" href="/part/123" title="Fixture part">'
            '<img data-src="/images/part.jpg"><span class="price price--withoutTax">$12.50</span>'
            '</a></li>')
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: urls.append(url) or
                        {"ok": True, "content": html})
    hit = adapter.search("M3 & #1")[0]
    assert hit["kind"] == "product"
    assert hit["price"] == 12.50 and hit["currency"] == "USD"
    assert hit["thumbnail"].endswith("/images/part.jpg")
    assert "%26" in urls[0] and "%23" in urls[0]


def test_wall_and_unparseable(monkeypatch):
    adapter = adapter_module.ADAPTER
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": adapter.challenge_markers[0]})
    assert adapter.search("bolt")[0]["reason"] == "blocked"
    monkeypatch.setattr(session, "browser_fetch", lambda url, site:
                        {"ok": True, "content": "<html>unrecognized</html>"})
    assert adapter.search("bolt")[0]["reason"] == "parse_failed"
