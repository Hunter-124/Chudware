"""services/browser/adapters/componentsearchengine.py — Component Search Engine adapter.

Component Search Engine (componentsearchengine.com), the SamacSys / Supplyframe
library portal, hosts schematic symbols, PCB footprints and 3D STEP models per
manufacturer part number (MPN). Search/browse is public; downloading the library
archive requires the user's own free account. `acquire` lands the ECAD archive
(zip) or 3D model (STEP) with provenance for ecad_find_part / ecad_place_symbol.

Search results are a public HTML <table class="search-results-table">: each
row is <tr id="row-N" data-uid=...> whose part-number cell holds
<a href="/part-view/<mpn>/<manufacturer>" class="part-number">MPN</a> followed
by <span class="grey-text">Manufacturer</span> and a
<span class="td-description">Description</span>. The MODEL download is gated
behind the user's own free account, but the listing itself is public — a
"Sign In" header link is not a login wall.
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import urlsplit

from .. import session as _session
from ..cad_validate import classify_artifact, extract_cad
from . import (abs_url, download_path, fail, is_login_wall, login_required,
               model_selection_question, provenance_envelope, register, source_failure)
from ._retail import RetailAdapter, _challenge, _strip_tags, card_image

SITE = "componentsearchengine"
BASE_URL = "https://componentsearchengine.com"
SEARCH_URL_TMPL = BASE_URL + "/search?term={query}"
LICENSE_NOTE = (
    "Component Search Engine (SamacSys/Supplyframe) symbols/footprints/3D models "
    "are provided to registered account holders for use in the user's own "
    "designs under the portal's terms of use (parts are generally usable in "
    "commercial designs; redistribution of the library files themselves is not "
    "granted). This adapter downloads only under the user's own authenticated "
    "session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to download', 'id="id_username"', 'class="login-form"',
                       'Sign up to download', 'Please login or register')
_CHALLENGE_MARKERS = ("Access Denied", "Checking your browser", "verify you are human")
_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr\s*>", re.IGNORECASE | re.DOTALL)
_RESULT_RE = re.compile(
    r"""<a\b(?P<attrs>[^>]*?(?<![\w-])href\s*=\s*(?P<q>['"])(?P<href>[^'"]+)(?P=q)[^>]*)>"""
    r"(?P<title>.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(
    r"""<span\b[^>]*class\s*=\s*['"][^'"]*grey-text[^'"]*['"][^>]*>(?P<mfr>.*?)</span\s*>""",
    re.IGNORECASE | re.DOTALL)
_DOWNLOAD_RE = re.compile(
    r"""(?<![\w-])href\s*=\s*(?P<q>['"])(?P<href>[^'"]+\.(?:zip|step|stp|iges|igs|stl|3mf|obj)(?:[?#][^'"]*)?)(?P=q)""",
    re.IGNORECASE)
_CAD_FORMATS = {"step", "stp", "iges", "igs", "stl", "3mf", "obj"}
_IMAGES_RE = re.compile(r"""\bdata-images\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)


def _result_image(row: str, page_url: str) -> str | None:
    # Off-screen rows use no-image.svg as src; their actual image URLs are in
    # data-images, in the same order used by the site's lazy-image widget.
    image = card_image(row, page_url)
    if image and "no-image.svg" not in urlsplit(image).path.lower():
        return image
    for match in _IMAGES_RE.finditer(row):
        try:
            candidates = json.loads(html.unescape(match.group(2)))
        except (ValueError, TypeError):
            continue
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if isinstance(candidate, str) and "no-image.svg" not in candidate.lower():
                image = card_image(f'<img src="{html.escape(candidate, quote=True)}">', page_url)
                if image:
                    return image
    return None


class ComponentSearchEngineAdapter(RetailAdapter):
    key = SITE
    hosts = ("componentsearchengine.com",)
    result_kinds = ("model",)
    cad_acquisition = True
    login_required = True
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    # A login widget or download prompt can coexist with public results.
    login_wall_markers = ()
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search_page(self, content: str, page_url: str) -> list[dict]:
        rows = [m.group() for m in _ROW_RE.finditer(content)]
        matches = [(m, row) for row in rows for m in _RESULT_RE.finditer(row)]
        if not matches:
            matches = [(m, m.group()) for m in _RESULT_RE.finditer(content)]
        hits, seen = [], set()
        for match, row in matches:
            href = abs_url(page_url, html.unescape(match.group("href")))
            if not href:
                continue
            parsed = urlsplit(href)
            if (parsed.hostname or "").lower().removeprefix("www.") not in self.hosts:
                continue
            if not parsed.path.lower().startswith("/part-view/") or "part-number" not in match.group("attrs").lower():
                continue
            title = html.unescape(_strip_tags(match.group("title"))).strip()
            if not title or href in seen:
                continue
            seen.add(href)
            manufacturer = _MFR_RE.search(row)
            hits.append({
                "title": title, "url": href, "kind": "model", "mpn": title,
                "manufacturer": html.unescape(_strip_tags(manufacturer.group("mfr"))) if manufacturer else None,
                "thumbnail": _result_image(row, page_url),
                "model_available": True if "3D model available" in row else None,
            })
        if not hits and not self.is_no_results(content) and is_login_wall(
                content, ('id="id_username"', 'class="login-form"')):
            return [source_failure(SITE, "search", login_required(SITE, page_url), url=page_url)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(SITE, "acquire", "not_found", "Result has no part URL.")
        if result.get("kind") in {"series", "category"} and not (result.get("mpn") or result.get("sku")):
            return fail(SITE, "acquire", "unsupported", "Select a specific part before downloading.", url=url)
        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return source_failure(SITE, "acquire", page, url=url)
        content = page.get("content", "")
        final_url = page.get("final_url") or url
        if is_login_wall(content, _CHALLENGE_MARKERS):
            return _challenge(SITE, "acquire", url, content)
        if self._redirected_to_login(url, final_url):
            return source_failure(SITE, "acquire", login_required(SITE, url), url=url)
        links = list(dict.fromkeys(
            resolved for match in _DOWNLOAD_RE.finditer(content)
            if (resolved := abs_url(final_url, html.unescape(match.group("href"))))))
        if not links:
            if is_login_wall(content, _LOGIN_WALL_MARKERS):
                return source_failure(SITE, "acquire", login_required(SITE, url), url=url)
            return fail(SITE, "acquire", "not_found", "No direct library or 3D download link was found.", url=url)
        raw_format = str(result.get("format") or "").strip().lower()
        if raw_format and raw_format not in _CAD_FORMATS | {"zip"}:
            return fail(SITE, "acquire", "unsupported", "Select a supported download format.", url=url)
        requested = raw_format if raw_format in _CAD_FORMATS else None
        direct = [link for link in links if urlsplit(link).path.lower().rsplit(".", 1)[-1] in _CAD_FORMATS]
        if result.get("download_url"):
            selected = abs_url(final_url, result["download_url"])
            choices = [link for link in links if link == selected]
        elif requested:
            aliases = {"stp": "step", "igs": "iges"}
            wanted = aliases.get(requested, requested)
            choices = [link for link in direct
                       if aliases.get(urlsplit(link).path.lower().rsplit(".", 1)[-1],
                                      urlsplit(link).path.lower().rsplit(".", 1)[-1]) == wanted]
            if not choices and len(links) == 1 and urlsplit(links[0]).path.lower().endswith(".zip"):
                choices = links
        elif raw_format == "zip":
            choices = [link for link in links if urlsplit(link).path.lower().endswith(".zip")]
        else:
            choices = links
        if len(choices) != 1:
            candidates = choices if len(choices) > 1 else links
            return {**fail(SITE, "acquire", "unsupported",
                           "Select the desired download/configuration explicitly.", url=url,
                           ask_user=model_selection_question(SITE, url, candidates)),
                    "candidates": candidates, "selection_field": "download_url"}
        download_url = choices[0]
        if raw_format == "zip" and not urlsplit(download_url).path.lower().endswith(".zip"):
            return fail(SITE, "acquire", "unsupported", "The selected download is not a ZIP archive.", url=download_url)
        dest, refusal = download_path(SITE, download_url, default="download.zip")
        if refusal is not None:
            return source_failure(SITE, "acquire", refusal, url=download_url)
        outcome = _session.browser_download(download_url, SITE, dest)
        if not outcome.get("ok"):
            return source_failure(SITE, "acquire", outcome, url=download_url)
        artifact_path = outcome.get("dest")
        if not artifact_path:
            return fail(SITE, "acquire", "transport_failed", "Download returned no artifact path.", url=download_url)
        artifact = classify_artifact(artifact_path)
        if not artifact.get("ok"):
            return fail(SITE, "acquire", "parse_failed", artifact.get("reason", "Invalid download."), url=download_url)
        member = result.get("member")
        if member and not (artifact.get("member") or artifact.get("candidates")):
            return fail(SITE, "acquire", "unsupported", "The selected CAD member is unavailable.", url=download_url)
        if artifact["artifact_kind"] == "cad" or member or (requested and artifact.get("candidates")):
            artifact = extract_cad(artifact_path, requested_format=requested, requested_member=member)
            if not artifact.get("ok"):
                failure = fail(SITE, "acquire", "unsupported", artifact.get("reason", "Invalid CAD."),
                               url=download_url)
                if artifact.get("candidates"):
                    failure["candidates"] = artifact["candidates"]
                    failure["ask_user"] = model_selection_question(SITE, url, artifact["candidates"])
                    failure["selection_field"] = "member"
                return failure
            artifact_path = artifact["artifact_path"]
        elif artifact.get("candidates"):
            return {**fail(SITE, "acquire", "unsupported", artifact["reason"], url=download_url,
                           ask_user=model_selection_question(SITE, url, artifact["candidates"])),
                    "candidates": artifact["candidates"], "selection_field": "member"}
        elif requested:
            return fail(SITE, "acquire", "unsupported", "The download does not contain the requested 3D format.",
                        url=download_url)
        mfr = _MFR_RE.search(content)
        return provenance_envelope(
            source_url=url, session_profile_used=outcome.get("session_profile_used", SITE),
            fetched_ts=outcome.get("fetched_ts"), license_note=LICENSE_NOTE, artifact_path=artifact_path,
            artifact_kind=artifact["artifact_kind"], format=artifact["format"],
            archive_path=artifact.get("archive_path"), member=artifact.get("member"),
            download_url=download_url, title=result.get("title", ""), mpn=result.get("mpn"),
            manufacturer=html.unescape(_strip_tags(mfr.group("mfr"))) if mfr else result.get("manufacturer"),
        )


ADAPTER = register(ComponentSearchEngineAdapter())
