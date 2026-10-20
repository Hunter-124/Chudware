"""Search providers — contract ``contracts/research_tools.md`` §2.1.

Small classes behind one interface (mirrors ``services/agent/providers/``); selection is
env/config driven; :class:`MockSearchProvider` backs tests so the suite never touches the
network (the gate runs offline). Public HTTP uses the connection-pinned client
with explicit response and idle budgets.

Implemented providers are **Tavily** (recommended BYO-key default),
**DuckDuckGo HTML** (zero-key fallback, ``confidence:"limited"``), and the mock.
"""
from __future__ import annotations

import html as _html
import os
import re
import time
import urllib.parse
from typing import Optional

_UA = "Mozilla/5.0 (compatible; Chudware-research/1.0)"

class ProviderBlocked(RuntimeError):
    """Search host returned a bot challenge or an explicit access denial."""


_BLOCKED_PAGE = re.compile(
    r"(?:anomaly-modal|bots use duckduckgo too|please complete the following challenge|captcha)",
    re.I,
)


class SearchProvider:
    """One search back-end. ``search`` returns ``{"results": [...], "answer": str|None,
    "confidence": str}`` and *raises* on failure — the caller (``web_search``) converts a
    raised error into the §6 degradation shape."""

    name = "base"
    offline_safe = False
    requires_key: Optional[str] = None  # env var holding the API key, or None if keyless

    def search(self, query: str, max_results: int = 5) -> dict:
        raise NotImplementedError


class MockSearchProvider(SearchProvider):
    """Deterministic provider for tests — no network."""

    name = "mock"
    offline_safe = True

    def __init__(self, results: Optional[list] = None, answer: Optional[str] = None,
                 confidence: str = "high"):
        self._results = results or []
        self._answer = answer
        self._confidence = confidence

    def search(self, query: str, max_results: int = 5) -> dict:
        return {
            "results": list(self._results)[:max_results],
            "answer": self._answer,
            "confidence": self._confidence,
        }


class TavilyProvider(SearchProvider):
    """Tavily — recommended BYO-key default (LLM-ready answer + markdown)."""

    name = "tavily"
    requires_key = "TAVILY_API_KEY"
    ENDPOINT = "https://api.tavily.com/search"

    def search(self, query: str, max_results: int = 5) -> dict:
        key = os.environ.get(self.requires_key)
        if not key:
            raise RuntimeError("TAVILY_API_KEY not set")
        import json
        from services.net.safe_http import request

        resp = request(
            self.ENDPOINT, method="POST",
            body=json.dumps({"api_key": key, "query": query, "max_results": max_results,
                             "include_answer": True}).encode(),
            headers={"Content-Type": "application/json"}, idle_timeout=20.0,
            max_bytes=4 * 1024 * 1024, max_redirects=0,
        )
        if resp.status_code in (403, 429):
            raise ProviderBlocked(f"Tavily returned HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        results = [
            {"title": h.get("title", ""), "url": h.get("url", ""),
             "snippet": h.get("content", ""), "score": h.get("score")}
            for h in data.get("results", [])
        ]
        return {"results": results, "answer": data.get("answer"), "confidence": "high"}


class DDGProvider(SearchProvider):
    """DuckDuckGo HTML — the zero-key fallback. Low volume, exponential backoff,
    ``confidence:"limited"`` (contract §2.1). No API key anywhere."""

    name = "ddg"
    requires_key = None
    ENDPOINT = "https://html.duckduckgo.com/html/"

    def search(self, query: str, max_results: int = 5) -> dict:
        from urllib.parse import urlencode
        from services.net.safe_http import request

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = request(
                    self.ENDPOINT, method="POST", body=urlencode({"q": query}).encode(),
                    headers={"User-Agent": _UA,
                             "Content-Type": "application/x-www-form-urlencoded"},
                    idle_timeout=20.0, max_bytes=4 * 1024 * 1024,
                )
                if resp.status_code in (202, 403, 429) or _BLOCKED_PAGE.search(resp.text):
                    raise ProviderBlocked(f"DuckDuckGo returned an access challenge "
                                          f"(HTTP {resp.status_code})")
                resp.raise_for_status()
                return {
                    "results": parse_ddg_html(resp.text, max_results),
                    "answer": None,
                    "confidence": "limited",
                }
            except ProviderBlocked:
                raise  # retrying the same CAPTCHA/blocked host cannot solve it
            except Exception as exc:  # noqa: BLE001 — transient transport failure
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
        raise last_exc or RuntimeError("DuckDuckGo request failed")


_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return _html.unescape(_TAG_RE.sub("", s)).strip()


def _decode_ddg_href(href: str) -> str:
    """DDG wraps result links as ``/l/?uddg=<url-encoded>`` redirects — unwrap them."""
    if "uddg=" in href:
        q = urllib.parse.urlparse(href).query
        params = urllib.parse.parse_qs(q)
        if params.get("uddg"):
            return params["uddg"][0]
    if href.startswith("//"):
        return "https:" + href
    return href


def parse_ddg_html(html_text: str, max_results: int = 5) -> list[dict]:
    """Extract ``{title, url, snippet}`` rows from a DuckDuckGo HTML results page."""
    snippets = [_strip_tags(s) for s in _DDG_SNIPPET_RE.findall(html_text)]
    out: list[dict] = []
    for i, (href, title) in enumerate(_DDG_RESULT_RE.findall(html_text)):
        out.append({
            "title": _strip_tags(title),
            "url": _decode_ddg_href(href),
            "snippet": snippets[i] if i < len(snippets) else "",
        })
        if len(out) >= max_results:
            break
    return out


# Priority order for auto-selection among *keyed* providers.
_KEYED_PROVIDERS = (TavilyProvider,)
_REGISTRY = {
    "tavily": TavilyProvider,
    "ddg": DDGProvider,
    "duckduckgo": DDGProvider,
    "mock": MockSearchProvider,
}


def select_provider(name: Optional[str] = None) -> SearchProvider:
    """Resolve a provider (contract §2.1 selection order): explicit ``name`` -> first
    keyed provider whose key env is present -> DuckDuckGo zero-key fallback."""
    if name:
        cls = _REGISTRY.get(name.lower())
        if cls is not None:
            return cls()
        # unknown name: fall through to auto-selection rather than error
    for cls in _KEYED_PROVIDERS:
        if cls.requires_key and os.environ.get(cls.requires_key):
            return cls()
    return DDGProvider()
