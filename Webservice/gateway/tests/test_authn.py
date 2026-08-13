"""Self-serve authentication: the security properties, not the happy path.

Every test here defends a property that, if it broke, would be exploitable
rather than merely annoying:

* the login endpoint is not an account-existence oracle,
* brute force is capped per-account and per-address,
* suspending an account ends its live sessions on the next request,
* verification tokens are single-use and never stored in plaintext,
* the session cookie carries HttpOnly / Secure / SameSite=Lax / Path=/,
* and the gateway refuses to boot with no strong auth configured while
  accepting ``session_secret`` alone as sufficient.

Everything runs in NON-dev mode against a real PostgreSQL (the ``pg`` fixture),
because the session branch of ``AuthMiddleware._authenticate`` is unreachable
in dev mode and the whole point is to exercise the real one.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import accounts  # noqa: E402
import auth  # noqa: E402
import authn  # noqa: E402
import db  # noqa: E402
import limits  # noqa: E402
import mailer  # noqa: E402
import oauth  # noqa: E402
from config import settings  # noqa: E402

# A routable-looking client address so the per-IP limiter has a stable key that
# is not shared with any other test module.
CLIENT = ("203.0.113.9", 44_321)

_DELIVERED_MESSAGES: list[dict] = []

GOOD_PASSWORD = "correct horse battery staple"
WRONG_PASSWORD = "incorrect horse battery staple"


def _protected_app() -> FastAPI:
    """The real auth stack over two protected routes.

    Two, because ``_deny_http`` deliberately answers differently either side of
    the ``/api`` boundary: a browser navigation gets an HTML status page, an
    XHR gets JSON. No other routers are mounted, so a failure here is
    authentication and never a downstream handler.

    ``Request`` must be imported at module scope: this file uses
    ``from __future__ import annotations`` and FastAPI resolves the annotation
    against module globals.
    """
    inner = FastAPI()
    inner.include_router(authn.router)
    inner.include_router(oauth.router)

    @inner.get("/api/whoami")
    async def whoami(request: Request):
        user = auth.get_current_user(request)
        return {"email": user.email, "role": user.role, "status": "ok"}

    @inner.get("/portal")
    async def portal(request: Request):
        auth.get_current_user(request)
        return {"page": "portal"}

    inner.add_middleware(auth.AuthMiddleware)
    return inner


@pytest.fixture()
def client(pg, monkeypatch):
    """A gateway in production auth mode, with virgin rate limiters."""
    limits.login_by_email._hits.clear()
    limits.login_by_ip._hits.clear()
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://chudware.test")
    app = _protected_app()
    with TestClient(app, client=CLIENT, base_url="https://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _outbox() -> list[dict]:
    with db.tx() as cur:
        cur.execute(
            "SELECT id, to_addr, template, ctx_json, sent_at FROM mail_outbox"
            " ORDER BY id"
        )
        return list(cur.fetchall())


def _drain() -> int:
    """Run one real drain pass. Delivery is what the drain does, not what
    ``send()`` does — asserting on the queued row alone would pass even with
    the drain task never wired into app startup."""
    return asyncio.run(mailer.drain_once())


def _delivered(template: str) -> dict:
    """The single delivered message of ``template``, with ctx decoded."""
    rows = [r for r in _outbox() if r["template"] == template]
    assert len(rows) == 1, f"expected exactly one {template} mail, got {len(rows)}"
    row = rows[0]
    assert row["sent_at"] is not None, f"{template} was queued but never delivered"
    return {**row, "ctx": json.loads(row["ctx_json"])}


def _token_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["token"][0]


def register(client: TestClient, email: str, password: str = GOOD_PASSWORD):
    return client.post(
        "/api/auth/register", json={"email": email, "password": password}
    )


def register_and_verify(client: TestClient, email: str, password: str = GOOD_PASSWORD):
    """Walk the whole customer path: register, drain the mail, click the link."""
    assert register(client, email, password).status_code == 202
    assert _drain() == 1
    verify_url = _delivered("verify_email")["ctx"]["verify_url"]
    response = client.get(
        f"/api/auth/verify?token={_token_from(verify_url)}", follow_redirects=False
    )
    assert response.status_code == 303
    return response


def login(client: TestClient, email: str, password: str):
    return client.post("/api/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# Registration -> verification -> trial
# ---------------------------------------------------------------------------


def test_registration_pends_then_verification_activates_a_trial(client):
    assert register(client, "new@example.com").status_code == 202

    account = accounts.get_account("new@example.com")
    assert account.status == "pending"
    assert account.plan == "trial"
    credential = authn.get_credential("new@example.com")
    assert credential is not None and credential["email_verified"] is False

    # Nothing is delivered by send(); the drain delivers.
    queued = _outbox()
    assert len(queued) == 1 and queued[0]["sent_at"] is None
    assert _drain() == 1

    mail = _delivered("verify_email")
    assert mail["to_addr"] == "new@example.com"
    # The link really is in the message a customer receives, not merely in ctx.
    rendered = mailer.render("verify_email", mail["ctx"])
    assert mail["ctx"]["verify_url"] in rendered.text

    client.get(
        f"/api/auth/verify?token={_token_from(mail['ctx']['verify_url'])}",
        follow_redirects=False,
    )

    account = accounts.get_account("new@example.com")
    assert account.status == "active"
    assert account.plan == "trial"
    ends = authn.trial_ends_at("new@example.com")
    expected = time.time() + settings.trial_days * 86_400
    assert abs(ends - expected) < 300
    assert authn.get_credential("new@example.com")["email_verified"] is True


def test_verified_user_reaches_a_protected_route(client):
    register_and_verify(client, "member@example.com")
    body = client.get("/api/whoami").json()
    assert body == {"email": "member@example.com", "role": "user", "status": "ok"}


def test_websocket_revalidation_tracks_actual_session_and_revocation(client):
    from auth import AuthError, revalidate_websocket_user
    register_and_verify(client, "member@example.com")
    sid = authn.create_session("member@example.com")
    assert sid
    scope = {
        "state": {"user": auth.User("member@example.com", "Member", "user", False)},
        "headers": [(b"cookie", f"{authn.SESSION_COOKIE}={sid}".encode())],
    }
    assert revalidate_websocket_user(scope).email == "member@example.com"
    authn.revoke_session(sid)
    with pytest.raises(AuthError) as exc:
        revalidate_websocket_user(scope)
    assert exc.value.status == 401


def test_unverified_account_cannot_log_in_even_with_the_right_password(client):
    register(client, "unverified@example.com")
    response = login(client, "unverified@example.com", GOOD_PASSWORD)
    assert response.status_code == 403
    assert authn.SESSION_COOKIE not in client.cookies


def test_self_registration_can_never_be_admin(client):
    register_and_verify(client, "sneaky@example.com")
    assert accounts.get_account("sneaky@example.com").role == "user"
    assert client.get("/api/whoami").json()["role"] == "user"


# ---------------------------------------------------------------------------
# No account-existence oracle
# ---------------------------------------------------------------------------


def test_unknown_email_and_wrong_password_are_byte_identical(client):
    register_and_verify(client, "real@example.com")
    client.post("/api/auth/logout")

    wrong = login(client, "real@example.com", WRONG_PASSWORD)
    unknown = login(client, "ghost@example.com", WRONG_PASSWORD)

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.content == unknown.content, (
        f"login is an enumeration oracle: {wrong.content!r} != {unknown.content!r}"
    )
    # Neither answer may leak through a header either.
    assert "set-cookie" not in wrong.headers
    assert "set-cookie" not in unknown.headers


def test_unknown_email_path_still_spends_an_argon2_verification(client):
    """The timing half of the same property.

    An unknown email must not return before a real hash comparison would have.
    Wall-clock timing is too flaky to assert directly in CI, so this asserts
    the mechanism: burn_dummy_verification runs the configured hasher, and the
    login handler calls it. A refactor that drops the call fails here.
    """
    calls: list[str] = []
    original = authn.burn_dummy_verification

    def spy(password: str) -> None:
        calls.append(password)
        original(password)

    authn.burn_dummy_verification = spy  # noqa: B010 - restored below
    try:
        assert login(client, "nobody@example.com", WRONG_PASSWORD).status_code == 401
    finally:
        authn.burn_dummy_verification = original
    assert calls == [WRONG_PASSWORD]


def test_registering_a_taken_address_returns_the_same_202_not_a_409(client):
    fresh = register(client, "first@example.com")
    _drain()
    repeat = register(client, "first@example.com")

    assert repeat.status_code == fresh.status_code == 202
    assert repeat.content == fresh.content

    assert _drain() == 1
    warning = _delivered("register_exists")
    assert warning["to_addr"] == "first@example.com"
    # And the second attempt did NOT overwrite the first password.
    assert len([r for r in _outbox() if r["template"] == "verify_email"]) == 1


# ---------------------------------------------------------------------------
# Brute force
# ---------------------------------------------------------------------------


def test_sixth_wrong_password_for_one_email_is_throttled(client):
    register_and_verify(client, "target@example.com")
    client.post("/api/auth/logout")

    per_email = limits.login_by_email.limit
    for attempt in range(per_email):
        response = login(client, "target@example.com", WRONG_PASSWORD)
        assert response.status_code == 401, f"attempt {attempt + 1} was not a 401"

    throttled = login(client, "target@example.com", WRONG_PASSWORD)
    assert throttled.status_code == 429
    assert int(throttled.headers["Retry-After"]) >= 1

    # Even the CORRECT password is refused while the account is throttled —
    # otherwise the limiter is a speed bump, not a lock.
    assert login(client, "target@example.com", GOOD_PASSWORD).status_code == 429


def test_a_different_email_from_the_same_ip_is_still_allowed(client):
    per_email = limits.login_by_email.limit
    per_ip = limits.login_by_ip.limit

    for _ in range(per_email + 1):
        login(client, "target@example.com", WRONG_PASSWORD)
    assert login(client, "target@example.com", WRONG_PASSWORD).status_code == 429

    # The per-email lockout must not have locked the address out of the
    # service; a shared NAT or a university must keep working.
    spent = per_email + 2
    for n in range(spent, per_ip):
        response = login(client, f"other{n}@example.com", WRONG_PASSWORD)
        assert response.status_code == 401, (
            f"request {n + 1} of the {per_ip}-request IP budget was refused"
        )

    exhausted = login(client, "last@example.com", WRONG_PASSWORD)
    assert exhausted.status_code == 429
    assert int(exhausted.headers["Retry-After"]) >= 1


def test_login_limiters_are_enforced_before_any_hashing(client):
    """A throttled caller must not be able to spend the server's argon2 budget.

    64 MiB per hash makes an unthrottled login endpoint a memory-exhaustion
    amplifier, so the 429 has to come out before verify_password is reached.
    """
    register_and_verify(client, "costly@example.com")
    client.post("/api/auth/logout")

    for _ in range(limits.login_by_email.limit):
        login(client, "costly@example.com", WRONG_PASSWORD)

    calls: list[str] = []
    original = authn.verify_password
    authn.verify_password = lambda h, p: calls.append(p) or original(h, p)  # type: ignore[assignment]
    try:
        assert login(client, "costly@example.com", GOOD_PASSWORD).status_code == 429
    finally:
        authn.verify_password = original
    assert calls == [], "argon2 ran for a request that was already over budget"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_login_sets_a_hardened_session_cookie(client):
    register_and_verify(client, "cookie@example.com")
    client.post("/api/auth/logout")

    response = login(client, "cookie@example.com", GOOD_PASSWORD)
    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]

    assert set_cookie.startswith(f"{authn.SESSION_COOKIE}=")
    assert "; Path=/" in set_cookie
    assert "; HttpOnly" in set_cookie
    # Lax, not Strict: Strict drops the cookie on the OAuth callback's
    # cross-site top-level navigation and sign-in silently fails.
    assert "; SameSite=Lax" in set_cookie
    assert "; SameSite=Strict" not in set_cookie
    # testserver is plain http but is NOT localhost, so Secure must be set.
    assert "; Secure" in set_cookie

    sid = set_cookie.split(";", 1)[0].split("=", 1)[1]
    assert len(sid) >= 40  # secrets.token_urlsafe(32)
    assert authn.lookup_session(sid) == "cookie@example.com"


def test_session_bearer_is_never_stored_in_plaintext(client):
    register_and_verify(client, "digest@example.com")
    response = login(client, "digest@example.com", GOOD_PASSWORD)
    sid = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    with db.tx() as cur:
        cur.execute(
            "SELECT sid FROM sessions WHERE email = %s AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
            ("digest@example.com",),
        )
        stored = cur.fetchone()["sid"]
    assert stored == authn.token_hash(sid)
    assert sid != stored
    assert authn.lookup_session(sid) == "digest@example.com"


def test_suspending_an_account_kills_its_live_session_on_the_next_request(client):
    register_and_verify(client, "doomed@example.com")
    assert client.get("/api/whoami").status_code == 200
    sid = client.cookies[authn.SESSION_COOKIE]

    accounts.set_status("doomed@example.com", "suspended", decided_by="admin@example.com")

    # THE point of server-side sessions. No logout, no cookie expiry, no
    # waiting for a token to age out — the very next request is refused.
    denied = client.get("/api/whoami")
    assert denied.status_code == 403
    assert denied.json()["account_status"] == "suspended"

    # And the row itself is revoked, so reinstating the account later does not
    # silently resurrect a session an operator believed they had ended.
    with db.tx() as cur:
        cur.execute("SELECT revoked_at FROM sessions WHERE sid = %s", (authn.token_hash(sid),))
        assert cur.fetchone()["revoked_at"] is not None
    assert authn.lookup_session(sid) is None
    # The cookie is now inert: subsequent requests are plain unauthenticated,
    # and reinstating the account later cannot resurrect this session.
    assert client.get("/api/whoami").status_code == 401
    accounts.set_status("doomed@example.com", "active", decided_by="admin@example.com")
    assert client.get("/api/whoami").status_code == 401
    assert authn.lookup_session(sid) is None


def test_logout_revokes_only_the_presented_session(client):
    register_and_verify(client, "two@example.com")
    other = authn.create_session("two@example.com")
    mine = client.cookies[authn.SESSION_COOKIE]

    assert client.post("/api/auth/logout").status_code == 200
    assert authn.lookup_session(mine) is None
    assert authn.lookup_session(other) == "two@example.com"
    assert client.get("/api/whoami").status_code == 401


def test_an_expired_session_does_not_authenticate(client):
    register_and_verify(client, "stale@example.com")
    sid = client.cookies[authn.SESSION_COOKIE]
    with db.tx() as cur:
        cur.execute(
            "UPDATE sessions SET expires_at = now() - interval '1 second'"
            " WHERE sid = %s",
            (authn.token_hash(sid),),
        )
    assert authn.lookup_session(sid) is None
    assert client.get("/api/whoami").status_code == 401


def test_an_expired_trial_stops_being_authorized(client):
    register_and_verify(client, "lapsed@example.com")
    assert client.get("/api/whoami").status_code == 200
    with db.tx() as cur:
        cur.execute(
            "UPDATE accounts SET trial_ends_at = %s WHERE email = %s",
            (time.time() - 86_400, "lapsed@example.com"),
        )
    denied = client.get("/api/whoami")
    assert denied.status_code == 403
    assert denied.json()["account_status"] == "trial_expired"
    # A lapsed trial is not a decision against the user, so the session lives.
    assert authn.lookup_session(client.cookies[authn.SESSION_COOKIE]) is not None

    # A browser navigation gets the page, not the JSON — and the page names the
    # actual problem instead of a generic refusal.
    page = client.get("/portal")
    assert page.status_code == 403
    assert "trial has ended" in page.text
    assert "Pick a plan" in page.text


# ---------------------------------------------------------------------------
# Email tokens
# ---------------------------------------------------------------------------


def test_a_verify_token_is_single_use_and_never_stored_in_plaintext(client):
    register(client, "token@example.com")
    _drain()
    token = _token_from(_delivered("verify_email")["ctx"]["verify_url"])

    with db.tx() as cur:
        cur.execute("SELECT token_hash, email, purpose, used_at FROM email_tokens")
        rows = list(cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["token_hash"] == authn.token_hash(token)
    assert rows[0]["purpose"] == authn.PURPOSE_VERIFY
    # The plaintext must appear NOWHERE in the row, not merely not in its own
    # column — a database dump is the threat this defends against.
    assert token not in json.dumps(rows[0], default=str)

    first = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert first.status_code == 303

    second = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert second.status_code == 400
    assert "no longer valid" in second.text


def test_a_verify_token_is_not_redeemable_as_a_password_reset(client):
    register(client, "crossuse@example.com")
    _drain()
    token = _token_from(_delivered("verify_email")["ctx"]["verify_url"])

    response = client.post(
        "/api/auth/reset", json={"token": token, "password": "another good passphrase"}
    )
    assert response.status_code == 400
    assert authn.get_credential("crossuse@example.com")["email_verified"] is False


def test_password_reset_verifies_the_address_and_evicts_other_sessions(client):
    register_and_verify(client, "reset@example.com")
    stolen = authn.create_session("reset@example.com")

    assert client.post(
        "/api/auth/forgot", json={"email": "reset@example.com"}
    ).status_code == 202
    assert _drain() == 1  # the verification mail already went out above
    reset_url = _delivered("password_reset")["ctx"]["reset_url"]

    new_password = "a different good passphrase"
    response = client.post(
        "/api/auth/reset",
        json={"token": _token_from(reset_url), "password": new_password},
    )
    assert response.status_code == 200

    # A password change has to evict whoever prompted it.
    assert authn.lookup_session(stolen) is None
    client.post("/api/auth/logout")
    assert login(client, "reset@example.com", GOOD_PASSWORD).status_code == 401
    assert login(client, "reset@example.com", new_password).status_code == 200


def test_forgot_on_an_unknown_address_is_silent_and_still_202(client):
    assert client.post(
        "/api/auth/forgot", json={"email": "nobody@example.com"}
    ).status_code == 202
    assert _outbox() == []


def test_forgot_on_an_oauth_only_address_offers_no_reset_link(client):
    """An identity with no password must not be convertible into one without
    re-proving ownership — so it gets told which button to press instead."""
    accounts.ensure_account("social@example.com", display="social")
    authn.link_oauth_identity("google", "sub-123", "social@example.com")

    assert client.post(
        "/api/auth/forgot", json={"email": "social@example.com"}
    ).status_code == 202
    assert _drain() == 1
    mail = _delivered("oauth_only_reset")
    assert mail["ctx"]["provider"] == "google"
    assert [r["template"] for r in _outbox()] == ["oauth_only_reset"]


def test_resend_verification_invalidates_the_previous_link(client):
    register(client, "resend@example.com")
    _drain()
    first = _token_from(_delivered("verify_email")["ctx"]["verify_url"])

    assert client.post(
        "/api/auth/resend-verification", json={"email": "resend@example.com"}
    ).status_code == 202
    assert _drain() == 1

    urls = [
        json.loads(r["ctx_json"])["verify_url"]
        for r in _outbox()
        if r["template"] == "verify_email"
    ]
    assert len(urls) == 2
    second = _token_from(urls[1])
    assert second != first

    # The superseded link must be dead, not merely one of two live links.
    assert client.get(
        f"/api/auth/verify?token={first}", follow_redirects=False
    ).status_code == 400
    assert client.get(
        f"/api/auth/verify?token={second}", follow_redirects=False
    ).status_code == 303


# ---------------------------------------------------------------------------
# Password storage
# ---------------------------------------------------------------------------


def test_passwords_are_stored_as_argon2id_with_the_configured_cost(client):
    register(client, "hashed@example.com")
    stored = authn.get_credential("hashed@example.com")["password_hash"]

    assert stored.startswith("$argon2id$")
    assert "m=65536" in stored and "t=3" in stored and "p=4" in stored
    assert GOOD_PASSWORD not in stored
    assert authn.verify_password(stored, GOOD_PASSWORD) is True
    assert authn.verify_password(stored, WRONG_PASSWORD) is False
    # Salted: the same password twice must not produce the same hash.
    assert authn.hash_password(GOOD_PASSWORD) != stored


def test_a_short_password_is_refused_at_registration(client):
    response = register(client, "weak@example.com", "short")
    assert response.status_code == 400
    assert accounts.get_account("weak@example.com") is None


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def test_oauth_state_is_single_use_and_bound_to_its_provider_and_browser(client):
    state, verifier = oauth.mint_state("google")
    assert oauth.check_state("github", state, verifier) is False, "state crossed providers"
    assert oauth.check_state("google", state, "") is False, "state worked without its browser"
    assert oauth.check_state("google", state, "stolen-browser") is False
    assert oauth.check_state("google", state, verifier) is True
    assert oauth.check_state("google", state, verifier) is False, "state was replayable"


def test_oauth_state_with_a_forged_signature_is_rejected(client):
    state, verifier = oauth.mint_state("google")
    nonce, _, _signature = state.partition(".")
    assert oauth.check_state("google", f"{nonce}.{'0' * 64}", verifier) is False
    assert oauth.check_state("google", nonce, verifier) is False
    assert oauth.check_state("google", "", verifier) is False


def test_an_expired_oauth_state_is_rejected(client):
    state, verifier = oauth.mint_state("google")
    with db.tx() as cur:
        cur.execute(
            "UPDATE email_tokens SET expires_at = now() - interval '1 second'"
            " WHERE token_hash = %s",
            (authn.token_hash(state),),
        )
    assert oauth.check_state("google", state, verifier) is False

def test_stolen_oauth_state_without_initiating_cookie_is_rejected_and_cleared(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    state, _verifier = oauth.mint_state("google")

    response = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "expired or was already used" in response.text
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{oauth.OAUTH_TRANSACTION_COOKIE}=;")
    assert "Max-Age=0" in cookie


def test_matching_oauth_cookie_succeeds_once_and_is_cleared(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(oauth, "resolve_identity", _identity)
    state, verifier = oauth.mint_state("google")
    client.cookies.set(oauth.OAUTH_TRANSACTION_COOKIE, verifier)

    response = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    cookies = response.headers.get_list("set-cookie")
    assert any(cookie.startswith(f"{authn.SESSION_COOKIE}=") for cookie in cookies)
    assert any(
        cookie.startswith(f"{oauth.OAUTH_TRANSACTION_COOKIE}=;") and "Max-Age=0" in cookie
        for cookie in cookies
    )

    client.cookies.set(oauth.OAUTH_TRANSACTION_COOKIE, verifier)
    replay = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert replay.status_code == 400


async def _identity(_provider, _code):
    return "provider-subject", "social@example.com"


def test_hostile_provider_error_is_html_escaped_and_cookie_cleared(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    hostile = "%3Cscript%3Ealert%281%29%3C%2Fscript%3E%26%22%27"
    response = client.get(
        f"/api/auth/oauth/google/callback?error={hostile}", follow_redirects=False
    )
    assert response.status_code == 400
    assert "<script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&amp;&quot;&#x27;" in response.text
    assert response.headers["set-cookie"].startswith(
        f"{oauth.OAUTH_TRANSACTION_COOKIE}=;"
    )


def test_an_oauth_callback_without_valid_state_is_refused(client):
    monkey = client.get(
        "/api/auth/oauth/google/callback?code=abc&state=forged", follow_redirects=False
    )
    # Provider credentials are unset in tests, so this is refused before any
    # network call — which is itself the right answer for an unconfigured
    # provider, and must never be a redirect that sets a cookie.
    assert monkey.status_code == 400
    assert monkey.headers["set-cookie"].startswith(
        f"{oauth.OAUTH_TRANSACTION_COOKIE}=;"
    )


def test_an_oauth_identity_links_to_an_existing_password_account(client):
    register_and_verify(client, "both@example.com")
    client.post("/api/auth/logout")

    resolved = oauth.link_account("github", "gh-4242", "both@example.com")
    assert resolved == "both@example.com"

    # One account, not two, and the password still works.
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM accounts")
        assert cur.fetchone()["n"] == 1
    assert authn.oauth_email("github", "gh-4242") == "both@example.com"
    assert login(client, "both@example.com", GOOD_PASSWORD).status_code == 200


def test_a_relinked_identity_follows_its_subject_not_its_email(client):
    """A provider-side email change must not hand the account to a new row."""
    accounts.ensure_account("old@example.com", display="old")
    authn.link_oauth_identity("google", "sub-9", "old@example.com")
    assert oauth.link_account("google", "sub-9", "new@example.com") == "old@example.com"
    assert accounts.get_account("new@example.com") is None


def test_an_unconfigured_provider_is_not_startable(client):
    assert oauth.configured_providers() == []
    response = client.get("/api/auth/oauth/google/start", follow_redirects=False)
    assert response.status_code == 400
    assert "not enabled" in response.text


def test_a_configured_provider_binds_state_to_a_short_lived_browser_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id-123")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret-456")
    assert oauth.configured_providers() == ["google"]

    response = client.get("/api/auth/oauth/google/start", follow_redirects=False)
    assert response.status_code == 302
    target = urlparse(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == (
        oauth.PROVIDERS["google"].authorize_url
    )
    query = parse_qs(target.query)
    assert query["client_id"] == ["client-id-123"]
    assert query["redirect_uri"] == [
        "https://chudware.test/api/auth/oauth/google/callback"
    ]
    assert query["response_type"] == ["code"]
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{oauth.OAUTH_TRANSACTION_COOKIE}=")
    assert "; HttpOnly" in cookie and "; Secure" in cookie and "; SameSite=Lax" in cookie
    assert f"Max-Age={oauth.OAUTH_TRANSACTION_MAX_AGE}" in cookie

    state = query["state"][0]
    verifier = client.cookies.get(oauth.OAUTH_TRANSACTION_COOKIE)
    assert oauth.check_state("google", state, verifier) is True
    assert oauth.check_state("google", state, verifier) is False


# ---------------------------------------------------------------------------
# Microsoft (Entra ID)
# ---------------------------------------------------------------------------

MS_TENANT = "11111111-2222-3333-4444-555555555555"
MS_OTHER_TENANT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
MS_OID = "99999999-8888-7777-6666-555555555555"
MS_CLIENT_ID = "ms-client-id"
# Sentinel for "this claim is not in the token at all", which is distinct from
# a claim present and false.
ABSENT = object()


@pytest.fixture(scope="module")
def ms_key():
    """One RSA key for the module — keygen is the expensive part, not signing."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def ms_claims(**overrides):
    tid = overrides.get("tid", MS_TENANT)
    claims = {
        "iss": f"https://login.microsoftonline.com/{tid}/v2.0",
        "aud": MS_CLIENT_ID,
        "sub": "pairwise-subject-for-this-app",
        "tid": tid,
        "oid": MS_OID,
        "email": "engineer@contoso.com",
        "preferred_username": "attacker@chudware.com",
        "xms_edov": True,
        "iat": int(time.time()) - 5,
        "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not ABSENT}


def ms_signed(monkeypatch, ms_key, claims, tenant=MS_TENANT):
    """A real RS256 token plus a JWKS cache holding the matching public key.

    The JWKS *lookup* is stubbed, never ``jwt.decode``: signature, audience,
    expiry and required-claim checks all run for real, which is the whole
    point of the tests below.
    """
    monkeypatch.setattr(settings, "microsoft_client_id", MS_CLIENT_ID)
    monkeypatch.setattr(settings, "microsoft_client_secret", "ms-secret")
    monkeypatch.setattr(settings, "microsoft_tenant", tenant)

    public_key = ms_key.public_key()

    class _Cache:
        def get_signing_key(self, token):
            return type("_Key", (), {"key": public_key})()

    monkeypatch.setitem(oauth._microsoft_jwks, tenant, _Cache())
    return jwt.encode(claims, ms_key, algorithm="RS256")


def test_a_microsoft_state_is_bound_to_microsoft(client):
    state, verifier = oauth.mint_state("microsoft")
    assert oauth.check_state("google", state, verifier) is False, "state crossed providers"
    assert oauth.check_state("microsoft", state, verifier) is True
    assert oauth.check_state("microsoft", state, verifier) is False, "state was replayable"


def test_an_unconfigured_microsoft_provider_is_not_startable(client):
    assert "microsoft" not in oauth.configured_providers()
    response = client.get("/api/auth/oauth/microsoft/start", follow_redirects=False)
    assert response.status_code == 400
    assert "not enabled" in response.text


def test_configured_microsoft_redirects_to_its_tenant_with_a_state(client, monkeypatch):
    monkeypatch.setattr(settings, "microsoft_client_id", MS_CLIENT_ID)
    monkeypatch.setattr(settings, "microsoft_client_secret", "ms-secret")
    monkeypatch.setattr(settings, "microsoft_tenant", MS_TENANT)
    assert oauth.configured_providers() == ["microsoft"]

    response = client.get("/api/auth/oauth/microsoft/start", follow_redirects=False)
    assert response.status_code == 302
    target = urlparse(response.headers["location"])
    assert target.netloc == "login.microsoftonline.com"
    # The tenant is resolved from configuration at request time, so it appears
    # in the path even though PROVIDERS was built at import.
    assert target.path == f"/{MS_TENANT}/oauth2/v2.0/authorize"
    query = parse_qs(target.query)
    assert query["client_id"] == [MS_CLIENT_ID]
    assert query["redirect_uri"] == [
        "https://chudware.test/api/auth/oauth/microsoft/callback"
    ]
    assert query["response_type"] == ["code"]
    assert query["prompt"] == ["select_account"]
    verifier = client.cookies.get(oauth.OAUTH_TRANSACTION_COOKIE)
    assert oauth.check_state("microsoft", query["state"][0], verifier) is True


@pytest.mark.parametrize("edov", [True, "true", "1"])
def test_a_domain_verified_microsoft_token_yields_tid_dot_oid(monkeypatch, ms_key, edov):
    """Entra emits xms_edov as a boolean or as a string; both mean verified."""
    token = ms_signed(monkeypatch, ms_key, ms_claims(xms_edov=edov))
    subject, email = oauth._verify_microsoft_id_token(token)
    # oid+tid, not the app-scoped pairwise sub.
    assert subject == f"{MS_TENANT}.{MS_OID}"
    assert email == "engineer@contoso.com"


@pytest.mark.parametrize("edov", [ABSENT, False, "false", "0"])
def test_a_microsoft_token_without_a_verified_domain_is_refused(
    monkeypatch, ms_key, edov
):
    """nOAuth: without xms_edov the email is tenant-controlled input, and
    link_account() links by email, so trusting it is account takeover."""
    token = ms_signed(monkeypatch, ms_key, ms_claims(xms_edov=edov))
    with pytest.raises(oauth.OAuthError) as caught:
        oauth._verify_microsoft_id_token(token)
    assert "owns that email address" in caught.value.message


def test_a_microsoft_token_whose_issuer_disagrees_with_its_tid_is_refused(
    monkeypatch, ms_key
):
    """PyJWT's issuer check is off (the issuer is per-tenant), so this is the
    replacement check doing its job."""
    claims = ms_claims()
    claims["iss"] = f"https://login.microsoftonline.com/{MS_OTHER_TENANT}/v2.0"
    token = ms_signed(monkeypatch, ms_key, claims)
    with pytest.raises(oauth.OAuthError) as caught:
        oauth._verify_microsoft_id_token(token)
    assert "unexpected issuer" in caught.value.message


def test_a_single_tenant_deployment_refuses_another_tenants_token(monkeypatch, ms_key):
    """Internally consistent, correctly signed — and from the wrong directory."""
    token = ms_signed(
        monkeypatch, ms_key, ms_claims(tid=MS_OTHER_TENANT), tenant=MS_TENANT
    )
    with pytest.raises(oauth.OAuthError) as caught:
        oauth._verify_microsoft_id_token(token)
    assert "not in this deployment's directory" in caught.value.message


# ---------------------------------------------------------------------------
# Public-path boundary and the startup guard
# ---------------------------------------------------------------------------


def test_the_auth_subtree_is_public_but_its_prefix_neighbours_are_not(client):
    assert auth._is_public_path("/api/auth") is True
    assert auth._is_public_path("/api/auth/login") is True
    assert auth._is_public_path("/api/auth/oauth/google/callback") is True
    # The '/' boundary is load-bearing: a bare prefix match would open these.
    assert auth._is_public_path("/api/authorizations") is False
    assert auth._is_public_path("/api/auth-admin") is False
    assert auth._is_public_path("/healthzFOO") is False


def test_login_is_reachable_without_a_session_and_whoami_is_not(client):
    assert client.get("/api/whoami").status_code == 401
    assert login(client, "nobody@example.com", WRONG_PASSWORD).status_code == 401


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"dev_mode": True}, id="dev_mode"),
        pytest.param({"session_secret": "s3cret"}, id="session_secret_only"),
        pytest.param(
            {"cf_team_domain": "https://team.cloudflareaccess.com", "cf_aud": "aud"},
            id="cloudflare_access",
        ),
    ],
)
def test_the_startup_guard_accepts_each_strong_auth_configuration(kwargs):
    """session_secret alone is the fourth accepted condition.

    Without it a self-serve deployment — which authenticates its own users and
    needs no edge IdP at all — would refuse to boot.
    """
    from config import Settings

    cfg = Settings(
        **{
            "dev_mode": False,
            "session_secret": "",
            "cf_team_domain": "",
            "cf_aud": "",
            **kwargs,
        }
    )
    auth.AuthMiddleware(FastAPI(), cfg=cfg)  # must not raise


def test_the_startup_guard_still_refuses_a_deployment_with_no_strong_auth():
    from config import Settings

    cfg = Settings(dev_mode=False, session_secret="", cf_team_domain="", cf_aud="")
    with pytest.raises(RuntimeError, match="no strong auth configured"):
        auth.AuthMiddleware(FastAPI(), cfg=cfg)


# ---------------------------------------------------------------------------
# Status pages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        ("pending", "Confirm your email address"),
        ("denied", "declined"),
        ("suspended", "suspended"),
        ("trial_expired", "trial has ended"),
    ],
)
def test_the_denial_page_says_what_is_actually_wrong(status, expected):
    page = auth.status_page_html("someone@example.com", status)
    assert expected in page
    assert "someone@example.com" in page
    # The page this replaced told every blocked user to email the operator to
    # be added to a YAML file. That file does not exist any more.
    assert "users.yaml" not in page


def test_the_denial_page_escapes_the_email_it_echoes():
    page = auth.status_page_html('<script>alert(1)</script>@x.test', "denied")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
