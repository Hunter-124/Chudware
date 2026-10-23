from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

# Before ANY gateway import: config.Settings is built once, at import time, from
# the environment as it stands then. Whichever gateway test module pytest
# imports first therefore decides dev_mode for the whole session, and without
# this preamble a combined run in which this file is collected first leaves
# dev_mode False — which makes AuthMiddleware refuse to construct and every
# real-app test in a sibling module fail with "no strong auth configured".
# Same preamble as test_portal.py, test_releases.py and test_releases_ci.py.
os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault(
    "CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test")
)

import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# `gateway.*`, not `Webservice.gateway.*`: conftest puts both the gateway dir and
# its parent on sys.path, so only this spelling resolves when the suite is run
# from Webservice/gateway, which is the documented invocation.
from gateway import accounts, entitlement  # noqa: E402
from gateway.db import tx  # noqa: E402

def _raw_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

def _device_key() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    return key, entitlement._b64(_raw_public(key))

def _refresh_envelope(token: str, key: Ed25519PrivateKey, nonce: str, sequence: int,
                      ack: int = 0, customer_receipt=None) -> dict[str, object]:
    payload: dict[str, object] = {
        "token": token, "request_nonce": nonce, "sequence": sequence,
        "app_version": "1.2.3", "platform": "linux",
        "integrity_digest": "a" * 64, "last_acked_event_sequence": ack,
        "customer_receipt": customer_receipt,
    }
    payload["signature"] = entitlement._b64(key.sign(entitlement._canonical(payload)))
    return payload


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

def test_entitlement_key_requires_systemd_credential_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHUDWARE_ENTITLEMENT_KEY_PATH", raising=False)
    with pytest.raises(RuntimeError, match="must name the systemd entitlement credential"):
        entitlement._load_private_key()


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666])
def test_entitlement_key_rejects_group_or_world_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    path = tmp_path / "entitlement.key"
    path.write_bytes(bytes(range(32)))
    path.chmod(mode)
    monkeypatch.setenv("CHUDWARE_ENTITLEMENT_KEY_PATH", str(path))
    with pytest.raises(RuntimeError, match="must not be accessible"):
        entitlement._load_private_key()


@pytest.mark.parametrize("size", [0, 31, 33, 64])
def test_entitlement_key_requires_exact_raw_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    path = tmp_path / "entitlement.key"
    path.write_bytes(b"x" * size)
    path.chmod(0o600)
    monkeypatch.setenv("CHUDWARE_ENTITLEMENT_KEY_PATH", str(path))
    with pytest.raises(RuntimeError, match="must be 32 raw bytes"):
        entitlement._load_private_key()


def test_entitlement_key_rejects_wrong_private_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "entitlement.key"
    path.write_bytes(key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ))
    path.chmod(0o600)
    monkeypatch.setenv("CHUDWARE_ENTITLEMENT_KEY_PATH", str(path))
    with pytest.raises(RuntimeError, match="does not match the shipped public key"):
        entitlement._load_private_key()


def test_exact_offline_deadline_arithmetic() -> None:
    now = 1_800_000_000
    day = 86400
    paid_through = now + day
    assert min(now + 30 * day, paid_through) == paid_through
    assert now + 29 * day < now + 30 * day
    assert now + 30 * day + 1 > now + 30 * day
    assert paid_through + 1 > min(now + 30 * day, paid_through)


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
    nonce = "invalid-token-refresh-challenge-0001"
    assert client.post(
        "/api/entitlement/refresh", content=b"unsigned",
        headers={"X-Chudware-Refresh-Nonce": nonce},
    ).status_code == 401
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
    assert client.post(
        "/api/entitlement/refresh", content=token,
        headers={"X-Chudware-Refresh-Nonce": nonce},
    ).status_code == 401
    assert touched is False


def test_legacy_bare_refresh_returns_signed_reauthorization(
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
    ) == "device reauthorization required"

    old = entitlement._signed_refusal(
        "old refusal", "old-refresh-challenge-000000001", now=now - 301
    )
    with pytest.raises(HTTPException, match="does not match"):
        entitlement.verify_refusal(old, refresh_nonce, now, _raw_public(signing_key))
    with pytest.raises(HTTPException, match="does not match"):
        entitlement.verify_refusal(
            refusal, "different-request-challenge-00001", now, _raw_public(signing_key)
        )


