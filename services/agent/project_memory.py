"""Project-scoped agent memory — SQLite + FTS5 under each PLM project.

Store path: ``{project_root}/.plm/memory.db`` (auto-created). No external deps.

Designed to be cheap on tokens: callers pass a token budget; ``recall`` returns
only top-k hits that fit. Never dump the whole DB into the model context.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'note',
    source TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,
    created REAL NOT NULL,
    accessed REAL NOT NULL,
    content_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    kind,
    source,
    content='memories',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content, kind, source)
  VALUES (new.id, new.content, new.kind, new.source);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, kind, source)
  VALUES ('delete', old.id, old.content, old.kind, old.source);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, kind, source)
  VALUES ('delete', old.id, old.content, old.kind, old.source);
  INSERT INTO memories_fts(rowid, content, kind, source)
  VALUES (new.id, new.content, new.kind, new.source);
END;
"""

_lock = threading.RLock()
_conns: dict[str, sqlite3.Connection] = {}

_WS_RE = re.compile(r"\s+")


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def memory_db_path(project_root: str | Path) -> Path:
    root = Path(project_root).expanduser().resolve()
    return root / ".plm" / "memory.db"


def _normalize_content(content: str) -> str:
    return _WS_RE.sub(" ", (content or "").strip())


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


def _connect(project_root: str | Path) -> sqlite3.Connection:
    path = memory_db_path(project_root)
    key = str(path)
    with _lock:
        conn = _conns.get(key)
        if conn is not None:
            return conn
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        conn.executescript(_SCHEMA)
        conn.commit()
        _conns[key] = conn
        return conn


def close_all() -> None:
    """Close cached connections (tests / shutdown)."""
    with _lock:
        for c in _conns.values():
            try:
                c.close()
            except sqlite3.Error:
                pass
        _conns.clear()


# --------------------------------------------------------------------------- OMP bridge hybrid backend
# Optional: recall/remember/stats can route through the OMP bridge's mnemopi-backed
# memory_* RPCs (real reuse of vendor/omp/packages/mnemopi — see
# OMP_INTEGRATION_PLAN.md §RPC CONTRACT, docs/omp-integration/01-DECISIONS.md §Memory)
# instead of the SQLite+FTS5 path below. Off by default (unchanged behavior); opt in
# with env AI_CAD_MEMORY_BACKEND=mnemopi or config.json agent.memory_backend="mnemopi".
# Any bridge failure (bun/script missing, crash, timeout, RPC error) raises
# omp_bridge.BridgeUnavailable, caught here to fall back to SQLite+FTS5 transparently.

def _bridge():
    from services.agent import omp_bridge  # noqa: PLC0415
    return omp_bridge


def _memory_backend(cfg: dict | None = None) -> str:
    """``"sqlite"`` (default) or ``"mnemopi"``. Env wins over config.json."""
    env = (os.environ.get("AI_CAD_MEMORY_BACKEND") or "").strip().lower()
    if env in ("mnemopi", "omp", "bridge"):
        return "mnemopi"
    if env:
        return "sqlite"
    try:
        from services.agent.config import load_config  # noqa: PLC0415
        c = cfg if cfg is not None else load_config()
        agent = c.get("agent") if isinstance(c, dict) else None
        backend = agent.get("memory_backend") if isinstance(agent, dict) else None
        if isinstance(backend, str) and backend.strip().lower() in ("mnemopi", "omp", "bridge"):
            return "mnemopi"
    except Exception:  # noqa: BLE001 — bad/missing config never breaks memory
        pass
    return "sqlite"


