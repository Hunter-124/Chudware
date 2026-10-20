"""services.plm.vault — the PLM vault engine (contract §2–§5, aicad.plm/1).

The storage engine behind Pillar 19. A *project* is a folder; the *vault* is a
hidden git repo at ``.aicadvault/`` (contract §2). This class ties the pieces
together:

  * :mod:`gitbind`   — the dulwich-primary binding (linear, append-only)
  * :mod:`snapshot`  — explode/re-assemble ``.step+`` / ``.ecad+`` containers
  * :mod:`manifest`  — the aicad.plm/1 registry (items, revs, lifecycle)
  * :mod:`history`   — entry kinds + per-item meta.json (§3.4)

Ease-of-use law (contract §8): zero-config init (open == create if absent), no
git vocabulary surfaced, history never rewritten (restore = new forward entry).

Every public method returns plain Python; the ``plm_*`` MCP tools + the §7.1
``PLMVaultClient`` wrap these in the ``{ok, …}`` envelope.
"""
from __future__ import annotations

import getpass
import json
import os
import tempfile
import time
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dulwich.objects import Blob

from services.plm import assist, doctypes, generic_snapshot, gitbind, manifest, snapshot
from services.plm import history as hist  # aliased: Vault.history() method shadows the name
from services.plm import items as items_mod
from services.plm import permissions, planning, summarize

# document containers we version (contract §2)
_CONTAINER_SUFFIXES = (".step+", ".ecad+")
CONTAINER_ITEM_ADD_HINT = (
    "design documents (.step+/.ecad+) are versioned automatically by "
    "plm_save/plm_checkpoint; do not register them by hand — call "
    "plm_checkpoint instead"
)

# versioned shadow-tree top-level dirs the container explode does not itself
# own but that MUST survive every whole-tree commit (carry-forward law). The
# planning store (aicad.planning/1) lives under planning/ and is written via a
# dedicated path; without carry-forward it would drop out of any container save.
_CARRY_FORWARD_TOPLEVEL = frozenset({"planning"})

# doc-kind inference from container extension (best-effort; refined by document.json)
_SUFFIX_KIND = {".step+": "part", ".ecad+": "ecad"}

# authoritative doc-kind from the document's schema string (T-R4: drawings and
# CAM jobs share the .step+ container, so the suffix alone is not enough)
_SCHEMA_KIND = {
    "aicad.part": "part",
    "aicad.asm": "assembly",
    "aicad.drawing": "drawing",
    "aicad.pcb": "ecad",
    "aicad.sch": "ecad",
    "aicad.cam": "cam",
    "aicad.req": "requirements",
}

_local_config_thread_lock = threading.Lock()


