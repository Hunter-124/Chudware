from __future__ import annotations

import io
import json
import zipfile

import pytest

from services.plm import branches
from services.plm.vault import Vault, VaultError
from services.plm.worker_rpc import WORKER_METHODS


def _write_part(root, wall: float) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("document.json", json.dumps({
            "schema": "aicad.part/1", "id": "part-1", "name": "Part",
            "features": [{"id": "box", "op": "box", "params": {"wall": wall}}],
        }))
        zf.writestr("thumbnail.png", b"png")
    (root / "Part.step+").write_bytes(buf.getvalue())


def _build_graph(root):
    _write_part(root, 1.0)
    vault = Vault.open(root)
    vault.checkpoint("seed")
    branches.create_branch(vault, "feature")
    branches.switch_branch(vault, "feature")
    _write_part(root, 2.0)
    feature_commit = vault.checkpoint("feature only")["commit"]
    branches.switch_branch(vault, "main")
    _write_part(root, 3.0)
    vault.checkpoint("main only")
    topology = {b["name"]: b for b in branches.branch_topology(vault)}
    assert topology["feature"]["ahead"] == 1
    assert topology["feature"]["behind"] == 1
    merged = branches.merge_branches(vault, "feature", "main")
    v1 = vault.tag_version(message="first")
    v2 = vault.tag_version(message="second")
    return vault, feature_commit, merged["commit"], v1, v2


def test_real_dag_versions_release_and_rpc(tmp_path):
    vault, feature_commit, merge_commit, v1, v2 = _build_graph(tmp_path)
    try:
        assert (v1["name"], v2["name"]) == ("V1", "V2")
        with pytest.raises(VaultError) as exc:
            vault.tag_version(name="V1")
        assert exc.value.reason == "already_exists"

        graph = vault.version_graph()
        shas = [node["commit"] for node in graph["nodes"]]
        assert feature_commit in shas
        assert feature_commit not in [entry["commit"] for entry in vault.history(scope="all")]
        index = {sha: i for i, sha in enumerate(shas)}
        for node in graph["nodes"]:
            for parent in node["parents"]:
                assert index[parent] > index[node["commit"]]
        merge = next(node for node in graph["nodes"] if node["commit"] == merge_commit)
        assert merge["kind"] == "merge"

        item_id = vault.manifest()["items"][0]["item_id"]
        vault.release(item_id, "release", geometry_step=b"ISO-10303-21;", skip_preflight=True)
        released = vault.version_graph()
        release_nodes = [n for n in released["nodes"] if n["kind"] == "release"]
        assert len(release_nodes) >= 2
        assert release_nodes[0]["pin"] is True
        assert release_nodes[1]["pin"] is False
    finally:
        vault.close()

    rpc = WORKER_METHODS["plm_version_graph"]({"project_dir": str(tmp_path)}, lambda *_: None)
    assert rpc["ok"]
    assert set(rpc) == {"ok", "head", "active_branch", "truncated", "nodes", "branches", "versions"}
    assert set(rpc["nodes"][0]) == {
        "commit", "parents", "kind", "scope", "authored_at", "author", "ai",
        "message", "summary", "thumbnail_ref", "refs", "release", "pin",
    }
