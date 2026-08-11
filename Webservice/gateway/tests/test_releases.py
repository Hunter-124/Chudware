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
        "sha256": "a" * 64,
        "size": 123456789,
        "signature": base64.b64encode(b"s" * 64).decode("ascii"),
        "notes_url": "https://chudware.com/releases/1.2.0",
        "artifact_key": "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage",
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
        "version", "min_supported", "sha256", "size", "signature", "notes_url"
    }


def test_valid_entitlement_gets_fifteen_minute_presigned_download(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(releases, "_active_entitlement", lambda token: token == "valid")
    monkeypatch.setattr(releases, "_presigned_url", lambda _key: "https://r2.example/signed")
    response = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64",
        headers={"Authorization": "Bearer valid"},
    )
    assert response.status_code == 200
    assert response.json()["url"] == "https://r2.example/signed"
    assert response.headers["cache-control"] == "private, no-store"


def test_authenticated_download_fails_actionably_when_r2_is_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(releases, "_active_entitlement", lambda _token: True)
    for name in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    response = client.get(
        "/api/releases/latest?channel=stable&platform=linux&arch=x86_64",
        headers={"X-Chudware-Entitlement": "valid"},
    )
    assert response.status_code == 503
    assert "R2_ENDPOINT" in response.json()["detail"]
    assert "R2_ACCESS_KEY_ID" in response.json()["detail"]
    assert "R2_SECRET_ACCESS_KEY" in response.json()["detail"]


def test_publish_is_bearer_gated_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
    statements: list[tuple[str, tuple[Any, ...]]] = []

    class Cursor:
        def execute(self, statement: str, values: tuple[Any, ...]) -> None:
            statements.append((statement, values))

    @contextmanager
    def fake_tx():
        yield Cursor()

    monkeypatch.setattr(releases, "tx", fake_tx)
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
    payload.pop("artifact_key")
    payload["artifact_key"] = "stable/1.2.0/Chudware-1.2.0-linux-x86_64.AppImage"

    assert publish_client.post("/api/releases/latest/publish", json=payload).status_code == 401
    response = publish_client.post(
        "/api/releases/latest/publish",
        json=payload,
        headers={"Authorization": "Bearer publish-secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(statements) == 1
    assert "ON CONFLICT (channel, version, platform, arch) DO UPDATE" in statements[0][0]


def test_publish_rejects_noncanonical_artifact_key(
    monkeypatch: pytest.MonkeyPatch, manifest_row: dict[str, Any]
) -> None:
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
