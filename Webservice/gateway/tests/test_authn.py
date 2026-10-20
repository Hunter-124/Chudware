"""Self-serve authentication: the security properties, not the happy path.

Every test here defends a property that, if it broke, would be exploitable
rather than merely annoying:

* the login endpoint is not an account-existence oracle,
* wrong passwords are capped per-account and client network, never successes,
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
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from html.parser import HTMLParser
from pathlib import Path
from threading import Barrier, BoundedSemaphore, Event, Lock
from urllib.parse import parse_qs, quote, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request
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
    monkeypatch.setattr(mailer, "outbound", limits.RateLimiter(5, 3600.0))
    monkeypatch.setattr(
        mailer, "outbound_global",
        limits.RateLimiter(settings.mail_global_per_hour, 3600.0, max_keys=1),
    )
    _DELIVERED_MESSAGES.clear()
    monkeypatch.setattr(mailer, "_deliver", lambda _to, _message: None)
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://chudware.test")
    app = _protected_app()
    with TestClient(app, client=CLIENT, base_url="https://testserver") as c:
        yield c


@pytest.fixture()
def password_workers(monkeypatch):
    """Isolate bounded worker admission for blocking/cancellation regressions."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(authn, "_PASSWORD_POOL", pool)
        monkeypatch.setattr(authn, "_PASSWORD_SLOTS", BoundedSemaphore(2))
        yield


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
    """Capture the fixture mailbox before successful delivery erases envelopes."""
    pending = [
        {**row, "ctx": mailer.decrypt_context(row["to_addr"], row["template"], row["ctx_json"])}
        for row in _outbox() if row["sent_at"] is None and row["ctx_json"] != "{}"
    ]
    delivered = asyncio.run(mailer.drain_once())
    sent = {row["id"] for row in _outbox() if row["sent_at"] is not None}
    _DELIVERED_MESSAGES.extend(row for row in pending if row["id"] in sent)
    return delivered


def _delivered(template: str) -> dict:
    rows = [r for r in _DELIVERED_MESSAGES if r["template"] == template]
    assert len(rows) == 1, f"expected exactly one {template} mail, got {len(rows)}"
    return rows[0]


def _token_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["token"][0]


def _confirm_verification(client: TestClient, token: str):
    """Follow the mail link, then submit the actual server-rendered browser form."""
    class ConfirmationForm(HTMLParser):
        forms: list[dict[str, str]]

        def __init__(self):
            super().__init__()
            self.forms = []

        def handle_starttag(self, tag, attrs):
            if tag == "form":
                self.forms.append(dict(attrs))

    page = client.get(f"/api/auth/verify?token={quote(token, safe='')}", follow_redirects=False)
    assert page.status_code == 200
    assert "set-cookie" not in page.headers
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    parser = ConfirmationForm()
    parser.feed(page.text)
    assert len(parser.forms) == 1
    form = parser.forms[0]
    assert form["method"].lower() == "post"
    return client.post(form["action"], follow_redirects=False)


def _request(path: str, address: str = CLIENT[0]) -> Request:
    return Request({
        "type": "http", "scheme": "https", "method": "POST",
        "path": path, "headers": [], "client": (address, CLIENT[1]),
        "server": ("testserver", 443), "query_string": b"",
    })


def register(client: TestClient, email: str, password: str = GOOD_PASSWORD):
    return client.post(
        "/api/auth/register", json={"email": email, "password": password}
    )


def register_and_verify(client: TestClient, email: str, password: str = GOOD_PASSWORD):
    """Walk the customer path: register, read mail, open link, confirm, sign in."""
    assert register(client, email, password).status_code == 202
    assert _drain() == 1
    verify_url = _delivered("verify_email")["ctx"]["verify_url"]
    response = _confirm_verification(client, _token_from(verify_url))
    assert response.status_code == 303
    assert "set-cookie" not in response.headers
    assert login(client, email, password).status_code == 200
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

    token = _token_from(mail["ctx"]["verify_url"])
    for _ in range(2):
        preview = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
        assert preview.status_code == 200
        assert "set-cookie" not in preview.headers
        assert accounts.get_account("new@example.com").status == "pending"
        assert authn.get_credential("new@example.com")["email_verified"] is False
        with db.tx() as cur:
            cur.execute("SELECT used_at FROM email_tokens WHERE token_hash = %s", (authn.token_hash(token),))
            assert cur.fetchone()["used_at"] is None
    assert _confirm_verification(client, token).status_code == 303

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


@pytest.mark.parametrize("verified", [False, True])
def test_registering_a_taken_address_returns_the_same_202_not_a_409(client, verified):
    fresh = register(client, "first@example.com")
    _drain()
    if verified:
        token = _token_from(_delivered("verify_email")["ctx"]["verify_url"])
        assert _confirm_verification(client, token).status_code == 303
    original = authn.get_credential("first@example.com")
    repeat = register(client, "first@example.com", WRONG_PASSWORD)

    assert repeat.status_code == fresh.status_code == 202
    assert repeat.content == fresh.content

    assert _drain() == 1
    warning = _delivered("register_exists")
    assert warning["to_addr"] == "first@example.com"
    # And the second attempt did NOT overwrite the first password.
    assert len([r for r in _outbox() if r["template"] == "verify_email"]) == 1
    assert authn.get_credential("first@example.com") == original
    assert authn.verify_password(original["password_hash"], GOOD_PASSWORD)
    assert not authn.verify_password(original["password_hash"], WRONG_PASSWORD)


@pytest.mark.parametrize("peer,forwarded,expected", [
    ("127.0.0.1", "not-an-address", "127.0.0.1"),
    ("::1", "198.51.100.1, 198.51.100.2", "::1"),
    ("127.0.0.1", " 2001:0db8::0001 ", "2001:db8::1"),
    ("203.0.113.9", "198.51.100.1", "203.0.113.9"),
])
def test_session_metadata_uses_only_valid_trusted_forwarded_addresses(peer, forwarded, expected):
    request = _request("/api/auth/login", peer)
    request.scope["headers"] = [(b"cf-connecting-ip", forwarded.encode())]
    assert authn.client_ip(request) == expected


