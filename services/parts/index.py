"""SQLite FTS5 search index for the parts library — aicad.parts/1 (T-P1).

Uses only stdlib sqlite3 (FTS5 module). The live index is always an in-memory
database (fast, lock-free reads); it is also the lookup store for get() calls.

Persistence (T-PL4)
-------------------
When constructed with a ``path`` the index can be snapshotted to / restored from
an on-disk sqlite file, keyed by a caller-supplied *fingerprint* of everything
that could change the record set.  :meth:`load_if_fresh` restores a matching
snapshot with ZERO provider enumeration; :meth:`persist` writes one atomically.
Both degrade to "no cache" on any error — a broken cache never breaks search.

Schema
------
parts_fts (FTS5 virtual table):
    part_id TEXT, domain TEXT, title TEXT, standard TEXT, designation TEXT, text TEXT
parts_meta (regular table):
    part_id TEXT PRIMARY KEY, record_json TEXT
parts_cache_meta (regular table):
    key TEXT PRIMARY KEY, value TEXT   -- fingerprint, built_at, record_count,
                                       -- schema_version

Ranking (contracts/parts.md §4):
    1. Exact designation match  (+1000)
    2. Standard prefix match    (+100)
    3. FTS5 BM25 score          (abs(bm25))
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# Bumped by hand whenever the on-disk table layout changes. A snapshot written
# by a different schema version is discarded rather than migrated.
_SCHEMA_VERSION = 1


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

_CREATE_CACHE_META = """
CREATE TABLE IF NOT EXISTS parts_cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CREATE_IDX = "CREATE INDEX IF NOT EXISTS idx_parts_meta_domain ON parts_meta(domain);"


