"""Test database: a real PostgreSQL, never a fake.

There is deliberately no SQLite fallback and no stub cursor. A fake would let a
``%s``/``?`` placeholder slip, an ``ON CONFLICT`` clause go untested, or a
transaction-semantics bug pass green here and fail only in production — which
is precisely the class of bug the port exists to avoid.

By default the session spins up its own throwaway cluster with ``initdb`` into
a temporary directory, listening on a unix socket inside that directory: no
TCP port to collide with a parallel CI job, no daemon to install, no root. Set
``CHUDWARE_TEST_DB_URL`` to point at an existing server instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))


@pytest.fixture()
def reload_settings(monkeypatch):
    """Re-read ``Settings`` from the current environment WITHOUT replacing the
    singleton, and undo it after the test.

    ``auth``, ``accounts``, ``limits`` and friends all do ``from config import
    settings`` at import time, so they hold a REFERENCE to one object. Fixtures
    that instead did ``config.settings = config.load_settings()`` left whichever
    module imported first reading the discarded object — and which module that is
    depends purely on pytest's file order. Every symptom of that has been an
    order-dependent result rather than an honest one: ``test_auth_accounts_gate``
    failing "no strong auth configured" only when ``test_accounts`` ran first, and
    ``test_chat``'s WS broadcast blocking forever because ``AuthMiddleware`` hit
    that same fail-fast inside the message loop and no frame was ever sent.

    Mutating field-by-field through ``monkeypatch`` keeps one object for everyone
    and restores each value at teardown, so a later module is never handed a
    settings object shaped by an earlier one.
    """
    import dataclasses  # noqa: PLC0415

    def _reload():
        import config  # noqa: PLC0415

        fresh = config.load_settings()
        for field in dataclasses.fields(fresh):
            monkeypatch.setattr(
                config.settings, field.name, getattr(fresh, field.name), raising=False
            )
        return config.settings

    return _reload


# Fedora installs the server binaries in /usr/bin, but only from
# postgresql-server (the client package gives psql and no initdb); versioned
# side-by-side installs land under /usr/pgsql-NN/bin or
# /usr/lib64/pgsql/postgresql-NN/bin; Homebrew uses the two brew prefixes.
_BIN_GLOBS = (
    "/usr/pgsql-*/bin",
    "/usr/lib64/pgsql/postgresql-*/bin",
    "/opt/homebrew/opt/postgresql@*/bin",
    "/usr/local/opt/postgresql@*/bin",
)
_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

_INSTALL_HINT = """PostgreSQL server binaries not found: the gateway test suite runs against a
real database and needs `initdb` and `postgres`, not just `psql`.

  Fedora/RHEL : sudo dnf install postgresql16-server   (the plain `postgresql`
                package is client-only and does NOT ship initdb)
  Debian      : sudo apt install postgresql
  macOS       : brew install postgresql@16

Or point the suite at a server you already have:

  CHUDWARE_TEST_DB_URL=postgresql://user:pass@127.0.0.1:5432/scratch pytest tests

Or name the directory holding the binaries with CHUDWARE_TEST_PG_BIN."""


def _pg_bin_dir() -> Path:
    """Directory containing initdb + postgres, searched widest-net first."""
    explicit = (os.environ.get("CHUDWARE_TEST_PG_BIN") or "").strip()
    if explicit:
        d = Path(explicit)
        if (d / "initdb").exists():
            return d
        raise RuntimeError(f"CHUDWARE_TEST_PG_BIN={explicit} has no initdb\n\n{_INSTALL_HINT}")

    found = shutil.which("initdb")
    if found and (Path(found).parent / "postgres").exists():
        return Path(found).parent

    candidates: list[Path] = []
    for pattern in _BIN_GLOBS:
        candidates.extend(Path("/").glob(pattern.lstrip("/")))
    candidates.extend(Path(d) for d in _BIN_DIRS)
    # Newest version first: "postgresql@16" sorts after "postgresql@9".
    for d in sorted(candidates, reverse=True):
        if (d / "initdb").exists() and (d / "postgres").exists():
            return d
    raise RuntimeError(_INSTALL_HINT)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[str]:
    """A PostgreSQL instance for the whole session. Yields a base DSN."""
    external = (os.environ.get("CHUDWARE_TEST_DB_URL") or "").strip()
    if external:
        yield external
        return

    bindir = _pg_bin_dir()
    # Short path: the unix socket path has a hard ~107 byte limit, and pytest's
    # tmp_path_factory roots are long enough to blow it.
    root = Path(tempfile.mkdtemp(prefix="chwpg-"))
    datadir = root / "d"
    subprocess.run(
        [
            str(bindir / "initdb"),
            "-D", str(datadir),
            "-U", "postgres",
            "-A", "trust",           # socket-only, inside a 0700 temp dir
            "-E", "UTF8",
            "--locale=C",
            "--no-sync",             # throwaway cluster; durability is noise
        ],
        check=True,
        capture_output=True,
    )
    logfile = root / "postgres.log"
    subprocess.run(
        [
            str(bindir / "pg_ctl"),
            "-D", str(datadir),
            "-l", str(logfile),
            "-w",
            "-o",
            # No TCP listener at all: nothing to collide with a parallel job.
            f"-k {root} -h '' -F -c full_page_writes=off -c synchronous_commit=off",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql://postgres@/postgres?host={root}"
    finally:
        subprocess.run(
            [str(bindir / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "stop"],
            capture_output=True,
        )
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def pg(pg_server: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A virgin schema with every migration applied. Yields the DSN.

    Autouse, deliberately. Half the suite reaches the store indirectly — an
    admin route that renders a pending count, an auth middleware that calls
    ensure_account — and requiring each of those to remember an opt-in fixture
    would leave tests silently pointed at whatever CHUDWARE_DB_URL happens to
    be set on the machine. Which, on the production host, is production.

    Resetting the schema rather than creating a database per test keeps this at
    a few milliseconds; the migrations are small and the isolation is identical
    because nothing in this schema lives outside ``public``.
    """
    import psycopg

    import db  # noqa: PLC0415 - after sys.path is set up above

    monkeypatch.setenv("CHUDWARE_DB_URL", pg_server)
    db.close_pool()
    with psycopg.connect(pg_server, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    db.migrate()
    try:
        yield pg_server
    finally:
        # psycopg's pool runs worker threads; leaving them alive across the
        # session trips PythonFinalizationError at interpreter exit.
        db.close_pool()