@pytest.mark.parametrize("status", ["pending", "active", "denied", "suspended"])
def test_registration_never_adds_a_password_to_an_existing_passwordless_account(
    client, status,
):
    email = "passwordless@example.com"
    accounts.ensure_account(email)
    if status != "pending":
        accounts.set_status(email, status)
    original = accounts.get_account(email)
    prior_token = authn.mint_token(email, authn.PURPOSE_VERIFY, authn.VERIFY_TTL)

    response = register(client, f"  {email.upper()}  ", WRONG_PASSWORD)
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert authn.get_credential(email) is None
    assert accounts.get_account(email) == original
    assert [row["template"] for row in _outbox()] == ["register_exists"]
    assert authn._live_token_email(prior_token, authn.PURPOSE_VERIFY) == email
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM accounts")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM email_tokens")
        assert cur.fetchone()["n"] == 1
    assert login(client, email, WRONG_PASSWORD).status_code == 401


def test_registration_losing_account_insert_race_cannot_attach_its_password(client, monkeypatch):
    email = "other-creator@example.com"
    real_lookup = accounts.get_account
    created = []

    def racing_lookup(address):
        account = real_lookup(address)
        if address == email and account is None and not created:
            # A non-registration creator does not share the advisory lock. It
            # commits a passwordless account after our precheck but before INSERT.
            with ThreadPoolExecutor(max_workers=1) as pool:
                created.append(pool.submit(accounts.ensure_account, email).result(timeout=10))
        return account

    monkeypatch.setattr(accounts, "get_account", racing_lookup)
    assert register(client, email, WRONG_PASSWORD).status_code == 202
    assert accounts.get_account(email) == created[0]
    assert authn.get_credential(email) is None
    assert [row["template"] for row in _outbox()] == ["register_exists"]
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM email_tokens")
        assert cur.fetchone()["n"] == 0


def test_racing_registrations_create_one_credential_and_one_verification(client, monkeypatch):
    email = "registration-race@example.com"
    hashed = Barrier(2)
    real_hash = authn.hash_password

    def concurrent_hash(password):
        digest = real_hash(password)
        hashed.wait(timeout=10)
        return digest

    monkeypatch.setattr(authn, "hash_password", concurrent_hash)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(register, client, email, GOOD_PASSWORD)
        second = pool.submit(register, client, email.upper(), WRONG_PASSWORD)
        responses = [first.result(timeout=15), second.result(timeout=15)]
    assert all(response.status_code == 202 for response in responses)
    assert responses[0].content == responses[1].content
    digest = authn.get_credential(email)["password_hash"]
    assert sum(authn.verify_password(digest, password) for password in [GOOD_PASSWORD, WRONG_PASSWORD]) == 1
    assert sorted(row["template"] for row in _outbox()) == ["register_exists", "verify_email"]
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM accounts")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM email_tokens WHERE used_at IS NULL")
        assert cur.fetchone()["n"] == 1


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

    # Only this account+network is locked. Correct passwords are refused here
    # until its failed-guess budget recovers, without affecting other networks.
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


def test_successful_logins_never_consume_the_failed_password_budget(client):
    email = "frequent@example.com"
    register_and_verify(client, email)
    for _ in range(limits.login_by_email.limit + 1):
        assert login(client, email.upper(), GOOD_PASSWORD).status_code == 200
    key = f"{limits.client_network(CLIENT[0])}|{email}"
    assert limits.login_by_email.allowed(key)
    assert key not in limits.login_by_email._hits
    # Successes also do not shorten the number of subsequent failed guesses.
    for _ in range(limits.login_by_email.limit):
        assert login(client, email, WRONG_PASSWORD).status_code == 401
    assert login(client, email, WRONG_PASSWORD).status_code == 429


def test_correct_unverified_password_is_not_counted_as_a_failed_guess(client):
    email = "still-pending@example.com"
    assert register(client, email).status_code == 202
    for _ in range(limits.login_by_email.limit + 1):
        assert login(client, email, GOOD_PASSWORD).status_code == 403
    for _ in range(limits.login_by_email.limit):
        assert login(client, email, WRONG_PASSWORD).status_code == 401
    assert login(client, email, WRONG_PASSWORD).status_code == 429


def test_failed_login_budget_is_normalized_and_isolated_by_network(client):
    email = "owner-network@example.com"
    register_and_verify(client, email)
    with TestClient(client.app, client=("198.51.100.44", 44_322), base_url="https://testserver") as attacker:
        for n in range(limits.login_by_email.limit):
            submitted = email.upper() if n % 2 else f" {email} "
            assert login(attacker, submitted, WRONG_PASSWORD).status_code == 401
        assert login(attacker, email, GOOD_PASSWORD).status_code == 429
        assert login(client, email, GOOD_PASSWORD).status_code == 200


def test_rotating_ipv6_addresses_share_the_failed_login_budget(client):
    email = "ipv6-owner@example.com"
    register_and_verify(client, email)
    for n in range(limits.login_by_email.limit):
        address = f"2001:db8:1234:5678::{n + 1}"
        with TestClient(client.app, client=(address, 44_322), base_url="https://testserver") as attacker:
            assert login(attacker, email, WRONG_PASSWORD).status_code == 401
    with TestClient(client.app, client=("2001:db8:1234:5678::ffff", 44_322), base_url="https://testserver") as attacker:
        assert login(attacker, email, GOOD_PASSWORD).status_code == 429
    with TestClient(client.app, client=("2001:db8:1234:5679::1", 44_322), base_url="https://testserver") as owner:
        assert login(owner, email, GOOD_PASSWORD).status_code == 200


def test_all_request_argon2_work_is_off_loop_and_outside_database_transactions(client, monkeypatch):
    real_hasher = authn._HASHER
    real_work = authn._password_work
    calls = []

    class GuardedHasher:
        def _off_loop(self):
            with pytest.raises(RuntimeError):
                asyncio.get_running_loop()
            assert db._active.get() is None

        def hash(self, password):
            self._off_loop()
            calls.append("hash")
            return real_hasher.hash(password)

        def verify(self, digest, password):
            self._off_loop()
            calls.append("verify")
            return real_hasher.verify(digest, password)

        def check_needs_rehash(self, digest):
            self._off_loop()
            return True  # Exercise the successful-login parameter upgrade too.

    async def checked_work(function, *args):
        assert db._active.get() is None, "request held a transaction across password work"
        return await real_work(function, *args)

    monkeypatch.setattr(authn, "_HASHER", GuardedHasher())
    monkeypatch.setattr(authn, "_password_work", checked_work)
    email = "off-loop@example.com"
    assert register(client, email).status_code == 202
    assert _drain() == 1
    token = _token_from(_delivered("verify_email")["ctx"]["verify_url"])
    assert _confirm_verification(client, token).status_code == 303
    assert login(client, email, GOOD_PASSWORD).status_code == 200
    assert login(client, "off-loop-unknown@example.com", WRONG_PASSWORD).status_code == 401
    assert client.post("/api/auth/forgot", json={"email": email}).status_code == 202
    assert _drain() == 1
    reset_token = _token_from(_delivered("password_reset")["ctx"]["reset_url"])
    assert client.post(
        "/api/auth/reset", json={"token": reset_token, "password": WRONG_PASSWORD},
    ).status_code == 200
    assert calls.count("hash") == 3
    assert calls.count("verify") == 2


