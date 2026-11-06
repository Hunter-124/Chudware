"""Parker catalog search and unambiguous, validated artifact acquisition.

The small industrial adapter base is shared by the hydraulic and linear vendors:
site-specific result parsing stays in each adapter, while download selection and
validation have the same safety requirements for all four.
"""
from __future__ import annotations

import html
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .. import challenge, session as _session
from ..cad_validate import classify_artifact, validate_cad
from . import (abs_url, download_path, fail, login_required, make_hit,
               provenance_envelope, quote_query, register, source_failure)
from ._retail import (_is_captcha, _price, _stock, card_blocks, card_image,
                      parse_jsonld_products, visible_text)

SITE = "parker"
BASE_URL = "https://www.parker.com"
SEARCH_URL_TMPL = (BASE_URL + "/us/en/search?searchbox={query}&selectCategory=products"
                   "&siteSectionEN_fq=p-939117180")
LICENSE_NOTE = ("Parker CAD models and datasheets are fetched under the user's own "
                "session for engineering use and are not redistributed. "
                "See contracts/license_debt.md.")

_RESULT_RE = re.compile(
    r'<a\b(?=[^>]*\bclass\s*=\s*["\'][^"\']*\bnav-link-text\b)'
    r'(?=[^>]*\bhref\s*=\s*["\'](?P<href>[^"\']+)["\'])[^>]*>'
    r'.*?<div\b[^>]*\bclass\s*=\s*["\'][^"\']*SelectorContainerInner-1[^"\']*["\'][^>]*>'
    r'(?P<title>.*?)</div>', re.I | re.S)


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def _field(block: str, *names: str) -> str | None:
    """Read an explicitly labelled card field, never a neighbouring card."""
    for name in names:
        escaped = re.escape(name)
        attr = re.search(rf'(?<![\w-])data-{escaped}\s*=\s*["\']([^"\']+)["\']', block, re.I)
        if attr:
            return html.unescape(attr.group(1)).strip() or None
        element = re.search(
            rf'<(?P<tag>[\w:-]+)\b(?=[^>]*\b(?:class|itemprop)\s*=\s*["\']'
            rf'(?:[^"\']*\s)?{escaped}(?:\s[^"\']*)?["\'])[^>]*>(?P<value>.*?)</(?P=tag)\s*>',
            block, re.I | re.S)
        if element:
            return _text(element.group("value")) or None
        meta = re.search(
            rf'<(?:meta|input)\b(?=[^>]*\bitemprop\s*=\s*["\']{escaped}["\'])'
            rf'(?=[^>]*\bcontent\s*=\s*["\'](?P<value>[^"\']+)["\'])[^>]*>',
            block, re.I)
        if meta:
            return html.unescape(meta.group("value")).strip() or None
    return None


def _card_hit(site: str, block: str, base: str, href: str, title: str, *,
              kind: str, manufacturer: str) -> dict | None:
    url = abs_url(base, html.unescape(href))
    title = _text(title)
    if not url or not title:
        return None
    products = parse_jsonld_products(block)
    product = next((item for item in products if item.get("url") and
                    abs_url(base, item["url"]) == url), None)
    if product is None and len(products) == 1 and not products[0].get("url"):
        product = products[0]
    product = product or {}
    raw_price = _field(block, "price") or product.get("price")
    currency = _field(block, "currency", "priceCurrency") or product.get("currency")
    formats = []
    if kind in {"product", "series", "model"}:
        parser = _DownloadLinks()
        parser.feed(block)
        for link, _ in parser.links:
            fmt = _format(link)
            if fmt and fmt not in {"pdf", "zip"} and fmt not in formats:
                formats.append(fmt)
    return make_hit(site, title=title, url=url, kind=kind,
                    sku=_field(block, "sku", "part-number") or product.get("sku"),
                    mpn=_field(block, "mpn", "manufacturer-part-number") or product.get("mpn"),
                    manufacturer=_field(block, "manufacturer") or product.get("brand") or manufacturer,
                    price=_price(raw_price), currency=currency,
                    stock=_stock(_field(block, "stock", "inventoryLevel") or product.get("stock")),
                    material=_field(block, "material") or product.get("material"),
                    model_formats=formats, model_available=True if formats else None,
                    thumbnail=(card_image(block, base) or abs_url(base, product.get("image")))
                    if kind in {"product", "series", "model"} else None)


