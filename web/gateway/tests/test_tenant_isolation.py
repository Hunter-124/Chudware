"""Tenant-isolation tests — workspace.py / bucket.py are per-account trees.

Proves that with dev-mode auth standing in for the general population of
self-registered accounts, one account can never read, overwrite, rename, or
delete another account's files or chat history, that jails reject escapes and
symlinks, that uploads are size-capped with no partials left behind, and that
the legacy shared tree is reachable only by an explicit admin opt-in.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

# Dev auth BEFORE importing the app, so X-Dev-User is accepted.
os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import accounts, app as app_mod, db, engine_bridge, mailer, orgs, plans  # noqa: E402
from gateway.config import settings  # noqa: E402
from gateway.limits import RateLimiter, user_slug  # noqa: E402
import gateway.workspace as workspace_mod  # noqa: E402

# Ordinary tenants (never admin) — the multi-tenant population.
A = {"X-Dev-User": "alice@crimson.ua.edu"}
B = {"X-Dev-User": "bob@crimson.ua.edu"}
# The only identity allowed scope=shared.
ADMIN = {"X-Dev-User": "admin@example.com"}


def _seed(email: str, display: str, *, admin: bool = False) -> None:
    """Register and activate one account, optionally promoting it.

    accounts.py deliberately exposes no role setter — admin is granted through
    the admin API and never self-assigned — so the promotion is a direct row
    update here rather than a public call that does not (and should not) exist.
    """
    accounts.ensure_account(email, display=display)
    accounts.set_status(email, "active", decided_by="test")
    if admin:
        with db.tx() as cur:
            cur.execute(
                "UPDATE accounts SET role = 'admin' WHERE email = %s", (email,)
            )


@pytest.fixture(autouse=True)
def _isolate(pg, tmp_path, monkeypatch):
    """tmp-backed workspace/bucket roots + seeded accounts + hermetic startup."""
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(settings, "data_root", tmp_path / "data")
    ws = tmp_path / "ws"        # legacy shared workspace root (pre-tenant data)
    bucket = tmp_path / "bucket"
    ws.mkdir()
    bucket.mkdir()
    monkeypatch.setattr(settings, "workspace_root", ws)
    monkeypatch.setattr(settings, "bucket_root", bucket)
    # Process-global limiters otherwise arrive with budget already spent by an
    # earlier test file; the org/team cases below make many small writes.
    monkeypatch.setattr(orgs, "api_writes", RateLimiter(10_000, 60.0))
    monkeypatch.setattr(mailer, "outbound", RateLimiter(10_000, 3600.0))
    monkeypatch.setattr(settings, "mail_global_per_hour", 10_000)
    monkeypatch.setattr(mailer, "outbound_global", RateLimiter(10_000, 3600.0, max_keys=1))

    _seed("alice@crimson.ua.edu", "Alice")
    _seed("bob@crimson.ua.edu", "Bob")
    _seed("admin@example.com", "Test Admin", admin=True)
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
    assert client.get("/workspace/download", params={"path": "evil"}, headers=A).status_code == 403
    # ...and resolving THROUGH it lands outside the jail, also refused.
    r = client.get("/workspace/download", params={"path": "evil/legacy.txt"}, headers=A)
    assert r.status_code == 403


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
    trial = plans.PLANS["trial"]
    monkeypatch.setitem(
        plans.PLANS,
        "trial",
        replace(trial, limits=replace(trial.limits, storage_quota_bytes=10)),
    )
    r = _upload_ws(client, A, name="q.bin", data=b"y" * 50)
    assert r.status_code == 507
    accounts.set_plan("alice@crimson.ua.edu", "studio")
    r = _upload_ws(client, A, name="q.bin", data=b"y" * 50)
    assert r.status_code == 200


@pytest.mark.parametrize("kind", ["file", "tree"])
def test_workspace_copy_enforces_quota_without_partial_destination(
    client, monkeypatch, kind
):
    root = _ws_user_root("alice@crimson.ua.edu")
    root.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        (root / "source.bin").write_bytes(b"123456")
        src, dst = "source.bin", "copied.bin"
    else:
        (root / "source" / "nested").mkdir(parents=True)
        (root / "source" / "nested" / "payload.bin").write_bytes(b"123456")
        src, dst = "source", "copied"

    trial = plans.PLANS["trial"]
    monkeypatch.setitem(
        plans.PLANS,
        "trial",
        replace(trial, limits=replace(trial.limits, storage_quota_bytes=10)),
    )
    r = client.post(
        "/workspace/copy", headers=A, json={"src": src, "dst": dst, "recursive": True}
    )
    assert r.status_code == 507, r.text
    assert not (root / dst).exists()
    assert (root / src).exists()


@pytest.mark.parametrize("kind", ["file", "tree"])
def test_workspace_copy_and_delete_preserve_source(client, kind):
    root = _ws_user_root("alice@crimson.ua.edu")
    root.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        source = root / "source.bin"
        destination = root / "copied.bin"
        payload = source
        copied_payload = destination
    else:
        source = root / "source"
        destination = root / "copied"
        payload = source / "nested" / "payload.bin"
        payload.parent.mkdir(parents=True)
        copied_payload = destination / "nested" / "payload.bin"
    payload.write_bytes(b"workspace payload")

    response = client.post(
        "/workspace/copy", headers=A,
        json={"src": source.name, "dst": destination.name, "recursive": True},
    )
    assert response.status_code == 200, response.text
    assert copied_payload.read_bytes() == b"workspace payload"
    response = client.post("/workspace/delete", headers=A, json={"path": destination.name})
    assert response.status_code == 200, response.text
    assert not destination.exists()
    assert payload.read_bytes() == b"workspace payload"


def test_user_slug_distinguishes_max_length_colliding_prefix_emails():
    prefix = "a" * 118
    first = f"{prefix}x@example.com"
    second = f"{prefix}y@example.com"
    first_slug = user_slug(first)
    second_slug = user_slug(second)
    assert first_slug != second_slug
    assert len(first_slug) <= 120
    assert len(second_slug) <= 120
    assert first_slug.startswith("a" * 54 + "~z")
    assert second_slug.startswith("a" * 54 + "~z")
    assert user_slug("alice@crimson.ua.edu") == "alice~40crimson.ua.edu"


def test_workspace_paths_stay_inside_the_callers_own_root(client):
    """Browser paths are relative and identity is an immutable workspace key."""
    mine = _ws_user_root("alice@crimson.ua.edu")
    theirs = _ws_user_root("bob@crimson.ua.edu")
    response = _upload_ws(client, A)
    assert response.status_code == 200
    assert response.json()["path"] == "part.step"
    assert "abs_path" not in response.json()
    assert (mine / response.json()["path"]).is_file()
    listing = client.get("/workspace/list", headers=A).json()
    assert listing["root"] == "."
    assert listing["workspace_key"] == "user:" + accounts.get_account("alice@crimson.ua.edu").uid
    for payload in (response.text, client.get("/workspace/list", headers=A).text):
        assert str(settings.workspace_root) not in payload
        assert str(theirs) not in payload


def test_files_aicm_cannot_read_across_tenants(client):
    assert _upload_ws(client, A).status_code == 200
    key = "user:" + accounts.get_account("alice@crimson.ua.edu").uid
    params = {"workspace_key": key, "path": "part.step"}
    assert client.get("/files/aicm", params=params, headers=B).status_code == 403
    assert client.get("/files/aicm", params=params, headers=A).status_code == 200
    params["path"] = str(_ws_user_root("alice@crimson.ua.edu") / "part.step")
    assert client.get("/files/aicm", params=params, headers=A).status_code == 400


def test_mesh_handoffs_require_the_accounts_cell_grant(client, monkeypatch, tmp_path):
    from gateway import cells
    monkeypatch.setattr(settings, "cells_root", tmp_path / "cells")
    principal = cells.resolve_workspace("alice@crimson.ua.edu")
    mesh_root = settings.cells_root / principal.cell_id / "state" / "mesh"
    mesh_root.mkdir(parents=True)
    name = uuid.uuid4().hex + ".aicm"
    mesh = mesh_root / name
    mesh.write_bytes(b"alice mesh")
    doc_id = "doc-" + uuid.uuid4().hex
    params = {"workspace_key": principal.workspace_key, "doc_id": doc_id}
    assert client.get("/files/aicm", params=params, headers=A).status_code == 403
    engine_bridge.grant_mesh_document(principal, doc_id, "/state/mesh/" + name)
    assert client.get("/files/aicm", params=params, headers=B).status_code == 403
    response = client.get("/files/aicm", params=params, headers=A)
    assert response.status_code == 200 and response.content == b"alice mesh"
    assert client.get("/files/aicm", params={**params, "doc_id": "other"}, headers=A).status_code == 403
    assert client.get("/files/aicm", params={**params, "shm": "legacy"}, headers=A).status_code == 400
    cells.revoke("alice@crimson.ua.edu")
    assert client.get("/files/aicm", params=params, headers=A).status_code == 403


def test_healthz_presence_is_account_scoped_except_for_admin(client, monkeypatch):
    live = app_mod.Presence()
    monkeypatch.setattr(app_mod, "presence", live)
    asyncio.run(live.join("personal:alice@crimson.ua.edu", "alice@crimson.ua.edu", "Alice"))
    asyncio.run(live.join("personal:bob@crimson.ua.edu", "bob@crimson.ua.edu", "Bob"))
    assert client.get("/healthz", headers=A).json()["presence"] == 1
    assert client.get("/healthz", headers=B).json()["presence"] == 1
    assert client.get("/healthz", headers=ADMIN).json()["presence"] == 2
    assert client.get("/healthz").json() == {"ok": True}


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
    assert listing["root"] == "."

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


@pytest.mark.parametrize("route", ["/workspace/mkdir", "/api/bucket/mkdir"])
@pytest.mark.parametrize("path", ["nul\x00path", "line\nbreak", "del\x7f"])
def test_control_character_paths_are_bad_requests_not_server_errors(client, route, path):
    assert client.post(route, headers=A, json={"path": path}).status_code == 400


def test_bucket_rejects_dotdot_absolute_and_symlink(client):
    assert client.get("/api/bucket/download", params={"path": "../x"}, headers=A).status_code == 400
    assert client.get("/api/bucket/download", params={"path": "/etc/passwd"}, headers=A).status_code == 400
    assert client.post("/api/bucket/mkdir", headers=A, json={"path": "../x"}).status_code == 400
    assert _upload_bucket(client, A).status_code == 200  # create A's root
    root_a = _bucket_user_root("alice@crimson.ua.edu")
    (root_a / "evil").symlink_to(settings.bucket_root)
    assert client.get("/api/bucket/download", params={"path": "evil"}, headers=A).status_code == 403


def test_bucket_rename_cannot_move_the_bucket_root_out_of_its_jail(client):
    # An empty/missing `src` resolves to the scope root (list and mkdir want
    # that). Rename must refuse it: `src.parent / name` sits in the shared
    # users/ parent, so renaming the root MOVED the caller's whole bucket next
    # to every other tenant's. `{"src": ""}` was the entire exploit.
    assert _upload_bucket(client, A).status_code == 200  # create A's root
    root_a = _bucket_user_root("alice@crimson.ua.edu")
    parent = root_a.parent
    for body in ({"src": "", "name": "escaped"}, {"name": "escaped"}, {"src": ".", "name": "escaped"}):
        r = client.post("/api/bucket/rename", headers=A, json=body)
        assert r.status_code == 400, (body, r.text)
    assert root_a.is_dir(), "the bucket root must still be where the tenant's scope points"
    assert (root_a / "notes.txt").is_file(), "the tenant's own file must not have moved"
    assert not (parent / "escaped").exists(), "nothing may be created beside other tenants' buckets"


# ---------------------------------------------------------------------------
# Admin escape hatch onto the legacy shared tree
# ---------------------------------------------------------------------------


def test_shared_scope_refused_and_preserves_legacy_data(client):
    (settings.workspace_root / "legacy.step").write_text("team data", encoding="utf-8")
    (settings.bucket_root / "legacy.txt").write_text("team drop", encoding="utf-8")
    for headers in (A, B, ADMIN):
        assert client.get("/workspace/list", params={"scope": "shared"}, headers=headers).status_code == 403
        assert client.get("/workspace/download", params={"path": "legacy.step", "scope": "shared"},
                          headers=headers).status_code == 403
    assert client.get("/workspace/list", params={"scope": "root"}, headers=ADMIN).status_code == 400
    own = client.get("/workspace/list", headers=ADMIN).json()
    assert all(f["name"] != "legacy.step" for f in own["files"])
    # Legacy shared storage is not admitted, even for administrators.
    assert client.get("/api/bucket/list", params={"scope": "shared"}, headers=B).status_code == 403
    assert (settings.workspace_root / "legacy.step").read_text() == "team data"
    assert (settings.bucket_root / "legacy.txt").read_text() == "team drop"


# ---------------------------------------------------------------------------
# Shared team buckets (?team=<uuid>)
# ---------------------------------------------------------------------------

ALICE = "alice@crimson.ua.edu"
BOB = "bob@crimson.ua.edu"
NO_SUCH_TEAM = "00000000-0000-4000-8000-00000000dead"


def _invite_token(to: str) -> str:
    with db.tx() as cur:
        cur.execute(
            "SELECT ctx_json FROM mail_outbox"
            " WHERE to_addr = %s AND template = 'org_invite' ORDER BY id DESC LIMIT 1",
            (to,),
        )
        row = cur.fetchone()
    assert row is not None, f"no invitation queued for {to}"
    ctx = mailer.decrypt_context(to, "org_invite", row["ctx_json"])
    return ctx["invite_url"].split("org_invite=", 1)[1].split("#", 1)[0]


def _org_with_bob(client) -> tuple[str, str]:
    """Alice owns an org on a studio plan; Bob accepts an invite into General."""
    accounts.set_plan(ALICE, "studio")
    created = client.post("/api/orgs", headers=A, json={"name": "Bridge Works"})
    assert created.status_code == 201, created.text
    org = created.json()
    team_id = org["teams"][0]["id"]
    r = client.post(
        f"/api/orgs/{org['id']}/invitations", headers=A, json={"email": BOB, "role": "member"}
    )
    assert r.status_code == 201, r.text
    accepted = client.post(
        "/api/orgs/invitations/accept", headers=B, json={"token": _invite_token(BOB)}
    )
    assert accepted.status_code == 200, accepted.text
    return org["id"], team_id


def _team_root(org_id: str, team_id: str) -> Path:
    return settings.bucket_root / "orgs" / org_id / "teams" / team_id


def test_team_bucket_is_shared_by_the_team_and_lands_outside_personal_trees(client):
    org_id, team_id = _org_with_bob(client)

    r = _upload_bucket(client, A, name="shared.txt", data=b"team drop")
    assert r.status_code == 200
    r = client.post(
        "/api/bucket/upload",
        headers=A,
        params={"team": team_id},
        files=[("files", ("plan.txt", b"team plan"))],
        data={"dir": ""},
    )
    assert r.status_code == 200, r.text

    # It landed in the org/team tree, not in either personal tree.
    assert (_team_root(org_id, team_id) / "plan.txt").is_file()
    assert not (_bucket_user_root(ALICE) / "plan.txt").exists()
    assert not (_bucket_user_root(BOB) / "plan.txt").exists()

    # Bob sees it on the team destination and can read it back...
    listing = client.get("/api/bucket/list", headers=B, params={"team": team_id}).json()
    assert [f["name"] for f in listing["files"]] == ["plan.txt"]
    assert listing["scope"] == "team"
    assert listing["team"]["id"] == team_id
    assert listing["team"]["org_id"] == org_id
    assert listing["team"]["name"] == "General"
    assert listing["root"] == "."
    got = client.get(
        "/api/bucket/download", headers=B, params={"path": "plan.txt", "team": team_id}
    )
    assert got.status_code == 200
    assert got.content == b"team plan"

    # ...and can write to it, while Alice's personal drop stays private.
    r = client.post(
        "/api/bucket/upload",
        headers=B,
        params={"team": team_id},
        files=[("files", ("bob.txt", b"from bob"))],
        data={"dir": ""},
    )
    assert r.status_code == 200, r.text
    personal = client.get("/api/bucket/list", headers=B).json()
    assert [f["name"] for f in personal["files"]] == []
    assert personal["scope"] == "personal"
    assert personal["team"] is None
    assert personal["root"] == "."


def test_personal_bucket_listing_keeps_its_existing_shape(client):
    assert _upload_bucket(client, A, name="notes.txt").status_code == 200
    listing = client.get("/api/bucket/list", headers=A).json()
    assert {"root", "dir", "dirs", "files"} <= set(listing)
    assert listing["root"] == "."
    assert [f["name"] for f in listing["files"]] == ["notes.txt"]


def test_team_bucket_is_404_for_outsiders_including_the_site_admin(client):
    org_id, team_id = _org_with_bob(client)
    client.post(
        "/api/bucket/upload",
        headers=A,
        params={"team": team_id},
        files=[("files", ("plan.txt", b"team plan"))],
        data={"dir": ""},
    )
    # The site admin runs the gateway; that is not membership in a customer org.
    for params in ({"team": team_id},):
        assert client.get("/api/bucket/list", headers=ADMIN, params=params).status_code == 404
        assert client.get(
            "/api/bucket/download", headers=ADMIN, params={"path": "plan.txt", **params}
        ).status_code == 404
        assert client.post(
            "/api/bucket/mkdir", headers=ADMIN, params=params, json={"path": "x"}
        ).status_code == 404
        assert client.post(
            "/api/bucket/delete", headers=ADMIN, params=params, json={"path": "plan.txt"}
        ).status_code == 404
    assert (_team_root(org_id, team_id) / "plan.txt").is_file()


@pytest.mark.parametrize(
    "bad", ["not-a-uuid", NO_SUCH_TEAM, NO_SUCH_TEAM.upper(), "../../etc", ""]
)
def test_bad_team_ids_never_reach_the_filesystem(client, bad):
    _org_with_bob(client)
    r = client.get("/api/bucket/list", headers=A, params={"team": bad} if bad else {})
    # An empty selector is simply the personal tree; anything else is opaque.
    assert r.status_code == (200 if bad == "" else 404)
    assert not (settings.bucket_root / "orgs").exists() or [
        p.name for p in (settings.bucket_root / "orgs").iterdir()
    ]
    assert not (settings.bucket_root.parent / "etc").exists()


def test_team_and_shared_scope_cannot_be_combined(client):
    _, team_id = _org_with_bob(client)
    r = client.get(
        "/api/bucket/list", headers=ADMIN, params={"team": team_id, "scope": "shared"}
    )
    assert r.status_code == 400


def test_removed_member_loses_team_bucket_access_immediately(client):
    org_id, team_id = _org_with_bob(client)
    assert client.get("/api/bucket/list", headers=B, params={"team": team_id}).status_code == 200

    # Off the team but still in the org: an ordinary member cannot even see it.
    assert client.delete(
        f"/api/orgs/{org_id}/teams/{team_id}/members/{BOB}", headers=A
    ).status_code == 200
    assert client.get("/api/bucket/list", headers=B, params={"team": team_id}).status_code == 404
    assert client.post(
        "/api/bucket/upload",
        headers=B,
        params={"team": team_id},
        files=[("files", ("sneak.txt", b"nope"))],
        data={"dir": ""},
    ).status_code == 404

    # Back on the team, access returns; out of the org entirely, it goes again.
    assert client.post(
        f"/api/orgs/{org_id}/teams/{team_id}/members", headers=A, json={"email": BOB}
    ).status_code == 201
    assert client.get("/api/bucket/list", headers=B, params={"team": team_id}).status_code == 200
    assert client.delete(f"/api/orgs/{org_id}/members/{BOB}", headers=A).status_code == 200
    assert client.get("/api/bucket/list", headers=B, params={"team": team_id}).status_code == 404


def test_org_admin_reaches_a_team_they_never_joined(client):
    org_id, team_id = _org_with_bob(client)
    made = client.post(f"/api/orgs/{org_id}/teams", headers=A, json={"name": "Fixtures"})
    assert made.status_code == 201, made.text
    other_team = made.json()["team"]["id"]
    # Bob is an ordinary member and was never added: opaque 404.
    assert client.get(
        "/api/bucket/list", headers=B, params={"team": other_team}
    ).status_code == 404
    # Alice owns the org, so every team's tree is hers to reach.
    assert client.get(
        "/api/bucket/list", headers=A, params={"team": other_team}
    ).status_code == 200


def test_owner_downgrade_and_suspension_block_shared_writes_but_not_reads(client):
    org_id, team_id = _org_with_bob(client)
    assert client.post(
        "/api/bucket/upload",
        headers=B,
        params={"team": team_id},
        files=[("files", ("plan.txt", b"team plan"))],
        data={"dir": ""},
    ).status_code == 200

    accounts.set_plan(ALICE, "desktop")  # the owner's plan no longer funds it
    assert client.get("/api/bucket/list", headers=B, params={"team": team_id}).status_code == 200
    assert client.get(
        "/api/bucket/download", headers=B, params={"path": "plan.txt", "team": team_id}
    ).status_code == 200
    for call in (
        lambda: client.post(
            "/api/bucket/upload",
            headers=B,
            params={"team": team_id},
            files=[("files", ("more.txt", b"blocked"))],
            data={"dir": ""},
        ),
        lambda: client.post(
            "/api/bucket/mkdir", headers=B, params={"team": team_id}, json={"path": "sub"}
        ),
        lambda: client.post(
            "/api/bucket/rename",
            headers=B,
            params={"team": team_id},
            json={"src": "plan.txt", "name": "renamed.txt"},
        ),
        lambda: client.post(
            "/api/bucket/delete", headers=B, params={"team": team_id}, json={"path": "plan.txt"}
        ),
    ):
        assert call().status_code == 402
    assert (_team_root(org_id, team_id) / "plan.txt").is_file()

    # Restored plan, writes resume; a suspended owner freezes them again.
    accounts.set_plan(ALICE, "studio")
    assert client.post(
        "/api/bucket/mkdir", headers=B, params={"team": team_id}, json={"path": "sub"}
    ).status_code == 200
    accounts.set_status(ALICE, "suspended", decided_by="test")
    assert client.post(
        "/api/bucket/mkdir", headers=B, params={"team": team_id}, json={"path": "sub2"}
    ).status_code == 402
    assert client.get("/api/bucket/list", headers=B, params={"team": team_id}).status_code == 200


def test_team_bucket_quota_comes_from_the_org_owner_plan(client, monkeypatch):
    _, team_id = _org_with_bob(client)
    studio = plans.PLANS["studio"]
    monkeypatch.setitem(
        plans.PLANS,
        "studio",
        replace(studio, limits=replace(studio.limits, storage_quota_bytes=10)),
    )
    # Bob is on the default trial plan, but the OWNER's (shrunken) studio quota
    # is what governs the shared tree.
    r = client.post(
        "/api/bucket/upload",
        headers=B,
        params={"team": team_id},
        files=[("files", ("big.bin", b"y" * 50))],
        data={"dir": ""},
    )
    assert r.status_code == 507
    # The same account's personal tree is unaffected: that one is Bob's plan.
    assert _upload_bucket(client, B, name="big.bin", data=b"y" * 50).status_code == 200


@pytest.mark.parametrize("surface", ["workspace", "bucket"])
def test_slow_storage_does_not_block_gateway_health(client, monkeypatch, surface):
    """A network-backed quota scan must not monopolize the ASGI event loop."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import gateway.bucket as bucket_mod

    entered = Event()
    release = Event()

    def slow_quota(*args, **kwargs):
        entered.set()
        assert release.wait(10), "storage operation was never released"

    module = workspace_mod if surface == "workspace" else bucket_mod
    upload = _upload_ws if surface == "workspace" else _upload_bucket
    monkeypatch.setattr(module, "enforce_disk_quota", slow_quota)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(upload, client, A, name="slow-storage.txt")
        try:
            assert entered.wait(5), "upload did not enter storage operation"
            health = pool.submit(client.get, "/healthz")
            assert health.result(timeout=2).status_code == 200
        finally:
            release.set()
        assert writing.result(timeout=5).status_code == 200


