"""Actor-scoped reverse-engineering artifact delivery."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from gateway import cells, engine_bridge, workspace as workspace_mod


def _actor_root(principal):
    actor = hashlib.sha256(principal.actor_key.encode()).hexdigest()[:32]
    owner = hashlib.sha256(repr((principal.workspace_key, principal.actor_key)).encode()).hexdigest()[:32]
    return cells.state_root(principal) / "actors" / actor / "data" / owner / "reveng"


def _grant(root, path, size, modified, kind="gaussian_ply"):
    token = hashlib.sha256(f"{path}\0{size}\0{modified}\0{kind}".encode()).hexdigest()
    refs = root / "artifact_refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / token).write_text(json.dumps({"path": path, "size": size,
                                          "mtime_ns": modified, "kind": kind}))
    return token


def _artifact(root, name="session-abc/splat/cloud.ply", content=b"ply\nowned"):
    artifact = root / name
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(content)
    stat = artifact.stat()
    return artifact, _grant(root, name, stat.st_size, stat.st_mtime_ns)


def test_owned_artifact_stream_and_cross_actor_refusal(client, monkeypatch):
    principal = cells.resolve_workspace()
    root = _actor_root(principal)
    artifact, token = _artifact(root)
    response = client.get(f"/reveng/artifact/{token}")
    assert response.status_code == 200
    assert response.content == artifact.read_bytes()
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-length"] == str(artifact.stat().st_size)

    monkeypatch.setattr(workspace_mod.cells, "resolve_workspace",
                        lambda: replace(principal, actor_key="different-actor"))
    assert client.get(f"/reveng/artifact/{token}").status_code == 404


def test_cross_workspace_refusal(client, monkeypatch):
    principal = cells.resolve_workspace()
    _, token = _artifact(_actor_root(principal))
    monkeypatch.setattr(workspace_mod.cells, "resolve_workspace",
                        lambda: replace(principal, workspace_key="user:other"))
    assert client.get(f"/reveng/artifact/{token}").status_code == 404


def test_artifact_ref_rejects_traversal_symlinks_and_unapproved_kind(client):
    root = _actor_root(cells.resolve_workspace())
    artifact, token = _artifact(root)
    outside = root / "outside.ply"
    outside.write_bytes(b"outside")
    info = outside.stat()
    traversal = _grant(root, "session-abc/../../outside.ply", info.st_size, info.st_mtime_ns)
    assert client.get(f"/reveng/artifact/{traversal}").status_code == 404
    assert client.get("/reveng/artifact/%2e%2e%2foutside.ply").status_code in (400, 404)
    assert client.get("/reveng/artifact/not-a-token").status_code == 404

    link = artifact.parent / "link.ply"
    link.symlink_to(outside)
    symlink_token = _grant(root, "session-abc/splat/link.ply", info.st_size, info.st_mtime_ns)
    assert client.get(f"/reveng/artifact/{symlink_token}").status_code == 403
    bad_kind = _grant(root, "session-abc/splat/cloud.ply", artifact.stat().st_size,
                      artifact.stat().st_mtime_ns, "arbitrary_file")
    assert client.get(f"/reveng/artifact/{bad_kind}").status_code == 404

    artifact.write_bytes(b"replacement artifact")
    assert client.get(f"/reveng/artifact/{token}").status_code == 404


def test_artifact_ref_enforces_size_cap(client, monkeypatch):
    root = _actor_root(cells.resolve_workspace())
    _, token = _artifact(root)
    monkeypatch.setattr(workspace_mod, "_REVENG_ARTIFACT_MAX", 2)
    assert client.get(f"/reveng/artifact/{token}").status_code in (403, 404)


@pytest.mark.parametrize("method,right", [
    ("reveng_sparse_preview", "read"),
    ("reveng_confirm_scale", "write"),
    ("reveng_import_splat", "write"),
    ("reveng_convert_splat", "write"),
    ("reveng_splat_to_mesh", "run"),
])
def test_reveng_methods_are_actor_scoped_and_admitted(method, right):
    assert cells.method_policy()[method] == {"cell": "allow", "right": right, "scope": "actor"}
    frame = json.dumps({"id": 1, "method": method, "params": {}})
    assert engine_bridge.parse_browser_request(frame, right="owner")["method"] == method
    if right != "read":
        with pytest.raises(engine_bridge.InvalidRequest) as error:
            engine_bridge.parse_browser_request(frame, right="viewer")
        assert error.value.code == "forbidden_right"
