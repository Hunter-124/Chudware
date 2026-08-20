from __future__ import annotations

import base64
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Webservice.gateway import auth, releases


@pytest.fixture
def manifest_row() -> dict[str, Any]:
    return {
        "version": "1.2.0",
        "min_supported": "1.0.0",
        "key_id": "release-v1",
        "sha256": "a" * 64,
        "size": 123456789,
        "signature": base64.b64encode(b"s" * 64).decode("ascii"),
        "notes_url": "https://chudware.com/releases/1.2.0",
        "artifact_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage",
        "payload_manifest_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.payload-manifest.v1.json",
        "payload_manifest_sha256": "b" * 64,
        "payload_signature_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.payload-manifest.v1.sig",
        "payload_signature_sha256": "c" * 64,
        "commit_sha": "d" * 40,
        "sbom_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.sbom.cdx.json",
        "sbom_sha256": "d" * 64,
        "sbom_signature_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.sbom.cdx.json.sig",
        "sbom_signature_sha256": "e" * 64,
        "provenance_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.provenance.slsa.json",
        "provenance_sha256": "f" * 64,
        "provenance_signature_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.provenance.slsa.json.sig",
        "provenance_signature_sha256": "1" * 64,
        "platform_test_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.platform-test.v1.json",
        "platform_test_sha256": "2" * 64,
        "platform_test_signature_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage.platform-test.v1.json.sig",
        "platform_test_signature_sha256": "3" * 64,
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]) -> TestClient:
    monkeypatch.setattr(releases, "_manifest_by_ip", releases.RateLimiter(1000, 60.0))
    monkeypatch.setattr(releases, "_fetch_latest", lambda *_: dict(manifest_row))
    app = FastAPI()
    app.include_router(releases.router)
    return TestClient(app)


def test_anonymous_and_bad_entitlement_receive_byte_identical_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: False)
    monkeypatch.setattr(
        releases,
        "_presigned_url",
        lambda _key: pytest.fail("anonymous or invalid callers must not mint a URL"),
    )
    query = "?channel=stable&platform=linux&arch=x86_64"
    anonymous = client.get("/api/releases/latest" + query)
    invalid = client.get(
        "/api/releases/latest" + query,
        headers={"X-Chudware-Entitlement": "forged.email@example.com.token"},
    )
    assert anonymous.status_code == invalid.status_code == 200
    assert anonymous.content == invalid.content
    assert "url" not in anonymous.json()
    assert set(anonymous.json()) == {
        "version", "min_supported", "key_id", "sha256", "size", "signature", "notes_url",
        "payload_manifest_key", "payload_manifest_sha256", "payload_signature_key",
        "payload_signature_sha256",
    }


def test_valid_entitlement_gets_fifteen_minute_presigned_download(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in releases._R2_ENV:
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(releases, "_active_entitlement", lambda token: token == "valid")
    monkeypatch.setattr(releases, "_presigned_url", lambda _key: "https://r2.example/signed")
    response = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64",
        headers={"Authorization": "Bearer valid"},
    )
    assert response.status_code == 200
    assert response.json()["url"] == "https://r2.example/signed"
    assert response.headers["cache-control"] == "private, no-store"


