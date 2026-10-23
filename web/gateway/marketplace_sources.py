"""Compliant third-party model discovery and import for seller studio."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from config import settings
from db import tx
from limits import api_writes, uploads
from marketplace_authz import (
    MarketplaceError,
    marketplace_enabled_or_404,
    require_seller,
    require_user,
)
from marketplace_schema import (
    IMAGE_EXTENSIONS,
    SOURCE_EXTENSIONS,
    normalize_tags,
    safe_media_type,
    slugify,
)
from provider_keys import _key_bytes

router = APIRouter(prefix="/api/studio", tags=["marketplace-sources"])

_USER_AGENT = "Chudware-Marketplace/1.0 (+https://chudware.com; compliant model importer)"
_HTML_LIMIT = 2 * 1024 * 1024
_JSON_LIMIT = 2 * 1024 * 1024
_REDIRECT_LIMIT = 4
_TIMEOUT = httpx.Timeout(12.0, connect=5.0, read=8.0, write=8.0, pool=5.0)
_TOTAL_FETCH_SECONDS = 12.0
_SOURCE_ACCOUNT_VERSION = 1
_NONCE_BYTES = 12
_ONSHAPE_STATE_TTL = 10 * 60
_ONSHAPE_AUTHORIZE_URL = "https://oauth.onshape.com/oauth/authorize"
_ONSHAPE_TOKEN_URL = "https://oauth.onshape.com/oauth/token"


@dataclass(frozen=True)
class ExternalModel:
    title: str
    description: str
    author: str
    license_hint: str | None
    image_urls: list[str]
    download_url: str | None
    source_ref: str
    tags: list[str]
    extra: dict[str, Any]


MatchFn = Callable[[str], str | None]
FetchFn = Callable[[str, str, str], Awaitable[ExternalModel]]


@dataclass(frozen=True)
class Adapter:
    source_id: str
    kind: str
    page_domains: tuple[str, ...]
    request_domains: tuple[str, ...]
    asset_domains: tuple[str, ...]
    credential_setting: str | None
    match: MatchFn
    fetch: FetchFn


@dataclass(frozen=True)
class _PinnedUrl:
    url: str
    connect_url: str
    host: str


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.links: list[tuple[set[str], str]] = []
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.json_ld_blocks: list[str] = []
        self._in_title = False
        self._in_json_ld = False
        self._script_parts: list[str] = []
        self._text_chars = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        tag = tag.lower()
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").strip().lower()
            content = values.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "link":
            rel = {part.lower() for part in values.get("rel", "").split()}
            href = values.get("href", "").strip()
            if rel and href:
                self.links.append((rel, href))
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and values.get("type", "").lower().split(";", 1)[0].strip() == "application/ld+json":
            self._in_json_ld = True
            self._script_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            block = "".join(self._script_parts).strip()
            if block:
                self.json_ld_blocks.append(block)
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._script_parts.append(data)
            return
        value = " ".join(data.split())
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        if self._text_chars < 200_000:
            self.text_parts.append(value)
            self._text_chars += len(value)


_LICENSE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CC-BY-NC-4.0", re.compile(r"creativecommons\.org/licenses/by-nc/4\.0|\bcc[ -]?by[ -]?nc[ -]?4\.0\b", re.I)),
    ("CC-BY-ND-4.0", re.compile(r"creativecommons\.org/licenses/by-nd/4\.0|\bcc[ -]?by[ -]?nd[ -]?4\.0\b", re.I)),
    ("CC-BY-SA-4.0", re.compile(r"creativecommons\.org/licenses/by-sa/4\.0|\bcc[ -]?by[ -]?sa[ -]?4\.0\b", re.I)),
    ("CC-BY-4.0", re.compile(r"creativecommons\.org/licenses/by/4\.0|\bcc[ -]?by[ -]?4\.0\b", re.I)),
    ("CC0-1.0", re.compile(r"creativecommons\.org/publicdomain/zero/1\.0|\bcc0[ -]?1\.0\b", re.I)),
    ("Apache-2.0", re.compile(r"\bApache(?: License)?[ -]?2\.0\b|spdx\.org/licenses/Apache-2\.0", re.I)),
    ("GPL-3.0-or-later", re.compile(r"\bGPL(?:v|[ -])?3(?:\.0)?(?: or later|\+)?\b|GPL-3\.0-or-later", re.I)),
    ("CERN-OHL-P-2.0", re.compile(r"\bCERN[ -]OHL[ -]P[ -]?2\.0\b", re.I)),
    ("CERN-OHL-W-2.0", re.compile(r"\bCERN[ -]OHL[ -]W[ -]?2\.0\b", re.I)),
    ("CERN-OHL-S-2.0", re.compile(r"\bCERN[ -]OHL[ -]S[ -]?2\.0\b", re.I)),
    ("MIT", re.compile(r"\bMIT License\b|spdx\.org/licenses/MIT(?:\.html)?\b", re.I)),
)


def _detect_license(*values: object) -> str | None:
    text = "\n".join(str(value) for value in values if value)
    for license_id, pattern in _LICENSE_PATTERNS:
        if pattern.search(text):
            return license_id
    return None


def _clean_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int, float))]
    return []


def _walk_json_ld(value: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    pending = [value]
    while pending and len(found) < 2_000:
        current = pending.pop()
        if isinstance(current, dict):
            found.append(current)
            pending.extend(list(current.values())[:2_000])
        elif isinstance(current, list):
            pending.extend(current[:2_000])
        if len(pending) > 4_000:
            del pending[:-4_000]
    return found


def _json_ld_fields(parser: _MetadataParser) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    for block in parser.json_ld_blocks:
        try:
            nodes.extend(_walk_json_ld(json.loads(block)))
        except (TypeError, ValueError):
            continue
        if len(nodes) >= 2_000:
            nodes = nodes[:2_000]
            break
    preferred = next(
        (node for node in nodes if any(kind.lower() in {"product", "creativework", "3dmodel", "mediaobject"} for kind in _strings(node.get("@type")))),
        nodes[0] if nodes else {},
    )
    author_value = preferred.get("author") or preferred.get("creator")
    author = (author_value.get("name") or author_value.get("alternateName") or "") if isinstance(author_value, dict) else (author_value or "")
    image_value = preferred.get("image") or preferred.get("thumbnailUrl") or []
    images: list[str] = []
    for item in image_value if isinstance(image_value, list) else [image_value]:
        if isinstance(item, dict):
            item = item.get("url") or item.get("contentUrl")
        if isinstance(item, str) and item:
            images.append(item)
    keywords = preferred.get("keywords") or []
    if isinstance(keywords, str):
        keywords = re.split(r"[,;]", keywords)
    return {
        "title": preferred.get("name") or preferred.get("headline") or "",
        "description": preferred.get("description") or "", "author": author,
        "images": images, "license": preferred.get("license") or preferred.get("usageInfo") or "",
        "tags": keywords if isinstance(keywords, list) else [], "raw": nodes,
    }


async def _pin_url(
    url: str, expected_domains: tuple[str, ...]
) -> _PinnedUrl:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise MarketplaceError(
            400, "invalid_source_url", "the source URL is malformed"
        ) from exc
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise MarketplaceError(
            400,
            "invalid_source_url",
            "source URLs must use public HTTPS hosts",
        )
    if port not in (None, 443):
        raise MarketplaceError(
            400,
            "invalid_source_url",
            "source URLs must use the standard HTTPS port",
        )
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise MarketplaceError(
            400, "invalid_source_url", "the source host is invalid"
        ) from exc
    if expected_domains and not any(
        host == suffix or host.endswith("." + suffix)
        for suffix in expected_domains
    ):
        raise MarketplaceError(
            400,
            "invalid_source_url",
            "the URL does not belong to that source",
        )
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            443,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise MarketplaceError(
            400,
            "invalid_source_url",
            "the source host could not be resolved",
        ) from exc
    if not infos:
        raise MarketplaceError(
            400,
            "invalid_source_url",
            "the source host could not be resolved",
        )
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if (
            not address.is_global
            or address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise MarketplaceError(
                400,
                "invalid_source_url",
                "the source host resolves to a non-public address",
            )
        if address not in addresses:
            addresses.append(address)
    normalized = urlunsplit(
        ("https", host, parts.path or "/", parts.query, "")
    )
    address = addresses[0]
    connect_host = (
        f"[{address.compressed}]"
        if isinstance(address, ipaddress.IPv6Address)
        else address.compressed
    )
    connect_url = urlunsplit(
        ("https", connect_host, parts.path or "/", parts.query, "")
    )
    return _PinnedUrl(normalized, connect_url, host)


async def _validated_url(url: str, expected_domains: tuple[str, ...]) -> str:
    try:
        async with asyncio.timeout(_TOTAL_FETCH_SECONDS):
            return (await _pin_url(url, expected_domains)).url
    except TimeoutError as exc:
        raise MarketplaceError(
            400,
            "invalid_source_url",
            "the source host could not be resolved in time",
        ) from exc


def _pinned_request(
    client: httpx.AsyncClient,
    method: str,
    pinned: _PinnedUrl,
    headers: dict[str, str],
    content: bytes | str | None,
) -> httpx.Request:
    hop_headers = {
        name: value for name, value in headers.items() if name.lower() != "host"
    }
    hop_headers["Host"] = pinned.host
    request = client.build_request(
        method,
        pinned.connect_url,
        headers=hop_headers,
        content=content,
    )
    # httpcore 1.x consumes this extension when starting TLS, so certificate
    # verification remains bound to the source hostname rather than its IP.
    request.extensions["sni_hostname"] = pinned.host
    return request


async def _read_response(
    method: str,
    url: str,
    *,
    expected_domains: tuple[str, ...],
    headers: dict[str, str] | None = None,
    content: bytes | str | None = None,
    max_bytes: int,
    acceptable: tuple[int, ...] = (200,),
) -> tuple[bytes, str, httpx.Headers]:
    request_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        **(headers or {}),
    }
    current = url
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TOTAL_FETCH_SECONDS
    try:
        async with asyncio.timeout_at(deadline):
            for redirect_count in range(_REDIRECT_LIMIT + 1):
                pinned = await _pin_url(current, expected_domains)
                try:
                    # One client per hop prevents an IP-keyed TLS connection
                    # from being reused for a different redirect hostname.
                    async with httpx.AsyncClient(
                        timeout=_TIMEOUT,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        response = await client.send(
                            _pinned_request(
                                client,
                                method,
                                pinned,
                                request_headers,
                                content,
                            ),
                            stream=True,
                        )
                        try:
                            if response.status_code in (
                                301,
                                302,
                                303,
                                307,
                                308,
                            ):
                                location = response.headers.get("location", "")
                                if (
                                    redirect_count >= _REDIRECT_LIMIT
                                    or not location
                                ):
                                    raise MarketplaceError(
                                        502,
                                        "source_fetch_failed",
                                        "the source redirected too many times",
                                    )
                                current = urljoin(pinned.url, location)
                                if response.status_code == 303:
                                    method, content = "GET", None
                                continue
                            if response.status_code not in acceptable:
                                raise MarketplaceError(
                                    502,
                                    "source_fetch_failed",
                                    f"the source returned HTTP {response.status_code}",
                                )
                            declared = response.headers.get("content-length")
                            if declared:
                                try:
                                    too_large = int(declared) > max_bytes
                                except ValueError:
                                    too_large = False
                                if too_large:
                                    raise MarketplaceError(
                                        502,
                                        "source_fetch_failed",
                                        "the source response was too large",
                                    )
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                if loop.time() >= deadline:
                                    raise TimeoutError
                                body.extend(chunk)
                                if len(body) > max_bytes:
                                    raise MarketplaceError(
                                        502,
                                        "source_fetch_failed",
                                        "the source response was too large",
                                    )
                            return bytes(body), pinned.url, response.headers
                        finally:
                            await response.aclose()
                except httpx.HTTPError as exc:
                    raise MarketplaceError(
                        502,
                        "source_fetch_failed",
                        "the source did not respond",
                    ) from exc
    except TimeoutError as exc:
        raise MarketplaceError(
            502,
            "source_fetch_failed",
            "the source exceeded the total fetch deadline",
        ) from exc
    raise MarketplaceError(
        502,
        "source_fetch_failed",
        "the source redirected too many times",
    )


async def _json_request(url: str, *, expected_domains: tuple[str, ...], headers: dict[str, str] | None = None) -> tuple[Any, str]:
    body, final_url, response_headers = await _read_response("GET", url, expected_domains=expected_domains, headers=headers, max_bytes=_JSON_LIMIT)
    if "json" not in response_headers.get("content-type", "").lower():
        raise MarketplaceError(502, "source_fetch_failed", "the source API returned non-JSON content")
    try:
        return json.loads(body), final_url
    except (TypeError, ValueError) as exc:
        raise MarketplaceError(502, "source_fetch_failed", "the source API returned invalid JSON") from exc


async def _robots_allows(url: str, domains: tuple[str, ...]) -> bool:
    parts = urlsplit(url)
    robots_url = urlunsplit(("https", parts.netloc, "/robots.txt", "", ""))
    body, _, _ = await _read_response(
        "GET",
        robots_url,
        expected_domains=domains,
        headers={"Accept": "text/plain"},
        max_bytes=256 * 1024,
        acceptable=(200, 404),
    )
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(body.decode("utf-8", "replace").splitlines())
    return parser.can_fetch(_USER_AGENT, url)


async def _metadata_model(url: str, ref: str, domains: tuple[str, ...]) -> ExternalModel:
    if not await _robots_allows(url, domains):
        raise MarketplaceError(
            400,
            "source_robots_restricted",
            "the source robots policy does not permit metadata retrieval",
        )
    body, final_url, headers = await _read_response("GET", url, expected_domains=domains, headers={"Accept": "text/html,application/xhtml+xml"}, max_bytes=_HTML_LIMIT)
    content_type = headers.get("content-type", "").lower()
    if not (content_type.startswith("text/html") or content_type.startswith("application/xhtml+xml")):
        raise MarketplaceError(400, "source_not_html", "the source page is not HTML")
    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type)
    if match:
        charset = match.group(1).strip('"\'')
    parser = _MetadataParser()
    try:
        parser.feed(body.decode(charset, "replace"))
        parser.close()
    except Exception as exc:
        raise MarketplaceError(502, "source_fetch_failed", "the source page metadata could not be parsed") from exc
    robots = ",".join(
        (
            headers.get("x-robots-tag", ""),
            parser.meta.get("robots", ""),
            parser.meta.get("googlebot", ""),
        )
    ).lower()
    if any(directive in robots for directive in ("noindex", "nofollow", "none")):
        raise MarketplaceError(
            400,
            "source_robots_restricted",
            "the source page does not permit automated metadata use",
        )
    structured = _json_ld_fields(parser)
    canonical = urljoin(final_url, next((href for rel, href in parser.links if "canonical" in rel), final_url))
    title = parser.meta.get("og:title") or str(structured["title"]) or " ".join(parser.title_parts)
    description = parser.meta.get("og:description") or parser.meta.get("description") or str(structured["description"])
    author = parser.meta.get("author") or parser.meta.get("article:author") or parser.meta.get("og:article:author") or str(structured["author"])
    images = list(dict.fromkeys(urljoin(final_url, item) for item in [parser.meta.get("og:image", ""), *structured["images"]] if item))[:12]
    license_links = [href for rel, href in parser.links if "license" in rel]
    return ExternalModel(
        _clean_text(title, 140), _clean_text(description, 40_000), _clean_text(author, 200),
        _detect_license(*license_links, structured["license"], " ".join(parser.text_parts)), images, None, ref,
        normalize_tags(structured["tags"]),
        {"mode": "metadata", "canonical_url": canonical, "open_graph": dict(parser.meta), "json_ld": structured["raw"], "license_links": license_links},
    )


def _path_ref(url: str) -> str | None:
    path = urlsplit(url).path.strip("/")
    return path or None


def _regex_ref(pattern: str, url: str) -> str | None:
    match = re.search(pattern, urlsplit(url).path, re.I)
    return match.group(1) if match else None


def _github_ref(url: str) -> str | None:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() in {
        "about", "apps", "features", "marketplace", "orgs", "settings", "topics",
    }:
        return None
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def _onshape_ref(url: str) -> str | None:
    match = re.search(
        r"/documents/([0-9a-f]{24})/(w|v|m)/([0-9a-f]{24})(?:/e/([0-9a-f]{24}))?",
        urlsplit(url).path,
        re.I,
    )
    return ":".join(part or "" for part in match.groups()) if match else None


async def _fetch_metadata(url: str, ref: str, token: str) -> ExternalModel:
    del token
    host = (urlsplit(url).hostname or "").lower()
    adapter = next(
        (
            item
            for item in SOURCE_ADAPTERS.values()
            if item.match(url) == ref
            and any(host == domain or host.endswith("." + domain) for domain in item.page_domains)
        ),
        None,
    )
    if adapter is None:
        raise MarketplaceError(
            400, "invalid_source_url", "the URL does not match a supported source"
        )
    return await _metadata_model(url, ref, adapter.page_domains)


async def _fetch_link(url: str, ref: str, token: str) -> ExternalModel:
    del url, token
    return ExternalModel("", "", "", None, [], None, ref, [], {})


async def _fetch_github(url: str, ref: str, token: str) -> ExternalModel:
    del url
    owner, repo = ref.split("/", 1)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    raw, _ = await _json_request(
        base, expected_domains=("api.github.com",), headers=headers
    )
    license_raw: Any = {}
    release_raw: Any = {}
    try:
        license_raw, _ = await _json_request(
            f"{base}/license", expected_domains=("api.github.com",), headers=headers
        )
    except MarketplaceError as exc:
        if "HTTP 404" not in str(exc.detail):
            raise
    try:
        release_raw, _ = await _json_request(
            f"{base}/releases/latest",
            expected_domains=("api.github.com",),
            headers=headers,
        )
    except MarketplaceError as exc:
        if "HTTP 404" not in str(exc.detail):
            raise
    spdx_values = [
        str(value)
        for value in (
            license_raw.get("license", {}).get("spdx_id")
            if isinstance(license_raw, dict)
            else "",
            raw.get("license", {}).get("spdx_id")
            if isinstance(raw, dict) and isinstance(raw.get("license"), dict)
            else "",
        )
        if value
    ]
    seeded_ids = {license_id for license_id, _ in _LICENSE_PATTERNS}
    license_hint = next(
        (value for value in spdx_values if value in seeded_ids),
        _detect_license(*spdx_values),
    )
    assets = release_raw.get("assets", []) if isinstance(release_raw, dict) else []
    candidates = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and any(
            str(asset.get("name") or "").lower().endswith(ext)
            for ext in SOURCE_EXTENSIONS
        )
    ]
    candidate = candidates[0] if candidates else None
    owner_raw = raw.get("owner", {}) if isinstance(raw, dict) else {}
    return ExternalModel(
        _clean_text(raw.get("name") or repo, 140),
        _clean_text(raw.get("description"), 40_000),
        _clean_text(
            owner_raw.get("login") if isinstance(owner_raw, dict) else owner, 200
        ),
        license_hint,
        [str(owner_raw.get("avatar_url"))]
        if isinstance(owner_raw, dict) and owner_raw.get("avatar_url")
        else [],
        str(candidate.get("browser_download_url")) if candidate else None,
        ref,
        normalize_tags(raw.get("topics", [])),
        {
            "mode": "api",
            "repository": raw,
            "license": license_raw,
            "release": release_raw,
            "download_name": candidate.get("name") if candidate else "",
        },
    )


async def _fetch_thingiverse(url: str, ref: str, token: str) -> ExternalModel:
    del url
    base = f"https://api.thingiverse.com/things/{quote(ref, safe='')}"
    suffix = f"?access_token={quote(token, safe='')}"
    raw, _ = await _json_request(
        base + suffix, expected_domains=("api.thingiverse.com",)
    )
    files, _ = await _json_request(
        base + "/files" + suffix, expected_domains=("api.thingiverse.com",)
    )
    creator = raw.get("creator", {}) if isinstance(raw, dict) else {}
    images: list[str] = []
    for image in (
        raw.get("images", [])
        if isinstance(raw, dict) and isinstance(raw.get("images"), list)
        else []
    ):
        if isinstance(image, dict):
            value = image.get("url") or image.get("large") or image.get("medium")
            if value:
                images.append(str(value))
    file_rows = files if isinstance(files, list) else []
    candidate = next(
        (
            item
            for item in file_rows
            if isinstance(item, dict)
            and any(
                str(item.get("name") or "").lower().endswith(ext)
                for ext in SOURCE_EXTENSIONS
            )
        ),
        None,
    )
    return ExternalModel(
        _clean_text(raw.get("name"), 140),
        _clean_text(raw.get("description"), 40_000),
        _clean_text(creator.get("name") if isinstance(creator, dict) else "", 200),
        _detect_license(raw.get("license"), raw.get("license_url")),
        list(dict.fromkeys(images))[:12],
        str(candidate.get("download_url"))
        if candidate and candidate.get("download_url")
        else None,
        ref,
        normalize_tags(raw.get("tags")),
        {
            "mode": "api",
            "thing": raw,
            "files": files,
            "download_name": candidate.get("name") if candidate else "",
        },
    )


async def _fetch_sketchfab(url: str, ref: str, token: str) -> ExternalModel:
    del url
    headers = {"Authorization": f"Token {token}"}
    raw, _ = await _json_request(
        f"https://api.sketchfab.com/v3/models/{quote(ref, safe='')}",
        expected_domains=("api.sketchfab.com",),
        headers=headers,
    )
    license_raw = raw.get("license", {}) if isinstance(raw, dict) else {}
    license_hint = _detect_license(
        license_raw.get("slug") if isinstance(license_raw, dict) else "",
        license_raw.get("url") if isinstance(license_raw, dict) else "",
    )
    if (
        not raw.get("isDownloadable")
        or not license_hint
        or not license_hint.startswith("CC")
    ):
        raise MarketplaceError(
            400,
            "source_license_restricted",
            "Sketchfab imports must be downloadable under a detected Creative Commons license",
        )
    download_raw, _ = await _json_request(
        f"https://api.sketchfab.com/v3/models/{quote(ref, safe='')}/download",
        expected_domains=("api.sketchfab.com",),
        headers=headers,
    )
    chosen: dict[str, Any] | None = None
    for format_name in ("glb", "gltf", "source"):
        value = download_raw.get(format_name) if isinstance(download_raw, dict) else None
        if isinstance(value, dict) and value.get("url"):
            chosen = value
            break
    user_raw = raw.get("user", {}) if isinstance(raw, dict) else {}
    thumbnails = raw.get("thumbnails", {}) if isinstance(raw, dict) else {}
    images_raw = thumbnails.get("images", []) if isinstance(thumbnails, dict) else []
    images = [
        str(item.get("url"))
        for item in images_raw
        if isinstance(item, dict) and item.get("url")
    ]
    return ExternalModel(
        _clean_text(raw.get("name"), 140),
        _clean_text(raw.get("description"), 40_000),
        _clean_text(
            (user_raw.get("displayName") or user_raw.get("username"))
            if isinstance(user_raw, dict)
            else "",
            200,
        ),
        license_hint,
        list(dict.fromkeys(reversed(images)))[:12],
        str(chosen.get("url")) if chosen else None,
        ref,
        normalize_tags(raw.get("tags")),
        {
            "mode": "api",
            "model": raw,
            "download": download_raw,
            "download_name": "sketchfab-model.zip",
        },
    )


async def _fetch_myminifactory(url: str, ref: str, token: str) -> ExternalModel:
    del url
    endpoint = (
        f"https://www.myminifactory.com/api/v2/objects/{quote(ref, safe='')}"
        f"?key={quote(token, safe='')}"
    )
    raw, _ = await _json_request(
        endpoint, expected_domains=("myminifactory.com",)
    )
    designer = raw.get("designer", {}) if isinstance(raw, dict) else {}
    images: list[str] = []
    for image in (
        raw.get("images", [])
        if isinstance(raw, dict) and isinstance(raw.get("images"), list)
        else []
    ):
        if not isinstance(image, dict):
            continue
        for key in ("original", "standard", "thumbnail"):
            value = image.get(key)
            if isinstance(value, dict) and value.get("url"):
                images.append(str(value["url"]))
                break
    files = raw.get("files", []) if isinstance(raw, dict) else []
    candidate = next(
        (
            item
            for item in files
            if isinstance(item, dict)
            and item.get("download_url")
            and any(
                str(item.get("filename") or "").lower().endswith(ext)
                for ext in SOURCE_EXTENSIONS
            )
        ),
        None,
    )
    download_url = (
        str(candidate.get("download_url"))
        if candidate
        else (
            str(raw.get("archive_download_url"))
            if raw.get("archive_download_url")
            else None
        )
    )
    return ExternalModel(
        _clean_text(raw.get("name"), 140),
        _clean_text(raw.get("description"), 40_000),
        _clean_text(
            (designer.get("name") or designer.get("username"))
            if isinstance(designer, dict)
            else "",
            200,
        ),
        _detect_license(raw.get("license"), raw.get("licenses")),
        list(dict.fromkeys(images))[:12],
        download_url,
        ref,
        normalize_tags(raw.get("tags")),
        {
            "mode": "api",
            "object": raw,
            "download_name": candidate.get("filename")
            if candidate
            else "myminifactory-model.zip",
        },
    )


async def _fetch_nih3d(url: str, ref: str, token: str) -> ExternalModel:
    del token
    try:
        raw, _ = await _json_request(
            f"https://3d.nih.gov/api/entries/{quote(ref, safe='')}",
            expected_domains=("3d.nih.gov",),
        )
    except MarketplaceError:
        model = await _metadata_model(url, ref, ("3d.nih.gov",))
        return ExternalModel(
            model.title,
            model.description,
            model.author or "NIH 3D contributor",
            model.license_hint,
            model.image_urls,
            None,
            model.source_ref,
            model.tags,
            {**model.extra, "mode": "public_api_metadata_fallback"},
        )
    author_raw = raw.get("author") or raw.get("creator") or raw.get("user") or {}
    author = (
        author_raw.get("name") or author_raw.get("username")
        if isinstance(author_raw, dict)
        else author_raw
    )
    images = _strings(raw.get("images") or raw.get("image") or raw.get("thumbnail"))
    files = raw.get("files") or raw.get("outputs") or []
    candidate = next(
        (
            item
            for item in files
            if isinstance(item, dict)
            and (item.get("download_url") or item.get("url"))
            and any(
                str(item.get("filename") or item.get("name") or "")
                .lower()
                .endswith(ext)
                for ext in SOURCE_EXTENSIONS
            )
        ),
        None,
    )
    return ExternalModel(
        _clean_text(raw.get("title") or raw.get("name"), 140),
        _clean_text(raw.get("description") or raw.get("summary"), 40_000),
        _clean_text(author or "NIH 3D contributor", 200),
        _detect_license(raw.get("license"), raw.get("license_url")),
        images[:12],
        str(candidate.get("download_url") or candidate.get("url"))
        if candidate
        else None,
        ref,
        normalize_tags(raw.get("tags") or raw.get("keywords")),
        {
            "mode": "api",
            "entry": raw,
            "download_name": (
                candidate.get("filename") or candidate.get("name")
                if candidate
                else ""
            ),
        },
    )


async def _fetch_onshape(url: str, ref: str, token: str) -> ExternalModel:
    del url
    document_id, workspace_kind, workspace_id, element_id = ref.split(":", 3)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    raw, _ = await _json_request(
        f"https://cad.onshape.com/api/documents/{quote(document_id, safe='')}",
        expected_domains=("cad.onshape.com",),
        headers=headers,
    )
    elements: Any = []
    if workspace_kind == "w":
        elements, _ = await _json_request(
            f"https://cad.onshape.com/api/documents/d/{quote(document_id, safe='')}"
            f"/w/{quote(workspace_id, safe='')}/elements",
            expected_domains=("cad.onshape.com",),
            headers=headers,
        )
    element = (
        next(
            (
                item
                for item in elements
                if isinstance(item, dict)
                and str(item.get("id") or item.get("elementId")) == element_id
            ),
            {},
        )
        if isinstance(elements, list)
        else {}
    )
    element_type = str(element.get("elementType") or element.get("type") or "").upper()
    download_url = None
    if element_id and workspace_kind == "w" and element_type in {
        "PARTSTUDIO",
        "ASSEMBLY",
    }:
        family = "partstudios" if element_type == "PARTSTUDIO" else "assemblies"
        download_url = (
            f"https://cad.onshape.com/api/{family}/d/{document_id}/w/{workspace_id}"
            f"/e/{element_id}/stl?mode=binary&units=millimeter"
        )
    owner = raw.get("owner", {}) if isinstance(raw, dict) else {}
    name = str(element.get("name") or raw.get("name") or "onshape-model")
    return ExternalModel(
        _clean_text(name, 140),
        _clean_text(raw.get("description"), 40_000),
        _clean_text(
            (owner.get("name") or owner.get("email"))
            if isinstance(owner, dict)
            else "Onshape user",
            200,
        ),
        None,
        [],
        download_url,
        ref,
        [],
        {
            "mode": "oauth",
            "document": raw,
            "element": element,
            "download_name": f"{slugify(name)}.stl",
        },
    )


SOURCE_ADAPTERS: dict[str, Adapter] = {
    "grabcad": Adapter("grabcad", "metadata", ("grabcad.com",), ("grabcad.com",), ("grabcad.com",), None, lambda url: _regex_ref(r"/library/([^/?#]+)", url), _fetch_metadata),
    "thingiverse": Adapter("thingiverse", "api", ("thingiverse.com",), ("api.thingiverse.com", "thingiverse.com"), ("thingiverse.com",), "mk_thingiverse_token", lambda url: _regex_ref(r"/(?:thing:|things/)(\d+)", url), _fetch_thingiverse),
    "printables": Adapter("printables", "metadata", ("printables.com",), ("printables.com",), ("printables.com",), None, lambda url: _regex_ref(r"/model/(\d+)", url), _fetch_metadata),
    "github": Adapter("github", "api", ("github.com",), ("api.github.com", "github.com"), ("github.com", "githubusercontent.com"), "mk_github_token", _github_ref, _fetch_github),
    "sketchfab": Adapter("sketchfab", "api", ("sketchfab.com",), ("api.sketchfab.com", "sketchfab.com"), ("sketchfab.com", "sketchfabusercontent.com"), "mk_sketchfab_token", lambda url: _regex_ref(r"/(?:3d-models/[^/]*-|models/)([0-9a-f]{32})(?:/|$)", url), _fetch_sketchfab),
    "onshape": Adapter("onshape", "oauth", ("onshape.com",), ("cad.onshape.com", "oauth.onshape.com", "onshape.com"), ("cad.onshape.com",), None, _onshape_ref, _fetch_onshape),
    "myminifactory": Adapter("myminifactory", "api", ("myminifactory.com",), ("myminifactory.com",), ("myminifactory.com",), "mk_myminifactory_token", lambda url: _regex_ref(r"/object/(?:[^/]*-)?(\d+)(?:/|$)", url), _fetch_myminifactory),
    "cults3d": Adapter("cults3d", "metadata", ("cults3d.com",), ("cults3d.com",), ("cults3d.com",), None, _path_ref, _fetch_metadata),
    "thangs": Adapter("thangs", "metadata", ("thangs.com",), ("thangs.com",), ("thangs.com",), None, _path_ref, _fetch_metadata),
    "traceparts": Adapter("traceparts", "link", ("traceparts.com",), (), (), None, _path_ref, _fetch_link),
    "nih3d": Adapter("nih3d", "api", ("3d.nih.gov",), ("3d.nih.gov",), ("3d.nih.gov",), None, lambda url: _regex_ref(r"/entries/(\d+)", url), _fetch_nih3d),
}


def _source_row(source_id: str) -> dict[str, Any]:
    with tx() as cur:
        cur.execute("SELECT * FROM mk_sources WHERE id = %s", (source_id,))
        row = cur.fetchone()
    if row is None or not row["enabled"]:
        raise MarketplaceError(
            404, "source_disabled", "that import source is unavailable"
        )
    adapter = SOURCE_ADAPTERS.get(source_id)
    if adapter is None:
        raise MarketplaceError(
            404, "source_disabled", "that import source is unavailable"
        )
    if str(row["kind"]) != adapter.kind:
        raise MarketplaceError(
            503,
            "source_misconfigured",
            "the source capability does not match the adapter",
        )
    return dict(row)


def _configured_token(adapter: Adapter) -> str:
    if not adapter.credential_setting:
        return ""
    return str(getattr(settings, adapter.credential_setting, "") or "").strip()


def _onshape_configured() -> bool:
    return bool(
        settings.mk_onshape_client_id
        and settings.mk_onshape_client_secret
        and settings.session_secret
        and settings.provider_key_encryption_key
    )


async def _probe_with_token(
    source_id: str, url: str, token: str
) -> dict[str, Any]:
    source = _source_row(source_id)
    adapter = SOURCE_ADAPTERS[source_id]
    normalized_url = await _validated_url(url, adapter.page_domains)
    ref = adapter.match(normalized_url)
    if not ref:
        raise MarketplaceError(
            400,
            "invalid_source_url",
            "the URL does not match the selected source",
        )
    effective_fetch = adapter.fetch
    effective_kind = adapter.kind
    if (
        adapter.kind in {"api", "oauth"}
        and source_id != "nih3d"
        and not token
    ):
        effective_fetch = _fetch_metadata
        effective_kind = "metadata"
    model = await effective_fetch(normalized_url, ref, token)
    if model.source_ref != ref:
        raise MarketplaceError(
            502,
            "source_fetch_failed",
            "the source returned a mismatched model reference",
        )
    payload = asdict(model)
    payload["effective_kind"] = effective_kind
    payload["source_url"] = normalized_url
    payload["supports_file_fetch"] = bool(
        source["supports_file_fetch"] and effective_kind in {"api", "oauth"}
    )
    return payload


async def probe(source_id: str, url: str) -> dict[str, Any]:
    adapter = SOURCE_ADAPTERS.get(str(source_id or "").strip().lower())
    if adapter is None:
        raise MarketplaceError(400, "unknown_source", "unknown import source")
    return await _probe_with_token(
        adapter.source_id, str(url or "").strip(), _configured_token(adapter)
    )


def _source_aad(email: str, source_id: str) -> bytes:
    return (
        b"chudware-source-account-v1\0"
        + email.strip().lower().encode()
        + b"\0"
        + source_id.encode()
    )


def _encrypt_source_tokens(
    email: str, source_id: str, payload: dict[str, Any]
) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    try:
        ciphertext = AESGCM(_key_bytes()).encrypt(
            nonce, plaintext, _source_aad(email, source_id)
        )
    except RuntimeError as exc:
        raise MarketplaceError(
            503,
            "source_unconfigured",
            "encrypted source accounts are not configured",
        ) from exc
    return bytes((_SOURCE_ACCOUNT_VERSION,)) + nonce + ciphertext


def _decrypt_source_tokens(
    email: str, source_id: str, envelope: bytes
) -> dict[str, Any]:
    raw = bytes(envelope)
    if len(raw) <= 1 + _NONCE_BYTES or raw[0] != _SOURCE_ACCOUNT_VERSION:
        raise MarketplaceError(
            503,
            "source_account_invalid",
            "the linked source account must be reconnected",
        )
    try:
        plaintext = AESGCM(_key_bytes()).decrypt(
            raw[1 : 1 + _NONCE_BYTES],
            raw[1 + _NONCE_BYTES :],
            _source_aad(email, source_id),
        )
        payload = json.loads(plaintext)
    except RuntimeError as exc:
        raise MarketplaceError(
            503,
            "source_unconfigured",
            "encrypted source accounts are not configured",
        ) from exc
    except (InvalidTag, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise MarketplaceError(
            503,
            "source_account_invalid",
            "the linked source account must be reconnected",
        ) from exc
    if not isinstance(payload, dict):
        raise MarketplaceError(
            503,
            "source_account_invalid",
            "the linked source account must be reconnected",
        )
    return payload


def _seller(request: Request) -> tuple[Any, dict[str, Any]]:
    user = require_user(request)
    with tx() as cur:
        cur.execute(
            "SELECT * FROM mk_sellers WHERE email = %s", (user.email.lower(),)
        )
        row = cur.fetchone()
    require_seller(request, row)
    return user, dict(row)


async def _onshape_token(email: str) -> str:
    with tx() as cur:
        cur.execute(
            """
            SELECT encrypted_tokens, expires_at
              FROM mk_source_accounts
             WHERE email = %s AND source_id = 'onshape'
            """,
            (email.lower(),),
        )
        row = cur.fetchone()
    if row is None:
        return ""
    tokens = _decrypt_source_tokens(email, "onshape", row["encrypted_tokens"])
    expires_at = row["expires_at"]
    if (
        expires_at is not None
        and expires_at <= datetime.now(timezone.utc) + timedelta(seconds=30)
    ):
        refresh_token = str(tokens.get("refresh_token") or "")
        if (
            not refresh_token
            or not settings.mk_onshape_client_id
            or not settings.mk_onshape_client_secret
        ):
            return ""
        form = urlencode(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        headers = {
            "Authorization": "Basic "
            + base64.b64encode(
                f"{settings.mk_onshape_client_id}:{settings.mk_onshape_client_secret}".encode()
            ).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        response_body, _, response_headers = await _read_response(
            "POST",
            _ONSHAPE_TOKEN_URL,
            expected_domains=("oauth.onshape.com",),
            headers=headers,
            content=form,
            max_bytes=_JSON_LIMIT,
        )
        if "json" not in response_headers.get("content-type", "").lower():
            raise MarketplaceError(
                502,
                "source_fetch_failed",
                "Onshape returned an invalid token response",
            )
        try:
            refreshed = json.loads(response_body)
        except ValueError as exc:
            raise MarketplaceError(
                502,
                "source_fetch_failed",
                "Onshape returned an invalid token response",
            ) from exc
        if not isinstance(refreshed, dict) or not refreshed.get("access_token"):
            raise MarketplaceError(
                502,
                "source_fetch_failed",
                "Onshape returned an invalid token response",
            )
        refreshed.setdefault("refresh_token", refresh_token)
        tokens = refreshed
        try:
            lifetime = max(60, int(tokens.get("expires_in") or 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=lifetime)
        with tx() as cur:
            cur.execute(
                """
                UPDATE mk_source_accounts
                   SET encrypted_tokens = %s, expires_at = %s, last_used_at = now()
                 WHERE email = %s AND source_id = 'onshape'
                """,
                (
                    _encrypt_source_tokens(email, "onshape", tokens),
                    expires_at,
                    email.lower(),
                ),
            )
    else:
        with tx() as cur:
            cur.execute(
                """
                UPDATE mk_source_accounts
                   SET last_used_at = now()
                 WHERE email = %s AND source_id = 'onshape'
                """,
                (email.lower(),),
            )
    return str(tokens.get("access_token") or "")


def _import_view(row: dict[str, Any]) -> dict[str, Any]:
    def timestamp(value: Any) -> str | None:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value) if value is not None else None

    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return {
        "id": str(row["id"]),
        "source_id": str(row["source_id"]),
        "source_ref": str(row.get("source_ref") or ""),
        "source_url": str(row["source_url"]),
        "status": str(row["status"]),
        "preview": payload if isinstance(payload, dict) and payload else None,
        "error": str(row.get("error") or ""),
        "listing_id": str(row["listing_id"]) if row.get("listing_id") else None,
        "created_at": timestamp(row.get("created_at")),
        "completed_at": timestamp(row.get("completed_at")),
    }


def _deduplicated_import_view(row: dict[str, Any]) -> dict[str, Any]:
    public_listing_id = row.get("public_listing_id")
    return {
        "source_id": str(row["source_id"]),
        "source_ref": str(row.get("source_ref") or ""),
        "status": str(row["status"]),
        "listing_id": (
            str(public_listing_id) if public_listing_id is not None else None
        ),
    }


@router.get("/sources")
async def list_sources(request: Request) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller(request)
    with tx() as cur:
        cur.execute("SELECT * FROM mk_sources ORDER BY sort_order, id")
        rows = [dict(row) for row in cur.fetchall()]
        cur.execute(
            "SELECT source_id FROM mk_source_accounts WHERE email = %s",
            (user.email.lower(),),
        )
        connected = {str(row["source_id"]) for row in cur.fetchall()}
    items = []
    for source in rows:
        adapter = SOURCE_ADAPTERS.get(str(source["id"]))
        if adapter is None:
            continue
        server_configured = (
            bool(_configured_token(adapter)) if adapter.credential_setting else True
        )
        account_connected = adapter.source_id in connected
        if adapter.source_id == "onshape":
            server_configured = _onshape_configured()
            degraded = not server_configured or not account_connected
        elif adapter.kind == "api" and adapter.source_id != "nih3d":
            degraded = not server_configured
        else:
            degraded = False
        items.append(
            {
                "id": str(source["id"]),
                "name": str(source["name"]),
                "url": str(source["url"]),
                "kind": str(source["kind"]),
                "supports_file_fetch": bool(source["supports_file_fetch"]),
                "enabled": bool(source["enabled"]),
                "configured": server_configured,
                "degraded": degraded,
                "effective_kind": "metadata"
                if degraded
                else str(source["kind"]),
                "file_fetch_available": bool(
                    source["supports_file_fetch"] and not degraded
                ),
                "account_connected": account_connected,
                "notes": str(source["notes"]),
            }
        )
    return {"items": items}


@router.post("/imports")
async def create_import(
    request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller(request)
    api_writes.enforce(user.email, "marketplace imports")
    source_id = str(body.get("source_id") or "").strip().lower()
    url = str(body.get("url") or "").strip()
    adapter = SOURCE_ADAPTERS.get(source_id)
    if adapter is None:
        raise MarketplaceError(400, "unknown_source", "unknown import source")
    _source_row(source_id)
    normalized_url = await _validated_url(url, adapter.page_domains)
    source_ref = adapter.match(normalized_url)
    if not source_ref:
        raise MarketplaceError(
            400,
            "invalid_source_url",
            "the URL does not match the selected source",
        )
    import_id = str(uuid.uuid4())
    requester = user.email.lower()
    with tx() as cur:
        cur.execute(
            """
            INSERT INTO mk_imports (
                id, source_id, source_ref, source_url, requested_by, status
            ) VALUES (%s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (source_id, source_ref) WHERE source_ref <> ''
            DO NOTHING
            """,
            (
                import_id,
                source_id,
                source_ref,
                normalized_url,
                requester,
            ),
        )
        if cur.rowcount == 0:
            cur.execute(
                """
                SELECT i.*,
                       CASE WHEN l.status = 'published'
                            THEN l.id END AS public_listing_id
                  FROM mk_imports AS i
                  LEFT JOIN mk_listings AS l ON l.id = i.listing_id
                 WHERE i.source_id = %s AND i.source_ref = %s
                   FOR UPDATE OF i
                """,
                (source_id, source_ref),
            )
            existing_row = cur.fetchone()
            if existing_row is None:
                raise MarketplaceError(
                    409,
                    "import_conflict",
                    "that source model is already being imported",
                )
            existing = dict(existing_row)
            if existing["status"] == "published":
                if str(existing["requested_by"]).lower() == requester:
                    return {
                        "import": _import_view(existing),
                        "existing": True,
                    }
                return {
                    "import": _deduplicated_import_view(existing),
                    "existing": True,
                }
            # The global source-ref row is a retryable lease until publication.
            # Reusing and transferring it avoids both a new index and disclosure
            # of the prior requester's account-specific probe payload.
            import_id = str(existing["id"])
            cur.execute(
                """
                UPDATE mk_imports
                   SET source_url = %s, requested_by = %s,
                       status = 'pending', payload = '{}'::jsonb,
                       error = '', listing_id = NULL,
                       created_at = now(), completed_at = NULL
                 WHERE id = %s
                """,
                (normalized_url, requester, import_id),
            )
    token = (
        await _onshape_token(user.email)
        if source_id == "onshape" and _onshape_configured()
        else _configured_token(adapter)
    )
    try:
        payload = await _probe_with_token(source_id, normalized_url, token)
        status = "linked" if adapter.kind == "link" else "fetched"
        with tx() as cur:
            cur.execute(
                """
                UPDATE mk_imports
                   SET payload = %s::jsonb, status = %s,
                       completed_at = now(), error = ''
                 WHERE id = %s AND requested_by = %s
                RETURNING *
                """,
                (json.dumps(payload), status, import_id, requester),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    SELECT i.source_id, i.source_ref, i.status,
                           CASE WHEN l.status = 'published'
                                THEN l.id END AS public_listing_id
                      FROM mk_imports AS i
                      LEFT JOIN mk_listings AS l ON l.id = i.listing_id
                     WHERE i.id = %s
                    """,
                    (import_id,),
                )
                displaced = cur.fetchone()
                if displaced is None:
                    raise MarketplaceError(
                        409,
                        "import_conflict",
                        "that source import is no longer available",
                    )
                return {
                    "import": _deduplicated_import_view(dict(displaced)),
                    "existing": True,
                }
    except Exception as exc:
        detail = (
            exc.detail.get("detail", "source import failed")
            if isinstance(exc, MarketplaceError) and isinstance(exc.detail, dict)
            else "source import failed"
        )
        with tx() as cur:
            cur.execute(
                """
                UPDATE mk_imports
                   SET status = 'failed', error = %s, completed_at = now()
                 WHERE id = %s AND requested_by = %s
                """,
                (str(detail)[:2000], import_id, requester),
            )
        raise
    return {"import": _import_view(dict(row)), "existing": False}


