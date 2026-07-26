"""services/browser/page_cache.py — bounded on-disk cache for fetched pages.

Every supplier adapter reaches the network through the single chokepoint
:func:`services.browser.session.browser_fetch`. Repeat searches (and the very
common case of two adapters wanting the same product page minutes apart) used to
re-render the page in CEF each time: seconds of latency, a rate-limiter wait, and
one more request against a vendor that is already watching us. This module puts a
content cache in front of that chokepoint.

Layout — ``<paths.ROOT>/cache/pages/``::

    index.sqlite        metadata only: key, site, url, status, fetched_ts,
                        last_used, bytes, blob_path
    ab/abcdef….html.gz  the gzipped HTML body, one file per entry, sharded by
                        the first two hex chars of the key

Bodies live OUTSIDE sqlite on purpose. Product pages are routinely 1–4 MiB of
markup; storing them as sqlite BLOBs bloats the db file, makes every metadata
query page through body data, and turns eviction into a VACUUM problem (sqlite
does not return freed pages to the filesystem without one, and VACUUM rewrites
the entire db). With separate files, eviction is an ``os.remove`` and a row
delete, and the metadata db stays small enough to stay in the OS page cache.

Bounded three ways: a TTL (entries older than it are misses and get deleted
lazily), a total-size cap enforced by LRU eviction on ``last_used``, and a
per-entry size ceiling. Eviction runs at most once a minute and only after a
write, so the read path never pays for it. ``last_used`` touches are batched
through a dirty set flushed at most every few seconds — a 100-result search does
one UPDATE, not 100.

Environment:

- ``CHUDWARE_PAGE_CACHE=0``       — kill switch; nothing is read, written, or created.
- ``CHUDWARE_PAGE_CACHE_TTL_S``   — entry lifetime in seconds (default 6 h).
- ``CHUDWARE_PAGE_CACHE_MAX_MB``  — total on-disk budget in MiB (default 512).

Nothing here ever raises at the caller. A missing directory, a corrupt index, a
read-only volume, a half-written blob — every one of them degrades to "there is
no cache" for that call. A cache is an optimisation; it is never allowed to be
the reason a fetch fails.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from services import paths

#: Entry lifetime (seconds) before a hit is treated as a miss. Product pages
#: (price, stock) go stale; six hours keeps a working session snappy without
#: serving yesterday's availability.
_DEFAULT_TTL_S = 6 * 3600
#: Total on-disk budget (MiB) across blobs before LRU eviction kicks in.
_DEFAULT_MAX_MB = 512
#: Largest single page we are willing to store, compressed input size. Above
#: this the page is almost certainly a data dump, not a product page.
_MAX_ENTRY_BYTES = 4 * 1024 * 1024
#: Minimum spacing (seconds) between ``last_used`` flushes — the disk-wear guard.
_LAST_USED_FLUSH_S = 5.0
#: Minimum spacing (seconds) between LRU eviction sweeps.
_EVICT_INTERVAL_S = 60.0
#: Evict down to this fraction of the cap so a sweep is not needed every put.
_EVICT_TARGET_FRAC = 0.9

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    key        TEXT PRIMARY KEY,
    site       TEXT,
    url        TEXT,
    status     INT,
    fetched_ts REAL,
    last_used  REAL,
    bytes      INT,
    blob_path  TEXT
);
CREATE INDEX IF NOT EXISTS pages_last_used ON pages(last_used);
CREATE INDEX IF NOT EXISTS pages_site ON pages(site);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_dir: Path | None = None
#: key -> last_used epoch, coalesced until the next flush.
_dirty: dict[str, float] = {}
_last_flush = 0.0
_last_evict = 0.0
_hits = 0
_misses = 0


# --- configuration -------------------------------------------------------------

_FALSEY = {"0", "false", "no", "off", ""}


def enabled() -> bool:
    """False when ``CHUDWARE_PAGE_CACHE`` is switched off (default on)."""
    return os.environ.get("CHUDWARE_PAGE_CACHE", "1").strip().lower() not in _FALSEY


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        val = float(raw)
        return val if val > 0 else default
    except Exception:  # noqa: BLE001 — a bad env var must not disable fetching
        return default


def _ttl_s() -> float:
    return _env_float("CHUDWARE_PAGE_CACHE_TTL_S", float(_DEFAULT_TTL_S))


def _max_bytes() -> float:
    return _env_float("CHUDWARE_PAGE_CACHE_MAX_MB", float(_DEFAULT_MAX_MB)) * 1024 * 1024


def cache_dir() -> Path:
    """The on-disk cache root. Resolved per call so test isolation (which swaps
    ``services.paths.ROOT``) takes effect without reimporting this module."""
    return Path(paths.ROOT) / "cache" / "pages"


def key_for(url: str, site: str) -> str:
    """Stable 128-bit content key for a (site, url) pair."""
    return hashlib.blake2b(f"{site}\n{url}".encode("utf-8", "replace"),
                           digest_size=16).hexdigest()


# --- connection ----------------------------------------------------------------

def _connect() -> sqlite3.Connection | None:
    """Open (once) the metadata db, creating the tree. None if unusable."""
    global _conn, _conn_dir
    d = cache_dir()
    if _conn is not None and _conn_dir == d:
        return _conn
    _close_locked()
    try:
        d.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(d / "index.sqlite"), timeout=2.0,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    except Exception:  # noqa: BLE001 — unwritable dir / corrupt db / locked file
        return None
    _conn, _conn_dir = conn, d
    return conn


def _close_locked() -> None:
    global _conn, _conn_dir
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001
            pass
    _conn, _conn_dir = None, None


def _drop_connection() -> None:
    """Forget a connection that just errored so the next call reopens it."""
    with _lock:
        _close_locked()


def close() -> None:
    """Flush pending LRU touches and drop the connection (tests, shutdown)."""
    with _lock:
        try:
            _flush_locked(force=True)
        except Exception:  # noqa: BLE001
            pass
        _close_locked()


# --- timestamps ----------------------------------------------------------------

def _iso(epoch: float) -> str:
    """Epoch → the contract's ISO-8601 ``Z`` instant (matches research.common.now_ts)."""
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: Any) -> float:
    """Contract ISO-8601 ``Z`` instant → epoch, falling back to now."""
    if isinstance(iso, (int, float)):
        return float(iso)
    try:
        return _dt.datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except Exception:  # noqa: BLE001
        return time.time()


