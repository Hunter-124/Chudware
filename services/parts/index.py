"""SQLite FTS5 search index for the parts library — aicad.parts/1.

Uses only stdlib sqlite3 (FTS5 module). The live index is always an in-memory
database (fast, lock-free reads); it is also the lookup store for get() calls.

Persistence
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
    3. Coverage-aware term-quorum bonus (dominates bm25 magnitude)
    4. FTS5 BM25 score          (-bm25, small non-zero weight on ``text``)
"""
from __future__ import annotations

import contextlib
import itertools
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from services.parts.query_norm import expand_terms, normalize_query, term_tiers

log = logging.getLogger(__name__)

# Bumped by hand whenever the on-disk table layout changes. A snapshot written
# by a different schema version is discarded rather than migrated.
_SCHEMA_VERSION = 2


_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS parts_fts USING fts5(
    part_id UNINDEXED,
    domain,
    title,
    standard,
    designation,
    text,
    prefix='2 3'
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
        norm_q = normalize_query(query)

        # Build the coverage-aware, quorum-relaxed match plan: an ordered list
        # of (match_expression, quorum) tiers, most confident (full term-group
        # coverage) first. A term-group with zero corpus matches is dropped
        # from every match expression so a single rare/absent token can never
        # carry the whole query (see contracts/parts.md §4 accuracy bug).
        plan = _build_fts_query(query, conn)
        if not plan:
            if not query.strip():
                # Whitespace-only query (with a domain filter) → enumerate,
                # same as the no-domain-and-no-query case above.
                return self._enumerate(domain=domains, limit=limit)
            # Real query terms, but NONE of them match anything in the
            # corpus: this must be an empty result, not an arbitrary
            # enumeration of unrelated records (that was the original
            # accuracy bug's twin — silently substituting junk for "no
            # match").
            return []

        domain_filter = ""
        if domains:
            domain_filter = "AND f.domain IN (%s)" % ",".join("?" * len(domains))

        # Best score seen per part_id across tiers. Each tier's SQL score
        # already embeds a large quorum bonus (quorum * _QUORUM_UNIT) that
        # dwarfs any plausible bm25 magnitude, so merging by max-score and
        # sorting once at the end can never place a lower-coverage row above
        # a higher-coverage one, regardless of bm25.
        best: dict[str, tuple[float, str]] = {}
        for match_expr, quorum in plan:
            sql = f"""
            SELECT
                m.record_json,
                f.part_id,
                (CASE WHEN lower(f.designation) = ?  THEN 1000 ELSE 0 END
                 + CASE WHEN lower(f.standard)   LIKE ? THEN  100 ELSE 0 END
                 + {int(quorum) * _QUORUM_UNIT}
                 + (-1.0 * bm25(parts_fts, 0, 3, 2, 1, 1))) AS score
            FROM parts_fts f
            JOIN parts_meta m ON f.part_id = m.part_id
            WHERE parts_fts MATCH ? {domain_filter}
            ORDER BY score DESC
            LIMIT ?
            """
            params: list = [norm_q, norm_q + "%", match_expr]
            if domains:
                params.extend(domains)
            params.append(limit)
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # Malformed match expression for this tier — skip it, try the
                # next (looser) tier rather than failing the whole search.
                continue
            for row in rows:
                pid = row["part_id"]
                score = row["score"]
                prev = best.get(pid)
                if prev is None or score > prev[0]:
                    best[pid] = (score, row["record_json"])
            if len(best) >= limit:
                # AND (or the current tier) already yields enough — no need to
                # relax further.
                break

        if not best:
            return self._enumerate(domain=domains, limit=limit)

        ordered = sorted(best.values(), key=lambda t: -t[0])[:limit]
        results = []
        for _, record_json in ordered:
            try:
                results.append(json.loads(record_json))
            except Exception:  # noqa: BLE001
                pass
        return results

    def term_coverage(self, query: str) -> tuple[int, int]:
        """(# query term-groups with >=1 corpus match, total term-groups).

        Cheap: one FTS5 existence probe per group. Never raises; returns
        ``(0, 0)`` for an empty/unparseable query.
        """
        try:
            groups = expand_terms(query)
        except Exception:  # noqa: BLE001
            groups = None
        if not groups:
            return (0, 0)
        matched = 0
        conn = self._conn
        for group in groups:
            clause = _group_match_clause(group)
            if not clause:
                continue
            try:
                row = conn.execute(
                    "SELECT 1 FROM parts_fts WHERE parts_fts MATCH ? LIMIT 1",
                    (clause,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is not None:
                matched += 1
        return (matched, len(groups))

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
    # Build FTS text: title + standard + designation + aliases + param values.
    # `aliases` matters: a standard's alternate designations are how buyers spell it
    # ("DIN 912" for ISO 4762, "SHCS"), and without them "DIN 912 M3" matched no
    # fastener at all and relaxed down to an O-ring whose 0.912" ID carried the "912".
    params = rec.get("params") or {}
    param_text = " ".join(str(v) for v in params.values())
    alias_text = " ".join(str(a) for a in (rec.get("aliases") or []) if a)
    text = f"{title} {standard} {designation} {alias_text} {param_text}".strip()
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


# Score bonus per satisfied term-group-quorum level. Deliberately far larger
# than any plausible bm25 magnitude (small corpora, short text fields) so a
# row's coverage tier always dominates the ranking, never the reverse.
_QUORUM_UNIT = 1000

# Above this many surviving term-groups, exhaustively enumerating every
# "k of n" combination for quorum relaxation is no longer cheap; fall back to
# a flat OR of every surviving group as the single relaxed tier instead.
_MAX_QUORUM_GROUPS = 8

# Only alnum survives into an FTS5 *bare* prefix token (`term*`) — prefix
# queries cannot be quoted, so this is the injection guard for that path.
_PREFIX_SAFE_RE = re.compile(r"[^0-9a-z]+")


def _quote_fts_token(term: str) -> str:
    """Quote one literal token as an FTS5 phrase — safe against any operator."""
    return '"' + term.replace('"', '""') + '"'


def _prefix_fts_token(term: str) -> str:
    """Bare alnum-only prefix token (``term*``), or "" if nothing safe remains."""
    bare = _PREFIX_SAFE_RE.sub("", term.lower())
    return bare + "*" if bare else ""


def _group_match_clause(group: list[str], *, prefix: bool = False) -> str:
    """FTS5 match expression for one synonym group: ``("a" OR "b" OR c*)``.

    Every literal variant is quoted (safe against FTS5 operator injection).
    When ``prefix`` is set, an unquoted alnum-only prefix variant of each
    term is also OR'd in (FTS5 prefix queries cannot be quoted).
    """
    parts: list[str] = []
    seen: set[str] = set()
    for term in group or ():
        term = (term or "").strip()
        if not term:
            continue
        quoted = _quote_fts_token(term)
        if quoted not in seen:
            seen.add(quoted)
            parts.append(quoted)
        if prefix:
            pfx = _prefix_fts_token(term)
            if pfx and pfx not in seen:
                seen.add(pfx)
                parts.append(pfx)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "(" + " OR ".join(parts) + ")"


def _clause_has_hit(clause: str, conn: sqlite3.Connection) -> bool:
    """True when ``clause`` matches at least one indexed record."""
    try:
        return conn.execute(
            "SELECT 1 FROM parts_fts WHERE parts_fts MATCH ? LIMIT 1", (clause,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _narrowest_present_clause(core: list[str], fallback: list[str],
                              conn: sqlite3.Connection, *, prefix: bool) -> str:
    """The tightest clause for one term-group that still matches the corpus.

    Core variants first: a hit there means the corpus carries the term as the user
    typed it, so the broadening fallbacks (query_norm._term_tiers — the fragments of
    a glued token, e.g. bare "3" from "m3", bare "12"/"mm" from "12mm") stay OUT of
    the clause. Those fragments hit 753 and 318 of the 3182 shipped records against
    46 for "m3" and 72 for "12mm", so including them let any record with a stray
    digit satisfy the group and reach the top coverage tier — that is what ranked a
    KiCad mounting-hole footprint and three shaft collars above
    fastener:ISO 4762:M3 for "M3 socket head cap screw 12mm". Only a core MISS falls
    back to the full group, which keeps a glued spelling the corpus does not carry
    ("608zz" -> "608") findable.
    """
    core_clause = _group_match_clause(core, prefix=prefix)
    if core_clause and _clause_has_hit(core_clause, conn):
        return core_clause
    full_clause = _group_match_clause([*core, *fallback], prefix=prefix)
    if full_clause and full_clause != core_clause and _clause_has_hit(full_clause, conn):
        return full_clause
    return ""


def _build_fts_query(query: str, conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Build a coverage-aware, quorum-relaxed FTS5 match plan for ``query``.

    Splits the query into tiered synonym term-groups (:func:`term_tiers`) and
    quotes every literal so no bare operator ever reaches FTS5. Each group
    contributes its NARROWEST clause that still matches something
    (:func:`_narrowest_present_clause`); a group with ZERO corpus matches at any
    width is dropped from every match expression it would otherwise appear in —
    one rare/absent token (e.g. a bearing designation like "17") must never carry
    an unrelated multi-term query — but it still counts toward the group total
    (see :meth:`PartsIndex.term_coverage`).

    Returns an ordered list of ``(match_expression, quorum)`` tiers, most
    confident first:
      1. AND of every surviving group (full coverage), quorum = group count.
      2. Progressively relaxed "k of n surviving groups" unions, quorum = k,
         for decreasing k — a higher-quorum hit is always listed in an
         earlier tier than a lower-quorum one.
    Returns ``[]`` when nothing in the query matches anything in the corpus.
    """
    try:
        tiers = term_tiers(query)
    except Exception:  # noqa: BLE001
        tiers = None

    if not tiers:
        tokens = [t for t in (query or "").strip().split() if t]
        if not tokens:
            return []
        tiers = [([t], []) for t in tokens]

    n_groups = len(tiers)
    present: list[str] = []
    for i, (core, fallback) in enumerate(tiers):
        clause = _narrowest_present_clause(core, fallback, conn,
                                           prefix=(i == n_groups - 1))
        if clause:
            present.append(clause)

    if not present:
        return []

    m = len(present)
    plan: list[tuple[str, int]] = []
    seen_exprs: set[str] = set()

    def _add(expr: str, quorum: int) -> None:
        if expr and expr not in seen_exprs:
            seen_exprs.add(expr)
            plan.append((expr, quorum))

    # Tier 1: every surviving group required (full coverage).
    _add(" AND ".join(present), m)

    if m > 1:
        if m <= _MAX_QUORUM_GROUPS:
            for k in range(m - 1, 0, -1):
                combos = [
                    "(" + " AND ".join(combo) + ")"
                    for combo in itertools.combinations(present, k)
                ]
                _add(" OR ".join(combos), k)
        else:
            # Too many groups to enumerate combinations cheaply — the single
            # relaxed fallback tier is "any surviving group matches".
            _add(" OR ".join(present), 1)

    return plan
