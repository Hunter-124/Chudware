"""Fixture-based parser test for the HDSC (Huada Semiconductor) search adapter.

HDSC_SEARCH_HTML holds two VERBATIM ``cc-postslist--item`` result blocks from a
real HDSC search page fetched live 2026-07-10 from
``/en/search?type=post&search=HC32`` (HTTP 200, server-rendered). The EN site's
only search tab is "Post", so results are NEWS/ARTICLE posts, not products — the
adapter emits ``kind="post"`` hits. Each result's title comes from the anchor's
``title`` attribute (inner text carries ``<em>`` highlight spans); the publish
date follows in a ``cc-postslist--date`` span. Some titles are Chinese even on
the EN index — that is the real content.

Offline style mirrors test_cui_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

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
    from engine import paths as engine_paths
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
    assert all(h["kind"] == "post" for h in hits)

    first = hits[0]
    assert first["url"] == "https://www.hdsc.com.cn/en/596.html"
    assert first["title"].startswith("华大半导体荣获2024")
    assert first["date"] == "2024-06-04"

    second = hits[1]
    assert second["url"] == "https://www.hdsc.com.cn/en/633.html"
    assert second["date"] == "2023-11-09"


def test_hdsc_search_url_is_post_search_endpoint():
    from services.browser.adapters import hdsc
    assert "search?type=post&search=" in hdsc.SEARCH_URL_TMPL
