"""Budget-meter credential reads (tools/flywheel/bench/spend_guard.py).

The bridge's auth store is WAL-mode and rotates the Anthropic access token
every few hours. Reading it with ``immutable=1`` silently skips the ``-wal``
file, so the guard hands the usage endpoint a long-expired token, gets HTTP
401, reports UNREADABLE and fail-safe-stops every live run.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tools.flywheel.bench import spend_guard


def _make_store(path):
    """WAL-mode auth store with one committed-but-uncheckpointed update.

    Returns the still-open writer connection: closing the last handle
    checkpoints the WAL, which would hide the bug the test is about.
    """
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE auth_credentials ("
        " provider TEXT, credential_type TEXT, disabled_cause TEXT,"
        " updated_at INTEGER, data TEXT)"
    )
    conn.execute(
        "INSERT INTO auth_credentials VALUES (?,?,?,?,?)",
        (
            "anthropic",
            "oauth",
            None,
            1,
            json.dumps({"access": "expired-token", "refresh": "r", "expires": 1}),
        ),
    )
    conn.commit()
    # Token rotation lands in the -wal file, not the main database file.
    conn.execute(
        "UPDATE auth_credentials SET data = ?, updated_at = 2 WHERE provider = 'anthropic'",
        (json.dumps({"access": "fresh-token", "refresh": "r", "expires": 9_999_999_999_999}),),
    )
    conn.commit()
    return conn


@pytest.fixture()
def store(tmp_path, monkeypatch):
    db = tmp_path / "omp-bridge-auth.db"
    conn = _make_store(db)
    monkeypatch.setattr(spend_guard, "bridge_auth_db", lambda: db)
    try:
        yield db
    finally:
        conn.close()


def test_rotated_token_in_the_wal_is_visible(store):
    cred = spend_guard._load_bridge_credential("anthropic")
    assert cred is not None
    assert cred["access_token"] == "fresh-token"


def test_missing_store_reads_as_no_credential(tmp_path, monkeypatch):
    monkeypatch.setattr(spend_guard, "bridge_auth_db", lambda: tmp_path / "absent.db")
    assert spend_guard._load_bridge_credential("anthropic") is None


def test_checkpointed_store_without_wal_still_reads(tmp_path, monkeypatch):
    """The immutable fallback must keep working for a quiesced store."""
    db = tmp_path / "omp-bridge-auth.db"
    conn = _make_store(db)
    conn.close()  # last handle out: WAL is checkpointed into the db file
    monkeypatch.setattr(spend_guard, "bridge_auth_db", lambda: db)
    cred = spend_guard._load_bridge_credential("anthropic")
    assert cred is not None and cred["access_token"] == "fresh-token"
