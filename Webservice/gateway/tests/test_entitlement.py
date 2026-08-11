from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from Webservice.gateway import accounts, entitlement


def _raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _account(now: int, *, active: bool = True):
    return SimpleNamespace(
        email="buyer@example.com",
        status="active" if active else "suspended",
        plan="desktop",
        subscription_status="active",
        paid_through=float(now + 40 * 86400),
        subscribed_since=float(now - 400 * 86400),
        trial_ends_at=None,
        created_at=float(now - 400 * 86400),
    )


@pytest.fixture
def signing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    path = tmp_path / "entitlement.key"
    path.write_bytes(raw)
    path.chmod(0o600)
    monkeypatch.setenv("CHUDWARE_ENTITLEMENT_KEY_PATH", str(path))
    monkeypatch.setattr(entitlement, "EMBEDDED_PUBLIC_KEY", _raw_public(key))
    return key


def test_grace_arithmetic_and_paid_through_clamp() -> None:
    now = 1_800_000_000
    assert entitlement.grace_days(now, now) == 7
    assert entitlement.grace_days(now, now - 400 * 86400) == 20
    assert entitlement.grace_days(now, now - 689 * 86400) == 29
    # 690 days is the first day the 30-day ceiling binds.
    assert entitlement.grace_days(now, now - 690 * 86400) == 30
    assert entitlement.grace_days(now, now - 5000 * 86400) == 30
    paid_through = now + 86400
    assert min(now + 30 * 86400, paid_through + 3 * 86400) == paid_through + 3 * 86400


def test_refresh_rejects_unsigned_and_wrong_key_before_account_lookup(
    signing_key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = FastAPI()
    app.include_router(entitlement.router)
    client = TestClient(app)
    touched = False

    def account_lookup(_email: str):
        nonlocal touched
        touched = True
        return None

    monkeypatch.setattr(entitlement.accounts, "get_account", account_lookup)
    assert client.post("/api/entitlement/refresh", content=b"unsigned").status_code == 401
    assert touched is False

    now = int(time.time())
    wrong = Ed25519PrivateKey.generate()
    payload = {
        "v": 1, "sub": "269c2977-156f-4ef3-8740-cd62fc586677",
        "email": "buyer@example.com", "plan": "desktop", "issued_at": now,
        "paid_through": now + 86400, "subscribed_since": now - 86400,
        "grace_days": 7, "nonce": "request-nonce",
    }
    token = entitlement._compact(payload, wrong)
    assert client.post("/api/entitlement/refresh", content=token).status_code == 401
    assert touched is False


def test_signed_refusal_is_immediate_and_old_refusal_cannot_replay(
    signing_key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    account = _account(now, active=False)
    subject = "269c2977-156f-4ef3-8740-cd62fc586677"
    payload = {
        "v": 1, "sub": subject, "email": account.email, "plan": "desktop",
        "issued_at": now, "paid_through": now + 86400,
        "subscribed_since": now - 400 * 86400, "grace_days": 30,
        "nonce": "current-request-nonce",
    }
    refresh_nonce = "fresh-request-challenge-0000000001"
    token = entitlement._compact(payload, signing_key)
    monkeypatch.setattr(entitlement.accounts, "get_account", lambda _email: account)
    monkeypatch.setattr(entitlement, "_subject_matches", lambda _email, _subject: True)

    app = FastAPI()
    app.include_router(entitlement.router)
    response = TestClient(app).post(
        "/api/entitlement/refresh", content=token,
        headers={"X-Chudware-Refresh-Nonce": refresh_nonce},
    )
    assert response.status_code == 200
    refusal = response.json()
    assert refusal["entitled"] is False
    assert entitlement.verify_refusal(
        refusal, refresh_nonce, now, _raw_public(signing_key)
    ) == "desktop entitlement is inactive"

    old = entitlement._signed_refusal(
        "old refusal", "old-refresh-challenge-000000001", now=now - 301
    )
    with pytest.raises(HTTPException, match="does not match"):
        entitlement.verify_refusal(old, refresh_nonce, now, _raw_public(signing_key))
    with pytest.raises(HTTPException, match="does not match"):
        entitlement.verify_refusal(
            refusal, "different-request-challenge-00001", now, _raw_public(signing_key)
        )


def test_issue_token_has_exact_payload_and_max_tenure_grace(
    signing_key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000
    account = _account(now)
    monkeypatch.setattr(entitlement, "_load_private_key", lambda: signing_key)
    token = entitlement._issue(account, "269c2977-156f-4ef3-8740-cd62fc586677", now)
    payload = entitlement.verify_compact(token, _raw_public(signing_key))
    assert set(payload) == {
        "v", "sub", "email", "plan", "issued_at", "paid_through",
        "subscribed_since", "grace_days", "nonce",
    }
    assert payload["grace_days"] == 20


def test_device_checkin_and_revocation_refuses_next_refresh(
    signing_key: Ed25519PrivateKey,
) -> None:
    now = int(time.time())
    email = "device-owner@example.com"
    accounts.ensure_account(email, display="Device Owner")
    accounts.set_status(email, "active", decided_by="test")
    accounts.set_plan(email, "desktop")
    accounts.set_subscription(
        email,
        subscription_id="sub_device",
        subscription_status="active",
        price_id="price_desktop",
        subscribed_since=now - 86400,
        paid_through=now + 30 * 86400,
        is_edu=False,
    )
    account = accounts.get_account(email)
    assert account is not None
    subject = entitlement._subject_for(email, "Workshop Laptop")
    token = entitlement._issue(account, subject, now)

    app = FastAPI()
    app.include_router(entitlement.router)
    client = TestClient(app)
    first_nonce = "first-device-refresh-challenge-0001"
    refreshed = client.post(
        "/api/entitlement/refresh",
        content=token,
        headers={"X-Chudware-Refresh-Nonce": first_nonce},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["entitled"] is True
    rows = entitlement.list_devices(email)
    assert len(rows) == 1
    assert rows[0]["device_name"] == "Workshop Laptop"
    assert rows[0]["last_checkin_at"] is not None

    assert entitlement.revoke_device(email, subject) is True
    second_nonce = "second-device-refresh-challenge-0002"
    refused = client.post(
        "/api/entitlement/refresh",
        content=refreshed.json()["token"],
        headers={"X-Chudware-Refresh-Nonce": second_nonce},
    )
    assert refused.status_code == 200
    assert refused.json()["entitled"] is False
    reason = entitlement.verify_refusal(
        refused.json(), second_nonce, now, _raw_public(signing_key)
    )
    assert "no longer valid" in reason
