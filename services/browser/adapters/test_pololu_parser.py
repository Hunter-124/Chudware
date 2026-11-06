"""Pololu search-result and document-acquisition fixtures; no network needed."""
from __future__ import annotations

import pytest


# Search rows and image URLs captured from the public encoder search. The third
# row exercises a mismatched image link, and the sidebar is not a search result.
POLOLU_SEARCH_HTML = '''
<html><body><a href="/account/login">Log In</a>
<dl id='search'>
<dt><a href="/product/5111"><img alt="75:1 Micro Metal Gearmotor LP 6V with 12 CPR Encoder, Side Connector" src="https://a.pololu-files.com/picture/0J12417.100.jpg?61cf841ab8d9f37214d5ef97f5eb6345" /></a>
<a href="/product/5111">Pololu item 5111: 75:1 Micro Metal Gearmotor LP 6V with 12 CPR <strong>Encoder</strong>, Side Connector</a></dt>
<dd>description</dd>
<dt><a href='/product/5110'><img src="https://a.pololu-files.com/picture/0J12415.100.jpg?db3264e9c34370e00c87132ede2e701a"></a>
<a href='/product/5110'>Pololu item 5110: 75:1 Micro Metal Gearmotor LP 6V with 12 CPR <strong>Encoder</strong>, Back Connector</a></dt>
<dt><a href="/product/999"><img src="/picture/unrelated.jpg"></a>
<a href="/product/42">Unnumbered product</a></dt>
</dl>
<div id="right_col"><a href="/product/5267">New product, not a match</a></div>
</body></html>
'''


@pytest.fixture()
def pololu_mod():
    from services.browser import session
    from services.browser.adapters import pololu
    return session, pololu


def test_pololu_card_images_and_unknown_fields(pololu_mod, monkeypatch):
    session, pololu = pololu_mod
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": POLOLU_SEARCH_HTML})
    hits = pololu.ADAPTER.search("encoder")
    assert len(hits) == 3
    first, second, third = hits
    assert first["site"] == "pololu" and first["kind"] == "product"
    assert first["title"] == "75:1 Micro Metal Gearmotor LP 6V with 12 CPR Encoder, Side Connector"
    assert first["url"] == "https://www.pololu.com/product/5111" and first["sku"] == "5111"
    assert first["thumbnail"] == "https://a.pololu-files.com/picture/0J12417.100.jpg?61cf841ab8d9f37214d5ef97f5eb6345"
    assert first["mpn"] is None and first["price"] is None and first["currency"] is None
    assert first["stock"] is None and first["model"] == {"available": None, "formats": []}
    assert second["sku"] == "5110"
    assert second["thumbnail"] == "https://a.pololu-files.com/picture/0J12415.100.jpg?db3264e9c34370e00c87132ede2e701a"
    assert third["sku"] is None and third["thumbnail"] is None


def test_pololu_capabilities_and_query_encoding(pololu_mod):
    _, pololu = pololu_mod
    assert pololu.ADAPTER.hosts == ("pololu.com",)
    assert pololu.ADAPTER.result_kinds == ("product",)
    assert not pololu.ADAPTER.cad_acquisition and not pololu.ADAPTER.login_required and not pololu.ADAPTER.api
    assert pololu.ADAPTER.search_url("motor & A/B #1") == (
        "https://www.pololu.com/search?query=motor+%26+A%2FB+%231")


def test_pololu_empty_does_not_return_sidebar_products(pololu_mod, monkeypatch):
    session, pololu = pololu_mod
    page = '<p>Sorry, your search returned no results</p><aside><a href="/product/42">Other</a></aside>'
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    assert pololu.ADAPTER.search("nothing") == []
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {
        "ok": True, "content": '<main>Unrecognized search markup</main><a href="/product/42">Other</a>'})
    failure = pololu.ADAPTER.search("motor")[0]
    assert failure["reason"] == "parse_failed" and failure["site"] == "pololu"


@pytest.mark.parametrize(("page", "reason"), [
    ("<h1>Access Denied</h1>", "blocked"),
    ("<h1>Verify you are human</h1>", "captcha"),
    ("<p>Please log in to continue</p>", "login_required"),
])
def test_pololu_wall_types(pololu_mod, monkeypatch, page, reason):
    session, pololu = pololu_mod
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {"ok": True, "content": page})
    failure = pololu.ADAPTER.search("motor")[0]
    assert failure["ok"] is False and failure["reason"] == reason
    assert failure["site"] == "pololu" and failure["op"] == "search"


def test_pololu_transport_failure(pololu_mod, monkeypatch):
    session, pololu = pololu_mod
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {
        "ok": False, "reason": "timeout", "status": None})
    failure = pololu.ADAPTER.search("motor")[0]
    assert failure["reason"] == "timeout" and failure["url"] == pololu.ADAPTER.search_url("motor")


def test_pololu_datasheet_is_a_document(pololu_mod, monkeypatch, tmp_path):
    session, pololu = pololu_mod
    from services.browser.adapters import _retail
    monkeypatch.setattr(session, "browser_fetch", lambda url, site: {
        "ok": True, "content": '<a href="/file/datasheet.pdf">Datasheet</a>'})
    dest = tmp_path / "datasheet.pdf"
    monkeypatch.setattr(_retail, "download_path", lambda *args, **kwargs: (dest, None))

    def download(url, site, path):
        path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return {"ok": True, "dest": str(path)}

    monkeypatch.setattr(session, "browser_download", download)
    outcome = pololu.ADAPTER.acquire({"title": "Motor", "url": pololu.BASE_URL + "/product/5111"})
    assert outcome["ok"] and outcome["artifact_path"] == str(dest)
    assert outcome["artifact_kind"] == "document" and outcome["format"] == "pdf"

    def download_html(url, site, path):
        path.write_bytes(b"<html>Sign in</html>")
        return {"ok": True, "dest": str(path)}

    monkeypatch.setattr(session, "browser_download", download_html)
    rejected = pololu.ADAPTER.acquire({"title": "Motor", "url": pololu.BASE_URL + "/product/5111"})
    assert not rejected["ok"] and rejected["reason"] == "parse_failed"
    assert "artifact_path" not in rejected
