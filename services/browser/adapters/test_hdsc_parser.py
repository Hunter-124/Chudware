"""Fixture-based parser test for the HDSC (Huada Semiconductor) search adapter.

HDSC_SEARCH_HTML holds two VERBATIM ``cc-postslist--item`` result blocks from a
real HDSC search page fetched live 2026-07-10 from
``/en/search?type=post&search=HC32`` (HTTP 200, server-rendered). The EN site's
only search tab is "Post", so results are NEWS/ARTICLE posts, not products — the
adapter emits ``kind="page"`` hits. Each result's title comes from the anchor's
``title`` attribute (inner text carries ``<em>`` highlight spans); the publish
date follows in a ``cc-postslist--date`` span. Some titles are Chinese even on
the EN index — that is the real content.

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib
from urllib.parse import parse_qs, urlsplit

import pytest


HDSC_SEARCH_HTML = "<ul>" + r"""<li class="cc-postslist--item underline">
                                <div class="cc-postslist--main">
                                        <div class="cc-postslist--title ">
                        <a class="fn-th " href="https://www.hdsc.com.cn/en/596.html" title="华大半导体荣获2024中国IC设计成就奖“十大中国IC设计公司”">
                            华大半导体荣获2024中国IC设计成就奖“十大中国IC设计公司”                        </a>
                    </div>
                                                               <div class="cc-postslist--meta">
                                        <div class="cc-postslist--date ">
                                        <span class="date">2024-06-04</span>
                                    </div>
                                                                                    </div>
                </div>
            </li>
                                            <li class="cc-postslist--item underline">
                                <div class="cc-postslist--main">
                                        <div class="cc-postslist--title ">
                        <a class="fn-th " href="https://www.hdsc.com.cn/en/633.html" title="华大半导体旗下小华半导体获评 “2023年度最佳MCU芯片”">
                            华大半导体旗下小华半导体获评 “2023年度最佳MCU芯片”                        </a>
                    </div>
                                                               <div class="cc-postslist--meta">
                                        <div class="cc-postslist--date ">
                                        <span class="date">2023-11-09</span>
