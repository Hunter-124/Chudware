"""Tenant-isolation tests — workspace.py / bucket.py are per-account trees.

Proves that with dev-mode auth standing in for the ~tens of thousands of
auto-approved *.ua.edu identities, one account can never read, overwrite,
rename, or delete another account's files or chat history, that jails reject
escapes and symlinks, that uploads are size-capped with no partials left
behind, and that the legacy shared tree is reachable only by an explicit
admin opt-in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

# Dev auth BEFORE importing the app, so X-Dev-User is accepted.
os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import app as app_mod  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.limits import RateLimiter, user_slug  # noqa: E402
import gateway.workspace as workspace_mod  # noqa: E402

# Auto-approved domain users (never admin) — the multi-tenant population.
A = {"X-Dev-User": "alice@crimson.ua.edu"}
B = {"X-Dev-User": "bob@crimson.ua.edu"}
# Admin seeded into a tmp users file by the fixture (the tracked users.yaml is
# a template with no admin) — the only identity allowed scope=shared.
ADMIN = {"X-Dev-User": "admin@example.com"}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """tmp-backed workspace/bucket roots + seeded admin + hermetic startup."""
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(settings, "data_root", tmp_path / "data")
    ws = tmp_path / "ws"        # legacy shared workspace root (pre-tenant data)
    bucket = tmp_path / "bucket"
    ws.mkdir()
    bucket.mkdir()
    monkeypatch.setattr(settings, "workspace_root", ws)
    monkeypatch.setattr(settings, "bucket_root", bucket)

    # The middleware's UserDirectory was built from the tracked template; point
    # it at a tmp file that actually contains an admin account.
    users_file = tmp_path / "users.yaml"
    users_file.write_text(
        "users:\n"
        "  - email: admin@example.com\n"
        "    display: Test Admin\n"
        "    role: admin\n"
        "    heavy: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "users_file", users_file)
    monkeypatch.setattr(app_mod.user_directory, "path", users_file)
    app_mod.user_directory.reload(force=True)

    # Startup calls manager.adopt_running(); keep it off the real podman.
    async def _no_podman(args, timeout=120.0):
        return 0, "", ""

    monkeypatch.setattr(app_mod.manager, "run_podman", _no_podman)
    return tmp_path


@pytest.fixture
def client(_isolate):
    with TestClient(app_mod.app) as c:
        yield c


def _ws_user_root(email: str) -> Path:
    return settings.user_workspace_root(user_slug(email))


def _bucket_user_root(email: str) -> Path:
    return settings.user_bucket_root(user_slug(email))


def _upload_ws(client, headers, name="part.step", data=b"solid body", dir=""):
    return client.post(
        "/workspace/upload",
        headers=headers,
        files={"file": (name, data)},
        data={"dir": dir},
    )


def _upload_bucket(client, headers, name="notes.txt", data=b"hello bucket", dir=""):
    return client.post(
        "/api/bucket/upload",
        headers=headers,
        files=[("files", (name, data))],
        data={"dir": dir},
    )


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


def test_workspace_upload_is_invisible_to_other_user(client):
    r = _upload_ws(client, A)
    assert r.status_code == 200, r.text
    # The file landed in A's per-account root, not the legacy shared root.
    assert (_ws_user_root("alice@crimson.ua.edu") / "part.step").is_file()
    assert not (settings.workspace_root / "part.step").exists()

    listing = client.get("/workspace/list", headers=B).json()
    assert all(f["name"] != "part.step" for f in listing["files"])

    r = client.get("/workspace/download", params={"path": "part.step"}, headers=B)
    assert r.status_code == 404
    # A can still fetch their own file back.
    r = client.get("/workspace/download", params={"path": "part.step"}, headers=A)
    assert r.status_code == 200
    assert r.content == b"solid body"


def test_workspace_delete_and_rename_cannot_cross_tenants(client):
    assert _upload_ws(client, A).status_code == 200

    r = client.post("/workspace/delete", headers=B, json={"path": "part.step"})
    assert r.status_code == 404
    r = client.post(
        "/workspace/rename", headers=B, json={"src": "part.step", "dst": "stolen.step"}
    )
    assert r.status_code == 404

    # A's file is untouched.
    r = client.get("/workspace/download", params={"path": "part.step"}, headers=A)
    assert r.status_code == 200
    assert r.content == b"solid body"


def test_workspace_v2_chat_store_round_trips_and_is_per_account(client):
    store = {
        "version": 2,
        "active": "c1",
        "sessions": [
            {
                "id": "c1",
                "title": "alice design",
                "msgs": [{"u": True, "t": "Design a bracket", "e": ""}],
                "scope": 1,
            }
        ],
    }
    r = client.put("/workspace/chat", headers=A, json=store)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "sessions": 1}
    assert client.get("/workspace/chat", headers=A).json() == store
    assert client.get("/workspace/chat", headers=B).json() == {}

    # B writes the same current envelope shape into a different tenant file.
    theirs = {
        "version": 2,
        "active": "b1",
        "sessions": [{"id": "b1", "title": "bob design", "msgs": []}],
    }
    assert client.put("/workspace/chat", headers=B, json=theirs).status_code == 200
    assert client.get("/workspace/chat", headers=B).json() == theirs
    assert client.get("/workspace/chat", headers=A).json() == store

    chat_a = _ws_user_root("alice@crimson.ua.edu") / ".chudware_chat_history.json"
    chat_b = _ws_user_root("bob@crimson.ua.edu") / ".chudware_chat_history.json"
    assert chat_a.is_file()
    assert chat_b.is_file()
    assert chat_a != chat_b


def test_workspace_v2_chat_put_replaces_the_complete_store(client):
    old_store = {
        "version": 2,
        "active": "c1",
        "sessions": [
            {"id": "c1", "title": "old", "msgs": []},
            {"id": "c2", "title": "remove me", "msgs": []},
        ],
    }
    new_store = {
        "schema": 2,
        "active": "c3",
        "sessions": [{"id": "c3", "title": "current", "msgs": []}],
    }
    assert client.put("/workspace/chat", headers=A, json=old_store).status_code == 200
    assert client.put("/workspace/chat", headers=A, json=new_store).status_code == 200
    assert client.get("/workspace/chat", headers=A).json() == new_store


@pytest.mark.parametrize(
    "malformed",
    [
        {
            "version": 2,
            "active": "c1",
            "sessions": [{"id": "c1"}],
            "doc1": {"active": "legacy"},
        },
        {"doc1": {"active": "c1", "conversations": {}}},
        {"version": 2, "schema": 2, "active": "", "sessions": []},
        {"version": 2, "active": "missing", "sessions": [{"id": "c1"}]},
        {"version": 2, "active": "", "sessions": [{"id": "c1"}, {"id": "c1"}]},
    ],
)
def test_workspace_chat_rejects_malformed_or_mixed_stores_without_overwrite(
    client, malformed
):
    original = {
        "version": 2,
        "active": "c1",
        "sessions": [{"id": "c1", "title": "keep", "msgs": []}],
    }
    assert client.put("/workspace/chat", headers=A, json=original).status_code == 200
    r = client.put("/workspace/chat", headers=A, json=malformed)
    assert r.status_code == 400, r.text
    assert client.get("/workspace/chat", headers=A).json() == original


def test_workspace_chat_session_count_is_bounded(client):
    oversized = {
        "version": 2,
        "active": "",
        "sessions": [{"id": f"c{i}"} for i in range(workspace_mod._CHAT_MAX_SESSIONS + 1)],
    }
    r = client.put("/workspace/chat", headers=A, json=oversized)
    assert r.status_code == 400
    assert "too many chat sessions" in r.json()["detail"]
    assert client.get("/workspace/chat", headers=A).json() == {}


def test_workspace_recents_are_per_account(client):
    client.put("/workspace/recents", headers=A, json={"files": ["a/part.step"]})
    assert client.get("/workspace/recents", headers=A).json()["files"] == ["a/part.step"]
    assert client.get("/workspace/recents", headers=B).json()["files"] == []


def test_workspace_rejects_dotdot_and_absolute_paths(client):
    for bad in ("../secret", "a/../../x", ".."):
        assert client.get("/workspace/download", params={"path": bad}, headers=A).status_code == 400
        assert client.post("/workspace/mkdir", headers=A, json={"path": bad}).status_code == 400
    for bad in ("/etc/passwd", "/tmp/x"):
        assert client.get("/workspace/download", params={"path": bad}, headers=A).status_code == 400
        assert client.post("/workspace/mkdir", headers=A, json={"path": bad}).status_code == 400
    # Nothing escaped onto the legacy root (its only child is the per-account
    # users/ container) or outside it.
    assert [p.name for p in settings.workspace_root.iterdir()] == ["users"]
    assert not (settings.workspace_root.parent / "secret").exists()


def test_workspace_rejects_planted_symlink(client):
    root_a = _ws_user_root("alice@crimson.ua.edu")
    assert _upload_ws(client, A).status_code == 200  # creates root_a
    # A plants a symlink inside their own jail pointing at the legacy tree.
    (root_a / "evil").symlink_to(settings.workspace_root)
    (settings.workspace_root / "legacy.txt").write_text("shared", encoding="utf-8")
    # Final-component symlink is refused outright...
    assert client.get("/workspace/download", params={"path": "evil"}, headers=A).status_code == 400
    # ...and resolving THROUGH it lands outside the jail, also refused.
    r = client.get("/workspace/download", params={"path": "evil/legacy.txt"}, headers=A)
    assert r.status_code == 400


def test_workspace_upload_size_cap_leaves_no_partial(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 64)
    # Declared Content-Length over the cap → 413 before any byte is written.
    r = _upload_ws(client, A, name="big.bin", data=b"x" * 200)
    assert r.status_code == 413
    assert not (_ws_user_root("alice@crimson.ua.edu") / "big.bin").exists()


def test_workspace_chunked_upload_capped_midstream(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 64)
    # A generator body means Transfer-Encoding: chunked and NO Content-Length,
    # so only the during-streaming cap can stop it.
    boundary = "tenantboundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + b"x" * 200 + f"\r\n--{boundary}--\r\n".encode()

    def gen():
        yield body

    r = client.post(
        "/workspace/upload",
        headers={**A, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=gen(),
    )
    assert r.status_code == 413
    assert not (_ws_user_root("alice@crimson.ua.edu") / "big.bin").exists()


def test_workspace_write_rate_limit_enforced(client, monkeypatch):
    monkeypatch.setattr(workspace_mod, "api_writes", RateLimiter(2, 60.0))
    assert client.post("/workspace/mkdir", headers=A, json={"path": "d1"}).status_code == 200
    assert client.post("/workspace/mkdir", headers=A, json={"path": "d2"}).status_code == 200
    r = client.post("/workspace/mkdir", headers=A, json={"path": "d3"})
    assert r.status_code == 429


def test_workspace_disk_quota_enforced(client, monkeypatch):
    monkeypatch.setattr(settings, "max_user_disk_bytes", 10)
    r = _upload_ws(client, A, name="q.bin", data=b"y" * 50)
    assert r.status_code == 507


def test_workspace_paths_stay_inside_the_callers_own_root(client):
    """Absolute paths ARE returned — the WASM client needs them to drive
    doc_open (host/src/core/fs_backend.h) — but they may only ever name the
    caller's own tree, never the shared root and never another account's."""
    mine = _ws_user_root("alice@crimson.ua.edu")
    theirs = _ws_user_root("bob@crimson.ua.edu")

    r = _upload_ws(client, A)
    assert r.status_code == 200
    abs_path = Path(r.json()["abs_path"])
    assert abs_path.is_relative_to(mine)
    assert not abs_path.is_relative_to(theirs)

    listing = client.get("/workspace/list", headers=A).json()
    assert Path(listing["root"]) == mine
    # A standard user must never be handed the shared legacy root itself, and
    # no other account's tree may appear in any payload.
    for payload in (r.text, client.get("/workspace/list", headers=A).text):
        assert f'"{settings.workspace_root}"' not in payload
        assert str(theirs) not in payload


def test_files_aicm_cannot_read_across_tenants(client):
    assert _upload_ws(client, A).status_code == 200
    victim = _ws_user_root("alice@crimson.ua.edu") / "part.step"
    # The legacy allowlist covered ALL of workspace_root, including every
    # users/<slug>/ subtree — an absolute-path cross-tenant read channel.
    r = client.get("/files/aicm", params={"path": str(victim)}, headers=B)
    assert r.status_code == 403
    # The owner can still fetch their own published payloads.
    r = client.get("/files/aicm", params={"path": str(victim)}, headers=A)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Bucket isolation
# ---------------------------------------------------------------------------


def test_bucket_upload_is_invisible_to_other_user(client):
    r = _upload_bucket(client, A)
    assert r.status_code == 200, r.text
    assert (_bucket_user_root("alice@crimson.ua.edu") / "notes.txt").is_file()
    assert not (settings.bucket_root / "notes.txt").exists()

    listing = client.get("/api/bucket/list", headers=B).json()
    assert all(f["name"] != "notes.txt" for f in listing["files"])
    assert Path(listing["root"]) == _bucket_user_root("bob@crimson.ua.edu")

    r = client.get("/api/bucket/download", params={"path": "notes.txt"}, headers=B)
    assert r.status_code == 404


def test_bucket_delete_and_rename_cannot_cross_tenants(client):
    assert _upload_bucket(client, A).status_code == 200
    r = client.post("/api/bucket/delete", headers=B, json={"path": "notes.txt"})
    assert r.status_code == 404
    r = client.post("/api/bucket/rename", headers=B, json={"src": "notes.txt", "name": "taken.txt"})
    assert r.status_code == 404
    r = client.get("/api/bucket/download", params={"path": "notes.txt"}, headers=A)
    assert r.status_code == 200


def test_bucket_rejects_dotdot_absolute_and_symlink(client):
    assert client.get("/api/bucket/download", params={"path": "../x"}, headers=A).status_code == 400
    assert client.get("/api/bucket/download", params={"path": "/etc/passwd"}, headers=A).status_code == 400
    assert client.post("/api/bucket/mkdir", headers=A, json={"path": "../x"}).status_code == 400
    assert _upload_bucket(client, A).status_code == 200  # create A's root
    root_a = _bucket_user_root("alice@crimson.ua.edu")
    (root_a / "evil").symlink_to(settings.bucket_root)
    assert client.get("/api/bucket/download", params={"path": "evil"}, headers=A).status_code == 400


# ---------------------------------------------------------------------------
# Admin escape hatch onto the legacy shared tree
# ---------------------------------------------------------------------------


def test_shared_scope_is_admin_only_and_preserves_legacy_data(client):
    # Pre-tenant single-tenant content lives directly under the legacy root.
    (settings.workspace_root / "legacy.step").write_text("team data", encoding="utf-8")
    (settings.bucket_root / "legacy.txt").write_text("team drop", encoding="utf-8")

    # Non-admins can never opt in, on either module.
    for url in ("/workspace/list", "/api/bucket/list"):
        r = client.get(url, params={"scope": "shared"}, headers=B)
        assert r.status_code == 403
    # A garbage scope is rejected rather than silently widening access.
    assert client.get("/workspace/list", params={"scope": "root"}, headers=ADMIN).status_code == 400

    # Admin sees the legacy tree — and ONLY with the explicit opt-in.
    shared = client.get("/workspace/list", params={"scope": "shared"}, headers=ADMIN).json()
    assert any(f["name"] == "legacy.step" for f in shared["files"])
    own = client.get("/workspace/list", headers=ADMIN).json()
    assert all(f["name"] != "legacy.step" for f in own["files"])

    r = client.get(
        "/workspace/download",
        params={"path": "legacy.step", "scope": "shared"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    assert r.content == b"team data"

    shared_b = client.get("/api/bucket/list", params={"scope": "shared"}, headers=ADMIN).json()
    assert any(f["name"] == "legacy.txt" for f in shared_b["files"])

    # Legacy data survived everything above, unmoved and undeleted.
    assert (settings.workspace_root / "legacy.step").read_text() == "team data"
    assert (settings.bucket_root / "legacy.txt").read_text() == "team drop"
