"""Search PCBWay Community projects; project listings are not product SKUs.

The adapter links to projects but does not acquire their design files or accept
quote/order terms on the user's behalf.
"""
from __future__ import annotations

import html
import re

from .. import challenge, session as _session
from . import (abs_url, fail, is_login_wall, login_required, make_hit, quote_query,
               register, source_failure)
from ._retail import RetailAdapter, _challenge, _strip_tags, card_image, visible_text

SITE = "pcbway"
BASE_URL = "https://www.pcbway.com"
SEARCH_URL_TMPL = BASE_URL + "/project/projectsearch?q={query}&t=1"

_LOGIN_WALL_MARKERS = ('id="login-email"', 'class="login-panel"')
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption", "Just a moment")
_TERMS_MARKERS = ('Accept the terms', 'class="order-terms"', 'agree to the Terms')
_RESULT_RE = re.compile(
    r'''<a\b(?=[^>]*class=["'][^"']*\binfo-title\b[^"']*["'])'''
    r'''(?=[^>]*href=["'](?P<href>/project/shareproject/[^"'?#]+)(?:[?#][^"']*)?["'])[^>]*>'''
    r'''(?P<title>.*?)</a>''', re.IGNORECASE | re.DOTALL)
_ROW_START_RE = re.compile(r'''<div\b[^>]*class=["'][^"']*\bresult-row\b[^"']*["'][^>]*>''',
                           re.IGNORECASE)
_GRAPHIC_RE = re.compile(
    r'''<a\b(?=[^>]*class=["'][^"']*\binfo-graphic\b[^"']*["'])'''
    r'''(?=[^>]*href=["'](?P<href>[^"']+)["'])[^>]*>(?P<graphic>.*?)</a>''',
    re.IGNORECASE | re.DOTALL)
_BACKGROUND_RE = re.compile(r'''background-image\s*:\s*url\(\s*["']?([^"')\s]+)''', re.IGNORECASE)
_NO_RESULTS_RE = re.compile(r'\b(?:no results(?: found)?|no projects found|0 results)\b', re.IGNORECASE)


class PcbwayAdapter:
    key = SITE
    hosts = ("pcbway.com",)
    result_kinds = ("project",)
    cad_acquisition = False
    login_required = False
    api = False

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        response = _session.browser_fetch(url, SITE)
        if not response.get("ok"):
            return [source_failure(SITE, "search", response, url=url)]
        content = response.get("content", "")
        page_url = response.get("final_url") or url
        if challenge.classify_challenge(content, page_url) == "captcha" or is_login_wall(content, _CHALLENGE_MARKERS):
            return [_challenge(SITE, "search", page_url, content)]
        if RetailAdapter._redirected_to_login(url, page_url) or (
                is_login_wall(content, _LOGIN_WALL_MARKERS) and not _RESULT_RE.search(content)):
            return [source_failure(SITE, "search", login_required(SITE, page_url), url=page_url)]

        rows = list(_ROW_START_RE.finditer(content))
        blocks = [content[row.start():rows[i + 1].start() if i + 1 < len(rows) else len(content)]
                  for i, row in enumerate(rows)] if rows else [content]
        hits, seen = [], set()
        for block in blocks:
            for match in _RESULT_RE.finditer(block):
                project_url = abs_url(response.get("final_url") or url, html.unescape(match.group("href")))
                title = html.unescape(_strip_tags(match.group("title"))).strip()
                if not title or not project_url or project_url in seen:
                    continue
                seen.add(project_url)
                thumbnail = None
                for graphic in _GRAPHIC_RE.finditer(block):
                    graphic_url = abs_url(project_url, html.unescape(graphic.group("href")).split("?", 1)[0])
                    if graphic_url != project_url:
                        continue
                    image = _BACKGROUND_RE.search(graphic.group("graphic"))
                    if image:
                        thumbnail = abs_url(project_url, html.unescape(image.group(1)))
                    else:
                        thumbnail = card_image(graphic.group("graphic"), project_url)
                    break
                hits.append(make_hit(SITE, title=title, url=project_url, kind="project", thumbnail=thumbnail))
        if not hits and not _NO_RESULTS_RE.search(visible_text(content)):
            return [fail(SITE, "search", "parse_failed", "No recognizable PCBWay projects were rendered.", url=url)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(SITE, "acquire", "not_found", "Result has no project URL.")
        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return source_failure(SITE, "acquire", page, url=url)
        content = page.get("content", "")
        page_url = page.get("final_url") or url
        if challenge.classify_challenge(content, page_url) == "captcha" or is_login_wall(content, _CHALLENGE_MARKERS):
            return _challenge(SITE, "acquire", page_url, content)
        if RetailAdapter._redirected_to_login(url, page_url) or is_login_wall(content, _LOGIN_WALL_MARKERS):
            return source_failure(SITE, "acquire", login_required(SITE, page_url), url=page_url)
        if is_login_wall(content, _TERMS_MARKERS):
            return fail(SITE, "acquire", "terms_required", "PCBWay requires accepting terms.", url=url,
                        ask_user="Review and accept the terms on PCBWay before continuing.")
        return fail(SITE, "acquire", "unsupported",
                    "Project-file acquisition is not supported by this adapter.", url=url)


ADAPTER = register(PcbwayAdapter())
