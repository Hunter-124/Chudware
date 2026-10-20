"""Regression coverage for Access-provisioned pending accounts.

Cloudflare Access can prove an address and create the account row without going
through password registration.  That leaves no credentials row and, until a
user explicitly asks for one, no verification token or mail.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
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
import portal_serve  # noqa: E402
from config import settings  # noqa: E402

CLIENT = ("203.0.113.27", 44_321)


@pytest.fixture()
def client(pg, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://chudware.test")
    shipped = limits.login_by_ip
    fresh = limits.RateLimiter(shipped.limit, shipped.window)
    monkeypatch.setattr(authn, "login_by_ip", fresh)
    monkeypatch.setattr(mailer, "outbound", limits.RateLimiter(5, 3600.0))

    app = FastAPI()
    app.include_router(authn.router)
    # The pending page's resend handler is fetched over the public /auth-assets
    # route, which portal_serve owns. Without it the asset assertion below would
    # pass against a route that production does not actually serve.
    app.include_router(portal_serve.router)
    with TestClient(app, client=CLIENT, base_url="https://testserver") as test_client:
        yield test_client


def _counts(email: str) -> tuple[int, int]:
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM email_tokens WHERE email = %s", (email,))
        tokens = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM mail_outbox WHERE to_addr = %s", (email,))
        outbox = cur.fetchone()["n"]
    return tokens, outbox


def test_access_provisioned_account_can_resend_and_redeem_verification(client):
    email = "access-pending@example.com"
    account = accounts.ensure_account(email, display="Access User")
    assert account.status == "pending"
    assert authn.get_credential(email) is None
    assert _counts(email) == (0, 0)

    response = client.post("/api/auth/resend-verification", json={"email": email})
    assert response.status_code == 202
    assert _counts(email) == (1, 1)

    with db.tx() as cur:
        cur.execute(
            "SELECT template, ctx_json FROM mail_outbox WHERE to_addr = %s", (email,)
        )
        mail = cur.fetchone()
    assert mail["template"] == authn.PURPOSE_VERIFY
    verify_url = mailer.decrypt_context(email, mail["template"], mail["ctx_json"])["verify_url"]
    token = parse_qs(urlparse(verify_url).query)["token"][0]

    redeemed = client.get(
        f"/api/auth/verify?token={token}", follow_redirects=False
    )
    assert redeemed.status_code == 303
    assert "set-cookie" not in redeemed.headers
    assert authn.SESSION_COOKIE not in client.cookies
    assert accounts.get_account(email).status == "active"
    assert authn.get_credential(email) is None


def test_pending_page_does_not_claim_unsent_mail_exists(client):
    email = "nothing-sent@example.com"
    account = accounts.ensure_account(email, display="Access User")
    assert account.status == "pending"
    assert _counts(email) == (0, 0)

    page = auth.status_page_html(email, account.status)
    assert "We sent a confirmation link" not in page
    assert "Nothing in your inbox?" not in page
    assert "Use the button below to request a fresh verification link" in page
    assert 'data-email="nothing-sent@example.com"' in page
    asset_path = "/auth-assets/login-pending.650a485c0a6c.js"
    assert asset_path in page

    asset = client.get(asset_path)
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("application/javascript")
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
