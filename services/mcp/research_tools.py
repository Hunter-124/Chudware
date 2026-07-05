"""Research / look-it-up MCP tools — the Pillar 8 ``research_*`` family.

Thin MCP adapter over ``engine.research`` (the pure brain). Contract:
``contracts/research_tools.md``. v1 exposes ``web_search`` + ``fetch_page``; every helper
returns the §1 answer envelope (``{ok, answer, units, source_url, quote, fetched_ts,
confidence, cached}``) so a limited model quotes it verbatim and a frontier model chains
it. Failures come back as the §6 degradation shape (``{ok:false, reason, hint}``), never a
traceback — CI passes with no external keys/binaries.

T-W3 adds ``fit_calc`` and ``recommend_fit`` (ISO 286 fit calculator, Pillar 20).
These return the ``aicad.calc/1`` envelope (``contracts/advisor.md`` §7), not the §1
research envelope — different schema, same "code does the maths" discipline.
Call ``fit_calc`` BEFORE ``hole_sizing`` / bore-diameter decisions to get the ISO-table
band; use ``recommend_fit`` first when the fit class itself is not yet decided.

T-W4 adds ``research_eng_calc`` registry routing (beam, bolted_joint, thread_engagement,
press_fit, wall_thickness, snap_fit) and a ``list_calcs`` discovery tool. The existing
Pillar-8 ``research_eng_calc`` (axial_stress, hoop_stress, …) is preserved and extended:
calc_ids not found in the new registry fall through to the legacy Pillar-8 formulas.
"""
from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP

from mcp_server.cad_tools import _dumps, _err
from engine import research
import engine_oracle.research.calcs as _calcs  # registers all T-W4 calculators


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
                          raw_quote: Optional[str] = None,
                          prefilled: Optional[dict] = None) -> str:
        """Evaluate a named deterministic engineering formula in SI (code does the maths).

        **T-W4 registry** (aicad.calc/1 envelope, contracts/advisor.md §7):
        `beam`, `bolted_joint`, `thread_engagement`, `press_fit`,
        `wall_thickness`, `snap_fit`.  Call `list_calcs` for the full registry
        with input schemas.

        **Pillar-8 legacy formulas** (fallback, §1 envelope):
        `axial_stress`, `bending_stress`, `hoop_stress`, `safety_factor`,
        `cantilever_end_deflection`, `circle_area`.

        `params` is the input dict for the chosen formula.
        `prefilled` optionally records which inputs came from live geometry
        (ledger provenance per §7, e.g. `{"L_mm": "doc_bbox z body=shaft"}`).
        """
        try:
            kw = dict(params or {})
            # Route to T-W4 registry first
            entry = _calcs.get_calc(formula)
            if entry is not None:
                return _dumps(_calcs.run_calc(formula, kw, prefilled=prefilled))
            # Fall through to legacy Pillar-8 formulas
            if raw_quote is not None:
                kw["raw_quote"] = raw_quote
            return _dumps(research.eng_calc(formula, **kw))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def list_calcs() -> str:
        """Discover registered engineering calculators (T-W4 registry).

        Returns {calc_id: {input_schema, prefill_hints}} for all registered
        calculators.  Use calc_ids with ``research_eng_calc`` to compute results.
        """
        try:
            return _dumps({"ok": True, "calcs": _calcs.list_calcs()})
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

    # --- T-W3 ISO 286 fit calculator (aicad.calc/1 envelope, contracts/advisor.md §7) ---
    # IMPORTANT: call fit_calc BEFORE making any hole-sizing or bore-diameter decisions.
    # The fit class (e.g. H7/g6) must be resolved first — use recommend_fit if unknown.

    @mcp.tool()
    def fit_calc(nominal: float, fit: str,
                 prefilled: Optional[dict] = None) -> str:
        """Compute ISO 286 tolerance bands for a hole/shaft fit — code does the maths.

        Call this BEFORE hole sizing: it gives the ISO-table band the bore and shaft must
        hit. Returns an `aicad.calc/1` envelope (`contracts/advisor.md §7`) with:
        - `answer.hole`: [lower_mm, upper_mm] offsets from nominal (null if shaft-only).
        - `answer.shaft`: [lower_mm, upper_mm] offsets (null if hole-only).
        - `answer.clearance`: [min_mm, max_mm] (negative = interference). null if single.
        - `answer.fit_character`: "clearance" | "transition" | "press" | null.
        - `answer.hole_absolute` / `answer.shaft_absolute`: absolute size bands [min, max].

        `fit` accepts combined ("H7/g6", "H7\\g6") or single ("H7", "g6") forms.
        `prefilled` optionally records which inputs came from live geometry measurement
        (ledger provenance per §7, e.g. `{"nominal": "doc_eval_measure F7"}`).

        Spot-check values (ISO 286-2:2010): 20H7/g6 → hole 20.000–20.021, shaft
        19.980–19.993, clearance 0.007–0.041 mm, character=clearance.
        50H7/p6 → interference 0.001–0.042 mm, character=press.
        `source.kind` is "closed_form" for formula-derived deviations (g, f, e, d, h),
        "table" for tabulated ones (k, n, p, r, s).
        """
        try:
            return _dumps(research.fit_calc(nominal, fit, prefilled=prefilled))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def recommend_fit(function: str) -> str:
        """Map a functional description to recommended ISO 286 fit classes with rationale.

        `function` is a free-text keyword: "sliding", "press fit", "running bearing",
        "dowel pin", "location", "transition", "bearing seat", etc. Returns an
        `aicad.calc/1` envelope; `answer` is a list of matching fit recommendations,
        each with `recommended_classes`, `character` (clearance/transition/press),
        `rationale`, and `applications`.

        Use this when the fit class is not yet known — to decide WHICH fit before
        calling `fit_calc` to compute the actual bands. Example workflow:
        1. `recommend_fit("sliding")` → H7/g6
        2. `fit_calc(20.0, "H7/g6")` → hole 20.000–20.021, shaft 19.980–19.993
        3. Edit bore feature to 20.000–20.021 (or confirm with ask_user for
           function-critical dims).
        """
        try:
            return _dumps(research.recommend_fit(function))
        except Exception as e:  # noqa: BLE001
            return _err(e)