def test_password_pool_saturation_rejects_all_flows_without_queueing(
    client, monkeypatch, password_workers,
):
    release = Event()
    started = Event()
    count_lock = Lock()
    calls = []
    real_hash = authn.hash_password
    recovery_email = "busy-recovery@example.com"
    accounts.ensure_account(recovery_email)
    reset_token = authn.mint_token(recovery_email, authn.PURPOSE_RESET, authn.RESET_TTL)

    def blocked_hash(password):
        with count_lock:
            calls.append(password)
            if len(calls) == 2:
                started.set()
        assert release.wait(10), "password jobs were not released"
        return real_hash(password)

    monkeypatch.setattr(authn, "hash_password", blocked_hash)

    async def exercise():
        registering = [
            asyncio.create_task(authn.register(
                _request("/api/auth/register"),
                {"email": f"busy-{n}@example.com", "password": GOOD_PASSWORD},
            ))
            for n in range(2)
        ]
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=6)
            # The event loop still services unrelated work while KDFs are busy.
            response = await asyncio.wait_for(authn.logout(_request("/api/auth/logout")), timeout=1)
            assert response.status_code == 200
            assert not any(task.done() for task in registering)
            for handler, path, body in [
                (authn.register, "/api/auth/register", {"email": "overflow@example.com", "password": GOOD_PASSWORD}),
                (authn.login, "/api/auth/login", {"email": "unknown@example.com", "password": GOOD_PASSWORD}),
                (authn.reset, "/api/auth/reset", {"token": reset_token, "password": GOOD_PASSWORD}),
            ]:
                with pytest.raises(HTTPException) as caught:
                    await asyncio.wait_for(handler(_request(path), body), timeout=1)
                assert caught.value.status_code == 429
                assert caught.value.headers["Retry-After"] == "1"
            assert calls == [GOOD_PASSWORD, GOOD_PASSWORD]
            assert authn._live_token_email(reset_token, authn.PURPOSE_RESET) == recovery_email
            assert accounts.get_account("overflow@example.com") is None
        finally:
            release.set()
            await asyncio.gather(*registering)

    asyncio.run(exercise())


def test_cancelled_password_request_holds_slot_until_worker_actually_finishes(client, monkeypatch):
    started = Event()
    release = Event()
    calls = []
    slots = BoundedSemaphore(1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(authn, "_PASSWORD_POOL", pool)
        monkeypatch.setattr(authn, "_PASSWORD_SLOTS", slots)

        def blocked_hash(password):
            calls.append(password)
            started.set()
            assert release.wait(10), "cancelled job was not released"
            return "unused-cancelled-digest"

        monkeypatch.setattr(authn, "hash_password", blocked_hash)

        async def exercise():
            task = asyncio.create_task(authn.register(
                _request("/api/auth/register"),
                {"email": "cancelled@example.com", "password": GOOD_PASSWORD},
            ))
            try:
                assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=6)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                with pytest.raises(HTTPException) as caught:
                    await authn.register(
                        _request("/api/auth/register"),
                        {"email": "not-queued@example.com", "password": GOOD_PASSWORD},
                    )
                assert caught.value.status_code == 429
                assert calls == [GOOD_PASSWORD]
                assert not slots.acquire(blocking=False)
            finally:
                release.set()
                if not task.done():
                    await task

        asyncio.run(exercise())
    # Executor shutdown waited for the actual job, not the cancelled awaiter.
    assert slots.acquire(blocking=False)
    slots.release()
    assert accounts.get_account("cancelled@example.com") is None
    assert accounts.get_account("not-queued@example.com") is None


def test_inflight_password_login_cannot_survive_oauth_only_admin_cutover(client, monkeypatch):
    email = "retired-login@example.com"
    accounts.ensure_account(email)
    accounts.set_status(email, "active")
    authn.set_password(email, GOOD_PASSWORD, verified=True)
    started = Event()
    release = Event()
    real_verify = authn.verify_password

    def blocked_verify(digest, password):
        started.set()
        assert release.wait(10), "login comparison was not released"
        return real_verify(digest, password)

    monkeypatch.setattr(authn, "verify_password", blocked_verify)

    async def exercise():
        signing_in = asyncio.create_task(authn.login(
            _request("/api/auth/login"), {"email": email, "password": GOOD_PASSWORD},
        ))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=6)
            assert await asyncio.wait_for(
                asyncio.to_thread(authn._cli, ["grant-admin", "--email", email, "--oauth-only"]),
                timeout=3,
            ) == 0
        finally:
            release.set()
        with pytest.raises(HTTPException) as caught:
            await signing_in
        assert caught.value.status_code == 401

    asyncio.run(exercise())
    assert authn.get_credential(email) is None
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM sessions WHERE email = %s", (email,))
        assert cur.fetchone()["n"] == 0


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
    assert (
        f"Max-Age={settings.session_ttl_days * 24 * 3600}"
        in set_cookie.split("; ")
    )
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
    assert denied.status_code == 401

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

    first = _confirm_verification(client, token)
    assert first.status_code == 303

    second = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert second.status_code == 400
    assert "no longer valid" in second.text
    assert client.post(f"/api/auth/verify?token={token}", follow_redirects=False).status_code == 400


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
        r["ctx"]["verify_url"]
        for r in _DELIVERED_MESSAGES
        if r["template"] == "verify_email"
    ]
    assert len(urls) == 2
    second = _token_from(urls[1])
    assert second != first

    # The superseded link must be dead, not merely one of two live links.
    assert client.get(
        f"/api/auth/verify?token={first}", follow_redirects=False
    ).status_code == 400
    assert _confirm_verification(client, second).status_code == 303


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
    client.cookies.set(
        oauth.OAUTH_TRANSACTION_COOKIE, oauth._transaction_value(verifier, "/")
    )

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

    client.cookies.set(
        oauth.OAUTH_TRANSACTION_COOKIE, oauth._transaction_value(verifier, "/")
    )
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
    verifier, next_path = oauth._split_transaction(
        client.cookies.get(oauth.OAUTH_TRANSACTION_COOKIE)
    )
    assert next_path == "/", "an absent next must not become a destination"
    assert oauth.check_state("google", state, verifier, next_path) is True
    assert oauth.check_state("google", state, verifier, next_path) is False


