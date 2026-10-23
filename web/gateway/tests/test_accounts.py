"""Account store: registration, approval transitions, auto-approve, migration."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))


@pytest.fixture()
def accounts_env(pg, tmp_path, monkeypatch, reload_settings):
    """Fresh settings bound to a tmp_path data root and a virgin schema.

    ``reload_settings`` mutates the ONE settings object rather than swapping
    ``config.settings`` for a new one, so modules that captured it at import time
    (``auth``, ``limits``, ``accounts``) do not silently keep reading the old
    values — see the fixture's own note for what that cost.
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CHUDWARE_WEB_DATA_ROOT", str(data))
    monkeypatch.delenv("CHUDWARE_WEB_AUTO_APPROVE_DOMAINS", raising=False)

    reload_settings()

    import accounts  # type: ignore
    import config  # type: ignore

    return accounts, config, data


def _row_count(_data: Path) -> int:
    import db  # type: ignore

    with db.tx() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM accounts")
        return int(cur.fetchone()["n"])


# ---------------------------------------------------------------------------
# Registration / authorization basics
# ---------------------------------------------------------------------------


def test_unknown_email_becomes_pending_unauthorized_not_admin(accounts_env):
    accounts, _, _ = accounts_env
    acct = accounts.ensure_account("Stranger@Example.com", "Stranger")

    assert acct.email == "stranger@example.com"  # lowercased
    assert acct.status == "pending"
    assert acct.role == "user"
    assert acct.plan == "trial"
    assert not accounts.is_authorized(acct)
    assert accounts.effective_role(acct) == "user"
    assert accounts.effective_plan(acct) == "trial"


def test_ensure_account_idempotent_and_never_resurrects_denied(accounts_env):
    accounts, _, data = accounts_env
    first = accounts.ensure_account("denied@example.com", "First")
    second = accounts.ensure_account("denied@example.com", "Changed Name")

    assert first.created_at == second.created_at  # no rewrite
    assert second.display == "First"              # existing row wins
    assert _row_count(data) == 1

    accounts.set_status("denied@example.com", "denied", decided_by="admin@example.com")
    third = accounts.ensure_account("denied@example.com", "Again")
    assert third.status == "denied"               # not resurrected as pending
    assert not accounts.is_authorized(third)
    assert third.decided_by == "admin@example.com"
    assert _row_count(data) == 1


def test_approve_deny_suspend_transitions(accounts_env):
    accounts, _, _ = accounts_env
    acct = accounts.ensure_account("flow@example.com")
    assert not accounts.is_authorized(acct)

    acct = accounts.set_status("flow@example.com", "active", decided_by="admin@x.com")
    assert acct.status == "active"
    assert accounts.is_authorized(acct)
    assert acct.decided_at is not None
    assert acct.decided_by == "admin@x.com"

    acct = accounts.set_status("flow@example.com", "suspended", decided_by="admin@x.com")
    assert acct.status == "suspended"
    assert not accounts.is_authorized(acct)

    acct = accounts.set_status("flow@example.com", "active", decided_by="admin@x.com")
    assert accounts.is_authorized(acct)

    acct = accounts.set_status("flow@example.com", "denied", decided_by="admin@x.com")
    assert acct.status == "denied"
    assert not accounts.is_authorized(acct)

    # Unknown emails are a no-op, never an implicit create.
    assert accounts.set_status("ghost@example.com", "active") is None
    assert accounts.get_account("ghost@example.com") is None

    with pytest.raises(ValueError):
        accounts.set_status("flow@example.com", "bogus")


