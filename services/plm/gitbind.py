"""services.plm.gitbind — the git binding for the PLM vault (contract §7 verdict).

OSS survey verdict (docs/PLM_OSS_SURVEY.md, frozen in contracts/plm.md):

  * **dulwich (Apache-2.0 arm) is the PRIMARY, guaranteed binding.** Pure-Python,
    ``pip install``-only, no binary-on-PATH assumption — identical on Fedora and
    Windows. It is a hard ``requirements.txt`` dep of ``services/plm/``.
  * **subprocess ``git`` is an OPTIONAL accelerator**, feature-detected on PATH,
    used only for expensive operations (``gc``/``repack``) when present. Its
    absence degrades transparently to the pure-dulwich path. It is never bundled
    (GPL, subprocess-only mere-aggregation isolation).
  * pygit2 is NOT used (per-platform native wheel + GPL-linking-exception call).

This module exposes a thin :class:`GitBinding` that the rest of ``services.plm``
uses; nothing above it touches dulwich or git directly. The public vault surface
(contract §8) never surfaces any of this vocabulary to a user.

Design notes
------------
* The vault is a *bare-ish* repo: ``.aicadvault/`` holds the git object store and
  refs, but has no checked-out working tree — the working files live in the
  project folder and are exploded into the shadow tree in-memory on save
  (contract §2). We therefore build trees and commits directly from blobs rather
  than staging a work tree.
* History is linear and append-only (contract §3): every commit has exactly the
  current HEAD as its single parent. No branch/merge/amend/rebase is ever issued
  (contract §8.3).
"""
from __future__ import annotations

import heapq
import os
import shutil
import subprocess
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# dulwich is the PRIMARY binding — a hard dependency of services/plm/.
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from services.plm.git_security import git_argv, git_env

VAULT_DIRNAME = ".aicadvault"


class ConcurrentUpdate(Exception):
    """HEAD moved under a concurrent writer between our snapshot read and the CAS commit.
    Retryable: the caller should rebuild its shadow tree from fresh HEAD and commit again."""

# git binary is an OPTIONAL accelerator; feature-detected once.
_GIT_ON_PATH: bool | None = None
# Process-wide flattened-tree cache. Commit contents are content-addressed and
# immutable, so a tree once read is always valid. worker_rpc opens a fresh
# GitBinding per call, so this must live at module scope to help warm overview
# / history / manifest across RPCs. Values are treated as immutable; read_tree
# returns a shallow copy so callers may mutate their map safely.
_TREE_CACHE_MAX = 48
_tree_cache: dict[str, dict[str, bytes]] = {}
_tree_cache_order: list[str] = []


def _tree_cache_get(commit_hex: str) -> dict[str, bytes] | None:
    return _tree_cache.get(commit_hex)


def _tree_cache_put(commit_hex: str, tree: dict[str, bytes]) -> None:
    if commit_hex in _tree_cache:
        return
    _tree_cache[commit_hex] = tree
    _tree_cache_order.append(commit_hex)
    while len(_tree_cache_order) > _TREE_CACHE_MAX:
        old = _tree_cache_order.pop(0)
        _tree_cache.pop(old, None)


def _meta_from_commit(commit_hex: str, c: Commit) -> dict[str, Any]:
    """Commit-level metadata from an already-decoded commit object."""
    return {
        "commit": commit_hex,
        "authored_at": int(c.author_time),
        "author_raw": c.author.decode("utf-8", "replace"),
        "message": c.message.decode("utf-8", "replace"),
        "parents": [p.decode("ascii") for p in c.parents],
    }



def git_available() -> bool:
    """Return True when a system ``git`` binary is on PATH (optional accelerator)."""
    global _GIT_ON_PATH
    if _GIT_ON_PATH is None:
        _GIT_ON_PATH = shutil.which("git") is not None
    return _GIT_ON_PATH


@dataclass(frozen=True)
class EntryMeta:
    """The mutable metadata carried in a history commit (contract §3.4).

    NONE of this ever lands in ``document.json`` (the byte-stability law, §0).
    """

    kind: str  # auto | checkpoint | release | restore
    message: str
    author_name: str
    author_email: str
    authored_at: int | None = None  # epoch seconds; defaults to now
    scope: str = "design"  # design (timeline) | planning (tasks/issues/milestones)