# The shape the desktop app sends: it parks a loopback listener and needs the
# browser to land back on /desktop/authorize with every one of these intact.
DESKTOP_NEXT = (
    "/desktop/authorize?port=49152&state=" + "d" * 40 + "&device=x&device_public_key=y"
)


def _google(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(oauth, "resolve_identity", _identity)


def _start(client, next_value: str) -> str:
    """Drive /start with a raw ``next`` and return the minted state."""
    response = client.get(
        f"/api/auth/oauth/google/start?next={quote(next_value, safe='')}",
        follow_redirects=False,
    )
    assert response.status_code == 302
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


def _oauth_callback(client, monkeypatch):
    """Real browser state/cookie flow; only provider identity retrieval is fake."""
    _google(monkeypatch)
    state = _start(client, "/")
    return client.get(
        f"/api/auth/oauth/google/callback?code=fixture-code&state={state}",
        follow_redirects=False,
    )


def test_oauth_discards_preregistered_password_and_stale_password_links(client, monkeypatch):
    email = "social@example.com"
    assert register(client, email).status_code == 202
    assert login(client, email, GOOD_PASSWORD).status_code == 403
    assert client.post("/api/auth/forgot", json={"email": email}).status_code == 202
    assert _drain() == 2
    verify_token = _token_from(_delivered("verify_email")["ctx"]["verify_url"])
    reset_token = _token_from(_delivered("password_reset")["ctx"]["reset_url"])
    old_session = authn.create_session(email)  # Pending-account sessions are supported.

    response = _oauth_callback(client, monkeypatch)
    assert response.status_code == 303
    oauth_session = client.cookies[authn.SESSION_COOKIE]
    assert authn.lookup_session(oauth_session) == email
    assert client.get("/api/whoami").json()["email"] == email
    account = accounts.get_account(email)
    assert (account.status, account.plan) == ("active", "trial")
    assert abs(account.trial_ends_at - (time.time() + settings.trial_days * 86_400)) < 300
    assert authn.get_credential(email) is None
    assert authn.lookup_session(old_session) is None

    assert client.get(
        f"/api/auth/verify?token={verify_token}", follow_redirects=False
    ).status_code == 400
    assert client.post(
        "/api/auth/reset", json={"token": reset_token, "password": WRONG_PASSWORD}
    ).status_code == 400
    assert authn.get_credential(email) is None
    assert client.get("/api/whoami").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert login(client, email, GOOD_PASSWORD).status_code == 401

    # A new recovery request cannot reintroduce password login either.
    assert client.post("/api/auth/forgot", json={"email": email}).status_code == 202
    assert _drain() == 1
    assert _delivered("oauth_only_reset")["ctx"]["provider"] == "google"


@pytest.mark.parametrize("plan", ["trial", "studio"])
def test_oauth_preserves_verified_password_and_existing_entitlements(client, monkeypatch, plan):
    email = "social@example.com"
    register_and_verify(client, email)
    accounts.set_plan(email, plan)
    before = accounts.get_account(email)
    credential = authn.get_credential(email)
    old_session = client.cookies[authn.SESSION_COOKIE]

    assert _oauth_callback(client, monkeypatch).status_code == 303
    assert client.get("/api/whoami").json()["email"] == email
    assert authn.get_credential(email) == credential
    assert authn.lookup_session(old_session) == email
    after = accounts.get_account(email)
    assert (after.status, after.plan, after.trial_ends_at) == (
        before.status, before.plan, before.trial_ends_at
    )
    assert client.post("/api/auth/logout").status_code == 200
    assert login(client, email, GOOD_PASSWORD).status_code == 200


@pytest.mark.parametrize("status", ["denied", "suspended"])
def test_oauth_cannot_issue_a_session_or_reactivate_a_blocked_account(
    client, monkeypatch, status
):
    email = "social@example.com"
    register_and_verify(client, email)
    accounts.set_status(email, status, decided_by="operator@example.com")
    client.cookies.clear()
    response = _oauth_callback(client, monkeypatch)
    assert response.status_code == 403
    assert oauth.OAUTH_TRANSACTION_COOKIE not in client.cookies
    assert authn.SESSION_COOKIE not in client.cookies
    assert accounts.get_account(email).status == status
    assert login(client, email, GOOD_PASSWORD).status_code == 403
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM sessions WHERE email = %s AND revoked_at IS NULL",
            (email,),
        )
        assert cur.fetchone()["n"] == 0


def test_oauth_completion_failure_rolls_back_link_password_and_tokens(client, monkeypatch):
    email = "social@example.com"
    assert register(client, email).status_code == 202
    credential = authn.get_credential(email)
    verify_token = authn.mint_token(email, authn.PURPOSE_VERIFY, authn.VERIFY_TTL)
    reset_token = authn.mint_token(email, authn.PURPOSE_RESET, authn.RESET_TTL)

    def fail_session(*_args, **_kwargs):
        raise RuntimeError("fixture session failure")

    monkeypatch.setattr(authn, "create_session", fail_session)
    with pytest.raises(RuntimeError, match="fixture session failure"):
        _oauth_callback(client, monkeypatch)
    assert accounts.get_account(email).status == "pending"
    assert authn.get_credential(email) == credential
    assert authn.oauth_email("google", "provider-subject") is None
    assert authn.consume_token(verify_token, authn.PURPOSE_VERIFY) == email
    assert authn.consume_token(reset_token, authn.PURPOSE_RESET) == email


def test_a_desktop_next_survives_start_and_is_the_callback_destination(
    client, monkeypatch
):
    _google(monkeypatch)
    state = _start(client, DESKTOP_NEXT)

    response = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == DESKTOP_NEXT
    assert any(
        cookie.startswith(f"{authn.SESSION_COOKIE}=")
        for cookie in response.headers.get_list("set-cookie")
    )


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "/\\evil.example/steal",
        "/desktop/authorize\\?port=1",
        "/desktop\r\nSet-Cookie: x=1",
        "desktop/authorize",
    ],
)
def test_an_offsite_next_collapses_to_the_portal_root(client, monkeypatch, hostile):
    _google(monkeypatch)
    state = _start(client, hostile)

    response = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_swapping_the_cookies_next_invalidates_the_state(client, monkeypatch):
    _google(monkeypatch)
    state = _start(client, DESKTOP_NEXT)
    verifier, _ = oauth._split_transaction(
        client.cookies.get(oauth.OAUTH_TRANSACTION_COOKIE)
    )
    client.cookies.set(
        oauth.OAUTH_TRANSACTION_COOKIE,
        oauth._transaction_value(verifier, "/admin"),
    )

    response = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "expired or was already used" in response.text