@pytest.mark.parametrize("status", ["suspended", "denied"])
def test_status_revokes_dormant_sessions_before_reactivation(
    accounts_env, monkeypatch, status
):
    import auth  # type: ignore
    import authn  # type: ignore
    import chat  # type: ignore
    import db  # type: ignore
    import limits  # type: ignore
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    accounts, config, _ = accounts_env
    monkeypatch.setattr(config.settings, "dev_mode", False)
    monkeypatch.setattr(config.settings, "session_secret", "test-status-revocation")
    limits.login_by_email._hits.clear()
    limits.login_by_ip._hits.clear()
    email = "dormant@example.com"
    password = "correct horse battery staple"
    accounts.ensure_account(email)
    authn.set_password(email, password, verified=True)
    authn.start_trial(email)
    accounts.ensure_account("unaffected@example.com")
    authn.start_trial("unaffected@example.com")
    unaffected = authn.create_session("unaffected@example.com")
    revoked = []
    monkeypatch.setattr(chat.hub, "revoke", revoked.append)

    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(authn.router)
    app.include_router(accounts.router)
    with TestClient(app, base_url="https://testserver") as client:
        assert client.post(
            "/api/auth/login", json={"email": email, "password": password}
        ).status_code == 200
        dormant = client.cookies[authn.SESSION_COOKIE]
        other = authn.create_session(email)
        expired = authn.create_session(email, ttl=timedelta(seconds=-1))
        already_revoked = authn.create_session(email)
        authn.revoke_session(already_revoked)
        with db.tx() as cur:
            cur.execute(
                "SELECT revoked_at FROM sessions WHERE sid = %s",
                (authn.token_hash(already_revoked),),
            )
            original_revoked_at = cur.fetchone()["revoked_at"]

        # No request or lookup while inactive: middleware cannot do the revoking.
        accounts.set_status(
            email.upper(), status, decided_by="operator@example.com", notes="blocked"
        )
        accounts.set_status(email, "active", decided_by="operator@example.com")
        assert revoked == [email]
        with db.tx() as cur:
            cur.execute("SELECT sid, revoked_at FROM sessions WHERE email = %s", (email,))
            rows = cur.fetchall()
        assert len(rows) == 4
        assert all(row["revoked_at"] is not None for row in rows)
        assert next(
            row["revoked_at"] for row in rows
            if row["sid"] == authn.token_hash(already_revoked)
        ) == original_revoked_at
        for sid in (dormant, other, expired, already_revoked):
            assert authn.lookup_session(sid) is None
        assert authn.lookup_session(unaffected) == "unaffected@example.com"
        assert client.get("/api/me/account").status_code == 401

        assert client.post(
            "/api/auth/login", json={"email": email, "password": password}
        ).status_code == 200
        fresh = client.cookies[authn.SESSION_COOKIE]
        assert fresh not in (dormant, other, expired, already_revoked)
        assert authn.lookup_session(fresh) == email
        assert client.get("/api/me/account").status_code == 200


@pytest.mark.parametrize("status", ["suspended", "denied"])
def test_session_revocation_failure_rolls_back_status_decision(
    accounts_env, monkeypatch, status
):
    import authn  # type: ignore
    import chat  # type: ignore
    import db  # type: ignore
    import psycopg

    accounts, _, _ = accounts_env
    email = "rollback@example.com"
    accounts.ensure_account(email)
    before = accounts.set_status(
        email, "active", decided_by="original@example.com", notes="original decision"
    )
    sid = authn.create_session(email)
    revoked = []
    monkeypatch.setattr(chat.hub, "revoke", revoked.append)
    with db.tx() as cur:
        cur.execute("""
            CREATE FUNCTION reject_session_revocation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'session revocation failed';
            END;
            $$
        """)
        cur.execute("""
            CREATE TRIGGER reject_session_revocation BEFORE UPDATE ON sessions
            FOR EACH ROW EXECUTE FUNCTION reject_session_revocation()
        """)
    with pytest.raises(psycopg.errors.RaiseException, match="session revocation failed"):
        accounts.set_status(
            email, status, decided_by="new@example.com", notes="must roll back"
        )
    assert accounts.get_account(email) == before
    assert authn.lookup_session(sid) == email
    assert revoked == []
    with db.tx() as cur:
        cur.execute("DROP TRIGGER reject_session_revocation ON sessions")
        cur.execute("DROP FUNCTION reject_session_revocation()")
    assert accounts.set_status(email, status).status == status
    assert authn.lookup_session(sid) is None
    assert revoked == [email]


