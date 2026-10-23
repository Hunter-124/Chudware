#!/usr/bin/env python3
"""Delete expired detailed security batches in small committed transactions."""
from __future__ import annotations

import os
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))

import psycopg

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 10_000


def batch_size() -> int:
    raw = os.environ.get("CHUDWARE_SECURITY_RETENTION_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("CHUDWARE_SECURITY_RETENTION_BATCH_SIZE must be an integer") from exc
    if not 1 <= value <= MAX_BATCH_SIZE:
        raise ValueError(
            f"CHUDWARE_SECURITY_RETENTION_BATCH_SIZE must be between 1 and {MAX_BATCH_SIZE}"
        )
    return value


def purge_expired(dsn: str, limit: int) -> int:
    """Delete complete expired batches, committing after at most ``limit`` rows."""
    deleted = 0
    with psycopg.connect(dsn) as conn:
        while True:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM security_event_batches
                    WHERE batch_id IN (
                        SELECT batch_id
                        FROM security_event_batches
                        WHERE expires_at <= now()
                        ORDER BY expires_at, batch_id
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    """,
                    (limit,),
                )
                count = cur.rowcount
            conn.commit()
            deleted += count
            if count < limit:
                return deleted


def main() -> int:
    dsn = (os.environ.get("CHUDWARE_DB_URL") or "").strip()
    if not dsn:
        print("security retention failed: CHUDWARE_DB_URL is required", file=sys.stderr)
        return 2
    try:
        import db
        deleted = purge_expired(db.dsn(), batch_size())
    except Exception as exc:  # systemd/journal must expose failure without secrets
        print(f"security retention failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"deleted_security_batches={deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
