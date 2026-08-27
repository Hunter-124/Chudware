"""Cited machining reference lookup and project-local user-PDF indexing.

The bundled JSON contains only curated, paraphrased standards-derived facts. User-owned
PDF text is never returned wholesale or copied outside its per-project SQLite index.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

_DATA_PATH = Path(__file__).parent / "data" / "machining_reference.json"
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_DATA: list[dict[str, Any]] | None = None
_INDEX_PATHS: dict[str, Path] = {}

_INDEX_SCHEMA = """
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE TABLE pages (
    id INTEGER PRIMARY KEY,
    page_index INTEGER NOT NULL UNIQUE,
    pdf_page INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE pages_fts USING fts5(
    text,
    content='pages',
    content_rowid='id',
    tokenize='porter unicode61'
);
"""

_DELEGATES: tuple[dict[str, Any], ...] = (
    {
        "id": "delegate-fit-calc",
        "topic": "ISO limits and fits, clearance, transition, press and shrink fits",
        "delegate_tool": "fit_calc",
        "keywords": ("fit", "fits", "clearance", "interference", "press", "shrink", "h7", "g6", "m6", "p6", "iso286"),
        "reason": "ISO fit bands are computed by the canonical fit calculator rather than duplicated in the reference dataset.",
        "source": {"standard": "ISO 286-1:2010 / ISO 286-2:2010", "section": "Use fit_calc for the selected nominal diameter and fit class"},
    },
    {
        "id": "delegate-thread-spec",
        "topic": "basic metric and Unified thread geometry",
        "delegate_tool": "research_thread_spec",
        "keywords": ("thread", "threads", "unc", "unf", "pitch", "major", "minor", "iso724", "b1.1"),
        "reason": "Basic 60-degree thread geometry is canonical in research_thread_spec.",
        "source": {"standard": "ISO 724 / ASME B1.1", "section": "Use research_thread_spec for a concrete designation"},
    },
    {
        "id": "delegate-material-lookup",
        "topic": "named material mechanical properties",
        "delegate_tool": "research_material_lookup",
        "keywords": ("material", "yield", "ultimate", "modulus", "density", "poisson", "alloy"),
        "reason": "Named material facts belong to the unified material registry.",
        "source": {"standard": "Chudware unified material registry", "section": "Use research_material_lookup for a named grade and property"},
    },
    {
        "id": "delegate-tap-drill",
        "topic": "tap drill and threaded-hole pilot sizes",
        "delegate_tool": "tap_drill",
        "keywords": ("tap", "tapdrill", "pilot", "percent", "thread"),
        "reason": "Tap-drill sizes are canonical in the feeds/speeds machining dataset.",
        "source": {"standard": "ISO 261 / ASME B1.1 series", "section": "Use tap_drill with the concrete thread designation"},
    },
    {
        "id": "delegate-feeds-speeds",
        "topic": "cutting speeds feeds chip load depth and width of cut",
        "delegate_tool": "feeds_speeds",
        "keywords": ("feed", "feeds", "speed", "speeds", "rpm", "chipload", "sfm", "doc", "woc"),
        "reason": "Cutting envelopes are canonical in feeds_speeds and include material, tool, operation, and machine clamps.",
        "source": {"standard": "Chudware cited cutting-data registry", "section": "Use feeds_speeds with material, tool, and operation"},
    },
)


def _records() -> list[dict[str, Any]]:
    global _DATA
    if _DATA is None:
        payload = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        _DATA = payload["records"]
    return _DATA


def _tokens(value: Any) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(str(value or ""))]


def _bounded_limit(value: Any, default: int, maximum: int) -> int:
    try:
        return max(1, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _delegate_hits(query_tokens: set[str]) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    for delegate in _DELEGATES:
        matched = query_tokens.intersection(delegate["keywords"])
        tool = delegate["delegate_tool"]
        if tool == "research_thread_spec":
            designation = any(re.fullmatch(r"m\d+(?:x\d+)?", token) for token in query_tokens)
            specific = query_tokens.intersection(
                {"geometry", "designation", "unc", "unf", "pitch", "major", "minor", "iso724", "b1", "1"}
            )
            if not designation and not specific:
                continue
        elif tool == "research_material_lookup":
            property_terms = query_tokens.intersection(
                {"yield", "ultimate", "modulus", "density", "poisson"}
            )
            named_grade = any(any(char.isdigit() for char in token) for token in query_tokens)
            named_grade = named_grade or bool(query_tokens.intersection(
                {"steel", "aluminum", "aluminium", "titanium", "inconel", "brass", "bronze", "alloy"}
            ))
            if not property_terms and not named_grade:
                continue
        elif tool == "tap_drill":
            explicit = query_tokens.intersection({"tap", "tapdrill", "percent"})
            if not explicit and not {"thread", "pilot"}.issubset(query_tokens):
                continue
        if not matched:
            continue
        row = {key: deepcopy(value) for key, value in delegate.items() if key != "keywords"}
        row["score"] = 100 + len(matched)
        hits.append(row)
    return hits


def _matches_filter(row: dict[str, Any], field: str, wanted: str | None) -> bool:
    if wanted is None or not str(wanted).strip():
        return True
    actual = row.get(field)
    if actual is None:
        return False
    wanted_tokens = set(_tokens(wanted))
    return wanted_tokens.issubset(set(_tokens(actual)))


def machining_ref_search(
    query: str,
    topic: str | None = None,
    material: str | None = None,
    process: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search cited rows with token/field scoring and canonical-tool delegation."""
    query_tokens = set(_tokens(query))
    if not query_tokens and not any((topic, material, process)):
        return {"ok": False, "reason": "bad_query", "hint": "Provide a query or at least one field filter."}
    cap = _bounded_limit(limit, 10, 25)
    scored: list[dict[str, Any]] = (
        [] if any((topic, material, process)) else _delegate_hits(query_tokens)
    )
    for row in _records():
        if not _matches_filter(row, "topic", topic):
            continue
        if not _matches_filter(row, "material", material):
            continue
        if not _matches_filter(row, "process", process):
            continue
        fields = {
            "id": set(_tokens(row.get("id"))),
            "topic": set(_tokens(row.get("topic"))),
            "process": set(_tokens(row.get("process"))),
            "material": set(_tokens(row.get("material"))),
            "conditions": set(_tokens(row.get("conditions"))),
            "notes": set(_tokens(row.get("notes"))),
            "body": set(_tokens(json.dumps(
                row.get("table") or row.get("value") or row.get("range") or ""
            ))),
        }
        score = (
            7 * len(query_tokens & fields["topic"])
            + 5 * len(query_tokens & fields["id"])
            + 4 * len(query_tokens & fields["process"])
            + 4 * len(query_tokens & fields["material"])
            + 2 * len(query_tokens & fields["conditions"])
            + len(query_tokens & fields["notes"])
            + len(query_tokens & fields["body"])
        )
        if not query_tokens:
            score = 1
        if score:
            hit = deepcopy(row)
            hit["score"] = score
            scored.append(hit)
    scored.sort(key=lambda row: (-float(row["score"]), str(row["id"])))
    results = scored[:cap]
    return {
        "ok": True,
        "query": query,
        "filters": {"topic": topic, "material": material, "process": process},
        "count": len(results),
        "results": results,
    }


def machining_ref_get(record_id: str) -> dict[str, Any]:
    """Return one exact curated record by stable id."""
    wanted = str(record_id or "").strip()
    if not wanted:
        return {"ok": False, "reason": "bad_query", "hint": "record_id is required."}
    for row in _records():
        if row.get("id") == wanted:
            return {"ok": True, "record": deepcopy(row)}
    return {"ok": False, "reason": "not_found", "hint": f"No machining reference record {wanted!r}."}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_pdf_pages(path: Path) -> dict[str, Any]:
    """Mirror office_read_pdf's pypdfium2 per-page extraction path."""
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415
    except (ImportError, ModuleNotFoundError):
        return {"ok": False, "reason": "engine_missing", "hint": "pypdfium2 is not installed"}
    try:
        extractor_version = version("pypdfium2")
    except PackageNotFoundError:
        extractor_version = "unknown"
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as exc:  # noqa: BLE001
        reason = "password_protected" if "password" in str(exc).lower() else "open_failed"
        return {"ok": False, "reason": reason, "hint": str(exc)}
    pages: list[dict[str, Any]] = []
    page_errors: list[dict[str, Any]] = []
    try:
        count = len(doc)
        for index in range(count):
            page = None
            textpage = None
            try:
                page = doc[index]
                textpage = page.get_textpage()
                text = textpage.get_text_range()
            except Exception as exc:  # noqa: BLE001
                text = ""
                page_errors.append({"pdf_page": index + 1, "reason": "extract_failed", "hint": str(exc)})
            finally:
                if textpage is not None and hasattr(textpage, "close"):
                    textpage.close()
                if page is not None and hasattr(page, "close"):
                    page.close()
            pages.append({"index": index, "text": text})
    finally:
        doc.close()
    return {
        "ok": True,
        "pages": pages,
        "page_count": len(pages),
        "page_errors": page_errors,
        "extractor_version": extractor_version,
    }


def _resolve_project_root(project_root: str | None) -> Path:
    if project_root and str(project_root).strip():
        return Path(project_root).expanduser().resolve()
    for key in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT"):
        value = os.environ.get(key, "").strip()
        if value:
            return Path(value).expanduser().resolve()
    try:
        from services.agent.project_context import resolve_project_root  # noqa: PLC0415

        return resolve_project_root(Path.cwd())
    except Exception:  # noqa: BLE001
        return Path.cwd().resolve()


def _write_index(
    db_path: Path,
    source_id: str,
    canonical_path: Path,
    sha256: str,
    pages: list[dict[str, Any]],
    extractor_version: str,
) -> dict[str, Any]:
    temp_path = db_path.with_name("index.sqlite.tmp")
    temp_path.unlink(missing_ok=True)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(temp_path)
        conn.executescript(_INDEX_SCHEMA)
        indexed_at = time.time()
        conn.execute(
            "INSERT INTO sources(source_id, canonical_path, sha256, page_count, extractor, extractor_version, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, str(canonical_path), sha256, len(pages), "pypdfium2",
             extractor_version, indexed_at),
        )
        conn.executemany(
            "INSERT INTO pages(page_index, pdf_page, text) VALUES (?, ?, ?)",
            [(int(page["index"]), int(page["index"]) + 1, str(page.get("text") or "").replace("\x00", "")) for page in pages],
        )
        conn.execute("INSERT INTO pages_fts(pages_fts) VALUES ('rebuild')")
        conn.commit()
        conn.close()
        conn = None
        temp_path.replace(db_path)
        return {"ok": True, "indexed_at": indexed_at}
    except sqlite3.OperationalError as exc:
        reason = "fts5_unavailable" if "fts5" in str(exc).lower() or "no such module" in str(exc).lower() else "index_failed"
        return {"ok": False, "reason": reason, "hint": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "index_failed", "hint": str(exc)}
    finally:
        if conn is not None:
            conn.close()
        temp_path.unlink(missing_ok=True)