@pytest.mark.parametrize("status", ["suspended", "denied"])
def test_enclosing_rollback_restores_status_and_sessions(accounts_env, status):
    import authn  # type: ignore
    import db  # type: ignore

    accounts, _, _ = accounts_env
    email = "outer-rollback@example.com"
    accounts.ensure_account(email)
    before = accounts.set_status(email, "active", decided_by="operator@example.com")
    sid = authn.create_session(email)
    with pytest.raises(RuntimeError, match="abort enclosing decision"):
        with db.tx() as cur:
            accounts.set_status(email, status, notes="rolled back")
            cur.execute(
                "SELECT revoked_at FROM sessions WHERE sid = %s",
                (authn.token_hash(sid),),
            )
            assert cur.fetchone()["revoked_at"] is not None
            raise RuntimeError("abort enclosing decision")
    assert accounts.get_account(email) == before
    assert authn.lookup_session(sid) == email


def test_pending_count_and_listing(accounts_env):
    accounts, _, _ = accounts_env
    for i in range(5):
        accounts.ensure_account(f"u{i}@example.com")
    accounts.set_status("u0@example.com", "active", decided_by="a")
    accounts.set_status("u1@example.com", "denied", decided_by="a")

    assert accounts.pending_count() == 3

    pending, total = accounts.list_accounts("pending")
    assert total == 3 and len(pending) == 3
    assert all(a.status == "pending" for a in pending)

    everything, total = accounts.list_accounts(None, limit=2, offset=0)
    assert total == 5 and len(everything) == 2  # paginated

    with pytest.raises(ValueError):
        accounts.list_accounts("bogus")


# ---------------------------------------------------------------------------
# Registration defaults + privilege escalation guards
# ---------------------------------------------------------------------------


def test_every_new_account_is_pending_regardless_of_domain(accounts_env):
    """No domain self-activates any more.

    Auto-approval by email domain was deleted with users.yaml: activation is
    now earned by verifying the address (authn.start_trial promotes only a
    *pending* row) or granted by an admin. A domain that quietly created
    `active`/`trial` rows would strand every one of its registrants off the
    verified trial window, because there would be no pending row left to promote.
    """
    accounts, _, _ = accounts_env
    for email in ("student@crimson.ua.edu", "staff@ua.edu", "person@other.net"):
        acct = accounts.ensure_account(email, "Someone")
        assert acct.status == "pending", email
        assert acct.plan == "trial"
        assert acct.role == "user"
        assert acct.decided_by == ""
        assert not accounts.is_authorized(acct)


def test_admin_role_never_self_assigned(accounts_env):
    accounts, _, _ = accounts_env

    # No parameter of ensure_account can influence role.
    for email in ("a@ua.edu", "b@example.com"):
        assert accounts.ensure_account(email).role == "user"

    # set_status (the only status mutation) cannot grant admin either.
    accounts.ensure_account("c@example.com")
    acct = accounts.set_status("c@example.com", "active", decided_by="admin@x.com")
    assert acct.role == "user"
    assert accounts.effective_role(acct) == "user"

    # A crafted Account object cannot escalate: effective_role collapses
    # anything that is not an active account to plain user.
    forged = accounts.Account(
        email="forged@example.com", display="", status="pending", plan="free",
        uid="00000000-0000-4000-8000-000000000001",
        role="admin", created_at=0.0, decided_at=None, decided_by="",
        stripe_customer_id="", notes="",
    )
    assert accounts.effective_role(forged) == "user"
    assert not accounts.is_authorized(forged)
    assert accounts.effective_role(None) == "user"

    # The public router exposes no role-setting endpoint at all.
    paths = {r.path for r in accounts.router.routes}
    assert not any("role" in p for p in paths)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_ensure_account_single_row(accounts_env):
    accounts, _, data = accounts_env
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: accounts.ensure_account("race@example.com", "Racer"),
            range(32),
        ))
    assert _row_count(data) == 1
    assert all(r.email == "race@example.com" for r in results)
    assert len({r.created_at for r in results}) == 1  # one insert, shared row


# ---------------------------------------------------------------------------
# Store helpers used by billing
# ---------------------------------------------------------------------------


def test_stripe_customer_lookup_roundtrip(accounts_env):
    accounts, _, _ = accounts_env
    accounts.ensure_account("pay@example.com")
    assert accounts.get_account_by_stripe_customer("cus_123") is None

    acct = accounts.set_stripe_customer("pay@example.com", "cus_123")
    assert acct.stripe_customer_id == "cus_123"
    found = accounts.get_account_by_stripe_customer("cus_123")
    assert found is not None and found.email == "pay@example.com"

    assert accounts.set_stripe_customer("ghost@example.com", "cus_x") is None
    assert accounts.set_plan("ghost@example.com", "desktop") is None
    with pytest.raises(ValueError):
        accounts.set_plan("pay@example.com", "team")


