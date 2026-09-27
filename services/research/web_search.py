"""``web_search(query, provider?)`` — contract ``contracts/research_tools.md`` §2.

Returns the §1 answer envelope plus ``query``, ``provider``, ``results[]``. Search needs
live network, so v1 does not cache search results: in offline mode it returns the §6
``offline`` degradation (use a cached ``fetch_page`` instead).
"""
from __future__ import annotations

import os
import re
from typing import Union

from .common import envelope, fail, is_offline, now_ts
from .providers import (DDGProvider, ProviderBlocked, SearchProvider, TavilyProvider,
                        select_provider)


def _relax_query(query: str) -> str:
    """Drop numeric exact-match clauses and the narrow tail of a long query once."""
    without_numbers = re.sub(r'"[+-]?\d+(?:\.\d+)?"', "", query)
    terms = without_numbers.split()
    if len(terms) > 3:
        terms = terms[:min(4, max(3, len(terms) // 2))]
    return " ".join(terms).strip()


def _candidates(provider: Union[str, SearchProvider, None]) -> list[SearchProvider]:
    if isinstance(provider, SearchProvider):
        return [provider]  # an injected provider is an isolated test/user-selected seam
    first = select_provider(provider)
    candidates = [first]
    for alternate in (TavilyProvider, DDGProvider):
        if (alternate.name != first.name
                and (not alternate.requires_key or os.environ.get(alternate.requires_key))):
            candidates.append(alternate())
    return candidates


def web_search(
    query: str,
    provider: Union[str, SearchProvider, None] = None,
    max_results: int = 5,
) -> dict:
    """Search the web. ``provider`` may be a provider name (``"tavily"``/``"ddg"``), a
    :class:`SearchProvider` instance (used directly — the test seam), or ``None`` for
    auto-selection (contract §2.1)."""
    query = str(query or "").strip()
    if not query:
        return fail("bad_query", "Provide a non-empty search query.")

    if is_offline():
        return fail(
            "offline",
            "Offline mode is on; web_search needs network. Disable offline_mode, or use a cached fetch_page.",
        )

    tried: list[str] = []
    blocked: list[str] = []
    errors: list[str] = []
    relaxed = _relax_query(query)
    queries = (query, relaxed) if relaxed and relaxed != query else (query,)
    for index, prov in enumerate(_candidates(provider)):
        if index and not blocked:
            break  # alternates are for a blocked provider, not ordinary zero hits
        tried.append(prov.name)
        for searched_query in queries:
            try:
                out = prov.search(searched_query, max_results=max_results)
            except ProviderBlocked:
                blocked.append(prov.name)
                break  # another query cannot bypass a provider challenge
            except Exception as exc:  # noqa: BLE001 — degrade, never raise across the boundary
                errors.append(f"{prov.name}: {exc}")
                break
            results = out.get("results", []) or []
            if results:
                return envelope(
                    out.get("answer"),
                    source_url=results[0].get("url"),
                    fetched_ts=now_ts(),
                    confidence=out.get("confidence", "high"),
                    query=query,
                    searched_query=searched_query,
                    provider=prov.name,
                    results=results,
                )
            if searched_query != query:
                break
    if blocked:
        result = fail("provider_blocked",
                      f"Search provider(s) {', '.join(blocked)} returned an access "
                      "challenge; no results from the remaining configured providers.")
    elif errors:
        result = fail("network", f"Search failed via {', '.join(errors)}.")
    else:
        suffix = " and a relaxed query" if len(queries) > 1 else ""
        result = fail("no_results",
                      f"No search results for {query!r}{suffix}. "
                      f"Providers tried: {', '.join(tried)}.")
    result["providers_tried"] = tried
    return result
