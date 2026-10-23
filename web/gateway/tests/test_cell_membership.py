"""Real PostgreSQL regressions for immutable cell identities and membership locks."""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException
from psycopg import sql

from gateway import accounts, db, orgs
from gateway.config import settings
from gateway.limits import RateLimiter

OWNER = "cell-owner@example.com"
MEMBER = "cell-member@example.com"


@pytest.fixture
def membership(pg, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspace")
    monkeypatch.setattr(settings, "bucket_root", tmp_path / "bucket")
    monkeypatch.setattr(settings, "data_root", tmp_path / "data")
    monkeypatch.setattr(orgs, "api_writes", RateLimiter(10_000, 60))
    monkeypatch.setattr(accounts, "api_writes", RateLimiter(10_000, 60))
    monkeypatch.setattr(orgs, "_caller", lambda request: SimpleNamespace(email=request.email))
    calls = []
    cell_hooks = SimpleNamespace(
        revoke=lambda email, team_id=None: calls.append(("actor", email, team_id)),
        revoke_team=lambda team_id: calls.append(("team", team_id)),
    )
    monkeypatch.setattr(accounts, "_cells", lambda: cell_hooks)
    monkeypatch.setattr(orgs, "_cells", lambda: cell_hooks)
    chat_hooks = SimpleNamespace(revoke=lambda *args: None, revoke_team=lambda *args: None)
    monkeypatch.setattr(orgs, "_chat_hub", lambda: chat_hooks)
    for email in (OWNER, MEMBER):
        accounts.ensure_account(email)
        accounts.set_status(email, "active")
        accounts.set_plan(email, "studio")
    org_id, team_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.tx() as cur:
        cur.execute("INSERT INTO organizations (id,name) VALUES (%s,'Cell test')", (org_id,))
        cur.execute(
            "INSERT INTO org_members (org_id,email,role) VALUES (%s,%s,'owner'),(%s,%s,'member')",
            (org_id, OWNER, org_id, MEMBER),
        )
        cur.execute("INSERT INTO teams (id,org_id,name) VALUES (%s,%s,'Design')", (team_id, org_id))
        cur.execute(
            "INSERT INTO team_members (team_id,email,role) VALUES (%s,%s,'member')",
            (team_id, MEMBER),
        )
    calls.clear()
    return SimpleNamespace(org_id=org_id, team_id=team_id, calls=calls)


def invoke(fn, *args):
    return asyncio.run(fn(*args))


def test_uid_default_is_immutable_and_unique(pg):
    first = accounts.ensure_account("uid-a@example.com")
    second = accounts.ensure_account("uid-b@example.com")
    assert str(uuid.UUID(first.uid)) == first.uid
    assert first.uid != second.uid
    assert accounts.ensure_account(first.email).uid == first.uid
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        with db.tx() as cur:
            cur.execute("UPDATE accounts SET uid = %s WHERE email = %s", (second.uid, first.email))
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        with db.tx() as cur:
            cur.execute("UPDATE accounts SET uid = NULL WHERE email = %s", (first.email,))
    with db.tx() as cur:
        cur.execute("UPDATE accounts SET uid = uid WHERE email = %s", (first.email,))
    assert accounts.get_account(first.email).uid == first.uid


def test_migration_backfills_distinct_uids_and_accepts_viewer(pg):
    # Exercise 0022 against an old schema, not only a fresh migrated database.
    schema = "cell_migration_" + uuid.uuid4().hex
    with db.tx() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        cur.execute("CREATE TABLE accounts (email TEXT PRIMARY KEY)")
        cur.execute("INSERT INTO accounts VALUES ('old-a@example.com'),('old-b@example.com')")
        cur.execute("CREATE TABLE team_members (role TEXT CHECK (role IN ('manager','member')))")
        migration = Path(accounts.__file__).parent / "migrations/0022_account_uid_viewer.sql"
        cur.execute(migration.read_text())
        cur.execute("SELECT uid FROM accounts")
        values = [str(row["uid"]) for row in cur.fetchall()]
        assert len(set(values)) == 2
        assert all(str(uuid.UUID(value)) == value for value in values)
        cur.execute("INSERT INTO accounts (email) VALUES ('new@example.com') RETURNING uid")
        assert str(cur.fetchone()["uid"]) not in values
        cur.execute("INSERT INTO team_members (role) VALUES ('viewer')")
        cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_deletion_tombstones_slug_trees_and_reregisters_new_uid(membership, monkeypatch):
    from gateway import cells
    from gateway.limits import user_slug

    email = MEMBER
    original = accounts.get_account(email)
    workspace = settings.user_workspace_root(user_slug(email))
    bucket = settings.user_bucket_root(user_slug(email))
    for root in (workspace, bucket):
        root.mkdir(parents=True)
        (root / "private.step+").write_text("retained deleted data")
    # Keep the real filesystem tombstoning; observe only the live-engine hook.
    monkeypatch.setattr(accounts, "_cells", lambda: SimpleNamespace(
        revoke=lambda email: membership.calls.append(("delete", email)),
        tombstone_personal=cells.tombstone_personal,
    ))
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO cell_principal_epochs (workspace_key,actor_key,epoch) VALUES (%s,%s,7)",
            ("user:" + original.uid, original.uid),
        )
    assert accounts.delete_account(email).uid == original.uid
    assert accounts.get_account(email) is None
    assert not workspace.exists() and not bucket.exists()
    assert membership.calls == [("delete", email)]
    replacement = accounts.ensure_account(email)
    assert replacement.uid != original.uid
    workspace.mkdir(parents=True)
    bucket.mkdir(parents=True)
    assert not (workspace / "private.step+").exists()
    assert not (bucket / "private.step+").exists()
    retained = list(settings.workspace_root.rglob("private.step+")) + list(settings.bucket_root.rglob("private.step+"))
    assert len(retained) == 2
    assert all(path.read_text() == "retained deleted data" for path in retained)
    with db.tx() as cur:
        cur.execute("SELECT epoch FROM cell_principal_epochs WHERE actor_key = %s", (original.uid,))
        assert cur.fetchone()["epoch"] == 7


