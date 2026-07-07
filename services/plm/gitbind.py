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

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# dulwich is the PRIMARY binding — a hard dependency of services/plm/.
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

VAULT_DIRNAME = ".aicadvault"

# git binary is an OPTIONAL accelerator; feature-detected once.
_GIT_ON_PATH: bool | None = None


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
        """Return the whole shadow tree at *commit_hex* as ``{path: bytes}``."""
        commit = self._repo[commit_hex.encode("ascii")]
        return self._flatten_tree(commit.tree, prefix="")

    def read_blob_at(self, commit_hex: str, path: str) -> bytes | None:
        """Return the bytes of a single shadow-tree path at *commit_hex*, or None."""
        return self.read_tree(commit_hex).get(path)

    def commit_meta(self, commit_hex: str) -> dict[str, Any]:
        """Return commit-level metadata (author, timestamp, message, parents)."""
        c: Commit = self._repo[commit_hex.encode("ascii")]
        return {
            "commit": commit_hex,
            "authored_at": int(c.author_time),
            "author_raw": c.author.decode("utf-8", "replace"),
            "message": c.message.decode("utf-8", "replace"),
            "parents": [p.decode("ascii") for p in c.parents],
        }

    def log(self, limit: int | None = None) -> list[str]:
        """Return commit ids newest-first along the linear parent chain."""
        out: list[str] = []
        head = self.head_commit()
        while head is not None:
            out.append(head)
            if limit is not None and len(out) >= limit:
                break
            parents = self._repo[head.encode("ascii")].parents
            head = parents[0].decode("ascii") if parents else None
        return out

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
        trailer = f"aicad-plm-kind: {meta.kind}\n"
        if meta.scope and meta.scope != "design":
            trailer += f"aicad-plm-scope: {meta.scope}\n"
        commit.message = (f"{body}\n\n{trailer}").encode("utf-8")

        obj_store.add_object(commit)
        self._repo.refs.set_if_equals(
            b"HEAD",
            parent.encode("ascii") if parent else None,
            commit.id,
        )
        return commit.id.decode("ascii")

    # -- maintenance -------------------------------------------------------

    def repack(self) -> str:
        """Pack loose objects. Uses system ``git gc`` when present (accelerator),
        else dulwich's own packer. Never fails the caller on absence."""
        if git_available():
            try:
                subprocess.run(
                    ["git", "--git-dir", str(self._vault_dir), "gc", "--quiet"],
                    check=True,
                    capture_output=True,
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
