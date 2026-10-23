"""Workspace file API: relative-path contract, jail, uploads, JSON stores and meshes."""

from __future__ import annotations

import json

import pytest

from gateway import cells, engine_bridge, workspace as workspace_mod
from gateway.config import settings


def _upload(client, name="part.step", data=b"solid", directory=""):
    return client.post("/workspace/upload", files={"file": (name, data)}, data={"dir": directory})


def _workspace_key() -> str:
    return "user:" + settings.operator


def test_relative_upload_and_list_contract(client, workspace):
    response = _upload(client, "part.aicm", b"mesh")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": "part.aicm", "bytes": 4,
                               "workspace_key": _workspace_key()}
    assert (workspace / "part.aicm").read_bytes() == b"mesh"
    listing = client.get("/workspace/list").json()
    assert listing["root"] == "."
    assert listing["workspace_key"] == _workspace_key()
    assert listing["files"][0]["path"] == "part.aicm"
    for payload in (response.text, client.get("/workspace/list").text):
        assert str(workspace) not in payload


def test_upload_into_subdirectory_and_download(client, workspace):
    assert client.post("/workspace/mkdir", json={"path": "parts/brackets"}).status_code == 200
    response = _upload(client, "b.step", b"bracket", "parts/brackets")
    assert response.json()["path"] == "parts/brackets/b.step"
    download = client.get("/workspace/download", params={"path": "parts/brackets/b.step"})
    assert download.status_code == 200
    assert download.content == b"bracket"
    assert "b.step" in download.headers["content-disposition"]
    listing = client.get("/workspace/list", params={"dir": "parts"}).json()
    assert listing["dirs"] == [{"name": "brackets", "path": "parts/brackets"}]


def test_upload_replaces_existing_file_atomically(client, workspace):
    (workspace / "existing").write_bytes(b"previous")
    assert _upload(client, "existing", b"new").status_code == 200
    assert (workspace / "existing").read_bytes() == b"new"
    assert not any(p.name.startswith(".upload-") for p in workspace.iterdir())


@pytest.mark.parametrize("name", [".hidden", ""])
def test_upload_refuses_dotfiles(client, workspace, name):
    response = _upload(client, name or ".", b"x")
    assert response.status_code in (400, 422)
    assert list(workspace.iterdir()) == []


def test_upload_size_cap_leaves_no_partial(client, workspace, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 64)
    assert _upload(client, "big.bin", b"x" * 200).status_code == 413
    assert not (workspace / "big.bin").exists()


