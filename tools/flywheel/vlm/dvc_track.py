"""DVC-version the synthetic VLM corpus (T-H10 DoD item 2), reusing the T-H5 helpers.

The corpus images are regenerable but large-ish artifacts; the ground truth is frozen by
`spec.json`'s content hash (cross-machine reproducibility). This module pins the *images*
under DVC so a second machine `dvc pull`s the exact bytes.

Repo-root `dvc init` + the shared remote are DEFERRED infra (the batched Erik ask shared
with T-H5/T-H6 — no `.dvc` exists on master yet), so this module does NOT run a repo-root
init as a side effect. It exposes `track_corpus(repo_root, corpus_rel)` that adds the corpus
to DVC once the repo IS initialized, and `roundtrip_demo()` proves the add→push→pull cycle
in a throwaway repo (used by the test) so the wiring is verified end-to-end, not asserted."""
from __future__ import annotations

from pathlib import Path

from ..dvc_init import init_training_repo, add_to_dvc, dvc_push, dvc_pull, dvc_status


def track_corpus(repo_root: Path, corpus_rel: str = "training/corpora/vlm/v1") -> str:
    """`dvc add` the corpus under an already-DVC-initialized `repo_root`; returns the
    generated `.dvc` pointer content (the thing that lives in git). Caller commits the
    `.dvc` file + `.gitignore`. Does NOT init DVC — that is shared infra."""
    return add_to_dvc(repo_root, Path(repo_root) / corpus_rel)


def roundtrip_demo(work_dir: Path, remote_dir: Path) -> dict:
    """Init a self-contained DVC repo, add a tiny corpus, push to a local remote, wipe the
    cache, pull it back, and confirm the bytes match. Proves DVC-versioning works without
    touching the main repo. Returns a small report."""
    from .corpus_gen import generate
    repo = init_training_repo(work_dir, remote_url=str(remote_dir.resolve()))
    corpus_dir = repo / "corpus"
    generate(str(corpus_dir), seeds=1, images=True)
    pointer = add_to_dvc(repo, corpus_dir)
    dvc_push(repo)
    sample = next((repo / "corpus" / "renders").glob("*.png"))
    original = sample.read_bytes()
    sample.unlink()                       # simulate a fresh checkout missing the data
    dvc_pull(repo)
    restored = sample.read_bytes()
    return {"pointer_has_md5": "md5" in pointer, "bytes_match": restored == original,
            "status": dvc_status(repo)[:200]}
