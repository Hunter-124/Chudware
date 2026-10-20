"""Security and tenancy contracts for browser-driven AI subscription sign-in."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway import accounts, db, provider_accounts
from gateway.config import settings
from gateway.limits import RateLimiter

A = "alice@example.com"
B = "bob@example.com"
OPERATOR = "operator@example.com"

CLAUDE_ACCESS = "claude-access-token-never-leak-0001"
CLAUDE_REFRESH = "claude-refresh-token-never-leak-0001"
CODEX_ACCESS_PAYLOAD = {
    "https://api.openai.com/auth": {
        "chatgpt_account_id": "acct-chatgpt-9",
        "chatgpt_plan_type": "plus",
    },
    "https://api.openai.com/profile": {"email": "Owner@Example.com"},
}


def _seed(email: str, *, plan: str = "studio", ai_grant: bool = True) -> None:
    """An approved account with a live metered pool, so `key_source` defaults to
    "pool" and the subscription path is proven to override a working fallback
    rather than merely filling a hole."""
    accounts.ensure_account(email, display=email.split("@", 1)[0].title())
    accounts.set_status(email, "active", decided_by="test")
    with db.tx() as cur:
        cur.execute(
            "UPDATE accounts SET plan = %s, ai_grant = %s, credit_balance = 5000 "
            "WHERE email = %s",
            (plan, ai_grant, email),
        )

def _verified(email: str, *, via_oauth: bool = False) -> None:
    with db.tx() as cur:
        if via_oauth:
            cur.execute(
                "INSERT INTO oauth_identities(provider, subject, email) VALUES ('google', %s, %s)",
                (f"test-{email}", email),
            )
        else:
            cur.execute(
                "INSERT INTO credentials(email, password_hash, email_verified) VALUES (%s, 'unused', TRUE)",
                (email,),
            )


def _trial_domain(domain: str = "example.com") -> None:
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO trial_domains(domain, trial_days, ai_credits) VALUES (%s, 14, 0)",
            (domain,),
        )



def _client_for(email: str, role: str = "user") -> TestClient:
    app = FastAPI()
    app.include_router(provider_accounts.router)

    class AttachUser:
        def __init__(self, inner: ASGIApp) -> None:
            self.app = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                scope.setdefault("state", {})["user"] = SimpleNamespace(
                    email=email, display=email, role=role, heavy=False
                )
            await self.app(scope, receive, send)

    app.add_middleware(AttachUser)
    return TestClient(app)


@pytest.fixture(autouse=True)
def encryption_secret(monkeypatch: pytest.MonkeyPatch):
    encoded = base64.urlsafe_b64encode(b"S" * 32).decode().rstrip("=")
    monkeypatch.setattr(settings, "provider_key_encryption_key", encoded)
    monkeypatch.setattr(provider_accounts, "provider_oauth_starts", RateLimiter(50, 60.0))
    monkeypatch.setattr(
        provider_accounts, "provider_oauth_completions", RateLimiter(500, 60.0)
    )
    yield
    db.close_pool()


def _response(status: int, payload: dict | str) -> httpx.Response:
    if isinstance(payload, str):
        return httpx.Response(status, text=payload, request=httpx.Request("POST", "https://x"))
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "https://x"))


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


class VendorStub:
    """Records every outbound token/device call and answers it scriptedly."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.device_approved = False
        self.claude_status = 200
        self.refresh_status = 200
        self.access_counter = 0

    def __call__(self, url: str, *, json_body=None, form_body=None, headers=None):
        body = json_body if json_body is not None else (form_body or {})
        self.calls.append((url, dict(body)))

        if url == provider_accounts.ANTHROPIC_TOKEN_URL:
            if body.get("grant_type") == "refresh_token":
                if self.refresh_status != 200:
                    return _response(self.refresh_status, {"error": "invalid_grant"})
                self.access_counter += 1
                return _response(
                    200,
                    {
                        "access_token": f"{CLAUDE_ACCESS}-refreshed-{self.access_counter}",
                        "refresh_token": f"{CLAUDE_REFRESH}-rotated-{self.access_counter}",
                        "expires_in": 3600,
                    },
                )
            if self.claude_status != 200:
                return _response(
                    self.claude_status,
                    {"error_description": "authorization code is invalid"},
                )
            return _response(
                200,
                {
                    "access_token": CLAUDE_ACCESS,
                    "refresh_token": CLAUDE_REFRESH,
                    "expires_in": 3600,
                    "account": {"uuid": "acct-claude-7", "email_address": "owner@example.com"},
                    "organization": {"uuid": "org-1", "name": "Max"},
                },
            )

        if url == provider_accounts.CODEX_DEVICE_USERCODE_URL:
            return _response(200, {"device_auth_id": "dev-1", "user_code": "ABCD-EFGH"})

        if url == provider_accounts.CODEX_DEVICE_TOKEN_URL:
            if not self.device_approved:
                return _response(403, {"error": "authorization_pending"})
            return _response(
                200, {"authorization_code": "codex-code-1", "code_verifier": "codex-verifier-1"}
            )

        if url == provider_accounts.CODEX_TOKEN_URL:
            return _response(
                200,
                {
                    "access_token": _jwt(CODEX_ACCESS_PAYLOAD),
                    "refresh_token": "codex-refresh-1",
                    "expires_in": 3600,
                },
            )

        raise AssertionError(f"unexpected vendor call to {url}")


