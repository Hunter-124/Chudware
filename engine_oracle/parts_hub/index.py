"""SQLite FTS5 search index for the parts library — aicad.parts/1 (T-P1).

Uses only stdlib sqlite3 (FTS5 module). The index is an in-memory database rebuilt
from provider enumerations; it is also the lookup store for get() calls.

Schema
------
parts_fts (FTS5 virtual table):
    part_id TEXT, domain TEXT, title TEXT, standard TEXT, designation TEXT, text TEXT
parts_meta (regular table):
    part_id TEXT PRIMARY KEY, record_json TEXT

Ranking (contracts/parts.md §4):
    1. Exact designation match  (+1000)
    2. Standard prefix match    (+100)
    3. FTS5 BM25 score          (abs(bm25))
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Iterator


_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS parts_fts USING fts5(
    part_id UNINDEXED,
    domain,
    title,
    standard,
    designation,
    text
);
"""

_CREATE_META = """
CREATE TABLE IF NOT EXISTS parts_meta (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id TEXT UNIQUE NOT NULL,
    domain  TEXT NOT NULL,
    record_json TEXT NOT NULL
);
"""

_CREATE_IDX = "CREATE INDEX IF NOT EXISTS idx_parts_meta_domain ON parts_meta(domain);"


class PartsIndex:
    """In-memory SQLite FTS5 index over all indexed CatalogProvider records.

    Rebuilt idempotently via :meth:`rebuild`; thread-safe for concurrent reads
    after build (sqlite3 WAL is not needed since this is always :memory:).
    """

    def __init__(self) -> None:
        self._conn: sqlite3.Connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._build_schema()
        self._record_count: int = 0
        self._built_at: float = 0.0

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _build_schema(self) -> None:
        with self._conn:
            self._conn.execute(_CREATE_META)
            self._conn.execute(_CREATE_IDX)
            self._conn.execute(_CREATE_FTS)

    # ------------------------------------------------------------------
    # Index population
    # ------------------------------------------------------------------

    def rebuild(self, record_iter: Iterator[dict]) -> int:
        """Clear the index and repopulate from ``record_iter``. Returns count."""
        conn = self._conn
        with conn:
            conn.execute("DELETE FROM parts_meta")
            # For a plain FTS5 table, DROP and recreate is the reliable way to clear.
            conn.execute("DROP TABLE IF EXISTS parts_fts")
            conn.execute(_CREATE_FTS)

        count = 0
        batch: list[tuple] = []
        for rec in record_iter:
            row = _record_to_row(rec)
            if row is None:
                continue
            batch.append(row)
            count += 1
            if len(batch) >= 500:
                _insert_batch(conn, batch)
                batch.clear()
        if batch:
            _insert_batch(conn, batch)

        self._record_count = count
        self._built_at = time.time()
        return count

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               limit: int = 20) -> list[dict]:
        """Ranked FTS5 search. Returns PartRecord dicts, best-first."""
        if not query and domain is None:
            return self._enumerate(domain=None, limit=limit)

        conn = self._conn
        norm_q = query.strip().lower()

        # Build the FTS query string: quote tokens to prevent FTS5 syntax errors.
        fts_query = _build_fts_query(query)

        # Run the FTS query plus scoring.
        domain_filter = "AND f.domain = ?" if domain else ""
        params: list = []

        if fts_query:
            sql = f"""
            SELECT
                m.record_json,
                f.domain,
                f.part_id,
                (CASE WHEN lower(f.designation) = ?  THEN 1000 ELSE 0 END
                 + CASE WHEN lower(f.standard)   LIKE ? THEN  100 ELSE 0 END
                 + abs(bm25(parts_fts, 0, 0, 3, 2, 1, 0))) AS score
            FROM parts_fts f
            JOIN parts_meta m ON f.part_id = m.part_id
            WHERE parts_fts MATCH ? {domain_filter}
            ORDER BY score DESC
            LIMIT ?
            """
            params = [norm_q, norm_q + "%", fts_query]
            if domain:
                params.append(domain)
            params.append(limit)
        else:
            # Empty query with domain filter → enumerate.
            return self._enumerate(domain=domain, limit=limit)

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # FTS query parse error (e.g. empty after quoting) → fall back.
            return self._enumerate(domain=domain, limit=limit)

        results = []
        seen: set[str] = set()
        for row in rows:
            pid = row["part_id"]
            if pid in seen:
                continue
            seen.add(pid)
            try:
                results.append(json.loads(row["record_json"]))
            except Exception:  # noqa: BLE001
                pass
        return results

    def _enumerate(self, *, domain: str | None, limit: int) -> list[dict]:
        """Return up to limit records, optionally filtered by domain."""
        if domain:
            rows = self._conn.execute(
                "SELECT record_json FROM parts_meta WHERE domain=? LIMIT ?",
                (domain, limit)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT record_json FROM parts_meta LIMIT ?",
                (limit,)
            ).fetchall()
        results = []
        for row in rows:
            try:
                results.append(json.loads(row["record_json"]))
            except Exception:  # noqa: BLE001
                pass
        return results

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, part_id: str) -> dict | None:
        """Return the PartRecord for part_id, or None."""
        row = self._conn.execute(
            "SELECT record_json FROM parts_meta WHERE part_id=?",
            (part_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["record_json"])
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def record_count(self) -> int:
        return self._record_count

    @property
    def built_at(self) -> float:
        return self._built_at


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record_to_row(rec: dict) -> tuple | None:
    """Convert a PartRecord dict to the (part_id, domain, designation, standard, record_json) tuple."""
    part_id = rec.get("part_id")
    domain = rec.get("domain")
    if not part_id or not domain:
        return None
    title = rec.get("title") or ""
    standard = rec.get("standard") or ""
    designation = rec.get("designation") or ""
    # Build FTS text: title + standard + designation + param values.
    params = rec.get("params") or {}
    param_text = " ".join(str(v) for v in params.values())
    text = f"{title} {standard} {designation} {param_text}".strip()
    record_json = json.dumps(rec, ensure_ascii=False)
    # (rowid, part_id, domain, record_json) for parts_meta
    # (part_id, domain, title, standard, designation, text) for fts
    return (part_id, domain, designation, standard, title, text, record_json)


def _insert_batch(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    """Insert a batch of rows into parts_meta and parts_fts.

    Each tuple: (part_id, domain, designation, standard, title, text, record_json)
    """
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO parts_meta
               (part_id, domain, record_json)
               VALUES (?, ?, ?)""",
            [(r[0], r[1], r[6]) for r in batch]
        )
        # Insert into FTS: (part_id, domain, title, standard, designation, text)
        conn.executemany(
            """INSERT INTO parts_fts
               (part_id, domain, title, standard, designation, text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(r[0], r[1], r[4], r[3], r[2], r[5]) for r in batch]
        )


def _build_fts_query(query: str) -> str:
    """Build a safe FTS5 query from a user query string.

    Quotes each token to prevent FTS5 operator injection. Returns empty string
    if there's nothing to search.
    """
    tokens = query.strip().split()
    if not tokens:
        return ""
    # Use OR to match any token; full phrase also tried via prefix matching.
    # Quote each token for safety.
    quoted = ['"' + t.replace('"', '') + '"' for t in tokens if t]
    if not quoted:
        return ""
    return " OR ".join(quoted)
