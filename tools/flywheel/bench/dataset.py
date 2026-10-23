"""Frozen dataset specs for the bench suite.

A bench dataset is *frozen* by a deterministic content hash computed over its sorted
member files. Two properties this buys us (reproducible from the dataset hash on a
second machine/OS):

  * the same bytes on any OS -> the same hash, so a report's `dataset_hash` is a
    verifiable fingerprint of exactly what was scored;
  * DVC stores the bytes; this hash is what CI/gate asserts without needing
    a DVC remote — the hash travels in git inside the frozen `spec.json`.

Pure stdlib. Hashing is byte-exact and path-relative (forward-slash normalized) so
Windows and Linux agree.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Optional


# The frozen manifest lives alongside the data but is metadata, not data, so it is
# excluded from the content hash — otherwise writing it would change the very hash it
# records.
MANIFEST_NAME = "spec.json"


def _iter_files(root: str, ignore: frozenset[str]) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name in ignore:
                continue
            out.append(os.path.join(dirpath, name))
    return out


# Text member extensions get their line endings normalized to LF before hashing, so a
# git checkout that applies CRLF conversion on Windows (autocrlf) yields the SAME hash as
# a Linux checkout — the "reproducible on a second OS" property the tests check.
# Binary members (renders, STEP, glTF) are hashed raw. `.gitattributes` in the corpus
# dirs is the belt to this suspenders (forces `-text` so git stores bytes verbatim).
_TEXT_EXTS = frozenset({".json", ".jsonl", ".txt", ".md", ".csv", ".req", ".yaml", ".yml"})


def _member_digest_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        data = f.read()
    if os.path.splitext(path)[1].lower() in _TEXT_EXTS:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def content_hash(root: str, *, algo: str = "sha256",
                 ignore: frozenset[str] = frozenset({MANIFEST_NAME})) -> str:
    """Deterministic hash of a directory tree: relative-path + normalized bytes of each file.

    Path separators are normalized to '/' and text-member line endings to LF so the hash
    is OS-independent. Files named in `ignore` (the frozen manifest by default) are skipped.
    """
    h = hashlib.new(algo)
    root = os.path.abspath(root)
    for path in _iter_files(root, ignore):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(_member_digest_bytes(path))
        h.update(b"\0")
    return f"{algo}:{h.hexdigest()}"


@dataclass
class DatasetSpec:
    """Frozen dataset descriptor. Serialized as `spec.json` next to the data.

    dvc_ref:   the pointer a DVC remote resolves to fetch the bytes (e.g.
               'corpus/regen_fix/v1'); optional until the remote exists.
    split:     name of the held-out split this spec addresses ('test' | 'smoke' | 'all').
    structures: the held-out STRUCTURE keys covered (split on structures, not
               samples). Documented so a reader can see the generalization boundary.
    """
    id: str
    root: str
    hash: str
    split: str = "all"
    dvc_ref: Optional[str] = None
    n_examples: int = 0
    structures: list[str] = field(default_factory=list)

    @staticmethod
    def freeze(id: str, root: str, *, split: str = "all", dvc_ref: Optional[str] = None,
               n_examples: int = 0, structures: Optional[list[str]] = None) -> "DatasetSpec":
        return DatasetSpec(
            id=id, root=root, hash=content_hash(root), split=split, dvc_ref=dvc_ref,
            n_examples=n_examples, structures=sorted(structures or []),
        )

    def verify(self) -> bool:
        """True iff the on-disk bytes still match the frozen hash (reproducibility check)."""
        return content_hash(self.root) == self.hash

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in self.__dict__.items() if k != "root"},
                      f, indent=2, sort_keys=True)

    @staticmethod
    def load(path: str, root: str) -> "DatasetSpec":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return DatasetSpec(root=root, **d)