def _kind_from_title(title: str, default: str = "product") -> str:
    return "series" if re.search(r"\b(?:series|family)\b", _text(title), re.I) else default


class _DownloadLinks(HTMLParser):
    """Collect links outside related/recommended-product sections."""
    _void = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.current: list[str] | None = None
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        blocked = bool(self.stack and self.stack[-1][1]) or bool(re.search(
            r"(?:^|[\s_-])(?:related|recommended|similar|suggested)(?:$|[\s_-])",
            (fields.get("class") or "") + " " + (fields.get("id") or ""), re.I))
        if tag == "a" and fields.get("href") and not blocked:
            self.current = [fields["href"], ""]
        if tag not in self._void:
            self.stack.append((tag, blocked))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            self.links.append((self.current[0], self.current[1].strip()))
            self.current = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


_FORMATS = {".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges",
            ".stl": "stl", ".3mf": "3mf", ".obj": "obj", ".zip": "zip", ".pdf": "pdf"}


def _format(url: str) -> str | None:
    try:
        return _FORMATS.get(Path(urlsplit(url).path).suffix.lower())
    except ValueError:
        return None


class _IndustrialAdapter:
    """Shared flow for these manufacturer sites; never choose a variant by position."""
    key = ""
    base_url = ""
    search_url_tmpl = ""
    manufacturer = ""
    license_note = ""
    distributor_url = ""
    distributor_only = False
    login_markers: tuple[str, ...] = ()

    def _wall(self, content: str, url: str, op: str) -> dict | None:
        if _is_captcha(url, content):
            return fail(self.key, op, "captcha", "The site returned a CAPTCHA challenge.", url=url)
        kind = challenge.classify_challenge(content, url)
        if re.search(r"<(?:title|h1)\b[^>]*>\s*(?:access denied|403 forbidden)", content, re.I):
            return fail(self.key, op, "blocked", "The site denied access.", url=url)
        path = urlsplit(url).path.lower()
        auth_url = re.search(r"/(?:sign-?in|log-?in|auth)(?:/|$)", path)
        auth_heading = re.search(r"<(?:title|h1)\b[^>]*>\s*(?:sign|log)[ -]?in\b", content, re.I)
        auth_page = challenge.is_login_page(content, url) and bool(auth_url or auth_heading)
        marker = any(item.lower() in content.lower() for item in self.login_markers)
        if kind != "2fa" and not auth_page and marker:
            # Many pages carry a sign-in panel in site chrome. A usable result
            # card or download link means that panel is not a page-level wall.
            if op == "search" and self.parse_search(content, url):
                return None
            if op == "acquire":
                parser = _DownloadLinks()
                parser.feed(content)
                if any(_format(href) for href, _ in parser.links):
                    return None
        if kind != "2fa" and not auth_page and not marker:
            return None
        if self.distributor_only or self.distributor_url:
            result = fail(self.key, op, "unsupported",
                          "This manufacturer sells through distributors; individual sign-in is not available.",
                          url=url)
            if self.distributor_url:
                result["distributors_url"] = self.distributor_url
            return result
        return source_failure(self.key, op, login_required(self.key, url), url=url)

    def search(self, query: str) -> list[dict]:
        url = self.search_url_tmpl.format(query=quote_query(query))
        page = _session.browser_fetch(url, self.key)
        if not page.get("ok"):
            return [source_failure(self.key, "search", page, url=url)]
        content = page.get("content") or ""
        base = page.get("final_url") or url
        wall = self._wall(content, base, "search")
        if wall:
            return [wall]
        hits = self.parse_search(content, base)
        if not hits:
            # Structured product records are a useful fallback when a site has no SSR cards.
            for item in parse_jsonld_products(content):
                target = abs_url(base, item.get("url"))
                if not target or not item.get("title"):
                    continue
                hits.append(make_hit(
                    self.key, title=_text(item["title"]), url=target,
                    kind=_kind_from_title(item["title"]), sku=item.get("sku"), mpn=item.get("mpn"),
                    manufacturer=item.get("brand") or self.manufacturer,
                    price=_price(item.get("price")), currency=item.get("currency"),
                    stock=_stock(item.get("stock")), material=item.get("material"),
                    thumbnail=abs_url(base, item.get("image"))))
        if hits:
            return hits
        if re.search(r"\b(?:no results|no products found|0 results)\b", visible_text(content), re.I):
            return []
        return [fail(self.key, "search", "parse_failed", "No recognizable search results on the page.", url=url)]

    def acquire(self, result: dict) -> dict:
        url = result.get("url") or ""
        if not abs_url(self.base_url, url) or not url.startswith(("http://", "https://")):
            return fail(self.key, "acquire", "not_found", "Result has no absolute product URL.", url=url or None)
        if result.get("kind") in {"category", "page", "project"}:
            return fail(self.key, "acquire", "unsupported", "Select a specific product or document first.", url=url)

        if _format(url) and result.get("kind") in {"model", "document"}:
            links = [(url, "")]
        else:
            page = _session.browser_fetch(url, self.key)
            if not page.get("ok"):
                return source_failure(self.key, "acquire", page, url=url)
            base = page.get("final_url") or url
            content = page.get("content") or ""
            wall = self._wall(content, base, "acquire")
            if wall:
                return wall
            parser = _DownloadLinks()
            parser.feed(content)
            links = [(absolute, label) for href, label in parser.links
                     if (absolute := abs_url(base, href)) and _format(absolute)]

        # Identical repeated links are common; different links are never silently
        # treated as equivalent configurations. A caller may choose an exact URL.
        unique = dict(links)
        selected_url = result.get("download_url")
        if selected_url:
            selected_url = abs_url(url, selected_url)
            unique = {u: label for u, label in unique.items() if u == selected_url}
            if not unique:
                return fail(self.key, "acquire", "not_found", "The selected download is not on this page.", url=url)
        requested = str(result.get("format") or "").lower().lstrip(".")
        requested = {"stp": "step", "igs": "iges"}.get(requested, requested)
        if result.get("kind") == "document" and requested and requested != "pdf":
            return fail(self.key, "acquire", "unsupported", "The selected result is a document, not CAD.", url=url)
        if result.get("kind") == "series" and not selected_url:
            identifier = result.get("sku") or result.get("mpn")
            if identifier:
                exact = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(str(identifier)) + r"(?![A-Za-z0-9_-])", re.I)
                cad_links = {u: label for u, label in unique.items() if _format(u) != "pdf"}
                matched = {u: label for u, label in cad_links.items() if exact.search(u + " " + label)}
                if cad_links and not matched and requested != "pdf":
                    return fail(self.key, "acquire", "unsupported",
                                "No CAD link identifies the selected configuration.", url=url)
                unique = {u: label for u, label in unique.items()
                          if _format(u) == "pdf" or u in matched}
            elif requested and requested != "pdf":
                return fail(self.key, "acquire", "unsupported",
                            "Select a specific configuration before downloading CAD.", url=url)
            else:
                documents = {u: label for u, label in unique.items() if _format(u) == "pdf"}
                if not documents and unique:
                    return fail(self.key, "acquire", "unsupported",
                                "Select a specific configuration before downloading CAD.", url=url)
                unique = documents
        elif result.get("kind") == "product" and not selected_url and requested != "pdf":
            identifier = result.get("sku") or result.get("mpn")
            if identifier:
                exact = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(str(identifier)) + r"(?![A-Za-z0-9_-])", re.I)
                cad_links = {u: label for u, label in unique.items() if _format(u) != "pdf"}
                matched = {u: label for u, label in cad_links.items() if exact.search(u + " " + label)}
                if cad_links and not matched and not exact.search(url):
                    return fail(self.key, "acquire", "unsupported",
                                "The CAD link cannot be tied to the selected product configuration.", url=url)
                if matched:
                    unique = {u: label for u, label in unique.items()
                              if _format(u) == "pdf" or u in matched}
        if requested:
            unique = {u: label for u, label in unique.items() if _format(u) == requested}
            if not unique:
                return fail(self.key, "acquire", "unsupported", "The requested format is not linked on this page.", url=url)
        elif result.get("kind") == "document":
            unique = {u: label for u, label in unique.items() if _format(u) == "pdf"}
        else:
            cad = {u: label for u, label in unique.items() if _format(u) != "pdf"}
            if cad:
                unique = cad
            else:
                unique = {u: label for u, label in unique.items() if _format(u) == "pdf"}

        # A page can include datasheets for other products. Prefer only an
        # explicitly labelled PDF of this page, or an identifier-matching link.
        if len(unique) > 1:
            identifier = result.get("sku") or result.get("mpn")
            if identifier:
                exact = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(str(identifier)) + r"(?![A-Za-z0-9_-])", re.I)
                matched = {u: label for u, label in unique.items() if exact.search(u + " " + label)}
                if matched:
                    unique = matched
            if len(unique) > 1 and all(_format(u) == "pdf" for u in unique):
                page_docs = {u: label for u, label in unique.items()
                             if re.search(r"\b(?:pdf of (?:this )?page|this (?:product|page))\b", label, re.I)}
                if len(page_docs) == 1:
                    unique = page_docs
        if not unique:
            return fail(self.key, "acquire", "not_found", "No unambiguous downloadable artifact is linked on this page.", url=url)
        if len(unique) != 1:
            return fail(self.key, "acquire", "unsupported", "Multiple downloads are available; select the exact configuration and format.", url=url)
        download_url = next(iter(unique))
        fmt = _format(download_url)
        dest, refusal = download_path(self.key, download_url, default="download." + fmt)
        if refusal is not None:
            refused = fail(self.key, "acquire", "policy_refused", refusal.get("hint", ""), url=download_url)
            refused["detail"] = refusal.get("reason") or "unsafe_filename"
            return refused

        # Stage and validate before replacing any previously downloaded artifact.
        stage = None
        try:
            with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".industrial-", suffix=dest.suffix,
                                             delete=False) as temp:
                stage = Path(temp.name)
            outcome = _session.browser_download(download_url, self.key, stage)
            if not outcome.get("ok"):
                return source_failure(self.key, "acquire", outcome, url=download_url)
            if fmt in {"pdf", "zip"}:
                checked = classify_artifact(stage, kind="document" if fmt == "pdf" else "archive")
                artifact_kind = "document" if fmt == "pdf" else "archive"
            else:
                checked = validate_cad(stage)
                artifact_kind = "cad"
            if not checked.get("ok"):
                return fail(self.key, "acquire", "parse_failed",
                            "Downloaded artifact failed validation: " + str(checked.get("reason") or "invalid content"),
                            url=download_url)
            os.replace(stage, dest)
        except OSError as exc:
            return fail(self.key, "acquire", "transport_failed", str(exc), url=download_url)
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)
        return provenance_envelope(source_url=url, session_profile_used=outcome.get("session_profile_used", self.key),
                                   license_note=self.license_note, artifact_path=str(dest),
                                   artifact_kind=artifact_kind, format=fmt,
                                   title=result.get("title", ""), manufacturer=result.get("manufacturer") or self.manufacturer)


class ParkerAdapter(_IndustrialAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    manufacturer = "Parker"
    license_note = LICENSE_NOTE
    login_markers = ("Please log in", "Log in to download", "Sign in to your account")
    result_kinds = ("product", "series")

    def parse_search(self, content: str, base: str) -> list[dict]:
        blocks = (card_blocks(content, "div", "product-card") or
                  card_blocks(content, "div", "search-result") or
                  card_blocks(content, "a", "nav-link-text"))
        hits = []
        for block in blocks:
            match = _RESULT_RE.search(block)
            if match:
                hit = _card_hit(self.key, block, base, match.group("href"), match.group("title"),
                                kind=_kind_from_title(match.group("title")), manufacturer=self.manufacturer)
                if hit:
                    hits.append(hit)
        return hits


ADAPTER = register(ParkerAdapter())