def machining_ref_index_pdf(path: str, project_root: str | None = None) -> dict[str, Any]:
    """Index user-owned PDF pages into ``.plm/references/<sha256>/index.sqlite``."""
    root = _resolve_project_root(project_root)
    pdf_path = Path(path).expanduser()
    if not pdf_path.is_absolute():
        pdf_path = root / pdf_path
    pdf_path = pdf_path.resolve()
    if not pdf_path.is_file():
        return {"ok": False, "reason": "not_found", "hint": f"PDF does not exist: {pdf_path}"}
    if pdf_path.suffix.lower() != ".pdf":
        return {"ok": False, "reason": "unsupported_format", "hint": "machining_ref_index_pdf accepts PDF files only."}
    try:
        digest = _sha256(pdf_path)
    except OSError as exc:
        return {"ok": False, "reason": "open_failed", "hint": str(exc)}
    extracted = _extract_pdf_pages(pdf_path)
    if not extracted.get("ok"):
        return extracted
    source_id = f"pdf-{digest[:16]}"
    index_dir = root / ".plm" / "references" / digest
    try:
        index_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "reason": "index_failed", "hint": str(exc)}
    resolved_index_dir = index_dir.resolve()
    if not resolved_index_dir.is_relative_to(root):
        return {
            "ok": False,
            "reason": "unsafe_index_path",
            "hint": f"Project reference directory resolves outside project root: {resolved_index_dir}",
        }
    index_dir = resolved_index_dir
    db_path = index_dir / "index.sqlite"
    written = _write_index(
        db_path, source_id, pdf_path, digest, extracted["pages"],
        str(extracted.get("extractor_version") or "unknown"),
    )
    if not written.get("ok"):
        return written
    _INDEX_PATHS[source_id] = db_path
    no_text_pages = [int(page["index"]) + 1 for page in extracted["pages"] if not str(page.get("text") or "").strip()]
    return {
        "ok": True,
        "source_id": source_id,
        "sha256": digest,
        "canonical_path": str(pdf_path),
        "index_path": str(db_path),
        "page_count": extracted["page_count"],
        "indexed_pages": extracted["page_count"] - len(no_text_pages),
        "scanned_pages": no_text_pages,
        "no_text_pages": no_text_pages,
        "ocr_used": False,
        "extractor": {
            "name": "pypdfium2",
            "version": str(extracted.get("extractor_version") or "unknown"),
        },
        "indexed_at": written["indexed_at"],
        "page_errors": extracted.get("page_errors", []),
    }


