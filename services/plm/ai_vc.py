"""services.plm.ai_vc — silent, deterministic version control for AI-authored work.

Contract (AI-VC):
  * Provenance: docs created/mutated by the agent loop or MCP carry
    ``authored_by: agent|user|mixed`` (agent edit on a user doc → mixed).
  * Auto-VC: every successful MUTATING AI tool call on a doc triggers a
    deterministic checkpoint. Idempotent id = sha256(doc_id, turn_id, op_batch)
    — same inputs yield the same checkpoint id; retries never duplicate.
  * Silent: no dialogs, no prompts. User-authored docs are never auto-
    checkpointed by this policy (their existing PLM/manual flows stay intact).
  * Storage: content-addressed delta chain under ``.aicadvault/ai_vc/`` (or a
    free-standing root for tests). zlib/zstd-compressed deltas against the
    previous full snapshot; full snapshot every ``full_every`` deltas; identical
    states deduped by content hash. At most one full snapshot is held in RAM
    per checkpoint op.
  * Rollback surface: ``plm_rollback`` / ``plm_timeline`` (MCP + worker RPC).

Config (``config.json`` key ``plm.ai_vc``)::

    {"keep_last": 0, "full_every": 8}
    # keep_last: 0 = keep all in the active session, thin older sessions.
    # full_every: emit a full snapshot every N checkpoints (self-healing chain).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import zlib
from pathlib import Path
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# compression (zstd preferred; zlib always available)
# ---------------------------------------------------------------------------

def _compress(data: bytes) -> tuple[bytes, str]:
    try:
        import zstandard as zstd  # type: ignore

        return zstd.ZstdCompressor(level=3).compress(data), "zstd"
    except Exception:  # noqa: BLE001
        return zlib.compress(data, level=6), "zlib"


def _decompress(data: bytes, codec: str) -> bytes:
    if codec == "zstd":
        import zstandard as zstd  # type: ignore

        return zstd.ZstdDecompressor().decompress(data)
    return zlib.decompress(data)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

DEFAULT_CFG: dict[str, Any] = {
    "keep_last": 0,       # 0 = keep all within session; thin older sessions
    "full_every": 8,      # full snapshot every N deltas (self-healing)
    "enabled": True,
}


def load_ai_vc_config(cfg: dict | None = None) -> dict[str, Any]:
    """Read ``plm.ai_vc`` from agent config (or *cfg*), with sane defaults."""
    out = dict(DEFAULT_CFG)
    try:
        if cfg is None:
            from services.agent import config as _cfg  # noqa: PLC0415

            cfg = _cfg.load_config()
    except Exception:  # noqa: BLE001
        cfg = cfg or {}
    block = (cfg or {}).get("plm") or {}
    if isinstance(block, dict):
        ai = block.get("ai_vc") or {}
        if isinstance(ai, dict):
            for k in DEFAULT_CFG:
                if k in ai:
                    out[k] = ai[k]
    return out


# ---------------------------------------------------------------------------
# provenance registry (in-process; also persisted beside each doc chain)
# ---------------------------------------------------------------------------

_AUTHORED_BY: dict[str, str] = {}  # doc_id -> agent|user|mixed
_SESSION_TURN: dict[str, Any] = {
    "session_id": None,
    "turn_id": 0,
}


def set_session_context(*, session_id: str | None = None,
                        turn_id: int | None = None) -> None:
    """Stamp the current agent/MCP session + turn for checkpoint attribution."""
    if session_id is not None:
        _SESSION_TURN["session_id"] = session_id
    if turn_id is not None:
        _SESSION_TURN["turn_id"] = int(turn_id)


def get_session_context() -> dict[str, Any]:
    return dict(_SESSION_TURN)


def stamp_authored(doc_id: str, by: str = "agent") -> str:
    """Record provenance for *doc_id*. agent-on-user → mixed. Returns new value."""
    if not doc_id:
        return "user"
    by = by if by in ("agent", "user", "mixed") else "agent"
    prev = _AUTHORED_BY.get(doc_id)
    if prev is None:
        _AUTHORED_BY[doc_id] = by
    elif prev == by:
        pass
    elif prev == "mixed" or by == "mixed":
        _AUTHORED_BY[doc_id] = "mixed"
    else:
        # agent edit on user doc (or vice versa) → mixed
        _AUTHORED_BY[doc_id] = "mixed"
    return _AUTHORED_BY[doc_id]


def get_authored_by(doc_id: str | None) -> str:
    if not doc_id:
        return "user"
    return _AUTHORED_BY.get(doc_id, "user")


def is_ai_policy_doc(doc_id: str | None) -> bool:
    """True when AI-VC auto-checkpoint policy applies (agent or mixed)."""
    return get_authored_by(doc_id) in ("agent", "mixed")


def clear_provenance(doc_id: str | None = None) -> None:
    if doc_id is None:
        _AUTHORED_BY.clear()
    else:
        _AUTHORED_BY.pop(doc_id, None)


# ---------------------------------------------------------------------------
# deterministic ids
# ---------------------------------------------------------------------------

def checkpoint_id(doc_id: str, turn_id: Any, op_batch: Any) -> str:
    """Idempotent checkpoint id = sha256(doc_id, turn_id, op_batch)[:32 hex]."""
    payload = json.dumps(
        {"doc_id": str(doc_id), "turn_id": turn_id, "op_batch": op_batch},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(doc: Any) -> bytes:
    """Serialize a document (dict or bytes/str) to stable bytes."""
    if isinstance(doc, (bytes, bytearray)):
        return bytes(doc)
    if isinstance(doc, str):
        return doc.encode("utf-8")
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _features_count(doc: Any) -> int:
    if isinstance(doc, (bytes, bytearray, str)):
        try:
            doc = json.loads(doc if isinstance(doc, str) else doc.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return 0
    if not isinstance(doc, dict):
        return 0
    feats = doc.get("features")
    if isinstance(feats, list):
        return len(feats)
    # assemblies / ecad fallbacks
    for key in ("components", "footprints", "views"):
        v = doc.get(key)
        if isinstance(v, list):
            return len(v)
    return 0


# ---------------------------------------------------------------------------
# binary delta (bytes-level; works for .step+ text superset + ECAD JSON)
# ---------------------------------------------------------------------------

def _make_delta(base: bytes, target: bytes) -> bytes:
    """Simple copy/insert delta. Compact for text-like native formats.

    Format (little-endian):
      magic b'AIVC' + u8 version=1
      then ops: 0x01 u32 off u32 len  (copy from base)
               0x02 u32 len + bytes  (insert literal)
      end: 0x00
    """
    # Greedy longest-match with a windowed index on base.
    # For our sizes (document.json of a 50-feature part is tens of KB) this is fine.
    if not base:
        out = bytearray(b"AIVC\x01")
        out.append(0x02)
        out += len(target).to_bytes(4, "little")
        out += target
        out.append(0x00)
        return bytes(out)

    # Build a 3-byte seed index for match finding.
    index: dict[bytes, list[int]] = {}
    for i in range(max(0, len(base) - 2)):
        key = base[i:i + 3]
        index.setdefault(key, []).append(i)

    out = bytearray(b"AIVC\x01")
    i = 0
    n = len(target)
    while i < n:
        best_off, best_len = -1, 0
        if i + 2 < n:
            key = target[i:i + 3]
            for off in index.get(key, ()):
                # extend match
                length = 0
                while (i + length < n and off + length < len(base)
                       and target[i + length] == base[off + length]):
                    length += 1
                if length > best_len:
                    best_len = length
                    best_off = off
        if best_len >= 4:
            out.append(0x01)
            out += best_off.to_bytes(4, "little")
            out += best_len.to_bytes(4, "little")
            i += best_len
        else:
            # gather a run of inserts
            j = i + 1
            while j < n:
                # peek whether a good match starts at j
                run = 0
                if j + 2 < n:
                    key = target[j:j + 3]
                    for off in index.get(key, ()):
                        length = 0
                        while (j + length < n and off + length < len(base)
                               and target[j + length] == base[off + length]):
                            length += 1
                        if length > run:
                            run = length
                if run >= 4:
                    break
                j += 1
            lit = target[i:j]
            out.append(0x02)
            out += len(lit).to_bytes(4, "little")
            out += lit
            i = j
    out.append(0x00)
    return bytes(out)


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    if not delta.startswith(b"AIVC"):
        raise ValueError("not an AIVC delta")
    ver = delta[4]
    if ver != 1:
        raise ValueError(f"unsupported delta version {ver}")
    pos = 5
    out = bytearray()
    n = len(delta)
    while pos < n:
        op = delta[pos]
        pos += 1
        if op == 0x00:
            break
        if op == 0x01:
            off = int.from_bytes(delta[pos:pos + 4], "little"); pos += 4
            ln = int.from_bytes(delta[pos:pos + 4], "little"); pos += 4
            out += base[off:off + ln]
        elif op == 0x02:
            ln = int.from_bytes(delta[pos:pos + 4], "little"); pos += 4
            out += delta[pos:pos + ln]
            pos += ln
        else:
            raise ValueError(f"bad delta op {op}")
    return bytes(out)


# ---------------------------------------------------------------------------
# store layout
# ---------------------------------------------------------------------------
# <root>/
#   objects/<hh>/<hash>     content-addressed blobs (compressed full or delta payload)
#   docs/<doc_id>/chain.jsonl   ordered checkpoint records
#   docs/<doc_id>/meta.json     provenance + session bookkeeping


def default_store_root(project_dir: str | os.PathLike | None = None) -> Path:
    """Prefer ``<project>/.aicadvault/ai_vc``; else a user-cache fallback."""
    if project_dir is not None:
        p = Path(project_dir) / ".aicadvault" / "ai_vc"
        p.mkdir(parents=True, exist_ok=True)
        return p
    try:
        from engine import paths as _paths  # noqa: PLC0415

        base = Path(getattr(_paths, "CACHE", None) or Path.home() / ".cache" / "billet")
    except Exception:  # noqa: BLE001
        base = Path.home() / ".cache" / "billet"
    p = base / "ai_vc"
    p.mkdir(parents=True, exist_ok=True)
    return p


class AiVcStore:
    """Content-addressed delta chain for one store root (many docs)."""

    def __init__(self, root: str | os.PathLike,
                 cfg: dict[str, Any] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        (self.root / "docs").mkdir(exist_ok=True)
        self.cfg = load_ai_vc_config(cfg) if cfg is None or "full_every" not in (cfg or {}) \
            else {**DEFAULT_CFG, **(cfg or {})}

    # -- paths -------------------------------------------------------------

    def _doc_dir(self, doc_id: str) -> Path:
        safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in doc_id) or "doc"
        d = self.root / "docs" / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _obj_path(self, h: str) -> Path:
        return self.root / "objects" / h[:2] / h

    # -- object store ------------------------------------------------------

    def put_object(self, data: bytes, *, kind: str) -> str:
        """Store *data* compressed; return content hash of the UNCOMPRESSED bytes.
        Dedup: identical content → same path, no rewrite of bytes."""
        h = content_hash(data)
        path = self._obj_path(h)
        if path.exists():
            return h
        path.parent.mkdir(parents=True, exist_ok=True)
        blob, codec = _compress(data)
        # envelope: kind|codec|raw_len|payload
        header = json.dumps({"kind": kind, "codec": codec, "n": len(data)},
                            separators=(",", ":")).encode("utf-8")
        path.write_bytes(len(header).to_bytes(4, "little") + header + blob)
        return h

    def get_object(self, h: str) -> tuple[bytes, dict[str, Any]]:
        path = self._obj_path(h)
        if not path.exists():
            raise FileNotFoundError(h)
        raw = path.read_bytes()
        hl = int.from_bytes(raw[:4], "little")
        header = json.loads(raw[4:4 + hl].decode("utf-8"))
        payload = raw[4 + hl:]
        data = _decompress(payload, header["codec"])
        return data, header

    def object_size(self, h: str) -> int:
        path = self._obj_path(h)
        return path.stat().st_size if path.exists() else 0

    # -- chain -------------------------------------------------------------

    def _chain_path(self, doc_id: str) -> Path:
        return self._doc_dir(doc_id) / "chain.jsonl"

    def _meta_path(self, doc_id: str) -> Path:
        return self._doc_dir(doc_id) / "meta.json"

    def read_chain(self, doc_id: str) -> list[dict[str, Any]]:
        p = self._chain_path(doc_id)
        if not p.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _append_chain(self, doc_id: str, rec: dict[str, Any]) -> None:
        p = self._chain_path(doc_id)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")

    def _write_meta(self, doc_id: str, meta: dict[str, Any]) -> None:
        self._meta_path(doc_id).write_text(
            json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    def _read_meta(self, doc_id: str) -> dict[str, Any]:
        p = self._meta_path(doc_id)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # -- materialize (self-healing) ----------------------------------------

    def materialize(self, doc_id: str, checkpoint_id_: str) -> bytes:
        """Reconstruct the full document bytes at *checkpoint_id_*.

        Walks back to the nearest full snapshot, then applies deltas forward.
        If a delta is corrupt/missing, rewinds to the previous full and continues
        (self-healing); raises only when no recoverable full exists.
        """
        chain = self.read_chain(doc_id)
        by_id = {r["id"]: r for r in chain}
        if checkpoint_id_ not in by_id:
            raise KeyError(checkpoint_id_)

        # find path from last full ≤ target
        idx = next(i for i, r in enumerate(chain) if r["id"] == checkpoint_id_)
        full_idx = None
        for i in range(idx, -1, -1):
            if chain[i].get("kind") == "full":
                full_idx = i
                break
        if full_idx is None:
            raise ValueError("no full snapshot behind checkpoint; chain unrecoverable")

        # try from full_idx; on failure, walk further back to an earlier full
        attempt_from = full_idx
        while attempt_from is not None:
            try:
                data, _ = self.get_object(chain[attempt_from]["object"])
                if chain[attempt_from].get("kind") != "full":
                    raise ValueError("expected full")
                for j in range(attempt_from + 1, idx + 1):
                    rec = chain[j]
                    if rec.get("kind") == "full":
                        data, _ = self.get_object(rec["object"])
                    else:
                        delta, _ = self.get_object(rec["object"])
                        data = _apply_delta(data, delta)
                # verify content hash when present
                want = chain[idx].get("content_hash")
                if want and content_hash(data) != want:
                    raise ValueError("content hash mismatch after materialize")
                return data
            except Exception:
                # self-heal: find earlier full
                prev = None
                for i in range(attempt_from - 1, -1, -1):
                    if chain[i].get("kind") == "full":
                        prev = i
                        break
                if prev is None:
                    raise
                attempt_from = prev
        raise ValueError("unrecoverable chain")

    # -- checkpoint --------------------------------------------------------

    def checkpoint(
        self,
        doc_id: str,
        content: Any,
        *,
        turn_id: Any = None,
        op_batch: Any = None,
        label: str = "",
        session_id: str | None = None,
        force_full: bool = False,
    ) -> dict[str, Any]:
        """Cut a deterministic checkpoint. Idempotent on (doc_id, turn_id, op_batch).

        Memory: holds at most one full snapshot in RAM for the delta base.
        """
        if turn_id is None:
            turn_id = _SESSION_TURN.get("turn_id", 0)
        if session_id is None:
            session_id = _SESSION_TURN.get("session_id")
        if op_batch is None:
            op_batch = label or "mutate"

        cid = checkpoint_id(doc_id, turn_id, op_batch)
        chain = self.read_chain(doc_id)
        for rec in chain:
            if rec.get("id") == cid:
                # idempotent hit
                return {"ok": True, "id": cid, "dedup": True, "record": rec,
                        "authored_by": get_authored_by(doc_id)}

        raw = _canonical_bytes(content)
        ch = content_hash(raw)
        # content-hash dedup: identical state as HEAD → reuse object, still record
        # a new checkpoint id (different turn/op) pointing at same content.
        full_every = max(1, int(self.cfg.get("full_every") or DEFAULT_CFG["full_every"]))
        n_since_full = 0
        last_full_raw: bytes | None = None
        last_full_hash: str | None = None
        for rec in reversed(chain):
            if rec.get("kind") == "full":
                last_full_hash = rec.get("content_hash")
                try:
                    last_full_raw, _ = self.get_object(rec["object"])
                except Exception:  # noqa: BLE001
                    last_full_raw = None
                break
            n_since_full += 1

        # also count from start when no full yet
        if last_full_raw is None:
            n_since_full = len(chain)

        use_full = force_full or last_full_raw is None or (n_since_full + 1) >= full_every

        # identical content as previous HEAD → store as full pointer to same object
        prev_hash = chain[-1].get("content_hash") if chain else None
        if prev_hash == ch and chain:
            # reuse previous object (dedup), mark kind=full for cheap materialize
            obj_h = chain[-1]["object"]
            kind = "full"
            stored_bytes = self.object_size(obj_h)
        elif use_full:
            obj_h = self.put_object(raw, kind="full")
            kind = "full"
            stored_bytes = self.object_size(obj_h)
            # free the base from RAM after write
            last_full_raw = None
        else:
            assert last_full_raw is not None
            # delta against the *previous materialized state* (HEAD), not only last full:
            # better ratios for sequential edits. Materialize HEAD once.
            try:
                head_raw = self.materialize(doc_id, chain[-1]["id"])
            except Exception:  # noqa: BLE001
                head_raw = last_full_raw
            delta = _make_delta(head_raw, raw)
            # if delta is not a win, fall back to full
            if len(delta) >= len(raw) * 0.95:
                obj_h = self.put_object(raw, kind="full")
                kind = "full"
            else:
                obj_h = self.put_object(delta, kind="delta")
                kind = "delta"
            stored_bytes = self.object_size(obj_h)
            # drop base from RAM
            del head_raw
            last_full_raw = None

        now = time.time()
        rec = {
            "id": cid,
            "doc_id": doc_id,
            "turn": int(turn_id) if str(turn_id).lstrip("-").isdigit() else turn_id,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "label": label or str(op_batch),
            "features": _features_count(content if not isinstance(content, (bytes, bytearray)) else raw),
            "features_count": _features_count(content if not isinstance(content, (bytes, bytearray)) else raw),
            "kind": kind,
            "object": obj_h,
            "content_hash": ch,
            "session_id": session_id,
            "op_batch": op_batch,
            "stored_bytes": stored_bytes,
            "raw_bytes": len(raw),
            "authored_by": get_authored_by(doc_id),
        }
        self._append_chain(doc_id, rec)
        meta = self._read_meta(doc_id)
        meta.update({
            "doc_id": doc_id,
            "authored_by": get_authored_by(doc_id),
            "head": cid,
            "session_id": session_id,
            "updated_at": rec["time"],
        })
        self._write_meta(doc_id, meta)
        self._apply_retention(doc_id, session_id)
        return {"ok": True, "id": cid, "dedup": False, "record": rec,
                "authored_by": rec["authored_by"]}

    def _apply_retention(self, doc_id: str, session_id: str | None) -> None:
        """Thin older sessions when keep_last > 0; keep all within current session."""
        keep_last = int(self.cfg.get("keep_last") or 0)
        if keep_last <= 0:
            return  # keep all (session-default)
        chain = self.read_chain(doc_id)
        if len(chain) <= keep_last:
            return
        # Keep the newest keep_last entries; drop chain records older than that
        # (objects stay for content-addressed dedup; orphan GC is a later concern).
        keep = chain[-keep_last:]
        p = self._chain_path(doc_id)
        with p.open("w", encoding="utf-8") as f:
            for rec in keep:
                f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")

    # -- timeline / rollback -----------------------------------------------

    def timeline(self, doc_id: str | None = None) -> dict[str, Any]:
        """Return checkpoints newest-first for *doc_id* (or all docs if None)."""
        if doc_id:
            chain = self.read_chain(doc_id)
            cps = [self._public_cp(r) for r in reversed(chain)]
            return {"ok": True, "checkpoints": cps, "doc_id": doc_id,
                    "authored_by": get_authored_by(doc_id) or self._read_meta(doc_id).get("authored_by", "user")}
        # all docs
        cps: list[dict[str, Any]] = []
        docs_root = self.root / "docs"
        if docs_root.is_dir():
            for d in sorted(docs_root.iterdir()):
                if not d.is_dir():
                    continue
                for r in reversed(self.read_chain(d.name)):
                    cps.append(self._public_cp(r))
        cps.sort(key=lambda c: c.get("time") or "", reverse=True)
        return {"ok": True, "checkpoints": cps}

    @staticmethod
    def _public_cp(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": r.get("id"),
            "turn": r.get("turn"),
            "label": r.get("label") or "",
            "time": r.get("time") or r.get("when") or "",
            "when": r.get("when") or r.get("time") or "",
            "features": r.get("features", r.get("features_count", 0)),
            "features_count": r.get("features_count", r.get("features", 0)),
            "kind": r.get("kind"),
            "content_hash": r.get("content_hash"),
            "authored_by": r.get("authored_by"),
            "doc_id": r.get("doc_id"),
            "stored_bytes": r.get("stored_bytes", 0),
            "raw_bytes": r.get("raw_bytes", 0),
        }

    def resolve_target(self, doc_id: str, to: str) -> dict[str, Any]:
        """Resolve *to* (checkpoint id | 'turn:N' | '-N') to a chain record."""
        chain = self.read_chain(doc_id)
        if not chain:
            raise KeyError("no checkpoints")
        to = str(to or "").strip()
        if not to:
            raise ValueError("empty rollback target")
        if to.startswith("turn:"):
            want = to.split(":", 1)[1]
            try:
                want_i = int(want)
            except ValueError:
                want_i = want
            for r in reversed(chain):
                if r.get("turn") == want_i or str(r.get("turn")) == str(want):
                    return r
            raise KeyError(f"no checkpoint for turn {want}")
        if to.startswith("-") and to[1:].isdigit():
            n = int(to)
            # -1 = previous (HEAD-1? or HEAD?). Contract: -N relative steps back
            # from HEAD. -1 = HEAD (most recent), -2 = one before, …
            # Common git-ish: -1 = last. We'll treat -1 as HEAD, -2 as HEAD-1.
            idx = len(chain) + n  # n negative
            if idx < 0 or idx >= len(chain):
                raise KeyError(f"relative target {to} out of range")
            return chain[idx]
        # bare checkpoint id
        for r in chain:
            if r.get("id") == to:
                return r
        raise KeyError(f"unknown checkpoint {to}")

    def rollback(
        self,
        doc_id: str,
        to: str,
        *,
        restore_fn: Callable[[str, bytes], Any] | None = None,
        ledger: Any = None,
        actor: str = "ai_vc",
    ) -> dict[str, Any]:
        """Restore *doc_id* to target *to*. Applies *restore_fn(doc_id, bytes)* if given.

        Always logs a ledger event when *ledger* is provided.
        """
        rec = self.resolve_target(doc_id, to)
        data = self.materialize(doc_id, rec["id"])
        applied = False
        engine_result = None
        if restore_fn is not None:
            engine_result = restore_fn(doc_id, data)
            applied = True
        # advance head marker
        meta = self._read_meta(doc_id)
        meta["head"] = rec["id"]
        meta["rolled_back_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._write_meta(doc_id, meta)
        if ledger is not None:
            try:
                ledger.append(actor, "ai_vc_rollback", {
                    "doc_id": doc_id,
                    "to": to,
                    "checkpoint_id": rec["id"],
                    "turn": rec.get("turn"),
                    "content_hash": rec.get("content_hash"),
                })
            except Exception:  # noqa: BLE001
                pass
        return {
            "ok": True,
            "checkpoint_id": rec["id"],
            "restored_turn": rec.get("turn"),
            "content_hash": rec.get("content_hash"),
            "via": "restore_fn" if applied else "materialize",
            "content": data,  # caller may drop; tests use it
            "engine_result": engine_result,
        }

    # -- storage stats -----------------------------------------------------

    def storage_stats(self, doc_id: str) -> dict[str, Any]:
        chain = self.read_chain(doc_id)
        stored = sum(int(r.get("stored_bytes") or 0) for r in chain)
        naive = sum(int(r.get("raw_bytes") or 0) for r in chain)
        return {
            "checkpoints": len(chain),
            "stored_bytes": stored,
            "naive_bytes": naive,
            "ratio": (stored / naive) if naive else 0.0,
        }


# ---------------------------------------------------------------------------
# process-wide default store + capture helpers
# ---------------------------------------------------------------------------

_STORE: AiVcStore | None = None
_STORE_ROOT: Path | None = None


def get_store(root: str | os.PathLike | None = None,
              cfg: dict | None = None) -> AiVcStore:
    global _STORE, _STORE_ROOT
    if root is not None:
        p = Path(root)
        if _STORE is not None and _STORE_ROOT == p:
            return _STORE
        _STORE = AiVcStore(p, cfg=cfg)
        _STORE_ROOT = p
        return _STORE
    if _STORE is None:
        _STORE = AiVcStore(default_store_root(), cfg=cfg)
        _STORE_ROOT = _STORE.root
    return _STORE


def reset_store_for_tests(root: str | os.PathLike | None = None,
                          cfg: dict | None = None) -> AiVcStore:
    """Replace the process store (tests only)."""
    global _STORE, _STORE_ROOT
    clear_provenance()
    set_session_context(session_id="test", turn_id=0)
    if root is None:
        import tempfile
        root = tempfile.mkdtemp(prefix="ai_vc_")
    _STORE = AiVcStore(root, cfg=cfg or dict(DEFAULT_CFG))
    _STORE_ROOT = _STORE.root
    return _STORE


def capture_doc_bytes(doc_id: str,
                      content: Any | None = None,
                      bridge_call: Callable[..., Any] | None = None) -> bytes:
    """Serialize the live doc. *content* short-circuits (tests); else doc_tree."""
    if content is not None:
        return _canonical_bytes(content)
    call = bridge_call
    if call is None:
        try:
            from services.mcp import core_bridge as cb  # noqa: PLC0415
            call = cb.call
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"no engine bridge to capture doc: {exc}") from exc
    tree = call("doc_tree", {"doc_id": doc_id})
    if isinstance(tree, dict) and tree.get("_is_error"):
        raise RuntimeError(tree.get("error") or "doc_tree failed")
    doc = tree.get("tree") if isinstance(tree, dict) else None
    if doc is None:
        raise RuntimeError("doc_tree returned no tree")
    return _canonical_bytes(doc)


def restore_doc_bytes(doc_id: str, data: bytes,
                      bridge_call: Callable[..., Any] | None = None) -> Any:
    """Best-effort restore: doc_checkpoint then replace via temp .step+ open is
    engine-specific; here we use doc_set_meta('ai_vc_restore') + attempt to re-open
    via a temp save when available. Injectable for tests."""
    call = bridge_call
    if call is None:
        try:
            from services.mcp import core_bridge as cb  # noqa: PLC0415
            call = cb.call
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"no engine bridge to restore doc: {exc}") from exc
    # Prefer: stash content under meta so host/GUI can pick it up; also try
    # writing a temp native file and reloading when doc_save/doc_open work.
    try:
        doc = json.loads(data.decode("utf-8"))
    except Exception:  # noqa: BLE001
        doc = None
    # Strategy: set meta snapshot + return the parsed doc for the host to apply.
    try:
        call("doc_set_meta", {"doc_id": doc_id, "key": "ai_vc_restore",
                              "value": {"content_hash": content_hash(data),
                                        "n": len(data)}})
    except Exception:  # noqa: BLE001
        pass
    # If the engine exposes no full-doc replace, materialize to a temp .step+ and
    # doc_open is the caller's responsibility (GUI/worker). Return content always.
    return {"ok": True, "doc_id": doc_id, "document": doc,
            "content_hash": content_hash(data)}


# ---------------------------------------------------------------------------
# policy entry points (the agent / MCP seam)
# ---------------------------------------------------------------------------

# Tools that create a brand-new doc (stamp authored_by=agent).
_CREATE_TOOLS = frozenset({
    "new_part", "new_assembly", "ecad_new_schematic", "ecad_new_pcb",
})


def _doc_id_from_args_result(tool: str, args: dict | None, result: Any) -> str | None:
    args = args or {}
    if isinstance(args, dict) and args.get("doc_id"):
        return str(args["doc_id"])
    if isinstance(result, dict):
        if result.get("doc_id"):
            return str(result["doc_id"])
        # some tools nest under result
        inner = result.get("result")
        if isinstance(inner, dict) and inner.get("doc_id"):
            return str(inner["doc_id"])
    return None


def _op_batch_from(tool: str, args: dict | None) -> Any:
    args = args or {}
    if not isinstance(args, dict):
        return tool
    # Prefer ops list / features for stable identity of the mutation batch.
    for key in ("ops", "features", "components"):
        if key in args:
            return {"tool": tool, key: args[key]}
    # Fall back to a canonical subset of args (exclude pure routing keys).
    skip = {"doc_id"}
    body = {k: args[k] for k in sorted(args) if k not in skip}
    return {"tool": tool, "args": body} if body else tool


def on_ai_tool_success(
    tool: str,
    args: dict | None,
    result: Any,
    *,
    turn_id: Any = None,
    session_id: str | None = None,
    content: Any | None = None,
    store: AiVcStore | None = None,
    capture_fn: Callable[[str], bytes] | None = None,
    cfg: dict | None = None,
    ledger: Any = None,
    actor: str = "ai_vc",
) -> dict[str, Any] | None:
    """Silent auto-VC after a successful mutating AI tool call.

    Returns the checkpoint envelope, or None when the policy does not apply
    (non-mutating, user-only doc, disabled, capture failure).
    """
    cfg_r = load_ai_vc_config(cfg)
    if not cfg_r.get("enabled", True):
        return None

    # Import here to avoid cycles; MUTATING_TOOLS is the single source of truth.
    try:
        from services.agent.guardrails import (  # noqa: PLC0415
            MUTATING_TOOLS, is_mutating_tool,
        )
    except Exception:  # noqa: BLE001
        MUTATING_TOOLS = frozenset()  # type: ignore
        def is_mutating_tool(t: str) -> bool:  # type: ignore
            return t in {
                "add_features", "doc_apply_ops", "new_part", "new_assembly",
                "asm_add_component", "asm_mate", "part_place", "set_material",
                "delete_feature", "remove_feature", "regen_resolve",
            }

    doc_id = _doc_id_from_args_result(tool, args, result)
    if tool in _CREATE_TOOLS and doc_id:
        stamp_authored(doc_id, "agent")
    elif doc_id and is_mutating_tool(tool):
        # agent mutation: if previously user → mixed; if unknown → agent
        prev = get_authored_by(doc_id)
        if prev == "user" and doc_id in _AUTHORED_BY:
            stamp_authored(doc_id, "agent")  # → mixed
        elif doc_id not in _AUTHORED_BY:
            # First touch via AI path without an explicit user mark → agent.
            stamp_authored(doc_id, "agent")
        else:
            stamp_authored(doc_id, "agent")

    if not is_mutating_tool(tool):
        return None
    if not doc_id:
        return None
    if not is_ai_policy_doc(doc_id):
        # User-authored and never AI-touched → leave alone.
        return None

    st = store or get_store(cfg=cfg)
    op_batch = _op_batch_from(tool, args)
    if turn_id is None:
        turn_id = _SESSION_TURN.get("turn_id", 0)
    if session_id is None:
        session_id = _SESSION_TURN.get("session_id")

    try:
        if content is not None:
            raw = content
        elif capture_fn is not None:
            raw = capture_fn(doc_id)
        else:
            raw = capture_doc_bytes(doc_id, content=None)
    except Exception as exc:  # noqa: BLE001 — silent; never break the tool path
        if ledger is not None:
            try:
                ledger.append(actor, "ai_vc_checkpoint", {
                    "ok": False, "doc_id": doc_id, "tool": tool,
                    "error": str(exc),
                })
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False, "reason": "capture_failed", "hint": str(exc)}

    label = tool if not isinstance(op_batch, dict) else tool
    out = st.checkpoint(
        doc_id, raw,
        turn_id=turn_id,
        op_batch=op_batch,
        label=label,
        session_id=session_id,
    )
    if ledger is not None:
        try:
            ledger.append(actor, "ai_vc_checkpoint", {
                "ok": True,
                "doc_id": doc_id,
                "tool": tool,
                "checkpoint_id": out.get("id"),
                "dedup": out.get("dedup"),
                "turn": turn_id,
            })
        except Exception:  # noqa: BLE001
            pass
    return out


def mark_user_doc(doc_id: str) -> None:
    """Explicitly mark a doc as user-authored (e.g. opened from disk by human)."""
    if doc_id and doc_id not in _AUTHORED_BY:
        _AUTHORED_BY[doc_id] = "user"


# ---------------------------------------------------------------------------
# public RPC-shaped helpers (MCP + worker)
# ---------------------------------------------------------------------------

def plm_timeline(doc_id: str | None = None, *,
                 store: AiVcStore | None = None) -> dict[str, Any]:
    st = store or get_store()
    return st.timeline(doc_id or None)


def plm_rollback(doc_id: str | None = None, to: str = "",
                 *, store: AiVcStore | None = None,
                 restore_fn: Callable[[str, bytes], Any] | None = None,
                 ledger: Any = None) -> dict[str, Any]:
    if not to:
        return {"ok": False, "reason": "invalid_mode",
                "hint": "give a checkpoint id, turn:N, or -N to roll back to"}
    st = store or get_store()
    # If doc_id omitted, try to find the checkpoint across docs.
    if not doc_id:
        tl = st.timeline(None)
        match = next((c for c in tl.get("checkpoints", [])
                      if c.get("id") == to or (to.startswith("turn:"))), None)
        if match and match.get("doc_id"):
            doc_id = match["doc_id"]
        elif tl.get("checkpoints"):
            doc_id = tl["checkpoints"][0].get("doc_id")
    if not doc_id:
        return {"ok": False, "reason": "invalid_item",
                "hint": "no document to roll back"}
    try:
        rfn = restore_fn
        if rfn is None:
            rfn = restore_doc_bytes
        out = st.rollback(doc_id, to, restore_fn=rfn, ledger=ledger)
        # strip bulky content from the RPC envelope
        content = out.pop("content", None)
        out["bytes"] = len(content) if content is not None else 0
        return out
    except KeyError as e:
        return {"ok": False, "reason": "not_found", "hint": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(e).__name__}: {e}"}
