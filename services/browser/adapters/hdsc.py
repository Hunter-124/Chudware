"""services/browser/adapters/hdsc.py — HDSC (Huada Semiconductor) source adapter (Parts Library V2).

Huada Semiconductor (hdsc.com.cn) is a Chinese MCU vendor — the HC32 Cortex-M
family. The site is CN-hosted and can be slow.

Live-verified 2026-07-10: the old ``/search?q=`` guess 404'd. The site's only
English search surface is a single "Post" tab — ``/en/search?type=post&search=<q>``
(HTTP 200, server-rendered). It indexes NEWS/ARTICLE posts, not a product
catalog (the EN site has no product-search tab; datasheets live under a separate
product-center browse). Results are ``cc-postslist--item`` cards: a
``cc-postslist--title`` block wraps ``<a class="fn-th" href="…/en/<id>.html"
title="…">`` and a sibling ``cc-postslist--date`` carries the publish date. This
adapter overrides :meth:`parse_search` to read that markup and emit
``kind="post"`` hits honestly (title from the anchor's ``title`` attribute — the
inner text carries ``<em>`` search-highlight spans). Note some post titles are
Chinese even on the EN index. Open catalog — no login.
"""
from __future__ import annotations

import re

from . import register
from ._retail import RetailAdapter, _strip_tags

SITE = "hdsc"
BASE_URL = "https://www.hdsc.com.cn/en"
SEARCH_URL_TMPL = BASE_URL + "/search?type=post&search={query}"
LICENSE_NOTE = (
    "HDSC datasheets remain Huada Semiconductor's own manufacturer documents, "
    "fetched here for the user's own engineering reference only. This adapter "
    "never redistributes or LLM-generates page content — only a small "
    "spec/part record copied with provenance (source URL + fetch timestamp) and "
    "the manufacturer's datasheet saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", "登录", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

# One HDSC post-search result: a ``cc-postslist--title`` block wrapping the post
# anchor. Prefer the ``title`` attribute (clean) over the inner text (``<em>``
# highlight spans). The publish date follows in a ``cc-postslist--date`` span.
_TITLE_RE = re.compile(
    r'<div class="cc-postslist--title\s*">\s*'
    r'<a\b[^>]*\bclass="fn-th\s*"[^>]*\bhref="(?P<href>[^"]+)"[^>]*'
    r'\btitle="(?P<title>[^"]*)"',
    re.IGNORECASE | re.DOTALL)
_DATE_RE = re.compile(
    r'<div class="cc-postslist--date\s*">\s*<span class="date">\s*'
    r'(?P<date>[^<]+?)\s*</span>', re.IGNORECASE | re.DOTALL)


class HdscAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search(self, content: str) -> list[dict]:
        """HDSC's EN search indexes news/article POSTS (no product SKU/price).
        Parse each ``cc-postslist--title`` anchor + its publish date."""
        hits: list[dict] = []
        seen: set[str] = set()
        cards = list(_TITLE_RE.finditer(content))
        for i, m in enumerate(cards):
            href = m.group("href")
            url = href if href.startswith("http") else BASE_URL + href
            if url in seen:
                continue
            seen.add(url)
            title = _strip_tags(m.group("title"))
            if not title:
                continue
            hit = {
                "ok": True,
                "title": title,
                "url": url,
                "site": SITE,
                "kind": "post",
            }
            end = cards[i + 1].start() if i + 1 < len(cards) else len(content)
            date_m = _DATE_RE.search(content, m.end(), end)
            if date_m:
                hit["date"] = date_m.group("date").strip()
            hits.append(hit)
        return hits


ADAPTER = register(HdscAdapter())