def test_a_malformed_transaction_cookie_is_not_a_verifier(client, monkeypatch):
    _google(monkeypatch)
    state = _start(client, DESKTOP_NEXT)
    verifier, _ = oauth._split_transaction(
        client.cookies.get(oauth.OAUTH_TRANSACTION_COOKIE)
    )
    # The old, unstructured value: a bare verifier with no destination beside it.
    client.cookies.set(oauth.OAUTH_TRANSACTION_COOKIE, verifier)

    response = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_a_desktop_state_is_still_single_use(client, monkeypatch):
    _google(monkeypatch)
    state = _start(client, DESKTOP_NEXT)
    cookie = client.cookies.get(oauth.OAUTH_TRANSACTION_COOKIE)

    first = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert first.status_code == 303
    client.cookies.set(oauth.OAUTH_TRANSACTION_COOKIE, cookie)
    replay = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert replay.status_code == 400


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
    verifier, next_path = oauth._split_transaction(
        client.cookies.get(oauth.OAUTH_TRANSACTION_COOKIE)
    )
    assert oauth.check_state("microsoft", query["state"][0], verifier, next_path) is True


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


def test_a_personal_microsoft_account_signs_in_without_xms_edov(monkeypatch, ms_key):
    """MSA never emits xms_edov, and nOAuth does not apply to it.

    The claim is Entra-only, so requiring it in the consumers tenant refuses
    every personal Microsoft account. That tenant has no administrator who can
    mint a user with someone else's address, which is the whole premise of the
    guard, so the exemption is narrow rather than a hole.
    """
    consumers = oauth.MICROSOFT_CONSUMERS_TID
    token = ms_signed(
        monkeypatch,
        ms_key,
        ms_claims(tid=consumers, xms_edov=ABSENT, email="hunter@outlook.com"),
        tenant="common",
    )
    subject, email = oauth._verify_microsoft_id_token(token)
    assert subject == f"{consumers}.{MS_OID}"
    assert email == "hunter@outlook.com"


def test_the_consumers_exemption_does_not_leak_to_a_real_tenant(monkeypatch, ms_key):
    """A directory that merely claims a consumers-like issuer is still Entra."""
    token = ms_signed(
        monkeypatch, ms_key, ms_claims(xms_edov=ABSENT), tenant="common"
    )
    with pytest.raises(oauth.OAuthError) as caught:
        oauth._verify_microsoft_id_token(token)
    assert "owns that email address" in caught.value.message


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


# ---------------------------------------------------------------------------
# Operator bootstrap CLI (grant-admin)
# ---------------------------------------------------------------------------


def _grant(email: str, password: str, monkeypatch) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
    return authn._cli(["grant-admin", "--email", email, "--password-stdin"])


def test_grant_admin_creates_an_active_admin_that_can_sign_in(client, monkeypatch):
    """The bootstrap has to produce an account that reaches /admin with no
    mailed verification link — a first admin exists before mail works."""
    assert _grant("Hunter@Chudware.com", "correct horse battery", monkeypatch) == 0

    account = accounts.get_account("hunter@chudware.com")
    assert account is not None
    assert (account.status, account.role) == ("active", "admin")
    assert accounts.effective_role(account) == "admin"

    response = client.post(
        "/api/auth/login",
        json={"email": "hunter@chudware.com", "password": "correct horse battery"},
    )
    assert response.status_code == 200


def test_grant_admin_is_idempotent_and_resets_the_password(monkeypatch):
    assert _grant("hunter@chudware.com", "first password here", monkeypatch) == 0
    assert _grant("hunter@chudware.com", "second password here", monkeypatch) == 0

    stored = authn.get_credential("hunter@chudware.com")
    assert stored is not None
    assert authn.verify_password(stored["password_hash"], "second password here")
    assert not authn.verify_password(stored["password_hash"], "first password here")


def test_grant_admin_refuses_a_password_under_the_minimum(monkeypatch):
    """A weak password must fail before the role is granted, not after."""
    assert _grant("weak@chudware.com", "x" * (authn.MIN_PASSWORD_CHARS - 1), monkeypatch) == 1
    assert accounts.get_account("weak@chudware.com") is None


def test_grant_admin_refuses_a_malformed_address(monkeypatch):
    assert _grant("not-an-email", "correct horse battery", monkeypatch) == 1


def test_ops_session_still_refuses_an_inactive_account():
    accounts.ensure_account("pending@chudware.com")
    assert authn._cli(["ops-session", "--email", "pending@chudware.com"]) == 1


def test_grant_admin_oauth_only_creates_an_admin_with_no_password(monkeypatch):
    """An OAuth operator must get the role without a password row existing —
    a throwaway password would be a second, weaker way into /admin."""

    def _no_prompt(*_a, **_k):
        raise AssertionError("--oauth-only must not prompt for a password")

    monkeypatch.setattr("getpass.getpass", _no_prompt)
    assert authn._cli(["grant-admin", "--email", "sso@chudware.com", "--oauth-only"]) == 0
    account = accounts.get_account("sso@chudware.com")
    assert (account.status, account.role) == ("active", "admin")
    assert authn.get_credential("sso@chudware.com") is None