class GitBinding:
    """Thin dulwich-backed binding over a single ``.aicadvault/`` repo.

    All history operations here are linear + append-only. The class holds an open
    :class:`dulwich.repo.Repo`; callers get and put whole shadow trees as
    ``{path: bytes}`` maps.
    """

    def __init__(self, repo: Repo, vault_dir: Path) -> None:
        self._repo = repo
        self._vault_dir = vault_dir
        # Path reads share decoded commit/tree objects within one RPC. Bound
        # metadata only: never retain geometry blobs here.
        self._read_cache: OrderedDict[bytes, Commit | Tree] = OrderedDict()

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open_or_create(cls, project_dir: str | os.PathLike) -> GitBinding:
        """Open the vault under *project_dir*, creating it silently if absent.

        Zero-config (contract §8.2): there is no init step, wizard, or prompt.
        """
        project = Path(project_dir)
        vault_dir = project / VAULT_DIRNAME
        if (vault_dir / "objects").is_dir() or (vault_dir / "HEAD").is_file():
            repo = Repo(str(vault_dir))
        else:
            vault_dir.mkdir(parents=True, exist_ok=True)
            # A bare repo: object store + refs, no working tree (the project
            # folder is the working tree; the shadow tree is committed directly).
            repo = Repo.init_bare(str(vault_dir))
            _hide_on_windows(vault_dir)
        return cls(repo, vault_dir)

    @classmethod
    def exists(cls, project_dir: str | os.PathLike) -> bool:
        vault_dir = Path(project_dir) / VAULT_DIRNAME
        return (vault_dir / "objects").is_dir() or (vault_dir / "HEAD").is_file()

    @property
    def vault_dir(self) -> Path:
        return self._vault_dir

    def close(self) -> None:
        self._repo.close()

    # -- reading -----------------------------------------------------------

    def head_commit(self) -> str | None:
        """Return the current HEAD commit id (hex), or None on an empty vault."""
        try:
            head = self._repo.head()
        except KeyError:
            return None
        return head.decode("ascii") if isinstance(head, bytes) else str(head)

    def read_tree(self, commit_hex: str) -> dict[str, bytes]:
        """Return the whole shadow tree at *commit_hex* as ``{path: bytes}``.

        Results are memoized process-wide (commits are immutable). The returned
        dict is a shallow copy — callers may add/remove keys safely.
        """
        hit = _tree_cache_get(commit_hex)
        if hit is not None:
            return dict(hit)
        commit = self._repo[commit_hex.encode("ascii")]
        out = self._flatten_tree(commit.tree, prefix="")
        _tree_cache_put(commit_hex, out)
        return dict(out)

    def read_blob_at(self, commit_hex: str, path: str) -> bytes | None:
        """Return the bytes of a single shadow-tree path at *commit_hex*, or None.

        Walks only the path components (and uses the tree cache when warm) —
        never flattens the whole commit just to fetch ``manifest.json``.
        """
        hit = _tree_cache_get(commit_hex)
        if hit is not None:
            return hit.get(path)
        entry = self.entry_at(commit_hex, path)
        if entry is None or entry[0] == 0o040000:
            return None
        try:
            return self._repo[entry[1]].data
        except KeyError:
            return None

    def _read_object(self, oid: bytes) -> Commit | Tree:
        hit = self._read_cache.get(oid)
        if hit is not None:
            self._read_cache.move_to_end(oid)
            return hit
        obj = self._repo[oid]
        if not isinstance(obj, (Commit, Tree)):
            raise TypeError("expected commit or tree")
        self._read_cache[oid] = obj
        if len(self._read_cache) > 256:
            self._read_cache.popitem(last=False)
        return obj

    def entry_at(self, commit_hex: str, path: str) -> tuple[int, bytes] | None:
        """Resolve a path to its mode/object id without reading its blob."""
        try:
            commit = self._read_object(commit_hex.encode("ascii"))
            entry = (0o040000, commit.tree)
            for part in filter(None, path.split("/")):
                if entry[0] != 0o040000:
                    return None
                tree = self._read_object(entry[1])
                entry = tree[part.encode("utf-8")]
            return entry
        except KeyError:
            return None

    def subtree_ids(self, commit_hex: str, prefix: str) -> dict[str, bytes]:
        """Map blob paths beneath a directory to ids, without decoding blobs."""
        entry = self.entry_at(commit_hex, prefix)
        if entry is None or entry[0] != 0o040000:
            return {}
        base = prefix.rstrip("/") + "/"
        return {base + path: oid for path, oid in self.tree_blob_ids(entry[1]).items()}

    def tree_blob_ids(self, tree_oid: bytes) -> dict[str, bytes]:
        """Map blob paths (relative to a tree object) to ids, without decoding blobs."""
        out: dict[str, bytes] = {}

        def walk(oid: bytes, base: str) -> None:
            for child in self._read_object(oid).items():
                path = base + child.path.decode("utf-8")
                if child.mode == 0o040000:
                    walk(child.sha, path + "/")
                else:
                    out[path] = child.sha

        walk(tree_oid, "")
        return out

    def child_ids(self, commit_hex: str, prefix: str) -> dict[str, bytes]:
        """Map the immediate children of a directory to their object ids (one tree read)."""
        entry = self.entry_at(commit_hex, prefix)
        if entry is None or entry[0] != 0o040000:
            return {}
        return {child.path.decode("utf-8"): child.sha
                for child in self._read_object(entry[1]).items()}

    def read_subtree(self, commit_hex: str, prefix: str) -> dict[str, bytes]:
        """Read only one item's blobs, reusing a warm whole-tree snapshot."""
        prefix = prefix.rstrip("/") + "/"
        hit = _tree_cache_get(commit_hex)
        if hit is not None:
            return {path: data for path, data in hit.items() if path.startswith(prefix)}
        return {path: self._repo[oid].data
                for path, oid in self.subtree_ids(commit_hex, prefix).items()}

    def commit_meta(self, commit_hex: str) -> dict[str, Any]:
        """Return commit-level metadata (author, timestamp, message, parents)."""
        return _meta_from_commit(commit_hex, self._read_object(commit_hex.encode("ascii")))

    def iter_log(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield ``(commit_id, meta)`` newest-first along the linear parent chain.

        Lazy on purpose: a caller that stops early (limited or filtered
        history) never reads — let alone decodes — the older tail of the
        chain. The parent link and the metadata come out of the same object
        read, so a full drain costs exactly one decode per commit, the same as
        :meth:`log`.
        """
        head = self.head_commit()
        while head is not None:
            meta = _meta_from_commit(head, self._read_object(head.encode("ascii")))
            yield head, meta
            parents = meta["parents"]
            head = parents[0] if parents else None

    def log(self, limit: int | None = None) -> list[str]:
        """Return commit ids newest-first along the linear parent chain."""
        out: list[str] = []
        for commit_hex, _meta in self.iter_log():
            out.append(commit_hex)
            if limit is not None and len(out) >= limit:
                break
        return out

    def log_dag(self, start_shas: list[str], limit: int) -> list[str]:
        """Return a newest-first topological walk of all reachable parents."""
        reachable: set[str] = set()
        stack = [sha for sha in start_shas if sha]
        while stack:
            sha = stack.pop()
            if sha in reachable:
                continue
            try:
                parents = self.commit_meta(sha)["parents"]
            except KeyError:
                continue
            reachable.add(sha)
            stack.extend(p for p in parents if p not in reachable)

        # Kahn over child -> parent edges guarantees every child is emitted
        # before its parents. Timestamp chooses among currently-ready commits.
        child_count = {sha: 0 for sha in reachable}
        parents_by_sha: dict[str, list[str]] = {}
        times: dict[str, int] = {}
        for sha in reachable:
            meta = self.commit_meta(sha)
            parents = [p for p in meta["parents"] if p in reachable]
            parents_by_sha[sha] = parents
            times[sha] = int(meta["authored_at"])
            for parent in parents:
                child_count[parent] += 1
        ready = [(-times[sha], sha) for sha, count in child_count.items() if count == 0]
        heapq.heapify(ready)
        out: list[str] = []
        while ready and len(out) < max(0, int(limit)):
            _neg_time, sha = heapq.heappop(ready)
            out.append(sha)
            for parent in parents_by_sha[sha]:
                child_count[parent] -= 1
                if child_count[parent] == 0:
                    heapq.heappush(ready, (-times[parent], parent))
        return out

    def list_refs(self, prefix: str) -> dict[str, str]:
        """Return refs beneath *prefix* as ``{full_name: target_sha}``."""
        raw_prefix = prefix.encode("utf-8")
        out: dict[str, str] = {}
        for key in self._repo.refs.keys():
            if key.startswith(raw_prefix):
                target = self._repo.refs[key]
                out[key.decode("utf-8")] = target.decode("ascii")
        return out

    def set_ref(self, name: str, sha: str) -> None:
        self._repo.refs[name.encode("utf-8")] = sha.encode("ascii")

    def ref_target(self, name: str) -> str | None:
        try:
            target = self._repo.refs[name.encode("utf-8")]
        except KeyError:
            return None
        return target.decode("ascii")

    # -- writing -----------------------------------------------------------

    def commit_tree(self, files: dict[str, bytes], meta: EntryMeta) -> str:
        """Write *files* as a whole shadow tree and commit it onto HEAD (linear).

        Unchanged blobs are naturally deduplicated by content addressing — a
        typical save touches one item, so only its ~3 small blobs are new
        (contract §2). Returns the new commit id (hex).
        """
        obj_store = self._repo.object_store
        root = self._build_tree(files)

        commit = Commit()
        commit.tree = root.id
        parent = self.head_commit()
        commit.parents = [parent.encode("ascii")] if parent else []
        who = f"{meta.author_name} <{meta.author_email}>".encode("utf-8")
        commit.author = commit.committer = who
        when = meta.authored_at if meta.authored_at is not None else int(time.time())
        commit.author_time = commit.commit_time = when
        commit.author_timezone = commit.commit_timezone = 0
        # The entry KIND lives in the commit message trailer so the log is
        # self-describing without a side table (contract §3.1). meta.json in the
        # tree carries the same for readers that don't parse commit trailers.
        body = meta.message or ""
        trailer = f"chudware-plm-kind: {meta.kind}\n"
        if meta.scope and meta.scope != "design":
            trailer += f"chudware-plm-scope: {meta.scope}\n"
        commit.message = (f"{body}\n\n{trailer}").encode("utf-8")

        obj_store.add_object(commit)
        # Compare-and-swap HEAD. If a concurrent session moved HEAD between our head_commit() read
        # (line above) and now, set_if_equals returns False and our commit is orphaned — NOT on
        # HEAD. The old code discarded this boolean and returned an unreachable commit id, silently
        # losing the writer's change (and dropping the other writer's item edits, since our tree was
        # built from a stale snapshot). Surface it so the caller can rebuild against fresh HEAD.
        swapped = self._repo.refs.set_if_equals(
            b"HEAD",
            parent.encode("ascii") if parent else None,
            commit.id,
        )
        if not swapped:
            raise ConcurrentUpdate(
                "HEAD moved under a concurrent write; rebuild the shadow tree and retry")
        return commit.id.decode("ascii")

    # -- maintenance -------------------------------------------------------

    def repack(self) -> str:
        """Pack loose objects. Uses system ``git gc`` when present (accelerator),
        else dulwich's own packer. Never fails the caller on absence."""
        if git_available():
            try:
                subprocess.run(
                    git_argv(["--git-dir", str(self._vault_dir), "gc", "--quiet"]),
                    check=True,
                    capture_output=True,
                    env=git_env(),
                )
                return "git-gc"
            except (subprocess.CalledProcessError, OSError):
                pass  # fall through to the guaranteed dulwich path
        try:
            self._repo.object_store.pack_loose_objects()
            return "dulwich-pack"
        except Exception:
            return "noop"

    # -- internals ---------------------------------------------------------

    def _build_tree(self, files: dict[str, bytes]) -> Tree:
        """Build a (possibly nested) git tree from a flat ``{path: bytes}`` map."""
        obj_store = self._repo.object_store
        # group paths by their first component to build nested subtrees
        nested: dict[str, Any] = {}
        for path, data in files.items():
            parts = path.split("/")
            node = nested
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = data

        def build(node: dict[str, Any]) -> Tree:
            tree = Tree()
            for name, val in sorted(node.items()):
                if isinstance(val, dict):
                    sub = build(val)
                    obj_store.add_object(sub)
                    tree.add(name.encode("utf-8"), 0o040000, sub.id)
                else:
                    blob = Blob.from_string(val)
                    obj_store.add_object(blob)
                    tree.add(name.encode("utf-8"), 0o100644, blob.id)
            return tree

        root = build(nested)
        obj_store.add_object(root)
        return root

    def _flatten_tree(self, tree_id: bytes, prefix: str) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        tree = self._repo[tree_id]
        for entry in tree.items():
            name = entry.path.decode("utf-8")
            full = f"{prefix}{name}"
            obj = self._repo[entry.sha]
            if isinstance(obj, Tree):
                out.update(self._flatten_tree(entry.sha, prefix=f"{full}/"))
            else:
                out[full] = obj.data
        return out


def _hide_on_windows(path: Path) -> None:
    """Best-effort: mark ``.aicadvault`` hidden on Windows (contract §2 'HIDDEN').

    On POSIX the leading dot already hides it; this is a no-op there.
    """
    if os.name == "nt":  # pragma: no cover - windows only
        try:
            import ctypes

            FILE_ATTRIBUTE_HIDDEN = 0x02
            ctypes.windll.kernel32.SetFileAttributesW(str(path), FILE_ATTRIBUTE_HIDDEN)
        except Exception:
            pass


def open_binding(project_dir: str | os.PathLike) -> GitBinding:
    """Open (or silently create) the vault binding for *project_dir*."""
    return GitBinding.open_or_create(project_dir)
