"""PostgreSQL access for the gateway: one pool, one transaction helper, and a
plain-SQL migration runner.

Design notes
------------
- One process-wide ``psycopg_pool.ConnectionPool``. It is opened on first use
  and **fails fast** if Postgres is unreachable: a gateway that boots with a
  dead store is worse than one that refuses to boot, because every request
  then 500s with a different error and the health check looks alive. This
  mirrors ``auth.AuthMiddleware.__init__``'s refuse-to-construct posture.
- ``tx()`` yields a ``dict_row`` cursor inside a transaction: COMMIT on clean
  exit, ROLLBACK on any exception. Callers never see a connection. It is
  RE-ENTRANT per thread — an inner ``tx()`` joins the transaction the outer one
  already opened and does not commit on its own. That is what lets
  ``billing._process_event_once`` insert the Stripe event id and call
  ``accounts.set_plan`` in a single atomic unit without widening any signature.
- Migrations are plain SQL files, ``migrations/NNNN_name.sql``, applied in
  filename order and recorded in ``schema_migrations``. No Alembic: the repo is
  stdlib-first, the schema is small, and a versioned directory of SQL is
  reviewable in a way generated migration graphs are not.
- The whole migration pass runs inside a Postgres advisory lock so two uvicorn
  workers starting at once cannot apply the same file twice.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("chudware.gateway.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Arbitrary but fixed: the key identifying the schema-migration critical
# section. Any other advisory lock in this codebase must use a different one.
_MIGRATION_LOCK_KEY = 0x43485744  # "CHWD"

# Migration filenames are NNNN_name.sql. Anything else in the directory is a
# mistake (an editor backup, a half-renamed file) and must not be applied
# silently in whatever order the filesystem happens to return.
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

DEFAULT_DSN = "postgresql:///chudware_prod"

_pool: ConnectionPool | None = None
_pool_dsn: str | None = None


class DatabaseUnavailable(RuntimeError):
    """Postgres could not be reached. Raised instead of limping along."""


def dsn() -> str:
    """Connection string. Read from the environment rather than ``config.py``
    because the pool has to exist before anything else is wired up."""
    environment = os.environ.get("CHUDWARE_DEPLOY_ENV", "prod")
    if environment not in {"prod", "staging"}:
        raise DatabaseUnavailable("unknown deployment environment")
    raw = (os.environ.get("CHUDWARE_DB_URL") or "").strip()
    if environment == "staging":
        if not raw:
            raise DatabaseUnavailable("staging requires an explicit database credential")
        try:
            fields = psycopg.conninfo.conninfo_to_dict(raw)
        except psycopg.Error as exc:
            raise DatabaseUnavailable("invalid staging database credential") from exc
        if fields.get("dbname") != "chudware_staging" or fields.get("user") != "chudware_staging":
            raise DatabaseUnavailable("staging requires its own database and login role")
    return raw or DEFAULT_DSN


def pool() -> ConnectionPool:
    """Process-wide pool, re-created if the DSN changes (tests)."""
    global _pool, _pool_dsn
    want = dsn()
    if _pool is not None and _pool_dsn == want:
        return _pool
    if _pool is not None:
        close_pool()
    try:
        p = ConnectionPool(
            want,
            min_size=1,
            max_size=int(os.environ.get("CHUDWARE_DB_POOL_MAX") or 10),
            kwargs={"row_factory": dict_row},
            open=True,
            # Fail fast rather than queue every request behind a dead server.
            timeout=float(os.environ.get("CHUDWARE_DB_TIMEOUT") or 10.0),
        )
        p.wait(timeout=float(os.environ.get("CHUDWARE_DB_CONNECT_TIMEOUT") or 10.0))
    except Exception as exc:
        raise DatabaseUnavailable(
            f"cannot connect to PostgreSQL at {_redact(want)}: {exc}. "
            "Set CHUDWARE_DB_URL to a reachable database; the gateway will not "
            "start without its store."
        ) from exc
    _pool = p
    _pool_dsn = want
    return p


def close_pool() -> None:
    """Drop the pool (shutdown, and between tests that move the DSN)."""
    global _pool, _pool_dsn
    p, _pool, _pool_dsn = _pool, None, None
    if p is not None:
        try:
            p.close()
        except Exception:  # pragma: no cover - teardown must never raise
            log.debug("db: pool close failed", exc_info=True)


def _redact(url: str) -> str:
    """Strip the password out of a DSN before it reaches a log line."""
    return re.sub(r"://([^:/@]*):[^@]*@", r"://\1:***@", url)


@dataclass
class _Open:
    """The transaction open in the current context, and how deeply nested it is."""

    cursor: psycopg.Cursor
    depth: int = 0


# ContextVar, NOT threading.local. FastAPI runs `async def` endpoints as tasks
# on one event-loop thread, so a thread-local slot would be SHARED by every
# live coroutine: two overlapping Stripe deliveries would join each other's
# transaction and a rollback of one would discard the other's committed
# entitlement. Each asyncio Task gets a copied context, each threadpool-
# dispatched `def` endpoint gets its own, and plain sync callers get the
# default — correct in all three models FastAPI actually uses.
_active: ContextVar[_Open | None] = ContextVar("chudware_db_tx", default=None)

# Joining a connection that already blew up would silently write into a doomed
# transaction. These are the states in which joining is legitimate: IDLE is the
# outermost transaction before its first statement.
_JOINABLE = frozenset(
    {
        psycopg.pq.TransactionStatus.IDLE,
        psycopg.pq.TransactionStatus.ACTIVE,
        psycopg.pq.TransactionStatus.INTRANS,
    }
)


@contextmanager
def tx() -> Iterator[psycopg.Cursor]:
    """Yield a cursor inside a transaction.

    The outermost call commits on success, rolls back on any exception, and
    returns the connection to the pool. A nested call joins that same
    transaction and does nothing on exit, so a caller can compose two modules'
    writes into one atomic unit without either module knowing.
    """
    open_ = _active.get()
    if open_ is not None:
        conn = open_.cursor.connection
        if conn.closed or conn.info.transaction_status not in _JOINABLE:
            raise RuntimeError(
                "nested tx() found the enclosing transaction dead or aborted "
                f"(closed={conn.closed}, status={conn.info.transaction_status!r}); "
                "the outer transaction must be unwound before opening another"
            )
        open_.depth += 1
        try:
            yield open_.cursor
        finally:
            open_.depth -= 1
        return
    with pool().connection() as conn:  # psycopg commits/rolls back on exit
        with conn.cursor() as cur:
            open_ = _Open(cur)
            token = _active.set(open_)
            failed = False
            try:
                yield cur
            except BaseException:
                failed = True
                raise
            finally:
                leaked = open_.depth
                _active.reset(token)
                if leaked and not failed:
                    # A nested tx() escaped its `with` — anything it wrote is
                    # about to be committed by a caller that has no idea.
                    raise RuntimeError(
                        f"transaction exited with {leaked} nested tx() still open"
                    )


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def migration_files(directory: Path | None = None) -> list[Path]:
    """Every ``NNNN_name.sql`` in filename order. Raises on a stray .sql file
    rather than guessing whether it was meant to run."""
    d = MIGRATIONS_DIR if directory is None else directory
    if not d.is_dir():
        raise FileNotFoundError(f"migrations directory missing: {d}")
    files = []
    for path in sorted(d.iterdir()):
        if path.suffix != ".sql":
            continue
        if not _MIGRATION_RE.match(path.name):
            raise ValueError(
                f"migration {path.name} does not match NNNN_name.sql — rename it "
                "or move it out of the migrations directory"
            )
        files.append(path)
    versions = [p.name for p in files]
    if len(set(versions)) != len(versions):  # pragma: no cover - filesystem-unique
        raise ValueError(f"duplicate migration versions in {d}")
    return files


def _apply_python_migration(cur, version: str) -> bool:
    """Apply data rewrites that cannot be expressed portably in PostgreSQL SQL."""
    if version != "0011_session_hashes.sql":
        return False
    import hashlib

    cur.execute("SELECT sid FROM sessions")
    for row in cur.fetchall():
        sid = row["sid"]
        if len(sid) == 64 and all(ch in "0123456789abcdef" for ch in sid):
            continue
        digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()
        cur.execute("UPDATE sessions SET sid = %s WHERE sid = %s", (digest, sid))
    return True


def migrate(directory: Path | None = None) -> list[str]:
    """Apply every unapplied migration, in order, under an advisory lock.

    Returns the versions applied by THIS call — empty on an up-to-date
    database, which is what makes a second run a no-op.

    Each file runs in its own transaction together with its
    ``schema_migrations`` row, so a failure leaves the database at the last
    fully applied version. On failure the filename and the verbatim SQL error
    are logged before the exception propagates: systemd's ``Restart=on-failure``
    would otherwise turn this into a crash loop with no diagnosis.
    """
    files = migration_files(directory)
    applied: list[str] = []
    with pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_MIGRATIONS)
        conn.commit()
        # Session-scoped lock: held across the loop below, released by the
        # explicit unlock (or by the connection returning to the pool, which
        # psycopg resets). A second worker blocks here until we are done.
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
        conn.commit()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM schema_migrations")
                done = {row["version"] for row in cur.fetchall()}
            for path in files:
                if path.name in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                try:
                    with conn.cursor() as cur:
                        if not _apply_python_migration(cur, path.name):
                            cur.execute(sql)
                        cur.execute(
                            "INSERT INTO schema_migrations (version) VALUES (%s)",
                            (path.name,),
                        )
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    log.error("migration %s FAILED: %s", path.name, exc)
                    raise
                log.info("migration %s applied", path.name)
                applied.append(path.name)
        finally:
            # Never let a teardown failure mask the migration error above; the
            # lock is session-scoped and dies with the connection anyway.
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
                conn.commit()
            except Exception:
                log.debug("db: advisory unlock failed", exc_info=True)
    return applied


def init(directory: Path | None = None) -> None:
    """Open the pool and bring the schema up to date. Called once at startup.

    Any failure is fatal by design — see the module docstring.
    """
    pool()
    try:
        applied = migrate(directory)
    except DatabaseUnavailable:
        raise
    except Exception as exc:
        raise RuntimeError(f"database migration failed: {exc}") from exc
    if applied:
        log.info("database migrated: %s", ", ".join(applied))
    else:
        log.info("database schema up to date")