</div></li>""" + "</ul>"


@pytest.fixture()
def hdsc_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import hdsc
    importlib.reload(hdsc)
    yield session, hdsc
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_hdsc_search_parses_post_results(hdsc_mod, monkeypatch):
    session, hdsc = hdsc_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": HDSC_SEARCH_HTML})

    hits = hdsc.ADAPTER.search("HC32")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "hdsc" for h in hits)
    assert all(h["kind"] == "page" for h in hits)

    first = hits[0]
    assert first["url"] == "https://www.hdsc.com.cn/en/596.html"
    assert first["title"].startswith("华大半导体荣获2024")
    assert first["date"] == "2024-06-04"
    assert first["thumbnail"] is None and first["sku"] is None and first["mpn"] is None
    assert first["price"] is None and first["currency"] is None and first["stock"] is None
    assert first["model"] == {"available": None, "formats": []}

    second = hits[1]
    assert second["url"] == "https://www.hdsc.com.cn/en/633.html"
    assert second["date"] == "2023-11-09"


def test_hdsc_search_url_is_post_search_endpoint():
    from services.browser.adapters import hdsc
    assert "search?type=post&search=" in hdsc.SEARCH_URL_TMPL


def test_hdsc_query_and_capabilities(hdsc_mod, monkeypatch):
    session, hdsc = hdsc_mod
    seen = []
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, site: seen.append(url) or {"ok": True, "content": HDSC_SEARCH_HTML})
    query = "HC32 & A/B+#"
    hdsc.ADAPTER.search(query)
    assert parse_qs(urlsplit(seen[0]).query) == {"type": ["post"], "search": [query]}
    assert hdsc.ADAPTER.hosts == ("hdsc.com.cn",)
    assert hdsc.ADAPTER.result_kinds == ("page",)
    assert not hdsc.ADAPTER.cad_acquisition and not hdsc.ADAPTER.login_required and not hdsc.ADAPTER.api


def test_hdsc_post_identity_and_relative_link(hdsc_mod, monkeypatch):
    session, hdsc = hdsc_mod
    page = """<li class="cc-postslist--item">
      <img src="/company-logo.png" alt="logo">
      <a href="/en/101.html" class="fn-th"><em>HC32</em> &amp; news</a>
      <span class="date">2024-05-01</span></li>"""
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {"ok": True, "content": page})
    hit, = hdsc.ADAPTER.search("HC32")
    assert hit["url"] == "https://www.hdsc.com.cn/en/101.html"
    assert hit["title"] == "HC32 & news" and hit["date"] == "2024-05-01"
    assert hit["kind"] == "page" and hit["thumbnail"] is None


@pytest.mark.parametrize(("payload", "reason"), [
    ({"ok": True, "content": '<p class="cc-postslist--empty">No data</p>'}, None),
    ({"ok": True, "content": '<div>No data</div>'}, "parse_failed"),
    ({"ok": True, "content": "<h1>Access Denied</h1>"}, "blocked"),
    ({"ok": True, "content": '<h1>Access Denied</h1><div class="g-recaptcha"></div>'}, "captcha"),
    ({"ok": True, "content": '<div class="g-recaptcha"></div>'}, "captcha"),
    ({"ok": True, "content": '<h1>Sign In</h1><form><input type="password"></form>'}, "login_required"),
    ({"ok": True, "content": "", "final_url": "https://www.hdsc.com.cn/login"}, "login_required"),
    ({"ok": False, "reason": "timeout", "status": 504, "hint": "timed out"}, "timeout"),
])
def test_hdsc_empty_and_failure_semantics(hdsc_mod, monkeypatch, payload, reason):
    session, hdsc = hdsc_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: payload)
    hits = hdsc.ADAPTER.search("unknown")
    if reason is None:
        assert hits == []
    else:
        assert len(hits) == 1 and hits[0]["ok"] is False
        assert hits[0]["site"] == "hdsc" and hits[0]["op"] == "search"
        assert hits[0]["reason"] == reason


def test_hdsc_signin_header_is_not_a_wall(hdsc_mod, monkeypatch):
    session, hdsc = hdsc_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *_: {"ok": True, "content": "Sign In " + HDSC_SEARCH_HTML})
    assert len(hdsc.ADAPTER.search("HC32")) == 2


def test_hdsc_linked_datasheet_is_document(hdsc_mod, monkeypatch):
    session, hdsc = hdsc_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/documents/hc32-datasheet.pdf">Datasheet</a>'})

    def download(url, site, dest):
        assert url == "https://www.hdsc.com.cn/documents/hc32-datasheet.pdf"
        dest.write_bytes(b"%PDF-1.4\nfixture")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    result = hdsc.ADAPTER.acquire({"url": "https://www.hdsc.com.cn/en/596.html"})
    assert result["ok"] is True and result["artifact_kind"] == "document"
    assert result["format"] == "pdf"


def test_hdsc_article_without_datasheet_is_not_a_cad_artifact(hdsc_mod, monkeypatch):
    session, hdsc = hdsc_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *_: {"ok": True, "content": "<article>Company news</article>"})
    result = hdsc.ADAPTER.acquire({"url": "https://www.hdsc.com.cn/en/596.html"})
    assert result["ok"] is False and result["reason"] == "not_found"


@pytest.mark.parametrize(("state", "reason"), [("download", "timeout"), ("html", "parse_failed")])
def test_hdsc_pdf_acquisition_failure(hdsc_mod, monkeypatch, state, reason):
    session, hdsc = hdsc_mod
    monkeypatch.setattr(session, "browser_fetch", lambda *_: {
        "ok": True, "content": '<a href="/documents/hc32-datasheet.pdf">Datasheet</a>'})

    def download(url, site, dest):
        if state == "download":
            return {"ok": False, "reason": "timeout"}
        dest.write_bytes(b"<html>not PDF</html>")
        return {"ok": True, "dest": str(dest)}

    monkeypatch.setattr(session, "browser_download", download)
    result = hdsc.ADAPTER.acquire({"url": "https://www.hdsc.com.cn/en/596.html"})
    assert result["ok"] is False and result["reason"] == reason
    assert result["site"] == "hdsc" and result["op"] == "acquire"
