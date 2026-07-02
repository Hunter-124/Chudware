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

    # --- T-F2 source helpers (same §1 answer envelope; bundled-first, provenance-stamped) ---

    @mcp.tool()
    def research_material_lookup(material: str, property: Optional[str] = None) -> str:
        """Look up material properties (yield/ultimate/modulus/density/...) with provenance.

        Serves the bundled unified material registry first (`confidence:"bundled"`,
        first-party handbook nominal values) — pass `property` (e.g. "yield", "density")
        for one SI-normalised quantity, or omit it for the full set. A material not in the
        registry falls through to cited web search when online (never fabricates a value,
        never touches MatWeb/MakeItFrom). Quote the returned value verbatim.
        """
        try:
            return _dumps(research.material_lookup(material, property=property))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_thread_spec(designation: str) -> str:
        """Screw-thread dimensions COMPUTED from ISO 724 / ASME B1.1 (never fetched).

        Accepts metric (`M8`, `M8x1.25`) or Unified-inch (`1/4-20 UNC`, `#10-24`)
        designations; returns pitch/minor diameters and the tensile stress area. Bundled
        (`confidence:"bundled"`) — the standard is cited in `quote`.
        """
        try:
            return _dumps(research.thread_spec(designation))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_standard_section(designation: str) -> str:
        """Structural section properties: AISC W-shapes (`W8x31`) and ASME B36.10M pipe
        (`pipe 2 sch 40`). Returns area / moment of inertia / section modulus from the
        bundled official dimensional tables (`confidence:"bundled"`).
        """
        try:
            return _dumps(research.standard_section(designation))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_unit_convert(value: float, unit: str, family: str = "length") -> str:
        """Convert a value to its canonical SI unit deterministically (code converts, not
        the model — the ksi/MPa 6.9x error). `family` is `length` (->mm), `angle` (->deg)
        or `count`. A unit that does not belong to the family is rejected
        (`{ok:false, reason:"bad_unit"}`), never silently coerced.
        """
        try:
            return _dumps(research.unit_convert(value, unit, family=family))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_eng_calc(formula: str, params: Optional[dict] = None,
                          raw_quote: Optional[str] = None) -> str:
        """Evaluate a named deterministic engineering formula in SI (code does the maths).

        `formula` is one of `axial_stress`, `bending_stress`, `hoop_stress`,
        `safety_factor`, `cantilever_end_deflection`, `circle_area`; `params` is its input
        dict (e.g. {"force_n":1000,"area_mm2":100}). Result is range-checked; a bad/unknown
        formula degrades (`{ok:false}`) rather than raising.
        """
        try:
            kw = dict(params or {})
            if raw_quote is not None:
                kw["raw_quote"] = raw_quote
            return _dumps(research.eng_calc(formula, **kw))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_datasheet_fetch(source: str, tier: str = "datasheet") -> str:
        """Extract a datasheet PDF (local path or http(s) URL) to markdown with provenance.

        Uses only the MIT-licensed `pdfminer.six` extractor (AGPL PyMuPDF is excluded); if
        that optional dependency is absent it degrades (`{ok:false, reason:"no_pdf_tool"}`).
        Returns clean `content` markdown you should quote verbatim for any factual claim.
        """
        try:
            return _dumps(research.datasheet_fetch(source, tier=tier))
        except Exception as e:  # noqa: BLE001
            return _err(e)