# --- blobs ---------------------------------------------------------------------

def _blob_rel(key: str) -> str:
    return f"{key[:2]}/{key}.html.gz"


def _unlink(root: Path, rel: Any) -> None:
    if not rel:
        return
    try:
        os.remove(root / str(rel))
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — a stuck file just wastes space
        pass


# --- last_used batching --------------------------------------------------------

def _flush_locked(*, force: bool = False) -> None:
    """Write coalesced ``last_used`` touches. Caller holds ``_lock``."""
    global _last_flush
    if not _dirty:
        return
    now = time.time()
    if not force and (now - _last_flush) < _LAST_USED_FLUSH_S:
        return
    pending = list(_dirty.items())
    _dirty.clear()
    _last_flush = now
    conn = _connect()
    if conn is None:
        return
    try:
        conn.executemany("UPDATE pages SET last_used=? WHERE key=?",
                         [(ts, k) for k, ts in pending])
        conn.commit()
    except Exception:  # noqa: BLE001
        _close_locked()


# --- public API ----------------------------------------------------------------

def get(url: str, site: str, *, max_age_s: float | None = None) -> dict | None:
    """Return the cached ``browser_fetch`` payload for ``(site, url)``, or None.

    The payload is the original success dict plus ``cached: True`` and ``age_s``.
    Expired entries are deleted and reported as misses. Touching ``last_used`` is
    batched (see :data:`_LAST_USED_FLUSH_S`) — a hundred hits in a burst cost one
    UPDATE, not a hundred. Never raises, never evicts.
    """
    global _hits, _misses
    if not enabled():
        return None
    key = key_for(url, site)
    ttl = _ttl_s() if max_age_s is None else max_age_s
    try:
        with _lock:
            conn = _connect()
            if conn is None:
                _misses += 1
                return None
            try:
                row = conn.execute(
                    "SELECT status, fetched_ts, blob_path FROM pages WHERE key=?",
                    (key,)).fetchone()
            except Exception:  # noqa: BLE001 — corrupt/locked index → behave as empty
                _close_locked()
                _misses += 1
                return None
            if row is None:
                _misses += 1
                return None
            status, fetched_ts, blob_rel = row
            fetched_ts = float(fetched_ts or 0.0)
            now = time.time()
            age = now - fetched_ts
            if ttl > 0 and age > ttl:
                _delete_locked(conn, [(key, blob_rel)])
                _misses += 1
                return None
            root = cache_dir()
            try:
                with gzip.open(root / str(blob_rel), "rb") as fh:
                    content = fh.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — blob lost/truncated → drop the row
                _delete_locked(conn, [(key, blob_rel)])
                _misses += 1
                return None
            _dirty[key] = now
            _flush_locked()
            _hits += 1
    except Exception:  # noqa: BLE001 — belt and braces; a cache never fails a fetch
        return None
    from .profiles import slug  # lazy: profiles imports paths/json, keep get cheap
    return {"ok": True, "url": url, "status": status, "content": content,
            "fetched_ts": _iso(fetched_ts), "session_profile_used": slug(site),
            "cached": True, "age_s": round(max(age, 0.0), 3)}


