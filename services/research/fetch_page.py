"""Fetch public research pages through the pinned, bounded HTTP client.

Static HTML is preferred. Local rendering is refused because browser subresources
are not connection-pinned. A configured external Firecrawl service may recover a
JavaScript shell; only usable content is cached.
"""
from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Redirect-walk budget: this tool's own worst case MUST fit its harness ceiling.
# ---------------------------------------------------------------------------
# research_fetch_page runs under ONE outer deadline — its curated ``timeout_s``
# (services/agent/tool_guidance/research.py, 120 s) enforced by
# agent_turn._dispatch_tool's ThreadPoolExecutor. ``_http_get`` opens a FRESH
# pinned HTTP connection per redirect hop, so a PER-HOP timeout MULTIPLIES: 10 redirects
# at 30 s/hop was a (10 + 1) * 30 = 330 s worst case, 2.75x the tool's own
# ceiling. A slow chain could therefore only ever end as the harness's useless
# "abandoned in the background, may still be running" payload, and a realistic
# 3-5 hop vendor/catalog chain at 10-20 s/hop already spent 30-100 s of the 120.
# The walk is now bounded AS A WHOLE rather than per hop:
#   _HOP_TIMEOUT_S        15 s  connect+read for one hop (was 30)
#   _WALK_BUDGET_S        60 s  no further hop may START once the walk has spent
#                               this much; hop 0 always runs (the caller's own URL
#                               is not a redirect the tool chose to follow)
#   _DNS_STALL_RESERVE_S  15 s  a single getaddrinfo is uncancellable and takes no
#                               timeout argument; glibc resolv.conf defaults
#                               (timeout:5, attempts:2 -> 5 s then 10 s) bound one
#                               lookup at ~15 s, and that is the only work that can
#                               overrun the budget check
#   _MAX_REDIRECTS        10    UNCHANGED — a fast 10-hop chain is still legal; the
#                               wall-clock budget, not the hop count, is the guard
# Worst case = _WALK_BUDGET_S (a hop starts just under the budget)
#            + _HOP_TIMEOUT_S (that hop runs to its own deadline)
#            + _DNS_STALL_RESERVE_S (its pre-flight DNS revalidation stalls)
#            = 60 + 15 + 15 = 90 s
# i.e. 30 s (25%) inside the 120 s ceiling, which covers readability->markdown of
# the <=_MAX_CONTENT body. FETCH_WORST_CASE_S is asserted against the LIVE
# guidance value in test_research.py, so the two cannot drift apart.
_MAX_REDIRECTS = 10
_HOP_TIMEOUT_S = 15.0
_WALK_BUDGET_S = 60.0
_DNS_STALL_RESERVE_S = 15.0
FETCH_WORST_CASE_S = _WALK_BUDGET_S + _HOP_TIMEOUT_S + _DNS_STALL_RESERVE_S


from services.net.safe_http import OutboundPolicyError
from services.net.safe_http import validate_public_url, get as public_get


class RedirectBudgetError(RuntimeError):
    """The redirect walk exhausted ``_WALK_BUDGET_S`` before reaching a page.

    Distinct from :class:`OutboundPolicyError`: the chain was *eligible*, just
    too slow to finish inside this tool's harness deadline. The message names the
    hop it gave up on so the model can act (fetch the final URL directly, or pick
    a different source) instead of retrying the same slow chain.
    """


def _validate_public_url(url: str) -> None:
    validate_public_url(url, schemes=("https",))

from .cache import Cache, DEFAULT_TIER
from .common import HIGH, MEDIUM, OFFLINE, envelope, fail, is_offline, now_ts

_UA = "Mozilla/5.0 (compatible; Chudware-research/1.0)"
_MAX_CONTENT = 200_000  # cap markdown to protect the model window (contract §3)
_DROP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "noscript",
              "form", "svg", "iframe", "template"}