@pytest.fixture
def vendor(monkeypatch: pytest.MonkeyPatch) -> VendorStub:
    stub = VendorStub()
    monkeypatch.setattr(provider_accounts, "_post", stub)
    return stub


def _raw_rows(table: str) -> list[dict]:
    with db.tx() as cur:
        cur.execute(f"SELECT * FROM {table}")
        return list(cur.fetchall())


# ---------------------------------------------------------------------------
# Pasted-code parsing
# ---------------------------------------------------------------------------


def test_pasted_code_accepts_every_shape_a_browser_produces() -> None:
    assert provider_accounts.parse_pasted_code(
        "http://localhost:54545/callback?code=abc123&state=xyz"
    ) == ("abc123", "xyz")
    assert provider_accounts.parse_pasted_code("  abc123#xyz  ") == ("abc123", "xyz")
    assert provider_accounts.parse_pasted_code("abc123") == ("abc123", "")
    # Anthropic sometimes folds the state into the code as a fragment.
    assert provider_accounts.parse_pasted_code(
        "http://localhost:54545/callback?code=abc123%23xyz"
    ) == ("abc123", "xyz")


def test_pasted_code_surfaces_a_denial_instead_of_a_generic_failure() -> None:
    with pytest.raises(provider_accounts.SubscriptionAuthError) as caught:
        provider_accounts.parse_pasted_code(
            "http://localhost:54545/callback?error=access_denied"
            "&error_description=You+cancelled+the+request"
        )
    assert "You cancelled the request" in str(caught.value)

    with pytest.raises(provider_accounts.SubscriptionAuthError):
        provider_accounts.parse_pasted_code("https://claude.ai/oauth/authorize?foo=bar")

    with pytest.raises(provider_accounts.SubscriptionAuthError):
        provider_accounts.parse_pasted_code("   ")


# ---------------------------------------------------------------------------
# Claude: PKCE + paste
# ---------------------------------------------------------------------------


