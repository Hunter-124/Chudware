"""Datasheet ingest — parse a PDF datasheet into ``datasheet.json``.

Per ``contracts/reveng.md`` §1, each session has a ``datasheet/`` subdirectory.
This module writes:

  <session_root>/datasheet/datasheet.json   — structured extraction result
  <session_root>/datasheet/crops/           — PNG crops of detected drawing regions

Backend priority (lazy import, first available wins):

1. **docling** (IBM, MIT) — highest fidelity; table + figure extraction.
2. **pdfplumber** — good table extraction; no figure crops.
3. If neither is available: returns the honest degradation envelope
   ``{"ok": false, "reason": "backend_not_provisioned", "hint": "pip install docling"}``.

Part-number candidates are normalised with :func:`engine_oracle.spec_sheet_matcher.normalize_mpn`
(already on master) — no reinvention.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# MPN normalisation — reuse existing, no reinvention
# ---------------------------------------------------------------------------
try:
    from engine_oracle.spec_sheet_matcher import normalize_mpn
except Exception:  # pragma: no cover
    # Fallback shim when running standalone outside the package tree
    def normalize_mpn(pn: str) -> str:  # type: ignore[misc]
        return str(pn or "").strip()


# ---------------------------------------------------------------------------
# Regexes for MPN candidate extraction from plain text
# ---------------------------------------------------------------------------

# Typical bearing PN patterns: 6200, 6204-2RS, 6304ZZ, 32206, 51100, …
# Also common other part patterns: L-1680, AS-12345, SKF-6200, NSK6204, …
_MPN_PATTERNS: list[re.Pattern[str]] = [
    # ISO bearing number (6-digit or 4/5/6 digit with optional suffix)
    re.compile(r"\b([1-9][0-9]{3,5}(?:-?[A-Z0-9]{1,6})?)\b"),
    # Hyphenated part numbers: ABC-12345, L-1680
    re.compile(r"\b([A-Z]{1,4}-\d{3,6}(?:[A-Z0-9-]{0,8})?)\b"),
    # Prefixed bearing: SKF6200, NSK6204
    re.compile(r"\b([A-Z]{2,4}\d{4,6}(?:[A-Z0-9]{0,4})?)\b"),
]

# Rows with these header keywords are likely dimension tables
_DIM_KEYWORDS = frozenset({
    "od", "id", "width", "bore", "outer", "inner", "diameter",
    "d", "d1", "d2", "b", "h", "l", "length", "height",
    "thickness", "wall", "radius", "r", "mass", "weight",
})


def _extract_mpn_candidates(text: str) -> list[str]:
    """Return deduplicated, normalized MPN candidates from free text."""
    raw: list[str] = []
    for pat in _MPN_PATTERNS:
        raw.extend(m.group(1) for m in pat.finditer(text))
    seen: set[str] = set()
    result: list[str] = []
    for r in raw:
        n = normalize_mpn(r)
        if n and n not in seen:
            seen.add(n)
            result.append(n)
    return result


def _is_data_row(tokens: list[str]) -> bool:
    """True when the row contains at least one numeric value (table data row)."""
    return any(re.match(r"^-?\d+\.?\d*$", t) for t in tokens)


def _parse_text_tables(text: str) -> list[dict[str, Any]]:
    """Heuristic: find table blocks in plain text.

    Strategy: find consecutive lines that contain numeric tokens.  The line
    immediately before the first numeric line (if it has 3+ tokens) is treated
    as the header row.  This handles PDFs where the header "Part No. OD (mm)…"
    splits into more tokens than the data rows.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    tables: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        if not _is_data_row(lines[i].split()):
            i += 1
            continue
        # Found a data row — collect the run of data rows
        data_lines: list[list[str]] = []
        j = i
        while j < len(lines) and _is_data_row(lines[j].split()):
            data_lines.append(lines[j].split())
            j += 1
        if len(data_lines) < 1:
            i = j
            continue
        # Header: the line immediately before (if it has >=2 tokens)
        if i > 0 and len(lines[i - 1].split()) >= 2:
            header_tokens = lines[i - 1].split()
        else:
            header_tokens = [f"col{k}" for k in range(len(data_lines[0]))]
        tables.append({
            "headers": header_tokens,
            "rows": data_lines,
            "is_dimension_table": _is_dimension_table(header_tokens),
            "provenance": "pdfplumber_text",
        })
        i = j
    return tables


def _is_dimension_table(headers: list[str]) -> bool:
    """Heuristic: does this table look like a dimension table?"""
    norm = {h.strip().lower().rstrip(".") for h in headers}
    return bool(norm & _DIM_KEYWORDS)


# ---------------------------------------------------------------------------
# Backend: docling
# ---------------------------------------------------------------------------