def test_account_owner_cannot_be_deleted(membership):
    with pytest.raises(HTTPException) as error:
        accounts.delete_account(OWNER)
    assert error.value.status_code == 409
    assert accounts.get_account(OWNER) is not None


def test_viewer_schema_and_write_denial_preserve_org_manager_authority(membership):
    team = membership.team_id
    invoke(orgs.set_team_member_role, membership.org_id, team, MEMBER, SimpleNamespace(email=OWNER), {"role": "viewer"})
    assert orgs.resolve_team_bucket(MEMBER, team, write=False).team_role == "viewer"
    with pytest.raises(HTTPException) as error:
        orgs.resolve_team_bucket(MEMBER, team, write=True)
    assert error.value.status_code == 403
    assert error.value.detail == "forbidden_right"
    assert orgs.resolve_team_bucket(OWNER, team, write=True).org_role == "owner"
    assert membership.calls == [("actor", MEMBER, team)]
    # Explicit viewer membership cannot cap an organization's administrator.
    with db.tx() as cur:
        cur.execute("UPDATE org_members SET role = 'admin' WHERE org_id = %s AND email = %s", (membership.org_id, MEMBER))
    assert orgs.resolve_team_bucket(MEMBER, team, write=True).org_role == "admin"


@pytest.mark.parametrize("first", ["transfer", "remove"])
def test_concurrent_transfer_removal_preserves_owner(membership, monkeypatch, first):
    """Hold the first writer after authorization; the second must block there."""
    locked, release, second_attempt = threading.Event(), threading.Event(), threading.Event()
    original_require, original_lock, original_tx = orgs._require_org, orgs._lock_org, orgs.tx
    second_pid = []
    local = threading.local()

    @contextmanager
    def observed_tx():
        with original_tx() as cur:
            if getattr(local, "operation", "") != first:
                cur.execute("SELECT pg_backend_pid() AS pid")
                second_pid.append(cur.fetchone()["pid"])
            yield cur

    def observed_lock(cur, org_id):
        if getattr(local, "operation", "") != first:
            second_attempt.set()
        return original_lock(cur, org_id)

    def hold_authorized(*args, **kwargs):
        result = original_require(*args, **kwargs)
        if getattr(local, "operation", "") == first:
            locked.set()
            assert release.wait(10), "first writer was not released"
        return result

    monkeypatch.setattr(orgs, "tx", observed_tx)
    monkeypatch.setattr(orgs, "_lock_org", observed_lock)
    monkeypatch.setattr(orgs, "_require_org", hold_authorized)

    def mutate(operation):
        local.operation = operation
        try:
            request = SimpleNamespace(email=OWNER)
            if operation == "transfer":
                invoke(orgs.transfer_ownership, membership.org_id, request, {"email": MEMBER})
            else:
                invoke(orgs.remove_member, membership.org_id, MEMBER, request)
            return 200
        except HTTPException as error:
            return error.status_code

    other = "remove" if first == "transfer" else "transfer"
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(mutate, first)
        try:
            assert locked.wait(10)
            loser = pool.submit(mutate, other)
            assert second_attempt.wait(10)
            deadline = time.monotonic() + 10
            blockers = []
            while not blockers and time.monotonic() < deadline:
                with db.tx() as cur:
                    cur.execute("SELECT pg_blocking_pids(%s) AS blockers", (second_pid[0],))
                    blockers = cur.fetchone()["blockers"]
                if not blockers:
                    time.sleep(0.01)
            assert blockers, "second writer read permissions without waiting for organization lock"
        finally:
            release.set()
        assert winner.result(timeout=10) == 200
        assert loser.result(timeout=10) == (400 if first == "transfer" else 404)
    with db.tx() as cur:
        cur.execute("SELECT email FROM org_members WHERE org_id = %s AND role = 'owner'", (membership.org_id,))
        assert [row["email"] for row in cur.fetchall()] == [MEMBER if first == "transfer" else OWNER]