def test_pending_page_data(accounts_env):
    accounts, _, _ = accounts_env
    acct = accounts.ensure_account("waiting@example.com", "Waiting")
    page = accounts.pending_page_data(acct)
    assert page["status"] == "pending"
    assert page["registered_at"] == acct.created_at
    assert page["headline"]
    assert page["next_step"]

    denied = accounts.set_status("waiting@example.com", "denied", decided_by="a")
    page = accounts.pending_page_data(denied)
    assert page["status"] == "denied"
    assert page["decided_at"] == denied.decided_at


# ---------------------------------------------------------------------------
# HTTP API surface (admin gating + self-service)
# ---------------------------------------------------------------------------


class _User:
    def __init__(self, email: str, role: str = "user"):
        self.email = email
        self.display = email.split("@")[0]
        self.role = role


def _make_app(accounts, user):
    from fastapi import FastAPI

    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request, call_next):
        if user is not None:
            request.state.user = user
        return await call_next(request)

    app.include_router(accounts.router)
    return app


def test_admin_api_gating_and_self_service(accounts_env):
    accounts, _, _ = accounts_env
    accounts.ensure_account("newbie@example.com")

    from fastapi.testclient import TestClient

    # Unauthenticated: everything 401.
    anon = TestClient(_make_app(accounts, None))
    assert anon.get("/api/admin/accounts").status_code == 401
    assert anon.get("/api/me/account").status_code == 401

    # Non-admin: admin routes 403, self-service works.
    member = TestClient(_make_app(accounts, _User("newbie@example.com")))
    assert member.get("/api/admin/accounts").status_code == 403
    assert member.post("/api/admin/accounts/newbie@example.com/approve").status_code == 403
    me = member.get("/api/me/account")
    assert me.status_code == 200
    assert me.json()["status"] == "pending"
    assert me.json()["plan"] == "trial"

    # Admin: full queue access.
    admin = TestClient(_make_app(accounts, _User("boss@example.com", role="admin")))
    listing = admin.get("/api/admin/accounts?status=pending")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert admin.get("/api/admin/accounts/pending-count").json() == {"pending": 1}

    ok = admin.post("/api/admin/accounts/newbie@example.com/approve")
    assert ok.status_code == 200
    assert ok.json()["account"]["status"] == "active"
    assert accounts.is_authorized(accounts.get_account("newbie@example.com"))

    assert admin.post("/api/admin/accounts/ghost@example.com/approve").status_code == 404
    assert admin.post(
        "/api/admin/accounts/newbie@example.com/plan", json={"plan": "bogus"}
    ).status_code == 400
    assert admin.get("/api/admin/accounts?status=bogus").status_code == 400

    assert member.put(
        "/api/admin/accounts/newbie@example.com/security-policy",
        json={"policy": "minimized", "endpoint": None},
    ).status_code == 403
    assert admin.put(
        "/api/admin/accounts/newbie@example.com/security-policy",
        json={"policy": "minimized", "endpoint": None},
    ).status_code == 404
    accounts.set_plan("newbie@example.com", "enterprise")
    assigned = admin.put(
        "/api/admin/accounts/newbie@example.com/security-policy",
        json={"policy": "customer_endpoint", "endpoint": "https://security.example/receipt"},
    )
    assert assigned.status_code == 200
    assert admin.get("/api/admin/accounts/newbie@example.com/security-policy").json() == {
        "email": "newbie@example.com", "policy": "customer_endpoint",
        "endpoint": "https://security.example/receipt",
    }
    assert admin.put(
        "/api/admin/accounts/newbie@example.com/security-policy",
        json={"policy": "customer_endpoint", "endpoint": "http://insecure.example/receipt"},
    ).status_code == 400

    # Self-service reflects the approval.
    me = member.get("/api/me/account")
    assert me.json()["status"] == "active"
    assert me.json()["role"] == "user"  # approval never escalated the role
