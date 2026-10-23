"""The desktop app's sign-in, end to end, driven by a provider identity.

The unit tests around ``oauth.mint_state`` prove the destination survives one
start/callback pair. This file proves the thing the user actually does: the
native app opens the system browser at ``/desktop/authorize``, is bounced to
``/signin``, clicks *Google* instead of typing a password, and the loopback
listener the app is holding open receives the signed entitlement. Every link in
that chain lives in a different module (auth middleware, oauth, entitlement), so
only a composed test can catch a break in the seams between them.
"""

from __future__ import annotations

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

import accounts  # noqa: E402
import auth  # noqa: E402
import authn  # noqa: E402
import db  # noqa: E402
import entitlement  # noqa: E402
import limits  # noqa: E402
import oauth  # noqa: E402
from config import settings  # noqa: E402

CLIENT = ("203.0.113.77", 51000)
EMAIL = "social@example.com"
PORT = 49152
DEVICE = "hunter-desktop"
STATE = "s" * 43


async def _identity(_provider, _code):
    return "provider-subject", EMAIL


@pytest.fixture()
def signing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "entitlement.key"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    monkeypatch.setenv("CHUDWARE_ENTITLEMENT_KEY_PATH", str(path))
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(entitlement, "EMBEDDED_PUBLIC_KEY", public)
    return key


@pytest.fixture()
def client(pg, signing_key, monkeypatch):
    """The real auth middleware over the real oauth and entitlement routers."""
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "public_base_url", "https://chudware.test")
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(oauth, "resolve_identity", _identity)
    monkeypatch.setattr(oauth, "login_by_ip", limits.RateLimiter(20, 900.0))
    app = FastAPI()
    app.include_router(oauth.router)
    app.include_router(entitlement.router)
    app.add_middleware(auth.AuthMiddleware)
    with TestClient(app, client=CLIENT, base_url="https://testserver") as c:
        yield c


