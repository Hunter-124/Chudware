"""3D ContentCentral model search and direct authenticated CAD downloads.

Only direct model links exposed on the detail page can be acquired. Interactive
configurators are not driven by this adapter.
"""
from __future__ import annotations

import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from .. import session as _session
from ..cad_validate import extract_cad, validate_cad
from ..challenge import classify_challenge
from . import (abs_url, download_path, fail, login_required, make_hit, model_selection_question,
               provenance_envelope, quote_query, register)
from ._retail import card_image

SITE = "3dcontentcentral"
BASE_URL = "https://www.3dcontentcentral.com"
SEARCH_URL_TMPL = BASE_URL + "/Search.aspx?arg={query}"
LICENSE_NOTE = (
    "3D ContentCentral models: supplier-certified models are published by the "
    "supplier for use in the user's own designs; user-library models carry "
    "the uploader's terms. Downloads happen only under the user's own free "
    "account session and per-model terms are not verified by this adapter — "
    "read the model's page before reuse. See contracts/license_debt.md."
)

_SUPPLIER_RE = re.compile(
    r'<span[^>]*class=["\'][^"\']*supplier[^"\']*["\'][^>]*>(?P<supplier>[^<]+)</span>',
    re.IGNORECASE,
)
_FORMATS = {".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges",
            ".stl": "stl", ".obj": "obj", ".3mf": "3mf", ".zip": "zip"}
_RANK = {"step": 0, "iges": 1, "3mf": 2, "stl": 3, "obj": 4, "zip": 5}
_VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input",
                   "link", "meta", "param", "source", "track", "wbr"))


def _wall(content: str, url: str, op: str) -> dict | None:
    challenge = classify_challenge(content, url)
    if challenge == "captcha":
        return fail(SITE, op, "captcha", "The site presented a security challenge.", url=url)
    if re.search(r"\baccess denied\b|request blocked|automated access denied", content, re.I):
        return fail(SITE, op, "blocked", "The site blocked this request.", url=url)
    if challenge == "2fa" or re.search(
            r"you must be logged in to download|please (?:log|sign) in to (?:continue|download)|"
            r'class=["\'][^"\']*login-form', content, re.I):
        notice = login_required(SITE, url)
        return fail(SITE, op, "login_required", notice["hint"], url=url,
                    ask_user=notice["ask_user"])
    return None