@contextmanager
def _local_config_lock(path: Path) -> Iterator[None]:
    """Keep the read/merge/replace transaction exclusive across vault workers."""
    with _local_config_thread_lock:
        with open(path, "a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Dirty-scan stamp cache (process-wide)
# ---------------------------------------------------------------------------
# worker_rpc opens a fresh Vault per RPC (``_with_vault``), so instance-level
# memoization never hits across overview/status calls. This module-level map is
# keyed by the resolved project root and reused across short-lived Vaults in
# the same process (hostd / web worker bridge).
#
# Fast path: reuse a verdict only against an unchanged per-item HEAD key (the item's
# committed content blob ids, excluding the entry metadata every commit rewrites, plus its
# manifest record) and unchanged working stat fingerprint (mtime, ctime, size, inode).
# Verdicts survive HEAD moves, so a checkpoint re-examines only the items whose committed
# content or record it changed — never every container in the project. Same-size
# rewrites preserving mtime invalidate immediately on platforms with change-time ctime.
# Windows ctime is creation time: recheck each item's content after a bounded
# age measured AFTER its last comparison (never an expiring whole-scan TTL).
# root -> {"head": str|None, "item_oids": {item_id: subtree id at head},
#          "content_keys": {subtree id: content key},
#          "items": {item_id: {"stamp": tuple, "head_key": tuple, "dirty": bool}}}
_dirty_scan_cache: dict[str, dict[str, Any]] = {}
# Files under items/<id>/ that every commit rewrites for every item (entry metadata), so
# they say nothing about the item's content.
_PER_COMMIT_ITEM_FILES = frozenset({"meta.json", "summary.txt"})
_STAT_RECHECK_S: float | None = 30.0 if os.name == "nt" else None


def _stat_cache_expired(entry: dict[str, Any]) -> bool:
    return (_STAT_RECHECK_S is not None
            and time.monotonic() - entry.get("recorded_at", 0.0) >= _STAT_RECHECK_S)


def _vault_cache_key(project_dir: str | os.PathLike) -> str:
    return str(Path(project_dir).resolve())


def _item_record_key(it: dict[str, Any]) -> str:
    """Canonical manifest record of an item: any field change revalidates its verdict."""
    return json.dumps(it, sort_keys=True, separators=(",", ":"), default=str)


def _invalidate_dirty_cache(project_dir: str | os.PathLike) -> None:
    """Drop every cached dirty verdict for *project_dir* (forces a full rescan).

    Commits do not need this: verdicts are keyed by each item's HEAD subtree id.
    """
    _dirty_scan_cache.pop(_vault_cache_key(project_dir), None)


# ---------------------------------------------------------------------------
# Container explode cache (process-wide)
# ---------------------------------------------------------------------------
# Every whole-tree commit re-explodes EVERY tracked container, even though a
# save typically touches one of them: an N-container project paid N ZIP reads +
# N canonical JSON re-serializations per save. Keyed by container path and
# validated against an on-disk stamp, so an untouched container is exploded
# once per process and then reused verbatim (the exploded blobs are what the
# commit stores, so reuse is byte-identical by construction).
#
# The stamp is stat-only and includes ctime_ns and the inode alongside
# mtime_ns/size, so an in-place rewrite that preserves mtime (or a restore that
# copies timestamps) still misses. It is captured BEFORE the read: a write that
# races the read stores fresh content under a stale stamp, which only costs the
# next save a re-explode — never a stale commit.
#
# One more hazard the stat tuple alone cannot see, and git's index solves the
# same way ("racily clean"): a rewrite that lands in the SAME clock tick the
# entry was recorded in produces an identical stamp. An entry is therefore only
# trusted once the wall clock has moved past the tick it was stored in, so a
# same-tick rewrite re-explodes instead of silently committing stale blobs.
_EXPLODE_CACHE_MAX_BYTES = 64 * 1024 * 1024
# "<resolved root>\0<rel>" -> {"stamp": tuple, "exploded": ExplodedItem,
#                              "doc": dict|None, "bytes": int}
# Dirty scans retain canonical bytes; decode metadata only if a later save needs it.
_explode_cache: dict[str, dict[str, Any]] = {}
_explode_cache_bytes = 0


def _container_stamp(path: Path) -> tuple[int, int, int, int] | None:
    """Stat fingerprint of a container file, or None when it cannot be stat'ed."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (int(st.st_mtime_ns), int(st.st_ctime_ns), int(st.st_size), int(st.st_ino))


def _explode_cache_put(key: str, stamp: tuple[int, int, int, int],
                       exploded: snapshot.ExplodedItem, doc: dict[str, Any] | None = None) -> None:
    global _explode_cache_bytes
    _explode_cache_drop(key)
    size = len(exploded.document_json) + len(exploded.thumbnail_png or b"")
    size += sum(len(v) for v in exploded.extras.values())
    if size > _EXPLODE_CACHE_MAX_BYTES:
        return  # a single container larger than the whole budget is not cached
    _explode_cache[key] = {"stamp": stamp, "exploded": exploded,
                           "doc": doc, "bytes": size, "recorded_ns": time.time_ns(),
                           "recorded_at": time.monotonic()}
    _explode_cache_bytes += size
    while _explode_cache_bytes > _EXPLODE_CACHE_MAX_BYTES:
        oldest = next(iter(_explode_cache))  # insertion-ordered
        if oldest == key:
            break
        _explode_cache_drop(oldest)


def _explode_cache_get(key: str, stamp: tuple[int, int, int, int] | None
                       ) -> dict[str, Any] | None:
    """Cached explode for `key`, or None when it is absent, stale or racy."""
    if stamp is None:
        return None
    entry = _explode_cache.get(key)
    if entry is None or entry["stamp"] != stamp:
        return None
    if _stat_cache_expired(entry):
        _explode_cache_drop(key)
        return None
    if max(int(stamp[0]), int(stamp[1])) >= int(entry["recorded_ns"]):
        # Written in the same tick we recorded: the stat tuple cannot prove the
        # bytes are the ones we exploded. Re-read instead of trusting it.
        _explode_cache_drop(key)
        return None
    return entry


def _explode_cache_drop(key: str) -> None:
    global _explode_cache_bytes
    old = _explode_cache.pop(key, None)
    if old is not None:
        _explode_cache_bytes -= int(old["bytes"])


def _tree_working_stamp(root: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    """Stat fingerprint of a generic tree item (exclusion-filtered).

    Mirrors :func:`generic_snapshot._snapshot_tree` walk policy so a stamp hit
    means the files that would be content-hashed are unchanged on disk.
    """
    from services.plm.generic_snapshot import (  # local: avoid import cycle noise
        _EXCLUDE_DIR_PREFIXES,
        _EXCLUDE_DIRS,
    )
    out: list[tuple[str, int, int, int, int]] = []
    try:
        if not root.is_dir():
            return tuple()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in _EXCLUDE_DIRS
                and not any(d.startswith(p) for p in _EXCLUDE_DIR_PREFIXES)
            )
            for fn in sorted(filenames):
                if fn in _EXCLUDE_DIRS:
                    continue
                rel = (Path(dirpath) / fn).relative_to(root).as_posix()
                abs_path = generic_snapshot.contained_path(root, rel)
                try:
                    st = abs_path.stat()
                except OSError:
                    continue
                out.append((rel, int(st.st_mtime_ns), int(st.st_ctime_ns),
                            int(st.st_size), int(st.st_ino)))
    except OSError:
        return tuple()
    out.sort()
    return tuple(out)


def _kind_from_schema(schema: str) -> str | None:
    return _SCHEMA_KIND.get(str(schema).split("/", 1)[0])


class VaultError(Exception):
    """Raised for vault operations that fail with a contract reason code."""

    def __init__(self, reason: str, hint: str = "") -> None:
        super().__init__(hint or reason)
        self.reason = reason
        self.hint = hint


def _require_repaired_remotes(m: dict[str, Any]) -> None:
    if any(remote.get("credential_repair_required") for remote in m.get("remotes") or []):
        raise VaultError(
            "invalid_mode",
            "Remote credentials need explicit replacement with plm_add_remote before recording changes. "
            "Rotate exposed credentials and handle old history separately.",
        )


def _released_edit_hint(error: Any, item_id: str) -> str:
    """Turn the released-revision lock into the exact next PLM action."""
    if error.reason != "locked":
        return error.hint
    return (
        f"{error.hint}; call plm_item_set(path='<project-root>', "
        f"item_id={item_id!r}, start_new_rev=True)"
    )


class Vault:
    """An open project vault (contract §2)."""

    def __init__(self, project_dir: str | os.PathLike, binding: gitbind.GitBinding) -> None:
        self.project_dir = Path(project_dir)
        self._bind = binding

    # -- lifecycle (contract §8.2 zero-config) -----------------------------

    @classmethod
    def open(cls, project_dir: str | os.PathLike) -> "Vault":
        """Open a project folder, silently creating the vault if absent.

        Zero-config: no prompt, no setup, no visible files beyond ``.aicadvault/``.
        On first open, existing container files in the folder are registered.
        """
        pdir = Path(project_dir)
        if not pdir.is_dir():
            if (pdir.is_file()
                    and doctypes.detect_doc_type(str(pdir)) in doctypes.CONTAINER_DOC_TYPES):
                raise VaultError("invalid_mode", CONTAINER_ITEM_ADD_HINT)
            raise VaultError("invalid_project", f"not a folder: {project_dir}")
        fresh = not gitbind.GitBinding.exists(pdir)
        binding = gitbind.open_binding(pdir)
        v = cls(pdir, binding)
        if fresh:
            v._seed_initial_project()
        return v

    @classmethod
    def exists(cls, project_dir: str | os.PathLike) -> bool:
        return gitbind.GitBinding.exists(project_dir)

    def close(self) -> None:
        self._bind.close()

    # -- reading -----------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        """Read HEAD without writing; unsafe remote destinations are withheld."""
        head = self._bind.head_commit()
        if head is None:
            return manifest.new_manifest(self.project_dir.name, _now_iso())
        raw = self._bind.read_blob_at(head, "manifest.json")
        if raw is None:  # corrupted/empty — degrade gracefully (DoD)
            return manifest.new_manifest(self.project_dir.name, _now_iso())
        m = manifest.loads_local(raw)
        missing = {
            (it["item_id"], rr["rev"])
            for it in m["items"]
            for rr in it.get("released_revs", [])
            if not rr.get("commit")
        }
        if missing:
            # The release entry can be HEAD after a crash before its pin commit.
            # Walk back to the first entry containing each revision; later
            # checkpoints may have carried the null pin forward.
            for commit, meta in self._bind.iter_log():
                current_raw = self._bind.read_blob_at(commit, "manifest.json")
                if current_raw is None:
                    continue
                current = manifest.loads_local(current_raw)
                present = {
                    (it["item_id"], rr["rev"])
                    for it in current["items"]
                    for rr in it.get("released_revs", [])
                }
                parent = meta["parents"][0] if meta["parents"] else None
                parent_raw = self._bind.read_blob_at(parent, "manifest.json") if parent else None
                previous = manifest.loads_local(parent_raw) if parent_raw else {"items": []}
                prior = {
                    (it["item_id"], rr["rev"])
                    for it in previous["items"]
                    for rr in it.get("released_revs", [])
                }
                introduced = missing & (present - prior)
                for it in m["items"]:
                    for rr in it.get("released_revs", []):
                        if (it["item_id"], rr["rev"]) in introduced:
                            rr["commit"] = commit
                for freeze in m.get("bom_freezes", []):
                    if not freeze.get("commit") and (
                        freeze.get("assembly_item"), freeze.get("rev")
                    ) in introduced:
                        freeze["commit"] = commit
                missing -= introduced
                if not missing:
                    break
            if missing:
                raise VaultError("corrupt_item", "a released revision has no release entry")
        return m

    def status(self) -> dict[str, Any]:
        """Project state for the Hub/badges (contract §7 plm_status shape).

        Shared locks are live remote state. They are best-effort so an offline
        remote never turns otherwise valid local project status into a failure.
        """
        m = self.manifest()
        head = self._bind.head_commit()
        locks: list[dict[str, Any]] = []
        if m.get("remotes"):
            try:
                from services.plm import remote as remote_mod
                lock_result = remote_mod.list_locks(self.project_dir)
                if lock_result.get("ok"):
                    locks = list(lock_result.get("locks") or [])
            except Exception:
                locks = []
        return {
            "project_id": m["project"]["id"],
            "head": head,
            "dirty": self._dirty_items(m),
            "ai_mode": m.get("ai_mode", "assisted"),
            "auto_commit": bool(m.get("auto_commit", True)),
            "items": [self._item_view(it) for it in m["items"]],
            "remotes": m.get("remotes", []),
            "locks": locks,
        }

    def history(self, item_id: str | None = None, limit: int | None = None,
                scope: str = hist.SCOPE_DESIGN) -> list[dict[str, Any]]:
        """Return the timeline (filtered per item if given) — contract §7 plm_history.

        ``scope`` filters by entry scope: ``design`` (default — the curated
        timeline, hiding planning-store churn), ``planning`` (only task/issue/
        milestone changes), or ``all``.

        When *item_id* is omitted (hub overview / full timeline), entry views are
        built from a few path-direct blob reads per commit instead of flattening
        the whole shadow tree — ME489-scale trees are hundreds of blobs.

        When *item_id* is given the answer must mean "the versions of THIS
        file". A commit belongs in an item's timeline iff the item **exists**
        at that commit AND its content differs from its content at that
        commit's **parent** (the next-older commit in the scope-filtered
        chain). The parent comparison is the load-bearing part: comparing
        against the next *newer* commit instead credits a change to whichever
        later commit still carries the content (dropping the commit that
        actually made it), and treating "absent -> present" as just another
        difference emits entries from before the item existed, which cannot be
        diffed at all. The oldest surviving entry is therefore the item's
        creation, and every adjacent pair is a valid compare pair.
        """
        # The chain is walked lazily so a limited history stops decoding
        # commits as soon as it has its N entries: the scope filter runs on the
        # metadata that the walk already decoded (no second read per commit),
        # and the older tail of the chain is never touched at all.
        def scoped_chain() -> Iterator[tuple[str, dict[str, Any]]]:
            for commit, cmeta in self._bind.iter_log():
                if (scope != "all"
                        and hist.scope_from_commit_message(cmeta["message"]) != scope):
                    continue
                yield commit, cmeta

        if item_id is None:
            entries: list[dict[str, Any]] = []
            for commit, cmeta in scoped_chain():
                entries.append(self._entry_view_light(commit, meta=cmeta))
                if limit is not None and len(entries) >= limit:
                    break
            return entries

        # Compare content-addressed blob ids, not decoded project snapshots.
        # CAD history depends only on document.json; generic history includes
        # all item content except regenerated metadata, exactly as before.
        def signature_at(commit: str) -> object | None:
            prefix = snapshot.item_prefix(item_id)
            doc = self._bind.entry_at(commit, prefix + "document.json")
            if doc is not None and doc[0] != 0o040000:
                return doc[1]
            ids = self._bind.subtree_ids(commit, prefix)
            if not ids:
                return None
            return tuple(sorted((path, oid) for path, oid in ids.items()
                                if path[len(prefix):] not in ("meta.json", "summary.txt")))

        entries = []
        chain = scoped_chain()
        cur = next(chain, None)
        cur_sig = signature_at(cur[0]) if cur is not None else None
        while cur is not None:
            commit, cmeta = cur
            parent = next(chain, None)
            parent_sig = signature_at(parent[0]) if parent is not None else None
            emit = cur_sig is not None and not (
                parent is not None and parent_sig is not None and parent_sig == cur_sig)
            if emit:
                entries.append(
                    self._entry_view_light(commit, item_id=item_id, meta=cmeta))
                if limit is not None and len(entries) >= limit:
                    break
            cur, cur_sig = parent, parent_sig
        return entries


    # -- writing -----------------------------------------------------------

    def save(self, kind: str = hist.KIND_AUTO, *, message: str = "",
             author: dict[str, Any] | None = None, ai: dict[str, Any] | None = None,
             ai_by_item: dict[str, dict[str, Any]] | None = None,
             item_id: str | None = None,
             participants: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Cut a history entry over the whole shadow tree (contract §3.1).

        ``kind`` ∈ auto | checkpoint | restore. ``release`` goes through
        :meth:`release`. Registers any new container files first (zero-config).
        """
        if kind not in (hist.KIND_AUTO, hist.KIND_CHECKPOINT, hist.KIND_RESTORE):
            raise VaultError("invalid_mode", f"save does not accept kind={kind!r}")
        # Optimistic concurrency: manifest() reads HEAD, we build the shadow tree from that
        # snapshot, then commit_tree CAS-swaps HEAD. If a concurrent session moved HEAD in between,
        # commit_tree raises ConcurrentUpdate; we rebuild from fresh HEAD and retry (bounded) so
        # neither writer is silently lost. No cross-process lock — same lock-free ethos as handoff.
        author = author or _user_author()
        emeta = gitbind.EntryMeta(
            kind=kind,
            message=message,
            author_name=author["display"],
            author_email=f'{author["id"]}@aicad.local',
        )
        for _attempt in range(6):
            m = self.manifest()
            # T-R4 lifecycle rule (contract §4): no edit-in-place of a Released
            # item — "start a new revision" first. Restore is exempt (§8.3: you
            # can always go back).
            files = self._explode_project_into_shadow(
                m, kind=kind, message=message, author=author, ai=ai,
                ai_by_item=ai_by_item, participants=participants,
                enforce_released_lock=(kind != hist.KIND_RESTORE))
            try:
                commit = self._bind.commit_tree(files, emeta)
            except gitbind.ConcurrentUpdate:
                continue  # HEAD advanced under us — rebuild against the new snapshot and retry
            try:  # optional Box cloud mirror — spool-only, never blocks or fails a save
                from services.plm import box_mirror
                box_mirror.on_vault_saved(str(self.project_dir))
            except Exception:  # noqa: BLE001
                pass
            return {"commit": commit, "kind": kind,
                    "entry": self._entry_view(commit, self._bind.read_tree(commit), item_id)}
        raise VaultError("write_conflict",
                         "vault HEAD kept moving under concurrent writers; retry the save")

    def checkpoint(self, message: str = "", *, author: dict[str, Any] | None = None,
                   ai: dict[str, Any] | None = None,
                   ai_by_item: dict[str, dict[str, Any]] | None = None,
                   item_id: str | None = None,
                   participants: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Cut a named checkpoint entry (contract §3.2).

        When *message* is empty and the project's assist tier allows drafting
        (``assisted``/``managed``), a deterministic plain-language draft is
        templated from the semantic diff (contract §7 ``plm_checkpoint``:
        "drafts message from doc_diff if omitted"). At the ``manual`` floor
        the empty message stays empty — no assist behavior of any kind.
        """
        if not message and assist.allowed(self.ai_mode(), "draft_message"):
            message = self.draft_message(item_id=item_id)
        return self.save(
            hist.KIND_CHECKPOINT,
            message=message,
            author=author,
            ai=ai,
            ai_by_item=ai_by_item,
            item_id=item_id,
            participants=participants,
        )

    # -- assist tier (T-R3, contract §4 ai_mode) ----------------------------

    def ai_mode(self) -> str:
        """The project's assist tier: manual | assisted | managed (§4)."""
        return self.manifest().get("ai_mode", assist.DEFAULT_MODE)

    def set_ai_mode(self, mode: str) -> str:
        """Persist the assist tier for this project (contract §7 plm_set_ai_mode).

        The tier lives in ``manifest.json`` and is re-committed like any other
        manifest change, so it follows the project everywhere.
        """
        if mode not in manifest.AI_MODES:
            raise VaultError("invalid_mode",
                             "the assistance level can be manual, assisted, or managed")
        m = self.manifest()
        if m.get("ai_mode") == mode:
            return mode
        m["ai_mode"] = mode
        self._commit_manifest_change(m)
        return mode

    def auto_commit(self) -> bool:
        """Whether saves are auto-recorded to history (default true).

        Forward-tolerant: absent key reads as enabled. Lives in ``manifest.json``
        so the choice follows the project like ``ai_mode``.
        """
        return bool(self.manifest().get("auto_commit", True))

    def set_auto_commit(self, enabled: bool) -> bool:
        """Persist the auto-record-on-save flag for this project."""
        m = self.manifest()
        if bool(m.get("auto_commit", True)) == bool(enabled):
            return bool(enabled)
        m["auto_commit"] = bool(enabled)
        self._commit_manifest_change(m)
        return bool(enabled)

    # -- item registry (T-R4, contract §4) ----------------------------------

    def set_part_number(self, item_id: str, part_number: str) -> dict[str, Any]:
        """Edit an item's part number (auto-assigned, user-editable; unique
        within the project — contract §4). Persists via a manifest entry."""
        m = self.manifest()
        try:
            item = items_mod.set_part_number(m, item_id, part_number)
        except items_mod.ItemRuleError as e:
            raise VaultError(e.reason, e.hint)
        self._commit_manifest_change(m)
        return items_mod.item_view(item)

    def start_new_rev(self, item_id: str) -> dict[str, Any]:
        """The "start rev C" action: a Released item goes back to *In work*
        so it can be edited; the working file carries forward (contract §4,
        no edit-in-place of Released). Idempotent when already In work."""
        m = self.manifest()
        try:
            out = items_mod.start_new_rev(m, item_id)
        except items_mod.ItemRuleError as e:
            raise VaultError(e.reason, e.hint)
        self._commit_manifest_change(m)
        return {"item": items_mod.item_view(out["item"]),
                "working_rev": out["working_rev"]}

    def set_part_number_prefix(self, prefix: str) -> dict[str, Any]:
        """Configure the part-number scheme (T-R5 locked decision: numbers
        auto-suggest from a configurable scheme, e.g. ``PRJ-00042``).

        Only the prefix is configurable in v1; the counter stays the
        zero-padded max+1 rule in :func:`manifest.next_part_number`. Existing
        item numbers are never rewritten — the scheme applies to future
        suggestions only. Returns ``{part_number_prefix, next_part_number}``.
        """
        clean = "".join(ch for ch in prefix.upper() if ch.isalnum())
        if not clean:
            raise VaultError("invalid_mode",
                             "a part-number prefix needs letters or digits")
        m = self.manifest()
        if m.get("part_number_prefix") != clean:
            m["part_number_prefix"] = clean
            self._commit_manifest_change(m)
            m = self.manifest()
        return {"part_number_prefix": m["part_number_prefix"],
                "next_part_number": manifest.next_part_number(m)}

    def discard_working_rev(self, item_id: str) -> dict[str, Any]:
        """Undo a "start rev C" draft: return the working file to the item's
        last released form and mark it Released again (T-R5 locked decision —
        the auto-branch toast's "back to rev B (discard draft)" affordance).

        Forward-only like everything else: the return itself is a new history
        entry, so even a discarded draft can be recovered from the timeline.
        """
        m = self.manifest()
        item = manifest.find_item(m, item_id)
        if item is None:
            raise VaultError("invalid_item", f"no such item: {item_id}")
        pins = [rr for rr in item.get("released_revs", []) or [] if rr.get("commit")]
        if not pins:
            raise VaultError("not_found",
                             "this design has no released version to return to")
        if item["lifecycle"] == manifest.LIFECYCLE_RELEASED:
            return {"item": items_mod.item_view(item), "entry": None}
        res = self.restore(pins[-1]["commit"], item_id)
        m = self.manifest()
        item = manifest.find_item(m, item_id)
        item["lifecycle"] = manifest.LIFECYCLE_RELEASED
        self._commit_manifest_change(m)
        return {"item": items_mod.item_view(item), "entry": res["entry"]}

    # -- generic (non-CAD) items: docs, code, linked repos (2026-07) --------

    def add_item(self, rel_path: str, *, doc_type: str | None = None,
                 part_number: str | None = None) -> dict[str, Any]:
        """Track a non-CAD file or folder as a versioned item (contract §2).

        CAD ``.step+``/``.ecad+`` containers are registered automatically on
        save; this is the explicit "Track this file" path for everything else —
        specs, reports, spreadsheets, images, simulation folders, source trees.
        ``doc_type`` is auto-detected (:mod:`doctypes`) when omitted and is
        always user-retaggable. Returns the item view.
        """
        rel = str(rel_path).replace("\\", "/")
        abs_path = generic_snapshot.contained_path(self.project_dir, rel)
        detected_type = doctypes.detect_doc_type(str(abs_path))
        dt = doc_type or detected_type
        if (detected_type in doctypes.CONTAINER_DOC_TYPES
                or dt in doctypes.CONTAINER_DOC_TYPES):
            raise VaultError("invalid_mode", CONTAINER_ITEM_ADD_HINT)
        m = self.manifest()
        existing = manifest.find_item_by_path(m, rel)
        if existing is not None:
            view = items_mod.item_view(existing)
            view["already_tracked"] = True
            return view
        if not abs_path.exists():
            raise VaultError("not_found", f"there is nothing to track at {rel}")
        item = manifest.register_item(
            m, kind=doctypes.kind_for(dt), path=rel, doc_type=dt)
        if part_number:
            try:
                items_mod.set_part_number(m, item["item_id"], part_number)
            except items_mod.ItemRuleError as e:
                raise VaultError(e.reason, e.hint)
        self._commit_manifest_change(m)
        m = self.manifest()
        return items_mod.item_view(manifest.find_item(m, item["item_id"]))

    def untrack_item(self, item_id: str) -> dict[str, Any]:
        """Stop tracking an item (forward-only: history keeps its old versions,
        only the live registry drops it — a restore of an earlier point still
        recovers it). Returns the removed item view."""
        m = self.manifest()
        try:
            item = items_mod.untrack_item(m, item_id)
        except items_mod.ItemRuleError as e:
            raise VaultError(e.reason, e.hint)
        self._commit_manifest_change(m)
        return items_mod.item_view(item)

    def link_repo(self, url: str, name: str,
                  checkout_path: str | None = None) -> dict[str, Any]:
        """Link an external source repository by reference (contract §2 link).

        Stores only a reference (``link.json``) — never the code itself. At
        release the current upstream version is pinned (best-effort). The
        upstream version id is never surfaced to users as-is (contract §8.1).
        ``checkout_path`` (a local working copy, for pinning) is a per-machine
        detail recorded in the vault-local config, not the versioned tree.
        """
        url = str(url or "").strip()
        name = str(name or "").strip() or "Linked code"
        if not url:
            raise VaultError("invalid_mode", "a link needs a location")
        from services.plm.git_security import reject_location_credentials

        try:
            reject_location_credentials(url)
        except ValueError as exc:
            raise VaultError("invalid_mode", str(exc)) from exc
        m = self.manifest()
        item = manifest.register_item(
            m, kind=doctypes.kind_for("linked_repo"), path=name, doc_type="linked_repo")
        iid = item["item_id"]
        inject = {iid: {generic_snapshot.link_path(iid):
                        generic_snapshot.new_link_doc(url, name)}}
        files = self._explode_project_into_shadow(
            m, kind=hist.KIND_AUTO, message="", inject=inject)
        author = _user_author()
        emeta = gitbind.EntryMeta(
            kind=hist.KIND_AUTO, message="",
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local')
        self._bind.commit_tree(files, emeta)
        if checkout_path:
            self.set_local_config({"link_checkouts": {iid: str(checkout_path)}})
        m = self.manifest()
        return items_mod.item_view(manifest.find_item(m, iid))

    # -- vault-local (per-machine, unversioned) config ----------------------

    def get_local_config(self) -> dict[str, Any]:
        """Per-machine settings kept beside the vault (never in the shadow tree,
        never shared): visible columns, link checkout paths, last page. Returns
        ``{}`` when none is set."""
        import json as _json
        cfg_path = self._bind.vault_dir / "config.json"
        if not cfg_path.is_file():
            return {}
        try:
            cfg = _json.loads(cfg_path.read_text("utf-8"))
            from services.plm.git_security import SECRET_FIELDS, reject_location_credentials

            for approval in (cfg.get("approved_remotes") or {}).values():
                try:
                    if any(value for key, value in approval.items() if key.casefold() in SECRET_FIELDS):
                        raise ValueError("credentialed approval")
                    if isinstance(approval.get("url"), str):
                        reject_location_credentials(approval["url"])
                except ValueError:
                    approval["url"] = None
                    for key in list(approval):
                        if key.casefold() in SECRET_FIELDS:
                            del approval[key]
            return cfg
        except Exception:
            return {}

    def set_local_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge *patch* into the vault-local config (one level deep for dicts)."""
        import json as _json
        cfg_path = self._bind.vault_dir / "config.json"
        with _local_config_lock(cfg_path.with_name(cfg_path.name + ".lock")):
            cfg = self.get_local_config()
            for k, v in (patch or {}).items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
            tmp: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=cfg_path.parent,
                    prefix=f".{cfg_path.name}.", suffix=".tmp", delete=False,
                ) as handle:
                    tmp = Path(handle.name)
                    handle.write(_json.dumps(cfg, indent=2, sort_keys=True))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, cfg_path)
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            return cfg

    # -- planning store: tasks / issues / milestones (aicad.planning/1) ------

    def plan(self, op: str, entity: str, *, data: dict[str, Any] | None = None,
             ent_id: str | None = None, actor: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read or mutate the project planning store (contract plm_planning.md).

        ``op`` ∈ list | create | update | delete; ``entity`` ∈ task | issue |
        milestone. Mutations cut a planning-scoped history entry so the board is
        versioned/restore-safe/release-frozen with the design, but stays out of
        the curated design timeline (``plm_history`` default scope). Every
        mutation routes through the :mod:`permissions` seam (allow-all today).
        """
        op = str(op or "list")
        head = self._bind.head_commit()
        try:
            fname = planning.file_name(entity)
            raw = self._bind.read_blob_at(head, fname) if head else None
            coll = planning.load(raw, entity)
            if op == "list":
                return {"items": planning.entities(coll, entity)}
            action = {"create": "create", "update": "edit", "delete": "delete"}.get(op)
            if action is None:
                raise VaultError("invalid_mode", f"unknown planning action: {op!r}")
            permissions.require(actor, action, "planning")
            now = _now_iso()
            made: dict[str, Any] | None = None
            if op == "create":
                made = planning.create(coll, entity, data or {}, actor, now)
            elif op == "update":
                made = planning.update(coll, entity, str(ent_id or ""), data or {}, now)
            else:  # delete
                planning.delete(coll, entity, str(ent_id or ""))
        except planning.PlanningError as e:
            raise VaultError(e.reason, e.hint)
        except permissions.PermissionDenied as e:  # never in v1
            raise VaultError(e.reason, e.hint)
        self._commit_sibling(fname, planning.dump(coll), scope=hist.SCOPE_PLANNING)
        out: dict[str, Any] = {"items": planning.entities(coll, entity)}
        if made is not None:
            out["entity"] = made
        return out

    def overview(self, *, mine: dict[str, Any] | None = None) -> dict[str, Any]:
        """One round trip for the hub dashboard: unsaved items, recent design
        activity, open tasks/issues, and the next milestone."""
        m = self.manifest()
        dirty = self._dirty_items(m)
        head = self._bind.head_commit()
        # Path-direct blob reads — do not flatten the whole HEAD tree just for
        # three planning JSON files (ME489-scale trees are hundreds of blobs).
        def _plan_blob(name: str) -> bytes | None:
            if head is None:
                return None
            return self._bind.read_blob_at(head, f"planning/{name}")
        tasks = planning.entities(
            planning.load(_plan_blob("tasks.json"), "task"), "task")
        issues = planning.entities(
            planning.load(_plan_blob("issues.json"), "issue"), "issue")
        miles = planning.entities(
            planning.load(_plan_blob("milestones.json"), "milestone"), "milestone")
        open_tasks = [t for t in tasks if t.get("status") != "done"]
        open_issues = [i for i in issues if i.get("state") == "open"]
        open_ms = sorted(
            (ms for ms in miles if ms.get("status") == "open"),
            key=lambda ms: (ms.get("due") is None, ms.get("due") or ""))
        return {
            "head": head,
            "ai_mode": m.get("ai_mode", "assisted"),
            "dirty_items": [items_mod.item_view(it) for d in dirty
                            if (it := manifest.find_item(m, d)) is not None],
            "recent_entries": self.history(limit=5, scope=hist.SCOPE_DESIGN),
            "open_tasks": open_tasks,
            "open_issues": open_issues,
            "next_milestone": open_ms[0] if open_ms else None,
            "counts": {"items": len(m["items"]), "dirty": len(dirty),
                       "open_tasks": len(open_tasks), "open_issues": len(open_issues),
                       "open_milestones": len(open_ms)},
        }

    def _commit_sibling(self, path: str, blob: bytes, *, scope: str) -> str:
        """Commit a change to a single versioned sibling path (e.g. a planning
        file) by carrying the HEAD tree forward verbatim and overriding just
        that path. Does NOT re-read working design files, so a planning edit
        never captures unsaved design changes. Respects the geometry.step
        release-only invariant (§2) by not carrying it forward."""
        _require_repaired_remotes(self.manifest())
        head = self._bind.head_commit()
        if head is not None:
            files = {p: d for p, d in self._bind.read_tree(head).items()
                     if not p.endswith("/geometry.step")}
        else:
            files = {"manifest.json": manifest.dumps(self.manifest())}
        files[path] = blob
        author = _user_author()
        emeta = gitbind.EntryMeta(
            kind=hist.KIND_AUTO, message="",
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local',
            scope=scope)
        return self._bind.commit_tree(files, emeta)

    def _commit_manifest_change(self, m: dict[str, Any], *, remote_repair: bool = False) -> str:
        """Record a manifest change; only explicit remote replacement may repair.

        Unreplaced unsafe profiles are stored disabled, with no destination.
        Diagnostic markers are local-only and never enter the peer schema.
        """
        if remote_repair:
            m = dict(m)
            m["remotes"] = [dict(profile) for profile in m.get("remotes") or []]
            for profile in m["remotes"]:
                if profile.pop("credential_repair_required", False):
                    profile["url"] = None
        files = self._explode_project_into_shadow(m, kind=hist.KIND_AUTO, message="")
        author = _user_author()
        emeta = gitbind.EntryMeta(
            kind=hist.KIND_AUTO, message="",
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local',
        )
        commit = self._bind.commit_tree(files, emeta)
        return commit

    # -- deterministic message drafting (T-R3) ------------------------------

    def draft_message(self, item_id: str | None = None) -> str:
        """Deterministic plain-language draft of "what changed since the last
        entry" — the template behind Copilot-drafted checkpoint messages.

        Numbers are copied from the semantic diff, never invented (Pillar 13).
        Returns ``""`` when nothing changed (an empty draft, not filler).
        """
        m = self.manifest()
        head = self._bind.head_commit()
        head_tree = self._bind.read_tree(head) if head else {}
        parts: list[str] = []
        for it in m["items"]:
            if item_id is not None and it["item_id"] != item_id:
                continue
            exploded = self._working_exploded(it)
            if exploded is None:
                continue
            prev_raw = head_tree.get(snapshot.item_prefix(it["item_id"]) + "document.json")
            if prev_raw is not None and prev_raw == exploded.document_json:
                continue  # unchanged item — nothing to say
            parts.append(self._item_summary(prev_raw, exploded.document_json, it["path"]))
        return " ".join(p for p in parts if p and p != "No design changes.")

    def draft_release_note(self, item_id: str) -> str:
        """Deterministic draft of a release note: everything that changed on
        this item since its last released revision (or its first version)."""
        m = self.manifest()
        item = manifest.find_item(m, item_id)
        if item is None:
            raise VaultError("invalid_item", f"no such item: {item_id}")
        exploded = self._working_exploded(item)
        if exploded is None:
            return ""
        base_raw: bytes | None = None
        pins = [rr for rr in item.get("released_revs", []) or [] if rr.get("commit")]
        if pins:
            base_tree = self._bind.read_tree(pins[-1]["commit"])
            base_raw = base_tree.get(snapshot.item_prefix(item_id) + "document.json")
        return self._item_summary(base_raw, exploded.document_json, item["path"])

    def restore(self, commit: str, item_id: str | None = None) -> dict[str, Any]:
        """Forward-only restore (contract §3.3): re-assemble the working files
        from a prior entry's tree, then cut a NEW ``restore`` entry. History is
        never rewound."""
        _require_repaired_remotes(self.manifest())
        try:
            src_tree = self._bind.read_tree(commit)
        except KeyError:
            raise VaultError("not_found", "that point in history could not be found")
        m_raw = src_tree.get("manifest.json")
        m = manifest.loads(m_raw) if m_raw else self.manifest()

        # Re-assemble the working container files from the restored tree.
        targets = m["items"] if item_id is None else [it for it in m["items"] if it["item_id"] == item_id]
        if item_id is not None and not targets:
            raise VaultError("invalid_item", f"no such item: {item_id}")
        for it in targets:
            if it.get("path"):
                generic_snapshot.contained_path(self.project_dir, it["path"])
        for it in targets:
            dt = it.get("doc_type")
            if dt and dt in doctypes.GENERIC_DOC_TYPES:
                generic_snapshot.materialize(it, src_tree, self.project_dir)
                continue
            try:
                exploded = snapshot.read_item_from_tree(src_tree, it["item_id"])
            except KeyError:
                continue  # item not present at that point (e.g. added later)
            container = snapshot.reassemble_container(exploded)
            dst = generic_snapshot.contained_path(self.project_dir, it["path"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(container)

        # Append a new forward restore entry (never rewrite). Plain-language
        # "when" — no git vocabulary, no hashes (contract §8.1).
        when = self._readable_when(commit)
        return self.save(hist.KIND_RESTORE, message=f"Restored to {when}", item_id=item_id)

    def _readable_when(self, commit: str) -> str:
        """A plain-language description of a history point (no hash surfaced)."""
        try:
            cmeta = self._bind.commit_meta(commit)
            return time.strftime("%d %b %Y, %H:%M", time.gmtime(cmeta["authored_at"]))
        except Exception:
            return "an earlier version"

    def release(self, item_id: str, message: str, *,
                geometry_step: bytes | None = None,
                author: dict[str, Any] | None = None,
                skip_preflight: bool = False,
                bom_pins: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Release an item (contract §5): rev stamp + lifecycle + geometry
        freeze + (for assemblies) the BOM freeze pin set.

        This is the atomic release ENTRY. The full T-R4 flow — pre-flight
        battery, component-pin computation, stamp orders — is orchestrated by
        :func:`services.plm.release.release`, which passes ``bom_pins`` here.
        The In-work → Released transition is enforced: releasing an
        already-Released item needs "start a new revision" first (§4).
        """
        m = self.manifest()
        item = manifest.find_item(m, item_id)
        if item is None:
            raise VaultError("invalid_item", f"no such item: {item_id}")
        try:
            items_mod.require_in_work_for_release(item)
        except items_mod.ItemRuleError as e:
            raise VaultError(e.reason, e.hint)
        if not message and assist.allowed(self.ai_mode(), "draft_message"):
            # assisted+ tiers: deterministically draft the release note from
            # the changes since the last released revision (user-editable —
            # an explicit message always wins; manual tier never drafts).
            message = self.draft_release_note(item_id)
        if not message:
            raise VaultError("preflight_failed", "a release needs a note")

        # rev stamp + lifecycle transition (contract §5.2, §5.3)
        new_rev = manifest.next_rev_letter(item.get("rev"))
        prior_rev = item.get("rev")
        head = self._bind.head_commit()
        if prior_rev:
            for rr in item["released_revs"]:
                if rr["rev"] == prior_rev:
                    rr["lifecycle"] = manifest.LIFECYCLE_SUPERSEDED
        item["rev"] = new_rev
        item["lifecycle"] = manifest.LIFECYCLE_RELEASED
        item["released_revs"].append({
            "rev": new_rev, "commit": None, "released_at": _now_iso(),
            "lifecycle": manifest.LIFECYCLE_RELEASED,
        })

        # BOM freeze (contract §5.5): pin the component reference set to the
        # exact revs computed by services.plm.release.component_pins.
        freeze = None
        if bom_pins is not None:
            freeze = {"assembly_item": item_id, "rev": new_rev, "commit": None,
                      "pins": bom_pins}
            m.setdefault("bom_freezes", []).append(freeze)

        # explode project, freezing geometry.step for the released item only (§5.4)
        files = self._explode_project_into_shadow(
            m, kind=hist.KIND_RELEASE, message=message, author=author,
            geometry_for={item_id: geometry_step} if geometry_step is not None else None,
        )
        author = author or _user_author()
        emeta = gitbind.EntryMeta(
            kind=hist.KIND_RELEASE, message=message,
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local',
        )
        commit = self._bind.commit_tree(files, emeta)

        # Back-fill the release commit id into the rev pin. A commit cannot embed
        # its own hash, so the manifest just committed still has commit:null for
        # this rev. Persist the pin with a tiny follow-up commit that rewrites ONLY
        # manifest.json over the same tree (append-only-safe; the release stays the
        # authoritative geometry-freeze entry, this just resolves the pointer).
        # released_revs[].commit MUST be non-null in the stored manifest so §4/§5
        # readers can locate the frozen geometry.step later.
        item["released_revs"][-1]["commit"] = commit
        if freeze is not None:
            freeze["commit"] = commit
        files["manifest.json"] = manifest.dumps(m)
        pin_meta = gitbind.EntryMeta(
            kind=hist.KIND_RELEASE,
            message=f"{message}\n\n{hist.PIN_TRAILER} 1",
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local',
        )
        pinned_commit = self._bind.commit_tree(files, pin_meta)

        return {"commit": pinned_commit, "release_commit": commit, "rev": new_rev,
                "entry": self._entry_view(pinned_commit, self._bind.read_tree(pinned_commit), item_id),
                "prior_rev": prior_rev}

    # -- maintenance -------------------------------------------------------

    # -- lightweight branching (wave-2) ------------------------------------

    def branch_list(self) -> dict[str, Any]:
        """List named design branches (refs into the content-addressed vault)."""
        from services.plm import branches as branches_mod
        return branches_mod.list_branches(self)

    def branch_create(self, name: str, *, from_ref: str | None = None) -> dict[str, Any]:
        from services.plm import branches as branches_mod
        return branches_mod.create_branch(self, name, from_ref=from_ref)

    def branch_switch(self, name: str) -> dict[str, Any]:
        from services.plm import branches as branches_mod
        return branches_mod.switch_branch(self, name)

    def branch_merge(self, src: str, dst: str, *, message: str = "") -> dict[str, Any]:
        from services.plm import branches as branches_mod
        return branches_mod.merge_branches(self, src, dst, message=message)

    def active_branch(self) -> str:
        from services.plm import branches as branches_mod
        return branches_mod.active_branch(self)

    def tag_version(self, commit: str | None = None, name: str | None = None,
                    message: str = "") -> dict[str, Any]:
        from services.plm import versions
        return versions.tag_version(self, commit=commit, name=name, message=message)

    def list_versions(self) -> list[dict[str, Any]]:
        from services.plm import versions
        return versions.list_versions(self)

    def version_graph(self, limit: int = 400, scope: str = "all") -> dict[str, Any]:
        """Return the decorated multi-parent commit DAG."""
        from services.plm import branches as branches_mod

        head = self._bind.head_commit()
        branch_rows = branches_mod.branch_topology(self)
        version_rows = self.list_versions()
        starts = [b["head"] for b in branch_rows if b.get("head")]
        starts.extend(v["commit"] for v in version_rows if v.get("commit"))
        if head:
            starts.append(head)
        commits = self._bind.log_dag(starts, max(0, int(limit)))
        if scope != "all":
            commits = [c for c in commits if hist.scope_from_commit_message(
                self._bind.commit_meta(c)["message"]) == scope]

        decorations: dict[str, list[dict[str, str]]] = {}
        for branch in branch_rows:
            if branch.get("head"):
                decorations.setdefault(branch["head"], []).append(
                    {"type": "branch", "name": branch["name"]})
        for version in version_rows:
            decorations.setdefault(version["commit"], []).append(
                {"type": "version", "name": version["name"]})
        if head:
            decorations.setdefault(head, []).append(
                {"type": "head", "name": branches_mod.active_branch(self)})

        release_by_commit: dict[str, dict[str, str]] = {}
        manifest_raw = self._bind.read_blob_at(head, "manifest.json") if head else None
        if manifest_raw:
            current_manifest = manifest.loads_local(manifest_raw)
            for item in current_manifest.get("items", []):
                for released in item.get("released_revs", []) or []:
                    commit = released.get("commit")
                    if commit:
                        release_by_commit[commit] = {
                            "item_id": item.get("item_id", ""),
                            "rev": released.get("rev", ""),
                        }
                        decorations.setdefault(commit, []).append(
                            {"type": "release", "name": released.get("rev", "")})

        def legacy_pin(commit: str, parents: list[str]) -> bool:
            if len(parents) != 1:
                return False
            tree = self._bind.read_tree(commit)
            parent_tree = self._bind.read_tree(parents[0])
            changed = {p for p in set(tree) | set(parent_tree)
                       if tree.get(p) != parent_tree.get(p)}
            return changed == {"manifest.json"}

        nodes: list[dict[str, Any]] = []
        for commit in commits:
            cmeta = self._bind.commit_meta(commit)
            entry = self._entry_view_light(commit, meta=cmeta)
            parents = cmeta["parents"]
            pin = hist.pin_from_commit_message(cmeta["message"])
            if not pin and entry["kind"] == hist.KIND_RELEASE:
                pin = legacy_pin(commit, parents)
            ai = entry.get("ai") or {}
            nodes.append({
                **entry,
                "ai": {
                    "provider": str(ai.get("provider") or ""),
                    "model": str(ai.get("model") or ""),
                    "run_id": str(ai.get("run_id") or ""),
                },
                "parents": parents,
                "refs": decorations.get(commit, []),
                "release": release_by_commit.get(commit),
                "pin": pin,
            })
        return {
            "head": head or "",
            "active_branch": branches_mod.active_branch(self),
            "truncated": len(commits) >= max(0, int(limit)),
            "nodes": nodes,
            "branches": branch_rows,
            "versions": version_rows,
        }


    def repack(self) -> str:
        return self._bind.repack()

    # -- internals ---------------------------------------------------------

    def _seed_initial_project(self) -> None:
        """On first open, register existing containers and cut the seed entry."""
        m = manifest.new_manifest(self.project_dir.name, _now_iso())
        containers = self._project_containers()
        if not containers:
            # empty project — still commit an empty manifest so HEAD exists
            files = {"manifest.json": manifest.dumps(m)}
            emeta = gitbind.EntryMeta(kind=hist.KIND_AUTO, message="",
                                      author_name="System", author_email="system@aicad.local")
            self._bind.commit_tree(files, emeta)
            return
        # register each container, then cut one auto entry over all of them
        files = self._explode_project_into_shadow(m, kind=hist.KIND_AUTO, message="")
        emeta = gitbind.EntryMeta(kind=hist.KIND_AUTO, message="",
                                  author_name="System", author_email="system@aicad.local")
        self._bind.commit_tree(files, emeta)

    def _project_containers(self) -> list[Path]:
        """Every versioned container file under the project root, path-sorted.

        Dot-prefixed directories (``.aicadvault``, ``.git``, ``.plm``, …) are
        pruned at walk time — the same policy as :func:`_tree_working_stamp`.
        The old ``rglob('*')`` enumerated *and stat'ed* every object in the
        vault's own store before throwing the results away, on every save.
        The result set is unchanged: dot-prefixed names, at any depth, never
        qualified.
        """
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.project_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                if not any(fn.endswith(s) for s in _CONTAINER_SUFFIXES):
                    continue
                rel = (Path(dirpath) / fn).relative_to(self.project_dir).as_posix()
                generic_snapshot.contained_path(self.project_dir, rel)
                p = Path(dirpath) / fn
                try:
                    if p.is_file():  # skip dangling symlinks / non-regular entries
                        out.append(p)
                except OSError:
                    # Discovery is best-effort. Tracked paths are still checked
                    # explicitly in _explode_project_into_shadow on save.
                    continue
        out.sort()
        return out

    def _explode_project_into_shadow(
        self, m: dict[str, Any], *, kind: str, message: str,
        author: dict[str, Any] | None = None, ai: dict[str, Any] | None = None,
        ai_by_item: dict[str, dict[str, Any]] | None = None,
        participants: list[dict[str, Any]] | None = None,
        geometry_for: dict[str, bytes] | None = None,
        enforce_released_lock: bool = False,
        inject: dict[str, dict[str, bytes]] | None = None,
        inject_paths: dict[str, bytes] | None = None,
    ) -> dict[str, bytes]:
        """Explode every project container into the shadow tree + manifest.

        Registers newly-seen containers into ``m`` (zero-config first check-in).
        Generic (non-container) items are re-emitted here too, per the
        carry-forward law (:mod:`generic_snapshot`) — a whole-tree commit that
        omitted them would drop them from that point in history. Any versioned
        sibling trees (e.g. ``planning/``) are carried forward verbatim.
        Returns the whole ``{path: bytes}`` shadow tree to commit.
        """
        _require_repaired_remotes(m)
        author = author or _user_author()
        geometry_for = geometry_for or {}
        inject = inject or {}
        ai_block = ai if (ai and author.get("kind") == "ai") else None
        item_ai_blocks = ai_by_item if author.get("kind") == "ai" else None
        files: dict[str, bytes] = {}
        head = self._bind.head_commit()
        head_tree = self._bind.read_tree(head) if head else {}
        try:
            container_paths = {
                path.relative_to(self.project_dir).as_posix(): path
                for path in self._project_containers()
            }
        except OSError as exc:
            raise VaultError(
                "snapshot_failed",
                f"could not scan tracked project items: {exc}",
            ) from exc
        # Discovery is best-effort for new files, but manifest-owned containers
        # are read by their exact path so permission/stat failures cannot make
        # them disappear from a replacement tree.
        for tracked_item in m["items"]:
            rel = tracked_item.get("path", "")
            dt = tracked_item.get("doc_type")
            if (rel and (not dt or dt in doctypes.CONTAINER_DOC_TYPES)
                    and any(rel.endswith(s) for s in _CONTAINER_SUFFIXES)):
                container_paths.setdefault(
                    rel, generic_snapshot.contained_path(self.project_dir, rel))

        import json as _json
        root_key = _vault_cache_key(self.project_dir)
        for rel, path in sorted(container_paths.items()):
            path = generic_snapshot.contained_path(self.project_dir, rel)
            item = manifest.find_item_by_path(m, rel)
            # Stat first: an unchanged container is reused from the process
            # explode cache instead of being re-read and re-exploded. Any
            # change, a new file, or an unstattable path falls through to the
            # full read below.
            cache_key = f"{root_key}\x00{rel}"
            stamp = _container_stamp(path)
            cached = _explode_cache_get(cache_key, stamp)
            if cached is not None:
                exploded = cached["exploded"]
                doc = cached["doc"]
                if doc is None:
                    doc = _json.loads(exploded.document_json.decode("utf-8"))
                    cached["doc"] = doc
            else:
                try:
                    raw = path.read_bytes()
                except FileNotFoundError:
                    _explode_cache_drop(cache_key)
                    continue  # an actually missing tracked path is an intentional deletion
                except OSError as exc:
                    if item is None:
                        continue  # first discovery of unregistered junk remains best-effort
                    raise VaultError(
                        "snapshot_failed",
                        f"could not read tracked item {rel}: {exc}",
                    ) from exc
                try:
                    exploded = snapshot.explode_container(raw)
                    doc = _json.loads(exploded.document_json.decode("utf-8"))
                except Exception as exc:
                    _explode_cache_drop(cache_key)
                    if item is None:
                        continue  # first discovery of an invalid container is harmless
                    raise VaultError(
                        "corrupt_item",
                        f"tracked item {rel} is unreadable or corrupt",
                    ) from exc
                if stamp is not None:
                    _explode_cache_put(cache_key, stamp, exploded, doc)
            doc_uuid = doc.get("id", "")
            schema_kind = _kind_from_schema(doc.get("schema", ""))
            if item is None:
                suffix = next((s for s in _CONTAINER_SUFFIXES if rel.endswith(s)), "")
                item = manifest.register_item(
                    m, doc_uuid=doc_uuid,
                    kind=schema_kind or _SUFFIX_KIND.get(suffix, "part"), path=rel)
            elif schema_kind and item["kind"] != schema_kind:
                item["kind"] = schema_kind  # refine a suffix-guessed kind
            iid = item["item_id"]
            meta_json = hist.make_meta(
                item_id=iid, entry_kind=kind, authored_at=_now_iso(),
                author_kind=author.get("kind", "user"), author_id=author["id"],
                author_display=author["display"], message=message,
                doc_uuid=doc_uuid, rev=item.get("rev"), lifecycle=item["lifecycle"],
                ai=(item_ai_blocks or {}).get(iid, ai_block),
                participants=participants,
            )
            # plain-language change summary per entry (T-R3, contract §2
            # summary.txt): deterministic, numerals copied from doc_diff.
            prev_raw = head_tree.get(snapshot.item_prefix(iid) + "document.json")
            if (enforce_released_lock and prev_raw is not None
                    and prev_raw != exploded.document_json):
                try:
                    items_mod.guard_released_edit(m, [iid])
                except items_mod.ItemRuleError as e:
                    raise VaultError(e.reason, _released_edit_hint(e, iid))
            summary = self._item_summary(prev_raw, exploded.document_json, rel)
            files.update(snapshot.item_paths(
                iid, exploded, meta_json=meta_json,
                summary_txt=summary.encode("utf-8"),
                geometry_step=geometry_for.get(iid),
            ))

        # generic (non-container) items — the carry-forward law (contract §2):
        # re-emit their content every commit (re-reading the working file, or
        # carrying the HEAD blobs forward) so a whole-tree commit never drops
        # them from that point in history.
        for it in m["items"]:
            dt = it.get("doc_type")
            if not dt or dt in doctypes.CONTAINER_DOC_TYPES:
                continue  # container item (handled above) or legacy CAD item
            iid = it["item_id"]
            injected = inject.get(iid)
            try:
                content = (injected if injected is not None
                           else generic_snapshot.content_paths(
                               it, self.project_dir, head_tree))
            except OSError as exc:
                raise VaultError(
                    "snapshot_failed",
                    f"could not read tracked item {it['path']}: {exc}",
                ) from exc
            old_content = generic_snapshot.head_content(iid, head_tree)
            if enforce_released_lock and old_content and content != old_content:
                try:
                    items_mod.guard_released_edit(m, [iid])
                except items_mod.ItemRuleError as e:
                    raise VaultError(e.reason, _released_edit_hint(e, iid))
            summary = generic_snapshot.summarize(it, old_content, content)
            meta_json = hist.make_meta(
                item_id=iid, entry_kind=kind, authored_at=_now_iso(),
                author_kind=author.get("kind", "user"), author_id=author["id"],
                author_display=author["display"], message=message,
                doc_uuid=it.get("doc_uuid", ""), rev=it.get("rev"),
                lifecycle=it["lifecycle"],
                ai=(item_ai_blocks or {}).get(iid, ai_block),
                participants=participants,
            )
            p = snapshot.item_prefix(iid)
            files.update(content)
            files[p + "meta.json"] = meta_json
            files[p + "summary.txt"] = summary.encode("utf-8")

        # carry forward any versioned sibling trees the explode does not own
        # (e.g. planning/ — written via the dedicated planning path). Whole-tree
        # commits must include them or they vanish from that point in history.
        for path, data in head_tree.items():
            top = path.split("/", 1)[0]
            if top in _CARRY_FORWARD_TOPLEVEL and path not in files:
                files[path] = data

        # explicit sibling-path writes (e.g. planning files) override the
        # carried-forward blobs for exactly the paths being mutated.
        for path, data in (inject_paths or {}).items():
            files[path] = data

        files["manifest.json"] = manifest.dumps(m)
        return files

    def _working_exploded(self, item: dict[str, Any]) -> snapshot.ExplodedItem | None:
        """Explode an item's current working container (None when unreadable)."""
        path = generic_snapshot.contained_path(self.project_dir, item["path"])
        if not path.is_file():
            return None
        try:
            return snapshot.explode_container(path.read_bytes())
        except Exception:
            return None

    def _item_summary(self, prev_raw: bytes | None, cur_raw: bytes, rel_path: str) -> str:
        """Deterministic summary between two canonical document blobs.

        Degrades to an empty string if the semantic diff engine is not
        importable — a missing summary must never block a save.
        """
        import json as _json
        try:
            cur_doc = _json.loads(cur_raw.decode("utf-8"))
            if prev_raw is None:
                return summarize.summarize_first_version(cur_doc, rel_path)
            if prev_raw == cur_raw:
                return "No design changes."
            prev_doc = _json.loads(prev_raw.decode("utf-8"))
            return summarize.summary_between_docs(prev_doc, cur_doc, rel_path)
        except Exception:
            return ""

    def _working_stamp(self, it: dict[str, Any]) -> object | None:
        """Stat fingerprint of an item's working path, or None if unreadable.

        Containers/files: ``(mtime_ns, ctime_ns, size, inode)``. Tree items:
        sorted per-file tuples under the exclusion walk. Links have
        no working file (content lives in the shadow tree only) — stamped as
        the sentinel ``("link",)`` so they still cache.
        """
        dt = it.get("doc_type")
        if dt and dt in doctypes.GENERIC_DOC_TYPES:
            store = doctypes.storage_form(dt)
            if store == "link":
                return ("link",)
            path = generic_snapshot.contained_path(self.project_dir, it["path"])
            if store == "tree":
                if not path.is_dir():
                    return ("missing",)
                return ("tree", _tree_working_stamp(path))
            # file
            try:
                if not path.is_file():
                    return ("missing",)
                st = path.stat()
                return ("file", int(st.st_mtime_ns), int(st.st_ctime_ns),
                        int(st.st_size), int(st.st_ino))
            except OSError:
                return ("missing",)
        path = generic_snapshot.contained_path(self.project_dir, it["path"])
        try:
            if not path.is_file():
                return None
            st = path.stat()
            return ("container", int(st.st_mtime_ns), int(st.st_ctime_ns),
                    int(st.st_size), int(st.st_ino))
        except OSError:
            return None

    def _dirty_items(self, m: dict[str, Any], *,
                     changes: dict[str, str] | None = None) -> list[str]:
        """Return item_ids whose working content differs from HEAD.

        When requested, populate ``changes`` from the same stat/content scan:
        A has a working file but no HEAD content, D has HEAD content but no
        working file, and M differs from HEAD. The legacy dirty list deliberately
        keeps its existing missing-file semantics.
        """
        head = self._bind.head_commit()
        if head is None:
            if changes is not None:
                for it in m["items"]:
                    changes[it["item_id"]] = "A"
            return [it["item_id"] for it in m["items"]]

        # Per-item verdicts survive HEAD moves: each is keyed by the item's committed CONTENT
        # (its HEAD blob ids minus the per-commit entry metadata every commit rewrites) and
        # its manifest record, so a checkpoint revalidates only the items whose content it
        # changed instead of re-exploding every container in the project.
        root_key = _vault_cache_key(self.project_dir)
        cached = _dirty_scan_cache.get(root_key)
        if cached is None:
            cached = {"head": None, "items": {}, "content_keys": {}}
            _dirty_scan_cache[root_key] = cached
        if cached.get("head") != head or "item_oids" not in cached:
            cached["head"] = head
            cached["item_oids"] = self._bind.child_ids(head, "items")
            live_oids = set(cached["item_oids"].values())
            cached["content_keys"] = {oid: key for oid, key in cached.get("content_keys", {}).items()
                                      if oid in live_oids}
        item_cache: dict[str, dict[str, Any]] = cached["items"]
        item_oids: dict[str, bytes] = cached["item_oids"]
        content_keys: dict[bytes, tuple] = cached["content_keys"]

        def head_content_key(iid: str) -> tuple:
            oid = item_oids.get(iid)
            if oid is None:
                return ()
            key = content_keys.get(oid)
            if key is None:
                try:
                    blobs = self._bind.tree_blob_ids(oid)
                except TypeError:  # not a directory: its own id is the content
                    blobs = {"": oid}
                key = tuple(sorted((path, blob) for path, blob in blobs.items()
                                   if path not in _PER_COMMIT_ITEM_FILES))
                content_keys[oid] = key
            return key

        dirty: list[str] = []
        live_ids: set[str] = set()

        for it in m["items"]:
            iid = it["item_id"]
            live_ids.add(iid)
            stamp = self._working_stamp(it)
            head_key = (head_content_key(iid), _item_record_key(it))
            dt = it.get("doc_type")
            store = doctypes.storage_form(dt) if dt in doctypes.GENERIC_DOC_TYPES else None
            head_has_content = False
            missing = False
            if changes is not None:
                prefix = snapshot.item_prefix(iid)
                if store == "link":
                    content_path = prefix + "link.json"
                elif store == "tree":
                    content_path = prefix + "tree.index.json"
                elif store == "file":
                    content_path = generic_snapshot._file_path(iid, it["path"])
                else:
                    content_path = prefix + "document.json"
                head_has_content = self._bind.entry_at(head, content_path) is not None
                if store != "link" and (stamp is None or stamp == ("missing",)):
                    # is_file()/is_dir() suppresses OS errors. Only a genuine
                    # absent path is a deletion; permission errors and wrong-kind
                    # existing paths are not.
                    path = generic_snapshot.contained_path(self.project_dir, it["path"])
                    try:
                        path.stat()
                    except (FileNotFoundError, NotADirectoryError):
                        missing = True
                    except OSError:
                        pass
            def item_change(is_dirty: bool) -> str:
                if missing:
                    return "D" if head_has_content else ""
                if not head_has_content and store != "link":
                    return "A"
                return "M" if is_dirty else ""
            if stamp is None:
                path = generic_snapshot.contained_path(self.project_dir, it["path"])
                try:
                    path.stat()
                except (FileNotFoundError, NotADirectoryError):
                    # An actually missing container is intentionally not dirty.
                    item_cache.pop(iid, None)
                    if changes is not None:
                        changes[iid] = item_change(False)
                    continue
                except OSError:
                    pass
                # Existing-but-unreadable and wrong-kind paths are not clean.
                item_cache[iid] = {"stamp": ("unreadable",), "dirty": True}
                dirty.append(iid)
                if changes is not None:
                    changes[iid] = item_change(True)
                continue

            prev = item_cache.get(iid)
            if (prev is not None and prev.get("stamp") == stamp
                    and prev.get("head_key") == head_key
                    and not _stat_cache_expired(prev)
                    and self._stamp_is_stable(stamp, prev.get("recorded_ns", 0))
                    and (changes is None or "content_changed" in prev or store == "link")):
                if prev.get("dirty"):
                    dirty.append(iid)
                if changes is not None:
                    changes[iid] = item_change(bool(prev.get("content_changed",
                                                              prev.get("dirty"))))
                continue

            # Full content compare (cache miss or working file changed).
            is_dirty = False
            content_changed = False
            if dt and dt in doctypes.GENERIC_DOC_TYPES:
                item_tree = self._bind.read_subtree(head, snapshot.item_prefix(iid))
                is_dirty = generic_snapshot.is_dirty(it, self.project_dir, item_tree)
                content_changed = is_dirty
            else:
                path = generic_snapshot.contained_path(self.project_dir, it["path"])
                head_path = snapshot.item_prefix(iid) + "document.json"
                entry = self._bind.entry_at(head, head_path)
                head_oid = entry[1] if entry is not None and entry[0] != 0o040000 else None
                try:
                    cache_key = f"{root_key}\x00{it['path']}"
                    container_stamp = stamp[1:]
                    hit = _explode_cache_get(cache_key, container_stamp)
                    if hit is not None:
                        exploded = hit["exploded"]
                        is_dirty = Blob.from_string(exploded.document_json).id != head_oid
                    else:
                        head_doc = None

                        def reference_document(raw: bytes) -> bytes | None:
                            nonlocal head_doc
                            head_doc = (raw if Blob.from_string(raw).id == head_oid
                                        else self._bind.read_blob_at(head, head_path))
                            return head_doc

                        exploded = snapshot.explode_container(
                            path, reference_document=reference_document)
                        is_dirty = head_doc != exploded.document_json
                        _explode_cache_put(cache_key, container_stamp, exploded)
                except Exception:
                    # A malformed tracked container is not clean: surface it as
                    # dirty so callers do not mistake corruption for no change.
                    is_dirty = True
                if changes is not None and not is_dirty:
                    prefix = snapshot.item_prefix(iid)
                    old = {path: oid for path, oid in self._bind.subtree_ids(head, prefix).items()
                           if path == prefix + "document.json"
                           or path == prefix + "thumbnail.png"
                           or path.startswith(prefix + "extra/")}
                    current = snapshot.item_paths(iid, exploded)
                    content_changed = any(
                        Blob.from_string(data).id != old.pop(name, None)
                        for name, data in current.items()
                    ) or bool(old)
                else:
                    content_changed = is_dirty

            entry = {"stamp": stamp, "head_key": head_key, "dirty": is_dirty,
                     "recorded_ns": time.time_ns(), "recorded_at": time.monotonic()}
            if changes is not None:
                entry["content_changed"] = content_changed
            item_cache[iid] = entry
            if is_dirty:
                dirty.append(iid)
            if changes is not None:
                changes[iid] = item_change(content_changed)

        # Drop stamps for items no longer in the manifest (untrack / restore).
        stale = [k for k in item_cache if k not in live_ids]
        for k in stale:
            del item_cache[k]
        return dirty

    def working_changes(self, m: dict[str, Any], *, doc_type: str = "", kind: str = ""
                        ) -> tuple[list[str], dict[str, str], list[dict[str, str]]]:
        """Read-only working-file state for the Files page."""
        changes: dict[str, str] = {}
        dirty = self._dirty_items(m, changes=changes)
        registered = {it["path"] for it in m["items"]}
        untracked: list[dict[str, str]] = []
        root_key = _vault_cache_key(self.project_dir)
        for path in self._project_containers():
            rel = path.relative_to(self.project_dir).as_posix()
            if rel in registered:
                continue
            stamp = _container_stamp(path)
            key = f"{root_key}\x00{rel}"
            hit = _explode_cache_get(key, stamp)
            try:
                if hit is None:
                    exploded = snapshot.explode_container(path)
                    doc = json.loads(exploded.document_json)
                    if not isinstance(doc, dict):
                        continue
                    if stamp is not None:
                        _explode_cache_put(key, stamp, exploded, doc)
                else:
                    doc = hit["doc"]
                    if doc is None:
                        doc = json.loads(hit["exploded"].document_json)
                        hit["doc"] = doc
                    if not isinstance(doc, dict):
                        continue
            except Exception:  # malformed/new file or unreadable: discovery is best-effort
                continue
            suffix = next(s for s in _CONTAINER_SUFFIXES if rel.endswith(s))
            inferred_kind = _kind_from_schema(doc.get("schema", "")) or _SUFFIX_KIND[suffix]
            if (kind and inferred_kind != kind) or (doc_type and inferred_kind != doc_type):
                continue
            untracked.append({"path": rel, "change": "U"})
        return dirty, changes, untracked

    @staticmethod
    def _stamp_is_stable(stamp: tuple, recorded_ns: int) -> bool:
        """Reject racily-clean files written in the comparison's clock tick."""
        if stamp[0] in ("container", "file"):
            return max(stamp[1], stamp[2]) < recorded_ns
        if stamp[0] == "tree":
            return all(max(row[1], row[2]) < recorded_ns for row in stamp[1])
        return True

    def _item_view(self, it: dict[str, Any]) -> dict[str, Any]:
        return items_mod.item_view(it)

    def _entry_view_light(self, commit: str, *, item_id: str | None = None,
                          meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Entry view without flattening the whole commit tree.

        Used by overview/full-timeline history. Pulls commit meta + one
        representative item's meta/summary via path-direct blob reads.
        Callers that already decoded the commit pass its *meta* in rather than
        paying for a second decode of the same object.
        """
        cmeta = meta if meta is not None else self._bind.commit_meta(commit)
        kind = hist.kind_from_commit_message(cmeta["message"])
        scope = hist.scope_from_commit_message(cmeta["message"])
        msg = hist.user_message_from_commit(cmeta["message"])
        author = {
            "kind": "user", "id": "user", "display": "User",
            "account_id": None,
        }
        ai = None
        participants = None
        thumb_ref = None
        summary = ""
        # Resolve the representative at this commit, never from today's manifest.
        pick_iid = item_id if item_id is not None else self._first_item_id_at(commit)
        if pick_iid is not None:
            prefix = snapshot.item_prefix(pick_iid)
            mraw = self._bind.read_blob_at(commit, prefix + "meta.json")
            if mraw:
                import json as _json
                try:
                    md = _json.loads(mraw.decode("utf-8"))
                    author = dict(md.get("author") or author)
                    author.setdefault("account_id", None)
                    ai = md.get("ai")
                    participants = md.get("participants")
                except Exception:
                    pass
            thumb_entry = self._bind.entry_at(commit, prefix + "thumbnail.png")
            if thumb_entry is not None and thumb_entry[0] != 0o040000:
                thumb_ref = prefix + "thumbnail.png"
            sraw = self._bind.read_blob_at(commit, prefix + "summary.txt")
            if sraw:
                summary = sraw.decode("utf-8")
        return {
            "commit": commit, "kind": kind, "scope": scope,
            "authored_at": _iso(cmeta["authored_at"]),
            "author": author, "ai": ai, "message": msg,
            "thumbnail_ref": thumb_ref, "summary": summary,
            **({"participants": participants} if participants is not None else {}),
        }

    def _first_item_id_at(self, commit: str) -> str | None:
        """First ``items/<id>/`` child name at *commit* without full flatten."""
        try:
            entry = self._bind.entry_at(commit, "items")
            if entry is None or entry[0] != 0o040000:
                return None
            items_tree = self._bind._read_object(entry[1])  # noqa: SLF001
            for entry in items_tree.items():
                name = entry.path.decode("utf-8", "replace")
                if name:
                    return name
        except Exception:
            return None
        return None

    def _entry_view(self, commit: str, tree: dict[str, bytes], item_id: str | None, *,
                    meta: dict[str, Any] | None = None) -> dict[str, Any]:
        cmeta = meta if meta is not None else self._bind.commit_meta(commit)
        kind = hist.kind_from_commit_message(cmeta["message"])
        scope = hist.scope_from_commit_message(cmeta["message"])
        msg = hist.user_message_from_commit(cmeta["message"])
        # pull author + ai from a representative item meta.json
        author = {
            "kind": "user", "id": "user", "display": "User",
            "account_id": None,
        }
        ai = None
        participants = None
        thumb_ref = None
        summary = ""
        pick_iid = item_id
        if pick_iid is None:
            for path in tree:
                if path.startswith("items/") and path.endswith("/meta.json"):
                    pick_iid = path.split("/")[1]
                    break
        if pick_iid is not None:
            mraw = tree.get(snapshot.item_prefix(pick_iid) + "meta.json")
            if mraw:
                import json as _json
                md = _json.loads(mraw.decode("utf-8"))
                author = dict(md.get("author") or author)
                author.setdefault("account_id", None)
                ai = md.get("ai")
                participants = md.get("participants")
            if snapshot.item_prefix(pick_iid) + "thumbnail.png" in tree:
                thumb_ref = snapshot.item_prefix(pick_iid) + "thumbnail.png"
            summary = (tree.get(snapshot.item_prefix(pick_iid) + "summary.txt") or b"").decode("utf-8")
        return {
            "commit": commit, "kind": kind, "scope": scope,
            "authored_at": _iso(cmeta["authored_at"]),
            "author": author, "ai": ai, "message": msg,
            "thumbnail_ref": thumb_ref, "summary": summary,
            **({"participants": participants} if participants is not None else {}),
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _user_author() -> dict[str, Any]:
    """Session-aware user author with an OS identity fallback.

    Author lives ONLY in meta/commit (byte-stability law §0) — never document.json.
    ``account_id`` is reserved now so later account-backed sharing does not need
    a shape change.
    """
    uid = os.environ.get("CHUDWARE_USER_ID") or getpass.getuser() or "user"
    display = os.environ.get("CHUDWARE_USER_DISPLAY") or uid
    account_id = os.environ.get("CHUDWARE_ACCOUNT_ID") or None
    return {
        "kind": "user",
        "id": uid,
        "display": display,
        "account_id": account_id,
    }
