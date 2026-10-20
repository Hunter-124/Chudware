"""Trusted hostd request identity; never derive tenancy from tool parameters."""
from __future__ import annotations

import ast
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Mapping
from typing import Any, Iterator
from pathlib import Path

_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("chudware_request_context", default=None)
_ROOTS: ContextVar[dict[str, str]] = ContextVar("chudware_request_roots", default={})


def current_context() -> dict[str, Any]:
    return dict(_CONTEXT.get() or {})


def in_cell() -> bool:
    return bool((_CONTEXT.get() or {}).get("cell")) or os.environ.get("CHUDWARE_CELL") == "1"


def process_owner_key() -> tuple[str, str]:
    """Actor identity fixed by the trusted launcher, available before requests."""
    workspace = os.environ.get("CHUDWARE_WORKSPACE_KEY", "")
    actor = os.environ.get("CHUDWARE_ACTOR_KEY", "")
    actor_id = os.environ.get("CHUDWARE_ACTOR_HEX", "")
    if (os.environ.get("CHUDWARE_CELL") != "1"
            or os.environ.get("CHUDWARE_ACTOR_SANDBOX") != "1"
            or not workspace or not actor
            or actor_id != hashlib.sha256(actor.encode()).hexdigest()[:32]):
        raise ValueError("cell_refused")
    return workspace, actor


def process_runtime_root() -> Path:
    process_owner_key()
    return Path("/actor/run")

def owner_key() -> tuple[str, str] | None:
    ctx = _CONTEXT.get() or {}
    if not ctx.get("cell"):
        if in_cell():
            raise ValueError("cell_refused")
        return None
    return str(ctx["workspace_key"]), str(ctx["actor_key"])


def owns(owner: Any) -> bool:
    return owner == owner_key()


def require_owner(owner: Any) -> None:
    if not owns(owner):
        raise KeyError("not_found")


def scoped_key(handle: str) -> str:
    """A registry key, NOT a filesystem name; desktop keys remain unchanged."""
    owner = owner_key()
    return handle if owner is None else repr((owner, handle))



def owns_registry_key(key: str) -> bool:
    """Inspect an existing scoped in-memory key without resolving an object."""
    owner = owner_key()
    if owner is None:
        return True
    value = ast.literal_eval(key)
    return isinstance(value, tuple) and len(value) == 2 and value[0] == owner

def actor_hex() -> str:
    ctx = _CONTEXT.get() or {}
    owner_key()  # refuse a cell without its authenticated envelope
    return str(ctx.get("actor_hex") or "")



def control_delegate(action: str) -> dict[str, str] | None:
    delegate = current_context().get("control_delegate")
    if delegate is None:
        return None
    if (not in_cell() or action != "cancel" or not isinstance(delegate, Mapping)
            or set(delegate) != {"actor_key", "right", "action"}
            or not isinstance(delegate.get("actor_key"), str) or not delegate["actor_key"]
            or delegate.get("right") != "manager" or delegate.get("action") != "cancel"):
        raise ValueError("forbidden_right")
    return dict(delegate)

@contextmanager
def bind_context(ctx: Mapping[str, Any] | None) -> Iterator[None]:
    value = dict(ctx) if isinstance(ctx, Mapping) else None
    if value and value.get("cell"):
        if (not all(isinstance(value.get(k), str) and value[k] for k in ("workspace_key", "actor_key", "actor_hex"))
                or value["actor_hex"] != hashlib.sha256(value["actor_key"].encode()).hexdigest()[:32]
                or value.get("right") not in {"owner", "manager", "editor", "viewer"}
                or type(value.get("epoch")) is not int):
            raise ValueError("cell_refused")
        if os.environ.get("CHUDWARE_ACTOR_SANDBOX") == "1":
            if (value["workspace_key"], value["actor_key"]) != process_owner_key():
                raise ValueError("cell_refused")
        if "control_delegate" in value:
            delegate = value["control_delegate"]
            if (not isinstance(delegate, Mapping)
                    or set(delegate) != {"actor_key", "right", "action"}
                    or not isinstance(delegate.get("actor_key"), str) or not delegate["actor_key"]
                    or delegate.get("right") != "manager" or delegate.get("action") != "cancel"):
                raise ValueError("forbidden_right")
            value["control_delegate"] = dict(delegate)
    elif os.environ.get("CHUDWARE_CELL") == "1":
        raise ValueError("cell_refused")
    token = _CONTEXT.set(value)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


@contextmanager
def bind_roots(project_root: str, save_dir: str = "") -> Iterator[None]:
    roots = ({"CHUDWARE_FS_ROOT": project_root, "CHUDWARE_PROJECT_DIR": project_root,
              "CHUDWARE_PROJECT_SAVE_DIR": save_dir or project_root}
             if project_root else dict(_ROOTS.get()))
    token = _ROOTS.set(roots)
    try:
        yield
    finally:
        _ROOTS.reset(token)


def root_value(name: str, default: str = "") -> str:
    roots = _ROOTS.get()
    if name in roots:
        return roots[name]
    return os.environ.get(name, default)


def actor_state_root() -> Path:
    """Private sandbox state; the launcher, never a request, selects its base."""
    owner = owner_key() if (_CONTEXT.get() or {}).get("cell") else process_owner_key()
    if owner is None:
        raise ValueError("cell_refused")
    base = Path(os.environ.get("XDG_DATA_HOME", "/actor/data"))
    # Separate actors even in in-process registry fixtures. The namespace exposes
    # only this actor's base; this digest is not an object handle.
    return base / hashlib.sha256(repr(owner).encode()).hexdigest()[:32]


def cell_path(raw: str, *, private: bool = False) -> Path:
    """Resolve a cell path beneath its namespace roots, including symlink targets."""
    if not in_cell():
        return Path(raw).expanduser().resolve()
    owner_key()
    roots = [Path("/work")]
    if private:
        roots.append(Path("/actor"))
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    resolved = candidate.resolve()
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError("cell_refused")
    return resolved


def sandbox_argv(argv: list[str], mode: str = "rw") -> list[str]:
    if not in_cell():
        return argv
    if mode not in {"rw", "ro"}:
        raise ValueError("cell_refused")
    return ["/opt/chudware/bin/chudware-actor-sandbox", actor_hex(), mode, "--", *argv]


@contextmanager
def _handles_db() -> Iterator[sqlite3.Connection]:
    root = actor_state_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(root / "handles.sqlite")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS handles (handle TEXT PRIMARY KEY, kind TEXT NOT NULL, workspace TEXT NOT NULL, actor TEXT NOT NULL, parent TEXT NOT NULL)")
        with db:
            yield db
    finally:
        db.close()


def mint_handle(kind: str, parent: str = "") -> str:
    owner = owner_key()
    if owner is None or kind not in {"session", "run"}:
        raise ValueError("cell_refused")
    handle = secrets.token_urlsafe(32)
    with _handles_db() as db:
        db.execute("INSERT INTO handles VALUES (?, ?, ?, ?, ?)", (handle, kind, *owner, parent))
    return handle


def require_handle(handle: str, kind: str, parent: str | None = None) -> None:
    if not in_cell():
        return
    owner = owner_key()
    with _handles_db() as db:
        row = db.execute("SELECT parent FROM handles WHERE handle=? AND kind=? AND workspace=? AND actor=?", (handle, kind, *owner)).fetchone()
    if row is None or (parent is not None and row[0] != parent):
        raise KeyError("not_found")
