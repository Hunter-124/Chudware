"""services/browser/adapters/ultralibrarian.py — Ultra Librarian source adapter.

Ultra Librarian hosts ECAD symbols, footprints and 3D models. Its search app
is on app.ultralibrarian.com and an anonymous request may redirect to sign-in.
Search-card and static-download parsing are best effort; a signed-in download
has not been verified.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from .. import session as _session
from ..cad_validate import classify_artifact, extract_cad
from . import (abs_url, download_path, fail, is_login_wall, login_required,
               model_selection_question, provenance_envelope, register, source_failure)
from ._retail import RetailAdapter, _challenge, _strip_tags, card_blocks, card_image

SITE = "ultralibrarian"
BASE_URL = "https://app.ultralibrarian.com"
SEARCH_URL_TMPL = BASE_URL + "/search?queryText={query}"
LICENSE_NOTE = (
    "Ultra Librarian symbols/footprints/3D models are provided to registered "
    "account holders for use in the user's own designs under Ultra Librarian's "
    "terms of use (free parts are generally usable in commercial designs; "
    "redistribution of the library files themselves is not granted). This "
    "adapter downloads only under the user's own authenticated Ultra Librarian "
    "session. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ('Log in to Ultra Librarian', 'id="id_username"',
                       'class="login-form"', 'Sign up to download', 'Create a free account')
_CHALLENGE_MARKERS = ("Access Denied", "Checking your browser", "verify you are human")
_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr\s*>", re.IGNORECASE | re.DOTALL)
_RESULT_RE = re.compile(
    r"""<a\b(?P<attrs>[^>]*?(?<![\w-])href\s*=\s*(?P<q>['"])(?P<href>[^'"]+)(?P=q)[^>]*)>"""
    r"(?P<title>.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_MFR_RE = re.compile(
    r"""<span\b[^>]*class\s*=\s*['"][^'"]*manufacturer[^'"]*['"][^>]*>(?P<mfr>.*?)</span\s*>""",
    re.IGNORECASE | re.DOTALL)
_MFR_ATTR_RE = re.compile(r"""(?<![\w-])data-manufacturer\s*=\s*(?P<q>['"])(?P<mfr>.*?)(?P=q)""",
                          re.IGNORECASE | re.DOTALL)
_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_DOWNLOAD_RE = re.compile(
    r"""(?<![\w-])href\s*=\s*(?P<q>['"])(?P<href>[^'"]+\.(?:zip|step|stp|iges|igs|stl|3mf|obj)(?:[?#][^'"]*)?)(?P=q)""",
    re.IGNORECASE)
_CAD_FORMATS = {"step", "stp", "iges", "igs", "stl", "3mf", "obj"}


class UltraLibrarianAdapter(RetailAdapter):
    key = SITE
    hosts = ("ultralibrarian.com", "app.ultralibrarian.com", "sso.ultralibrarian.com")
    result_kinds = ("model",)
    cad_acquisition = True
    login_required = True
    api = False
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = ()
    challenge_markers = _CHALLENGE_MARKERS

    def parse_search_page(self, content: str, page_url: str) -> list[dict]:
        blocks = [m.group() for m in _ROW_RE.finditer(content)]
        for name in ("part-card", "result-card", "search-result"):
            blocks.extend(card_blocks(content, "div", name))
            blocks.extend(card_blocks(content, "article", name))
        matches = [(m, block) for block in blocks for m in _RESULT_RE.finditer(block)]
        if not matches:
            matches = [(m, m.group()) for m in _RESULT_RE.finditer(content)]
        hits, seen = [], set()
        for match, block in matches:
            href = abs_url(page_url, html.unescape(match.group("href")))
            if not href:
                continue
            parsed = urlsplit(href)
            if (parsed.hostname or "").lower().removeprefix("www.") not in self.hosts:
                continue
            if not parsed.path.lower().startswith("/details/"):
                continue
            title = html.unescape(_strip_tags(_MFR_RE.sub("", match.group("title")))).strip()
            if not title or href in seen:
                continue
            seen.add(href)
            mfr = _MFR_ATTR_RE.search(match.group("attrs")) or _MFR_RE.search(block)
            available = None
            if re.search(r"\b3d model available\b|/3dyes\.svg", block, re.I):
                available = True
            elif re.search(r"\b3d (?:model )?not available\b|/3dno\.svg", block, re.I):
                available = False
            # Symbol/footprint/3D availability icons are not part previews.
            images = "".join(m.group() for m in _IMAGE_RE.finditer(block)
                             if not re.search(r"/(?:images/symbols|search_icons)/", m.group(), re.I))
            hits.append({
                "title": title, "url": href, "kind": "model", "mpn": title,
                "manufacturer": html.unescape(_strip_tags(mfr.group("mfr"))) if mfr else None,
                "thumbnail": card_image(images, page_url), "model_available": available,
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


ADAPTER = register(UltraLibrarianAdapter())