def _device_public_key() -> str:
    key = Ed25519PrivateKey.generate()
    return entitlement._b64(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


def _authorize_path(device_public_key: str) -> str:
    return (
        f"/desktop/authorize?port={PORT}&state={STATE}&device={DEVICE}"
        f"&device_public_key={quote(device_public_key, safe='')}"
    )


def _entitle(email: str) -> None:
    """An account whose desktop window is open, as `grant-subscription` leaves it."""
    now = time.time()
    accounts.set_status(email, "active", decided_by="test")
    accounts.set_plan(email, "enterprise")
    accounts.set_subscription(
        email,
        subscription_status="active",
        subscribed_since=now - 86400,
        paid_through=now + 3650 * 86400,
        is_edu=False,
    )


def test_desktop_signin_completes_through_google(client):
    device_public_key = _device_public_key()
    authorize = _authorize_path(device_public_key)

    # 1. The app opens the browser at /desktop/authorize with no session.
    bounced = client.get(
        authorize, headers={"accept": "text/html"}, follow_redirects=False
    )
    assert bounced.status_code == 303
    signin = bounced.headers["location"]
    assert signin == "/signin?next=" + quote(authorize, safe="")
    next_path = parse_qs(urlparse(signin).query)["next"][0]

    # 2. The user clicks Google. login*.js stamps that next onto the anchor.
    start = client.get(
        f"/api/auth/oauth/google/start?next={quote(next_path, safe='')}",
        follow_redirects=False,
    )
    assert start.status_code == 302
    assert start.headers["location"].startswith("https://accounts.google.com/")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    # 3. Google redirects back. The session lands on the desktop handshake,
    #    not on the marketing page.
    callback = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == authorize

    # 4. The account now needs an open desktop window to be handed a token.
    _entitle(EMAIL)
    consent = client.get(authorize, follow_redirects=False)
    assert consent.status_code == 200
    assert "Authorize Chudware" in consent.text
    # A consent screen has to say what is being consented to, and it has to
    # arrive styled: this page is the last step of the sign-in flow, so it
    # carries that flow's stylesheet rather than browser defaults.
    assert entitlement._AUTH_CSS in consent.text
    for shown in (EMAIL, "Enterprise", DEVICE, f"127.0.0.1:{PORT}"):
        assert shown in consent.text, f"consent page never names {shown}"

    # 5. Confirming hands the token to the loopback listener the app is holding.
    #    It must arrive as a document navigation, not a redirected form POST:
    #    the app's `form-action 'self'` CSP aborts the loopback hop otherwise,
    #    which is what made clicking Authorize appear to do nothing.
    confirmed = client.post(
        "/desktop/authorize",
        data={
            "port": str(PORT),
            "state": STATE,
            "device": DEVICE,
            "device_public_key": device_public_key,
        },
        follow_redirects=False,
    )
    assert confirmed.status_code == 200
    assert "location" not in confirmed.headers
    refresh = re.search(
        r'<meta http-equiv="refresh" content="0;url=([^"]+)"', confirmed.text
    )
    assert refresh, "handoff page never navigates to the loopback listener"
    target = html.unescape(refresh.group(1))
    # The manual fallback button has to point at the same place.
    assert f'class="primary" href="{refresh.group(1)}"' in confirmed.text
    loopback = urlparse(target)
    assert (loopback.scheme, loopback.hostname, loopback.port) == (
        "http",
        "127.0.0.1",
        PORT,
    )
    assert loopback.path == "/callback"
    query = parse_qs(loopback.query)
    assert query["state"] == [STATE]
    payload = entitlement.verify_compact(query["token"][0])
    assert payload["email"] == EMAIL
    assert payload["plan"] == "enterprise"
    assert payload["paid_through"] > time.time() + 3000 * 86400


def test_oauth_cannot_be_redirected_off_origin(client):
    """A hostile ``next`` cannot turn the sign-in link into an open redirect."""
    start = client.get(
        "/api/auth/oauth/google/start?next=https%3A%2F%2Fevil.test%2Fsteal",
        follow_redirects=False,
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"


def test_desktop_signin_refuses_an_account_without_a_desktop_window(client):
    """Signing in through Google is not itself an entitlement."""
    device_public_key = _device_public_key()
    authorize = _authorize_path(device_public_key)
    start = client.get(
        f"/api/auth/oauth/google/start?next={quote(authorize, safe='')}",
        follow_redirects=False,
    )
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    accounts.set_status(EMAIL, "active", decided_by="test")
    accounts.set_plan(EMAIL, "desktop")  # paid plan, never paid for

    refused = client.get(authorize, follow_redirects=False)
    assert refused.status_code == 403
    assert "not active" in refused.text
    # The refusal is a dead end for the user, so it gets the same shell and a
    # way forward rather than a bare heading.
    assert entitlement._AUTH_CSS in refused.text
    assert "#pricing" in refused.text
    assert authn.oauth_providers_for(EMAIL) == ["google"]


@pytest.mark.parametrize("address,network", [
    ("198.51.100.44", "198.51.100.44"),
    ("2001:db8:1234::1", "2001:db8:1234::/64"),
])
def test_oauth_start_bounds_database_states_despite_network_lru_churn(
    client, monkeypatch, address, network
):
    monkeypatch.setattr(settings, "github_client_id", "github-id")
    monkeypatch.setattr(settings, "github_client_secret", "github-secret")
    limiter = limits.RateLimiter(10_000, 900.0, max_keys=2)
    monkeypatch.setattr(oauth, "login_by_ip", limiter)
    key = f"oauth-network:{network}"
    with TestClient(
        client.app, client=("127.0.0.1", 51000), base_url="https://testserver"
    ) as browser:
        for i in range(oauth.OAUTH_STATES_PER_NETWORK):
            # Rotate IPv6 addresses within one /64, and alternate providers.
            ip = f"2001:db8:1234::{i + 1:x}" if ":" in address else address
            provider = "google" if i % 2 == 0 else "github"
            start = browser.get(
                f"/api/auth/oauth/{provider}/start",
                headers={"CF-Connecting-IP": ip}, follow_redirects=False,
            )
            assert start.status_code == 302
            if i == 0:
                first_state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
                first_cookie = browser.cookies.get(oauth.OAUTH_TRANSACTION_COOKIE)
            # Evict this network from the process limiter between every start.
            for other in (f"192.0.2.{2 * i + 1}", f"192.0.2.{2 * i + 2}"):
                churn = browser.get(
                    "/api/auth/oauth/google/start",
                    headers={"CF-Connecting-IP": other}, follow_redirects=False,
                )
                assert churn.status_code == 302
            assert network not in limiter._hits

        denied = browser.get(
            "/api/auth/oauth/google/start",
            headers={"CF-Connecting-IP": address}, follow_redirects=False,
        )
        assert denied.status_code == 429
        assert int(denied.headers["retry-after"]) > 0
        with db.tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM email_tokens WHERE email = %s AND purpose = %s",
                (key, authn.PURPOSE_OAUTH_STATE),
            )
            assert cur.fetchone()["n"] == oauth.OAUTH_STATES_PER_NETWORK

        # A full bucket must not evict a browser's already-issued capability.
        browser.cookies.clear()
        browser.cookies.set(oauth.OAUTH_TRANSACTION_COOKIE, first_cookie)
        callback = browser.get(
            f"/api/auth/oauth/google/callback?code=abc&state={first_state}",
            headers={"CF-Connecting-IP": address}, follow_redirects=False,
        )
        assert callback.status_code == 303
        resumed = browser.get(
            "/api/auth/oauth/google/start",
            headers={"CF-Connecting-IP": address}, follow_redirects=False,
        )
        assert resumed.status_code == 302, "a consumed state must free its database slot"
        with db.tx() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM email_tokens WHERE email = %s AND purpose = %s",
                (key, authn.PURPOSE_OAUTH_STATE),
            )
            assert cur.fetchone()["n"] == oauth.OAUTH_STATES_PER_NETWORK


