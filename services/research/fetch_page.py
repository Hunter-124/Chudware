"""``fetch_page(url)`` — contract ``contracts/research_tools.md`` §3.

Built-in **readability -> markdown** using only the standard library (``html.parser``,
``re``) so there is **zero new hard dependency** and the offline gate/CI stay green.
Firecrawl ``/scrape`` is used only when ``FIRECRAWL_API_KEY`` is present (hard/JS pages);
its absence degrades to the built-in extractor, never an error. Fetched content is cached
under the per-user knowledge cache (§4); offline mode serves cache only (§5).
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------------------
# Redirect-walk budget: this tool's own worst case MUST fit its harness ceiling.
# ---------------------------------------------------------------------------
# research_fetch_page runs under ONE outer deadline — its curated ``timeout_s``
# (services/agent/tool_guidance/research.py, 120 s) enforced by
# agent_turn._dispatch_tool's ThreadPoolExecutor. ``_http_get`` opens a FRESH
# httpx request per redirect hop, so a PER-HOP timeout MULTIPLIES: 10 redirects
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


class OutboundPolicyError(ValueError):
    """The requested URL is not an eligible public research destination."""


class RedirectBudgetError(RuntimeError):
    """The redirect walk exhausted ``_WALK_BUDGET_S`` before reaching a page.

    Distinct from :class:`OutboundPolicyError`: the chain was *eligible*, just
    too slow to finish inside this tool's harness deadline. The message names the
    hop it gave up on so the model can act (fetch the final URL directly, or pick
    a different source) instead of retrying the same slow chain.
    """


def _public_address(value: str) -> bool:
    """Return true only for globally routable IPv4/IPv6, including mapped forms."""
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global


def _validate_public_url(url: str) -> None:
    """Reject malformed, credential-bearing, non-HTTPS, or non-public destinations."""
    try:
        parsed = urlparse(str(url))
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise OutboundPolicyError("invalid URL") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise OutboundPolicyError("research fetch requires a public HTTPS URL")
    port = port or 443
    try:
        literal = ipaddress.ip_address(parsed.hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not _public_address(str(literal)):
            raise OutboundPolicyError("destination address is not public")
        return
    try:
        answers = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM,
                                     proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise OutboundPolicyError("destination DNS resolution failed") from exc
    addresses = [str(sockaddr[0]) for family, _type, _proto, _canon, sockaddr in answers
                 if family in (socket.AF_INET, socket.AF_INET6)]
    if not addresses or any(not _public_address(address) for address in addresses):
        raise OutboundPolicyError("destination DNS includes a non-public address")

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
    parser.feed(html_text)
    md = parser.markdown()
    if len(md) > _MAX_CONTENT:
        md = md[:_MAX_CONTENT] + "\n\n… [truncated]"
    return md, parser.title.strip()


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


def _http_get(url: str) -> tuple[str, str, str]:
    """Fetch a public HTTPS page, revalidating DNS and every redirect target.

    The redirect walk is bounded by ``_WALK_BUDGET_S`` in TOTAL (see the budget
    block at the top of this module): a hop that would start past the budget
    raises :class:`RedirectBudgetError` naming that hop, instead of letting
    per-hop timeouts multiply past this tool's harness deadline.
    """
    import httpx

    current = url
    started = time.monotonic()
    with httpx.Client(headers={"User-Agent": _UA}, timeout=_HOP_TIMEOUT_S,
                      follow_redirects=False) as client:
        for hop in range(_MAX_REDIRECTS + 1):
            elapsed = time.monotonic() - started
            if hop and elapsed >= _WALK_BUDGET_S:
                raise RedirectBudgetError(
                    f"gave up at redirect hop {hop} ({current}) after "
                    f"{elapsed:.1f}s of the {_WALK_BUDGET_S:.0f}s redirect budget")
            # One DNS revalidation per hop, at the top, before any connection is
            # opened to `current`. The redirect target used to be validated a
            # second time right after urljoin below; that was a strict duplicate
            # of this check on the next iteration (nothing is fetched in between),
            # so dropping it halves the uncancellable getaddrinfo work per hop
            # without weakening the policy.
            _validate_public_url(current)
            resp = client.get(current)
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise OutboundPolicyError("redirect omitted Location")
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            md, title = html_to_markdown(resp.text)
            return md, current, title
    raise OutboundPolicyError("too many redirects")


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
        content, final_url, title = (_fetcher or _http_get)(url)
    except Exception as exc:  # noqa: BLE001
        stale = store.get(url, tier=tier, allow_stale=True)
        if stale is not None:
            return _from_cache(stale, offline=False)  # stale beats nothing on a fetch error
        if isinstance(exc, RedirectBudgetError):
            # contract §6 `reason` is a closed token set, so this stays "network";
            # the hint carries the hop and the budget so the remedy is actionable
            # instead of "retry the same slow chain".
            return fail(
                "network",
                f"The redirect chain for this URL is too slow: {exc}. Fetch the "
                "final URL directly, or pick a different source.",
            )
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
