"""Offline fixtures based on the vendor's separated CGI search result sections."""
import json

from services.browser import session
from services.browser.adapters import professionalplastics as plastics


SEARCH_HTML = '''
<a class="standLink" href="/header">Header navigation</a>
<ul id="prod_results">
 <li>
  <div><a href="/AcrylicSheet_Pearlescent?&amp;search_id=42" class="standLink">
   <img class="lazy" data-src="/professionalplastics/content/images_inv/pearl.jpg" alt="Acrylic Sheet"></a></div>
  <div><a href="/AcrylicSheet_Pearlescent?&amp;search_id=42" class="standLink">Acrylic Sheet - Pearlescent</a>
   <ul><li>Decorative sheets</li></ul></div>
 </li>
 <li><a class="standLink" href="/Nylon?shape=rod&amp;search_id=42">Nylon Rod</a></li>
</ul>
<ul id="sr2_results"><li><a class="standLink" href="/AboutUs">
 <img src="/images/icon_pages_1.png">About Us</a></li></ul>
<ul id="sr3_results"><li><a class="standLink" href="/docs/acrylic.pdf">
 <img src="/images/download-pdf-00.png">Acrylic Datasheet</a></li></ul>
'''


def test_search_pairs_images_and_distinguishes_sections(monkeypatch):
    requests = []
    def fetch(url, site):
        requests.append((url, site))
        return {"ok": True, "content": SEARCH_HTML}
    monkeypatch.setattr(session, "browser_fetch", fetch)
    hits = plastics.ADAPTER.search("acrylic & sheet")
    assert requests == [(plastics.SEARCH_URL_TMPL.format(query="acrylic+%26+sheet"), plastics.SITE)]
    assert [h["kind"] for h in hits] == ["series", "series", "page", "document"]
    first, second, page, document = hits
    assert first["title"] == "Acrylic Sheet - Pearlescent"
    assert first["url"] == plastics.BASE_URL + "/AcrylicSheet_Pearlescent"
    assert first["thumbnail"] == plastics.BASE_URL + "/professionalplastics/content/images_inv/pearl.jpg"
    assert second["url"] == plastics.BASE_URL + "/Nylon?shape=rod"
    assert second["thumbnail"] is None
    assert page["url"] == plastics.BASE_URL + "/AboutUs" and page["thumbnail"] is None
    assert document["url"] == plastics.BASE_URL + "/docs/acrylic.pdf" and document["thumbnail"] is None
    assert all(hit["price"] is None and hit["stock"] is None for hit in hits)
    assert all(hit["model"] == {"available": None, "formats": []} for hit in hits)
    assert set(plastics.ADAPTER.result_kinds) == {"product", "series", "page", "document"}
    assert not plastics.ADAPTER.cad_acquisition


def test_standalone_anchor_and_jsonld_product(monkeypatch):
    obj = {"@type": "Product", "name": "Acrylic Rod", "url": "/rod", "sku": "R1",
           "material": "Acrylic", "image": "/rod.jpg",
           "offers": {"price": "5.50", "priceCurrency": "USD", "inventoryLevel": 9}}
    page = '<script type="application/ld+json">' + json.dumps(obj) + '</script>'
    page += '<a href="/AcrylicSheet?campaign=search" class="standLink">Acrylic Sheet</a>'
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    first, second = plastics.ADAPTER.search("acrylic")
    assert first["kind"] == "product" and first["sku"] == "R1"
    assert (first["price"], first["currency"], first["stock"], first["material"]) == (5.5, "USD", 9, "Acrylic")
    assert first["thumbnail"] == plastics.BASE_URL + "/rod.jpg"
    assert second["kind"] == "series" and second["url"] == plastics.BASE_URL + "/AcrylicSheet"


def test_direct_document_acquires_pdf_not_geometry(monkeypatch, tmp_path):
    dest = tmp_path / "acrylic.pdf"
    dest.write_bytes(b"%PDF-1.4\nfixture")
    monkeypatch.setattr(plastics, "download_path", lambda *_a, **_kw: (dest, None))
    calls = []
    def download(url, site, path):
        calls.append((url, site, path))
        return {"ok": True, "dest": str(path), "session_profile_used": site}
    monkeypatch.setattr(session, "browser_download", download)
    url = plastics.BASE_URL + "/docs/acrylic.pdf"
    result = plastics.ADAPTER.acquire({"kind": "document", "title": "Acrylic Datasheet", "url": url})
    assert calls == [(url, plastics.SITE, dest)]
    assert result["artifact_kind"] == "document" and result["format"] == "pdf"
    assert result["artifact_path"] == str(dest) == result["datasheet_path"]
    assert result["source_url"] == url and result["license_note"]
    dest.write_bytes(b"<html>not a PDF</html>")
    assert plastics.ADAPTER.acquire({"kind": "document", "url": url})["reason"] == "parse_failed"


def test_errors_distinguish_wall_parse_and_download(monkeypatch):
    content = {"value": "changed markup"}
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": content["value"]})
    assert plastics.ADAPTER.search("acrylic")[0]["reason"] == "parse_failed"
    content["value"] = "Access Denied"
    assert plastics.ADAPTER.search("acrylic")[0]["reason"] == "blocked"
    monkeypatch.setattr(plastics, "download_path", lambda *_a, **_kw: (None, {"reason": "unsafe_filename", "hint": "bad name"}))
    failure = plastics.ADAPTER.acquire({"kind": "document", "url": plastics.BASE_URL + "/bad.pdf"})
    assert failure["reason"] == "policy_refused" and failure["detail"] == "unsafe_filename"
    assert plastics.ADAPTER.acquire({"kind": "document", "url": plastics.BASE_URL + "/page"})["reason"] == "unsupported"