def _stable_int_id(raw_id: Any) -> int:
    """Deterministic non-negative int from a bridge-returned memory id.

    mnemopi memory ids are opaque strings (vendor/omp/packages/mnemopi/src/
    types.ts ``RecallResult.id``); this module's public API returns
    ``int | None`` (SQLite ``rowid``), so bridge ids are hashed into a stable
    surrogate int. Same string always hashes to the same int, so remember()'s
    "identical content -> identical id" dedupe contract still holds.
    """
    if isinstance(raw_id, int):
        return raw_id
    if isinstance(raw_id, str) and raw_id.isdigit():
        return int(raw_id)
    digest = hashlib.sha256(str(raw_id).encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big")


def _bridge_remember(bridge, project_root: str | Path, text: str, kind: str, source: str,
                      importance: float) -> int | None:
    """``remember`` via bridge ``memory_remember`` (mnemopi, real OMP reuse)."""
    root = str(Path(project_root).expanduser().resolve())
    result = bridge.call(
        "memory_remember",
        {"root": root, "content": text, "kind": kind, "source": source, "importance": importance},
    )
    raw_id = result.get("id", result.get("value"))
    return None if raw_id is None else _stable_int_id(raw_id)


def _bridge_recall(bridge, project_root: str | Path, query: str, limit: int, token_budget: int,
                    kind: str | None) -> list[dict[str, Any]]:
    """``recall`` via bridge ``memory_recall``; maps mnemopi ``RecallResult`` fields
    (vendor/omp/packages/mnemopi/src/types.ts) onto this module's hit shape. The
    bridge is handed ``tokenBudget`` and trusted to honor it server-side.
    """
    root = str(Path(project_root).expanduser().resolve())
    params: dict[str, Any] = {"root": root, "query": query, "limit": limit, "tokenBudget": token_budget}
    if kind:
        params["kind"] = kind
    result = bridge.call("memory_recall", params)
    hits = result.get("hits", result.get("value"))
    if not isinstance(hits, list):
        return []
    out: list[dict[str, Any]] = []
    for h in hits[:limit]:
        if not isinstance(h, dict):
            continue
        content = str(h.get("content") or "")
        try:
            tok = int(h["tokens"]) if h.get("tokens") is not None else estimate_tokens(content)
        except (TypeError, ValueError):
            tok = estimate_tokens(content)
        try:
            importance = float(h.get("importance") or 0.5)
        except (TypeError, ValueError):
            importance = 0.5
        try:
            score = float(h.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        raw_id = h.get("id")
        out.append({
            "id": _stable_int_id(raw_id) if raw_id is not None else 0,
            "content": content,
            "kind": str(h.get("kind") or h.get("memory_type") or h.get("memoryType") or "note"),
            "source": str(h.get("source") or ""),
            "importance": importance,
            "score": score,
            "tokens": tok,
        })
    return out


def _bridge_stats(bridge, project_root: str | Path) -> dict[str, Any]:
    """``stats`` via bridge ``memory_stats``."""
    root = str(Path(project_root).expanduser().resolve())
    result = bridge.call("memory_stats", {"root": root})
    path = str(result.get("path") or result.get("database") or memory_db_path(project_root))
    try:
        count = int(result.get("count", result.get("total_memories", 0)) or 0)
    except (TypeError, ValueError):
        count = 0
    exists = result.get("exists")
    exists = bool(exists) if exists is not None else count > 0
    out: dict[str, Any] = {"path": path, "count": count, "exists": exists}
    if exists:
        kinds = result.get("kinds")
        out["kinds"] = kinds if isinstance(kinds, dict) else {}
    return out


def remember(
    project_root: str | Path,
    content: str,
    *,
    kind: str = "note",
    source: str = "",
    importance: float = 0.5,
) -> int | None:
    """Insert a memory. Dedupes identical content (returns existing id). None if empty.

    Hybrid backend: routes through the OMP bridge (mnemopi) when
    ``AI_CAD_MEMORY_BACKEND=mnemopi`` / ``agent.memory_backend`` is set, falling
    back to SQLite+FTS5 below when the bridge is unavailable. Same signature and
    return shape either way.
    """
    text = _normalize_content(content)
    if not text:
        return None
    if len(text) > 4000:
        text = text[:3999].rstrip() + "…"
    kind = (kind or "note").strip()[:64] or "note"
    source = (source or "").strip()[:256]
    try:
        imp = float(importance)
    except (TypeError, ValueError):
        imp = 0.5
    imp = max(0.0, min(1.0, imp))

    if _memory_backend() == "mnemopi":
        bridge = _bridge()
        try:
            return _bridge_remember(bridge, project_root, text, kind, source, imp)
        except bridge.BridgeUnavailable:
            pass  # OMP bridge unavailable — fall back to SQLite+FTS5 below
    now = time.time()
    h = _content_hash(text)
    conn = _connect(project_root)
    with _lock:
        row = conn.execute(
            "SELECT id FROM memories WHERE content_hash = ?", (h,)
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE memories SET accessed = ?, importance = MAX(importance, ?) WHERE id = ?",
                (now, imp, int(row["id"])),
            )
            conn.commit()
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO memories (content, kind, source, importance, created, accessed, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (text, kind, source, imp, now, now, h),
        )
        conn.commit()
        return int(cur.lastrowid)


def _fts_query(raw: str) -> str:
    """Build a safe FTS5 query from free text (OR of quoted tokens)."""
    toks = re.findall(r"[A-Za-z0-9_./+-]{2,}", raw or "")
    if not toks:
        return ""
    # Cap token count; quote each to avoid FTS operators.
    parts = []
    for t in toks[:12]:
        safe = t.replace('"', "")
        if safe:
            parts.append(f'"{safe}"')
    return " OR ".join(parts)


def recall(
    project_root: str | Path,
    query: str,
    *,
    limit: int = 5,
    token_budget: int = 400,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    """Return top memories matching *query* within *token_budget*.

    Each item: ``{id, content, kind, source, importance, score, tokens}``.
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 5
    try:
        token_budget = max(50, min(4000, int(token_budget)))
    except (TypeError, ValueError):
        token_budget = 400

    if _memory_backend() == "mnemopi":
        bridge = _bridge()
        try:
            return _bridge_recall(bridge, project_root, q, limit, token_budget, kind)
        except bridge.BridgeUnavailable:
            pass  # OMP bridge unavailable — fall back to SQLite+FTS5 below

    path = memory_db_path(project_root)
    if not path.is_file():
        return []

    fts_q = _fts_query(q)
    conn = _connect(project_root)
    rows: list[sqlite3.Row] = []
    with _lock:
        try:
            if fts_q:
                sql = (
                    "SELECT m.id, m.content, m.kind, m.source, m.importance, "
                    "bm25(memories_fts) AS score "
                    "FROM memories_fts "
                    "JOIN memories m ON m.id = memories_fts.rowid "
                    "WHERE memories_fts MATCH ? "
                )
                args: list[Any] = [fts_q]
                if kind:
                    sql += "AND m.kind = ? "
                    args.append(kind)
                sql += "ORDER BY score LIMIT ?"
                args.append(limit * 3)  # over-fetch then budget-trim
                rows = list(conn.execute(sql, args).fetchall())
            if not rows:
                # Fallback: recent high-importance notes (no match).
                sql = (
                    "SELECT id, content, kind, source, importance, "
                    "(-importance) AS score FROM memories "
                )
                args = []
                if kind:
                    sql += "WHERE kind = ? "
                    args.append(kind)
                sql += "ORDER BY importance DESC, accessed DESC LIMIT ?"
                args.append(limit)
                rows = list(conn.execute(sql, args).fetchall())
            # Touch accessed for returned candidates (before budget trim uses same set).
            ids = [int(r["id"]) for r in rows[:limit]]
            if ids:
                now = time.time()
                conn.executemany(
                    "UPDATE memories SET accessed = ? WHERE id = ?",
                    [(now, i) for i in ids],
                )
                conn.commit()
        except sqlite3.Error:
            return []

    out: list[dict[str, Any]] = []
    used = 0
    for r in rows:
        content = str(r["content"] or "")
        tok = estimate_tokens(content)
        if out and used + tok > token_budget:
            continue
        if not out and tok > token_budget:
            # Always allow one truncated hit so recall is never empty when DB has data.
            content = content[: max(32, token_budget * 4 - 1)].rstrip() + "…"
            tok = estimate_tokens(content)
        item = {
            "id": int(r["id"]),
            "content": content,
            "kind": str(r["kind"] or "note"),
            "source": str(r["source"] or ""),
            "importance": float(r["importance"] or 0.5),
            "score": float(r["score"] or 0.0),
            "tokens": tok,
        }
        out.append(item)
        used += tok
        if len(out) >= limit or used >= token_budget:
            break
    return out


def format_for_context(hits: list[dict[str, Any]], *, header: str = "Project memory") -> str:
    """Render recall hits as a compact context block (empty string if none)."""
    if not hits:
        return ""
    lines = [f"[{header} — use when relevant; do not repeat verbatim unless needed]"]
    for i, h in enumerate(hits, 1):
        kind = h.get("kind") or "note"
        body = (h.get("content") or "").replace("\n", " ").strip()
        lines.append(f"{i}. ({kind}) {body}")
    return "\n".join(lines)


def stats(project_root: str | Path) -> dict[str, Any]:
    """Return basic counts for Settings / debug."""
    if _memory_backend() == "mnemopi":
        bridge = _bridge()
        try:
            return _bridge_stats(bridge, project_root)
        except bridge.BridgeUnavailable:
            pass  # OMP bridge unavailable — fall back to SQLite+FTS5 below
    path = memory_db_path(project_root)
    if not path.is_file():
        return {"path": str(path), "count": 0, "exists": False}
    conn = _connect(project_root)
    with _lock:
        try:
            n = int(conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
            kinds = {
                str(r["kind"]): int(r["n"])
                for r in conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM memories GROUP BY kind"
                ).fetchall()
            }
        except sqlite3.Error:
            return {"path": str(path), "count": 0, "exists": True}
    return {"path": str(path), "count": n, "exists": True, "kinds": kinds}


def resolve_project_root(
    explicit: str | Path | None = None,
    *,
    cfg: dict | None = None,
) -> Path | None:
    """Best-effort project root: explicit → config workspace_dir → None."""
    if explicit:
        p = Path(explicit).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if p.is_dir():
            return p
    try:
        from services.agent.config import workspace_dir  # noqa: PLC0415

        d = workspace_dir(cfg)
        if d is not None and Path(d).is_dir():
            return Path(d)
    except Exception:  # noqa: BLE001
        pass
    return None