def put(url: str, site: str, payload: dict) -> None:
    """Store a SUCCESSFUL ``browser_fetch`` payload. No-op for anything else.

    Failures, challenge/interstitial pages and HTTP>=400 responses are never
    cached: they all arrive as ``ok: False`` §6 envelopes from
    :mod:`services.browser.session` (which classifies them before we see them),
    and the status guard below is the second line of defence. Oversized bodies
    are skipped rather than allowed to dominate the budget. Runs the LRU sweep at
    most once a minute, on this (write) path only.
    """
    if not enabled():
        return
    try:
        if not isinstance(payload, dict) or not payload.get("ok"):
            return
        content = payload.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            return
        status = payload.get("status")
        if isinstance(status, (int, float)) and int(status) >= 400:
            return
        raw = content.encode("utf-8", "replace")
        if len(raw) > _MAX_ENTRY_BYTES:
            return
        key = key_for(url, site)
        rel = _blob_rel(key)
        with _lock:
            conn = _connect()
            if conn is None:
                return
            root = cache_dir()
            dest = root / rel
            tmp = dest.with_suffix(".tmp")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(tmp, "wb", compresslevel=6) as fh:
                    fh.write(raw)
                os.replace(tmp, dest)
                size = dest.stat().st_size
            except Exception:  # noqa: BLE001 — full disk / read-only volume
                try:
                    os.remove(tmp)
                except Exception:  # noqa: BLE001
                    pass
                return
            now = time.time()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO pages"
                    " (key, site, url, status, fetched_ts, last_used, bytes, blob_path)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (key, str(site), str(url),
                     int(status) if isinstance(status, (int, float)) else None,
                     _epoch(payload.get("fetched_ts")), now, int(size), rel))
                conn.commit()
            except Exception:  # noqa: BLE001
                _close_locked()
                _unlink(root, rel)
                return
            _flush_locked(force=True)
            _maybe_evict_locked(conn, now)
    except Exception:  # noqa: BLE001
        return


def stats() -> dict:
    """Entry/byte totals plus process-lifetime hit/miss counters."""
    out = {"entries": 0, "bytes": 0, "hits": _hits, "misses": _misses,
           "enabled": enabled()}
    if not out["enabled"]:
        return out
    try:
        with _lock:
            conn = _connect()
            if conn is None:
                return out
            try:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM pages").fetchone()
            except Exception:  # noqa: BLE001
                _close_locked()
                return out
            out["entries"] = int(row[0] or 0)
            out["bytes"] = int(row[1] or 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def purge(site: str | None = None) -> int:
    """Drop every entry (or every entry for ``site``). Returns rows removed."""
    if not enabled():
        return 0
    try:
        with _lock:
            _dirty.clear()
            conn = _connect()
            if conn is None:
                return 0
            try:
                if site is None:
                    rows = conn.execute("SELECT key, blob_path FROM pages").fetchall()
                else:
                    rows = conn.execute(
                        "SELECT key, blob_path FROM pages WHERE site=?",
                        (str(site),)).fetchall()
            except Exception:  # noqa: BLE001
                _close_locked()
                return 0
            return _delete_locked(conn, rows)
    except Exception:  # noqa: BLE001
        return 0


# --- eviction ------------------------------------------------------------------

def _delete_locked(conn: sqlite3.Connection, rows: list) -> int:
    """Remove rows (``(key, blob_path)`` pairs) and their blobs. Holds ``_lock``."""
    if not rows:
        return 0
    root = cache_dir()
    for _key, rel in rows:
        _unlink(root, rel)
    try:
        conn.executemany("DELETE FROM pages WHERE key=?", [(k,) for k, _ in rows])
        conn.commit()
    except Exception:  # noqa: BLE001
        _close_locked()
        return 0
    for key, _rel in rows:
        _dirty.pop(key, None)
    return len(rows)


def _maybe_evict_locked(conn: sqlite3.Connection, now: float) -> None:
    """LRU-evict down to the target when over budget. Write path only, ≤1/min."""
    global _last_evict
    if (now - _last_evict) < _EVICT_INTERVAL_S:
        return
    _last_evict = now
    cap = _max_bytes()
    try:
        total = int(conn.execute("SELECT COALESCE(SUM(bytes),0) FROM pages").fetchone()[0] or 0)
        if total <= cap:
            return
        target = cap * _EVICT_TARGET_FRAC
        victims: list[tuple[str, Any]] = []
        for key, rel, size in conn.execute(
                "SELECT key, blob_path, bytes FROM pages ORDER BY last_used ASC"):
            if total <= target:
                break
            victims.append((key, rel))
            total -= int(size or 0)
    except Exception:  # noqa: BLE001
        _close_locked()
        return
    _delete_locked(conn, victims)


def _force_evict_for_tests() -> None:
    """Run the sweep now, ignoring the once-a-minute throttle."""
    global _last_evict
    with _lock:
        conn = _connect()
        if conn is None:
            return
        _last_evict = 0.0
        _maybe_evict_locked(conn, time.time())


def reset_state() -> None:
    """Drop cached connection, pending touches, throttles and counters.

    For tests that re-point :data:`services.paths.ROOT`; harmless in production.
    """
    global _last_flush, _last_evict, _hits, _misses
    with _lock:
        _dirty.clear()
        _close_locked()
        _last_flush = 0.0
        _last_evict = 0.0
        _hits = 0
        _misses = 0
