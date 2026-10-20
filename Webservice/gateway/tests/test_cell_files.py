"""Consumer-visible file admission, quota and tenant-cell fetch regressions."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from types import SimpleNamespace

os.environ.setdefault("CHUDWARE_WEB_DEV", "1")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.formparsers import MultiPartParser

from gateway import accounts, bucket, cells, engine_bridge, limits, workspace
from gateway.config import settings


@pytest.fixture
def files_app(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    drop = tmp_path / "bucket"
    root.mkdir()
    drop.mkdir()
    principal = cells.CellPrincipal("00000000-0000-4000-8000-000000000001",
        "user:00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000001", "owner", 0, "a" * 32,
        root, "", "trial")
    state = {"principal": principal, "email": "files@example.com", "quota": 1024 * 1024}
    user = SimpleNamespace(email=state["email"], role="user")
    monkeypatch.setattr(workspace, "get_current_user", lambda request: user)
    monkeypatch.setattr(bucket, "get_current_user", lambda request: user)
    monkeypatch.setattr(cells, "personal_root", lambda email: root)
    monkeypatch.setattr(cells, "personal_bucket_root", lambda email: drop)
    monkeypatch.setattr(cells, "resolve_workspace", lambda email, team_id="": state["principal"])
    monkeypatch.setattr(workspace.orgs, "selected_workspace_team", lambda *args: state["principal"].team_id)
    monkeypatch.setattr(bucket, "get_account", lambda email: SimpleNamespace(plan="trial"))
    monkeypatch.setattr(settings, "bucket_root", drop)
    monkeypatch.setattr(settings, "max_upload_bytes", 4096)
    monkeypatch.setattr(limits, "limits_for", lambda plan: SimpleNamespace(storage_quota_bytes=state["quota"]))
    for module in (workspace, bucket):
        monkeypatch.setattr(module, "api_writes", limits.RateLimiter(1000, 60))
        monkeypatch.setattr(module, "uploads", limits.RateLimiter(1000, 60))
    app = FastAPI()
    app.include_router(workspace.router)
    app.include_router(bucket.router)
    state.update(app=app, root=root, drop=drop, client=TestClient(app))
    return state


def _raw_request(app, path, chunks, *, method="POST", headers=()):
    """Exercise the real ASGI route and count input reads, including chunked input."""
    consumed = 0
    messages = []
    pieces = list(chunks)

    async def receive():
        nonlocal consumed
        if consumed >= len(pieces):
            # A streaming response may listen for disconnect after reading input.
            await asyncio.sleep(0.01)
            return {"type": "http.disconnect"}
        index = consumed
        consumed += 1
        return {"type": "http.request", "body": pieces[index], "more_body": consumed < len(pieces)}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": list(headers), "client": ("127.0.0.1", 1), "server": ("test", 80)}
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return start["status"], consumed, body


def _multipart(field="file", payload=b"part"):
    return (b"--cell\r\nContent-Disposition: form-data; name=\"" + field.encode() +
            b"\"; filename=\"part.aicm\"\r\nContent-Type: application/octet-stream\r\n\r\n" +
            payload + b"\r\n--cell--\r\n")


@pytest.mark.parametrize("path,module", [("/workspace/upload", workspace), ("/api/bucket/upload", bucket)])
@pytest.mark.parametrize("rejection", ["rate", "size", "quota"])
def test_upload_rejected_before_multipart_or_receive(files_app, monkeypatch, path, module, rejection):
    def forbidden_form(*args, **kwargs):
        pytest.fail("request.form called before admission")
    monkeypatch.setattr(workspace.Request, "form", forbidden_form)
    headers = [(b"content-type", b"multipart/form-data; boundary=cell")]
    expected = {"rate": 429, "size": 413, "quota": 507}[rejection]
    if rejection == "rate":
        monkeypatch.setattr(module, "uploads", limits.RateLimiter(0, 60))
    elif rejection == "size":
        headers.append((b"content-length", b"99999"))
    else:
        files_app["quota"] = 1
        (files_app["root"] if module is workspace else files_app["drop"]).joinpath("full").write_bytes(b"xx")
    status, consumed, _ = _raw_request(files_app["app"], path, [_multipart()], headers=headers)
    assert status == expected
    assert consumed == 0


@pytest.mark.parametrize("path,field", [("/workspace/upload", "file"), ("/api/bucket/upload", "files")])
def test_chunked_multipart_cap_precedes_parser_buffer(files_app, monkeypatch, path, field):
    monkeypatch.setattr(settings, "max_upload_bytes", 256)
    buffered = 0
    original = MultiPartParser.on_part_data
    def part_data(self, data, start, end):
        nonlocal buffered
        buffered += end - start
        return original(self, data, start, end)
    monkeypatch.setattr(MultiPartParser, "on_part_data", part_data)
    body = _multipart(field, b"x" * 1024)
    status, consumed, _ = _raw_request(files_app["app"], path,
        [body[:160], body[160:500], body[500:]],
        headers=[(b"content-type", b"multipart/form-data; boundary=cell")])
    assert status == 413
    assert consumed == 2
    assert buffered < 160
    assert list(files_app["root"].iterdir()) == []
    assert list(files_app["drop"].iterdir()) == []


def test_relative_upload_and_list_contract(files_app):
    client = files_app["client"]
    response = client.post("/workspace/upload", files={"file": ("part.aicm", b"mesh")})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "path": "part.aicm", "bytes": 4,
                               "workspace_key": files_app["principal"].workspace_key}
    listing = client.get("/workspace/list").json()
    assert listing["root"] == "."
    assert listing["workspace_key"] == files_app["principal"].workspace_key
    assert listing["files"][0]["path"] == "part.aicm"


@pytest.mark.parametrize("recent", ["/etc/passwd", "../other.aicm", "C:/secret", "x" * 4097])
def test_recents_reject_nonrelative_or_oversized_path(files_app, recent):
    response = files_app["client"].put("/workspace/recents", json={"files": [recent]})
    assert response.status_code == 400
    assert not files_app["root"].joinpath(workspace._RECENTS_NAME).exists()


@pytest.mark.parametrize("kind", ["recents", "chat"])
def test_json_store_quota_and_replacement_delta(files_app, kind):
    client = files_app["client"]
    path = files_app["root"] / (workspace._RECENTS_NAME if kind == "recents" else workspace._CHAT_NAME)
    original = {"files": ["longname.aicm"]} if kind == "recents" else {
        "schema": 2, "active": "c", "sessions": [{"id": "c", "title": "a long title", "msgs": []}]}
    assert client.put(f"/workspace/{kind}", json=original).status_code == 200
    original_bytes = path.read_bytes()
    files_app["quota"] = len(original_bytes)
    short = {"files": ["p"]} if kind == "recents" else {"schema": 2, "active": "", "sessions": []}
    assert client.put(f"/workspace/{kind}", json=short).status_code == 200
    before = path.read_bytes()
    large = {"files": ["p" * 1000]} if kind == "recents" else {
        "schema": 2, "active": "c", "sessions": [{"id": "c", "title": "x" * 1000}]}
    assert client.put(f"/workspace/{kind}", json=large).status_code == 507
    assert path.read_bytes() == before
    assert client.get(f"/workspace/{kind}").json() == short


def test_chunked_chat_cap_stops_before_remaining_input(files_app, monkeypatch):
    monkeypatch.setattr(workspace, "_CHAT_MAX_BODY", 16)
    status, consumed, _ = _raw_request(files_app["app"], "/workspace/chat", [b"x" * 10] * 3,
        method="PUT", headers=[(b"content-type", b"application/json")])
    assert status == 413
    assert consumed == 2
    assert not files_app["root"].joinpath(workspace._CHAT_NAME).exists()


def test_team_viewer_cannot_upload(files_app):
    principal = files_app["principal"]
    files_app["principal"] = replace(principal, right="viewer", team_id="team", workspace_key="team:org/team")
    response = files_app["client"].post("/workspace/upload", files={"file": ("p", b"data")})
    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden_right"


def test_workspace_mesh_relative_paths_and_scope_match(files_app):
    client = files_app["client"]
    key = files_app["principal"].workspace_key
    files_app["root"].joinpath("model.aicm").write_bytes(b"mesh")
    assert client.get("/files/aicm", params={"workspace_key": key, "path": "model.aicm"}).content == b"mesh"
    for path in ("/dev/shm/chudware_doc", "aicm-shm://chudware_doc", "/etc/passwd"):
        assert client.get("/files/aicm", params={"workspace_key": key, "path": path}).status_code == 400
    assert client.get("/files/aicm", params={"workspace_key": "user:another", "path": "model.aicm"}).status_code == 403
    assert client.get("/files/aicm", params={"doc_id": "d"}).status_code == 422


def test_file_reads_and_listing_never_follow_symlinks(files_app, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"private")
    root = files_app["root"]
    (root / "leak").symlink_to(secret)
    (root / "linked_dir").symlink_to(tmp_path, target_is_directory=True)
    client = files_app["client"]
    assert client.get("/workspace/list").json()["files"] == []
    assert client.get("/workspace/list").json()["dirs"] == []
    assert client.get("/workspace/download", params={"path": "leak"}).status_code == 403
    assert client.get("/workspace/download", params={"path": "linked_dir/secret"}).status_code == 403
    assert client.get("/files/aicm", params={"workspace_key": files_app["principal"].workspace_key,
                                           "path": "leak"}).status_code == 403


def test_real_mesh_grant_becomes_403_after_epoch_change(files_app, pg):
    email = files_app["email"]
    accounts.ensure_account(email, display=email)
    accounts.set_status(email, "active", decided_by="test")
    account = accounts.get_account(email)
    principal = replace(files_app["principal"], account_uid=account.uid, actor_key=account.uid,
                        workspace_key=f"user:{account.uid}")
    files_app["principal"] = principal
    (files_app["root"] / "model.aicm").write_bytes(b"cell mesh")
    engine_bridge.grant_mesh_document(principal, "document-cell-files", "/work/model.aicm")
    params = {"workspace_key": principal.workspace_key, "doc_id": "document-cell-files"}
    response = files_app["client"].get("/files/aicm", params=params)
    assert response.status_code == 200
    assert response.content == b"cell mesh"
    # The grant remains cached; a fresh principal epoch alone must refuse it.
    files_app["principal"] = replace(principal, epoch=principal.epoch + 1)
    response = files_app["client"].get("/files/aicm", params=params)
    assert response.status_code == 403
    assert response.json()["detail"] == "document not authorized"


@pytest.mark.parametrize("path,field,root_key", [
    ("/workspace/upload", "file", "root"), ("/api/bucket/upload", "files", "drop")])
def test_upload_quota_replacement_preserves_previous_file(files_app, path, field, root_key):
    root = files_app[root_key]
    (root / "existing").write_bytes(b"previous")
    files_app["quota"] = 8
    response = files_app["client"].post(path, files={field: ("existing", b"new")})
    assert response.status_code == 200
    assert (root / "existing").read_bytes() == b"new"
    response = files_app["client"].post(path, files={field: ("existing", b"too big for quota")})
    assert response.status_code == 507
    assert (root / "existing").read_bytes() == b"new"
    assert not any(p.name.startswith(".upload-") for p in root.iterdir())


@pytest.mark.parametrize("path,field,module,root_key", [
    ("/workspace/upload", "file", workspace, "root"),
    ("/api/bucket/upload", "files", bucket, "drop")])
def test_upload_parent_swap_cannot_write_outside_cell(files_app, tmp_path, monkeypatch,
                                                      path, field, module, root_key):
    root = files_app[root_key]
    (root / "directory").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "part.aicm"
    secret.write_bytes(b"untouched")
    original_store = module.store_upload

    def swapped_store(*args, **kwargs):
        (root / "directory").rename(root / "original-directory")
        (root / "directory").symlink_to(outside, target_is_directory=True)
        return original_store(*args, **kwargs)

    monkeypatch.setattr(module, "store_upload", swapped_store)
    response = files_app["client"].post(path, files={field: ("part.aicm", b"attack")},
                                       data={"dir": "directory"})
    assert response.status_code in {400, 403}
    assert secret.read_bytes() == b"untouched"
    assert not (root / "original-directory" / "part.aicm").exists()


def test_v2_unicode_round_trip(files_app):
    body = {"version": 2, "active": "c", "sessions": [
        {"id": "c", "msgs": [{"t": "\u00e9\U0001f600", "u": True}]}]}
    client = files_app["client"]
    assert client.put("/workspace/chat", content=json.dumps(body),
                      headers={"Content-Type": "application/json"}).status_code == 200
    assert client.get("/workspace/chat").json() == body
