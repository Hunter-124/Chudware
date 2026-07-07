"""Unit tests for product-media harvest (engine_oracle.parts_hub.product_media).

Offline: ``services.browser.session.browser_fetch`` is monkeypatched to return
canned HTML — no live network. Asserts every image (og:image, JSON-LD, <img>
src/srcset) and document (.pdf / doc-word anchors) is extracted, absolutized
against the page URL, and de-duped.
"""
from __future__ import annotations

import pytest

from engine_oracle.parts_hub import product_media

_HTML = """
<html><head>
  <meta property="og:image" content="https://cdn.example.com/hero.jpg">
</head><body>
  <script type="application/ld+json">
    {"@type":"Product","name":"Widget",
     "image":["https://cdn.example.com/ld1.jpg","/rel/ld2.png"]}
  </script>
  <img src="/gallery/a.jpg">
  <img src="/gallery/small.jpg"
       srcset="/gallery/small.jpg 320w, /gallery/large.jpg 1280w">
  <img src="https://cdn.example.com/hero.jpg">  <!-- dup of og:image -->
  <img src="data:image/png;base64,AAAA">        <!-- ignored -->
  <a href="/docs/datasheet.pdf">Datasheet</a>
  <a href="/docs/manual.pdf">User Manual</a>
  <a href="https://cdn.example.com/spec">Spec Sheet</a>
  <a href="/other/page.html">Related</a>          <!-- not a doc -->
  <a href="/docs/datasheet.pdf">Datasheet again</a> <!-- dup -->
</body></html>
"""

_PAGE_URL = "https://shop.example.com/products/widget"


@pytest.fixture()
def canned_fetch(monkeypatch):
    def _fetch(url, site, **_k):
        return {"ok": True, "url": url, "content": _HTML,
                "session_profile_used": site}
    monkeypatch.setattr(product_media._session, "browser_fetch", _fetch)


def test_harvest_extracts_images_docs_absolutized_deduped(canned_fetch):
    r = product_media.harvest_product_media(_PAGE_URL)
    assert r["ok"] is True
    assert r["source_url"] == _PAGE_URL

    imgs = r["images"]
    # og:image + 2 JSON-LD + gallery a + largest-srcset — all absolute, no dup, no data:.
    assert "https://cdn.example.com/hero.jpg" in imgs
    assert "https://cdn.example.com/ld1.jpg" in imgs
    assert "https://shop.example.com/rel/ld2.png" in imgs          # absolutized
    assert "https://shop.example.com/gallery/a.jpg" in imgs
    assert "https://shop.example.com/gallery/large.jpg" in imgs     # largest srcset
    assert "https://shop.example.com/gallery/small.jpg" in imgs
    assert all(not u.startswith("data:") for u in imgs)
    assert len(imgs) == len(set(imgs))                              # de-duped
    assert imgs.count("https://cdn.example.com/hero.jpg") == 1

    docs = r["documents"]
    assert "https://shop.example.com/docs/datasheet.pdf" in docs
    assert "https://shop.example.com/docs/manual.pdf" in docs
    assert "https://cdn.example.com/spec" in docs                   # doc-word, no .pdf
    assert "https://shop.example.com/other/page.html" not in docs   # not a doc
    assert len(docs) == len(set(docs))                              # de-duped


def test_catalog_part_id_resolves(monkeypatch):
    seen = {}

    def _fetch(url, site, **_k):
        seen["url"], seen["site"] = url, site
        return {"ok": True, "url": url, "content": _HTML, "session_profile_used": site}
    monkeypatch.setattr(product_media._session, "browser_fetch", _fetch)

    pid = "catalog:grabcad::https://grabcad.example/model/123"
    r = product_media.harvest_product_media(pid)
    assert r["ok"] is True
    assert seen["url"] == "https://grabcad.example/model/123"
    assert seen["site"] == "grabcad"
    assert r["source_url"] == "https://grabcad.example/model/123"


def test_bad_reference_is_structured_failure():
    r = product_media.harvest_product_media("not a url")
    assert r["ok"] is False and r["reason"] == "bad_reference"


def test_fetch_failure_propagates_honestly(monkeypatch):
    def _fetch(url, site, **_k):
        return {"ok": False, "reason": "captcha_or_2fa_detected", "hint": "re-login"}
    monkeypatch.setattr(product_media._session, "browser_fetch", _fetch)

    r = product_media.harvest_product_media(_PAGE_URL)
    assert r["ok"] is False
    assert r["reason"] == "captcha_or_2fa_detected"


def test_page_with_no_media_is_ok_empty(monkeypatch):
    def _fetch(url, site, **_k):
        return {"ok": True, "url": url, "content": "<html><body>nothing</body></html>",
                "session_profile_used": site}
    monkeypatch.setattr(product_media._session, "browser_fetch", _fetch)

    r = product_media.harvest_product_media(_PAGE_URL)
    assert r["ok"] is True
    assert r["images"] == [] and r["documents"] == []
