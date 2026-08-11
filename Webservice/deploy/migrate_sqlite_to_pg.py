#!/usr/bin/env python3
"""One-shot cutover: copy the live SQLite stores into PostgreSQL.

Run ONCE, with the gateway stopped, after the schema exists:

    systemctl --user stop chudware-gateway
    cd ~/Repositories/CAD/Webservice/gateway
    CHUDWARE_DB_URL="postgresql://chudware_prod:...@127.0.0.1/chudware_prod" \\
        .venv/bin/python ../deploy/migrate_sqlite_to_pg.py --data-root ~/chudware-web-data
    systemctl --user start chudware-gateway

Idempotent: every insert is ``ON CONFLICT DO NOTHING`` keyed on the natural
primary key, so a re-run after a partial failure resumes instead of duplicating.
It never UPDATEs — if a row already exists in Postgres, Postgres wins, because
by then the gateway may have written something newer than the SQLite snapshot.

Reads the SQLite files read-only (``mode=ro``) so a botched run cannot damage
the source of truth you still need to fall back to.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1] / "gateway"
sys.path.insert(0, str(GATEWAY_DIR))

import db  # noqa: E402  (after sys.path)

ACCOUNT_COLUMNS = (
    "email",
    "display",
    "status",
    "plan",
    "role",
    "created_at",
    "decided_at",
    "decided_by",
    "stripe_customer_id",
    "notes",
    "heavy",
)


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        print(f"  {path.name}: absent, nothing to import")
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def import_accounts(path: Path) -> tuple[int, int]:
    conn = _open_ro(path)
    if conn is None or not _table_exists(conn, "accounts"):
        return (0, 0)
    try:
        rows = conn.execute(f"SELECT {', '.join(ACCOUNT_COLUMNS)} FROM accounts").fetchall()
    finally:
        conn.close()
    inserted = 0
    with db.tx() as cur:
        for row in rows:
            cur.execute(
                f"""
                INSERT INTO accounts ({', '.join(ACCOUNT_COLUMNS)})
                VALUES ({', '.join(['%s'] * len(ACCOUNT_COLUMNS))})
                ON CONFLICT (email) DO NOTHING
                """,
                (
                    row["email"],
                    row["display"],
                    row["status"],
                    row["plan"],
                    row["role"],
                    float(row["created_at"]),
                    None if row["decided_at"] is None else float(row["decided_at"]),
                    row["decided_by"],
                    row["stripe_customer_id"],
                    row["notes"],
                    bool(row["heavy"]),
                ),
            )
            inserted += cur.rowcount
    return (inserted, len(rows))


def import_chat(path: Path) -> tuple[int, int]:
    conn = _open_ro(path)
    if conn is None or not _table_exists(conn, "messages"):
        return (0, 0)
    try:
        rows = conn.execute(
            "SELECT id, ts, email, display, text FROM messages ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    inserted = 0
    with db.tx() as cur:
        for row in rows:
            # Keep the original ids: history is rendered oldest-first by id and
            # the web client dedupes on it.
            cur.execute(
                "INSERT INTO messages (id, ts, email, display, text)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    int(row["id"]),
                    float(row["ts"]),
                    row["email"],
                    row["display"],
                    row["text"],
                ),
            )
            inserted += cur.rowcount
        # BIGSERIAL would otherwise hand out id 1 again and collide.
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('messages', 'id'),"
            " COALESCE((SELECT MAX(id) FROM messages), 1))"
        )
    return (inserted, len(rows))


def import_events(path: Path) -> tuple[int, int]:
    conn = _open_ro(path)
    if conn is None or not _table_exists(conn, "processed_events"):
        return (0, 0)
    try:
        rows = conn.execute(
            "SELECT event_id, event_type, processed_at FROM processed_events"
        ).fetchall()
    finally:
        conn.close()
    inserted = 0
    with db.tx() as cur:
        for row in rows:
            cur.execute(
                "INSERT INTO processed_events (event_id, event_type, processed_at)"
                " VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
                (row["event_id"], row["event_type"], float(row["processed_at"])),
            )
            inserted += cur.rowcount
    return (inserted, len(rows))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--data-root",
        type=Path,
        default=Path("~/chudware-web-data").expanduser(),
        help="directory holding accounts.db / chat.db / billing_webhook_events.sqlite3",
    )
    args = ap.parse_args(argv)
    root = args.data_root.expanduser()

    print(f"source: {root}")
    print(f"target: {db._redact(db.dsn())}")
    # Schema first — this is also the fail-fast check that the DSN is real.
    db.init()

    total_new = 0
    for label, fn, filename in (
        ("accounts", import_accounts, "accounts.db"),
        ("chat", import_chat, "chat.db"),
        ("webhook events", import_events, "billing_webhook_events.sqlite3"),
    ):
        new, seen = fn(root / filename)
        total_new += new
        print(f"  {label}: {new} inserted, {seen - new} already present")

    with db.tx() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM accounts")
        accounts_n = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM messages")
        messages_n = cur.fetchone()["n"]
    print(f"postgres now holds {accounts_n} account(s), {messages_n} message(s)")
    print(f"{total_new} row(s) imported")
    db.close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