@pytest.mark.parametrize("verified", [False, True])
def test_grant_admin_oauth_only_retires_existing_password_tokens_and_sessions(
    client, monkeypatch, verified
):
    email = "sso@chudware.com"
    if verified:
        register_and_verify(client, email)
    else:
        assert register(client, email).status_code == 202
    verify_token = authn.mint_token(email, authn.PURPOSE_VERIFY, authn.VERIFY_TTL)
    assert client.post("/api/auth/forgot", json={"email": email}).status_code == 202
    assert _drain() >= 1
    reset_token = _token_from(_delivered("password_reset")["ctx"]["reset_url"])
    old_session = authn.create_session(email)

    assert authn._cli(["grant-admin", "--email", email, "--oauth-only"]) == 0
    assert (accounts.get_account(email).status, accounts.get_account(email).role) == (
        "active", "admin"
    )
    assert authn.get_credential(email) is None
    assert authn.lookup_session(old_session) is None
    client.cookies.clear()
    assert login(client, email, GOOD_PASSWORD).status_code == 401
    assert client.get(
        f"/api/auth/verify?token={verify_token}", follow_redirects=False
    ).status_code == 400
    assert client.post(
        "/api/auth/reset", json={"token": reset_token, "password": WRONG_PASSWORD}
    ).status_code == 400
    assert authn.get_credential(email) is None
    # Before the first provider link, anonymous signup cannot restore a password.
    assert register(client, email).status_code == 202
    assert authn.get_credential(email) is None

    async def operator_identity(_provider, _code):
        return "operator-subject", email

    _google(monkeypatch)
    monkeypatch.setattr(oauth, "resolve_identity", operator_identity)
    state = _start(client, "/")
    response = client.get(
        f"/api/auth/oauth/google/callback?code=fixture-code&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/api/whoami").json() == {
        "email": email, "role": "admin", "status": "ok"
    }
    assert authn.get_credential(email) is None


def test_grant_admin_oauth_only_serializes_with_inflight_password_reset(client, monkeypatch):
    email = "sso@chudware.com"
    assert register(client, email).status_code == 202
    token = authn.mint_token(email, authn.PURPOSE_RESET, authn.RESET_TTL)
    consumed = Event()
    release = Event()
    admin_lock_entered = Event()
    real_consume = authn.consume_token
    real_lock = authn._lock_account

    def paused_consume(raw, purpose):
        result = real_consume(raw, purpose)
        if purpose == authn.PURPOSE_RESET and result is not None:
            consumed.set()
            assert release.wait(10), "reset transition was not released"
        return result

    def observed_lock(address):
        if consumed.is_set() and not release.is_set():
            admin_lock_entered.set()
        return real_lock(address)

    monkeypatch.setattr(authn, "consume_token", paused_consume)
    monkeypatch.setattr(authn, "_lock_account", observed_lock)
    request = Request({
        "type": "http", "scheme": "https", "method": "POST",
        "path": "/api/auth/reset", "headers": [], "client": CLIENT,
        "server": ("testserver", 443), "query_string": b"",
    })

    with ThreadPoolExecutor(max_workers=2) as pool:
        resetting = pool.submit(
            lambda: asyncio.run(authn.reset(
                request, {"token": token, "password": WRONG_PASSWORD}
            ))
        )
        granting = None
        try:
            assert consumed.wait(10), "reset did not consume its token"
            granting = pool.submit(
                authn._cli, ["grant-admin", "--email", email, "--oauth-only"]
            )
            assert admin_lock_entered.wait(10), "admin grant did not reach the account lock"
            with pytest.raises(FutureTimeout):
                granting.result(timeout=0.1)
        finally:
            release.set()
        assert resetting.result(timeout=10).status_code == 200
        assert granting.result(timeout=10) == 0

    assert authn.get_credential(email) is None
    assert login(client, email, WRONG_PASSWORD).status_code == 401
    assert authn.consume_token(token, authn.PURPOSE_RESET) is None
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM sessions WHERE email = %s AND revoked_at IS NULL",
            (email,),
        )
        assert cur.fetchone()["n"] == 0


def test_grant_admin_refuses_oauth_only_together_with_a_password(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("correct horse battery\n"))
    with pytest.raises(SystemExit) as caught:
        authn._cli(
            [
                "grant-admin",
                "--email",
                "both@chudware.com",
                "--oauth-only",
                "--password-stdin",
            ]
        )
    assert caught.value.code != 0
    assert accounts.get_account("both@chudware.com") is None


def test_grant_subscription_opens_a_far_future_entitlement_window():
    """There is no infinity sentinel: _entitlement_window only accepts
    paid_through > now with subscribed_since > 0, so the grant has to land as a
    real far-future epoch on an active account."""
    import entitlement

    accounts.ensure_account("seat@chudware.com")
    assert authn._cli(["grant-subscription", "--email", "seat@chudware.com"]) == 0
    account = accounts.get_account("seat@chudware.com")
    assert account.plan == "enterprise"
    assert account.subscription_status == "active"
    now = int(time.time())
    window = entitlement._entitlement_window(account, now)
    assert window is not None
    paid_through, subscribed_since = window
    assert subscribed_since > 0
    # At least 99 years out; the CLI grants 100 by default.
    assert paid_through - now > 99 * 365 * 86_400


def test_grant_subscription_is_idempotent_and_never_shortens_the_window():
    accounts.ensure_account("repeat@chudware.com")
    assert authn._cli(["grant-subscription", "--email", "repeat@chudware.com"]) == 0
    first = accounts.get_account("repeat@chudware.com").paid_through
    assert (
        authn._cli(
            ["grant-subscription", "--email", "repeat@chudware.com", "--years", "1"]
        )
        == 0
    )
    assert accounts.get_account("repeat@chudware.com").paid_through == first


def test_grant_subscription_refuses_an_unknown_account_and_an_unknown_plan():
    assert authn._cli(["grant-subscription", "--email", "ghost@chudware.com"]) == 1
    accounts.ensure_account("known@chudware.com")
    assert (
        authn._cli(
            ["grant-subscription", "--email", "known@chudware.com", "--plan", "moon"]
        )
        == 1
    )
    assert accounts.get_account("known@chudware.com").plan != "moon"


@pytest.mark.parametrize("existing", [False, True])
def test_verification_navigation_never_creates_or_replaces_session(client, existing):
    owner = "owner@example.com"
    if existing:
        register_and_verify(client, owner)
    before = client.cookies.get(authn.SESSION_COOKIE)
    accounts.ensure_account("attacker@example.com")
    authn.set_password("attacker@example.com", GOOD_PASSWORD, verified=False)
    token = authn.mint_token("attacker@example.com", authn.PURPOSE_VERIFY, authn.VERIFY_TTL)
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM sessions")
        count = cur.fetchone()["n"]
    preview = client.get(f"/api/auth/verify?token={token}", follow_redirects=False)
    assert preview.status_code == 200
    assert authn.get_credential("attacker@example.com")["email_verified"] is False
    assert client.cookies.get(authn.SESSION_COOKIE) == before
    response = _confirm_verification(client, token)
    assert response.status_code == 303
    assert response.headers["location"] == "/signin?verified=1"
    assert "set-cookie" not in response.headers
    assert client.cookies.get(authn.SESSION_COOKIE) == before
    assert authn.get_credential("attacker@example.com")["email_verified"] is True
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM sessions")
        assert cur.fetchone()["n"] == count
    if existing:
        assert client.get("/api/whoami").json()["email"] == owner
    else:
        assert client.get("/api/whoami").status_code == 401
    assert client.get(f"/api/auth/verify?token={token}", follow_redirects=False).status_code == 400


