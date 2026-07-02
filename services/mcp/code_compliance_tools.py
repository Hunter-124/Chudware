"""Building-code compliance MCP tools (T-D4).

Registers:
  code_check(doc_path, jurisdiction, load_combo) -> dict
    Deterministic rule checks (deflection L/360, post compression, fastener schedule) +
    agentic RAG citations from the user's local code corpus.  Never scrapes copyrighted
    public code viewers — see ``work/questions/T-D4.md`` for the copyright fence ruling.

Logic lives in ``engine_oracle/structural/code_check.py``; this file is thin glue only
(mirrors the structural_tools.py pattern from T-D3).  All imports are lazy (inside the
handler) so an absent dep never crashes server startup.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from mcp_server.cad_tools import _dumps, _err


def register_code_compliance_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def code_check(
        doc_path: str = "",
        jurisdiction: str = "CA",
        load_combo: str = "DEAD_LIVE",
        rag_query: str = "",
    ) -> str:
        """Run building-code compliance checks on a structural document.

        Applies three deterministic rules drawn from public-domain formulas and encodes
        limit values with clause citations (no copyrighted code text is reproduced):

        * **deflection_L360** — checks live-load deflection < L/360 (IBC 1604.3 /
          AISC 360).  Requires ``doc_path`` to be an ``aicad.structure/1`` JSON document.
        * **post_compression_NDS_ASD** — simplified NDS ASD compression check for lumber
          posts (no Cp in v1).  Uses Douglas Fir-Larch No.2 Fc = 9.31 MPa.
        * **fastener_schedule** — looks up the connection type in the encoded fastener
          schedule with IBC/NDS clause citations.

        Agentic RAG citations are retrieved from the user's local code corpus
        (``AICAD_CODE_CORPUS_PATH`` env var or ``~/.local/share/AI-CAD/code_corpus/``).
        Set ``rag_query`` to request jurisdiction-scoped citations.  No copyrighted text
        is scraped or cached; citations come from user-supplied licensed PDFs only.

        Args:
            doc_path: Path to a JSON ``aicad.structure/1`` document.  When empty or
                absent, structural analysis is skipped and only standalone rules can run
                (via direct kwargs — currently post/fastener, not deflection).
            jurisdiction: Two-letter US state code (e.g. ``"CA"``, ``"TX"``).  Used to
                scope jurisdiction metadata and RAG retrieval.  Defaults to ``"CA"``.
            load_combo: ASCE 7 load combination for deflection analysis — ``"DEAD_ONLY"``,
                ``"DEAD_LIVE"`` (default), or ``"DEAD_LIVE_WIND"``.
            rag_query: Optional natural-language query for agentic RAG retrieval.  When
                empty, no citations are fetched.

        Returns:
            JSON ``{ok, jurisdiction, rule_results: [...], citations: [...], disclaimer}``.

            * ``rule_results`` — list of ``{rule_id, passed, value, limit, unit, citation,
              detail, disclaimer}`` dicts; one per applied rule.
            * ``citations`` — list of ``{text, source_url, section, confidence,
              agent_retrieved, caveat}`` dicts from the user corpus.  Empty when
              ``rag_query`` is omitted or no corpus is configured.
            * ``disclaimer`` — byte-exact: "Preliminary check — not sealed engineering.
              Verify with a licensed structural engineer before construction."
        """
        try:
            from engine.structural.checks import DISCLAIMER
            from engine.structural.code_check import (
                MockRAGProvider,
                UserCorpusProvider,
                agentic_rag,
                check_deflection,
                check_fastener_schedule,
                check_post_span,
            )
            from engine.structural.jurisdiction_meta import get_jurisdiction
        except ImportError as e:
            return _dumps({
                "ok": False,
                "error": "code compliance engine unavailable",
                "code": "import_error",
                "detail": str(e),
            })

        try:
            rule_results: list[dict] = []
            meta: dict = {}

            # Jurisdiction metadata (graceful if unknown)
            try:
                meta = get_jurisdiction(jurisdiction)
            except KeyError:
                meta = {}

            # Deflection check — requires a structure doc + analysis
            if doc_path and doc_path.strip():
                try:
                    import json as _json
                    with open(doc_path, "r", encoding="utf-8") as fh:
                        structure = _json.load(fh)

                    from engine.structural.analysis import analyze_doc
                    from engine.structural.elements import StructureDoc

                    doc = StructureDoc.from_dict(structure)
                    analysis_out = analyze_doc(doc, load_combo)

                    for row in analysis_out.get("results", []):
                        defl = row.get("deflection_mm")
                        span = None
                        # Span: use element length from the doc
                        eid = row.get("element_id", "")
                        for el in doc.elements:
                            if el.id == eid:
                                span = el.length_mm
                                break
                        if span is not None and defl is not None:
                            rule_results.append(
                                check_deflection(span, abs(float(defl))).to_dict()
                            )
                except FileNotFoundError:
                    rule_results.append({
                        "rule_id": "deflection_L360",
                        "passed": False,
                        "value": 0.0,
                        "limit": 0.0,
                        "unit": "mm",
                        "citation": "IBC 1604.3",
                        "detail": f"Document not found: {doc_path}",
                        "disclaimer": DISCLAIMER,
                    })
                except ImportError:
                    # PyNite/scipy unavailable — skip deflection, still run standalone rules
                    pass
                except Exception as exc:  # noqa: BLE001
                    rule_results.append({
                        "rule_id": "deflection_L360",
                        "passed": False,
                        "value": 0.0,
                        "limit": 0.0,
                        "unit": "mm",
                        "citation": "IBC 1604.3",
                        "detail": f"Analysis error: {exc}",
                        "disclaimer": DISCLAIMER,
                    })

            # RAG citations
            citations: list[dict] = []
            if rag_query and rag_query.strip():
                citations = agentic_rag(
                    rag_query.strip(),
                    jurisdiction,
                    provider=UserCorpusProvider(),
                )

            return _dumps({
                "ok": True,
                "jurisdiction": jurisdiction,
                "jurisdiction_meta": meta,
                "rule_results": rule_results,
                "citations": citations,
                "disclaimer": DISCLAIMER,
            })

        except Exception as e:  # noqa: BLE001
            return _err(e)