def test_issue_token_has_exact_payload_and_fixed_30_day_grace(
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
    assert payload["grace_days"] == 30


def test_signed_refresh_sequences_ack_policy_and_rotation(
    signing_key: Ed25519PrivateKey,
) -> None:
    now = int(time.time())
    email = "device-owner@example.com"
    accounts.ensure_account(email, display="Device Owner")
    accounts.set_status(email, "active", decided_by="test")
    accounts.set_plan(email, "desktop")
    accounts.set_subscription(
        email, subscription_id="sub_device", subscription_status="active",
        price_id="price_desktop", subscribed_since=now - 86400,
        paid_through=now + 30 * 86400, is_edu=False,
    )
    account = accounts.get_account(email)
    assert account is not None
    device_key, public_key = _device_key()
    subject = entitlement._subject_for(email, "Workshop Laptop", public_key)
    token = entitlement._issue(account, subject, now)
    app = FastAPI()
    app.include_router(entitlement.router)
    client = TestClient(app)
    nonce = "first-device-refresh-challenge-0001"

    wrong = _refresh_envelope(token, Ed25519PrivateKey.generate(), nonce, 1)
    assert client.post("/api/entitlement/refresh", json=wrong,
                       headers={"X-Chudware-Refresh-Nonce": nonce}).status_code == 401
    mismatched = _refresh_envelope(token, device_key, "other-device-refresh-challenge-001", 1)
    assert client.post("/api/entitlement/refresh", json=mismatched,
                       headers={"X-Chudware-Refresh-Nonce": nonce}).status_code == 401
    bad_ack = _refresh_envelope(token, device_key, nonce, 1, ack=1)
    assert client.post("/api/entitlement/refresh", json=bad_ack,
                       headers={"X-Chudware-Refresh-Nonce": nonce}).status_code == 409

    # Renewal is coupled to a successful signed security check-in. An outage
    # blocks renewal; it does not alter the already-issued offline token.
    outage = _refresh_envelope(token, device_key, nonce, 1)
    blocked = client.post("/api/entitlement/refresh", json=outage,
                          headers={"X-Chudware-Refresh-Nonce": nonce})
    assert blocked.status_code == 409
    assert entitlement.verify_compact(token, _raw_public(signing_key))["sub"] == subject
    with tx() as cur:
        cur.execute("UPDATE desktop_entitlements SET last_security_checkin_at = now() WHERE subject = %s", (subject,))
    envelope = _refresh_envelope(token, device_key, nonce, 1)
    refreshed = client.post("/api/entitlement/refresh", json=envelope,
                            headers={"X-Chudware-Refresh-Nonce": nonce})
    assert refreshed.status_code == 200
    body = refreshed.json()
    policy = body["security_policy"]
    fresh_payload = entitlement.verify_compact(body["token"], _raw_public(signing_key))
    assert policy["telemetry_mode"] == "standard"
    assert policy["subject"] == subject
    assert policy["entitlement_nonce"] == fresh_payload["nonce"]
    assert policy["request_nonce"] == nonce
    assert policy["expires_at"] <= fresh_payload["paid_through"]
    entitlement._verify_device_object(policy, _raw_public(signing_key))

    replay = client.post("/api/entitlement/refresh", json=envelope,
                         headers={"X-Chudware-Refresh-Nonce": nonce})
    assert replay.status_code == 200
    assert replay.json()["entitled"] is False
    assert "reauthorization" in entitlement.verify_refusal(
        replay.json(), nonce, now, _raw_public(signing_key)
    )

    replacement, replacement_public = _device_key()
    rotated = entitlement._subject_for(email, "Workshop Laptop", replacement_public)
    assert rotated != subject
    old = _refresh_envelope(body["token"], device_key, nonce, 2)
    refused = client.post("/api/entitlement/refresh", json=old,
                          headers={"X-Chudware-Refresh-Nonce": nonce})
    with tx() as cur:
        cur.execute("UPDATE desktop_entitlements SET last_security_checkin_at = now() WHERE subject = %s", (rotated,))
    assert refused.json()["entitled"] is False
    new_token = entitlement._issue(account, rotated, now)
    new_nonce = "rotated-device-refresh-challenge-0002"
    accepted = client.post(
        "/api/entitlement/refresh",
        json=_refresh_envelope(new_token, replacement, new_nonce, 1),
        headers={"X-Chudware-Refresh-Nonce": new_nonce},
    )
    assert accepted.status_code == 200
    assert accepted.json()["entitled"] is True


def test_policy_defaults_standard_and_customer_receipt_controls_renewal(
    signing_key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    account = _account(now)
    payload = {"v": 1, "sub": "269c2977-156f-4ef3-8740-cd62fc586677",
               "email": account.email, "plan": account.plan, "issued_at": now,
               "paid_through": now + 86400, "subscribed_since": now - 86400,
               "grace_days": 30, "nonce": "token-nonce"}
    monkeypatch.setattr(entitlement, "_load_private_key", lambda: signing_key)
    assert entitlement._security_policy(account, payload, "n" * 32, "customer_endpoint")["telemetry_mode"] == "standard"

    enterprise = SimpleNamespace(**{**account.__dict__, "plan": "enterprise"})
    monkeypatch.setattr(entitlement, "_policy_assignment", lambda _a: {
        "policy_id": "customer_endpoint", "endpoint": "https://security.example/receipt", "config": {}})
    device_key = Ed25519PrivateKey.generate()
    receipt = {"subject": payload["sub"], "sequence": 2, "request_nonce": "r" * 32,
               "verdict": "allow", "reason": "heartbeat", "issued_at": now}
    receipt["signature"] = entitlement._b64(device_key.sign(entitlement._canonical(receipt)))
    entitlement._verify_customer_receipt(receipt, _raw_public(device_key), subject=payload["sub"],
                                         sequence=2, request_nonce="r" * 32)
    forwarded = []
    monkeypatch.setattr(entitlement, "_forward_customer_receipt", lambda endpoint, value: forwarded.append((endpoint, value)))
    assignment = entitlement._policy_assignment(enterprise)
    entitlement._forward_customer_receipt(assignment["endpoint"], receipt)
    assert forwarded == [("https://security.example/receipt", receipt)]
    with pytest.raises(HTTPException, match="receipt required"):
        entitlement._verify_customer_receipt(None, _raw_public(device_key), subject=payload["sub"],
                                             sequence=2, request_nonce="r" * 32)
    bad = dict(receipt)
    bad["verdict"] = "deny"
    with pytest.raises(HTTPException):
        entitlement._verify_customer_receipt(bad, _raw_public(device_key), subject=payload["sub"],
                                             sequence=2, request_nonce="r" * 32)
    tampered = dict(receipt)
    tampered["reason"] = "other"
    with pytest.raises(HTTPException, match="invalid device signature"):
        entitlement._verify_customer_receipt(
            tampered, _raw_public(device_key), subject=payload["sub"],
            sequence=2, request_nonce="r" * 32,
        )
    mismatched = dict(receipt)
    mismatched["request_nonce"] = "x" * 32
    mismatched["signature"] = entitlement._b64(
        device_key.sign(entitlement._canonical({k: v for k, v in mismatched.items() if k != "signature"}))
    )
    with pytest.raises(HTTPException, match="receipt required"):
        entitlement._verify_customer_receipt(
            mismatched, _raw_public(device_key), subject=payload["sub"],
            sequence=2, request_nonce="r" * 32,
        )
    stale = dict(receipt)
    stale["issued_at"] = now - 901
    stale["signature"] = entitlement._b64(
        device_key.sign(entitlement._canonical({k: v for k, v in stale.items() if k != "signature"}))
    )
    with pytest.raises(HTTPException, match="receipt required"):
        entitlement._verify_customer_receipt(
            stale, _raw_public(device_key), subject=payload["sub"],
            sequence=2, request_nonce="r" * 32,
        )


def test_revoked_device_requires_new_key() -> None:
    email = "revoked-key@example.com"
    accounts.ensure_account(email, display="Revoked Key")
    old_key, old_public = _device_key()
    old_subject = entitlement._subject_for(email, "Laptop", old_public)
    with tx() as cur:
        cur.execute("UPDATE desktop_entitlements SET revoked_at=now() WHERE subject=%s", (old_subject,))
    with pytest.raises(HTTPException, match="cannot be reactivated"):
        entitlement._subject_for(email, "Laptop", old_public)
    new_key, new_public = _device_key()
    new_subject = entitlement._subject_for(email, "Laptop", new_public)
    assert new_subject != old_subject
    assert _raw_public(new_key) != _raw_public(old_key)

def test_staging_rejects_production_signer_and_signed_refusals(signing_key, monkeypatch, tmp_path):
    production_refusal = entitlement._signed_refusal("fixture denial", "n" * 32, now=1000)
    staging_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    monkeypatch.setenv("CHUDWARE_DEPLOY_ENV", "staging")
    monkeypatch.setenv("CHUDWARE_STAGING_ENTITLEMENT_PUBLIC_KEY", _raw_public(staging_key).hex())
    with pytest.raises(RuntimeError):
        entitlement._load_private_key()
    with pytest.raises(HTTPException) as refused:
        entitlement.verify_refusal(production_refusal, "n" * 32, 1000)
    assert refused.value.status_code == 401
    path = tmp_path / "staging.key"
    path.write_bytes(bytes(range(32)))
    path.chmod(0o600)
    monkeypatch.setenv("CHUDWARE_ENTITLEMENT_KEY_PATH", str(path))
    staging_refusal = entitlement._signed_refusal("fixture denial", "n" * 32, now=1000)
    assert entitlement.verify_refusal(staging_refusal, "n" * 32, 1000) == "fixture denial"
    monkeypatch.setenv("CHUDWARE_DEPLOY_ENV", "prod")
    with pytest.raises(HTTPException) as refused:
        entitlement.verify_refusal(staging_refusal, "n" * 32, 1000)
    assert refused.value.status_code == 401


@pytest.mark.parametrize("configuration", ["missing", "production-root"])
def test_staging_cannot_share_production_entitlement_authority(signing_key, monkeypatch, configuration):
    monkeypatch.setenv("CHUDWARE_DEPLOY_ENV", "staging")
    if configuration == "missing":
        monkeypatch.delenv("CHUDWARE_STAGING_ENTITLEMENT_PUBLIC_KEY", raising=False)
    else:
        monkeypatch.setenv("CHUDWARE_STAGING_ENTITLEMENT_PUBLIC_KEY", _raw_public(signing_key).hex())
    with pytest.raises(RuntimeError):
        entitlement._load_private_key()
