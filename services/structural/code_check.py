"""Python building-code compliance helpers: deterministic rules + user-corpus retrieval.

Two-layer approach
------------------
1. **Deterministic rule engine** — IDS/ifctester pattern (each rule is a callable that
   returns a :class:`RuleResult`).  Encodes formulas and limits as Python code with
   clause citations; no copyrighted text is reproduced.  Three rules in v1:

   * :func:`check_deflection`  — L/360 live-load limit (IBC 1604.3 / AISC 360).
   * :func:`check_post_span`   — simplified lumber post capacity (NDS ASD; Fc is the
     same public-domain Douglas Fir-Larch No.2 value as ``checks.py``; dressed section
     areas come from the dressed-lumber table via
     :func:`services.structural.sections.get_lumber_section`).
   * :func:`check_fastener_schedule` — our own encoded fastener schedule with IBC table
     citation; not a reproduction of IBC Table 2304.10.1.

2. **Agentic RAG** — :func:`agentic_rag` retrieves jurisdiction-specific citations from
   *user-supplied* PDF documents in a local corpus directory.  It does **not** scrape
   copyrighted public code viewers at runtime.

Copyright fence (binding)
-------------------------
Building codes (IBC, ASCE 7, AISC 360, NDS full text) are copyrighted.  This module:
  * Encodes **formulas and numeric limits** as code — these are mathematical facts.
  * Carries **clause citations** (§ numbers) so the engineer can look up the source.
  * Does **not** quote, paraphrase, or cache any standard body's prose.
  * The RAG side indexes only user-provided documents; nothing copyrighted ships.

Disclaimer
----------
Every :class:`RuleResult` and the :func:`run_checks` aggregate output carry the same
mandatory disclaimer as :data:`~services.structural.checks.DISCLAIMER` (imported, not
redefined):

    "Preliminary check — not sealed engineering. Verify with a licensed structural
    engineer before construction."
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Optional, Protocol, runtime_checkable

# Import the byte-exact disclaimer from checks.py — not redefined here.
from services.structural.checks import DISCLAIMER

# Dressed-lumber section properties come from the Python section library
# (services/structural/data/lumber_table.json — ANSI/AWC PS 20 dressed dims).
from services.structural.sections import get_lumber_section

# --------------------------------------------------------------------------- RuleResult


@dataclass
class RuleResult:
    """Result from a single deterministic compliance rule.

    Attributes
    ----------
    rule_id:
        Machine-readable rule identifier (e.g. ``"deflection_L360"``).
    passed:
        True if the element satisfies the limit.
    value:
        The computed demand value (same units as ``limit``).
    limit:
        The allowable/code limit value.
    unit:
        Unit string for both ``value`` and ``limit`` (e.g. ``"mm"``, ``"kN"``).
    citation:
        Clause reference — e.g. ``"IBC 1604.3 / AISC 360 L/360 live-load deflection"``.
    detail:
        Human-readable explanation of the result.
    disclaimer:
        Always :data:`~services.structural.checks.DISCLAIMER`.
    """

    rule_id: str
    passed: bool
    value: float
    limit: float
    unit: str
    citation: str
    detail: str
    disclaimer: str = field(default_factory=lambda: DISCLAIMER)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- deterministic rules


def check_deflection(span_mm: float, deflection_mm: float) -> RuleResult:
    """Check live-load deflection against the L/360 limit (IBC 1604.3).

    Formula: allowable deflection = span / 360.  The element passes if::

        deflection_mm < span_mm / 360

    Parameters
    ----------
    span_mm:
        Clear span of the member in millimetres.
    deflection_mm:
        Computed maximum live-load deflection in millimetres (absolute value used).

    Returns
    -------
    :class:`RuleResult` with ``rule_id="deflection_L360"``.

    Notes
    -----
    The L/360 ratio is the IBC live-load limit for floor members (IBC Table 1604.3).
    Other limits (L/240 for roof, L/480 for brittle finishes) are deferred to later rules.
    """
    if span_mm <= 0:
        return RuleResult(
            rule_id="deflection_L360",
            passed=False,
            value=abs(deflection_mm),
            limit=0.0,
            unit="mm",
            citation="IBC 1604.3 / AISC 360 deflection limits (L/360 live load)",
            detail="Invalid span (≤ 0): cannot compute deflection limit.",
        )

    limit_mm = span_mm / 360.0
    defl = abs(deflection_mm)
    passed = defl < limit_mm

    if passed:
        detail = (
            f"Deflection {defl:.2f} mm < L/360 = {limit_mm:.2f} mm "
            f"(span {span_mm:.0f} mm) — PASS."
        )
    else:
        detail = (
            f"Deflection {defl:.2f} mm ≥ L/360 = {limit_mm:.2f} mm "
            f"(span {span_mm:.0f} mm) — FAIL."
        )

    return RuleResult(
        rule_id="deflection_L360",
        passed=passed,
        value=defl,
        limit=limit_mm,
        unit="mm",
        citation="IBC 1604.3 / AISC 360 deflection limits (L/360 live load)",
        detail=detail,
    )


# Post compression material constant — Douglas Fir-Larch No.2, Fc = 9.307 MPa
# (same public-domain reference value as checks.py; no Cp in v1).
# Section areas come from the lumber table (get_lumber_section) — ANSI/AWC PS 20
# dressed (S4S) dimensions — NOT a hardcoded table (a previous table here overstated
# areas by 19–43%, which overstated post capacity).
_FC_MPA = 9.307
# Unknown sizes fall back to the SMALLEST listed post (conservative: understates
# capacity, never overstates it) with an explicit note in the result detail.
_FALLBACK_POST = "4x4"


def check_post_span(post_size: str, load_kn: float) -> RuleResult:
    """Simplified NDS ASD compression check for a lumber post (no column stability Cp).

    Demand = load_kn (kN).  Capacity = Fc × A (NDS ASD; no load-duration/wet-service
    factors in v1).  Section area A comes from the dressed-lumber table
    (:func:`services.structural.sections.get_lumber_section`).
    Citation: "NDS 3.6 column compression / NDS Supplement Table 4D."

    Parameters
    ----------
    post_size:
        Nominal lumber size string, e.g. ``"4x4"``, ``"4x6"``, ``"6x6"``, ``"6x8"``.
        Case-insensitive.  Unknown sizes fall back to the smallest listed post
        (``4x4`` — conservative) with an explicit note in the result detail.
    load_kn:
        Applied compressive load in kilo-Newtons (positive = compression).

    Returns
    -------
    :class:`RuleResult` with ``rule_id="post_compression_NDS_ASD"``.
    """
    key = (post_size or "").strip().lower().replace(" ", "")
    try:
        section = get_lumber_section(key)
        size_used = key
        size_note = ""
    except KeyError:
        # Unknown size: fall back to the SMALLEST listed post — this understates
        # capacity (safe direction) rather than overstating it.
        section = get_lumber_section(_FALLBACK_POST)
        size_used = _FALLBACK_POST
        size_note = (
            f" (unknown post size {post_size!r}; fell back to smallest listed post "
            f"{_FALLBACK_POST} — conservative, capacity understated)"
        )

    A_mm2 = section["A"]  # dressed area, mm² (ANSI/AWC PS 20)
    capacity_kn = _FC_MPA * A_mm2 / 1000.0  # N → kN
    demand = abs(load_kn)
    passed = demand < capacity_kn

    if passed:
        detail = (
            f"Post load {demand:.2f} kN < NDS capacity {capacity_kn:.2f} kN "
            f"(size={size_used}{size_note}) — PASS."
        )
    else:
        detail = (
            f"Post load {demand:.2f} kN ≥ NDS capacity {capacity_kn:.2f} kN "
            f"(size={size_used}{size_note}) — FAIL."
        )

    return RuleResult(
        rule_id="post_compression_NDS_ASD",
        passed=passed,
        value=demand,
        limit=round(capacity_kn, 3),
        unit="kN",
        citation="NDS 3.6 column compression / NDS Supplement Table 4D (Douglas Fir-Larch No.2 Fc)",
        detail=detail,
    )


# Fastener schedule — our own encoded requirements with IBC clause citation.
# Source: public-domain construction practice + NDS general fastening requirements.
# This is NOT a reproduction of IBC Table 2304.10.1 text.
_FASTENER_SCHEDULE: dict[str, dict] = {
    # connection_type: {fastener, min_count, min_length_mm, note}
    "ledger_to_band_joist": {
        "fastener": "1/2in lag screw or through-bolt",
        "min_count": 2,
        "min_length_mm": 38.1,  # 1.5 in minimum penetration into solid lumber
        "note": "per IBC 2304.9.5 / AWC DCA6 ledger provisions",
    },
    "post_base": {
        "fastener": "structural post base connector (e.g. Simpson ABU/ABA series)",
        "min_count": 1,
        "min_length_mm": 0.0,
        "note": "connector must be rated for uplift per NDS 10.2 / IBC 2308.6",
    },
    "rim_joist_to_sill": {
        "fastener": "16d common nail (3.5 in / 88.9 mm)",
        "min_count": 3,
        "min_length_mm": 88.9,
        "note": "IBC 2304.9 nailing schedule — rim joist to sill plate",
    },
    "rafter_to_top_plate": {
        "fastener": "hurricane/rafter tie (e.g. Simpson H2.5)",
        "min_count": 1,
        "min_length_mm": 0.0,
        "note": "IBC 2308.8.5 / AWC WFCM rafter-to-plate connection",
    },
    "shear_wall_anchor": {
        "fastener": "1/2in anchor bolt with 3x3x0.229 plate washer",
        "min_count": 2,
        "min_length_mm": 178.0,  # 7 in minimum embedment per IBC 2308.3.1
        "note": "IBC 2308.3.1 sill plate anchor bolts",
    },
}


def check_fastener_schedule(connection_type: str) -> RuleResult:
    """Check that a connection type is in our encoded fastener schedule.

    Returns the required fastener specification if known, or a FAIL with an advisory
    to consult the full IBC Table 2304.10.1.

    Parameters
    ----------
    connection_type:
        Connection key (case-insensitive, spaces/hyphens normalised to underscores).
        Known types: ``"ledger_to_band_joist"``, ``"post_base"``, ``"rim_joist_to_sill"``,
        ``"rafter_to_top_plate"``, ``"shear_wall_anchor"``.

    Returns
    -------
    :class:`RuleResult` with ``rule_id="fastener_schedule"``.  ``passed=True`` means the
    connection type is encoded in our schedule; ``value=min_count``, ``limit=min_count``.
    """
    key = (connection_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    spec = _FASTENER_SCHEDULE.get(key)

    if spec is None:
        return RuleResult(
            rule_id="fastener_schedule",
            passed=False,
            value=0.0,
            limit=0.0,
            unit="count",
            citation="IBC 2304.10 / NDS 10 fastener schedules",
            detail=(
                f"Connection type {connection_type!r} is not in the encoded fastener schedule. "
                "Consult IBC Table 2304.10.1 and a licensed structural engineer."
            ),
        )

    return RuleResult(
        rule_id="fastener_schedule",
        passed=True,
        value=float(spec["min_count"]),
        limit=float(spec["min_count"]),
        unit="count",
        citation=f"IBC 2304.10 / NDS 10 — {spec['note']}",
        detail=(
            f"Required: {spec['fastener']} × {spec['min_count']} minimum "
            f"(min penetration {spec['min_length_mm']:.1f} mm). "
            f"Schedule encoded — PASS (verify quantities on site)."
        ),
    )


# --------------------------------------------------------------------------- RAG layer


@runtime_checkable
class RAGProvider(Protocol):
    """Protocol for citation retrieval backends.

    Implementations must be fence-safe: no copyrighted text is scraped or cached.
    """

    def retrieve(self, query: str, jurisdiction: str, max_results: int = 5) -> list[dict]:
        """Return a list of citation dicts: ``{text, source_url, section, confidence}``."""
        ...


class UserCorpusProvider:
    """Default RAG backend: searches user-supplied PDFs in a local corpus directory.

    Set ``CHUDWARE_CODE_CORPUS_PATH`` to a directory containing user-licensed PDF copies of
    building codes. If the directory is absent or empty, returns a no-corpus
    advisory citation with ``confidence="none"`` (never a provider call).

    Text extraction uses ``pdfminer.six`` (MIT) if available; degrades gracefully
    when absent.

    **No copyrighted text is bundled or fetched** — the user provides their own licensed
    documents. This backend retrieves local text snippets without an LLM/provider call.
    """

    _DEFAULT_CORPUS_DIR = os.path.join(
        os.path.expanduser("~"),
        ".local", "share", "Chudware", "code_corpus",
    )

    def __init__(self, corpus_dir: Optional[str] = None):
        self.corpus_dir = corpus_dir or os.environ.get(
            "CHUDWARE_CODE_CORPUS_PATH", self._DEFAULT_CORPUS_DIR
        )

    def _list_pdfs(self) -> list[str]:
        if not os.path.isdir(self.corpus_dir):
            return []
        return [
            os.path.join(self.corpus_dir, fn)
            for fn in os.listdir(self.corpus_dir)
            if fn.lower().endswith(".pdf")
        ]

    def _extract_text(self, pdf_path: str, max_chars: int = 50_000) -> str:
        """Extract text from a PDF using pdfminer.six (MIT) if available."""
        try:
            from pdfminer.high_level import extract_text as _pdfminer_extract
            text = _pdfminer_extract(pdf_path)
            return (text or "")[:max_chars]
        except ImportError:
            return ""
        except Exception:  # noqa: BLE001
            return ""

    def _keyword_score(self, text: str, query: str) -> float:
        """Simple BM25-style keyword overlap score (stdlib only)."""
        import re
        query_words = set(re.findall(r"\w+", query.lower()))
        if not query_words:
            return 0.0
        text_lower = text.lower()
        hits = sum(1 for w in query_words if w in text_lower)
        return hits / len(query_words)

    def retrieve(self, query: str, jurisdiction: str, max_results: int = 5) -> list[dict]:
        """Search user corpus for relevant passages; return citation list."""
        pdfs = self._list_pdfs()
        if not pdfs:
            return [{
                "text": (
                    "No user corpus found. Add licensed building-code PDFs to "
                    f"{self.corpus_dir} (or set CHUDWARE_CODE_CORPUS_PATH) to enable "
                    "jurisdiction-specific citations."
                ),
                "source_url": f"file://{self.corpus_dir}",
                "section": "corpus_advisory",
                "confidence": "none",
            }]

        scored: list[tuple[float, str, str]] = []  # (score, path, snippet)
        for pdf_path in pdfs:
            text = self._extract_text(pdf_path)
            if not text:
                continue
            score = self._keyword_score(text, query + " " + jurisdiction)
            if score > 0:
                # Extract a short snippet around the first keyword hit
                import re
                words = re.findall(r"\w+", query.lower())
                snippet = ""
                for w in words:
                    idx = text.lower().find(w)
                    if idx >= 0:
                        start = max(0, idx - 120)
                        end = min(len(text), idx + 300)
                        snippet = text[start:end].strip()
                        break
                scored.append((score, pdf_path, snippet[:400]))

        scored.sort(key=lambda x: x[0], reverse=True)
        citations = []
        for score, pdf_path, snippet in scored[:max_results]:
            citations.append({
                "text": snippet or "(passage extracted — see source document)",
                "source_url": f"file://{pdf_path}",
                "section": "user_corpus",
                "confidence": "user_supplied",
            })

        return citations or [{
            "text": (
                f"Query '{query}' found no matches in user corpus for {jurisdiction}. "
                "Add more licensed PDFs to improve coverage."
            ),
            "source_url": f"file://{self.corpus_dir}",
            "section": "corpus_advisory",
            "confidence": "none",
        }]


class MockRAGProvider:
    """Deterministic mock for tests — no network, no file I/O."""

    def __init__(self, citations: Optional[list[dict]] = None):
        self._citations = citations or [
            {
                "text": "Mock citation: L/360 live-load deflection limit.",
                "source_url": "mock://test",
                "section": "IBC 1604.3",
                "confidence": "mock",
            }
        ]

    def retrieve(self, query: str, jurisdiction: str, max_results: int = 5) -> list[dict]:
        return list(self._citations)[:max_results]


def agentic_rag(
    query: str,
    jurisdiction: str,
    provider: Optional[RAGProvider] = None,
    max_results: int = 5,
) -> list[dict]:
    """Retrieve jurisdiction-specific compliance citations using the configured RAG backend.

    Parameters
    ----------
    query:
        Natural-language query describing the compliance topic (e.g. "deflection limit
        for floor joists under live load").
    jurisdiction:
        Two-letter US state code (e.g. ``"CA"``).  Used to filter/scope retrieval.
    provider:
        Optional :class:`RAGProvider` instance.  Defaults to :class:`UserCorpusProvider`.
        Pass a :class:`MockRAGProvider` in tests.
    max_results:
        Maximum number of citations to return.

    Returns
    -------
    List of citation dicts: ``{text, source_url, section, confidence}``.
    Every citation is marked ``"agent_retrieved": true`` and carries a retrieval caveat.

    Copyright fence
    ---------------
    The default :class:`UserCorpusProvider` only reads user-supplied PDFs.
    It does not scrape public building-code viewers; retrieved snippets retain
    their local source citations and the user-supplied confidence classification.
    """
    if provider is None:
        provider = UserCorpusProvider()

    raw = provider.retrieve(query, jurisdiction, max_results=max_results)
    # Tag every citation with agent_retrieved + caveat
    tagged = []
    for c in raw:
        c2 = dict(c)
        c2["agent_retrieved"] = True
        c2.setdefault(
            "caveat",
            "Agent-retrieved citation — accuracy not guaranteed; verify with the current "
            "edition of the applicable code and a licensed structural engineer.",
        )
        tagged.append(c2)
    return tagged


# --------------------------------------------------------------------------- orchestrator


def run_checks(
    span_mm: Optional[float] = None,
    deflection_mm: Optional[float] = None,
    post_size: Optional[str] = None,
    post_load_kn: Optional[float] = None,
    connection_type: Optional[str] = None,
    jurisdiction: str = "CA",
    rag_query: Optional[str] = None,
    rag_provider: Optional[RAGProvider] = None,
) -> dict:
    """Run all applicable deterministic rules + agentic RAG.

    Calls whichever deterministic checks have sufficient inputs; skips rules whose
    required parameters are ``None``.  Appends RAG citations when ``rag_query`` is given.

    Returns
    -------
    ``{rule_results: [...], citations: [...], disclaimer: str}``
    """
    rule_results: list[dict] = []

    if span_mm is not None and deflection_mm is not None:
        rule_results.append(check_deflection(span_mm, deflection_mm).to_dict())

    if post_size is not None and post_load_kn is not None:
        rule_results.append(check_post_span(post_size, post_load_kn).to_dict())

    if connection_type is not None:
        rule_results.append(check_fastener_schedule(connection_type).to_dict())

    citations: list[dict] = []
    if rag_query:
        citations = agentic_rag(rag_query, jurisdiction, provider=rag_provider)

    return {
        "rule_results": rule_results,
        "citations": citations,
        "disclaimer": DISCLAIMER,
    }
