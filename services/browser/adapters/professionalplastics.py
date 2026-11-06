"""services/browser/adapters/professionalplastics.py — Professional Plastics source adapter (Parts Library V2).

The CGI search separates product families, reference pages, and PDF documents.
Product images and titles are often in separate links to the same result.
Product-page acquisition uses the shared spec-table and datasheet handling.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .. import session as _session
from . import abs_url, download_path, fail, provenance_envelope, register, source_failure
from ._retail import RetailAdapter, card_image

SITE = "professionalplastics"
BASE_URL = "https://www.professionalplastics.com"
SEARCH_URL_TMPL = (BASE_URL + "/cgi-bin/pp.pl?pgm=co_search&func=process"
                   "&strfnbr=3&terms={query}&type=1")
LICENSE_NOTE = (
    "Professional Plastics product data: reference-only, link-out commerce data. "
    "This adapter never stores or redistributes Professional Plastics' page "
    "content — only a small price/stock-spec record copied with provenance "
    "(source URL + fetch timestamp) for the user's own sourcing decision, never "
    "LLM-generated. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('id="login-email"',)
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")

_SECTION_RE = re.compile(r'<ul\b[^>]*\bid=["\'](?:prod_results|sr2_results|sr3_results)["\']',
                         re.IGNORECASE)
_SECTION_KINDS = {"prod_results": "series", "sr2_results": "page", "sr3_results": "document"}


def _result_url(href: str) -> str | None:
    url = abs_url(BASE_URL, href)
    if not url:
        return None
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() not in {"search_id", "campaign"} and not key.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class _SearchLinks(HTMLParser):
    """Pair a result's image and title without borrowing media from adjacent hits."""

    def __init__(self, sections: bool):
        super().__init__(convert_charrefs=True)
        self.sections = sections
        self.section: str | None = None
        self.depth = 0
        self.current: dict | None = None
        self.results: dict[tuple[str, str], dict] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = {key.lower(): value or "" for key, value in attrs}
        if tag == "ul":
            kind = _SECTION_KINDS.get(fields.get("id", "").lower())
            if kind:
                self.section, self.depth = kind, 1
            elif self.section:
                self.depth += 1
        if tag == "a" and "standlink" in fields.get("class", "").lower().split():
            kind = self.section if self.sections else "series"
            url = _result_url(fields.get("href", ""))
            if kind and url:
                if kind == "series" and urlsplit(url).path.lower().endswith(".pdf"):
                    kind = "document"
                self.current = {"kind": kind, "url": url, "text": [], "thumbnail": None}
        elif tag == "img" and self.current and self.current["kind"] == "series":
            self.current["thumbnail"] = card_image(self.get_starttag_text(), BASE_URL)

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current is not None:
            item = self.current
            self.current = None
            key = (item["kind"], item["url"])
            hit = self.results.setdefault(key, {"ok": True, "title": "", "url": item["url"],
                                                 "kind": item["kind"], "thumbnail": None})
            title = " ".join("".join(item["text"]).split())
            if title:
                hit["title"] = title
            if item["thumbnail"]:
                hit["thumbnail"] = item["thumbnail"]
        if tag == "ul" and self.section:
            self.depth -= 1
            if self.depth == 0:
                self.section = None


class ProfessionalPlasticsAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS
    result_kinds = ("product", "series", "page", "document")

    def parse_search(self, content: str) -> list[dict]:
        hits = super().parse_search(content)
        parser = _SearchLinks(bool(_SECTION_RE.search(content or "")))
        parser.feed(content or "")
        seen = {_result_url(hit["url"]) for hit in hits}
        for hit in parser.results.values():
            if hit["title"] and hit["url"] not in seen:
                seen.add(hit["url"])
                hits.append(hit)
        return hits

    def acquire(self, result: dict) -> dict:
        if result.get("kind") != "document":
            return super().acquire(result)
        url = result.get("url") or ""
        if not url or not urlsplit(url).path.lower().endswith(".pdf"):
            return fail(SITE, "acquire", "unsupported", "The document is not a PDF.", url=url)
        dest, refusal = download_path(SITE, url, default="document.pdf")
        if refusal:
            return {**fail(SITE, "acquire", "policy_refused", refusal.get("hint", ""), url=url),
                    "detail": refusal.get("reason")}
        outcome = _session.browser_download(url, SITE, dest)
        if not outcome.get("ok"):
            return source_failure(SITE, "acquire", outcome, url=url)
        path = outcome.get("dest")
        if not path:
            return fail(SITE, "acquire", "parse_failed", "Download returned no file.", url=url)
        from services.browser.cad_validate import classify_artifact
        checked = classify_artifact(path, "document")
        if not checked["ok"]:
            return fail(SITE, "acquire", "parse_failed", checked["reason"], url=url)
        return provenance_envelope(
            source_url=url, session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE, artifact_path=path, artifact_kind="document",
            format="pdf", datasheet_path=path, title=result.get("title"))


ADAPTER = register(ProfessionalPlasticsAdapter())
