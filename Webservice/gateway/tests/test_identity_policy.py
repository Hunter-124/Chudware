"""Account-scoped telemetry policies against the disposable PostgreSQL fixture."""
from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def policy_client(pg, monkeypatch):
    import accounts
    import auth
    import config
    import limits

    monkeypatch.setattr(config.settings, "dev_mode", True)
    limits.api_writes._hits.clear()
    for email in ("operator@example.test", "tenant-a@example.test", "tenant-b@example.test"):
        accounts.ensure_account(email)
        accounts.set_status(email, "active", decided_by="fixture")
        accounts.set_plan(email, "enterprise")
    accounts.set_role("operator@example.test", "admin")
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(accounts.router)
    with TestClient(app) as client:
        yield client


def test_customer_endpoint_is_isolated_on_assignment_read_and_signed_policy(
    policy_client, monkeypatch
):
    import accounts
    import entitlement

    headers = {"X-Dev-User": "operator@example.test"}
    endpoints = {"tenant-a@example.test": "https://a.example.test/receipt",
                 "tenant-b@example.test": "https://b.example.test/receipt"}
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(entitlement, "_load_private_key", lambda: key)
    for email, endpoint in endpoints.items():
        response = policy_client.put(f"/api/admin/accounts/{email}/security-policy",
                                     headers=headers,
                                     json={"policy": "customer_endpoint", "endpoint": endpoint})
        assert response.status_code == 200
    now = int(time.time())
    for email, endpoint in endpoints.items():
        response = policy_client.get(f"/api/admin/accounts/{email}/security-policy",
                                     headers=headers)
        assert response.status_code == 200
        assert response.json() == {"email": email, "policy": "customer_endpoint",
                                   "endpoint": endpoint}
        account = accounts.get_account(email)
        assert entitlement._policy_assignment(account) == {
            "policy_id": "customer_endpoint", "endpoint": endpoint, "config": {}}
        payload = {"sub": "device-fixture", "email": email, "nonce": "fixture-lease",
                   "paid_through": now + 86400, "issued_at": now, "grace_days": 30}
        signed = entitlement._security_policy(account, payload, "fixture-challenge",
                                              "customer_endpoint", now)
        assert signed["customer_endpoint"] == endpoint
        signature = entitlement._unb64(signed.pop("signature"))
        key.public_key().verify(signature, entitlement._canonical(signed))

    # Changing one tenant back to minimized neither changes the other tenant's
    # forwarding endpoint nor includes the old endpoint in the signed policy.
    email = "tenant-a@example.test"
    assert policy_client.put(f"/api/admin/accounts/{email}/security-policy",
                             headers=headers,
                             json={"policy": "minimized", "endpoint": None}).status_code == 200
    assert entitlement._policy_assignment(accounts.get_account(email)) == {
        "policy_id": "minimized", "endpoint": None, "config": {}}
    assert entitlement._policy_assignment(accounts.get_account("tenant-b@example.test"))["endpoint"] == endpoints["tenant-b@example.test"]
    payload = {"sub": "device-fixture", "email": email, "nonce": "fixture-lease",
               "paid_through": now + 86400, "issued_at": now, "grace_days": 30}
    signed = entitlement._security_policy(accounts.get_account(email), payload,
                                          "fixture-challenge", "customer_endpoint", now)
    assert signed["telemetry_mode"] == "minimized"
    assert "customer_endpoint" not in signed


def test_customer_cannot_provision_another_accounts_endpoint(policy_client):
    response = policy_client.put(
        "/api/admin/accounts/tenant-b@example.test/security-policy",
        headers={"X-Dev-User": "tenant-a@example.test"},
        json={"policy": "customer_endpoint", "endpoint": "https://a.example.test/receipt"},
    )
    assert response.status_code == 403
    import accounts
    import entitlement
    assert entitlement._policy_assignment(accounts.get_account("tenant-b@example.test")) == {
        "policy_id": "standard", "endpoint": None, "config": {}}
