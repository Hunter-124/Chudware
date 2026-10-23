"""The account store, not Cloudflare Access, is the door to ``/admin``.

``AuthMiddleware._authenticate`` used to derive a ``cf_present`` flag from the
mere PRESENCE of a ``Cf-Access-Jwt-Assertion`` header or a ``CF_Authorization``
cookie and, when set, skip the ``chudware_sid`` branch entirely. Every browser
that had ever completed Cloudflare Access for the zone therefore had its live
gateway session ignored on every request, and was hard-401'd with "invalid
access token" the moment that edge cookie expired or stopped matching ``cf_aud``
— while holding a perfectly good session. ``revalidate_websocket_user`` never
had that flag, so the HTTP and WebSocket paths disagreed about who you were.

Nothing pinned the precedence, which is why it could be written that way at
all. These tests pin it: the session cookie decides first, Access is only a
fallback for a caller that has no session, and the fallback is not a bypass.

Cloudflare Access is CONFIGURED for the ``client`` fixture (``cf_team_domain`` +
``cf_aud``) so that every "an assertion rides along" case exercises the real
branch ordering rather than passing because the edge path was unreachable.
``self_serve_client`` is the opposite deployment — no Access at all, which is
what production became once the Access application was deleted — and pins that
a leftover edge cookie is answered as "not signed in" rather than as a fault.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

# Gateway modules are imported inside the fixture, NOT here. `test_accounts.py`
# sorts first and its fixture pops `config` from sys.modules and re-imports it, so
# anything that captured `settings` at collection time — `auth` binds it at import —
# is left pointing at the discarded module's object. Later modules then monkeypatch
# the live `settings` while `auth` reads the stale one, and `AuthMiddleware.__init__`
# hits its no-strong-auth fail-fast: importing `auth` up here broke four
# test_auth_accounts_gate cases that pass in every other order.
#
# `Request` is the one exception and MUST stay at module scope: this file uses
# `from __future__ import annotations`, so FastAPI resolves that annotation against
# module globals and a function-local import would 422 every request.

# PyJWT rejects this while decoding the token header, before any JWKS lookup, so
# no test in this file can reach out to Cloudflare. It stands in for the real
# failure mode: an assertion that is present but no longer verifiable.
STALE_CF_TOKEN = "not-a-cloudflare-access-jwt"


@pytest.fixture()
def client(pg):
    """Production auth with BOTH credentials configured — session secret and
    Cloudflare Access — because the bug was about which of the two wins.

    The middleware is handed an explicit ``cfg`` copy rather than a monkeypatched
    global. ``auth`` binds ``settings`` at import time and several fixtures in this
    suite swap ``config.settings`` for a fresh object, so patching the singleton
    made this file's result depend on which module imported ``auth`` first — it
    passed alone and failed in the full run, in both directions. ``cfg`` is the
    seam ``AuthMiddleware`` already exposes for exactly this.
    """
    import copy  # noqa: PLC0415

    import auth  # noqa: PLC0415
    from config import settings  # noqa: PLC0415

    cfg = copy.copy(settings)
    cfg.dev_mode = False
    cfg.session_secret = "test-session-secret"
    cfg.cf_team_domain = "https://chudware.cloudflareaccess.com"
    cfg.cf_aud = "a" * 64

    inner = FastAPI()

    @inner.get("/admin")
    async def admin(request: Request):
        user = auth.get_current_user(request)
        return {"email": user.email, "role": user.role}

    inner.add_middleware(auth.AuthMiddleware, cfg=cfg)
    with TestClient(inner, base_url="https://testserver") as c:
        yield c


@pytest.fixture()
def self_serve_client(pg):
    """A self-serve deployment: session secret only, no Cloudflare Access.

    This is production. The Access application in front of the gateway was
    deleted, so ``cf_team_domain``/``cf_aud`` describe an edge that no longer
    exists — while browsers that once completed Access keep presenting the
    ``CF_Authorization`` cookie it minted.
    """
    import copy  # noqa: PLC0415

    import auth  # noqa: PLC0415
    from config import settings  # noqa: PLC0415

    cfg = copy.copy(settings)
    cfg.dev_mode = False
    cfg.session_secret = "test-session-secret"
    cfg.cf_team_domain = ""
    cfg.cf_aud = ""

    inner = FastAPI()

    @inner.get("/admin")
    async def admin(request: Request):
        user = auth.get_current_user(request)
        return {"email": user.email, "role": user.role}

    inner.add_middleware(auth.AuthMiddleware, cfg=cfg)
    with TestClient(inner, base_url="https://testserver") as c:
        yield c



def _session_for(email: str, *, role: str = "user") -> str:
    """An active account at ``role`` plus a real session row for it.

    The session comes from ``authn.create_session`` rather than a forged cookie:
    a hand-signed value would still pass a signature check after the account
    store had ended the session, which is half of what these tests measure.
    """
    import accounts  # noqa: PLC0415
    import authn  # noqa: PLC0415

    accounts.ensure_account(email)
    accounts.set_status(email, "active", decided_by="ops@chudware.com")
    if role != "user":
        accounts.set_role(email, role)
    return authn.create_session(email)


def _session_cookie() -> str:
    import authn  # noqa: PLC0415

    return authn.SESSION_COOKIE


def test_admin_session_wins_over_a_stale_access_cookie(client):
    """The reported bug, verbatim: the operator's own session was ignored."""
    sid = _session_for("haedmonds69@gmail.com", role="admin")

    r = client.get(
        "/admin",
        cookies={_session_cookie(): sid, "CF_Authorization": STALE_CF_TOKEN},
    )

    assert r.status_code == 200
    assert r.json() == {"email": "haedmonds69@gmail.com", "role": "admin"}


