"""RE-29 — Web / part-number research ingest orchestrator.

Given one of:
  - a free-text *query*  (e.g. "SKF 6204-2RS bearing")
  - a manufacturer part number *part_number*  (e.g. "6204-2RS")
  - a vendor / listing *url*  (e.g. "https://www.skf.com/…")

this module PULLs datasheet PDF + spec data + images from the internet and
assembles them into a session ``datasheet/`` directory ready for the existing
``reveng_datasheet.json`` mission (n_ingest → n_mpn_fastpath → …).

Reuses existing seams — no reinvention:
  - ``engine_oracle.research.web_search`` / ``fetch_page`` / ``datasheet_fetch``
  - ``engine_oracle.spec_sheet_matcher.normalize_mpn``
  - ``engine_oracle.vision.datasheet_ingest.ingest_datasheet``
  - ``services.browser`` adapters for authenticated vendor pages (best-effort)

Every pulled dimension carries ``provenance`` (``"datasheet"`` / ``"web"`` /
``"vendor"``).  Offline / tools-absent → honest degradation envelope, never a
traceback.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Seam imports — all optional; graceful degradation if absent
#
# ORDERING-SAFE PATTERN
# =====================
# engine_oracle.research is a *package* whose members (web_search, fetch_page,
# datasheet_fetch) are also importable as sub-modules.  Python caches them at
# sys.modules["engine_oracle.research.X"] the first time any code touches them.
#
# A plain ``from engine_oracle.research import X as _X`` therefore binds _X to
# the *sub-module object* already in sys.modules, not to the attribute on
# whatever the test installs at sys.modules["engine_oracle.research"].  This
# makes the monkeypatch invisible after the first real import regardless of
# module-deletion tricks.
#
# Additionally, Python's package machinery sets the submodule as an attribute on
# the parent package object, so even ``from engine_oracle.vision import
# research_ingest`` can return a stale module object if engine_oracle.vision is
# still cached.
#
# Solution: resolve the three callables *lazily at call time* via sys.modules,
# using stable module-level sentinel names (_web_search, _fetch_page,
# _datasheet_fetch) that tests can override with monkeypatch.setattr(ri, ...).
# The sentinels are set to None here; _resolve_research_fn() does the actual
# lookup at first call, going through sys.modules["engine_oracle.research"] so a
# test replacement is always honoured.
# ---------------------------------------------------------------------------

import sys as _sys

# _UNSET sentinel: distinguishes "not yet resolved" from "explicitly None / offline".
# At module import these are _UNSET.  _get_research_fn() resolves through
# sys.modules["engine_oracle.research"] at call time (so test fakes always win).
# Tests that want to simulate offline mode set the attribute to None explicitly
# (monkeypatch.setattr(ri, "_web_search", None)), which _get_research_fn honours
# by returning None without attempting a sys.modules lookup.
_UNSET = object()

_web_search    = _UNSET  # type: ignore[assignment]
_fetch_page    = _UNSET  # type: ignore[assignment]
_datasheet_fetch = _UNSET  # type: ignore[assignment]


def _get_research_fn(name: str):
    """Return the named callable from the research seam, or None if unavailable.

    Lookup order:
    1. Module-level attribute ``_<name>``:
       - If callable  → return it (monkeypatch.setattr or a prior resolved value).
       - If None      → return None (test/caller explicitly disabled this seam).
       - If _UNSET    → fall through to sys.modules resolution below.
    2. ``sys.modules["engine_oracle.research"]`` (respects test fakes inserted via
       monkeypatch.setitem, which replace the whole package entry and therefore
       shadow any cached sub-module at ``engine_oracle.research.<name>``).
    3. Fresh importlib import (production first-call path).
    """
    val = globals().get(f"_{name}", _UNSET)
    if val is not _UNSET:
        # Explicitly set (callable mock OR None/offline). Honour it directly.
        return val if callable(val) else None
    # _UNSET: resolve through sys.modules so test fakes shadow real sub-modules.
    try:
        pkg = _sys.modules.get("engine_oracle.research")
        if pkg is None:
            import importlib as _il
            pkg = _il.import_module("engine_oracle.research")
        fn = getattr(pkg, name, None)
        if callable(fn):
            return fn
    except Exception:
        pass
    return None

try:
    from ..spec_sheet_matcher import normalize_mpn
except Exception:
    def normalize_mpn(pn: str) -> str:  # type: ignore[misc]
        return str(pn or "").strip()

try:
    from ..vision.datasheet_ingest import ingest_datasheet as _ingest_datasheet
except Exception:
    _ingest_datasheet = None  # type: ignore[assignment]

# Browser acquisition — best-effort; not required
try:
    from services.browser.session import BrowserSession as _BrowserSession
    _HAS_BROWSER = True
except Exception:
    _HAS_BROWSER = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PDF_EXT = re.compile(r"\.pdf$", re.IGNORECASE)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _is_url(s: str) -> bool:
    return bool(_URL_RE.match(s))


def _looks_like_pdf_url(url: str) -> bool:
    path = urlparse(url).path
    return bool(_PDF_EXT.search(path))


def _dim_mm(raw: Any, unit_hint: str = "mm") -> Optional[float]:
    """Parse a raw dimension value to float mm.  Returns None on failure."""
    try:
        v = float(str(raw).split()[0].replace(",", "."))
        # Crude unit normalisation
        if unit_hint in ("in", "inch", "inches"):
            v *= 25.4
        elif unit_hint in ("cm",):
            v *= 10.0
        elif unit_hint in ("m",):
            v *= 1000.0
        return round(v, 4)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core orchestrator
# ---------------------------------------------------------------------------

def research_and_ingest(
    session_root: Path,
    *,
    query: Optional[str] = None,
    part_number: Optional[str] = None,
    url: Optional[str] = None,
    max_web_results: int = 5,
) -> dict:
    """Pull web / datasheet data into *session_root*/datasheet/ and return a bundle.

    Returns
    -------
    dict
        ``{ok, session_id, datasheet_dir, candidate_mpns, candidate_dims,
           sources, mission_seed}`` on success.

        ``{ok: False, reason, hint}`` on failure.

    The ``mission_seed`` field contains the arguments that should be forwarded
    to the ``reveng_datasheet.json`` mission's ``n_ingest`` node (or handed off
    via the ``reveng_research_source`` MCP tool).
    """
    # Validate at least one input
    if not any([query, part_number, url]):
        return {
            "ok": False,
            "reason": "no_input",
            "hint": "Provide at least one of: query, part_number, url",
        }

    datasheet_dir = session_root / "datasheet"
    datasheet_dir.mkdir(parents=True, exist_ok=True)

    sources: list[dict] = []
    candidate_mpns: list[str] = []
    candidate_dims: list[dict] = []
    pdf_paths: list[Path] = []
    fetched_text: list[str] = []

    # ── 1. Normalise part number ───────────────────────────────────────────
    if part_number:
        normalised_mpn = normalize_mpn(part_number)
        if normalised_mpn and normalised_mpn not in candidate_mpns:
            candidate_mpns.append(normalised_mpn)
    else:
        normalised_mpn = None

    # ── 2. Resolve URL input ───────────────────────────────────────────────
    if url:
        src_result = _fetch_url(url, datasheet_dir)
        if src_result.get("ok"):
            sources.append(src_result["source"])
            if src_result.get("text"):
                fetched_text.append(src_result["text"])
            if src_result.get("pdf_path"):
                pdf_paths.append(Path(src_result["pdf_path"]))
            # Extract MPNs from page text
            for mpn in _extract_mpns_from_text(src_result.get("text", "")):
                if mpn not in candidate_mpns:
                    candidate_mpns.append(mpn)

    # ── 3. Web search for query / part number ─────────────────────────────
    search_query = query or (f"{normalised_mpn} datasheet specifications" if normalised_mpn else None)
    if search_query:
        search_result = _run_web_search(search_query, max_web_results, datasheet_dir)
        sources.extend(search_result.get("sources", []))
        fetched_text.extend(search_result.get("texts", []))
        pdf_paths.extend(Path(p) for p in search_result.get("pdf_paths", []))
        for mpn in search_result.get("mpns", []):
            if mpn not in candidate_mpns:
                candidate_mpns.append(mpn)

    # ── 4. Parse PDFs via datasheet_ingest (RE-15) ────────────────────────
    for pdf_path in pdf_paths:
        if not pdf_path.is_file():
            continue
        dims = _parse_pdf(pdf_path, datasheet_dir)
        for d in dims:
            d["provenance"] = "datasheet"
            candidate_dims.append(d)

    # ── 5. Extract dims from fetched web text (lower confidence) ──────────
    for text in fetched_text:
        for d in _extract_dims_from_text(text):
            d["provenance"] = "web"
            candidate_dims.append(d)

    # ── 6. Write bundle manifest ───────────────────────────────────────────
    if not sources and not candidate_dims:
        return {
            "ok": False,
            "reason": "no_data_found",
            "hint": (
                "No datasheet or web content could be retrieved. "
                "Check network connectivity or provide a direct PDF URL."
            ),
        }

    bundle: dict = {
        "schema": "aicad.reveng_research_bundle/1",
        "created_at": _now_iso(),
        "query": query,
        "part_number": normalised_mpn or part_number,
        "url": url,
        "candidate_mpns": candidate_mpns,
        "candidate_dims": candidate_dims,
        "sources": sources,
    }
    bundle_path = datasheet_dir / "research_bundle.json"
    bundle_path.write_text(json.dumps(bundle, indent=2))

    # mission_seed: what to hand off to reveng_datasheet n_ingest
    mission_seed = {
        "datasheet_dir": str(datasheet_dir),
        "part_number": candidate_mpns[0] if candidate_mpns else None,
        "bundle_path": str(bundle_path),
        "provenance_method": "web" if not pdf_paths else "datasheet",
    }

    return {
        "ok": True,
        "datasheet_dir": str(datasheet_dir),
        "candidate_mpns": candidate_mpns,
        "candidate_dims": candidate_dims,
        "sources": sources,
        "mission_seed": mission_seed,
    }


# ---------------------------------------------------------------------------
# Sub-helpers
# ---------------------------------------------------------------------------

def _fetch_url(url: str, datasheet_dir: Path) -> dict:
    """Fetch a single URL; download PDF if it looks like one."""
    source_record: dict = {
        "url": url,
        "kind": "url_direct",
        "ts": _now_iso(),
    }
    text = ""
    pdf_path: Optional[str] = None

    _df = _get_research_fn("datasheet_fetch")
    _fp = _get_research_fn("fetch_page")
    if _looks_like_pdf_url(url) and _df is not None:
        # Try to fetch as a PDF datasheet
        try:
            result = _df(url, tier="datasheet")
            if isinstance(result, str):
                result = json.loads(result)
            if result.get("ok") or result.get("content"):
                text = result.get("content", "")
                source_record["kind"] = "pdf_url"
                source_record["confidence"] = result.get("confidence", "web")
                # If the backend saved a local PDF, note it
                if result.get("local_path"):
                    pdf_path = result["local_path"]
        except Exception:
            pass
    elif _fp is not None:
        try:
            result = _fp(url, tier="datasheet")
            if isinstance(result, str):
                result = json.loads(result)
            text = result.get("content", "")
            source_record["confidence"] = result.get("confidence", "web")
        except Exception:
            pass
    elif _HAS_BROWSER:
        # Fallback: authenticated browser session for vendor pages
        try:
            session = _BrowserSession()
            text = session.get_text(url) or ""
            source_record["kind"] = "browser_vendor"
            source_record["confidence"] = "vendor"
        except Exception:
            pass

    if not text and not pdf_path:
        return {"ok": False, "reason": "fetch_failed", "url": url}

    source_record["text_len"] = len(text)
    return {"ok": True, "source": source_record, "text": text, "pdf_path": pdf_path}


def _run_web_search(
    query: str,
    max_results: int,
    datasheet_dir: Path,
) -> dict:
    """Run a web search and fetch top results for datasheets."""
    out: dict = {"sources": [], "texts": [], "pdf_paths": [], "mpns": []}

    _ws = _get_research_fn("web_search")
    if _ws is None:
        return out

    try:
        results = _ws(query, max_results=max_results)
        if isinstance(results, str):
            results = json.loads(results)
    except Exception:
        return out

    items = results.get("results") or results.get("items") or []
    for item in items[:max_results]:
        item_url = item.get("url") or item.get("link") or ""
        if not item_url:
            continue
        src = _fetch_url(item_url, datasheet_dir)
        if src.get("ok"):
            out["sources"].append(src["source"])
            if src.get("text"):
                out["texts"].append(src["text"])
                for mpn in _extract_mpns_from_text(src["text"]):
                    if mpn not in out["mpns"]:
                        out["mpns"].append(mpn)
            if src.get("pdf_path"):
                out["pdf_paths"].append(src["pdf_path"])
    return out


def _parse_pdf(pdf_path: Path, datasheet_dir: Path) -> list[dict]:
    """Delegate to datasheet_ingest (RE-15) and extract candidate dims."""
    if _ingest_datasheet is None:
        return []
    try:
        result = _ingest_datasheet(str(pdf_path), str(datasheet_dir))
        if isinstance(result, str):
            result = json.loads(result)
        dims: list[dict] = []
        for row in result.get("dim_rows", []) or []:
            # Each row: {part_no, ...dim fields}
            for key, val in row.items():
                if key in ("part_no", "part_number", "mpn"):
                    mpn = normalize_mpn(str(val))
                    if mpn:
                        continue  # handled separately
                mm = _dim_mm(val)
                if mm is not None:
                    dims.append({"label": key, "mm": mm, "raw": val})
        return dims
    except Exception:
        return []


# Regex: simple dimensional values like "12.5 mm", "OD: 42 mm", "width=8.0"
_DIM_RE = re.compile(
    r"(?:(?P<label>[A-Za-z][A-Za-z0-9_\s]{0,20}?)"
    r"[\s:=]+)?"
    r"(?P<value>-?\d+\.?\d*)"
    r"\s*(?P<unit>mm|cm|in|inch|m)\b",
    re.IGNORECASE,
)

_MPN_PAT = re.compile(r"\b([A-Z]{1,4}[\-]?[0-9]{3,6}(?:[A-Z0-9\-]{0,8})?)\b")


def _extract_mpns_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for m in _MPN_PAT.finditer(text):
        mpn = normalize_mpn(m.group(1))
        if mpn and mpn not in seen:
            seen.add(mpn)
            result.append(mpn)
    return result


def _extract_dims_from_text(text: str) -> list[dict]:
    result: list[dict] = []
    for m in _DIM_RE.finditer(text):
        label = (m.group("label") or "").strip().rstrip(":=")
        mm = _dim_mm(m.group("value"), m.group("unit"))
        if mm is not None and 0.1 <= mm <= 5000:
            result.append({"label": label or "dim", "mm": mm, "raw": m.group(0).strip()})
    return result
