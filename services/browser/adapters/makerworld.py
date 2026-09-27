"""MakerWorld discovery; guest download is blocked by its Bambu Cloud API."""
from __future__ import annotations

import html
import re
from urllib.parse import quote, urlparse

from .. import session
from . import register

SITE = "makerworld"
_MODEL = re.compile(r"/models/(\d+)(?:-[a-zA-Z0-9-]+)?")
_LINK = re.compile(r'href=["\'](?P<href>(?:https://makerworld\.com)?/(?:en/)?models/\d+[^"\']*)["\']', re.I)


class MakerWorldAdapter:
    key = SITE

    def search(self, query: str) -> list[dict]:
        url = "https://makerworld.com/en/search/models?keyword=" + quote(query)
        response = session.browser_fetch(url, SITE)
        if not response.get("ok"):
            return [response]
        seen: set[str] = set()
        hits = []
        for match in _LINK.finditer(response.get("content", "")):
            href = html.unescape(match.group("href"))
            mid = _MODEL.search(href)
            if not mid or mid.group(1) in seen:
                continue
            seen.add(mid.group(1))
            hits.append({"site": SITE, "url": "https://makerworld.com" + href if href.startswith("/") else href,
                         "title": href.rsplit("/", 1)[-1].split("?", 1)[0].replace("-", " ")})
        return hits

    def acquire(self, result: dict) -> dict:
        url = str(result.get("url") or "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"makerworld.com", "www.makerworld.com"} or not _MODEL.search(parsed.path):
            return {"ok": False, "reason": "no_cad_link", "detail": "Expected a MakerWorld model page.",
                    "candidates": []}
        # Verified against the public design-service metadata and the iot-service
        # profile download endpoint: anonymous metadata succeeds, signed download
        # returns HTTP 401 {error:'Please login.'}. HTML alone is not an artifact.
        return {"ok": False, "reason": "login_required",
                "detail": f"MakerWorld requires login to download {url}. Download the CAD file there and drop it in your project.",
                "candidates": [url]}


ADAPTER = register(MakerWorldAdapter())