def test_chunked_upload_capped_midstream(client, workspace, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 64)
    # A generator body means Transfer-Encoding: chunked and no Content-Length,
    # so only the streaming cap can stop it.
    boundary = "wsboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + b"x" * 200 + f"\r\n--{boundary}--\r\n".encode()

    def gen():
        yield body

    response = client.post("/workspace/upload", content=gen(),
                           headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert response.status_code == 413
    assert not (workspace / "big.bin").exists()


def test_upload_parent_swap_cannot_write_outside_workspace(client, workspace, tmp_path, monkeypatch):
    (workspace / "directory").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "part.aicm"
    secret.write_bytes(b"untouched")
    original_store = workspace_mod.store_upload

    def swapped_store(*args, **kwargs):
        (workspace / "directory").rename(workspace / "original-directory")
        (workspace / "directory").symlink_to(outside, target_is_directory=True)
        return original_store(*args, **kwargs)

    monkeypatch.setattr(workspace_mod, "store_upload", swapped_store)
    response = _upload(client, "part.aicm", b"attack", "directory")
    assert response.status_code in {400, 403}
    assert secret.read_bytes() == b"untouched"
    assert not (workspace / "original-directory" / "part.aicm").exists()


def test_rejects_dotdot_and_absolute_paths(client, workspace):
    for bad in ("../secret", "a/../../x", "..", "/etc/passwd", "/tmp/x"):
        assert client.get("/workspace/download", params={"path": bad}).status_code == 400
    for bad in ("../secret", "a/../../x", "..", "/etc/passwd", "/tmp/x", "C:/x"):
        assert client.post("/workspace/mkdir", json={"path": bad}).status_code == 400
    assert list(workspace.iterdir()) == []
    assert not (workspace.parent / "secret").exists()


@pytest.mark.parametrize("path", ["a\x00b", "a\nb", "a\x7fb"])
def test_control_character_paths_are_bad_requests(client, path):
    assert client.post("/workspace/mkdir", json={"path": path}).status_code == 400
    assert client.get("/workspace/download", params={"path": path}).status_code == 400


def test_reads_and_listing_never_follow_symlinks(client, workspace, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"private")
    (workspace / "leak").symlink_to(secret)
    (workspace / "linked_dir").symlink_to(tmp_path, target_is_directory=True)
    listing = client.get("/workspace/list").json()
    assert listing["files"] == [] and listing["dirs"] == []
    assert client.get("/workspace/download", params={"path": "leak"}).status_code == 403
    assert client.get("/workspace/download", params={"path": "linked_dir/secret"}).status_code == 403
    assert client.get("/workspace/list", params={"dir": "linked_dir"}).status_code == 403
    assert client.post("/workspace/rename", json={"src": "leak", "dst": "x"}).status_code == 400
    assert client.post("/workspace/delete", json={"path": "linked_dir"}).status_code == 400
    assert secret.read_bytes() == b"private"


def test_rename_refuses_existing_destination(client, workspace):
    (workspace / "a").write_bytes(b"a")
    (workspace / "b").write_bytes(b"b")
    assert client.post("/workspace/rename", json={"src": "a", "dst": "b"}).status_code == 409
    assert client.post("/workspace/rename", json={"src": "a", "dst": "c"}).json() == {
        "ok": True, "path": "c"}
    assert (workspace / "c").read_bytes() == b"a"


def test_delete_refuses_the_root(client, workspace):
    assert client.post("/workspace/delete", json={"path": ""}).status_code == 400
    assert workspace.is_dir()


@pytest.mark.parametrize("kind", ["file", "tree"])
def test_copy_and_delete_preserve_source(client, workspace, kind):
    if kind == "file":
        source, destination = workspace / "source.bin", workspace / "copied.bin"
        payload, copied_payload = source, destination
    else:
        source, destination = workspace / "source", workspace / "copied"
        payload = source / "nested" / "payload.bin"
        payload.parent.mkdir(parents=True)
        copied_payload = destination / "nested" / "payload.bin"
    payload.write_bytes(b"workspace payload")

    response = client.post("/workspace/copy", json={"src": source.name, "dst": destination.name,
                                                    "recursive": True})
    assert response.status_code == 200, response.text
    assert copied_payload.read_bytes() == b"workspace payload"
    assert client.post("/workspace/delete", json={"path": destination.name}).status_code == 200
    assert not destination.exists()
    assert payload.read_bytes() == b"workspace payload"


def test_copy_directory_requires_recursive(client, workspace):
    (workspace / "dir").mkdir()
    response = client.post("/workspace/copy", json={"src": "dir", "dst": "dir2", "recursive": False})
    assert response.status_code == 400
    assert not (workspace / "dir2").exists()


def test_recents_round_trip_and_deduplicate(client, workspace):
    body = {"files": ["a/part.step", "a/part.step", "b.step"]}
    assert client.put("/workspace/recents", json=body).json() == {
        "ok": True, "files": ["a/part.step", "b.step"]}
    assert client.get("/workspace/recents").json() == {"files": ["a/part.step", "b.step"]}
    assert (workspace / workspace_mod._RECENTS_NAME).is_file()


@pytest.mark.parametrize("recent", ["/etc/passwd", "../other.aicm", "C:/secret", "x" * 4097])
def test_recents_reject_nonrelative_or_oversized_path(client, workspace, recent):
    assert client.put("/workspace/recents", json={"files": [recent]}).status_code == 400
    assert not (workspace / workspace_mod._RECENTS_NAME).exists()


def test_chat_store_round_trips_and_replaces_completely(client, workspace):
    assert client.get("/workspace/chat").json() == {}
    store = {"version": 2, "active": "c1", "sessions": [
        {"id": "c1", "title": "design", "msgs": [{"u": True, "t": "Design a bracket", "e": ""}]},
        {"id": "c2", "title": "remove me", "msgs": []}]}
    assert client.put("/workspace/chat", json=store).json() == {"ok": True, "sessions": 2}
    assert client.get("/workspace/chat").json() == store
    replacement = {"schema": 2, "active": "c3", "sessions": [{"id": "c3", "msgs": []}]}
    assert client.put("/workspace/chat", json=replacement).status_code == 200
    assert client.get("/workspace/chat").json() == replacement


def test_chat_unicode_round_trip(client):
    body = {"version": 2, "active": "c", "sessions": [
        {"id": "c", "msgs": [{"t": "\u00e9\U0001f600", "u": True}]}]}
    assert client.put("/workspace/chat", content=json.dumps(body),
                      headers={"Content-Type": "application/json"}).status_code == 200
    assert client.get("/workspace/chat").json() == body


@pytest.mark.parametrize("malformed", [
    {"version": 2, "active": "c1", "sessions": [{"id": "c1"}], "doc1": {"active": "legacy"}},
    {"doc1": {"active": "c1", "conversations": {}}},
    {"version": 2, "schema": 2, "active": "", "sessions": []},
    {"version": 2, "active": "missing", "sessions": [{"id": "c1"}]},
    {"version": 2, "active": "", "sessions": [{"id": "c1"}, {"id": "c1"}]},
])
def test_chat_rejects_malformed_stores_without_overwrite(client, malformed):
    original = {"version": 2, "active": "c1", "sessions": [{"id": "c1", "title": "keep", "msgs": []}]}
    assert client.put("/workspace/chat", json=original).status_code == 200
    assert client.put("/workspace/chat", json=malformed).status_code == 400
    assert client.get("/workspace/chat").json() == original


def test_chat_session_count_is_bounded(client):
    oversized = {"version": 2, "active": "",
                 "sessions": [{"id": f"c{i}"} for i in range(workspace_mod._CHAT_MAX_SESSIONS + 1)]}
    response = client.put("/workspace/chat", json=oversized)
    assert response.status_code == 400
    assert "too many chat sessions" in response.json()["detail"]
    assert client.get("/workspace/chat").json() == {}


def test_files_aicm_relative_paths_and_key_match(client, workspace):
    key = _workspace_key()
    (workspace / "model.aicm").write_bytes(b"mesh")
    assert client.get("/files/aicm", params={"workspace_key": key, "path": "model.aicm"}).content == b"mesh"
    for path in ("/dev/shm/chudware_doc", "aicm-shm://chudware_doc", "/etc/passwd"):
        assert client.get("/files/aicm", params={"workspace_key": key, "path": path}).status_code == 400
    assert client.get("/files/aicm", params={"workspace_key": "user:another",
                                             "path": "model.aicm"}).status_code == 403
    assert client.get("/files/aicm", params={"workspace_key": key}).status_code == 400
    assert client.get("/files/aicm", params={"doc_id": "d"}).status_code == 422


def test_mesh_handoffs_require_a_cell_grant(client, gateway_home):
    principal = cells.resolve_workspace()
    mesh_root = settings.cells_root / principal.cell_id / "state" / "mesh"
    mesh_root.mkdir(parents=True)
    name = "a" * 32 + ".aicm"
    (mesh_root / name).write_bytes(b"cell mesh")
    params = {"workspace_key": principal.workspace_key, "doc_id": "doc-1"}
    assert client.get("/files/aicm", params=params).status_code == 403
    engine_bridge.grant_mesh_document(principal, "doc-1", "/state/mesh/" + name)
    try:
        response = client.get("/files/aicm", params=params)
        assert response.status_code == 200 and response.content == b"cell mesh"
        assert client.get("/files/aicm", params={**params, "doc_id": "other"}).status_code == 403
        assert client.get("/files/aicm", params={**params, "shm": "legacy"}).status_code == 400
    finally:
        engine_bridge._mesh_grants.clear()