def test_admin_session_wins_over_a_stale_access_header(client):
    """Same precedence for the header form — Cloudflare sends both."""
    sid = _session_for("haedmonds69@gmail.com", role="admin")

    r = client.get(
        "/admin",
        cookies={_session_cookie(): sid},
        headers={"Cf-Access-Jwt-Assertion": STALE_CF_TOKEN},
    )

    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_role_comes_from_the_store_and_not_from_the_caller(client):
    """A plain user stays a plain user however it decorates the request."""
    sid = _session_for("member@example.com")

    r = client.get(
        "/admin",
        cookies={
            _session_cookie(): sid,
            "CF_Authorization": STALE_CF_TOKEN,
            "dev_user": "root@example.com",
        },
        headers={
            "Cf-Access-Jwt-Assertion": STALE_CF_TOKEN,
            "X-Dev-User": "root@example.com",
            "Cf-Access-Authenticated-User-Email": "root@example.com",
        },
    )

    assert r.status_code == 200
    assert r.json() == {"email": "member@example.com", "role": "user"}


def test_access_fallback_is_not_a_bypass(client):
    """No session + an unverifiable assertion is still nobody.

    The message matters: it proves the Access branch really ran and rejected
    the token, rather than the request being refused earlier for want of any
    credential at all.
    """
    r = client.get("/admin", cookies={"CF_Authorization": STALE_CF_TOKEN})

    assert r.status_code == 401
    assert r.json()["error"] == "invalid access token"


def test_anonymous_admin_navigation_lands_on_the_gateway_signin(client):
    """What replaced the Cloudflare Access login page at the edge. Also the
    exact assertion deploy/verify_public.sh makes against production."""
    r = client.get("/admin", headers={"Accept": "text/html"}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/signin?next=%2Fadmin"


def test_suspending_an_admin_ends_its_session_even_with_an_assertion(client):
    """The account-store decision has to END the session, not merely be
    re-checked beside it — and an Access assertion must not readmit the caller
    now that the session branch is always reached first."""
    import accounts  # noqa: PLC0415
    import authn  # noqa: PLC0415

    sid = _session_for("expelled@example.com", role="admin")
    assert client.get("/admin", cookies={_session_cookie(): sid}).status_code == 200

    accounts.set_status("expelled@example.com", "suspended", decided_by="ops@chudware.com")

    r = client.get(
        "/admin",
        cookies={_session_cookie(): sid, "CF_Authorization": STALE_CF_TOKEN},
    )

    assert r.status_code == 401
    assert authn.lookup_session(sid) is None


def test_stale_access_cookie_is_not_a_fault_without_access(self_serve_client):
    """A cookie the caller cannot stop sending must never be a 5xx.

    ``CF_Authorization`` outlives the Access application that minted it. The
    token branch used to be entered before the "is Access even configured"
    check, so this exact request answered 500 — a stale client-side cookie
    turning every page of a self-serve deployment into a server error.
    """
    r = self_serve_client.get("/admin", cookies={"CF_Authorization": STALE_CF_TOKEN})

    assert r.status_code == 401
    assert r.json()["error"] == "not signed in"


def test_stale_access_header_is_not_a_fault_without_access(self_serve_client):
    """Same for the header form: nothing to verify it against is not a fault."""
    r = self_serve_client.get(
        "/admin", headers={"Cf-Access-Jwt-Assertion": STALE_CF_TOKEN}
    )

    assert r.status_code == 401
    assert r.json()["error"] == "not signed in"


def test_a_live_session_still_wins_without_access(self_serve_client):
    """The front door is unaffected by removing the edge."""
    sid = _session_for("haedmonds69@gmail.com", role="admin")

    r = self_serve_client.get(
        "/admin",
        cookies={_session_cookie(): sid, "CF_Authorization": STALE_CF_TOKEN},
    )

    assert r.status_code == 200
    assert r.json() == {"email": "haedmonds69@gmail.com", "role": "admin"}
