"""AuthMiddleware must gate on the account store, not on a bare allowlist.

Going public means an unknown-but-authenticated identity has to become a
*pending* account rather than either (a) being refused outright, which is a
dead end for a paying stranger, or (b) being let in, which is the bug this
whole change exists to prevent.

Covers the wiring in auth.AuthMiddleware._resolve_user (2026-07-28 rollout).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A gateway wired to a throwaway data root, with a 2-entry users.yaml."""
    monkeypatch.setenv("CHUDWARE_WEB_DEV", "1")
    monkeypatch.setenv("CHUDWARE_WEB_DATA_ROOT", str(tmp_path / "data"))
    # Auto-approval OFF: every new identity must go through the queue.
    monkeypatch.setenv("CHUDWARE_WEB_AUTO_APPROVE_DOMAINS", "")

    users = tmp_path / "users.yaml"
    users.write_text(
        "users:\n"
        "  - email: boss@example.com\n"
        "    display: Boss\n"
        "    role: admin\n"
        "    heavy: true\n"
        "  - email: teammate@example.com\n"
        "    display: Teammate\n"
        "    role: user\n"
        "    heavy: false\n"
    )
    monkeypatch.setenv("CHUDWARE_WEB_USERS_FILE", str(users))

    import config
    importlib.reload(config)
    import accounts
    importlib.reload(accounts)
    import auth
    importlib.reload(auth)

    directory = auth.UserDirectory(config.settings.users_file)
    middleware_app = _bare_app(auth, directory)
    return accounts, TestClient(middleware_app)


def _bare_app(auth, directory):
    """Minimal ASGI app behind the real AuthMiddleware — no session manager,
    no podman, so this test exercises authorization and nothing else.

    NOTE: `Request` must be imported at module scope. This file uses
    `from __future__ import annotations`, so FastAPI resolves the annotation
    against this module's globals — a function-local import would leave it
    unresolvable and every request would 422 as an unknown query parameter.
    """
    inner = FastAPI()

    @inner.get("/whoami")
    async def whoami(request: Request):
        user = auth.get_current_user(request)
        return {"email": user.email, "role": user.role, "heavy": user.heavy}

    inner.add_middleware(auth.AuthMiddleware, directory=directory)
    return inner


def hdr(email: str) -> dict[str, str]:
    return {"X-Dev-User": email}


def test_unknown_identity_becomes_pending_and_is_refused(env):
    accounts, client = env
    r = client.get("/whoami", headers=hdr("stranger@gmail.com"))
    assert r.status_code == 403

    # Refused, but REGISTERED — that is the whole point of the queue.
    acct = accounts.get_account("stranger@gmail.com")
    assert acct is not None
    assert acct.status == "pending"
    assert acct.role == "user"  # never admin on self-registration


def test_approving_a_pending_account_grants_access(env):
    accounts, client = env
    assert client.get("/whoami", headers=hdr("stranger@gmail.com")).status_code == 403

    accounts.set_status("stranger@gmail.com", "active", decided_by="boss@example.com")

    r = client.get("/whoami", headers=hdr("stranger@gmail.com"))
    assert r.status_code == 200
    assert r.json() == {"email": "stranger@gmail.com", "role": "user", "heavy": False}


def test_suspending_revokes_access_immediately(env):
    accounts, client = env
    accounts.set_status("stranger@gmail.com", "active") or accounts.ensure_account("stranger@gmail.com")
    accounts.set_status("stranger@gmail.com", "active")
    assert client.get("/whoami", headers=hdr("stranger@gmail.com")).status_code == 200

    accounts.set_status("stranger@gmail.com", "suspended", decided_by="boss@example.com")
    assert client.get("/whoami", headers=hdr("stranger@gmail.com")).status_code == 403


def test_legacy_allowlist_user_is_adopted_with_their_role(env):
    """An existing teammate must not be stranded in the queue by this upgrade."""
    accounts, client = env

    r = client.get("/whoami", headers=hdr("boss@example.com"))
    assert r.status_code == 200
    assert r.json()["role"] == "admin"
    assert r.json()["heavy"] is True

    acct = accounts.get_account("boss@example.com")
    assert acct.status == "active" and acct.role == "admin"

    r = client.get("/whoami", headers=hdr("teammate@example.com"))
    assert r.status_code == 200
    assert r.json()["role"] == "user"


def test_admin_denial_outranks_the_yaml_file(env):
    """Adoption must never resurrect an account an admin already shut off."""
    accounts, client = env
    assert client.get("/whoami", headers=hdr("teammate@example.com")).status_code == 200

    accounts.set_status("teammate@example.com", "denied", decided_by="boss@example.com")

    # Still on the allowlist, but the admin decision wins.
    assert client.get("/whoami", headers=hdr("teammate@example.com")).status_code == 403
    assert accounts.get_account("teammate@example.com").status == "denied"


def test_auto_approve_domain_activates_without_an_admin(env, monkeypatch):
    accounts, client = env
    monkeypatch.setenv("CHUDWARE_WEB_AUTO_APPROVE_DOMAINS", "ua.edu")

    r = client.get("/whoami", headers=hdr("student@crimson.ua.edu"))
    assert r.status_code == 200
    assert r.json()["role"] == "user"  # domain users are NEVER admin

    # A domain that is not on the list still queues.
    assert client.get("/whoami", headers=hdr("nope@elsewhere.edu")).status_code == 403


def test_role_cannot_be_escalated_by_the_caller(env):
    """Nothing the client sends may influence the role — it comes from the DB."""
    accounts, client = env
    accounts.ensure_account("climber@gmail.com")
    accounts.set_status("climber@gmail.com", "active")

    r = client.get(
        "/whoami",
        headers={"X-Dev-User": "climber@gmail.com", "X-Chudware-Role": "admin"},
    )
    assert r.status_code == 200
    assert r.json()["role"] == "user"
