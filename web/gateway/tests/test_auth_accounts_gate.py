"""AuthMiddleware must gate on the account store.

Going public means an unknown-but-authenticated identity has to become a
*pending* account rather than either (a) being refused outright, which is a
dead end for a paying stranger, or (b) being let in, which is the bug this
whole change exists to prevent.

Covers the wiring in auth.AuthMiddleware._resolve_user.
"""

from __future__ import annotations

import sys
import base64
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def env(pg, monkeypatch):
    """The real AuthMiddleware over a virgin account store, in dev auth."""
    import accounts
    import auth
    from config import settings

    # Dev auth is the identity source here (X-Dev-User); the store is what
    # decides whether that identity gets a session, which is what we test.
    monkeypatch.setattr(settings, "dev_mode", True)
    return accounts, TestClient(_bare_app(auth))


def _bare_app(auth):
    """Minimal ASGI app behind the real AuthMiddleware — no routers mounted,
    so this test exercises authorization and nothing else.

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

    inner.add_middleware(auth.AuthMiddleware)
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
    accounts.ensure_account("stranger@gmail.com")
    accounts.set_status("stranger@gmail.com", "active")
    assert client.get("/whoami", headers=hdr("stranger@gmail.com")).status_code == 200

    accounts.set_status("stranger@gmail.com", "suspended", decided_by="boss@example.com")
    assert client.get("/whoami", headers=hdr("stranger@gmail.com")).status_code == 403


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


@pytest.mark.parametrize("parser", ["nested_json", "recursion_error"])
@pytest.mark.parametrize("credential", ["header", "cookie"])
def test_access_invalid_payload_is_normalized_on_protected_and_public_paths(pg, monkeypatch, parser, credential):
    import auth
    from config import settings
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "session_secret", "fixture-session-secret")
    monkeypatch.setattr(settings, "cf_team_domain", "https://fixture.invalid")
    monkeypatch.setattr(settings, "cf_aud", "fixture-audience")
    cache = auth.JWKSCache("https://fixture.invalid/keys")
    signing_client = cache._refresh_if_needed()
    def no_network(*args, **kwargs):
        raise AssertionError("invalid payload must be refused before JWKS network fetch")
    monkeypatch.setattr(signing_client, "fetch_data", no_network)
    if parser == "recursion_error":
        def recursion(*args):
            raise RecursionError("fixture payload recursion")
        monkeypatch.setattr(cache, "get_signing_key", recursion)
    def encoded(raw):
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    # Newer JSON parsers can accept this depth without recursion. It remains an
    # unusable Access payload (missing mandatory claims), refused before fetch.
    # The separate injected parser case proves RecursionError normalization.
    payload = '{"nested":' + "[" * 1100 + "0" + "]" * 1100 + "}" if parser == "nested_json" else (
        '{"exp":4102444800,"iss":"https://fixture.invalid","aud":"fixture-audience"}'
    )
    token = encoded('{"alg":"RS256","kid":"fixture"}') + "." + encoded(payload) + ".AA"
    app = FastAPI()
    @app.get("/api/protected")
    def protected():
        return {"ok": True}
    @app.get("/healthz")
    def health():
        return {"ok": True}
    app.add_middleware(auth.AuthMiddleware, jwks=cache)
    with TestClient(app) as client:
        headers = {"cf-access-jwt-assertion": token} if credential == "header" else {}
        if credential == "cookie":
            client.cookies.set("CF_Authorization", token)
        refused = client.get("/api/protected", headers=headers)
        assert refused.status_code == 401
        assert refused.json()["error"] == "invalid access token"
        # Optional authentication must fail softly rather than break public pages.
        assert client.get("/healthz", headers=headers).status_code == 200
