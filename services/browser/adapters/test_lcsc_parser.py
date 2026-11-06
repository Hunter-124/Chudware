"""Offline LCSC search and datasheet fixtures."""
import pytest

from services.browser.adapters import lcsc


# Rendered result-table shape (with per-row stock, price and images).
SEARCH_HTML = '''<table>
<tr><td><img data-src="/images/c8734.png"></td>
<td><span aria-label="ST STM32F103 detail page" title="C8734">C8734</span></td>
<td><span class="mpn">STM32F103</span><span class="manufacturer">ST</span></td>
<td><span class="price">$2.10</span><span class="stock">12,000 in stock</span></td></tr>
<tr><td><img src="/images/c123.png"></td>
<td><span aria-label="Other IC detail page" title="C123">C123</span></td>
<td><span class="price">$0.25</span><span class="stock">15 in stock</span></td></tr>
</table>'''

# Trimmed from the 2026-11-04 public SSR response: product rows were not rendered.
LIVE_SSR_SHELL = '''<main class="v-main"><div class="v-main__wrap">
<div aria-busy="true" aria-live="polite" role="alert"
class="v-skeleton-loader px-5 py-8 v-skeleton-loader--is-loading theme--light">
<div class="v-skeleton-loader__table v-skeleton-loader__bone"></div></div></div></main>'''


def test_search_preserves_row_data_and_encodes_query(monkeypatch):
    requested = []
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda url, site: (
        requested.append((url, site)) or {"ok": True, "content": SEARCH_HTML}))
    first, second = lcsc.ADAPTER.search("C++ & /#")
    assert requested == [("https://www.lcsc.com/search?q=C%2B%2B+%26+%2F%23", "lcsc")]
    assert (first["kind"], first["sku"], first["mpn"], first["manufacturer"]) == (
        "product", "C8734", "STM32F103", "ST")
    assert (first["price"], first["currency"], first["stock"]) == (2.10, "USD", 12000)
    assert first["thumbnail"] == "https://www.lcsc.com/images/c8734.png"
    assert (second["sku"], second["price"], second["stock"]) == ("C123", 0.25, 15)
    assert second["thumbnail"].endswith("/images/c123.png")


def test_login_widget_is_not_a_wall_when_results_exist(monkeypatch):
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {
        "ok": True, "content": SEARCH_HTML + '<div class="login-dialog"></div>'})
    assert lcsc.ADAPTER.search("chip")[0]["sku"] == "C8734"
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {
        "ok": True, "content": "<form></form>", "final_url": "https://www.lcsc.com/login"})
    assert lcsc.ADAPTER.search("chip")[0]["reason"] == "login_required"


@pytest.mark.parametrize(("content", "reason"), [
    ("<html>Access Denied</html>", "blocked"),
    ("<html>Verify you are human</html>", "captcha"),
    ('<div id="login-email"></div>', "login_required"),
    (LIVE_SSR_SHELL, "parse_failed"),
])
def test_search_failures_are_not_empty(monkeypatch, content, reason):
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {"ok": True, "content": content})
    failure = lcsc.ADAPTER.search("chip")[0]
    assert (failure["ok"], failure["site"], failure["op"], failure["reason"]) == (
        False, "lcsc", "search", reason)


def test_search_confirmed_no_results(monkeypatch):
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {"ok": True, "content": "No results found"})
    assert lcsc.ADAPTER.search("nothing") == []


def test_acquire_returns_pdf_document_not_geometry(monkeypatch, tmp_path):
    page = '<span class="lcsc-number">C8734</span><a href="../files/datasheet.pdf">Datasheet</a>'
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {
        "ok": True, "content": page, "final_url": "https://www.lcsc.com/products/C8734/page"})
    downloaded = []
    def download(url, site, dest):
        downloaded.append(url)
        dest.write_bytes(b"%PDF-1.7\n%%EOF")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(lcsc, "download_path", lambda *args, **kwargs: (tmp_path / "datasheet.pdf", None))
    monkeypatch.setattr(lcsc._session, "browser_download", download)
    result = lcsc.ADAPTER.acquire({"url": "https://www.lcsc.com/product-detail/C8734.html", "sku": "C8734"})
    assert downloaded == ["https://www.lcsc.com/products/files/datasheet.pdf"]
    assert (result["ok"], result["artifact_kind"], result["format"], result["sku"]) == (True, "document", "pdf", "C8734")
    assert result["datasheet_path"] == result["artifact_path"]


def test_acquire_rejects_mismatched_sku_and_invalid_pdf(monkeypatch, tmp_path):
    page = '<span class="lcsc-number">C123</span><a href="/datasheet.pdf">Datasheet</a>'
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    monkeypatch.setattr(lcsc._session, "browser_download", lambda *_: pytest.fail("wrong SKU download"))
    result = lcsc.ADAPTER.acquire({"url": "https://www.lcsc.com/product-detail/C8734.html", "sku": "C8734"})
    assert result["reason"] == "unsupported"
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/datasheet.pdf">Datasheet</a>'})
    assert lcsc.ADAPTER.acquire({"url": "https://www.lcsc.com/product-detail/C123.html", "sku": "C8734"})["reason"] == "unsupported"
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    monkeypatch.setattr(lcsc, "download_path", lambda *args, **kwargs: (tmp_path / "doc.pdf", None))
    def invalid_download(url, site, dest):
        dest.write_text("<html>blocked</html>")
        return {"ok": True, "dest": str(dest)}
    monkeypatch.setattr(lcsc._session, "browser_download", invalid_download)
    assert lcsc.ADAPTER.acquire({"url": "https://www.lcsc.com/product-detail/C123.html"})["reason"] == "parse_failed"


def test_missing_pdf_and_download_failure_are_not_success(monkeypatch):
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/privacy.pdf">Privacy policy</a>'})
    monkeypatch.setattr(lcsc._session, "browser_download", lambda *_: pytest.fail("unrelated PDF selected"))
    assert lcsc.ADAPTER.acquire({"url": "https://www.lcsc.com/p"})["reason"] == "not_found"
    monkeypatch.setattr(lcsc._session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/ds.pdf">Datasheet</a>'})
    monkeypatch.setattr(lcsc._session, "browser_download", lambda *_: {"ok": False, "reason": "blocked", "hint": "blocked"})
    assert lcsc.ADAPTER.acquire({"url": "https://www.lcsc.com/p"})["reason"] == "blocked"