def test_oauth_start_prunes_expired_and_used_states_not_email_capabilities(client):
    for _ in range(oauth.OAUTH_STATES_PER_NETWORK):
        start = client.get("/api/auth/oauth/google/start", follow_redirects=False)
        assert start.status_code == 302
    # Include a legacy state row (empty email) and a different token purpose.
    authn.store_token("used-legacy-state", "", authn.PURPOSE_OAUTH_STATE, authn.OAUTH_STATE_TTL)
    with db.tx() as cur:
        cur.execute(
            "UPDATE email_tokens SET expires_at = now() - interval '1 second'"
            " WHERE purpose = %s", (authn.PURPOSE_OAUTH_STATE,),
        )
        cur.execute(
            "UPDATE email_tokens SET used_at = now(), expires_at = now() + interval '1 hour'"
            " WHERE token_hash = %s", (authn.token_hash("used-legacy-state"),),
        )
        cur.execute(
            "INSERT INTO email_tokens (token_hash, email, purpose, expires_at)"
            " VALUES (%s, %s, %s, now() - interval '1 second')",
            ("unrelated-email-capability", "ada@example.com", authn.PURPOSE_VERIFY),
        )
    restarted = client.get("/api/auth/oauth/google/start", follow_redirects=False)
    assert restarted.status_code == 302
    with db.tx() as cur:
        cur.execute(
            "SELECT email, EXTRACT(EPOCH FROM expires_at - now()) AS remaining"
            " FROM email_tokens WHERE purpose = %s", (authn.PURPOSE_OAUTH_STATE,),
        )
        (row,) = cur.fetchall()
        assert row["email"] == f"oauth-network:{CLIENT[0]}"
        assert 0 < row["remaining"] <= authn.OAUTH_STATE_TTL.total_seconds()
        cur.execute(
            "SELECT count(*) AS n FROM email_tokens WHERE purpose = %s",
            (authn.PURPOSE_VERIFY,),
        )
        assert cur.fetchone()["n"] == 1


def test_concurrent_oauth_start_consumers_cannot_exceed_database_cap(client, monkeypatch):
    monkeypatch.setattr(oauth, "login_by_ip", limits.RateLimiter(10_000, 900.0))

    def start(i):
        with TestClient(
            client.app, client=("127.0.0.1", 51000 + i), base_url="https://testserver"
        ) as browser:
            return browser.get(
                "/api/auth/oauth/google/start",
                headers={"CF-Connecting-IP": f"2001:db8:5678::{i + 1:x}"},
                follow_redirects=False,
            ).status_code

    with ThreadPoolExecutor(max_workers=8) as workers:
        statuses = list(workers.map(start, range(oauth.OAUTH_STATES_PER_NETWORK + 6)))
    assert statuses.count(302) == oauth.OAUTH_STATES_PER_NETWORK
    assert statuses.count(429) == 6
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM email_tokens WHERE purpose = %s",
            (authn.PURPOSE_OAUTH_STATE,),
        )
        assert cur.fetchone()["n"] == oauth.OAUTH_STATES_PER_NETWORK


@pytest.mark.parametrize("tamper", ["verifier", "destination"])
def test_bounded_oauth_state_preserves_cookie_and_destination_binding(client, tamper):
    start = client.get("/api/auth/oauth/google/start?next=%2Fapp", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    transaction = client.cookies.get(oauth.OAUTH_TRANSACTION_COOKIE)
    verifier, next_path = oauth._split_transaction(transaction)
    forged = oauth._transaction_value(
        "another-browser" if tamper == "verifier" else verifier,
        "/different" if tamper == "destination" else next_path,
    )
    client.cookies.clear()
    client.cookies.set(oauth.OAUTH_TRANSACTION_COOKIE, forged)
    rejected = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}", follow_redirects=False,
    )
    assert rejected.status_code == 400
    with db.tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM email_tokens WHERE token_hash = %s",
            (authn.token_hash(state),),
        )
        assert cur.fetchone()["n"] == 1, "a forged cookie must not consume the state"
    client.cookies.clear()
    client.cookies.set(oauth.OAUTH_TRANSACTION_COOKIE, transaction)
    accepted = client.get(
        f"/api/auth/oauth/google/callback?code=abc&state={state}", follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/app"
    assert oauth.check_state("google", state, verifier, next_path) is False


def test_oauth_start_and_callback_share_the_canonical_network_rate_key(client, monkeypatch):
    monkeypatch.setattr(oauth, "login_by_ip", limits.RateLimiter(2, 900.0))
    with TestClient(
        client.app, client=("127.0.0.1", 51000), base_url="https://testserver"
    ) as browser:
        start = browser.get(
            "/api/auth/oauth/google/start",
            headers={"CF-Connecting-IP": "2001:db8:9999::1"}, follow_redirects=False,
        )
        assert start.status_code == 302
        cancelled = browser.get(
            "/api/auth/oauth/google/callback?error=cancelled",
            headers={"CF-Connecting-IP": "2001:db8:9999::2"}, follow_redirects=False,
        )
        assert cancelled.status_code == 400
        denied = browser.get(
            "/api/auth/oauth/google/callback?error=cancelled",
            headers={"CF-Connecting-IP": "2001:db8:9999::3"}, follow_redirects=False,
        )
        assert denied.status_code == 429