@pytest.mark.parametrize("mutation", ["suspended", "denied", "active", "plan", "role", "subscription"])
def test_account_changes_revoke_idle_engine_principals(membership, mutation):
    if mutation == "plan":
        accounts.set_plan(MEMBER, "desktop")
    elif mutation == "role":
        accounts.set_role(MEMBER, "admin")
    elif mutation == "subscription":
        accounts.set_subscription(MEMBER, subscription_status="canceled", is_edu=False)
    else:
        accounts.set_status(MEMBER, mutation)
    assert membership.calls == [("actor", MEMBER, None)]


@pytest.mark.parametrize("mutation", ["org_role", "org_remove", "team_role", "team_remove", "team_add", "invitation_accept", "team_delete", "org_delete", "transfer"])
def test_membership_changes_revoke_idle_engine_principals(membership, mutation):
    request = SimpleNamespace(email=OWNER)
    org, team = membership.org_id, membership.team_id
    if mutation == "org_role":
        invoke(orgs.set_member_role, org, MEMBER, request, {"role": "admin"})
    elif mutation == "org_remove":
        invoke(orgs.remove_member, org, MEMBER, request)
    elif mutation == "team_role":
        invoke(orgs.set_team_member_role, org, team, MEMBER, request, {"role": "viewer"})
    elif mutation == "team_add":
        with db.tx() as cur:
            cur.execute("DELETE FROM team_members WHERE team_id = %s AND email = %s", (team, MEMBER))
        invoke(orgs.add_team_member, org, team, request, {"email": MEMBER, "role": "viewer"})
    elif mutation == "invitation_accept":
        invitation = str(uuid.uuid4())
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO org_invitations (id,org_id,email,role,token_hash,expires_at)"
                " VALUES (%s,%s,%s,'member',%s,now() + interval '1 hour')",
                (invitation, org, MEMBER, orgs._token_hash(invitation)),
            )
        invoke(orgs.accept_invitation, SimpleNamespace(email=MEMBER), {"invitation_id": invitation})
    elif mutation == "team_remove":
        invoke(orgs.remove_team_member, org, team, MEMBER, request)
    elif mutation == "team_delete":
        invoke(orgs.delete_team, org, team, request)
    elif mutation == "org_delete":
        invoke(orgs.delete_organization, org, request)
    else:
        invoke(orgs.transfer_ownership, org, request, {"email": MEMBER})
    expected = [("team", team)] if mutation in {"team_delete", "org_delete", "transfer"} else [("actor", MEMBER, team)]
    assert membership.calls == expected


def test_customer_security_endpoints_are_account_local(membership, monkeypatch):
    monkeypatch.setattr(accounts, "_require_admin", lambda request: SimpleNamespace(email="operator@example.com"))
    for email in (OWNER, MEMBER):
        accounts.set_plan(email, "enterprise")
    invoke(
        accounts.admin_set_security_policy, OWNER, None,
        {"policy": "customer_endpoint", "endpoint": "https://owner.example/telemetry"},
    )
    invoke(
        accounts.admin_set_security_policy, MEMBER, None,
        {"policy": "customer_endpoint", "endpoint": "https://member.example/telemetry"},
    )
    assert invoke(accounts.admin_get_security_policy, OWNER, None)["endpoint"] == "https://owner.example/telemetry"
    assert invoke(accounts.admin_get_security_policy, MEMBER, None)["endpoint"] == "https://member.example/telemetry"
    invoke(accounts.admin_set_security_policy, MEMBER, None, {"policy": "standard", "endpoint": None})
    assert invoke(accounts.admin_get_security_policy, MEMBER, None)["endpoint"] is None
    assert invoke(accounts.admin_get_security_policy, OWNER, None)["endpoint"] == "https://owner.example/telemetry"


def test_admin_deletion_endpoint_uses_identity_lifecycle(membership, monkeypatch):
    from gateway import cells

    monkeypatch.setattr(accounts, "_require_admin", lambda request: SimpleNamespace(email="operator@example.com"))
    monkeypatch.setattr(accounts, "_cells", lambda: SimpleNamespace(
        revoke=lambda email: membership.calls.append(("delete", email)),
        tombstone_personal=cells.tombstone_personal,
    ))
    assert invoke(accounts.admin_delete_account, MEMBER, None) == {"ok": True, "email": MEMBER}
    assert accounts.get_account(MEMBER) is None
    assert membership.calls == [("delete", MEMBER)]
    with pytest.raises(HTTPException) as error:
        invoke(accounts.admin_delete_account, MEMBER, None)
    assert error.value.status_code == 404
