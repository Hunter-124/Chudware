"""``fetch_page(url)`` — contract ``contracts/research_tools.md`` §3.

Built-in **readability -> markdown** using only the standard library (``html.parser``,
``re``) so there is **zero new hard dependency** and the offline gate/CI stay green.
Firecrawl ``/scrape`` is used only when ``FIRECRAWL_API_KEY`` is present (hard/JS pages);
its absence degrades to the built-in extractor, never an error. Fetched content is cached
under the per-user knowledge cache (§4); offline mode serves cache only (§5).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import urlparse

from .cache import Cache, DEFAULT_TIER
from .common import HIGH, MEDIUM, OFFLINE, envelope, fail, is_offline, now_ts

_UA = "Mozilla/5.0 (compatible; Chudware-research/1.0)"
_MAX_CONTENT = 200_000  # cap markdown to protect the model window (contract §3)
_DROP_TAGS = {"script", "style", "nav", "footer", "aside", "noscript", "form",
              "svg", "iframe", "template"}
_BLOCK_TAGS = {"p", "div", "section", "article", "br", "tr", "ul", "ol", "table",
               "blockquote", "pre", "hr"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _Readability(HTMLParser):
    """Minimal readability: drop boilerplate/script tags, keep visible text, map headings
    to ``#`` and list items to ``- ``. Not a full DOM cleaner — good enough for a
    provenance quote + a model-readable summary without any third-party dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._drop_depth = 0
        self.title: str = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag in _HEADINGS:
            self._out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._out.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self._out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._drop_depth:
            self._drop_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        elif tag in _HEADINGS or tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._in_title:
            self.title += data
            return
        if data.strip():
            self._out.append(data)

    def markdown(self) -> str:
        text = "".join(self._out)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html_text: str) -> tuple[str, str]:
    """Return ``(markdown, title)`` from raw HTML using the stdlib readability parser."""
    parser = _Readability()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001 — malformed HTML: keep whatever parsed
        pass
    md = parser.markdown()
    if len(md) > _MAX_CONTENT:
        md = md[:_MAX_CONTENT] + "\n\n… [truncated]"
    return md, parser.title.strip()


def _valid_url(url: str) -> bool:
    try:
        p = urlparse(str(url))
    except Exception:  # noqa: BLE001
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _http_get(url: str) -> tuple[str, str, str]:
    """Live fetch. Returns ``(markdown, final_url, title)``. Uses the Firecrawl key
    pool when any keys are configured (rotates on rate limits; returns markdown
    directly), else httpx + the stdlib extractor."""
    from . import firecrawl_pool

    import httpx

    if firecrawl_pool.load_keys():
        try:
            md, meta = firecrawl_pool.scrape(url)
            if md:
                return md[:_MAX_CONTENT], url, str(meta.get("title") or "")
        except Exception:  # noqa: BLE001 — degrade to the built-in fetch, never error out
            pass

    resp = httpx.get(url, headers={"User-Agent": _UA}, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    md, title = html_to_markdown(resp.text)
    return md, str(resp.url), title


def _from_cache(rec: dict, *, offline: bool) -> dict:
    stale = bool(rec.get("_stale"))
    conf = OFFLINE if offline else (MEDIUM if stale else HIGH)
    note = " (offline: cache only)" if offline else (" (stale cache)" if stale else "")
    content = rec.get("content", "")
    return envelope(
        f"{rec.get('title') or rec.get('url')} — from cache{note} ({len(content)} chars)",
        source_url=rec.get("final_url") or rec.get("url"),
        quote=None,
        fetched_ts=rec.get("fetched_ts"),
        confidence=conf,
        cached=True,
        url=rec.get("url"),
        title=rec.get("title", ""),
        content=content,
    )


def fetch_page(
    url: str,
    *,
    tier: str = DEFAULT_TIER,
    cache: Optional[Cache] = None,
    force: bool = False,
    _fetcher: Optional[Callable[[str], tuple[str, str, str]]] = None,
) -> dict:
    """Fetch ``url`` -> markdown with provenance, using the local cache and honoring
    offline mode. ``_fetcher`` is a test seam (defaults to :func:`_http_get`)."""
    if not _valid_url(url):
        return fail("bad_url", "Provide an absolute http(s) URL, e.g. https://example.org/page.")

    store = cache if cache is not None else Cache()
    offline = is_offline()
    hit = store.get(url, tier=tier, allow_stale=offline)

    # Serve cache unless a fresh copy was explicitly forced (offline always serves cache).
    if hit is not None and (offline or not force):
        return _from_cache(hit, offline=offline)

    if offline:
        return fail(
            "offline",
            "Offline mode is on and this URL is not cached. Disable offline_mode or fetch it while online.",
        )

    try:
        content, final_url, title = (_fetcher or _http_get)(url)
    except Exception as exc:  # noqa: BLE001
        stale = store.get(url, tier=tier, allow_stale=True)
        if stale is not None:
            return _from_cache(stale, offline=False)  # stale beats nothing on a fetch error
        return fail("network", f"Could not fetch the page: {exc}. Check the URL / connectivity.")

    fetched_ts = now_ts()
    store.put(url, content, tier=tier, fetched_ts=fetched_ts,
              meta={"title": title, "final_url": final_url})
    return envelope(
        f"{title or url} — fetched (readability->markdown, {len(content)} chars)",
        source_url=final_url or url,
        quote=None,
        fetched_ts=fetched_ts,
        confidence=HIGH,
        cached=False,
        url=url,
        title=title,
        content=content,
    )
