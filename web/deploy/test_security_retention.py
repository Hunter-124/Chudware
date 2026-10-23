from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("security_retention.py")
SPEC = importlib.util.spec_from_file_location("security_retention", MODULE_PATH)
assert SPEC and SPEC.loader
retention = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retention)


class Cursor:
    def __init__(self, counts: list[int], sql: list[str], params: list[tuple[int]]) -> None:
        self._counts = counts
        self._sql = sql
        self._params = params
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement: str, params: tuple[int]) -> None:
        self._sql.append(" ".join(statement.split()))
        self._params.append(params)
        self.rowcount = self._counts.pop(0)


class Connection:
    def __init__(self, counts: list[int]) -> None:
        self.counts = counts
        self.commits = 0
        self.sql: list[str] = []
        self.params: list[tuple[int]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return Cursor(self.counts, self.sql, self.params)

    def commit(self) -> None:
        self.commits += 1


def test_purge_commits_each_bounded_batch(monkeypatch):
    connection = Connection([3, 3, 1])
    monkeypatch.setattr(retention.psycopg, "connect", lambda dsn: connection)

    assert retention.purge_expired("postgresql:///test", 3) == 7
    assert connection.commits == 3
    assert connection.params == [(3,), (3,), (3,)]
    assert all("expires_at <= now()" in sql for sql in connection.sql)
    assert all("LIMIT %s FOR UPDATE SKIP LOCKED" in sql for sql in connection.sql)
    assert all("security_actions" not in sql and "security_appeals" not in sql for sql in connection.sql)


def test_batch_size_is_strictly_bounded(monkeypatch):
    monkeypatch.setenv("CHUDWARE_SECURITY_RETENTION_BATCH_SIZE", "0")
    with pytest.raises(ValueError):
        retention.batch_size()
    monkeypatch.setenv("CHUDWARE_SECURITY_RETENTION_BATCH_SIZE", "10001")
    with pytest.raises(ValueError):
        retention.batch_size()


def test_main_reports_row_count_only(monkeypatch, capsys):
    monkeypatch.setenv("CHUDWARE_DB_URL", "postgresql://user:secret@example/test")
    monkeypatch.setattr(retention, "purge_expired", lambda _dsn, _limit: 42)

    assert retention.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "deleted_security_batches=42\n"
    assert captured.err == ""
    assert "secret" not in captured.out


def test_main_surfaces_failure_without_dsn(monkeypatch, capsys):
    monkeypatch.delenv("CHUDWARE_DB_URL", raising=False)
    assert retention.main() == 2
    assert "CHUDWARE_DB_URL is required" in capsys.readouterr().err

def test_staging_retention_rejects_foreign_database_before_connecting(monkeypatch, capsys):
    monkeypatch.setenv("CHUDWARE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("CHUDWARE_DB_URL", "postgresql://chudware_prod:fixture@localhost/chudware_prod")
    monkeypatch.setattr(retention.psycopg, "connect", lambda *_: pytest.fail("foreign database must not be contacted"))
    assert retention.main() == 1
    assert "fixture" not in capsys.readouterr().err