class _ModelCards(HTMLParser):
    """Associate a model link with its own card, not a page-wide image."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.depth = 0
        self.frames: list[dict] = []
        self.anchor: dict | None = None
        self.rows: list[dict] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        fields = {key.lower(): value or "" for key, value in attrs}
        markup = self.get_starttag_text()
        for frame in self.frames:
            frame["markup"].append(markup)
        if self.anchor:
            self.anchor["markup"].append(markup)
            if tag == "img" and not self.anchor["alt"]:
                self.anchor["alt"] = fields.get("alt", "")
            if tag == "span" and "supplier" in fields.get("class", "").lower():
                self.anchor["supplier_depth"] = self.depth + 1
        if tag not in _VOID:
            self.depth += 1
        if tag in {"div", "li", "article", "tr"} and (
                tag in {"li", "article", "tr"} or
                re.search(r"model|result|card|item", fields.get("class", ""), re.I)):
            self.frames.append({"depth": self.depth, "markup": [markup], "rows": []})
        if tag == "a" and self.anchor is None and "model-title" in fields.get("class", "").lower():
            href = abs_url(self.base, fields.get("href"))
            parsed = urlparse(href or "")
            if (parsed.hostname and (parsed.hostname == "3dcontentcentral.com" or
                    parsed.hostname.endswith(".3dcontentcentral.com")) and
                    re.search(r"/(?:parts/|[^/]*download-model)", parsed.path, re.I)):
                self.anchor = {"url": href, "depth": self.depth, "text": [], "supplier": [],
                               "supplier_depth": None, "markup": [markup], "alt": ""}

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.anchor:
            self.anchor["text"].append(data)
            if self.anchor["supplier_depth"] is not None:
                self.anchor["supplier"].append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _VOID:
            return
        if self.anchor and tag == "a" and self.anchor["depth"] == self.depth:
            title = " ".join("".join(self.anchor["text"]).split()) or self.anchor["alt"].strip()
            if title:
                row = {"title": title, "url": self.anchor["url"],
                       "supplier": " ".join("".join(self.anchor["supplier"]).split()) or None,
                       "thumbnail": card_image("".join(self.anchor["markup"]), self.base)}
                self.rows.append(row)
                for frame in self.frames:
                    frame["rows"].append(row)
            self.anchor = None
        elif self.anchor and self.anchor["supplier_depth"] == self.depth and tag == "span":
            self.anchor["supplier_depth"] = None
        while self.frames and self.frames[-1]["depth"] == self.depth:
            frame = self.frames.pop()
            if len({row["url"] for row in frame["rows"]}) == 1:
                image = card_image("".join(frame["markup"]), self.base)
                for row in frame["rows"]:
                    row["thumbnail"] = row["thumbnail"] or image
        self.depth = max(0, self.depth - 1)


class _RenderedModelCards(HTMLParser):
    """Parse the site's rendered model-item cards and their labeled fields."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.rows: list[dict] = []
        self.card = None
        self.field = None

    def _finish(self):
        if self.card and self.card["url"] and self.card["title"]:
            self.card["title"] = " ".join("".join(self.card["title"]).split())
            self.card["supplier"] = " ".join("".join(self.card["supplier"]).split()) or None
            if self.card["title"]:
                self.rows.append(self.card)
        self.card = None
        self.field = None

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        classes = fields.get("class", "").split()
        if tag == "div" and "model-item" in classes:
            self._finish()
            self.card = {"title": [], "url": None, "supplier": [], "thumbnail": None}
        if self.card is None:
            return
        if tag == "div" and "model-item-image-preview" in classes:
            match = re.search(r"""background-image\s*:\s*url\(\s*['"]?([^'")]+)""",
                              fields.get("style", ""), re.I)
            if match:
                self.card["thumbnail"] = abs_url(self.base, match.group(1).strip())
        if tag == "div" and fields.get("id") in ("dvValue_Name", "dvValue_Supplier"):
            self.field = "title" if fields["id"] == "dvValue_Name" else "supplier"
        if tag == "a" and self.field == "title":
            href = abs_url(self.base, fields.get("href"))
            parsed = urlparse(href or "")
            if (parsed.hostname and (parsed.hostname == "3dcontentcentral.com" or
                    parsed.hostname.endswith(".3dcontentcentral.com")) and
                    re.search(r"/(?:download-model|secure/download-partner-model)\.aspx$", parsed.path, re.I)):
                self.card["url"] = href

    def handle_data(self, data):
        if self.card is not None and self.field:
            self.card[self.field].append(data)

    def handle_endtag(self, tag):
        if tag == "div":
            self.field = None

    def close(self):
        super().close()
        self._finish()


