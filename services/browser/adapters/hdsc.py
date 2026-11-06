"""services/browser/adapters/hdsc.py — HDSC (Huada Semiconductor) source adapter (Parts Library V2).

The English search indexes news/article posts, not a product catalog. Hits are
reference pages, with the post date when available. Search does not establish
that a post has a corresponding purchasable part or product image.
"""
from __future__ import annotations

import html
import re

from . import abs_url, make_hit, register
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

_LOGIN_WALL_MARKERS: tuple[str, ...] = ()
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

_CARD_RE = re.compile(
    r"""<li\b(?=[^>]*\bclass=["'][^"']*\bcc-postslist--item\b[^"']*["'])[^>]*>(.*?)</li>""",
    re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(
    r"""<a\b(?=[^>]*\bclass=["'][^"']*\bfn-th\b[^"']*["'])(?P<attrs>[^>]*)>"""
    r"(?P<text>.*?)</a>", re.IGNORECASE | re.DOTALL)
_DATE_RE = re.compile(
    r"""<span\b[^>]*\bclass=["'][^"']*\bdate\b[^"']*["'][^>]*>\s*(?P<date>[^<]+?)\s*</span>""",
    re.IGNORECASE | re.DOTALL)
_EMPTY_RE = re.compile(
    r"""<p\b[^>]*\bclass=["'][^"']*\bcc-postslist--empty\b[^"']*["'][^>]*>"""
    r"\s*No data\s*</p>", re.IGNORECASE)


def _attribute(tag: str, name: str) -> str:
    match = re.search(r"(?<![\w-])" + re.escape(name) + r"""\s*=\s*(["'])(.*?)\1""",
                      tag, re.IGNORECASE | re.DOTALL)
    return html.unescape(match.group(2)).strip() if match else ""


class HdscAdapter(RetailAdapter):
    key = SITE
    hosts = ("hdsc.com.cn",)
    result_kinds = ("page",)
    cad_acquisition = False
    login_required = False
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS

    def is_no_results(self, content: str) -> bool:
        return bool(_EMPTY_RE.search(content)) or super().is_no_results(content)

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for card in _CARD_RE.finditer(content):
            match = _TITLE_RE.search(card.group(1))
            if not match:
                continue
            attrs = match.group("attrs")
            url = abs_url(self.base_url + "/", _attribute(attrs, "href"))
            title = _attribute(attrs, "title") or html.unescape(_strip_tags(match.group("text")))
            if not url or not title or url in seen:
                continue
            seen.add(url)
            date = _DATE_RE.search(card.group(1))
            extra = {"ok": True}
            if date:
                extra["date"] = date.group("date").strip()
            hits.append(make_hit(SITE, title=title, url=url, kind="page", extra=extra))
        return hits


ADAPTER = register(HdscAdapter())
