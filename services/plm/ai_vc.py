"""services.plm.ai_vc — silent, deterministic version control for AI-authored work.

Contract (AI-VC) v1 + v2 (prompt/tool granularity):
  * Provenance: docs created/mutated by the agent loop or MCP carry
    ``authored_by: agent|user|mixed`` (agent edit on a user doc → mixed).
  * Auto-VC v2: checkpoint BEFORE every user prompt turn
    (``kind:"prompt"``, label = first ~60 chars of the prompt) and BEFORE every
    mutating tool call (``kind:"tool"``, label = tool name) on agent/mixed docs.
    Idempotent id = sha256(doc_id, turn_id, op_batch) — retries never duplicate.
  * Silent: no dialogs, no prompts. User-authored docs are never auto-
    checkpointed by this policy (their existing PLM/manual flows stay intact).
  * Storage: content-addressed delta chain under ``.aicadvault/ai_vc/`` (or a
    free-standing root for tests). zlib/zstd-compressed deltas against the
    previous snapshot; full snapshot every ``full_every`` checkpoints; identical
    states deduped by content hash (no new full blob when unchanged). At most
    one full snapshot is held in RAM per checkpoint op.
  * Timeline rows: ``{id, turn, label, time, features_count, kind, msg_idx,
    call}`` where ``kind`` is ``prompt|tool`` and ``call`` is ``{turn, i}``
    for tool checkpoints. Storage encoding is ``storage_kind: full|delta``.
  * Rollback: ``to`` accepts checkpoint id | ``turn:N`` | ``-N`` | ``msg:<idx>``
    | ``call:<turn>:<i>``. Response includes ``chat_truncate_to`` (msg_idx).
  * Backing-file provenance: when the doc is saved to a file, each checkpoint
    also records ``file:{path, sha256, mtime, size}`` (additive — older records
    lack it). ``plm_rollback_check`` previews a rollback non-destructively and
    re-hashes the file to detect modifications made outside the session.
  * Rollback surface: ``plm_rollback`` / ``plm_timeline`` / ``plm_rollback_check``
    (MCP + worker RPC).

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
import zipfile
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
    "msg_idx": None,
    "doc_id": None,
}


def set_session_context(*, session_id: str | None = None,
                        turn_id: int | None = None,
                        msg_idx: int | None = None,
                        doc_id: str | None = None) -> None:
    """Stamp the current agent/MCP session + turn for checkpoint attribution.

    *msg_idx* is the GUI chat message ordinal for the active user prompt turn
    (threaded from the host through ``agent_turn.run_turn``).
    *doc_id* is the active document for prompt-boundary checkpoints.
    """
    if session_id is not None:
        _SESSION_TURN["session_id"] = session_id
    if turn_id is not None:
        _SESSION_TURN["turn_id"] = int(turn_id)
    if msg_idx is not None:
        _SESSION_TURN["msg_idx"] = int(msg_idx)
    if doc_id is not None:
        _SESSION_TURN["doc_id"] = str(doc_id)


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


def _authored_container_hash(path: Path) -> str | None:
    """sha256 over a ZIP container's authored members (name + bytes, name order).

    Derived build state (cache.*, geometry.step, study results — see
    :func:`services.plm.snapshot.is_derived_entry`) is excluded: the engine
    rewrites it after a rebuild without touching the document, so it is not a
    modification. ``None`` for a file that is not a readable ZIP container.
    """
    from services.plm.snapshot import is_derived_entry  # noqa: PLC0415 (light module)

    try:
        with zipfile.ZipFile(path) as zf:
            h = hashlib.sha256()
            for name in sorted(zf.namelist()):
                if is_derived_entry(name):
                    continue
                h.update(name.encode("utf-8") + b"\0")
                with zf.open(name) as member:
                    for chunk in iter(lambda: member.read(1 << 20), b""):
                        h.update(chunk)
                h.update(b"\0")
            return h.hexdigest()
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return None


def file_fingerprint(path: str | os.PathLike | None) -> dict[str, Any] | None:
    """Content hash + mtime + size of a backing file on disk.

    Hash is primary (mtime alone gives false positives from touch/sync and
    false negatives from same-second writes); mtime/size ride along as
    secondary signals for display. A ZIP container (``.step+``/``.ecad+``/
    ``.draw+``) also carries ``authored_sha256`` over its authored members only,
    so a rewrite of derived build state alone does not read as a modification.
    Returns ``None`` when the path is empty, missing, not a regular file, or
    unreadable — callers treat that as \"provenance unknown\", never as
    \"unmodified\".
    """
    if not path:
        return None
    try:
        p = Path(path)
        if not p.is_file():
            return None
        st = p.stat()
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        rec: dict[str, Any] = {"path": str(p), "sha256": h.hexdigest(),
                               "mtime": st.st_mtime, "size": st.st_size}
        authored = _authored_container_hash(p)
        if authored:
            rec["authored_sha256"] = authored
        return rec
    except OSError:
        return None


def _doc_backing_path(doc_id: str,
                      bridge_call: Callable[..., Any] | None = None) -> str | None:
    """Best-effort: the doc's current backing file (.step+ path) via ``doc_list``.

    The engine session tracks the save path per doc; ``None`` when the doc was
    never saved or the bridge is unavailable. Never raises.
    """
    if not doc_id:
        return None
    call = bridge_call
    if call is None:
        try:
            from services.mcp import core_bridge as cb  # noqa: PLC0415
            call = cb.call
        except Exception:  # noqa: BLE001
            return None
    try:
        docs = call("doc_list", {})
        if isinstance(docs, dict):
            docs = docs.get("docs") or docs.get("items") or []
        if not isinstance(docs, list):
            return None
        for d in docs:
            if not isinstance(d, dict):
                continue
            if str(d.get("doc_id") or d.get("id") or "") == str(doc_id):
                p = d.get("path")
                return str(p) if p else None
    except Exception:  # noqa: BLE001
        return None
    return None


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
        from services import paths as _paths  # noqa: PLC0415

        base = Path(getattr(_paths, "CACHE", None) or Path.home() / ".cache" / "chudware")
    except Exception:  # noqa: BLE001
        base = Path.home() / ".cache" / "chudware"
    p = base / "ai_vc"
    p.mkdir(parents=True, exist_ok=True)
    return p


class ChainCorruptionError(ValueError):
    """A chain file contains unreadable or non-record data."""

    def __init__(self, doc_id: str, line: int, detail: str) -> None:
        super().__init__(detail)
        self.doc_id = doc_id
        self.line = line
        self.detail = detail


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
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ChainCorruptionError(
                doc_id, 0, f"could not read chain: {type(exc).__name__}: {exc}"
            ) from exc
        out: list[dict[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ChainCorruptionError(
                    doc_id, line_number, f"malformed JSON: {exc.msg}"
                ) from exc
            if not isinstance(record, dict):
                raise ChainCorruptionError(
                    doc_id, line_number, "checkpoint record is not an object")
            out.append(record)
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

    @staticmethod
    def _storage_kind_of(rec: dict[str, Any]) -> str:
        """Return full|delta for a chain record (v1 kind or v2 storage_kind)."""
        sk = rec.get("storage_kind")
        if sk in ("full", "delta"):
            return sk
        # v1 records used kind for storage encoding
        k = rec.get("kind")
        if k in ("full", "delta"):
            return k
        return "full"

    @classmethod
    def _is_full_storage(cls, rec: dict[str, Any]) -> bool:
        return cls._storage_kind_of(rec) == "full"

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
            if self._is_full_storage(chain[i]):
                full_idx = i
                break
        if full_idx is None:
            raise ValueError("no full snapshot behind checkpoint; chain unrecoverable")

        # try from full_idx; on failure, walk further back to an earlier full
        attempt_from = full_idx
        while attempt_from is not None:
            try:
                data, _ = self.get_object(chain[attempt_from]["object"])
                if not self._is_full_storage(chain[attempt_from]):
                    raise ValueError("expected full")
                for j in range(attempt_from + 1, idx + 1):
                    rec = chain[j]
                    if self._is_full_storage(rec):
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
                    if self._is_full_storage(chain[i]):
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
        kind: str = "tool",
        msg_idx: int | None = None,
        call: dict[str, Any] | None = None,
        project_dir: str | None = None,
        doc_file: Any = None,
    ) -> dict[str, Any]:
        """Cut a deterministic checkpoint. Idempotent on (doc_id, turn_id, op_batch).

        *kind* is the event kind (``prompt`` | ``tool``). Storage encoding is
        recorded separately as ``storage_kind`` (``full`` | ``delta``).
        *doc_file* (optional) is the document's backing file — a path (hashed
        here) or a precomputed :func:`file_fingerprint` dict — recorded as
        ``rec[\"file\"]`` so a later rollback can detect modifications made
        outside this session. Additive: older records simply lack the key.

        Memory: holds at most one full snapshot in RAM for the delta base.
        """
        if turn_id is None:
            turn_id = _SESSION_TURN.get("turn_id", 0)
        if session_id is None:
            session_id = _SESSION_TURN.get("session_id")
        if msg_idx is None:
            mi = _SESSION_TURN.get("msg_idx")
            msg_idx = int(mi) if mi is not None else None
        if op_batch is None:
            op_batch = label or "mutate"

        event_kind = kind if kind in ("prompt", "tool") else "tool"

        cid = checkpoint_id(doc_id, turn_id, op_batch)
        chain = self.read_chain(doc_id)
        for rec in chain:
            if rec.get("id") == cid:
                # idempotent hit
                return {"ok": True, "id": cid, "dedup": True, "record": rec,
                        "authored_by": get_authored_by(doc_id),
                        "content_unchanged": bool(rec.get("content_unchanged"))}

        raw = _canonical_bytes(content)
        ch = content_hash(raw)
        # content-hash dedup: identical state as HEAD → reuse object, still record
        # a new checkpoint id (different turn/op) pointing at same content.
        full_every = max(1, int(self.cfg.get("full_every") or DEFAULT_CFG["full_every"]))
        n_since_full = 0
        last_full_raw: bytes | None = None
        for rec in reversed(chain):
            if self._is_full_storage(rec):
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
        content_unchanged = bool(prev_hash == ch and chain)
        if content_unchanged:
            # reuse previous object (dedup), mark storage_kind=full for cheap materialize
            obj_h = chain[-1]["object"]
            storage_kind = "full"
            stored_bytes = self.object_size(obj_h)
        elif use_full:
            obj_h = self.put_object(raw, kind="full")
            storage_kind = "full"
            stored_bytes = self.object_size(obj_h)
            last_full_raw = None
        else:
            assert last_full_raw is not None
            # delta against HEAD (previous materialized state) for better ratios
            try:
                head_raw = self.materialize(doc_id, chain[-1]["id"])
            except Exception:  # noqa: BLE001
                head_raw = last_full_raw
            delta = _make_delta(head_raw, raw)
            # if delta is not a win, fall back to full
            if len(delta) >= len(raw) * 0.95:
                obj_h = self.put_object(raw, kind="full")
                storage_kind = "full"
            else:
                obj_h = self.put_object(delta, kind="delta")
                storage_kind = "delta"
            stored_bytes = self.object_size(obj_h)
            del head_raw
            last_full_raw = None

        now = time.time()
        call_rec = None
        if isinstance(call, dict) and call:
            call_rec = {
                "turn": call.get("turn", turn_id),
                "i": call.get("i", 0),
            }
        feat_n = _features_count(
            content if not isinstance(content, (bytes, bytearray)) else raw)
        # Backing-file provenance (additive/optional): hash the file the doc
        # is saved to so rollback can warn when it changed outside the session.
        file_rec: dict[str, Any] | None = None
        if doc_file:
            if isinstance(doc_file, dict):
                file_rec = {k: doc_file[k] for k in ("path", "sha256", "mtime", "size")
                            if doc_file.get(k) is not None}
            else:
                file_rec = file_fingerprint(doc_file)
            if file_rec and not file_rec.get("sha256"):
                file_rec = None
        meta = self._read_meta(doc_id)
        parent = meta.get("head")
        if not parent and chain:
            parent = chain[-1].get("id")
        rec = {
            "id": cid,
            "parent": parent,
            "doc_id": doc_id,
            "turn": int(turn_id) if str(turn_id).lstrip("-").isdigit() else turn_id,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "label": label or str(op_batch),
            "features": feat_n,
            "features_count": feat_n,
            "kind": event_kind,              # prompt | tool (v2 timeline)
            "storage_kind": storage_kind,    # full | delta (encoding)
            "object": obj_h,
            "content_hash": ch,
            "session_id": session_id,
            "op_batch": op_batch,
            "stored_bytes": stored_bytes,
            "raw_bytes": len(raw),
            "authored_by": get_authored_by(doc_id),
            "msg_idx": msg_idx,
            "call": call_rec,
            "content_unchanged": content_unchanged,
        }
        if file_rec is not None:
            rec["file"] = file_rec
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
        _notify_cad_graph(project_dir=project_dir, doc_id=doc_id)
        return {"ok": True, "id": cid, "dedup": False, "record": rec,
                "authored_by": rec["authored_by"],
                "content_unchanged": content_unchanged}

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
        try:
            if doc_id:
                chain = self.read_chain(doc_id)
                cps = [self._public_cp(r) for r in reversed(chain)]
                return {
                    "ok": True,
                    "checkpoints": cps,
                    "doc_id": doc_id,
                    "authored_by": (
                        get_authored_by(doc_id)
                        or self._read_meta(doc_id).get("authored_by", "user")
                    ),
                }
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
        except ChainCorruptionError as exc:
            return {
                "ok": False,
                "reason": "timeline_corrupt",
                "hint": (
                    f"checkpoint timeline for {exc.doc_id} is corrupt"
                    + (f" at line {exc.line}" if exc.line else "")
                ),
                "corruption": {
                    "doc_id": exc.doc_id,
                    "line": exc.line,
                    "detail": exc.detail,
                },
                "checkpoints": [],
            }

    @staticmethod
    def _public_cp(r: dict[str, Any]) -> dict[str, Any]:
        # Event kind for timeline: prompt|tool. Fall back for v1 records that
        # stored full|delta in kind — surface as tool (post-mutation checkpoint).
        event_kind = r.get("kind")
        if event_kind in ("full", "delta") or not event_kind:
            event_kind = "tool"
        out = {
            "id": r.get("id"),
            "turn": r.get("turn"),
            "label": r.get("label") or "",
            "time": r.get("time") or r.get("when") or "",
            "when": r.get("when") or r.get("time") or "",
            "features": r.get("features", r.get("features_count", 0)),
            "features_count": r.get("features_count", r.get("features", 0)),
            "kind": event_kind,
            "content_hash": r.get("content_hash"),
            "authored_by": r.get("authored_by"),
            "parent": r.get("parent"),
            "doc_id": r.get("doc_id"),
            "stored_bytes": r.get("stored_bytes", 0),
            "raw_bytes": r.get("raw_bytes", 0),
            "msg_idx": r.get("msg_idx"),
            "call": r.get("call"),
            # The run that authored this checkpoint. call.i restarts at 0 every run, so
            # without this a consumer cannot tell "call 2 of this run" from "call 2 of a
            # run three sessions ago" on the same document.
            "session_id": r.get("session_id"),
            "storage_kind": AiVcStore._storage_kind_of(r),
            "file": r.get("file"),
        }
        return out

    def resolve_target(self, doc_id: str, to: str) -> dict[str, Any]:
        """Resolve *to* to a chain record.

        Accepted forms:
          * bare checkpoint id
          * ``turn:N``
          * ``-N`` (relative from HEAD; -1 = HEAD)
          * ``msg:<idx>`` — state before that user message (prompt kind)
          * ``call:<turn>:<i>`` — state before that tool call
        """
        chain = self.read_chain(doc_id)
        if not chain:
            raise KeyError("no checkpoints")
        to = str(to or "").strip()
        if not to:
            raise ValueError("empty rollback target")
        if to.startswith("msg:"):
            want = to.split(":", 1)[1]
            try:
                want_i = int(want)
            except ValueError as e:
                raise KeyError(f"invalid msg target {to}") from e
            for r in reversed(chain):
                mi = r.get("msg_idx")
                if mi is None:
                    continue
                if int(mi) == want_i and r.get("kind") == "prompt":
                    return r
            # fallback: any checkpoint with that msg_idx (oldest first = before)
            for r in chain:
                mi = r.get("msg_idx")
                if mi is not None and int(mi) == want_i:
                    return r
            raise KeyError(f"no checkpoint for msg {want}")
        if to.startswith("call:"):
            parts = to.split(":")
            # call:<turn>:<i>
            if len(parts) < 3:
                raise KeyError(f"invalid call target {to}")
            try:
                want_turn = int(parts[1])
                want_i = int(parts[2])
            except ValueError as e:
                raise KeyError(f"invalid call target {to}") from e
            for r in reversed(chain):
                c = r.get("call") or {}
                if not isinstance(c, dict):
                    continue
                ct = c.get("turn")
                ci = c.get("i")
                try:
                    if int(ct) == want_turn and int(ci) == want_i:
                        return r
                except (TypeError, ValueError):
                    continue
            raise KeyError(f"no checkpoint for call {want_turn}:{want_i}")
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
            # -1 = HEAD (most recent), -2 = one before, …
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

        Always logs a ledger event when *ledger* is provided. Returns
        ``chat_truncate_to`` (msg_idx) so the chat UI can rewind the transcript.
        """
        rec = self.resolve_target(doc_id, to)
        data = self.materialize(doc_id, rec["id"])
        applied = False
        engine_result = None
        if restore_fn is not None:
            engine_result = restore_fn(doc_id, data)
            applied = True
        meta = self._read_meta(doc_id)
        rolled_back_from = meta.get("head")
        meta["head"] = rec["id"]
        meta["rolled_back_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta["rolled_back_from"] = rolled_back_from
        self._write_meta(doc_id, meta)
        chat_truncate_to = rec.get("msg_idx")
        if chat_truncate_to is None and isinstance(rec.get("call"), dict):
            # tool checkpoint: truncate to the turn that owned the call
            chat_truncate_to = rec["call"].get("turn")
        if ledger is not None:
            try:
                ledger.append(actor, "ai_vc_rollback", {
                    "doc_id": doc_id,
                    "to": to,
                    "checkpoint_id": rec["id"],
                    "turn": rec.get("turn"),
                    "content_hash": rec.get("content_hash"),
                    "chat_truncate_to": chat_truncate_to,
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
            "chat_truncate_to": chat_truncate_to,
            "msg_idx": rec.get("msg_idx"),
            "kind": rec.get("kind"),
        }

    def rollback_preflight(
        self,
        doc_id: str,
        to: str,
        *,
        file_path: str | None = None,
        chat_size: int | None = None,
    ) -> dict[str, Any]:
        """Non-destructive preview of :meth:`rollback` — the warning ladder data.

        Returns what reverting to *to* would discard (checkpoints / turns /
        chat messages), whether geometry rolls back, and whether the document's
        backing file was modified outside this session (hash comparison against
        the checkpoint's recorded ``file`` block; *file_path* is the caller's
        fallback when the record predates file provenance). Raises the same
        ``KeyError``/``ValueError`` as :meth:`resolve_target`.
        """
        rec = self.resolve_target(doc_id, to)
        chain = self.read_chain(doc_id)
        idx = next(i for i, r in enumerate(chain) if r["id"] == rec["id"])
        after = chain[idx + 1:]
        turns = {str(r.get("turn")) for r in after if r.get("turn") is not None}
        chat_truncate_to = rec.get("msg_idx")
        if chat_truncate_to is None and isinstance(rec.get("call"), dict):
            chat_truncate_to = rec["call"].get("turn")
        discarded_messages: int | None = None
        if chat_size is not None and chat_truncate_to is not None:
            try:
                discarded_messages = max(0, int(chat_size) - int(chat_truncate_to))
            except (TypeError, ValueError):
                discarded_messages = None
        head = chain[-1] if chain else None
        geometry_rollback = bool(
            head and head.get("content_hash") and rec.get("content_hash")
            and head.get("content_hash") != rec.get("content_hash"))

        frec = rec.get("file")
        if not isinstance(frec, dict):
            frec = None
        recorded_sha = (frec or {}).get("sha256")
        recorded_mtime = (frec or {}).get("mtime")
        path = (frec or {}).get("path") or file_path or None
        cur = file_fingerprint(path) if path else None
        recorded_authored = (frec or {}).get("authored_sha256")
        if recorded_sha and cur and cur.get("sha256"):
            # Containers recorded with an authored hash compare authored members only:
            # a rewrite of derived build state (open-time cache refresh) is no edit.
            if recorded_authored and cur.get("authored_sha256"):
                externally_modified: bool | None = cur["authored_sha256"] != recorded_authored
            else:
                externally_modified = cur["sha256"] != recorded_sha
            provenance_known = True
        elif recorded_sha:
            # File unreadable/absent now — cannot verify, say so honestly.
            externally_modified = None
            provenance_known = True
        else:
            externally_modified = None
            provenance_known = False
        return {
            "ok": True,
            "doc_id": doc_id,
            "to": to,
            "checkpoint_id": rec["id"],
            "checkpoint_label": rec.get("label") or "",
            "checkpoint_when": rec.get("when") or rec.get("time") or "",
            "restored_turn": rec.get("turn"),
            "chat_truncate_to": chat_truncate_to,
            "discarded_checkpoints": len(after),
            "discarded_turns": len(turns),
            "discarded_messages": discarded_messages,
            "geometry_rollback": geometry_rollback,
            "file": {
                "path": path,
                "recorded_sha256": recorded_sha,
                "recorded_mtime": recorded_mtime,
                "current_sha256": (cur or {}).get("sha256"),
                "current_mtime": (cur or {}).get("mtime"),
                "provenance_known": provenance_known,
                "externally_modified": externally_modified,
            },
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

class StoreResolutionError(RuntimeError):
    """An explicit or bound project cannot provide its AI-VC store."""

    def __init__(self, reason: str, hint: str) -> None:
        super().__init__(hint)
        self.reason = reason
        self.hint = hint

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "reason": self.reason, "hint": self.hint}


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


def _active_branch_name(project_root: Path) -> str:
    """Read the active branch WITHOUT opening the vault.

    ``branches.active_branch`` routes through ``ensure_default_branch``, which
    rewrites ``branches/meta.json`` and the local config on every call. This is
    the AI-VC hot path — one resolution per tool call — so it reads the side
    table directly and falls back to the same default that bootstrap would seed.
    """
    meta_path = project_root / ".aicadvault" / "branches" / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        name = str(meta.get("active") or "").strip()
        if name:
            return name
    except (OSError, ValueError):
        pass
    return "main"


def resolve_store(
    project_dir: str | os.PathLike | None = None,
    *,
    cfg: dict | None = None,
) -> AiVcStore:
    """Resolve and bind the branch-local store for the active project.

    Explicit project paths win, followed by the agent's project and filesystem
    roots. With no project context — a headless MCP client — the cache-backed
    fallback store is preserved. Writers and readers both come through here, so
    a checkpoint written during an agent turn lands in the same branch-scoped
    root that ``plm_timeline`` and ``plm_rollback`` later read.
    """
    candidate = str(project_dir or "").strip()
    if not candidate:
        from services.agent.request_context import root_value
        candidate = (root_value("CHUDWARE_PROJECT_DIR").strip()
                     or root_value("CHUDWARE_FS_ROOT").strip())
    if not candidate:
        return get_store(cfg=cfg)
    try:
        from services.agent.project_context import resolve_project_root
        root = Path(resolve_project_root(candidate))
    except Exception as exc:  # noqa: BLE001
        raise StoreResolutionError(
            "project_root_unavailable",
            f"could not resolve project root {candidate}: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    if not (root / ".aicadvault").is_dir():
        raise StoreResolutionError(
            "vault_unavailable",
            f"version history is unavailable for {root}",
        )
    try:
        from services.plm import branches
        store_root = branches.ai_vc_root_for(root, _active_branch_name(root))
        return get_store(store_root, cfg=cfg)
    except StoreResolutionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StoreResolutionError(
            "vault_unavailable",
            f"could not open version history for {root}: "
            f"{type(exc).__name__}: {exc}",
        ) from exc


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


def _notify_cad_graph(*, project_dir: str | None = None,
                      doc_id: str | None = None) -> None:
    """Best-effort: tell CadGraph to rebuild after a successful AI-VC checkpoint."""
    try:
        from services.plm import cad_graph as _cg  # noqa: PLC0415
    except ImportError:
        return
    try:
        _cg.on_ai_vc_checkpoint(project_dir=project_dir, doc_id=doc_id)
    except Exception:  # noqa: BLE001 — never break VC path
        pass


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


def _prompt_label(text: str, limit: int = 60) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _is_mutating(tool: str) -> bool:
    try:
        from services.agent.guardrails import is_mutating_tool  # noqa: PLC0415
        return bool(is_mutating_tool(tool))
    except Exception:  # noqa: BLE001
        return tool in {
            "add_features", "doc_apply_ops", "new_part", "new_assembly",
            "asm_add_component", "asm_mate", "part_place", "set_material",
            "delete_feature", "remove_feature", "regen_resolve",
        }


def _stamp_from_tool(tool: str, doc_id: str | None) -> None:
    if not doc_id:
        return
    if tool in _CREATE_TOOLS:
        stamp_authored(doc_id, "agent")
    elif _is_mutating(tool):
        stamp_authored(doc_id, "agent")


def on_prompt_start(
    prompt: str,
    *,
    doc_id: str | None = None,
    msg_idx: int | None = None,
    turn_id: Any = None,
    session_id: str | None = None,
    content: Any | None = None,
    store: AiVcStore | None = None,
    capture_fn: Callable[[str], bytes] | None = None,
    cfg: dict | None = None,
    ledger: Any = None,
    actor: str = "ai_vc",
    project_dir: str | None = None,
    doc_file: Any = None,
) -> dict[str, Any] | None:
    """v2: checkpoint BEFORE a user prompt turn starts (kind=prompt).

    Label = first ~60 chars of the prompt. Content-hash dedup applies: if the
    doc state is unchanged, no new full snapshot blob is written.
    """
    cfg_r = load_ai_vc_config(cfg)
    if not cfg_r.get("enabled", True):
        return None

    if doc_id is None:
        d = _SESSION_TURN.get("doc_id")
        doc_id = str(d) if d else None
    if not doc_id:
        return None
    # Prompt checkpoints only apply to agent/mixed policy docs (or first AI touch).
    if not is_ai_policy_doc(doc_id) and doc_id not in _AUTHORED_BY:
        # Unknown doc at prompt time: do not force-stamp; skip.
        return None
    if not is_ai_policy_doc(doc_id):
        return None

    if msg_idx is None:
        mi = _SESSION_TURN.get("msg_idx")
        msg_idx = int(mi) if mi is not None else None
    if turn_id is None:
        # Prefer msg_idx as the turn ordinal for prompt checkpoints.
        turn_id = msg_idx if msg_idx is not None else _SESSION_TURN.get("turn_id", 0)
    if session_id is None:
        session_id = _SESSION_TURN.get("session_id")

    set_session_context(session_id=session_id, turn_id=turn_id,
                        msg_idx=msg_idx, doc_id=doc_id)

    label = _prompt_label(prompt)
    op_batch = {"kind": "prompt", "msg_idx": msg_idx, "label": label}

    try:
        st = store or resolve_store(project_dir, cfg=cfg)
    except StoreResolutionError as exc:
        return exc.envelope()
    try:
        if content is not None:
            raw = content
        elif capture_fn is not None:
            raw = capture_fn(doc_id)
        else:
            raw = capture_doc_bytes(doc_id, content=None)
    except Exception as exc:  # noqa: BLE001
        if ledger is not None:
            try:
                ledger.append(actor, "ai_vc_checkpoint", {
                    "ok": False, "doc_id": doc_id, "kind": "prompt",
                    "error": str(exc),
                })
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False, "reason": "capture_failed", "hint": str(exc)}

    if doc_file is None:
        doc_file = _doc_backing_path(doc_id)
    out = st.checkpoint(
        doc_id, raw,
        turn_id=turn_id,
        op_batch=op_batch,
        label=label,
        session_id=session_id,
        kind="prompt",
        msg_idx=msg_idx,
        project_dir=project_dir,
        doc_file=doc_file,
    )
    if ledger is not None:
        try:
            ledger.append(actor, "ai_vc_checkpoint", {
                "ok": True,
                "doc_id": doc_id,
                "kind": "prompt",
                "checkpoint_id": out.get("id"),
                "dedup": out.get("dedup"),
                "content_unchanged": out.get("content_unchanged"),
                "msg_idx": msg_idx,
                "turn": turn_id,
            })
        except Exception:  # noqa: BLE001
            pass
    return out


def on_before_tool(
    tool: str,
    args: dict | None,
    *,
    turn_id: Any = None,
    call_i: int | None = None,
    msg_idx: int | None = None,
    session_id: str | None = None,
    content: Any | None = None,
    store: AiVcStore | None = None,
    capture_fn: Callable[[str], bytes] | None = None,
    cfg: dict | None = None,
    ledger: Any = None,
    actor: str = "ai_vc",
    project_dir: str | None = None,
    result: Any = None,
    doc_file: Any = None,
) -> dict[str, Any] | None:
    """v2: checkpoint BEFORE a mutating tool call (kind=tool, label=tool name).

    ``call:{turn, i}`` uses turn=msg_idx when provided, else the internal turn
    ordinal. Idempotent + content-hash dedup as v1.
    """
    cfg_r = load_ai_vc_config(cfg)
    if not cfg_r.get("enabled", True):
        return None
    if not _is_mutating(tool):
        return None

    doc_id = _doc_id_from_args_result(tool, args, result)
    # Create tools may not have doc_id until after success — pre-checkpoint only
    # when we already know the doc.
    if not doc_id:
        d = _SESSION_TURN.get("doc_id")
        doc_id = str(d) if d else None
    if not doc_id:
        return None

    _stamp_from_tool(tool, doc_id)
    if not is_ai_policy_doc(doc_id):
        return None

    if msg_idx is None:
        mi = _SESSION_TURN.get("msg_idx")
        msg_idx = int(mi) if mi is not None else None
    if turn_id is None:
        turn_id = msg_idx if msg_idx is not None else _SESSION_TURN.get("turn_id", 0)
    if session_id is None:
        session_id = _SESSION_TURN.get("session_id")
    if call_i is None:
        call_i = 0

    call = {"turn": int(turn_id) if str(turn_id).lstrip("-").isdigit() else turn_id,
            "i": int(call_i)}
    # Distinct op_batch from post-success so both can coexist if both fire.
    op_batch = {"kind": "tool", "tool": tool, "call": call,
                "args": _op_batch_from(tool, args)}

    try:
        st = store or resolve_store(project_dir, cfg=cfg)
    except StoreResolutionError as exc:
        return exc.envelope()
    try:
        if content is not None:
            raw = content
        elif capture_fn is not None:
            raw = capture_fn(doc_id)
        else:
            raw = capture_doc_bytes(doc_id, content=None)
    except Exception as exc:  # noqa: BLE001
        if ledger is not None:
            try:
                ledger.append(actor, "ai_vc_checkpoint", {
                    "ok": False, "doc_id": doc_id, "tool": tool, "kind": "tool",
                    "error": str(exc),
                })
            except Exception:  # noqa: BLE001
                pass
        return {"ok": False, "reason": "capture_failed", "hint": str(exc)}

    if doc_file is None:
        doc_file = _doc_backing_path(doc_id)
    out = st.checkpoint(
        doc_id, raw,
        turn_id=turn_id,
        op_batch=op_batch,
        label=tool,
        session_id=session_id,
        kind="tool",
        msg_idx=msg_idx,
        call=call,
        project_dir=project_dir,
        doc_file=doc_file,
    )
    if ledger is not None:
        try:
            ledger.append(actor, "ai_vc_checkpoint", {
                "ok": True,
                "doc_id": doc_id,
                "tool": tool,
                "kind": "tool",
                "checkpoint_id": out.get("id"),
                "dedup": out.get("dedup"),
                "content_unchanged": out.get("content_unchanged"),
                "msg_idx": msg_idx,
                "call": call,
                "turn": turn_id,
            })
        except Exception:  # noqa: BLE001
            pass
    return out


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
    msg_idx: int | None = None,
    call_i: int | None = None,
    project_dir: str | None = None,
    before: bool = False,
    doc_file: Any = None,
) -> dict[str, Any] | None:
    """Silent auto-VC for a mutating AI tool call.

    v2 default path is *before* the tool (``on_before_tool``). When *before*
    is True this delegates there. Post-success remains for create tools that
    only receive a doc_id after the call, and for back-compat callers.
    """
    if before:
        return on_before_tool(
            tool, args,
            turn_id=turn_id, call_i=call_i, msg_idx=msg_idx,
            session_id=session_id, content=content, store=store,
            capture_fn=capture_fn, cfg=cfg, ledger=ledger, actor=actor,
            project_dir=project_dir, result=result, doc_file=doc_file,
        )

    cfg_r = load_ai_vc_config(cfg)
    if not cfg_r.get("enabled", True):
        return None

    doc_id = _doc_id_from_args_result(tool, args, result)
    _stamp_from_tool(tool, doc_id)

    if not _is_mutating(tool):
        return None
    if not doc_id:
        return None
    if not is_ai_policy_doc(doc_id):
        return None

    try:
        st = store or resolve_store(project_dir, cfg=cfg)
    except StoreResolutionError as exc:
        return exc.envelope()
    op_batch = _op_batch_from(tool, args)
    if msg_idx is None:
        mi = _SESSION_TURN.get("msg_idx")
        msg_idx = int(mi) if mi is not None else None
    if turn_id is None:
        turn_id = msg_idx if msg_idx is not None else _SESSION_TURN.get("turn_id", 0)
    if session_id is None:
        session_id = _SESSION_TURN.get("session_id")
    if call_i is None:
        call_i = 0
    call = {"turn": int(turn_id) if str(turn_id).lstrip("-").isdigit() else turn_id,
            "i": int(call_i)}

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

    label = tool
    if doc_file is None:
        doc_file = _doc_backing_path(doc_id)
    out = st.checkpoint(
        doc_id, raw,
        turn_id=turn_id,
        op_batch=op_batch,
        label=label,
        session_id=session_id,
        kind="tool",
        msg_idx=msg_idx,
        call=call,
        project_dir=project_dir,
        doc_file=doc_file,
    )
    if ledger is not None:
        try:
            ledger.append(actor, "ai_vc_checkpoint", {
                "ok": True,
                "doc_id": doc_id,
                "tool": tool,
                "kind": "tool",
                "checkpoint_id": out.get("id"),
                "dedup": out.get("dedup"),
                "content_unchanged": out.get("content_unchanged"),
                "msg_idx": msg_idx,
                "call": call,
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


def plm_rollback_check(doc_id: str | None = None, to: str = "",
                       *, store: AiVcStore | None = None,
                       file_path: str | None = None,
                       chat_size: int | None = None) -> dict[str, Any]:
    """Non-destructive preview of :func:`plm_rollback` for the confirm dialog.

    Same target resolution; returns the warning-ladder payload (discard counts,
    geometry rollback, external file modification) WITHOUT touching anything.
    """
    if not to:
        return {"ok": False, "reason": "invalid_mode",
                "hint": ("give a checkpoint id, turn:N, -N, msg:<idx>, "
                         "or call:<turn>:<i> to preview a roll back to")}
    st = store or get_store()
    if not doc_id:
        tl = st.timeline(None)
        match = next((c for c in tl.get("checkpoints", [])
                      if c.get("id") == to
                      or to.startswith("turn:")
                      or to.startswith("msg:")
                      or to.startswith("call:")), None)
        if match and match.get("doc_id"):
            doc_id = match["doc_id"]
        elif tl.get("checkpoints"):
            doc_id = tl["checkpoints"][0].get("doc_id")
    if not doc_id:
        return {"ok": False, "reason": "invalid_item",
                "hint": "no document to roll back"}
    try:
        return st.rollback_preflight(doc_id, to, file_path=file_path,
                                     chat_size=chat_size)
    except KeyError as e:
        return {"ok": False, "reason": "not_found", "hint": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(e).__name__}: {e}"}


def plm_rollback(doc_id: str | None = None, to: str = "",
                 *, store: AiVcStore | None = None,
                 restore_fn: Callable[[str, bytes], Any] | None = None,
                 ledger: Any = None) -> dict[str, Any]:
    if not to:
        return {"ok": False, "reason": "invalid_mode",
                "hint": ("give a checkpoint id, turn:N, -N, msg:<idx>, "
                         "or call:<turn>:<i> to roll back to")}
    st = store or get_store()
    # If doc_id omitted, try to find the checkpoint across docs.
    if not doc_id:
        tl = st.timeline(None)
        match = next((c for c in tl.get("checkpoints", [])
                      if c.get("id") == to
                      or to.startswith("turn:")
                      or to.startswith("msg:")
                      or to.startswith("call:")), None)
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