def _fts_query(raw: str) -> str:
    terms = _tokens(raw)[:12]
    return " OR ".join(f'"{term}"' for term in terms)


def _candidate_indexes(source_id: str | None) -> list[Path]:
    candidates: set[Path] = set(_INDEX_PATHS.values())
    roots = {_resolve_project_root(None)}
    for key in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT"):
        value = os.environ.get(key, "").strip()
        if value:
            roots.add(Path(value).expanduser().resolve())
    for root in roots:
        refs = root / ".plm" / "references"
        if refs.is_dir():
            candidates.update(refs.glob("*/index.sqlite"))
    if source_id and source_id in _INDEX_PATHS:
        return [_INDEX_PATHS[source_id]]
    return sorted(candidates)


def machining_ref_search_pdf(query: str, source_id: str | None = None, limit: int = 8) -> dict[str, Any]:
    """Search project-local page indexes and return bounded page-cited snippets."""
    fts_query = _fts_query(query)
    if not fts_query:
        return {"ok": False, "reason": "bad_query", "hint": "Provide searchable words."}
    cap = _bounded_limit(limit, 8, 20)
    candidates = _candidate_indexes(source_id)
    if not candidates:
        return {"ok": False, "reason": "not_indexed", "hint": "Index a PDF with machining_ref_index_pdf first."}
    results: list[dict[str, Any]] = []
    opened = 0
    errors: list[dict[str, str]] = []
    for db_path in candidates:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            source = conn.execute("SELECT source_id, sha256, canonical_path FROM sources LIMIT 1").fetchone()
            if source is None or (source_id and source["source_id"] != source_id):
                continue
            opened += 1
            rows = conn.execute(
                "SELECT p.page_index, p.pdf_page, snippet(pages_fts, 0, '[', ']', ' … ', 24) AS snippet, bm25(pages_fts) AS score "
                "FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
                "WHERE pages_fts MATCH ? ORDER BY score LIMIT ?",
                (fts_query, cap),
            ).fetchall()
            for row in rows:
                results.append({
                    "snippet": str(row["snippet"])[:800],
                    "score": float(row["score"]),
                    "source_path": source["canonical_path"],
                    "citation": {
                        "source_id": source["source_id"],
                        "sha256": source["sha256"],
                        "pdf_page": int(row["pdf_page"]),
                        "page_index": int(row["page_index"]),
                    },
                })
        except sqlite3.OperationalError as exc:
            reason = "fts5_unavailable" if "fts5" in str(exc).lower() or "no such module" in str(exc).lower() else "search_failed"
            errors.append({"reason": reason, "hint": str(exc)})
        except Exception as exc:  # noqa: BLE001
            errors.append({"reason": "search_failed", "hint": str(exc)})
        finally:
            if conn is not None:
                conn.close()
    if opened == 0:
        if errors:
            return {"ok": False, **errors[0]}
        return {"ok": False, "reason": "not_indexed", "hint": f"No indexed PDF matches source_id {source_id!r}."}
    results.sort(key=lambda row: (row["score"], row["citation"]["source_id"], row["citation"]["pdf_page"]))
    return {"ok": True, "query": query, "count": min(len(results), cap), "results": results[:cap], "errors": errors}
