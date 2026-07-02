"""Research / look-it-up MCP tools — the Pillar 8 ``research_*`` family.

Thin MCP adapter over ``engine.research`` (the pure brain). Contract:
``contracts/research_tools.md``. v1 exposes ``web_search`` + ``fetch_page``; every helper
returns the §1 answer envelope (``{ok, answer, units, source_url, quote, fetched_ts,
confidence, cached}``) so a limited model quotes it verbatim and a frontier model chains
it. Failures come back as the §6 degradation shape (``{ok:false, reason, hint}``), never a
traceback — CI passes with no external keys/binaries.
"""
from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from mcp_server.cad_tools import _dumps, _err
from engine import research


def register_research_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def research_web_search(query: str, provider: Optional[str] = None,
                            max_results: int = 5) -> str:
        """Search the web for up-to-date facts instead of recalling from memory.

        Returns ranked results plus (for providers that offer one) an LLM-ready `answer`,
        each with `source_url` for provenance. `provider` is optional: Tavily is used when
        `TAVILY_API_KEY` is set, else the keyless DuckDuckGo fallback (labeled
        `confidence:"limited"`). Offline mode returns `{ok:false, reason:"offline"}`.
        Use this to FIND sources, then `research_fetch_page` to READ one.
        """
        try:
            return _dumps(research.web_search(query, provider=provider,
                                              max_results=max_results))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_fetch_page(url: str, tier: str = "datasheet") -> str:
        """Fetch a page and return clean readability->markdown `content` with provenance.

        Caches per-user locally (keyed by URL) with tiered freshness — `tier` is one of
        `pricing` (hours), `datasheet` (days), `standard` (~immutable); pick by how fast
        the fact changes. `fetched_ts` and `cached` are always surfaced. Offline mode
        serves the cache only (`confidence:"offline"`) or `{ok:false, reason:"offline"}`
        on a miss. Quote the returned `content` verbatim for any factual claim.
        """
        try:
            return _dumps(research.fetch_page(url, tier=tier))
        except Exception as e:  # noqa: BLE001
            return _err(e)
