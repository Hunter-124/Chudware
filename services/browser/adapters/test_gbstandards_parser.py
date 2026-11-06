"""Offline standards search and document acquisition fixtures."""
from services.browser.adapters import gbstandards as gb


ROW = ('<a onclick="showInfo(\'A1B2C3\');">GB/T 70.1-2008</a>'
       '<td class="mytxt"><a>Hexagon socket head cap screws</a></td>')


def test_search_document_and_encoding(monkeypatch):
    urls = []
    monkeypatch.setattr(gb._session, "browser_fetch", lambda url, site:
                        urls.append(url) or {"ok": True, "content": ROW})
    hit = gb.ADAPTER.search("GB/T 70 & 1")[0]
    assert "GB%2FT+70+%26+1" in urls[0]
    assert hit["kind"] == "document" and hit["gb_number"] == "GB/T 70.1-2008"
    assert hit["transport_warning"] and hit["thumbnail"] is None


def test_search_failures_and_empty(monkeypatch):
    for content, expected in [("<html>unknown</html>", "parse_failed"),
                              ("captcha", "captcha"), ("Access Denied", "blocked")]:
        monkeypatch.setattr(gb._session, "browser_fetch", lambda url, site: {"ok": True, "content": content})
        assert gb.ADAPTER.search("test")[0]["reason"] == expected
    monkeypatch.setattr(gb._session, "browser_fetch", lambda url, site: {"ok": True, "content": "no results"})
    assert gb.ADAPTER.search("test") == []


def test_pdf_is_document(monkeypatch, tmp_path):
    monkeypatch.setattr(gb._session, "browser_fetch", lambda url, site: {"ok": True, "content": '<a href="docs/spec.pdf">PDF</a>'})
    monkeypatch.setattr(gb, "download_path", lambda *a, **kw: (tmp_path / "spec.pdf", None))
    monkeypatch.setattr(gb._session, "browser_download", lambda url, site, dest: {"ok": True, "dest": str(dest)})
    result = gb.ADAPTER.acquire({"url": "http://openstd.samr.gov.cn/bzgk/gb/item"})
    assert result["ok"] and result["artifact_kind"] == "document" and result["format"] == "pdf"
    assert result["transport_warning"]
