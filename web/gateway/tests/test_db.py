"""The database seam itself: migration runner, pool fail-fast, tx() semantics.

These are the invariants everything else in the gateway now rests on, so they
are tested directly rather than only through the modules that use them.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import psycopg
import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import db  # noqa: E402


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


def test_migrations_are_recorded_once_and_rerun_is_a_noop(pg):
    expected = [p.name for p in db.migration_files()]
    assert expected, "no migrations on disk"

    # The `pg` fixture already ran them once.
    again = db.migrate()
    assert again == [], "second run applied something; migrations are not idempotent"

    with db.tx() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        recorded = [r["version"] for r in cur.fetchall()]
    assert recorded == sorted(expected)


def test_migration_files_rejects_a_misnamed_file(tmp_path):
    (tmp_path / "0001_ok.sql").write_text("SELECT 1")
    (tmp_path / "oops.sql").write_text("SELECT 1")
    with pytest.raises(ValueError, match="oops.sql"):
        db.migration_files(tmp_path)


def test_migration_files_are_ordered_by_filename(tmp_path):
    for name in ("0010_j.sql", "0002_b.sql", "0001_a.sql"):
        (tmp_path / name).write_text("SELECT 1")
    assert [p.name for p in db.migration_files(tmp_path)] == [
        "0001_a.sql",
        "0002_b.sql",
        "0010_j.sql",
    ]


def test_failed_migration_names_the_file_and_leaves_it_unrecorded(pg, tmp_path, caplog):
    (tmp_path / "0001_fine.sql").write_text("CREATE TABLE probe_ok (x int)")
    (tmp_path / "0002_broken.sql").write_text("CREATE TABLE probe_bad (x NOT_A_TYPE)")
    with caplog.at_level("ERROR"), pytest.raises(psycopg.Error) as exc:
        db.migrate(tmp_path)

    # The operator must be able to read the filename AND the verbatim SQL error
    # out of the log; systemd Restart=on-failure otherwise hides it in a loop.
    assert "0002_broken.sql" in caplog.text
    assert "NOT_A_TYPE" in caplog.text or "NOT_A_TYPE" in str(exc.value)

    with db.tx() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        recorded = {r["version"] for r in cur.fetchall()}
        # Everything before the failure stuck; the failure itself did not.
        assert "0001_fine.sql" in recorded
        assert "0002_broken.sql" not in recorded
        cur.execute("SELECT to_regclass('public.probe_ok') AS t")
        assert cur.fetchone()["t"] is not None
        cur.execute("SELECT to_regclass('public.probe_bad') AS t")
        assert cur.fetchone()["t"] is None


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


def test_unreachable_database_fails_fast_with_an_actionable_error(monkeypatch):
    db.close_pool()
    monkeypatch.setenv("CHUDWARE_DB_URL", "postgresql://nobody@127.0.0.1:1/nope")
    monkeypatch.setenv("CHUDWARE_DB_CONNECT_TIMEOUT", "2")
    monkeypatch.setenv("CHUDWARE_DB_TIMEOUT", "2")
    with pytest.raises(db.DatabaseUnavailable) as exc:
        db.pool()
    assert "CHUDWARE_DB_URL" in str(exc.value)
    db.close_pool()


def test_dsn_password_is_redacted_before_logging():
    assert (
        db._redact("postgresql://user:hunter2@127.0.0.1:5432/x")
        == "postgresql://user:***@127.0.0.1:5432/x"
    )


# ---------------------------------------------------------------------------
# tx()
# ---------------------------------------------------------------------------


def test_tx_rolls_back_on_exception(pg):
    with pytest.raises(RuntimeError):
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO accounts (email, created_at) VALUES ('rb@x', 1)"
            )
            raise RuntimeError("boom")
    with db.tx() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM accounts")
        assert cur.fetchone()["n"] == 0


def test_nested_tx_joins_the_same_transaction(pg):
    with db.tx() as outer:
        with db.tx() as inner:
            assert inner is outer
            inner.execute(
                "INSERT INTO accounts (email, created_at) VALUES ('n@x', 1)"
            )
        # Still uncommitted: a separate connection cannot see it yet.
        with psycopg.connect(pg) as other:
            assert other.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    with db.tx() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM accounts")
        assert cur.fetchone()["n"] == 1


def test_nested_tx_failure_rolls_back_the_outer_write(pg):
    with pytest.raises(RuntimeError):
        with db.tx() as outer:
            outer.execute(
                "INSERT INTO accounts (email, created_at) VALUES ('o@x', 1)"
            )
            with db.tx() as inner:
                inner.execute(
                    "INSERT INTO accounts (email, created_at) VALUES ('i@x', 1)"
                )
                raise RuntimeError("boom")
    with db.tx() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM accounts")
        assert cur.fetchone()["n"] == 0


def test_leaked_nested_tx_is_detected_not_silently_committed(pg):
    """A nested tx() that escapes its `with` must not ride along to COMMIT."""
    with pytest.raises(RuntimeError, match="nested tx\\(\\) still open"):
        with db.tx() as _outer:
            leaked = db.tx()
            leaked.__enter__()  # never exited — the depth counter stays at 1


def test_concurrent_async_tasks_get_independent_transactions(pg):
    """Two overlapping webhook deliveries must NOT share one transaction.

    This is the test a ``threading.local`` implementation fails: every asyncio
    task runs on the same OS thread, so a thread-local slot would hand task B
    task A's cursor at the suspension point, and A's rollback would take B's
    committed entitlement with it. Each asyncio Task gets its own copy of the
    context, so a ContextVar keeps them apart.
    """

    async def deliver(event_id: str, email: str, plan: str, *, blow_up: bool) -> None:
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO processed_events (event_id, event_type, processed_at)"
                " VALUES (%s, 'test', 1) ON CONFLICT (event_id) DO NOTHING",
                (event_id,),
            )
            # Force a suspension INSIDE the transaction so the other task runs.
            await asyncio.sleep(0.05)
            with db.tx() as inner:  # a nested write, as accounts.set_plan does
                inner.execute(
                    "INSERT INTO accounts (email, created_at, plan)"
                    " VALUES (%s, 1, %s)",
                    (email, plan),
                )
            await asyncio.sleep(0.05)
            if blow_up:
                raise RuntimeError("A fails")

    async def run() -> None:
        results = await asyncio.gather(
            deliver("evt_a", "a@x", "desktop", blow_up=True),
            deliver("evt_b", "b@x", "studio", blow_up=False),
            return_exceptions=True,
        )
        assert isinstance(results[0], RuntimeError)
        assert results[1] is None

    asyncio.run(run())

    with db.tx() as cur:
        cur.execute("SELECT event_id FROM processed_events")
        assert {r["event_id"] for r in cur.fetchall()} == {"evt_b"}
        cur.execute("SELECT email, plan FROM accounts")
        # B's entitlement survived A's rollback, and A's did not leak through.
        assert [(r["email"], r["plan"]) for r in cur.fetchall()] == [("b@x", "studio")]