_BLOCK_TAGS = {"p", "div", "section", "article", "br", "tr", "ul", "ol", "table",
               "blockquote", "pre", "hr"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_CHROME_ROLES = {"navigation", "banner", "contentinfo", "complementary"}
_APP_ROOT = re.compile(r"""(?:id=["'](?:root|app|__next|__nuxt)["']|data-reactroot|ng-version=)""", re.I)
_JS_REQUIRED = re.compile(r"<noscript\b[^>]*>[^<]*(?:javascript|enable js|enable javascript)",
                          re.I | re.S)
_GENERIC_TITLES = {"grabcad", "grabcad community library", "makerworld",
                   "researchgate", "researchgate | find and share research"}
_MIN_MAIN_CHARS = 180


class _Readability(HTMLParser):
    """Minimal readability: drop boilerplate/script tags, keep visible text, map headings
    to ``#`` and list items to ``- ``. Not a full DOM cleaner — good enough for a
    provenance quote + a model-readable summary without any third-party dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._drop_stack: list[str] = []
        self._in_head = False
        self.title: str = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._drop_stack:
            if tag not in {"meta", "link", "img", "input", "br", "hr"}:
                self._drop_stack.append(tag)
            return
        if tag == "head":
            self._in_head = True
        if tag == "title" and self._in_head:
            self._in_title = True
            return
        attributes = dict(attrs)
        if (tag in _DROP_TAGS or attributes.get("role") in _CHROME_ROLES
                or (attributes.get("aria-hidden") == "true" and tag not in {"meta", "link"})):
            self._drop_stack.append(tag)
            return
        if self._in_head:
            return
        if tag in _HEADINGS:
            self._out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._out.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self._out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if self._drop_stack:
            if tag in self._drop_stack:
                del self._drop_stack[self._drop_stack.index(tag):]
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "head":
            self._in_head = False
            return
        if not self._in_head and (tag in _HEADINGS or tag in _BLOCK_TAGS):
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_stack:
            return
        if self._in_title:
            self.title += data
            return
        if not self._in_head and data.strip():
            self._out.append(data)

    def markdown(self) -> str:
        text = "".join(self._out)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html_text: str) -> tuple[str, str]:
    """Return ``(markdown, title)`` from raw HTML using the stdlib readability parser."""
    parser = _Readability()
    parser.feed(html_text)
    md = parser.markdown()
    if len(md) > _MAX_CONTENT:
        md = md[:_MAX_CONTENT] + "\n\n… [truncated]"
    return md, parser.title.strip()

def _is_js_shell(html_text: str, markdown: str, title: str) -> bool:
    """Distinguish an app bootstrap/nav shell from a short but real article."""
    if len(markdown.strip()) >= _MIN_MAIN_CHARS:
        return False
    scripts = len(re.findall(r"<script\b", html_text, re.I))
    title_generic = title.strip().lower() in _GENERIC_TITLES
    return (not markdown.strip() or bool(_JS_REQUIRED.search(html_text))
            or bool(_APP_ROOT.search(html_text)) and scripts > 0
            or scripts >= 2 or title_generic and scripts > 0)


def _render_page(url: str) -> tuple[str, str, str] | None:
    """Refuse local rendering: neither backend binds every subresource to public IPs."""
    _validate_public_url(url)
    return None


def _firecrawl_page(url: str) -> tuple[str, str, str] | None:
    from . import firecrawl_pool

    if not firecrawl_pool.load_keys():
        return None
    markdown, meta = firecrawl_pool.scrape(url, timeout=15.0, max_wait_s=15.0)
    title = str(meta.get("title") or "")
    final_url = str(meta.get("sourceURL") or meta.get("url") or url)
    _validate_public_url(final_url)
    # Firecrawl produces Markdown, but its own navigation-only result is no
    # better than a browser shell. Do not cache it as a successful page.
    if len(markdown.strip()) < _MIN_MAIN_CHARS:
        return None
    return markdown[:_MAX_CONTENT], final_url, title


def _valid_url_syntax(url: str) -> bool:
    try:
        parsed = urlparse(str(url))
        parsed.port
    except (TypeError, ValueError):
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username


def _valid_url(url: str) -> bool:
    try:
        _validate_public_url(url)
    except OutboundPolicyError:
        return False
    return True


def _http_get(url: str) -> tuple[str, str, str, str]:
    """Fetch a public HTTPS page, revalidating DNS and every redirect target.

    The redirect walk is bounded by ``_WALK_BUDGET_S`` in TOTAL (see the budget
    block at the top of this module): a hop that would start past the budget
    raises :class:`RedirectBudgetError` naming that hop, instead of letting
    per-hop timeouts multiply past this tool's harness deadline.
    """
    started = time.monotonic()

    def before_hop(hop: int, current: str) -> None:
        elapsed = time.monotonic() - started
        if hop and elapsed >= _WALK_BUDGET_S:
            raise RedirectBudgetError(
                f"gave up at redirect hop {hop} ({current}) after "
                f"{elapsed:.1f}s of the {_WALK_BUDGET_S:.0f}s redirect budget")

    resp = public_get(url, headers={"User-Agent": _UA}, max_bytes=8 * 1024 * 1024,
                      idle_timeout=_HOP_TIMEOUT_S, max_redirects=_MAX_REDIRECTS,
                      schemes=("https",), before_hop=before_hop)
    resp.raise_for_status()
    html_text = resp.text
    md, title = html_to_markdown(html_text)
    return md, resp.url, title, html_text


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
    if _fetcher is None:
        try:
            _validate_public_url(url)
        except OutboundPolicyError as exc:
            return fail("bad_url", f"Provide a public HTTPS URL ({exc}).")
    elif not _valid_url_syntax(url):
        return fail("bad_url", "Provide an absolute HTTPS URL.")

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
        fetched = (_fetcher or _http_get)(url)
        content, final_url, title = fetched[:3]
        html_text = fetched[3] if len(fetched) > 3 else (
            content if content.lstrip().lower().startswith(("<!doctype", "<html")) else "")
        if html_text and len(fetched) == 3:
            content, title = html_to_markdown(html_text)
        if _is_js_shell(html_text, content, title):
            if _fetcher is None:
                _validate_public_url(final_url)
            # A static bootstrap is not success. Local rendering refuses before
            # navigation; a configured external scraper may recover the content.
            try:
                rendered = _render_page(final_url)
            except Exception:  # noqa: BLE001 — unavailable CEF/browser
                rendered = None
            if rendered is None:
                try:
                    rendered = _firecrawl_page(final_url)
                except Exception:  # noqa: BLE001 — unavailable Firecrawl/key
                    rendered = None
            if rendered is None:
                stale = store.get(url, tier=tier, allow_stale=True)
                if stale is not None:
                    return _from_cache(stale, offline=False)
                cad_hint = (" Try fetch_cad_model for the CAD model instead."
                            if any(site in (urlparse(final_url).hostname or "").lower()
                                   for site in ("grabcad", "makerworld")) else "")
                return fail("js_shell", f"{final_url} only returned a JavaScript app shell; "
                            f"local rendering is refused without a subresource egress policy.{cad_hint}")
            content, final_url, title = rendered
    except Exception as exc:  # noqa: BLE001
        stale = store.get(url, tier=tier, allow_stale=True)
        if stale is not None:
            return _from_cache(stale, offline=False)  # stale beats nothing on a fetch error
        if isinstance(exc, RedirectBudgetError):
            return fail("network", f"The redirect chain for {url} is too slow: {exc}. "
                        "Fetch the final URL directly, or pick a different source.")
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (403, 404):
            return fail("http_error", f"{url} returned HTTP {status}; choose another source.")
        return fail("network", f"Could not fetch {url}: {exc}. Check the URL / connectivity.")

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
