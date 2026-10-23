"""services/browser/adapters/gbstandards.py — GB national-standards adapter.

openstd.samr.gov.cn (国家标准全文公开, "National Standards Full-Text Public
Disclosure") is the one FREE full-text portal for Chinese national standards
(GB / GB/T). Many GB/T standards are downloadable as free PDFs; this feeds the
reverse-engineering spec pipeline (thread/material/tolerance references).

Honest limits:
- Search is by GB number or keyword; the detail page is reached via
  ``newGbInfo?hcno=<hex>`` where the ``hcno`` token is extracted best-effort.
- `acquire` downloads the free full-text PDF where the portal exposes one.
  Standards ADAPTED from international standards (ISO/IEC) are copyright-blocked
  and NOT freely published — those degrade to a clear ``ok:false`` reason
  (copyright_blocked), NOT login_required (there is no login to offer).

Live-verified 2026-08-07: result rows invoke ``showInfo('<hcno>')`` and carry
the GB number and Chinese title in separate cells.
"""
from __future__ import annotations

import re

from .. import session as _session
from . import download_path, provenance_envelope, register

SITE = "gbstandards"
BASE_URL = "http://openstd.samr.gov.cn/bzgk/gb"
SEARCH_URL_TMPL = BASE_URL + "/std_list?p.p2={query}"
DETAIL_URL_TMPL = BASE_URL + "/newGbInfo?hcno={hcno}"
LICENSE_NOTE = (
    "GB/GB-T national standards are published for free full-text public "
    "disclosure by openstd.samr.gov.cn (SAMR). Freely-disclosed standards are "
    "fetched for the user's own engineering reference with provenance (source "
    "URL + fetch timestamp). Standards adapted from ISO/IEC are copyright-held "
    "and not freely published — those are not downloaded. See "
    "contracts/license_debt.md."
)

# Current result rows carry the hcno token in ``showInfo(...)`` and put the GB
# number and descriptive title in separate cells.
_RESULT_RE = re.compile(
    r'onclick="showInfo\([\'"](?P<hcno>[0-9A-Fa-f]+)[\'"]\);?"[^>]*>'
    r'(?P<number>.*?)</a>.*?'
    r'<td[^>]*class="[^"]*mytxt[^"]*"[^>]*>.*?<a[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL)
# Best-effort: GB number appears near the row (e.g. "GB/T 197-2018").
_GBNO_RE = re.compile(r'(GB(?:/T|/Z)?\s*\d+(?:\.\d+)?(?:-\d+)?)', re.IGNORECASE)
# Best-effort: a free full-text PDF link on the detail page.
_PDF_RE = re.compile(r'href="(?P<href>[^"]+\.pdf(?:\?[^"]*)?)"', re.IGNORECASE)
# Best-effort: markers that the standard is copyright-blocked (adapted / not free).
_COPYRIGHT_MARKERS = ('采用国际标准', '版权', '不公开', 'copyright', 'not available')


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


class GbStandardsAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=query.replace(" ", "+"))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        hits = []
        for m in _RESULT_RE.finditer(content):
            number = _strip_tags(m.group("number"))
            name = _strip_tags(m.group("title"))
            title = f"{number} — {name}" if name else number
            gb_match = _GBNO_RE.search(number)
            hits.append({
                "title": title,
                "url": DETAIL_URL_TMPL.format(hcno=m.group("hcno")),
                "site": SITE,
                "hcno": m.group("hcno"),
                "gb_number": gb_match.group(1).strip() if gb_match else None,
            })
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return {"ok": False, "reason": "bad_result", "hint": "result has no 'url'"}

        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return page
        content = page.get("content", "")

        pdf_match = _PDF_RE.search(content)
        if not pdf_match:
            # No free PDF: most often an ISO/IEC-adapted standard that is
            # copyright-held and not freely disclosed. Degrade honestly — there
            # is no login to offer, so this is a plain ok:false, not login_required.
            if any(mk in content for mk in _COPYRIGHT_MARKERS):
                return {"ok": False, "reason": "copyright_blocked",
                        "hint": "this standard appears to be adapted from an international "
                                "standard and is copyright-held; no free full text is published"}
            return {"ok": False, "reason": "no_download_link",
                    "hint": "no free full-text PDF found on this standard's detail page"}

        pdf_url = pdf_match.group("href")
        if pdf_url.startswith("/"):
            pdf_url = "http://openstd.samr.gov.cn" + pdf_url

        dest, refusal = download_path(SITE, pdf_url, default="standard.pdf")
        if refusal is not None:
            return refusal
        outcome = _session.browser_download(pdf_url, SITE, dest)
        if not outcome.get("ok"):
            return outcome

        gb_match = _GBNO_RE.search(content)
        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=outcome["dest"],
            title=result.get("title", ""),
            gb_number=(gb_match.group(1).strip() if gb_match else result.get("gb_number")),
            hcno=result.get("hcno"),
        )


ADAPTER = register(GbStandardsAdapter())