def test_authenticated_download_falls_back_to_this_host_when_r2_is_absent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No CDN credential is a supported deployment, not a broken one.

    Answering 503 here left a complete, correctly signed release undownloadable
    and every updater permanently stuck, so the gateway now points at its own
    artifact route instead.
    """
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: True)
    for name in releases._R2_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CHUDWARE_RELEASES_PUBLIC_BASE_URL", raising=False)
    response = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64",
        headers={"X-Chudware-Entitlement": "valid"},
    )
    assert response.status_code == 200
    assert response.json()["url"] == (
        "https://app.chudware.com/api/releases/artifact/"
        "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage"
    )
    assert response.headers["cache-control"] == "private, no-store"


def test_presigned_url_still_names_every_missing_r2_variable(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in releases._R2_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(Exception) as error:
        releases._presigned_url("stable/1.2.0/artifact")
    assert getattr(error.value, "status_code", None) == 503
    for name in releases._R2_ENV:
        assert name in error.value.detail


def test_publish_is_bearer_gated_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    statements: list[tuple[str, tuple[Any, ...]]] = []

    class Cursor:
        rowcount = 1

        def execute(self, statement: str, values: tuple[Any, ...]) -> None:
            statements.append((statement, values))

        def fetchone(self) -> dict[str, str | None]:
            return {"min_supported": None}

    @contextmanager
    def fake_tx():
        yield Cursor()

    monkeypatch.setattr(releases, "tx", fake_tx)
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    app = FastAPI()
    app.include_router(releases.router)
    publish_client = TestClient(app)
    payload = {
        "channel": "stable",
        "platform": "linux",
        "arch": "x86_64",
        **manifest_row,
    }
    assert publish_client.post("/api/releases/latest/publish", json=payload).status_code == 401
    response = publish_client.post(
        "/api/releases/latest/publish", json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["sbom_key"] == manifest_row["sbom_key"]
    assert len(statements) == 3
    assert "FOR UPDATE" in statements[0][0]
    assert "ON CONFLICT (channel, version, platform, arch) DO NOTHING" in statements[1][0]
    assert "release_channels" in statements[2][0]
    assert statements[1][1][5] == "release-v1"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("2.0.0", "1.99.99", True),
        ("2.10.0", "2.9.99", True),
        ("2.10.4", "2.10.3", True),
        ("2.9.0", "2.10.0", False),
    ],
)
def test_release_version_order_is_numeric(left: str, right: str, expected: bool) -> None:
    assert (releases._parse_release_version(left, "version") > releases._parse_release_version(right, "version")) is expected


@pytest.mark.parametrize("version", ["01.2.3", "1.02.3", "1.2.03", "1.2", "1.2.3-rc.1", "1.2.3+build", "v1.2.3"])
def test_release_version_rejects_noncanonical_and_prerelease(version: str) -> None:
    with pytest.raises(Exception) as error:
        releases._parse_release_version(version, "version")
    assert getattr(error.value, "status_code", None) == 422


def test_floor_update_locks_before_insert_and_rejects_numeric_rollback(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    statements: list[str] = []

    class Cursor:
        rowcount = 1

        def execute(self, statement: str, _values: tuple[Any, ...]) -> None:
            statements.append(statement)

        def fetchone(self) -> dict[str, str]:
            return {"min_supported": "2.10.0"}

    @contextmanager
    def fake_tx():
        yield Cursor()

    monkeypatch.setattr(releases, "tx", fake_tx)
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    payload = {"channel": "stable", "platform": "linux", "arch": "x86_64", **manifest_row, "min_supported": "2.9.0"}
    value = releases.PublishRelease(**payload)
    with pytest.raises(Exception) as error:
        releases._upsert_release(value)
    assert getattr(error.value, "status_code", None) == 409
    assert len(statements) == 1
    assert "FOR UPDATE" in statements[0]


def test_floor_update_accepts_2_10_after_2_9_under_same_lock(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    statements: list[str] = []

    class Cursor:
        rowcount = 1

        def execute(self, statement: str, _values: tuple[Any, ...]) -> None:
            statements.append(statement)

        def fetchone(self) -> dict[str, str]:
            return {"min_supported": "2.9.0"}

    @contextmanager
    def fake_tx():
        yield Cursor()

    monkeypatch.setattr(releases, "tx", fake_tx)
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    payload = {"channel": "stable", "platform": "linux", "arch": "x86_64", **manifest_row, "min_supported": "2.10.0"}
    releases._upsert_release(releases.PublishRelease(**payload))
    assert "FOR UPDATE" in statements[0]
    assert "INSERT INTO releases" in statements[1]
    assert "UPDATE release_channels" in statements[2]



def test_publish_rejects_noncanonical_artifact_key(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    app = FastAPI()
    app.include_router(releases.router)
    payload = {
        "channel": "stable",
        "platform": "linux",
        "arch": "x86_64",
        **manifest_row,
        "artifact_key": "stable/1.2.0/../../hostile.AppImage",
    }
    response = TestClient(app).post(
        "/api/releases/latest/publish",
        json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert "artifact_key" in response.json()["detail"]


def test_publish_rejects_signature_outside_artifact_domain(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {
        "channel": "stable",
        "platform": "linux",
        "arch": "x86_64",
        **manifest_row,
    }
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish",
        json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "signature does not match release identity and sha256"


def test_publish_rejects_unknown_release_key_id(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {
        "channel": "stable",
        "platform": "linux",
        "arch": "x86_64",
        **manifest_row,
        "key_id": "attacker-key",
    }
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish",
        json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "unknown release key_id"



@pytest.mark.parametrize("channel", ["stable", "beta"])
def test_publish_rejects_missing_payload_metadata(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any], channel: str
) -> None:
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {"channel": channel, "platform": "linux", "arch": "x86_64", **manifest_row}
    payload["artifact_key"] = payload["artifact_key"].replace("stable/", channel + "/", 1)
    for field in (
        "payload_manifest_key", "payload_manifest_sha256",
        "payload_signature_key", "payload_signature_sha256",
    ):
        payload.pop(field)
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish", json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422


def test_publish_rejects_identity_mismatched_payload_key(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {"channel": "stable", "platform": "linux", "arch": "x86_64", **manifest_row}
    payload["payload_manifest_key"] = "stable/9.9.9/other.payload-manifest.v1.json"
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish", json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert "payload_manifest_key" in response.json()["detail"]


def test_publish_rejects_noncanonical_payload_digest(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {"channel": "stable", "platform": "linux", "arch": "x86_64", **manifest_row}
    payload["payload_signature_sha256"] = "A" * 64
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post(
        "/api/releases/latest/publish", json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 422
    assert "payload_signature_sha256" in response.json()["detail"]

@pytest.mark.parametrize("field", [
    "sbom_key", "sbom_sha256", "sbom_signature_key", "sbom_signature_sha256",
    "provenance_key", "provenance_sha256", "provenance_signature_key", "provenance_signature_sha256",
    "platform_test_key", "platform_test_sha256", "platform_test_signature_key", "platform_test_signature_sha256",
])
def test_publish_rejects_missing_or_mismatched_evidence(monkeypatch, manifest_row, field):
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    monkeypatch.setenv("CHUDWARE_GATEWAY_RELEASE_TOKEN", "publish-secret")
    payload = {"channel": "stable", "platform": "linux", "arch": "x86_64", **manifest_row}
    if field.endswith("_key"):
        payload[field] = "stable/stale/hostile"
    else:
        payload.pop(field)
    app = FastAPI()
    app.include_router(releases.router)
    response = TestClient(app).post("/api/releases/latest/publish", json=payload, headers={"Authorization": "Bearer publish-secret"})
    assert response.status_code == 422


def test_only_publish_gate_updates_min_supported(monkeypatch, manifest_row):
    statements = []
    class Cursor:
        rowcount = 1

        def execute(self, statement, values):
            statements.append((statement, values))

        def fetchone(self):
            return {"min_supported": None}
    @contextmanager
    def fake_tx():
        yield Cursor()
    monkeypatch.setattr(releases, "tx", fake_tx)
    monkeypatch.setattr(releases, "_verify_artifact_signature", lambda *_: None)
    releases._upsert_release(releases.PublishRelease(channel="stable", platform="linux", arch="x86_64", **manifest_row))
    updates = [(sql, values) for sql, values in statements if sql.startswith("UPDATE release_channels")]
    assert len(updates) == 1
    assert updates[0][1] == (manifest_row["min_supported"], "stable")

def test_artifact_signature_preimage_is_domain_separated() -> None:
    digest = "a" * 64
    assert releases._artifact_signature_preimage(
        "release-v1", "1.2.3", "linux", "x86_64", digest
    ) == b"\0".join((
        b"chudware-artifact-v1", b"release-v1", b"1.2.3", b"linux",
        b"x86_64", digest.encode("ascii"),
    ))


def test_release_manifest_is_exactly_soft_auth_public() -> None:
    assert auth._is_public_path("/api/releases/latest")
    assert auth._is_public_path("/api/releases/latest/publish")
    assert not auth._is_public_path("/api/releases")
    assert not auth._is_public_path("/api/releases/latestly")


def test_anonymous_manifest_is_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(releases, "_manifest_by_ip", releases.RateLimiter(1, 60.0))
    query = "?channel=stable&platform=linux&arch=x86_64"
    assert client.get("/api/releases/latest" + query).status_code == 200
    response = client.get("/api/releases/latest" + query)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1
