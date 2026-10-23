"""Building-code compliance MCP tools.

Registers:
  code_check(doc_path, jurisdiction, load_combo, rag_query,
             post_size, post_load_kn, connection_type) -> dict
    Deterministic rule checks (deflection L/360, post compression, fastener schedule) +
    agentic RAG citations from the user's local code corpus.  Never scrapes copyrighted
    public code viewers.

Logic lives in ``engine_oracle/structural/code_check.py`` (stays Python — its
agentic_rag/UserCorpusProvider is corpus retrieval, not ported); this file is thin
glue only.  The deflection analysis it consumes is dispatched to the C++ core via
``core_bridge`` (``struct_analyze`` RPC) so the module keeps working after the legacy
structural oracle modules are deleted.  Oracle imports stay lazy (inside the handler)
so an absent dep never crashes server startup.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import core_bridge as cb
from .core_bridge import CoreError
from .fs_tools import _canonical_target

from services.mcp.cad_tools import _dumps, _err


def register_code_compliance_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def code_check(
        doc_path: str = "",
        jurisdiction: str = "CA",
        load_combo: str = "DEAD_LIVE",
        rag_query: str = "",
        post_size: str = "",
        post_load_kn: float = 0.0,
        connection_type: str = "",
    ) -> str:
        """Run building-code compliance checks on a structural document.

        Applies three deterministic rules drawn from public-domain formulas and encodes
        limit values with clause citations (no copyrighted code text is reproduced):

        * **deflection_L360** — checks live-load deflection < L/360 (IBC 1604.3 /
          AISC 360).  Requires ``doc_path`` to be an ``aicad.structure/1`` JSON document.
        * **post_compression_NDS_ASD** — simplified NDS ASD compression check for lumber
          posts (no Cp in v1).  Uses Douglas Fir-Larch No.2 Fc = 9.31 MPa with dressed
          section areas from the structural lumber table.  Runs when ``post_size`` is given.
        * **fastener_schedule** — looks up the connection type in the encoded fastener
          schedule with IBC/NDS clause citations.

        Agentic RAG citations are retrieved from the user's local code corpus
        (``CHUDWARE_CODE_CORPUS_PATH`` env var or ``~/.local/share/Chudware/code_corpus/``).
        Set ``rag_query`` to request jurisdiction-scoped citations.  No copyrighted text
        is scraped or cached; citations come from user-supplied licensed PDFs only.

        Args:
            doc_path: Path to a JSON ``aicad.structure/1`` document.  When empty or
                absent, structural analysis is skipped and only the standalone rules
                (``post_size``/``post_load_kn`` and ``connection_type``) can run.
            jurisdiction: Two-letter US state code (e.g. ``"CA"``, ``"TX"``).  Used to
                scope jurisdiction metadata and RAG retrieval.  Defaults to ``"CA"``.
            load_combo: ASCE 7 load combination for deflection analysis — ``"DEAD_ONLY"``,
                ``"DEAD_LIVE"`` (default), or ``"DEAD_LIVE_WIND"``.
            rag_query: Optional natural-language query for agentic RAG retrieval.  When
                empty, no citations are fetched.
            post_size: Optional nominal lumber post size (e.g. ``"4x4"``, ``"6x6"``).
                When non-empty, runs the post compression check with ``post_load_kn``.
                Unknown sizes fall back to the smallest listed post (4x4, conservative).
            post_load_kn: Applied compressive post load in kN (used with ``post_size``).
            connection_type: Optional connection key for the fastener-schedule check
                (e.g. ``"post_base"``, ``"ledger_to_band_joist"``).  When non-empty,
                runs the fastener-schedule lookup.

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
        if doc_path and doc_path.strip():
            target, blocked = _canonical_target(doc_path)
            if blocked:
                return _dumps(blocked)
            doc_path = str(target)

        try:
            from services.structural.code_check import (
                DISCLAIMER,
                UserCorpusProvider,
                agentic_rag,
                check_deflection,
                check_fastener_schedule,
                check_post_span,
            )
            from services.structural.jurisdiction_meta import get_jurisdiction
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

            # Deflection check — requires a structure doc + analysis (C++ core RPC)
            if doc_path and doc_path.strip():
                try:
                    import json as _json
                    with open(doc_path, "r", encoding="utf-8") as fh:
                        structure = _json.load(fh)

                    # element_id -> span (mm) map, straight from the raw doc
                    span_by_id = {
                        el.get("id"): el.get("length_mm")
                        for el in structure.get("elements", [])
                        if isinstance(el, dict)
                    }

                    analysis_out = cb.call(
                        "struct_analyze", {"doc_path": doc_path, "load_combo": load_combo})
                    if analysis_out.get("code") and analysis_out.get("message"):
                        # In-band soft failure from the core (bad schema, solver error).
                        raise RuntimeError(analysis_out["message"])

                    for row in analysis_out.get("results", []):
                        defl = row.get("deflection_mm")
                        span = span_by_id.get(row.get("element_id", ""))
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
                except CoreError as exc:
                    # C++ engine unavailable (libchudware not staged / stale lib without
                    # the struct_* dispatch) — record an advisory row instead of
                    # silently returning ok:true with no deflection result.
                    rule_results.append({
                        "rule_id": "deflection_L360",
                        "passed": False,
                        "skipped": True,
                        "value": 0.0,
                        "limit": 0.0,
                        "unit": "mm",
                        "citation": "IBC 1604.3",
                        "detail": (
                            "deflection check skipped: structural solver unavailable "
                            f"({exc}) — stage libchudware with the struct_* RPCs to enable "
                            "structural deflection analysis."
                        ),
                        "disclaimer": DISCLAIMER,
                    })
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

            # Post compression check — standalone kwargs
            if post_size and post_size.strip():
                rule_results.append(
                    check_post_span(post_size.strip(), post_load_kn).to_dict()
                )

            # Fastener schedule check — standalone kwarg
            if connection_type and connection_type.strip():
                rule_results.append(
                    check_fastener_schedule(connection_type.strip()).to_dict()
                )

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
