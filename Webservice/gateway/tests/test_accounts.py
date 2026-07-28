"""Account store: registration, approval transitions, auto-approve, migration."""

from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))


@pytest.fixture()
def accounts_env(tmp_path, monkeypatch):
    """Fresh accounts module bound to a tmp_path data root (fresh accounts.db)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CHUDWARE_WEB_DATA_ROOT", str(data))
    monkeypatch.delenv("CHUDWARE_WEB_AUTO_APPROVE_DOMAINS", raising=False)

    for mod in ("gateway.config", "gateway.limits", "gateway.accounts",
                "config", "limits", "accounts"):
        sys.modules.pop(mod, None)

    import config  # type: ignore

    config.settings = config.load_settings()

    import limits  # type: ignore

    limits.settings = config.settings

    import accounts  # type: ignore

    accounts.settings = config.settings
    accounts._CONN = None
    accounts._CONN_PATH = None
    return accounts, config, data


def _row_count(data: Path) -> int:
    conn = sqlite3.connect(data / "accounts.db")
    try:
        return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Registration / authorization basics
# ---------------------------------------------------------------------------


def test_unknown_email_becomes_pending_unauthorized_not_admin(accounts_env):
    accounts, _, _ = accounts_env
    acct = accounts.ensure_account("Stranger@Example.com", "Stranger")

    assert acct.email == "stranger@example.com"  # lowercased
    assert acct.status == "pending"
    assert acct.role == "user"
    assert acct.plan == "free"
    assert not accounts.is_authorized(acct)
    assert accounts.effective_role(acct) == "user"
    assert accounts.effective_plan(acct) == "free"


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
# Auto-approve domains + privilege escalation guards
# ---------------------------------------------------------------------------


def test_auto_approve_domain_creates_active_user(accounts_env, monkeypatch):
    accounts, _, _ = accounts_env
    monkeypatch.setenv("CHUDWARE_WEB_AUTO_APPROVE_DOMAINS", "ua.edu, example.org")

    acct = accounts.ensure_account("student@crimson.ua.edu", "Student")
    assert acct.status == "active"          # subdomain of ua.edu matches
    assert acct.role == "user"              # NEVER admin via auto-approve
    assert accounts.is_authorized(acct)
    assert acct.decided_by == "auto-domain"

    other = accounts.ensure_account("person@other.net")
    assert other.status == "pending"
    assert not accounts.is_authorized(other)


def test_admin_role_never_self_assigned(accounts_env, monkeypatch):
    accounts, _, _ = accounts_env
    monkeypatch.setenv("CHUDWARE_WEB_AUTO_APPROVE_DOMAINS", "ua.edu")

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
# users.yaml migration
# ---------------------------------------------------------------------------

USERS_YAML = """\
default_project: ""
default_doc: ""
users:
- email: admin@example.com
  display: Boss
  role: admin
  heavy: true
- email: user@example.com
  display: Regular
  role: user
  heavy: false
"""


def test_users_yaml_import_preserves_admin_and_is_idempotent(accounts_env, tmp_path):
    accounts, _, _ = accounts_env
    yaml_path = tmp_path / "users.yaml"
    yaml_path.write_text(USERS_YAML)

    result = accounts.import_users_yaml(yaml_path)
    assert sorted(result["imported"]) == ["admin@example.com", "user@example.com"]
    assert result["skipped"] == []

    admin = accounts.get_account("admin@example.com")
    assert admin.status == "active"
    assert admin.role == "admin"
    assert admin.heavy is True
    assert accounts.is_authorized(admin)
    assert accounts.effective_role(admin) == "admin"

    user = accounts.get_account("user@example.com")
    assert user.status == "active"
    assert user.role == "user"
    assert user.heavy is False

    # Second run: no duplication, no clobbering of decisions made since.
    accounts.set_status("user@example.com", "suspended", decided_by="admin@example.com")
    accounts.set_plan("admin@example.com", "team")
    again = accounts.import_users_yaml(yaml_path)
    assert again["imported"] == []
    assert sorted(again["skipped"]) == ["admin@example.com", "user@example.com"]
    assert accounts.get_account("user@example.com").status == "suspended"
    assert accounts.get_account("admin@example.com").plan == "team"


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
    assert accounts.set_plan("ghost@example.com", "pro") is None
    with pytest.raises(ValueError):
        accounts.set_plan("pay@example.com", "enterprise")


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
    assert me.json()["plan"] == "free"

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

    # Self-service reflects the approval.
    me = member.get("/api/me/account")
    assert me.json()["status"] == "active"
    assert me.json()["role"] == "user"  # approval never escalated the role
