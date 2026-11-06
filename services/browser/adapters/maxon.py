"""maxon search and product-page CAD/datasheet acquisition.

The en-us search embeds product records in Next.js data; its product links and
images belong to the US shop. Downloads remain subject to the vendor's terms.
"""
from __future__ import annotations

import html
import json
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from services.browser.cad_validate import classify_artifact, extract_cad, validate_cad
from services.browser.challenge import classify_challenge

from .. import session as _session
from . import (abs_url, download_path, fail, is_login_wall, login_required, make_hit,
               provenance_envelope, quote_query, register, source_failure)
from ._retail import _price

SITE = "maxon"
BASE_URL = "https://www.maxongroup.com"
SHOP_URL = "https://www.maxongroup.us"
SEARCH_URL_TMPL = BASE_URL + "/en-us/search?q={query}"
LICENSE_NOTE = (
    "maxon CAD models and datasheets are fetched under the user's own "
    "authenticated maxon session for their own engineering use and are not "
    "redistributed. This adapter downloads only under the user's own "
    "authenticated maxon session. See contracts/license_debt.md."
)
_LOGIN_WALL_MARKERS = ('Log in to maxon', 'id="loginForm"', 'class="login-form"',
                       'Please sign in to download')
_NEXT_DATA_RE = re.compile(
    r'<script\b(?=[^>]*\bid\s*=\s*["\']__NEXT_DATA__["\'])[^>]*>(?P<json>.*?)</script\s*>',
    re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*(["\'])(?P<href>.*?)\1[^>]*>(?P<text>.*?)</a\s*>',
    re.IGNORECASE | re.DOTALL)
_CAD_EXTENSIONS = {"step", "stp", "iges", "igs", "stl", "obj", "3mf", "zip", "rar"}
# Even unsupported native files count when deciding whether an archive is ambiguous.
_CAD_MEMBERS = {".step", ".stp", ".iges", ".igs", ".stl", ".obj", ".3mf",
                ".sldprt", ".sldasm", ".catpart", ".catproduct", ".ipt", ".iam",
                ".prt", ".asm", ".sat", ".x_t", ".dwg", ".dxf", ".fcstd", ".zip", ".rar", ".7z"}
_ALIASES = {"stp": "step", "igs": "iges"}


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _usd_price(preis: str) -> float | None:
    """Pull a numeric USD price from maxon's currency-tagged Preis field."""
    if not isinstance(preis, str):
        return None
    match = re.search(r"(?:^|\|)USD~~[^=|]*=([^|]+)", preis, re.IGNORECASE)
    return _price(match.group(1)) if match else None


def _category(record: dict) -> str | None:
    cats = [record.get(f"Kategorie{i}") for i in range(1, 5)]
    cats = [c.strip() for c in cats if isinstance(c, str) and c.strip()]
    return " / ".join(cats) if cats else None


class _CardFragments(HTMLParser):
    """Keep image/title/price association within each actual search card."""

    _VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.cards: list[str] = []
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if not self._depth:
            classes = dict(attrs).get("class") or ""
            if tag not in {"li", "article", "div"} or not re.search(
                    r"(?:^|\s)(?:product-item|product-card|search-result)(?:\s|$)", classes, re.I):
                return
            self._depth = 1
            self._parts = [self.get_starttag_text()]
        else:
            self._parts.append(self.get_starttag_text())
            if tag not in self._VOID:
                self._depth += 1

    def handle_startendtag(self, tag, attrs):
        if self._depth:
            self._parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self._depth and tag not in self._VOID:
            self._parts.append(f"</{tag}>")
            self._depth -= 1
            if not self._depth:
                self.cards.append("".join(self._parts))

    def handle_data(self, data):
        if self._depth:
            self._parts.append(data)

    def handle_entityref(self, name):
        if self._depth:
            self._parts.append(f"&{name};")

    def handle_charref(self, name):
        if self._depth:
            self._parts.append(f"&#{name};")


def _card_fragments(content: str) -> list[str]:
    parser = _CardFragments()
    parser.feed(content)
    return parser.cards


class MotorAdapter:
    """Product-specific download handling shared by the three motor vendors."""

    key: str
    license_note: str
    login_markers: tuple[str, ...]

    def _page_failure(self, op: str, url: str, content: str, *, check_login: bool = True) -> dict | None:
        challenge = classify_challenge(content, url)
        if challenge == "captcha" or "recaptcha verification failed" in content.lower():
            return fail(self.key, op, "captcha", "The vendor presented a captcha challenge.", url=url)
        if re.search(r"\b(?:access denied|request blocked|automated access denied)\b", content, re.I):
            return fail(self.key, op, "blocked", "The vendor blocked the request.", url=url)
        if challenge == "2fa" or (check_login and is_login_wall(content, self.login_markers)):
            question = login_required(self.key, url)
            return fail(self.key, op, "login_required", question["hint"], url=url,
                        ask_user=question.get("ask_user"))
        return None

    def acquire(self, result: dict) -> dict:
        url = abs_url("", result.get("url"))
        if not url:
            return fail(self.key, "acquire", "not_found", "The result has no valid product URL.")
        if result.get("kind") not in (None, "product", "model"):
            return fail(self.key, "acquire", "unsupported",
                        "Select a specific product/configuration before downloading.", url=url)
        requested = result.get("requested_format") or result.get("format") or ""
        if not isinstance(requested, str):
            return fail(self.key, "acquire", "unsupported", "Invalid requested format.", url=url)
        requested = _ALIASES.get(requested.lower().lstrip("."), requested.lower().lstrip("."))
        if requested and requested not in {"step", "iges", "stl", "obj", "3mf", "zip", "pdf"}:
            return fail(self.key, "acquire", "unsupported", "The requested format is not supported.", url=url)

        page = _session.browser_fetch(url, self.key)
        if not page.get("ok"):
            return source_failure(self.key, "acquire", page, url=url)
        content = page.get("content") or ""
        page_url = page.get("final_url") or url
        wall = self._page_failure("acquire", page_url, content)
        if wall:
            return wall

        links = []
        seen = set()
        for match in _ANCHOR_RE.finditer(content):
            link = abs_url(page_url, html.unescape(match.group("href")))
            if not link or link in seen:
                continue
            ext = Path(urlsplit(link).path).suffix.lower().lstrip(".")
            if ext in _CAD_EXTENSIONS or ext == "pdf":
                links.append((link, ext, _strip_tags(match.group("text"))))
                seen.add(link)
        cad = [item for item in links if item[1] in _CAD_EXTENSIONS]
        documents = [item for item in links if item[1] == "pdf"]
        selected_url = result.get("cad_url") or result.get("download_url")
        if requested == "pdf" or (not requested and not cad):
            candidates = documents
            # Prefer actual data/catalog sheets over unrelated manuals/forms.
            sheets = [item for item in candidates if re.search(
                r"data.?sheet|spec(?:ification)?|catalog", item[0] + " " + item[2], re.I)]
            if sheets and not selected_url:
                candidates = sheets
        else:
            candidates = cad
            if requested:
                candidates = [item for item in cad if _ALIASES.get(item[1], item[1]) == requested
                              or (item[1] in {"zip", "rar"} and requested not in {"pdf", "zip"})]
        if selected_url:
            selected_url = abs_url(page_url, selected_url)
            candidates = [item for item in candidates if item[0] == selected_url]
        if len(candidates) > 1:
            identity = str(result.get("sku") or result.get("mpn") or "").strip()
            if identity:
                identity_re = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(identity) +
                                         r"(?![A-Za-z0-9_-])", re.I)
                exact = [item for item in candidates if identity_re.search(item[0] + " " + item[2])]
                if len(exact) == 1:
                    candidates = exact
        if not candidates:
            return fail(self.key, "acquire", "not_found", "No matching download link was found.", url=page_url)
        if len(candidates) != 1:
            return fail(self.key, "acquire", "unsupported",
                        "Several downloads are available; select the exact product and download URL.", url=page_url)
        download_url, ext, _ = candidates[0]
        if ext == "rar":
            return fail(self.key, "acquire", "unsupported",
                        "This vendor supplies a RAR archive, which cannot be inspected for CAD here.",
                        url=download_url)
        dest, refusal = download_path(self.key, download_url, default="download." + ext)
        if refusal is not None:
            return source_failure(self.key, "acquire", refusal, url=download_url)
        outcome = _session.browser_download(download_url, self.key, dest)
        if not outcome.get("ok"):
            return source_failure(self.key, "acquire", outcome, url=download_url)
        path = outcome.get("dest") or str(dest)
        details: dict = {}
        if ext == "pdf":
            checked = classify_artifact(path, kind="document")
            if not checked.get("ok"):
                return fail(self.key, "acquire", "parse_failed", checked.get("reason", "Invalid PDF."), url=download_url)
            kind, fmt = "document", "pdf"
        else:
            wanted = requested if requested not in {"", "zip"} else None
            checked = validate_cad(path, requested_format=wanted)
            ambiguous_archive = ext == "zip" and bool(checked.get("candidates"))
            if (not checked.get("ok") and not ambiguous_archive) or (
                    wanted and not ambiguous_archive and checked.get("format") != wanted):
                return fail(self.key, "acquire", "parse_failed",
                            checked.get("reason") or "The requested CAD format was not found.", url=download_url)
            if ambiguous_archive and wanted:
                return fail(self.key, "acquire", "unsupported",
                            "The archive has multiple CAD models; select a configuration.", url=download_url)
            kind, fmt = ("archive", "zip") if ambiguous_archive else ("cad", checked["format"])
            if ext == "zip":
                # validate_cad has already bounded and checked every archive member's path.
                try:
                    with zipfile.ZipFile(path) as archive:
                        members = [entry for entry in archive.infolist() if not entry.is_dir()
                                   and Path(entry.filename).suffix.lower() in _CAD_MEMBERS]
                except (OSError, zipfile.BadZipFile) as exc:
                    return fail(self.key, "acquire", "parse_failed", str(exc), url=download_url)
                if requested == "zip" or len(members) != 1:
                    kind, fmt = "archive", "zip"
                    if len(members) != 1:
                        details["archive_note"] = "Multiple CAD files; select the correct configuration manually."
                else:
                    extracted = extract_cad(path, requested_format=wanted)
                    if not extracted.get("ok"):
                        return fail(self.key, "acquire", "parse_failed", extracted.get("reason", "Invalid CAD."),
                                    url=download_url)
                    details["archive_path"] = path
                    details["archive_member"] = extracted.get("member")
                    path = extracted["artifact_path"]
                    fmt = extracted["format"]
        return provenance_envelope(
            source_url=url, session_profile_used=outcome.get("session_profile_used", self.key),
            license_note=self.license_note, artifact_path=str(path), artifact_kind=kind, format=fmt,
            title=result.get("title", ""), sku=result.get("sku"), mpn=result.get("mpn"),
            manufacturer=result.get("manufacturer"), **details)


class MaxonAdapter(MotorAdapter):
    key = SITE
    hosts = ("maxongroup.com", "maxongroup.us")
    license_note = LICENSE_NOTE
    login_markers = _LOGIN_WALL_MARKERS

    def search(self, query: str) -> list[dict]:
        url = SEARCH_URL_TMPL.format(query=quote_query(query))
        result = _session.browser_fetch(url, SITE)
        if not result.get("ok"):
            return [source_failure(SITE, "search", result, url=url)]
        content = result.get("content") or ""
        match = _NEXT_DATA_RE.search(content)
        wall = self._page_failure("search", result.get("final_url") or url, content, check_login=not bool(match))
        if wall:
            return [wall]
        try:
            data = json.loads(match.group("json")) if match else None
            products = data["props"]["pageProps"]["productSearchData"]["products"]
            if not isinstance(products, list):
                raise ValueError("products is not a list")
        except (TypeError, ValueError, KeyError):
            return [fail(SITE, "search", "parse_failed", "The product search data is missing or malformed.", url=url)]
        hits = []
        for entry in products:
            record = entry.get("record") if isinstance(entry, dict) else None
            if not isinstance(record, dict):
                continue
            name, link = record.get("Name"), record.get("Deeplink")
            if not isinstance(name, str) or not name.strip() or not isinstance(link, str):
                continue
            product_url = abs_url(SHOP_URL, link)
            if not product_url:
                continue
            pn = record.get("Artikelnummer")
            pn = str(pn).strip() if pn is not None else None
            price = _usd_price(record.get("Preis"))
            image = record.get("ImageURL")
            thumbnail = abs_url(SHOP_URL, image) if isinstance(image, str) else None
            category = _category(record)
            hits.append(make_hit(SITE, title=html.unescape(name).strip(), url=product_url,
                                 kind="product", sku=pn or None, mpn=pn or None, manufacturer="maxon",
                                 price=price, currency="USD" if price is not None else None,
                                 thumbnail=thumbnail, extra={"category": category} if category else None))
        if products and not hits:
            return [fail(SITE, "search", "parse_failed", "No usable product records were found.", url=url)]
        return hits


ADAPTER = register(MaxonAdapter())