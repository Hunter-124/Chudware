"""``web_search(query, provider?)`` — contract ``contracts/research_tools.md`` §2.

Returns the §1 answer envelope plus ``query``, ``provider``, ``results[]``. Search needs
live network, so v1 does not cache search results: in offline mode it returns the §6
``offline`` degradation (use a cached ``fetch_page`` instead).
"""
from __future__ import annotations

from typing import Optional, Union

from .common import envelope, fail, is_offline, now_ts
from .providers import SearchProvider, select_provider


def web_search(
    query: str,
    provider: Union[str, SearchProvider, None] = None,
    max_results: int = 5,
) -> dict:
    """Search the web. ``provider`` may be a provider name (``"tavily"``/``"ddg"``), a
    :class:`SearchProvider` instance (used directly — the test seam), or ``None`` for
    auto-selection (contract §2.1)."""
    if not query or not str(query).strip():
        return fail("bad_query", "Provide a non-empty search query.")

    if is_offline():
        return fail(
            "offline",
            "Offline mode is on; web_search needs network. Disable offline_mode, or use a cached fetch_page.",
        )

    if isinstance(provider, SearchProvider):
        prov = provider
    else:
        prov = select_provider(provider)

    try:
        out = prov.search(str(query), max_results=max_results)
    except Exception as exc:  # noqa: BLE001 — degrade, never raise across the boundary
        return fail(
            "network",
            f"Search via '{getattr(prov, 'name', '?')}' failed: {exc}. "
            "Set TAVILY_API_KEY for a keyed provider, or retry.",
        )

    results = out.get("results", []) or []
    top_url = results[0].get("url") if results else None
    return envelope(
        out.get("answer"),
        units=None,
        source_url=top_url,
        quote=None,
        fetched_ts=now_ts(),
        confidence=out.get("confidence", "high"),
        cached=False,
        query=str(query),
        provider=prov.name,
        results=results,
    )
