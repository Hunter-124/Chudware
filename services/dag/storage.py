"""Actor-owned persisted DAG handle mapping; caller IDs never name cell files."""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from services.agent.request_context import actor_state_root, owner_key, require_owner
from .run_ids import validate_run_id


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    root: Path
    owner: tuple[str, str]
    autonomy: str

    @property
    def db(self) -> Path:
        return self.root / "checkpoint.db"

    @property
    def graph(self) -> Path:
        return self.root / "graph.json"


def private_runs_dir() -> Path:
    owner_key()  # fail closed before even resolving a directory
    return actor_state_root() / "dag"


def _connect(*, create: bool = False) -> sqlite3.Connection:
    base = private_runs_dir()
    if create:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = base / "handles.sqlite3"
    if not create and not path.exists():
        raise KeyError("not_found")
    connection = sqlite3.connect(str(path) if create else f"{path.as_uri()}?mode=rw", uri=not create)
    if create:
        connection.execute("CREATE TABLE IF NOT EXISTS runs (slot INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, handle TEXT UNIQUE, workspace TEXT NOT NULL, actor TEXT NOT NULL, autonomy TEXT NOT NULL)")
    return connection


def _record(row: tuple) -> RunRecord:
    slot, token, _, workspace, actor, autonomy = row
    require_owner((workspace, actor))
    return RunRecord(token, private_runs_dir() / "records" / str(slot), (workspace, actor), autonomy)


def create_run(handle: str | None = None, autonomy: str = "auto") -> RunRecord:
    if handle is not None:
        validate_run_id(handle)
    owner = owner_key()
    if owner is None:
        raise ValueError("cell_refused")
    token = secrets.token_hex(24)
    connection = _connect(create=True)
    try:
        with connection:
            # Reject existing aliases rather than overwriting a live checkpoint.
            if handle is not None and connection.execute("SELECT 1 FROM runs WHERE token=? OR handle=?", (handle, handle)).fetchone():
                raise ValueError("run already exists")
            cursor = connection.execute("INSERT INTO runs(token,handle,workspace,actor,autonomy) VALUES (?,?,?,?,?)", (token, handle, *owner, autonomy))
            row = connection.execute("SELECT * FROM runs WHERE slot=?", (cursor.lastrowid,)).fetchone()
        record = _record(row)
        record.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return record
    finally:
        connection.close()


def resolve_run(handle: str) -> RunRecord:
    try:
        validate_run_id(handle)
    except ValueError:
        raise KeyError("not_found") from None
    connection = _connect()
    try:
        row = connection.execute("SELECT * FROM runs WHERE token=? OR handle=?", (handle, handle)).fetchone()
        if row is None:
            raise KeyError("not_found")
        return _record(row)
    finally:
        connection.close()


def list_run_ids(limit: int = 5) -> list[str]:
    try:
        connection = _connect()
    except KeyError:
        return []
    try:
        owner = owner_key()
        rows = connection.execute("SELECT token FROM runs WHERE workspace=? AND actor=? ORDER BY slot DESC LIMIT ?", (*owner, limit)).fetchall()
        return [row[0] for row in rows]
    finally:
        connection.close()