def _ingest_docling(pdf_path: Path, out_dir: Path) -> dict[str, Any]:
    """Extract tables and drawing crops using docling (IBM, MIT-licensed)."""
    from docling.document_converter import DocumentConverter  # type: ignore[import]

    converter = DocumentConverter()
    result = converter.convert(str(pdf_path))
    doc = result.document

    tables: list[dict[str, Any]] = []
    crops: list[str] = []
    full_text = doc.export_to_text()

    for ti, table in enumerate(doc.tables):
        df = table.export_to_dataframe()
        headers = list(df.columns)
        rows = df.values.tolist()
        is_dim = _is_dimension_table(headers)
        tables.append({
            "table_index": ti,
            "is_dimension_table": is_dim,
            "headers": headers,
            "rows": [[str(c) for c in r] for r in rows],
            "provenance": "docling",
        })

    # Export page images and crop figures (docling may expose pictures)
    crops_dir = out_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    for fi, fig in enumerate(getattr(doc, "pictures", [])):
        try:
            img = fig.get_image(result)
            if img is not None:
                crop_path = crops_dir / f"fig_{fi:03d}.png"
                img.save(str(crop_path))
                crops.append(str(crop_path))
        except Exception:
            pass

    mpn_candidates = _extract_mpn_candidates(full_text)
    return {
        "ok": True,
        "backend": "docling",
        "tables": tables,
        "crops": crops,
        "mpn_candidates": mpn_candidates,
    }


# ---------------------------------------------------------------------------
# Backend: pdfplumber
# ---------------------------------------------------------------------------

def _ingest_pdfplumber(pdf_path: Path, out_dir: Path) -> dict[str, Any]:
    """Extract tables using pdfplumber. No figure crops (text/table only)."""
    import pdfplumber  # type: ignore[import]

    tables: list[dict[str, Any]] = []
    full_text_parts: list[str] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for pi, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            full_text_parts.append(page_text)

            raw_tables = page.extract_tables() or []
            for ti, table in enumerate(raw_tables):
                if not table:
                    continue
                headers = [str(c or "") for c in (table[0] or [])]
                rows = [[str(c or "") for c in row] for row in table[1:]]
                is_dim = _is_dimension_table(headers)
                tables.append({
                    "table_index": len(tables),
                    "page": pi + 1,
                    "is_dimension_table": is_dim,
                    "headers": headers,
                    "rows": rows,
                    "provenance": "pdfplumber",
                })

    full_text = "\n".join(full_text_parts)

    # If no tables found via layout heuristics, fall back to text-line parsing
    if not tables:
        for ti, t in enumerate(_parse_text_tables(full_text)):
            t["table_index"] = ti
            tables.append(t)

    mpn_candidates = _extract_mpn_candidates(full_text)
    crops_dir = out_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    return {
        "ok": True,
        "backend": "pdfplumber",
        "tables": tables,
        "crops": [],  # pdfplumber does not export figure crops
        "mpn_candidates": mpn_candidates,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ingest_datasheet(
    pdf_path: str | os.PathLike[str],
    session_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Parse *pdf_path* and write ``datasheet/datasheet.json`` under *session_root*.

    Returns the ``datasheet.json`` dict (``ok: true``) or a degradation
    envelope (``ok: false, reason, hint``) — never raises.

    Dimension tables are identified heuristically and tagged
    ``is_dimension_table: true``.  Drawing regions (figures) are cropped to
    ``datasheet/crops/fig_NNN.png`` when docling is the backend; pdfplumber
    yields no crops.

    MPN candidates are normalised via
    :func:`engine_oracle.spec_sheet_matcher.normalize_mpn` before being
    returned in ``mpn_candidates``.
    """
    pdf_path = Path(pdf_path)
    session_root = Path(session_root)
    out_dir = session_root / "datasheet"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not pdf_path.exists():
        result: dict[str, Any] = {
            "ok": False,
            "reason": "pdf_not_found",
            "hint": f"File not found: {pdf_path}",
        }
        (out_dir / "datasheet.json").write_text(json.dumps(result, indent=2))
        return result

    # Try backends in priority order
    for backend_name, backend_fn in [
        ("docling", _ingest_docling),
        ("pdfplumber", _ingest_pdfplumber),
    ]:
        mod_name = backend_name
        try:
            __import__(mod_name)
        except ImportError:
            continue

        try:
            data = backend_fn(pdf_path, out_dir)
        except Exception as exc:  # pragma: no cover
            data = {
                "ok": False,
                "reason": f"{backend_name}_error",
                "hint": str(exc),
            }

        data["pdf_path"] = str(pdf_path)
        data["session_root"] = str(session_root)
        (out_dir / "datasheet.json").write_text(json.dumps(data, indent=2))
        return data

    # No backend available
    envelope: dict[str, Any] = {
        "ok": False,
        "reason": "backend_not_provisioned",
        "hint": "pip install docling",
        "pdf_path": str(pdf_path),
        "session_root": str(session_root),
    }
    (out_dir / "datasheet.json").write_text(json.dumps(envelope, indent=2))
    return envelope
