"""Role-aware privacy coverage for the shared-host provider catalogue."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import providers  # noqa: E402


class _User:
    def __init__(self, role: str) -> None:
        self.email = f"{role}@example.com"
        self.display = role.title()
        self.role = role
        self.heavy = False


def _app_for(role: str) -> FastAPI:
    app = FastAPI()
    app.include_router(providers.router)

    class AttachUser:
        def __init__(self, app_: ASGIApp) -> None:
            self.app = app_

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                scope.setdefault("state", {})["user"] = _User(role)
            await self.app(scope, receive, send)

    app.add_middleware(AttachUser)
    return app


def _provider_state() -> dict[str, dict]:
    return {
        "codex": {
            "connected": True,
            "label": "owner@example.com (Primary)",
            "email": "owner@example.com",
            "account_id": "acct-owner-123",
            "accounts": [
                {
                    "account_id": "acct-owner-123",
                    "label": "owner@example.com",
                    "email": "owner@example.com",
                    "active": True,
                    "access_token": "host-account-token",
                }
            ],
            "refresh_token": "host-refresh-token",
        }
    }


def _configure_provider_data(monkeypatch) -> None:
    monkeypatch.setattr(
        providers,
        "_load_host_config",
        lambda: {
            "keys": {"openai": "host-api-key"},
            "provider": "codex-sub",
            "model": "privacy-model",
        },
    )
    monkeypatch.setattr(providers, "_subscription_status", _provider_state)
    monkeypatch.setitem(
        providers._MODEL_PRESETS,
        "codex-sub",
        [
            {
                "id": "privacy-model",
                "label": "Privacy Model",
                "metadata": {
                    "visible": "kept",
                    "credential": {"apiKey": "nested-api-key"},
                    "accessToken": "nested-access-token",
                },
            }
        ],
    )


def test_standard_user_sees_availability_and_models_without_account_identity(monkeypatch) -> None:
    _configure_provider_data(monkeypatch)

    with TestClient(_app_for("user")) as client:
        response = client.get("/api/providers")

    assert response.status_code == 200
    body = response.json()
    assert body["shared_host_accounts"] is True
    assert body["default_provider"] == "codex-sub"
    assert body["default_model"] == "privacy-model"
    assert {row["id"] for row in body["providers"]} == {"openai", "codex-sub"}

    subscription = next(row for row in body["providers"] if row["id"] == "codex-sub")
    assert set(subscription) == {"id", "name", "label", "auth", "models"}
    assert subscription["label"] == "OpenAI Codex (ChatGPT Plus/Pro)"
    assert subscription["models"] == [
        {
            "id": "privacy-model",
            "label": "Privacy Model",
            "metadata": {"visible": "kept"},
        }
    ]

    encoded = json.dumps(body)
    for private_value in (
        "owner@example.com",
        "acct-owner-123",
        "host-account-token",
        "host-refresh-token",
        "nested-api-key",
        "nested-access-token",
        "host-api-key",
    ):
        assert private_value not in encoded


def test_admin_keeps_scrubbed_operator_detail(monkeypatch) -> None:
    _configure_provider_data(monkeypatch)

    with TestClient(_app_for("admin")) as client:
        response = client.get("/api/providers")

    assert response.status_code == 200
    body = response.json()
    subscription = next(row for row in body["providers"] if row["id"] == "codex-sub")
    assert subscription["label"] == "owner@example.com (Primary)"
    assert subscription["email"] == "owner@example.com"
    assert subscription["account_id"] == "acct-owner-123"
    assert subscription["accounts"] == [
        {
            "account_id": "acct-owner-123",
            "label": "owner@example.com",
            "email": "owner@example.com",
            "active": True,
        }
    ]
    assert subscription["models"] == [
        {
            "id": "privacy-model",
            "label": "Privacy Model",
            "metadata": {"visible": "kept"},
        }
    ]

    encoded = json.dumps(body)
    for secret_value in (
        "host-api-key",
        "host-account-token",
        "host-refresh-token",
        "nested-api-key",
        "nested-access-token",
    ):
        assert secret_value not in encoded
