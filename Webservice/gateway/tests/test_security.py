from __future__ import annotations

import gzip
import json
import os
import time
import uuid
from pathlib import Path

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
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# `gateway.*`, not `Webservice.gateway.*`: conftest puts both the gateway dir and
# its parent on sys.path, so only this spelling resolves when the suite is run
# from Webservice/gateway, which is the documented invocation.
from gateway import accounts, entitlement, security  # noqa: E402
from gateway.db import tx  # noqa: E402


@pytest.fixture
def signing_key(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    path = tmp_path / "server.key"
    path.write_bytes(raw)
    path.chmod(0o600)
    monkeypatch.setenv("CHUDWARE_ENTITLEMENT_KEY_PATH", str(path))
    monkeypatch.setattr(entitlement, "EMBEDDED_PUBLIC_KEY", key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return key


@pytest.fixture
def client(signing_key):
    app = FastAPI()
    app.include_router(security.router)
    return TestClient(app)


def _device(email: str, name: str = "Laptop"):
    accounts.ensure_account(email, display=email)
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    subject = entitlement._subject_for(email, name, entitlement._b64(raw))
    return key, subject


def _batch(key, subject, sequence, reasons=(), *, policy="standard", batch_id=None):
    value = {
        "batch_id": str(batch_id or uuid.uuid4()), "subject": subject, "sequence": sequence,
        "policy_id": policy, "app_version": "1.2.3", "platform": "linux",
        "events": [{"reason": reason, "occurred_at": "2026-08-12T12:00:00Z"} for reason in reasons],
    }
    value["signature"] = entitlement._b64(key.sign(entitlement._canonical(value)))
    return value


def _post(client, batch, *, raw=None):
    body = raw if raw is not None else gzip.compress(json.dumps(batch).encode())
    return client.post("/api/security/events", content=body, headers={"Content-Encoding": "gzip"})


def test_valid_once_and_duplicate_idempotent(client):
    key, subject = _device("once@example.com")
    batch = _batch(key, subject, 1, ["heartbeat"])
    first = _post(client, batch)
    assert first.status_code == 200 and first.json()["status"] == "acknowledged"
    second = _post(client, batch)
    assert second.status_code == 200 and second.json()["sequence"] == 1
    with tx() as cur:
        cur.execute("SELECT count(*) AS n FROM security_event_batches WHERE subject=%s", (subject,))
        assert cur.fetchone()["n"] == 1


def test_bad_signature_policy_gzip_size_and_count_rejected(client):
    key, subject = _device("reject@example.com")
    bad = _batch(key, subject, 1)
    bad["signature"] = entitlement._b64(Ed25519PrivateKey.generate().sign(entitlement._canonical({k: v for k, v in bad.items() if k != "signature"})))
    assert _post(client, bad).status_code == 401
    wrong_policy = _batch(key, subject, 1, policy="minimized")
    assert _post(client, wrong_policy).status_code == 409
    assert _post(client, {}, raw=b"x" * (security.MAX_COMPRESSED_BYTES + 1)).status_code == 413
    too_many = _batch(key, subject, 1, ["heartbeat"] * 101)
    assert _post(client, too_many).status_code == 400


def test_rollback_reauthorizes_without_action(client):
    key, subject = _device("rollback@example.com")
    assert _post(client, _batch(key, subject, 2, ["heartbeat"])).status_code == 200
    rolled = _post(client, _batch(key, subject, 1, ["manifest_signature_invalid", "debugger_attached"]))
    assert rolled.status_code == 200 and rolled.json()["status"] == "reauthorize"
    with tx() as cur:
        cur.execute("SELECT count(*) AS n FROM security_actions WHERE subject=%s", (subject,))
        assert cur.fetchone()["n"] == 0


def test_one_debugger_no_sanction(client):
    key, subject = _device("debugger@example.com")
    response = _post(client, _batch(key, subject, 1, ["debugger_attached"]))
    assert response.status_code == 200 and response.json()["status"] == "acknowledged"
    with tx() as cur:
        cur.execute("SELECT revoked_at FROM desktop_entitlements WHERE subject=%s", (subject,))
        assert cur.fetchone()["revoked_at"] is None


def test_two_distinct_families_revoke_only_reporting_device(client):
    key1, subject1 = _device("shared@example.com", "One")
    _, subject2 = _device("shared@example.com", "Two")
    response = _post(client, _batch(key1, subject1, 1, ["protected_file_hash_mismatch", "debugger_attached"]))
    assert response.status_code == 200 and response.json()["status"] == "contained"
    with tx() as cur:
        cur.execute("SELECT subject::text AS subject, revoked_at FROM desktop_entitlements WHERE email=%s", ("shared@example.com",))
        rows = {row["subject"]: row["revoked_at"] for row in cur.fetchall()}
        assert rows[subject1] is not None
        assert rows[subject2] is None
        cur.execute("SELECT risk_score FROM security_actions WHERE subject=%s", (subject1,))
        assert cur.fetchone()["risk_score"] == 115


def test_long_reason_names_have_approved_weights(client):
    key, subject = _device("long-reasons@example.com")
    response = _post(client, _batch(key, subject, 1, [
        "protected_module_unlisted", "trace_environment_present",
    ]))
    assert response.status_code == 200
    assert response.json()["status"] == "acknowledged"
    assert response.json()["issued_at"].endswith("Z")
    with tx() as cur:
        cur.execute("SELECT risk_score FROM security_event_batches WHERE subject=%s", (subject,))
        assert cur.fetchone()["risk_score"] == 70


def test_minimized_persists_only_verdict_reason_and_time(client):
    key, subject = _device("minimized@example.com")
    with tx() as cur:
        cur.execute("UPDATE desktop_entitlements SET policy_id='minimized' WHERE subject=%s", (subject,))
    response = _post(client, _batch(key, subject, 1, ["debugger_attached"], policy="minimized"))
    assert response.status_code == 200
    with tx() as cur:
        cur.execute("SELECT app_version,platform,events,risk_score FROM security_event_batches WHERE subject=%s", (subject,))
        row = cur.fetchone()
    assert row["app_version"] == "acknowledged"
    assert row["platform"] == "debugger_attached"
    assert row["risk_score"] == 0
    assert row["events"] == [{"reason": "debugger_attached", "occurred_at": "2026-08-12T12:00:00+00:00"}]