def _new_memory_db() -> sqlite3.Connection:
    """A fresh in-memory db shared across threads.

    ``cached_statements=0`` is REQUIRED: PartsHub hands one index to many
    worker threads, and pysqlite's per-connection statement cache is not
    re-entrant — two threads running the same SQL would otherwise trample each
    other's cursor ("another row available" / garbled rows).
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False, cached_statements=0)
    conn.row_factory = sqlite3.Row
    return conn


class PartsIndex:
    """In-memory SQLite FTS5 index over all indexed CatalogProvider records.

    Rebuilt idempotently via :meth:`rebuild`; thread-safe for concurrent reads
    after build (sqlite3 WAL is not needed since the live db is always
    ``:memory:``).

    ``path`` (optional) enables the on-disk snapshot cache: :meth:`persist`
    writes the current index there atomically and :meth:`load_if_fresh`
    restores it when the caller's fingerprint still matches.  ``None`` (the
    default) is the historical pure in-memory behaviour.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path: Path | None = Path(path).expanduser() if path else None
        self._conn: sqlite3.Connection = _new_memory_db()
        self._build_schema()
        self._record_count: int = 0
        self._built_at: float = 0.0
        self._fingerprint: str = ""
        self._loaded_from_cache: bool = False

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _build_schema(self) -> None:
        with self._conn:
            self._conn.execute(_CREATE_META)
            self._conn.execute(_CREATE_IDX)
            self._conn.execute(_CREATE_FTS)
            self._conn.execute(_CREATE_CACHE_META)

    # ------------------------------------------------------------------
    # Index population
    # ------------------------------------------------------------------

    def rebuild(self, record_iter: Iterator[dict]) -> int:
        """Clear the index and repopulate from ``record_iter``. Returns count.

        Populates a NEW in-memory db and swaps it in only once the walk
        completed: concurrent readers keep a consistent index throughout (and a
        provider blowing up mid-walk leaves the previous index intact).
        """
        conn = _new_memory_db()
        with conn:
            conn.execute(_CREATE_META)
            conn.execute(_CREATE_IDX)
            conn.execute(_CREATE_FTS)
            conn.execute(_CREATE_CACHE_META)

        count = 0
        batch: list[tuple] = []
        try:
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
        except BaseException:
            with contextlib.suppress(Exception):
                conn.close()
            raise

        # Publish atomically; the outgoing connection is dropped, not closed,
        # so an in-flight search finishes against its own snapshot.
        self._conn = conn
        self._record_count = count
        self._built_at = time.time()
        self._fingerprint = ""
        self._loaded_from_cache = False
        return count

    # ------------------------------------------------------------------
    # On-disk snapshot cache
    # ------------------------------------------------------------------

    def load_if_fresh(self, fingerprint: str) -> bool:
        """Restore the on-disk snapshot when it matches ``fingerprint``.

        Returns True when the live index now holds the cached records (zero
        provider enumeration needed).  A missing file, a schema/fingerprint
        mismatch, a corrupt db or ANY error returns False — and best-effort
        deletes the unusable file so the next :meth:`persist` starts clean.
        The live index is left untouched on failure.
        """
        path = self._path
        if path is None or not fingerprint:
            return False
        if not path.is_file():
            return False

        src: sqlite3.Connection | None = None
        fresh: sqlite3.Connection | None = None
        try:
            src = sqlite3.connect(_read_only_uri(path), uri=True)
            meta = {
                str(k): str(v) for k, v in
                src.execute("SELECT key, value FROM parts_cache_meta").fetchall()
            }
            if meta.get("schema_version") != str(_SCHEMA_VERSION):
                raise _CacheMiss("schema_version mismatch")
            if meta.get("fingerprint") != fingerprint:
                raise _CacheMiss("fingerprint mismatch")
            # Probe both stores so a truncated file / missing FTS5 module fails
            # here rather than on the first user search.
            count = int(src.execute("SELECT count(*) FROM parts_meta").fetchone()[0])
            src.execute("SELECT part_id FROM parts_fts LIMIT 1").fetchone()

            fresh = _new_memory_db()
            src.backup(fresh)
            # Sanity: the restored copy must answer the same queries.
            fresh.execute("SELECT count(*) FROM parts_meta").fetchone()
        except Exception as exc:  # noqa: BLE001 — a bad cache is never fatal
            if not isinstance(exc, _CacheMiss):
                log.debug("parts index cache unusable at %s: %s", path, exc)
            with contextlib.suppress(Exception):
                if fresh is not None:
                    fresh.close()
            with contextlib.suppress(Exception):
                if src is not None:
                    src.close()
            self._discard_cache_file()
            return False

        with contextlib.suppress(Exception):
            src.close()
        # Swap the live connection WITHOUT closing the old one: a concurrent
        # reader may still be iterating it. Dropping our reference lets sqlite3
        # close it once that last cursor goes away.
        self._conn = fresh
        self._record_count = count
        self._built_at = _as_float(meta.get("built_at"), time.time())
        self._fingerprint = fingerprint
        self._loaded_from_cache = True
        return True

    def persist(self, fingerprint: str) -> None:
        """Atomically snapshot the live index to ``path`` under ``fingerprint``.

        Builds into ``<path>.tmp`` via :meth:`sqlite3.Connection.backup` then
        ``os.replace``s it into place.  Every OSError / sqlite error is
        swallowed: failing to write the cache must never break search.
        """
        path = self._path
        if path is None or not fingerprint:
            return
        tmp = path.with_suffix(".tmp")
        dest: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                tmp.unlink()
            dest = sqlite3.connect(str(tmp))
            self._conn.backup(dest)
            with dest:
                dest.execute(_CREATE_CACHE_META)
                dest.executemany(
                    "INSERT OR REPLACE INTO parts_cache_meta (key, value) VALUES (?, ?)",
                    [
                        ("schema_version", str(_SCHEMA_VERSION)),
                        ("fingerprint", str(fingerprint)),
                        ("built_at", repr(float(self._built_at or time.time()))),
                        ("record_count", str(int(self._record_count))),
                    ],
                )
            dest.close()
            dest = None
            os.replace(tmp, path)
            self._fingerprint = fingerprint
        except (OSError, sqlite3.Error, ValueError) as exc:
            log.debug("parts index cache write failed for %s: %s", path, exc)
        finally:
            if dest is not None:
                with contextlib.suppress(Exception):
                    dest.close()
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()

    def _discard_cache_file(self) -> None:
        """Best-effort removal of an unusable snapshot (never raises)."""
        if self._path is None:
            return
        with contextlib.suppress(OSError):
            self._path.unlink()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | list[str] | None = None,
               limit: int = 20) -> list[dict]:
        """Ranked FTS5 search. Returns PartRecord dicts, best-first.

        ``domain`` may be a single domain string or a list of domains
        (SQL ``IN (...)``) — Parts Library V2 taxonomy routing passes lists.
        """
        domains = _normalize_domains(domain)
        if not query and domains is None:
            return self._enumerate(domain=None, limit=limit)

        conn = self._conn
        norm_q = query.strip().lower()

        # Build the FTS query string: quote tokens to prevent FTS5 syntax errors.
        fts_query = _build_fts_query(query)

        # Run the FTS query plus scoring.
        domain_filter = ""
        if domains:
            domain_filter = "AND f.domain IN (%s)" % ",".join("?" * len(domains))
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
            if domains:
                params.extend(domains)
            params.append(limit)
        else:
            # Empty query with domain filter → enumerate.
            return self._enumerate(domain=domains, limit=limit)

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # FTS query parse error (e.g. empty after quoting) → fall back.
            return self._enumerate(domain=domains, limit=limit)

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

    def _enumerate(self, *, domain: str | list[str] | None, limit: int) -> list[dict]:
        """Return up to limit records, optionally filtered by domain(s)."""
        domains = _normalize_domains(domain)
        if domains:
            rows = self._conn.execute(
                "SELECT record_json FROM parts_meta WHERE domain IN (%s) LIMIT ?"
                % ",".join("?" * len(domains)),
                (*domains, limit)
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

    @property
    def cache_path(self) -> Path | None:
        """The on-disk snapshot location, or None for a pure in-memory index."""
        return self._path

    @property
    def fingerprint(self) -> str:
        """Fingerprint of the last successful load/persist ("" when unknown)."""
        return self._fingerprint

    @property
    def loaded_from_cache(self) -> bool:
        """True when the current contents came from the on-disk snapshot."""
        return self._loaded_from_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CacheMiss(Exception):
    """Internal: an expected (non-error) snapshot rejection."""


def _read_only_uri(path: Path) -> str:
    """sqlite3 read-only URI for ``path`` (handles spaces / odd characters)."""
    return path.absolute().as_uri() + "?mode=ro"


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _normalize_domains(domain: str | list | tuple | None) -> list[str] | None:
    """Coerce a domain filter (str | list[str] | None) to a non-empty list or None."""
    if domain is None:
        return None
    if isinstance(domain, str):
        return [domain] if domain else None
    domains = [str(d) for d in domain if d]
    return domains or None


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