class _DownloadLinks(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = abs_url(self.base, dict(attrs).get("href"))
        fmt = _FORMATS.get(Path(unquote(urlparse(href or "").path)).suffix.lower())
        if href and fmt:
            self.links.append((href, fmt))


class Content3DAdapter:
    key = SITE
    hosts = ("3dcontentcentral.com",)
    result_kinds = ("model",)
    cad_acquisition = True
    login_required = True
    api = False

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [result]
        content = result.get("content", "")
        wall = _wall(content, result.get("final_url") or url, "search")
        if wall:
            return [wall]
        parser = _ModelCards(result.get("final_url") or url)
        parser.feed(content)
        rendered = _RenderedModelCards(result.get("final_url") or url)
        rendered.feed(content)
        rendered.close()
        hits = []
        seen = set()
        for row in parser.rows + rendered.rows:
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            hits.append(make_hit(SITE, title=row["title"], url=row["url"], kind="model",
                                 manufacturer=row["supplier"], thumbnail=row["thumbnail"],
                                 extra={"supplier": row["supplier"]}))
        if not hits and not re.search(r"\bno (?:models|results|matches) found\b|"
                                      r"\b0 (?:models|results|matches)\b", content, re.I):
            return [fail(SITE, "search", "parse_failed",
                         "Could not parse model results from the search page.", url=url)]
        return hits

    def acquire(self, result: dict) -> dict:
        url = result.get("url", "")
        if not url:
            return fail(SITE, "acquire", "unsupported", "Result has no model URL.")

        page = _session.browser_fetch(url, SITE)
        if not page.get("ok"):
            return page
        content = page.get("content", "")
        wall = _wall(content, page.get("final_url") or url, "acquire")
        if wall:
            return wall
        requested = str(result.get("format") or result.get("requested_format") or "").lower().lstrip(".")
        requested = {"stp": "step", "igs": "iges"}.get(requested, requested)
        if requested and (requested not in _RANK or requested == "zip"):
            return fail(SITE, "acquire", "unsupported", "Requested CAD format is unsupported.", url=url)
        parser = _DownloadLinks(page.get("final_url") or url)
        parser.feed(content)
        filename = str(result.get("filename") or "").strip()
        links = [(href, fmt) for href, fmt in parser.links
                 if (not requested or fmt == requested or fmt == "zip")
                 and (not filename or Path(unquote(urlparse(href).path)).name == filename)]
        if not links:
            return fail(SITE, "acquire", "unsupported",
                        "No direct link for the requested model and format was found.", url=url)
        names = sorted({Path(unquote(urlparse(href).path)).name for href, _ in links})
        if len({Path(name).stem.casefold() for name in names}) > 1:
            return {**fail(SITE, "acquire", "unsupported",
                           "Select a specific model file before downloading.", url=url,
                           ask_user=model_selection_question(SITE, url, names)),
                    "candidates": names, "selection_field": "filename"}
        links.sort(key=lambda item: (item[1] == "zip", _RANK[item[1]], item[0]))
        download_url = links[0][0]
        dest, refusal = download_path(SITE, download_url, default="download.step")
        if refusal is not None:
            return {**fail(SITE, "acquire", "policy_refused", refusal.get("hint", ""),
                           url=download_url), "original_reason": refusal.get("reason")}
        # Download to a separate file so a failed fetch/validation cannot replace
        # a previously acquired model with a login page or truncated body.
        with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".cad-", suffix=dest.suffix,
                                         delete=False) as temp:
            staging = Path(temp.name)
        try:
            outcome = _session.browser_download(download_url, SITE, staging)
            if not outcome.get("ok"):
                return outcome
            member = result.get("archive_member")
            checked = extract_cad(staging, requested_format=requested or None,
                                  requested_member=member)
            if not checked["ok"]:
                if checked.get("candidates"):
                    return {**fail(SITE, "acquire", "unsupported",
                                   "The archive contains multiple models; select an archive member.",
                                   url=download_url,
                                   ask_user=model_selection_question(SITE, url, checked["candidates"])),
                            "candidates": checked["candidates"], "selection_field": "archive_member"}
                if requested or member:
                    available = validate_cad(staging)
                    if available["ok"] or available.get("candidates"):
                        return fail(SITE, "acquire", "unsupported",
                                    "The download does not contain the requested CAD file.", url=download_url)
                return fail(SITE, "acquire", "parse_failed", checked["reason"], url=download_url)
            if checked.get("member"):
                artifact_path = checked["artifact_path"]
            else:
                os.replace(staging, dest)
                artifact_path = str(dest)
        finally:
            staging.unlink(missing_ok=True)
        supplier_match = _SUPPLIER_RE.search(content)
        return provenance_envelope(
            source_url=url,
            session_profile_used=outcome.get("session_profile_used", SITE),
            license_note=LICENSE_NOTE,
            artifact_path=artifact_path, artifact_kind="cad", format=checked["format"],
            title=result.get("title", ""),
            supplier=supplier_match.group("supplier").strip() if supplier_match else result.get("supplier"),
        )


ADAPTER = register(Content3DAdapter())