def test_workspace_and_bucket_serialize_team_quota_mutations(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    from threading import Event
    import gateway.bucket as bucket_mod

    _, team_id = _org_with_bob(client)
    original_limits = plans.limits_for
    import gateway.limits as limits_mod
    monkeypatch.setattr(limits_mod, "limits_for",
                        lambda plan: replace(original_limits(plan), storage_quota_bytes=20_000))
    entered = Event()
    release = Event()
    second_admitted = Event()
    original_store = workspace_mod.store_upload
    original_scope = bucket_mod._caller_scope

    def held_store(*args, **kwargs):
        # Native storage mutation, not the earlier pre-buffer quota preflight,
        # is the critical section shared by workspace and bucket.
        entered.set()
        assert release.wait(10)
        return original_store(*args, **kwargs)

    def admitted_scope(*args, **kwargs):
        result = original_scope(*args, **kwargs)
        second_admitted.set()
        return result

    monkeypatch.setattr(workspace_mod, "store_upload", held_store)
    monkeypatch.setattr(bucket_mod, "_caller_scope", admitted_scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/workspace/upload?team=" + team_id,
                            headers=A, files={"file": ("first.bin", b"a" * 12_000)})
        try:
            assert entered.wait(5)
            second = pool.submit(client.post, "/api/bucket/upload?team=" + team_id,
                                 headers=B, files={"files": ("second.bin", b"b" * 12_000)})
            assert second_admitted.wait(5)
            # B has authorized the same root, but cannot mutate it halfway
            # through A's quota admission. This is only a gateway-process lock.
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.5)
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
        assert second.result(timeout=5).status_code == 507