@router.get("/imports/{import_id}")
async def get_import(import_id: str, request: Request) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller(request)
    try:
        canonical = str(uuid.UUID(import_id))
    except ValueError as exc:
        raise MarketplaceError(
            404, "import_not_found", "import not found"
        ) from exc
    with tx() as cur:
        cur.execute(
            """
            SELECT *
              FROM mk_imports
             WHERE id = %s AND requested_by = %s
            """,
            (canonical, user.email.lower()),
        )
        row = cur.fetchone()
    if row is None:
        raise MarketplaceError(404, "import_not_found", "import not found")
    return {"import": _import_view(dict(row))}


async def _download_to_path(
    url: str,
    target: Path,
    *,
    expected_domains: tuple[str, ...],
    headers: dict[str, str] | None,
    max_bytes: int,
) -> tuple[int, str, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    current = url
    request_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        **(headers or {}),
    }
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TOTAL_FETCH_SECONDS
    try:
        async with asyncio.timeout_at(deadline):
            for redirect_count in range(_REDIRECT_LIMIT + 1):
                pinned = await _pin_url(current, expected_domains)
                try:
                    async with httpx.AsyncClient(
                        timeout=_TIMEOUT,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        response = await client.send(
                            _pinned_request(
                                client,
                                "GET",
                                pinned,
                                request_headers,
                                None,
                            ),
                            stream=True,
                        )
                        try:
                            if response.status_code in (
                                301,
                                302,
                                303,
                                307,
                                308,
                            ):
                                location = response.headers.get("location", "")
                                if (
                                    redirect_count >= _REDIRECT_LIMIT
                                    or not location
                                ):
                                    raise MarketplaceError(
                                        502,
                                        "source_fetch_failed",
                                        "the asset redirected too many times",
                                    )
                                current = urljoin(pinned.url, location)
                                continue
                            if response.status_code != 200:
                                raise MarketplaceError(
                                    502,
                                    "source_fetch_failed",
                                    f"the source asset returned HTTP {response.status_code}",
                                )
                            declared = response.headers.get("content-length")
                            if declared:
                                try:
                                    too_large = int(declared) > max_bytes
                                except ValueError:
                                    too_large = False
                                if too_large:
                                    raise MarketplaceError(
                                        400,
                                        "asset_too_large",
                                        "the imported asset exceeds the marketplace limit",
                                    )
                            declared_type = (
                                response.headers.get(
                                    "content-type",
                                    "application/octet-stream",
                                )
                                .split(";", 1)[0]
                                .strip()
                                .lower()
                            )
                            content_type = safe_media_type(target.name)
                            if declared_type in {
                                "text/html",
                                "application/xhtml+xml",
                            }:
                                raise MarketplaceError(
                                    502,
                                    "source_fetch_failed",
                                    "the source asset returned a web page",
                                )
                            if content_type.startswith(
                                "image/"
                            ) and not declared_type.startswith("image/"):
                                raise MarketplaceError(
                                    502,
                                    "source_fetch_failed",
                                    "the source gallery URL did not return an image",
                                )
                            written = 0
                            with temp.open("wb") as handle:
                                async for chunk in response.aiter_bytes():
                                    if loop.time() >= deadline:
                                        raise TimeoutError
                                    written += len(chunk)
                                    if written > max_bytes:
                                        raise MarketplaceError(
                                            400,
                                            "asset_too_large",
                                            "the imported asset exceeds the marketplace limit",
                                        )
                                    digest.update(chunk)
                                    handle.write(chunk)
                            temp.replace(target)
                            return written, digest.hexdigest(), content_type
                        finally:
                            await response.aclose()
                except httpx.HTTPError as exc:
                    raise MarketplaceError(
                        502,
                        "source_fetch_failed",
                        "the source asset could not be downloaded",
                    ) from exc
    except TimeoutError as exc:
        raise MarketplaceError(
            502,
            "source_fetch_failed",
            "the source asset exceeded the total fetch deadline",
        ) from exc
    except OSError as exc:
        raise MarketplaceError(
            502,
            "source_fetch_failed",
            "the source asset could not be stored",
        ) from exc
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    raise MarketplaceError(
        502, "source_fetch_failed", "the asset redirected too many times"
    )


def _safe_filename(
    url: str, fallback: str, allowed_extensions: tuple[str, ...]
) -> str:
    raw = Path(urlsplit(url).path).name or fallback
    raw = (
        re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")[:160] or fallback
    )
    if not any(raw.lower().endswith(ext) for ext in allowed_extensions):
        fallback_ext = next(
            (
                ext
                for ext in allowed_extensions
                if fallback.lower().endswith(ext)
            ),
            allowed_extensions[0],
        )
        raw += fallback_ext
    return raw


def _download_headers(source_id: str, token: str) -> dict[str, str]:
    if not token:
        return {}
    if source_id == "sketchfab":
        return {"Authorization": f"Token {token}"}
    return {"Authorization": f"Bearer {token}"}


@router.post("/imports/{import_id}/accept")
async def accept_import(
    import_id: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _ = _seller(request)
    api_writes.enforce(user.email, "marketplace imports")
    try:
        canonical = str(uuid.UUID(import_id))
    except ValueError as exc:
        raise MarketplaceError(
            404, "import_not_found", "import not found"
        ) from exc
    with tx() as cur:
        cur.execute(
            """
            SELECT *
              FROM mk_imports
             WHERE id = %s AND requested_by = %s
            """,
            (canonical, user.email.lower()),
        )
        import_row = cur.fetchone()
    if import_row is None:
        raise MarketplaceError(404, "import_not_found", "import not found")
    import_row = dict(import_row)
    if import_row["status"] == "published" and import_row.get("listing_id"):
        return {
            "listing_id": str(import_row["listing_id"]),
            "status": "draft",
            "existing": True,
            "assets_imported": 0,
        }
    if import_row["status"] not in {"fetched", "linked"}:
        raise MarketplaceError(
            409, "import_not_ready", "the import is not ready to accept"
        )
    payload = import_row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise MarketplaceError(
                409, "import_not_ready", "the import payload is invalid"
            ) from exc
    if not isinstance(payload, dict):
        raise MarketplaceError(
            409, "import_not_ready", "the import payload is invalid"
        )
    source_id = str(import_row["source_id"])
    source = _source_row(source_id)
    adapter = SOURCE_ADAPTERS[source_id]
    detected_license = str(payload.get("license_hint") or "").strip()
    supplied_license = str(body.get("license_id") or "").strip()
    if (
        detected_license
        and supplied_license
        and supplied_license != detected_license
    ):
        raise MarketplaceError(
            400,
            "license_conflict",
            f"the source detected {detected_license}; choose that exact license",
        )
    license_id = supplied_license or detected_license
    if not license_id:
        raise MarketplaceError(
            400,
            "license_required",
            "choose a license because the source did not declare one",
        )
    detected_author = _clean_text(payload.get("author"), 200)
    author = (
        _clean_text(body.get("source_author"), 200)
        if "source_author" in body
        else detected_author
    )
    with tx() as cur:
        cur.execute(
            """
            SELECT requires_attribution
              FROM mk_licenses
             WHERE id = %s AND active
            """,
            (license_id,),
        )
        license_row = cur.fetchone()
    if license_row is None:
        raise MarketplaceError(
            400, "invalid_license", "choose an active marketplace license"
        )
    if (
        bool(license_row["requires_attribution"])
        and detected_author
        and not author
    ):
        raise MarketplaceError(
            400,
            "source_author_required",
            "an attribution license cannot erase the detected author",
        )
    if not author:
        raise MarketplaceError(
            400,
            "source_author_required",
            "record the upstream author before accepting this import",
        )
    title = (
        _clean_text(body.get("title"), 140)
        if "title" in body
        else _clean_text(payload.get("title"), 140)
    )
    if len(title) < 3:
        raise MarketplaceError(
            400,
            "invalid_title",
            "the imported listing needs a title of at least 3 characters",
        )
    description = (
        _clean_text(body.get("description"), 40_000)
        if "description" in body
        else _clean_text(payload.get("description"), 40_000)
    )
    summary = (
        _clean_text(body.get("summary"), 280)
        if "summary" in body
        else description[:280]
    )
    stored_images = [
        str(item)
        for item in payload.get("image_urls", [])
        if isinstance(item, str)
    ]
    selected_raw = body.get("image_urls", None)
    if selected_raw is None:
        selected_images = stored_images
    elif isinstance(selected_raw, list):
        selected_images = [str(item) for item in selected_raw]
    else:
        raise MarketplaceError(
            400, "invalid_images", "image_urls must be a list"
        )
    if any(item not in stored_images for item in selected_images):
        raise MarketplaceError(
            400,
            "invalid_images",
            "every selected image must come from the stored import preview",
        )
    selected_images = list(dict.fromkeys(selected_images))[:12]
    attach_file = body.get("attach_file", True) is not False
    file_allowed = bool(
        source["supports_file_fetch"] and payload.get("supports_file_fetch")
    )
    if attach_file and payload.get("download_url") and not file_allowed:
        raise MarketplaceError(
            400,
            "source_no_file_fetch",
            "this source does not permit automated file retrieval",
        )

    from marketplace import asset_path, normalize_listing_input

    normalized = normalize_listing_input(
        {
            "title": title,
            "summary": summary,
            "description": description,
            "category_id": body.get("category_id"),
            "license_id": license_id,
            "pricing_kind": "free",
            "price_cents": 0,
            "price_credits": 0,
            "tips_enabled": True,
            "origin": str(body.get("origin") or "").strip(),
            "origin_note": "",
            "source_id": source_id,
            "source_url": str(import_row["source_url"]),
            "source_author": author,
            "tags": body.get("tags", payload.get("tags", [])),
            "units": "mm",
        }
    )
    listing_id = str(uuid.uuid4())
    listing_slug = f"{slugify(title, max_len=69) or 'model'}-{listing_id[:8]}"
    token = (
        await _onshape_token(user.email)
        if source_id == "onshape" and _onshape_configured()
        else _configured_token(adapter)
    )
    imported_assets: list[dict[str, Any]] = []
    if file_allowed and (
        selected_images or (attach_file and payload.get("download_url"))
    ):
        uploads.enforce(user.email, "marketplace imports")
    root = settings.listing_asset_root(listing_id)
    try:
        if file_allowed:
            for index, image_url in enumerate(selected_images):
                filename = _safe_filename(
                    image_url, f"gallery-{index + 1}.jpg", IMAGE_EXTENSIONS
                )
                rel_path = (
                    f"imports/{uuid.uuid4().hex[:12]}-{filename}"
                )
                target = asset_path(listing_id, rel_path)
                size, sha256, content_type = await _download_to_path(
                    image_url,
                    target,
                    expected_domains=adapter.asset_domains,
                    headers={},
                    max_bytes=settings.marketplace_max_asset_bytes,
                )
                imported_assets.append(
                    {
                        "id": str(uuid.uuid4()),
                        "kind": "image",
                        "role": "gallery",
                        "filename": filename,
                        "rel_path": rel_path,
                        "content_type": content_type,
                        "format": Path(filename).suffix.lower().lstrip("."),
                        "bytes": size,
                        "sha256": sha256,
                        "sort_order": index,
                        "gated": False,
                    }
                )
        download_url = str(payload.get("download_url") or "")
        if attach_file and download_url and file_allowed:
            extra = (
                payload.get("extra")
                if isinstance(payload.get("extra"), dict)
                else {}
            )
            filename = _safe_filename(
                download_url,
                str(extra.get("download_name") or "source-model.zip"),
                SOURCE_EXTENSIONS,
            )
            rel_path = f"imports/{uuid.uuid4().hex[:12]}-{filename}"
            target = asset_path(listing_id, rel_path)
            size, sha256, content_type = await _download_to_path(
                download_url,
                target,
                expected_domains=adapter.asset_domains,
                headers=_download_headers(source_id, token),
                max_bytes=settings.marketplace_max_asset_bytes,
            )
            imported_assets.append(
                {
                    "id": str(uuid.uuid4()),
                    "kind": "source",
                    "role": "primary",
                    "filename": filename,
                    "rel_path": rel_path,
                    "content_type": content_type,
                    "format": Path(filename).suffix.lower().lstrip("."),
                    "bytes": size,
                    "sha256": sha256,
                    "sort_order": 0,
                    "gated": False,
                }
            )
        imported_bytes = sum(asset["bytes"] for asset in imported_assets)
        with tx() as cur:
            cur.execute(
                "SELECT 1 FROM mk_sellers WHERE email = %s FOR UPDATE",
                (user.email.lower(),),
            )
            cur.execute(
                """
                SELECT COALESCE(sum(a.bytes), 0) AS used
                  FROM mk_assets a
                  JOIN mk_listings l ON l.id = a.listing_id
                 WHERE l.seller_email = %s
                """,
                (user.email.lower(),),
            )
            used = int(cur.fetchone()["used"])
            if used + imported_bytes > settings.marketplace_seller_quota_bytes:
                raise MarketplaceError(
                    400,
                    "quota_exceeded",
                    "the imported assets exceed the seller storage quota",
                )
            cur.execute(
                """
                SELECT status, listing_id
                  FROM mk_imports
                 WHERE id = %s AND requested_by = %s
                   FOR UPDATE
                """,
                (canonical, user.email.lower()),
            )
            locked = cur.fetchone()
            if locked is None:
                raise MarketplaceError(
                    404, "import_not_found", "import not found"
                )
            if locked["status"] == "published" and locked["listing_id"]:
                existing_id = str(locked["listing_id"])
                for asset in imported_assets:
                    asset_path(
                        listing_id, asset["rel_path"]
                    ).unlink(missing_ok=True)
                return {
                    "listing_id": existing_id,
                    "status": "draft",
                    "existing": True,
                    "assets_imported": 0,
                }
            cur.execute(
                """
                INSERT INTO mk_listings (
                    id, slug, seller_email, title, summary, description,
                    category_id, license_id, status, pricing_kind,
                    price_cents, price_credits, tips_enabled, origin,
                    origin_note, attestation_status, open_source,
                    forked_from, source_id, source_url, source_author,
                    tags, units
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s,
                    %s, %s, %s, %s, %s, 'none', %s, %s, %s, %s,
                    %s, %s, %s
                )
                """,
                (
                    listing_id,
                    listing_slug,
                    user.email.lower(),
                    normalized["title"],
                    normalized.get("summary", ""),
                    normalized.get("description", ""),
                    normalized.get("category_id"),
                    normalized["license_id"],
                    normalized["pricing_kind"],
                    normalized.get("price_cents", 0),
                    normalized.get("price_credits", 0),
                    normalized.get("tips_enabled", False),
                    normalized["origin"],
                    normalized.get("origin_note", ""),
                    normalized["open_source"],
                    normalized.get("forked_from"),
                    normalized["source_id"],
                    normalized["source_url"],
                    normalized["source_author"],
                    normalized.get("tags", []),
                    normalized.get("units", "mm"),
                ),
            )
            for asset in imported_assets:
                cur.execute(
                    """
                    INSERT INTO mk_assets (
                        id, listing_id, kind, role, filename, rel_path,
                        content_type, format, bytes, sha256, sort_order,
                        gated
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s
                    )
                    """,
                    (
                        asset["id"],
                        listing_id,
                        asset["kind"],
                        asset["role"],
                        asset["filename"],
                        asset["rel_path"],
                        asset["content_type"],
                        asset["format"],
                        asset["bytes"],
                        asset["sha256"],
                        asset["sort_order"],
                        asset["gated"],
                    ),
                )
            cur.execute(
                """
                UPDATE mk_imports
                   SET listing_id = %s, status = 'published',
                       completed_at = now(), error = ''
                 WHERE id = %s
                """,
                (listing_id, canonical),
            )
    except Exception:
        for asset in imported_assets:
            try:
                asset_path(
                    listing_id, asset["rel_path"]
                ).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (root / "imports").rmdir()
            root.rmdir()
        except OSError:
            pass
        raise
    return {
        "listing_id": listing_id,
        "slug": listing_slug,
        "status": "draft",
        "existing": False,
        "assets_imported": len(imported_assets),
    }


_onshape_states: dict[str, tuple[str, str, float]] = {}
_onshape_states_lock = threading.Lock()


def _onshape_state_signature(nonce: str, email: str) -> str:
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        f"onshape:{nonce}:{email.lower()}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _mint_onshape_state(email: str) -> tuple[str, str]:
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    state = f"{nonce}.{_onshape_state_signature(nonce, email)}"
    now = time.monotonic()
    with _onshape_states_lock:
        for key, (_, _, expiry) in list(_onshape_states.items()):
            if expiry <= now:
                _onshape_states.pop(key, None)
        _onshape_states[nonce] = (
            email.lower(),
            verifier,
            now + _ONSHAPE_STATE_TTL,
        )
    return state, verifier


def _consume_onshape_state(state: str, email: str) -> str:
    nonce, separator, signature = (state or "").partition(".")
    if (
        not nonce
        or not separator
        or not hmac.compare_digest(
            signature, _onshape_state_signature(nonce, email)
        )
    ):
        return ""
    with _onshape_states_lock:
        record = _onshape_states.pop(nonce, None)
    if (
        record is None
        or record[0] != email.lower()
        or record[2] <= time.monotonic()
    ):
        return ""
    return record[1]


def _onshape_redirect_uri() -> str:
    return f"{settings.public_base_url}/api/studio/sources/onshape/callback"


@router.get("/sources/onshape/start", response_model=None)
async def onshape_start(request: Request) -> RedirectResponse:
    marketplace_enabled_or_404()
    user, _ = _seller(request)
    if not _onshape_configured():
        raise MarketplaceError(
            503,
            "source_unconfigured",
            "Onshape connection is not configured; imports remain available in metadata mode",
        )
    state, verifier = _mint_onshape_state(user.email)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    query = urlencode(
        {
            "client_id": settings.mk_onshape_client_id,
            "redirect_uri": _onshape_redirect_uri(),
            "response_type": "code",
            "scope": "OAuth2Read",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return RedirectResponse(
        f"{_ONSHAPE_AUTHORIZE_URL}?{query}", status_code=302
    )


@router.get("/sources/onshape/callback", response_model=None)
async def onshape_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> RedirectResponse:
    marketplace_enabled_or_404()
    user, _ = _seller(request)
    if error:
        raise MarketplaceError(
            400, "source_oauth_failed", f"Onshape reported {error}"
        )
    if not code:
        raise MarketplaceError(
            400,
            "source_oauth_failed",
            "Onshape returned no authorization code",
        )
    verifier = _consume_onshape_state(state, user.email)
    if not verifier:
        raise MarketplaceError(
            400,
            "source_oauth_failed",
            "the Onshape connection link expired or was already used",
        )
    form = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _onshape_redirect_uri(),
            "client_id": settings.mk_onshape_client_id,
            "code_verifier": verifier,
        }
    )
    headers = {
        "Authorization": "Basic "
        + base64.b64encode(
            f"{settings.mk_onshape_client_id}:{settings.mk_onshape_client_secret}".encode()
        ).decode(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    token_body, _, response_headers = await _read_response(
        "POST",
        _ONSHAPE_TOKEN_URL,
        expected_domains=("oauth.onshape.com",),
        headers=headers,
        content=form,
        max_bytes=_JSON_LIMIT,
    )
    if "json" not in response_headers.get("content-type", "").lower():
        raise MarketplaceError(
            502,
            "source_oauth_failed",
            "Onshape returned an invalid token response",
        )
    try:
        tokens = json.loads(token_body)
    except ValueError as exc:
        raise MarketplaceError(
            502,
            "source_oauth_failed",
            "Onshape returned an invalid token response",
        ) from exc
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise MarketplaceError(
            502,
            "source_oauth_failed",
            "Onshape returned an invalid token response",
        )
    try:
        lifetime = max(60, int(tokens.get("expires_in") or 3600))
    except (TypeError, ValueError):
        lifetime = 3600
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=lifetime)
    envelope = _encrypt_source_tokens(user.email, "onshape", tokens)
    label = _clean_text(
        tokens.get("user_name") or tokens.get("email") or "Onshape", 200
    )
    with tx() as cur:
        cur.execute(
            """
            INSERT INTO mk_source_accounts (
                email, source_id, account_label, encrypted_tokens,
                expires_at, created_at, last_used_at
            ) VALUES (%s, 'onshape', %s, %s, %s, now(), now())
            ON CONFLICT (email, source_id) DO UPDATE SET
                account_label = EXCLUDED.account_label,
                encrypted_tokens = EXCLUDED.encrypted_tokens,
                expires_at = EXCLUDED.expires_at,
                last_used_at = now()
            """,
            (user.email.lower(), label, envelope, expires_at),
        )
    return RedirectResponse("/studio#sources", status_code=303)


__all__ = [
    "Adapter",
    "ExternalModel",
    "SOURCE_ADAPTERS",
    "probe",
    "router",
]