@pytest.mark.parametrize("purpose,endpoint,url_key", [
    (authn.PURPOSE_VERIFY, "/api/auth/resend-verification", "verify_url"),
    (authn.PURPOSE_RESET, "/api/auth/forgot", "reset_url"),
])
def test_recipient_throttle_preserves_last_deliverable_token(client, purpose, endpoint, url_key):
    email = "budget@example.com"
    accounts.ensure_account(email)
    authn.set_password(email, GOOD_PASSWORD, verified=False)
    tokens = []
    for _ in range(6):
        assert client.post(endpoint, json={"email": email}).status_code == 202
    rows = _outbox()
    assert len(rows) == 5
    for row in rows:
        ctx = mailer.decrypt_context(email, purpose, row["ctx_json"])
        tokens.append(_token_from(ctx[url_key]))
    with db.tx() as cur:
        cur.execute("SELECT token_hash FROM email_tokens WHERE email = %s AND used_at IS NULL", (email,))
        assert [r["token_hash"] for r in cur.fetchall()] == [authn.token_hash(tokens[-1])]
    assert all(token not in json.dumps(rows, default=str) for token in tokens)
    assert _drain() == 5
    if purpose == authn.PURPOSE_VERIFY:
        assert _confirm_verification(client, tokens[-1]).status_code == 303
    else:
        assert client.post("/api/auth/reset", json={"token": tokens[-1], "password": WRONG_PASSWORD}).status_code == 200
    assert authn.consume_token(tokens[-1], purpose) is None


@pytest.mark.parametrize("purpose,endpoint,url_key", [
    (authn.PURPOSE_VERIFY, "/api/auth/resend-verification", "verify_url"),
    (authn.PURPOSE_RESET, "/api/auth/forgot", "reset_url"),
])
def test_enqueue_failure_rolls_back_token_replacement(client, monkeypatch, purpose, endpoint, url_key):
    email = "rollback@example.com"
    accounts.ensure_account(email)
    authn.set_password(email, GOOD_PASSWORD, verified=False)
    assert client.post(endpoint, json={"email": email}).status_code == 202
    row = _outbox()[0]
    token = _token_from(mailer.decrypt_context(email, purpose, row["ctx_json"])[url_key])
    def fail_enqueue(message):
        with db.tx() as cur:
            cur.execute("SELECT 1/0")
    monkeypatch.setattr(mailer, "enqueue", fail_enqueue)
    assert client.post(endpoint, json={"email": email}).status_code == 202
    assert len(_outbox()) == 1
    assert authn.consume_token(token, purpose) == email


def test_registration_equalizes_real_hash_work_across_existing_identity_types(client, monkeypatch):
    accounts.ensure_account("password@example.com")
    authn.set_password("password@example.com", GOOD_PASSWORD, verified=False)
    accounts.ensure_account("oauth@example.com")
    authn.link_oauth_identity("google", "existing-sub", "oauth@example.com")
    accounts.ensure_account("admin@example.com")
    accounts.set_role("admin@example.com", "admin")
    real_hash = authn.hash_password
    calls = []
    def counted(password):
        calls.append(password)
        return real_hash(password)
    monkeypatch.setattr(authn, "hash_password", counted)
    responses = []
    for email in ["fresh@example.com", "password@example.com", "oauth@example.com", "admin@example.com"]:
        before = len(calls)
        responses.append(register(client, email))
        assert len(calls) == before + 1
    assert all(r.status_code == 202 and r.content == responses[0].content for r in responses)
    assert calls == [GOOD_PASSWORD] * 4
    assert authn.verify_password(authn.get_credential("password@example.com")["password_hash"], GOOD_PASSWORD)
    assert authn.get_credential("oauth@example.com") is None
    assert authn.get_credential("admin@example.com") is None


def test_database_readable_reset_envelope_is_not_a_redemption_capability(client):
    email = "ciphertext@example.com"
    accounts.ensure_account(email)
    authn.set_password(email, GOOD_PASSWORD, verified=True)
    accounts.set_status(email, "active")
    assert client.post("/api/auth/forgot", json={"email": email}).status_code == 202
    row = _outbox()[0]
    envelope = json.loads(row["ctx_json"])
    assert set(envelope) == {"encrypted_v1"}
    token = _token_from(mailer.decrypt_context(email, authn.PURPOSE_RESET, row["ctx_json"])["reset_url"])
    assert token not in json.dumps(row, default=str)
    for readable_value in [row["ctx_json"], envelope["encrypted_v1"]]:
        assert client.post("/api/auth/reset", json={"token": readable_value, "password": WRONG_PASSWORD}).status_code == 400
    assert authn.verify_password(authn.get_credential(email)["password_hash"], GOOD_PASSWORD)
    assert client.post("/api/auth/reset", json={"token": token, "password": WRONG_PASSWORD}).status_code == 200
    assert authn.verify_password(authn.get_credential(email)["password_hash"], WRONG_PASSWORD)


def test_registration_throttle_precedes_equalized_hash_work(client, monkeypatch):
    for _ in range(limits.login_by_ip.limit):
        limits.login_by_ip.check(CLIENT[0])
    def forbidden_hash(password):
        raise AssertionError("throttled registration must not run Argon2")
    monkeypatch.setattr(authn, "hash_password", forbidden_hash)
    assert register(client, "throttled@example.com").status_code == 429


@pytest.mark.parametrize("prior_link", [False, True])
def test_register_first_token_budget_refusal_preserves_previous_capability(client, prior_link):
    email = "first-budget@example.com"
    previous = None
    if prior_link:
        accounts.ensure_account(email)
        previous = authn.mint_token(email, authn.PURPOSE_VERIFY, authn.VERIFY_TTL)
    # Reserve the recipient budget before registration. An existing passwordless
    # account remains untouched, including its independently issued capability.
    # No transport or prepare stub: the actual reservation contract must refuse.
    for _ in range(mailer.outbound.limit):
        assert mailer.outbound.check(email)
    assert register(client, email).status_code == 202
    if prior_link:
        assert authn.get_credential(email) is None
    else:
        assert authn.get_credential(email)["email_verified"] is False
    assert _outbox() == []
    with db.tx() as cur:
        cur.execute("SELECT token_hash FROM email_tokens WHERE email=%s AND used_at IS NULL", (email,))
        assert [row["token_hash"] for row in cur.fetchall()] == (
            [authn.token_hash(previous)] if previous else []
        )
    if previous:
        assert _confirm_verification(client, previous).status_code == 303
        assert authn.get_credential(email) is None
    else:
        # A budget-refused first registration has no link, but a later admitted
        # resend recovers normally without another registration/password change.
        mailer.outbound._hits.clear()
        assert client.post("/api/auth/resend-verification", json={"email": email}).status_code == 202
        assert _drain() == 1
        token = _token_from(_delivered(authn.PURPOSE_VERIFY)["ctx"]["verify_url"])
        assert _confirm_verification(client, token).status_code == 303