def test_claude_round_trip_binds_the_pasted_code_to_the_issued_challenge(
    vendor: VendorStub,
) -> None:
    _seed(A)
    client = _client_for(A)

    begin = client.post("/api/providers/accounts/anthropic-sub/begin", json={})
    assert begin.status_code == 200, begin.text
    body = begin.json()
    assert body["kind"] == "pkce_paste"

    query = parse_qs(urlsplit(body["authorize_url"]).query)
    assert query["client_id"] == [provider_accounts.ANTHROPIC_CLIENT_ID]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [provider_accounts.ANTHROPIC_REDIRECT_URI]
    challenge = query["code_challenge"][0]
    state = query["state"][0]

    done = client.post(
        "/api/providers/accounts/anthropic-sub/complete",
        json={
            "flow_id": body["flow_id"],
            "code": f"http://localhost:54545/callback?code=live-code&state={state}",
        },
    )
    assert done.status_code == 200, done.text
    account = done.json()["account"]
    assert done.json()["status"] == "connected"
    assert account["label"] == "owner@example.com"
    assert account["plan"] == "Max"
    assert account["scope"] == "personal"
    assert account["expired"] is False

    exchange = [call for call in vendor.calls if call[1].get("grant_type") == "authorization_code"]
    assert len(exchange) == 1
    sent = exchange[0][1]
    assert sent["code"] == "live-code"
    # The verifier the gateway kept must be the pre-image of the challenge it published.
    recomputed = (
        base64.urlsafe_b64encode(hashlib.sha256(sent["code_verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert recomputed == challenge


def test_a_pasted_state_from_another_sign_in_is_refused(vendor: VendorStub) -> None:
    _seed(A)
    client = _client_for(A)
    begin = client.post("/api/providers/accounts/anthropic-sub/begin", json={}).json()

    done = client.post(
        "/api/providers/accounts/anthropic-sub/complete",
        json={
            "flow_id": begin["flow_id"],
            "code": "http://localhost:54545/callback?code=live-code&state=someone-elses",
        },
    )
    assert done.status_code == 400
    assert "different sign-in" in done.json()["detail"]
    # Nothing was exchanged and nothing was stored.
    assert not [c for c in vendor.calls if c[0] == provider_accounts.ANTHROPIC_TOKEN_URL]
    assert _raw_rows("provider_accounts") == []


def test_the_vendors_own_rejection_is_shown_verbatim(vendor: VendorStub) -> None:
    _seed(A)
    vendor.claude_status = 400
    client = _client_for(A)
    begin = client.post("/api/providers/accounts/anthropic-sub/begin", json={}).json()
    done = client.post(
        "/api/providers/accounts/anthropic-sub/complete",
        json={"flow_id": begin["flow_id"], "code": "stale-code"},
    )
    assert done.status_code == 400
    assert "authorization code is invalid" in done.json()["detail"]


def test_a_flow_is_single_use_and_its_verifier_is_never_stored_in_the_clear(
    vendor: VendorStub,
) -> None:
    _seed(A)
    client = _client_for(A)
    begin = client.post("/api/providers/accounts/anthropic-sub/begin", json={}).json()

    flows = _raw_rows("provider_oauth_flows")
    assert len(flows) == 1
    blob = bytes(flows[0]["encrypted_secret"])
    exchange_verifier_marker = b"verifier"
    assert exchange_verifier_marker not in blob
    assert begin["flow_id"].encode() not in blob

    state = parse_qs(urlsplit(begin["authorize_url"]).query)["state"][0]
    first = client.post(
        "/api/providers/accounts/anthropic-sub/complete",
        json={"flow_id": begin["flow_id"], "code": f"code-1#{state}"},
    )
    assert first.status_code == 200
    assert _raw_rows("provider_oauth_flows") == []

    replay = client.post(
        "/api/providers/accounts/anthropic-sub/complete",
        json={"flow_id": begin["flow_id"], "code": f"code-1#{state}"},
    )
    assert replay.status_code == 410


def test_tokens_never_appear_in_a_response_or_in_plaintext_at_rest(
    vendor: VendorStub,
) -> None:
    _seed(A)
    client = _client_for(A)
    begin = client.post("/api/providers/accounts/anthropic-sub/begin", json={}).json()
    done = client.post(
        "/api/providers/accounts/anthropic-sub/complete",
        json={"flow_id": begin["flow_id"], "code": "code-1"},
    )
    assert CLAUDE_ACCESS not in done.text
    assert CLAUDE_REFRESH not in done.text

    listed = client.get("/api/providers/accounts")
    assert CLAUDE_ACCESS not in listed.text
    assert CLAUDE_REFRESH not in listed.text

    rows = _raw_rows("provider_accounts")
    assert len(rows) == 1
    stored = bytes(rows[0]["encrypted_tokens"])
    assert CLAUDE_ACCESS.encode() not in stored
    assert CLAUDE_REFRESH.encode() not in stored


# ---------------------------------------------------------------------------
# ChatGPT: device code
# ---------------------------------------------------------------------------


def test_chatgpt_device_flow_reports_pending_then_connects(vendor: VendorStub) -> None:
    _seed(A)
    client = _client_for(A)

    begin = client.post("/api/providers/accounts/openai-codex/begin", json={})
    assert begin.status_code == 200, begin.text
    body = begin.json()
    assert body["kind"] == "device"
    assert body["user_code"] == "ABCD-EFGH"
    assert body["verification_url"] == provider_accounts.CODEX_DEVICE_VERIFICATION_URL

    pending = client.post(
        "/api/providers/accounts/openai-codex/complete", json={"flow_id": body["flow_id"]}
    )
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"

    vendor.device_approved = True
    done = client.post(
        "/api/providers/accounts/openai-codex/complete", json={"flow_id": body["flow_id"]}
    )
    assert done.status_code == 200, done.text
    account = done.json()["account"]
    assert account["account_key"] == "acct-chatgpt-9"
    assert account["label"] == "owner@example.com"
    assert account["plan"] == "plus"


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


def _connect_claude(email: str, role: str = "user", scope: str = "personal") -> dict:
    client = _client_for(email, role)
    begin = client.post(
        "/api/providers/accounts/anthropic-sub/begin", json={"scope": scope}
    ).json()
    done = client.post(
        "/api/providers/accounts/anthropic-sub/complete",
        json={"flow_id": begin["flow_id"], "code": "code-1"},
    )
    assert done.status_code == 200, done.text
    return done.json()["account"]


def test_one_account_can_neither_see_nor_unlink_anothers_subscription(
    vendor: VendorStub,
) -> None:
    _seed(A)
    _seed(B)
    linked = _connect_claude(A)

    bob = _client_for(B)
    assert bob.get("/api/providers/accounts").json()["accounts"] == []

    stolen = bob.delete(f"/api/providers/accounts/anthropic-sub/{linked['account_key']}")
    assert stolen.status_code == 404
    assert len(_raw_rows("provider_accounts")) == 1

    # And Bob's turns never resolve Alice's personal grant.
    assert provider_accounts.resolve_subscription_token(B, "anthropic") is None
    assert provider_accounts.resolve_subscription_token(A, "anthropic") is not None


def test_only_an_admin_can_connect_or_see_a_shared_account(vendor: VendorStub) -> None:
    _seed(A)
    _seed(OPERATOR)

    refused = _client_for(A).post(
        "/api/providers/accounts/anthropic-sub/begin", json={"scope": "shared"}
    )
    assert refused.status_code == 403
    assert "administrator" in refused.json()["detail"]

    shared = _connect_claude(OPERATOR, role="admin", scope="shared")
    assert shared["scope"] == "shared"

    admin_view = _client_for(OPERATOR, "admin").get("/api/providers/accounts").json()
    assert admin_view["can_manage_shared"] is True
    assert [row["scope"] for row in admin_view["accounts"]] == ["shared"]

    user_view = _client_for(A).get("/api/providers/accounts").json()
    assert user_view["can_manage_shared"] is False
    assert user_view["accounts"] == []


def test_nontrial_customer_never_gets_admin_claude_link(
    vendor: VendorStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(A)
    _seed(OPERATOR)
    accounts.set_role(OPERATOR, "admin")
    _connect_claude(OPERATOR, role="admin", scope="shared")
    _verified(A)

    assert provider_accounts.resolve_subscription_token(A, "anthropic") is None


def test_trial_customer_gets_admin_claude_but_not_lookalike_or_unverified(
    vendor: VendorStub,
) -> None:
    _seed(OPERATOR)
    accounts.set_role(OPERATOR, "admin")
    _connect_claude(OPERATOR, role="admin", scope="shared")
    _trial_domain()
    eligible = "learner@dept.example.com"
    lookalike = "learner@notexample.com"
    pending = "pending@example.com"
    for email in (eligible, lookalike, pending):
        _seed(email)
        _verified(email, via_oauth=email == eligible)
    unverified = "unverified@example.com"
    _seed(unverified)
    accounts.set_status(pending, "pending", decided_by="test")

    grant = provider_accounts.resolve_subscription_token(eligible, "anthropic")
    assert grant is not None
    assert grant["owner_email"] == OPERATOR
    assert grant["sponsored"] is True
    assert grant["scope"] == "shared"
    for email in (lookalike, pending, unverified):
        assert provider_accounts.resolve_subscription_token(email, "anthropic") is None
    with db.tx() as cur:
        cur.execute(
            "UPDATE trial_domains SET expires_at = now() - interval '1 day'"
        )
    assert provider_accounts.resolve_subscription_token(eligible, "anthropic") is None


def test_own_personal_link_wins_over_trial_sponsor(vendor: VendorStub) -> None:
    _seed(A)
    _seed(OPERATOR)
    accounts.set_role(OPERATOR, "admin")
    _verified(A)
    _trial_domain()
    _connect_claude(OPERATOR, role="admin", scope="shared")
    _connect_claude(A)

    grant = provider_accounts.resolve_subscription_token(A, "anthropic")
    assert grant is not None
    assert grant["scope"] == "personal"
    assert grant["owner_email"] == A
    assert grant["sponsored"] is False
    overridden = provider_accounts.resolve_subscription_token(
        A, "anthropic", trial_provider_override=True
    )
    assert overridden["owner_email"] == A
    unverified_own = "other@outside.net"
    _seed(unverified_own)
    _connect_claude(unverified_own)
    assert provider_accounts.resolve_subscription_token(
        unverified_own, "anthropic", trial_provider_override=True
    ) is None

def test_sponsor_links_rotate_and_only_active_admins_qualify(vendor: VendorStub) -> None:
    student = "student@example.com"
    second_admin = "second-admin@elsewhere.net"
    _seed(student)
    _verified(student)
    _trial_domain()
    for admin in (OPERATOR, second_admin):
        _seed(admin)
        accounts.set_role(admin, "admin")
    _connect_claude(OPERATOR, role="admin", scope="shared")
    provider_accounts._persist_account(
        second_admin, "anthropic-sub", "personal",
        {
            "account_key": "second-claude",
            "label": "second",
            "plan": "Max",
            "tokens": {
                "access_token": "second-access",
                "refresh_token": "second-refresh",
                "expires_at_ms": 4_000_000_000_000,
            },
        },
    )
    first = provider_accounts.resolve_subscription_token(student, "anthropic")
    second = provider_accounts.resolve_subscription_token(student, "anthropic")
    assert first is not None and second is not None
    assert {first["owner_email"], second["owner_email"]} == {OPERATOR, second_admin}
    accounts.set_status(second_admin, "suspended", decided_by="test")
    assert provider_accounts.resolve_subscription_token(student, "anthropic")["owner_email"] == OPERATOR


def test_sponsor_refresh_uses_admin_owner_aad(vendor: VendorStub) -> None:
    _seed(A)
    _verified(A)
    _trial_domain()
    _seed(OPERATOR)
    accounts.set_role(OPERATOR, "admin")
    _connect_claude(OPERATOR, role="admin", scope="shared")
    _expire_stored_token(OPERATOR)
    grant = provider_accounts.resolve_subscription_token(A, "anthropic")
    assert grant is not None
    assert grant["owner_email"] == OPERATOR
    assert grant["access_token"] == f"{CLAUDE_ACCESS}-refreshed-1"


def test_an_unrelated_provider_never_resolves(vendor: VendorStub) -> None:
    _seed(A)
    _connect_claude(A)
    assert provider_accounts.resolve_subscription_token(A, "openai") is None
    assert provider_accounts.resolve_subscription_token(A, "openrouter") is None
    assert provider_accounts.resolve_subscription_token(A, "") is None


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _expire_stored_token(email: str) -> None:
    with db.tx() as cur:
        cur.execute(
            "UPDATE provider_accounts SET access_expires_at = now() - interval '1 hour'"
        )
        cur.execute(
            "SELECT email, provider, account_key, encrypted_tokens FROM provider_accounts"
        )
        row = cur.fetchone()
        tokens = provider_accounts._open(
            row["email"], row["provider"], "tokens", row["encrypted_tokens"]
        )
        tokens["expires_at_ms"] = int(time.time() * 1000) - 60_000
        cur.execute(
            "UPDATE provider_accounts SET encrypted_tokens = %s WHERE account_key = %s",
            (
                provider_accounts._seal(row["email"], row["provider"], "tokens", tokens),
                row["account_key"],
            ),
        )


def test_an_expired_grant_is_refreshed_once_and_the_rotation_is_persisted(
    vendor: VendorStub,
) -> None:
    _seed(A)
    _connect_claude(A)
    _expire_stored_token(A)

    grant = provider_accounts.resolve_subscription_token(A, "anthropic")
    assert grant is not None
    assert grant["access_token"] == f"{CLAUDE_ACCESS}-refreshed-1"

    refreshes = [c for c in vendor.calls if c[1].get("grant_type") == "refresh_token"]
    assert len(refreshes) == 1
    assert refreshes[0][1]["refresh_token"] == CLAUDE_REFRESH

    # The second resolve is served from the freshly stored token: a rotating
    # refresh token must never be spent twice.
    again = provider_accounts.resolve_subscription_token(A, "anthropic")
    assert again is not None
    assert again["access_token"] == f"{CLAUDE_ACCESS}-refreshed-1"
    assert len([c for c in vendor.calls if c[1].get("grant_type") == "refresh_token"]) == 1


def test_a_dead_grant_yields_no_token_instead_of_breaking_the_turn(
    vendor: VendorStub,
) -> None:
    _seed(A)
    _connect_claude(A)
    _expire_stored_token(A)
    vendor.refresh_status = 400

    assert provider_accounts.resolve_subscription_token(A, "anthropic") is None
    # The row survives so the portal can still show it and offer a reconnect.
    assert len(_raw_rows("provider_accounts")) == 1
    listed = _client_for(A).get("/api/providers/accounts").json()
    assert listed["accounts"][0]["expired"] is True


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_missing_encryption_key_fails_closed_before_any_vendor_call(
    monkeypatch: pytest.MonkeyPatch, vendor: VendorStub
) -> None:
    _seed(A)
    monkeypatch.setattr(settings, "provider_key_encryption_key", "")
    response = _client_for(A).post("/api/providers/accounts/openai-codex/begin", json={})
    assert response.status_code == 503
    assert response.json()["detail"] == "subscription account storage is unavailable"
    assert vendor.calls == []


def test_an_unknown_provider_is_a_404_not_a_crash() -> None:
    _seed(A)
    client = _client_for(A)
    assert client.post("/api/providers/accounts/gemini-sub/begin", json={}).status_code == 404
    assert client.get("/api/providers/accounts").status_code == 200


def test_another_accounts_flow_id_cannot_be_completed(vendor: VendorStub) -> None:
    _seed(A)
    _seed(B)
    begin = _client_for(A).post("/api/providers/accounts/anthropic-sub/begin", json={}).json()

    hijack = _client_for(B).post(
        "/api/providers/accounts/anthropic-sub/complete",
        json={"flow_id": begin["flow_id"], "code": "code-1"},
    )
    assert hijack.status_code == 410
    assert _raw_rows("provider_accounts") == []


def test_unlinking_removes_the_row_and_stops_resolution(vendor: VendorStub) -> None:
    _seed(A)
    linked = _connect_claude(A)
    client = _client_for(A)

    removed = client.delete(f"/api/providers/accounts/anthropic-sub/{linked['account_key']}")
    assert removed.status_code == 200
    assert removed.json() == {"ok": True}
    assert _raw_rows("provider_accounts") == []
    assert provider_accounts.resolve_subscription_token(A, "anthropic") is None

    assert client.delete(
        f"/api/providers/accounts/anthropic-sub/{linked['account_key']}"
    ).status_code == 404


