"""services/browser/adapters/datasheet4u.py — Datasheet4U source adapter (Parts Library V2).

Datasheet4U indexes manufacturer datasheets, not commerce products. Search rows
carry a part number, manufacturer and description; any downloaded PDF remains
a reference document rather than CAD geometry.
"""
from __future__ import annotations

import html
import re

from .. import session
from ..cad_validate import classify_artifact
from ..challenge import classify_challenge
from . import (abs_url, download_path, fail, is_login_wall, login_required, make_hit,
               provenance_envelope, register, source_failure)
from ._retail import (RetailAdapter, _challenge, _is_captcha, _standalone_login, _strip_tags,
                      card_image, find_datasheet_url)

SITE = "datasheet4u"
BASE_URL = "https://www.datasheet4u.com"
SEARCH_URL_TMPL = BASE_URL + "/share_search.php?sWord={query}"
LICENSE_NOTE = (
    "Datasheets served by Datasheet4U remain the respective manufacturers' own "
    "documents, fetched here for the user's own engineering reference only. This "
    "adapter never redistributes or LLM-generates page content — only a small "
    "part record copied with provenance (source URL + fetch timestamp) and the "
    "manufacturer's datasheet saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS: tuple[str, ...] = ()  # public datasheet DB — no login to browse
_CHALLENGE_MARKERS = ("Access Denied", "Pardon Our Interruption")

# A result row: the part anchor `<a class="line" href="/datasheets/<mfr>/<part>/<id>">`
# holds the URL + part number; the row's `mfr-name` span and Description cell give the
# manufacturer and blurb.
_ROW_RE = re.compile(r'<tr\b[^>]*>(?P<row>(?:(?!</tr>).)*?/datasheets/.*?)</tr>',
                     re.IGNORECASE | re.DOTALL)
_PART_RE = re.compile(
    r"""<a\b(?=[^>]*\bhref\s*=\s*["'](?P<href>(?:(?:https?:)?//(?:www\.)?datasheet4u\.com)?/datasheets/[^"']+)["'])"""
    r"""(?=[^>]*\bclass\s*=\s*["'][^"']*\bline\b[^"']*["'])[^>]*>(?P<part>.*?)</a>""",
    re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(r"""class=["'][^"']*\bmfr-name\b[^"']*["'][^>]*>(?P<mfr>.*?)</span>""",
                     re.IGNORECASE | re.DOTALL)
_DESC_RE = re.compile(r"""data-label=["']Description["'][^>]*>(?P<desc>.*?)</td>""",
                      re.IGNORECASE | re.DOTALL)
_DOWNLOAD_PAGE_RE = re.compile(
    r"""<a\b[^>]*\bhref=["'](?P<href>(?:(?:https?:)?//(?:www\.)?datasheet4u\.com)?/download/[^"']+\.html(?:\?[^"']*)?)["'][^>]*>""",
    re.IGNORECASE)
_PDF_LINK_RE = re.compile(
    r"""\b(?:href|src)=["'](?P<href>[^"']+\.pdf(?:\?[^"']*)?)["']""", re.IGNORECASE)


class Datasheet4uAdapter(RetailAdapter):
    key = SITE
    hosts = ("datasheet4u.com",)
    result_kinds = ("document",)
    cad_acquisition = False
    login_required = False
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS
    no_results_markers = ("No data that matches perfectly!",)

    def parse_search(self, content: str) -> list[dict]:
        hits: list[dict] = []
        seen: set[str] = set()
        for rm in _ROW_RE.finditer(content):
            row = rm.group("row")
            pm = _PART_RE.search(row)
            if not pm:
                continue
            url = abs_url(BASE_URL, html.unescape(pm.group("href")))
            part = html.unescape(_strip_tags(pm.group("part")))
            if not url or not part or url in seen:
                continue
            seen.add(url)
            mm = _MFR_RE.search(row)
            manufacturer = html.unescape(_strip_tags(mm.group("mfr"))) if mm else None
            dm = _DESC_RE.search(row)
            desc = html.unescape(_strip_tags(dm.group("desc"))) if dm else ""
            extra = {"ok": True}
            if desc:
                extra["description"] = desc
            hits.append(make_hit(SITE, title=part, url=url, kind="document", mpn=part,
                                 manufacturer=manufacturer or None,
                                 thumbnail=card_image(row, BASE_URL), extra=extra))
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url") or ""
        if not url:
            return fail(SITE, "acquire", "not_found", "result has no URL")

        def fetch_page(page_url: str) -> tuple[dict | None, dict | None]:
            page = session.browser_fetch(page_url, SITE)
            if not page.get("ok"):
                return None, source_failure(SITE, "acquire", page, url=page_url)
            content = page.get("content") or ""
            final_url = page.get("final_url") or page_url
            challenge = classify_challenge(content, final_url)
            if _is_captcha(final_url, content) or is_login_wall(content, self.challenge_markers):
                return None, _challenge(SITE, "acquire", final_url, content)
            if challenge == "2fa" or self._redirected_to_login(page_url, final_url) or _standalone_login(content):
                return None, source_failure(SITE, "acquire", login_required(SITE, final_url),
                                            url=final_url)
            return page, None

        page, error = fetch_page(url)
        if error:
            return error
        content = page.get("content") or ""
        page_url = page.get("final_url") or url
        pdf_url = abs_url(page_url, find_datasheet_url(content, page_url))
        if not pdf_url:
            # Current detail pages link to a separate /download/...html page.
            intermediate = _DOWNLOAD_PAGE_RE.search(content)
            if not intermediate:
                return fail(SITE, "acquire", "parse_failed",
                            "No datasheet download link found on the detail page.", url=page_url)
            download_page = abs_url(page_url, html.unescape(intermediate.group("href")))
            if not download_page:
                return fail(SITE, "acquire", "parse_failed", "Invalid download page URL.",
                            url=page_url)
            page, error = fetch_page(download_page)
            if error:
                return error
            content = page.get("content") or ""
            page_url = page.get("final_url") or download_page
            pdf_url = abs_url(page_url, find_datasheet_url(content, page_url))
            if not pdf_url:
                pdf_link = _PDF_LINK_RE.search(content)
                pdf_url = abs_url(page_url, html.unescape(pdf_link.group("href"))) if pdf_link else None
        if not pdf_url:
            return fail(SITE, "acquire", "parse_failed",
                        "No PDF link found on the download page.", url=page_url)

        dest, refusal = download_path(SITE, pdf_url, default="datasheet.pdf")
        if refusal:
            return source_failure(SITE, "acquire", refusal, url=pdf_url)
        outcome = session.browser_download(pdf_url, SITE, dest)
        if not outcome.get("ok"):
            return source_failure(SITE, "acquire", outcome, url=pdf_url)
        artifact = outcome.get("dest")
        if not artifact:
            return fail(SITE, "acquire", "parse_failed", "Download returned no file.", url=pdf_url)
        checked = classify_artifact(artifact, "document")
        if not checked["ok"] or checked["format"] != "pdf":
            return fail(SITE, "acquire", "parse_failed",
                        checked.get("reason") or "The download is not a PDF document.", url=pdf_url)
        return provenance_envelope(
            source_url=url, session_profile_used=page.get("session_profile_used") or SITE,
            license_note=self.license_note, artifact_path=str(artifact), artifact_kind="document",
            format="pdf", title=result.get("title"), mpn=result.get("mpn"),
            manufacturer=result.get("manufacturer"), datasheet_url=pdf_url,
            datasheet_path=str(artifact),
        )


ADAPTER = register(Datasheet4uAdapter())