@pytest.mark.parametrize("purpose,endpoint,url_key", [
    (authn.PURPOSE_VERIFY, "/api/auth/resend-verification", "verify_url"),
    (authn.PURPOSE_RESET, "/api/auth/forgot", "reset_url"),
])
def test_accepted_replacement_invalidates_previous_even_when_transport_finally_fails(
    client, monkeypatch, purpose, endpoint, url_key,
):
    import socket

    email = "accepted-then-failed@example.com"
    accounts.ensure_account(email)
    authn.set_password(email, GOOD_PASSWORD, verified=False)
    assert client.post(endpoint, json={"email": email}).status_code == 202
    previous = _token_from(mailer.decrypt_context(email, purpose, _outbox()[0]["ctx_json"])[url_key])
    assert _drain() == 1
    assert client.post(endpoint, json={"email": email}).status_code == 202
    latest = _outbox()[-1]
    replacement = _token_from(mailer.decrypt_context(email, purpose, latest["ctx_json"])[url_key])
    assert authn.consume_token(previous, purpose) is None
    # Real smtplib connection-refused; never contact a configured/provider host.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(mailer.settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(mailer.settings, "smtp_port", port)
    monkeypatch.setattr(mailer.settings, "smtp_tls", "implicit")
    monkeypatch.setattr(mailer, "_deliver", mailer._deliver_smtp)
    for _ in range(mailer.MAX_ATTEMPTS):
        with db.tx() as cur:
            cur.execute("UPDATE mail_outbox SET next_attempt_at=now() WHERE id=%s", (latest["id"],))
        assert asyncio.run(mailer.drain_once()) == 0
    with db.tx() as cur:
        cur.execute("SELECT attempts,ctx_json,sent_at FROM mail_outbox WHERE id=%s", (latest["id"],))
        row = cur.fetchone()
    assert row == {"attempts": mailer.MAX_ATTEMPTS, "ctx_json": "{}", "sent_at": None}
    assert authn.consume_token(previous, purpose) is None
    # Existing accepted-token policy is intentionally unchanged: the new token
    # remains valid, though no recipient delivery and no old-link recovery exist.
    assert authn.consume_token(replacement, purpose) == email


def test_protected_ops_session_uses_real_keys_without_secret_argv_or_ambient_configuration(
    pg, tmp_path,
):
    import base64
    import os
    import subprocess

    email = "operator-fixture@example.com"
    accounts.ensure_account(email)
    accounts.set_role(email, "admin")
    accounts.set_status(email, "active")
    helper = GATEWAY_DIR.parent / "deploy" / "gateway_preflight.py"
    values = {
        "CHUDWARE_DEPLOY_ENV": "prod", "CHUDWARE_DB_URL": pg,
        "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"m" * 32).decode(),
        "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"p" * 32).decode(),
        "CHUDWARE_MAIL_BACKEND": "smtp", "CHUDWARE_SMTP_HOST": "127.0.0.1",
        "CHUDWARE_SMTP_USER": "fixture", "CHUDWARE_SMTP_PASS": "fixture-transport-credential",
        # These optional credentials must not reach Settings construction.
        "CHUDWARE_WEB_STRIPE_SECRET_KEY": "fixture-partial-billing-would-refuse",
        "CHUDWARE_WEB_GOOGLE_CLIENT_ID": "fixture-partial-oauth",
    }
    for label, name in (("data", "CHUDWARE_WEB_DATA_ROOT"), ("workspace", "CHUDWARE_WORKSPACE_ROOT"),
                        ("bucket", "CHUDWARE_WEB_BUCKET_ROOT"), ("cells", "CHUDWARE_CELLS_ROOT")):
        values[name] = str(tmp_path / label)
        Path(values[name]).mkdir()
    path = tmp_path / "gateway.env"
    path.write_text("".join(name + "=" + value + "\n" for name, value in values.items()))
    path.chmod(0o600)
    command = [sys.executable, "-I", str(helper), "ops-session", "--env-file", str(path),
               "--environment", "prod", "--email", email, "--ttl-seconds", "120"]
    child = None
    try:
        # Hold the real account lock so the child remains observable at its DB sink.
        with db.tx() as cur:
            cur.execute("SELECT email FROM accounts WHERE email=%s FOR UPDATE", (email,))
            child = subprocess.Popen(command, env={
                "PATH": os.defpath, "CHUDWARE_DB_URL": "invalid-ambient-db",
                "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY": "invalid-ambient-key",
                "CHUDWARE_WEB_STRIPE_SECRET_KEY": "invalid-ambient-billing",
            }, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                cur.execute("SELECT pg_stat_clear_snapshot()")
                cur.execute("SELECT 1 FROM pg_stat_activity WHERE pid<>pg_backend_pid() "
                            "AND datname=current_database() AND wait_event_type='Lock'")
                if cur.fetchone():
                    break
                assert child.poll() is None, child.communicate()
                time.sleep(0.02)
            else:
                raise AssertionError("ops helper did not reach account locking")
            argv = Path(f"/proc/{child.pid}/cmdline").read_bytes()
            for name in ("CHUDWARE_DB_URL", "CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY",
                         "CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY", "CHUDWARE_SMTP_PASS"):
                assert values[name].encode() not in argv
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert stderr == ""
        token = stdout.strip()
        assert token and stdout == token + "\n"
        assert authn.lookup_session(token) == email
        with db.tx() as cur:
            cur.execute("SELECT user_agent,expires_at-created_at AS ttl FROM sessions WHERE sid=%s",
                        (authn.token_hash(token),))
            row = cur.fetchone()
        assert row["user_agent"] == "chudware-ops-cli"
        assert 119 <= row["ttl"].total_seconds() <= 121
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
