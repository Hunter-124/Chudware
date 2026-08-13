from __future__ import annotations

import os
import uuid
from pathlib import Path

os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from gateway import accounts, app as app_mod, appeals, db, entitlement
from gateway.config import settings
from gateway.limits import RateLimiter


def _account(email: str, *, admin: bool = False) -> None:
    accounts.ensure_account(email, display=email)
    accounts.set_status(email, "active", decided_by="test")
    accounts.set_plan(email, "desktop")
    if admin:
        with db.tx() as cur:
            cur.execute("UPDATE accounts SET role='admin' WHERE email=%s", (email,))


def _client(monkeypatch, email: str) -> TestClient:
    monkeypatch.setattr(settings, "dev_mode", True)
    return TestClient(app_mod.app, headers={"X-Dev-User": email})


def _contained(email: str, name: str = "Workstation") -> tuple[str, bytes, uuid.UUID]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    subject = entitlement._subject_for(email, name, entitlement._b64(public))
    action_id = uuid.uuid4()
    with db.tx() as cur:
        cur.execute("UPDATE desktop_entitlements SET revoked_at=now() WHERE email=%s AND subject=%s", (email, subject))
        cur.execute(
            "INSERT INTO security_actions (id,email,subject,action,reason_codes,risk_score,source_batches) VALUES (%s,%s,%s,'revoke_device',%s,135,%s)",
            (action_id, email, subject, ["file_hash_mismatch", "debugger_attached"], [uuid.uuid4()]),
        )
    return subject, public, action_id


def test_customer_create_list_duplicate_and_cross_tenant_404(pg, monkeypatch):
    owner, other = "appeal-owner@example.com", "appeal-other@example.com"
    _account(owner)
    _account(other)
    subject, _, _ = _contained(owner)
    owner_client = _client(monkeypatch, owner)
    other_client = _client(monkeypatch, other)

    foreign = other_client.post("/api/portal/security-appeals", json={"subject": subject, "message": "Please review."})
    assert foreign.status_code == 404
    created = owner_client.post("/api/portal/security-appeals", json={"subject": subject, "message": "False positive on my workstation."})
    assert created.status_code == 200
    appeal = created.json()["appeal"]
    assert appeal["subject"] == subject and appeal["state"] == "pending"
    assert appeal["reason_codes"] == ["file_hash_mismatch", "debugger_attached"]
    assert "risk_score" not in created.text and "source_batches" not in created.text
    duplicate = owner_client.post("/api/portal/security-appeals", json={"subject": subject, "message": "Again."})
    assert duplicate.status_code == 409
    listed = owner_client.get("/api/portal/security-appeals")
    assert listed.status_code == 200 and [row["id"] for row in listed.json()["appeals"]] == [appeal["id"]]
    assert other_client.get("/api/portal/security-appeals").json() == {"appeals": []}


def test_restore_audits_but_never_reactivates_old_key(pg, monkeypatch):
    owner, admin = "restore-owner@example.com", "appeals-admin@example.com"
    _account(owner)
    _account(admin, admin=True)
    subject, old_key, action_id = _contained(owner)
    created = _client(monkeypatch, owner).post(
        "/api/portal/security-appeals", json={"subject": subject, "message": "Please restore access."}
    ).json()["appeal"]
    resolved = _client(monkeypatch, admin).post(
        f"/api/admin/security/appeals/{created['id']}/resolve",
        json={"decision": "restore_device", "note": "Reviewed and restored."},
    )
    assert resolved.status_code == 200
    body = resolved.json()["appeal"]
    assert body["state"] == "restored" and body["decided_by"] == admin
    assert body["resolved_at"] is not None and body["decision_note"] == "Reviewed and restored."
    with db.tx() as cur:
        cur.execute("SELECT reversed_at,reversed_by FROM security_actions WHERE id=%s", (action_id,))
        action = cur.fetchone()
        cur.execute("SELECT revoked_at,device_public_key FROM desktop_entitlements WHERE subject=%s", (subject,))
        device = cur.fetchone()
    assert action["reversed_at"] is not None and action["reversed_by"] == admin
    assert device["revoked_at"] is not None and bytes(device["device_public_key"]) == old_key
    assert entitlement._subject_matches(owner, subject) is False

    new_private = Ed25519PrivateKey.generate()
    new_key = new_private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    rotated = entitlement._subject_for(owner, "Workstation", entitlement._b64(new_key))
    assert rotated != subject and entitlement._subject_matches(owner, rotated) is True
    with db.tx() as cur:
        cur.execute("SELECT device_public_key FROM desktop_entitlements WHERE subject=%s", (rotated,))
        assert bytes(cur.fetchone()["device_public_key"]) == new_key
    customer_history = _client(monkeypatch, owner).get("/api/portal/security-appeals").json()["appeals"]
    admin_history = _client(monkeypatch, admin).get("/api/admin/security/appeals").json()["appeals"]
    assert customer_history[0]["id"] == created["id"]
    assert admin_history[0]["id"] == created["id"]


def test_uphold_records_decision_without_reversal(pg, monkeypatch):
    owner, admin = "uphold-owner@example.com", "uphold-admin@example.com"
    _account(owner)
    _account(admin, admin=True)
    subject, _, action_id = _contained(owner)
    appeal = _client(monkeypatch, owner).post(
        "/api/portal/security-appeals", json={"subject": subject, "message": "Review this action."}
    ).json()["appeal"]
    admin_client = _client(monkeypatch, admin)
    response = admin_client.post(
        f"/api/admin/security/appeals/{appeal['id']}/resolve",
        json={"decision": "uphold", "note": "Action was supported."},
    )
    assert response.status_code == 200 and response.json()["appeal"]["state"] == "upheld"
    assert admin_client.get("/api/admin/security/appeals?state=upheld").json()["total"] == 1
    with db.tx() as cur:
        cur.execute("SELECT reversed_at,reversed_by FROM security_actions WHERE id=%s", (action_id,))
        action = cur.fetchone()
        cur.execute("SELECT revoked_at FROM desktop_entitlements WHERE subject=%s", (subject,))
        device = cur.fetchone()
    assert action["reversed_at"] is None and action["reversed_by"] is None
    assert device["revoked_at"] is not None


def test_validation_and_api_write_rate_limit(pg, monkeypatch):
    owner = "validation-owner@example.com"
    _account(owner)
    subject, _, _ = _contained(owner)
    client = _client(monkeypatch, owner)
    assert client.post("/api/portal/security-appeals", json={"subject": subject, "message": ""}).status_code == 400
    assert client.post("/api/portal/security-appeals", json={"subject": subject, "message": "é" * 2049}).status_code == 413
    limited = RateLimiter(0, 60)
    monkeypatch.setattr(appeals, "api_writes", limited)
    throttled = client.post("/api/portal/security-appeals", json={"subject": subject, "message": "Valid."})
    assert throttled.status_code == 429 and "Retry-After" in throttled.headers
