"""Role-aware privacy coverage for the shared-host provider catalogue, plus the
invariant that binds it to the turn path: every subscription this catalogue
advertises must be one ``resolve_subscription_token`` can actually serve, and
every one it can serve must be advertised. A gap in either direction is a bug
users see as "I connected Claude and still cannot pick it".
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import accounts  # noqa: E402
import db  # noqa: E402
import provider_accounts  # noqa: E402
import providers  # noqa: E402
from config import settings  # noqa: E402

OPERATOR = "operator@example.com"


@pytest.fixture()
def encryption_secret(monkeypatch):
    """provider_accounts seals tokens on write, so a real row needs a real key."""
    monkeypatch.setattr(
        settings,
        "provider_key_encryption_key",
        base64.urlsafe_b64encode(b"S" * 32).decode().rstrip("="),
    )
    yield
    db.close_pool()


def _link(email: str, provider: str, scope: str, *, account_key: str) -> dict:
    """Persist one subscription link through the real writer.

    Going through ``_persist_account`` rather than raw SQL is deliberate: a test
    that hand-rolled the row could pass against a shape the sign-in flow never
    produces.
    """
    accounts.ensure_account(email, display=email.split("@", 1)[0].title())
    accounts.set_status(email, "active", decided_by="test")
    return provider_accounts._persist_account(
        email,
        provider,
        scope,
        {
            "account_key": account_key,
            "label": f"{account_key}@vendor.example",
            "plan": f"{account_key}-org-plan",
            "tokens": {
                "access_token": f"access-{account_key}-never-leak",
                "refresh_token": f"refresh-{account_key}-never-leak",
                "expires_at_ms": 4_000_000_000_000,
            },
        },
    )



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


def _no_host_subscriptions(monkeypatch) -> None:
    """Silence the host daemon's store so only provider_accounts can answer."""
    monkeypatch.setattr(providers, "_subscription_status", dict)
    monkeypatch.setattr(providers, "_load_host_config", dict)


def test_account_sub_wire_covers_every_storage_provider() -> None:
    """A new subscription provider must be classified, not silently unlisted.

    provider_accounts can store a link this catalogue has no wire id for, and
    the failure mode is silent: the link works, the resolver serves it, and no
    client is ever told it exists.
    """
    assert set(providers._ACCOUNT_SUB_WIRE) == set(
        provider_accounts.SUBSCRIPTION_PROVIDERS
    )
    assert set(providers._ACCOUNT_SUB_WIRE.values()) <= set(providers._SUB_LABELS)
    for wire in providers._ACCOUNT_SUB_WIRE.values():
        assert providers._MODEL_PRESETS.get(wire), f"{wire} would list zero models"


def test_personal_link_is_listed_to_its_owner_without_vendor_identity(
    monkeypatch, encryption_secret
) -> None:
    _no_host_subscriptions(monkeypatch)
    _link("user@example.com", "anthropic-sub", "personal", account_key="acct-mine")

    with TestClient(_app_for("user")) as client:
        body = client.get("/api/providers").json()

    entry = next(row for row in body["providers"] if row["id"] == "anthropic-sub")
    assert entry["auth"] == "subscription"
    assert entry["label"] == "Anthropic Claude (Pro/Max)"
    assert set(entry) == {"id", "name", "label", "auth", "models"}
    assert entry["models"] == providers._MODEL_PRESETS["anthropic-sub"]
    encoded = json.dumps(body)
    for private in ("acct-mine@vendor.example", "acct-mine-org-plan",
                    "access-acct-mine-never-leak", "refresh-acct-mine-never-leak"):
        assert private not in encoded


def test_shared_link_is_listed_to_a_user_who_does_not_own_it(
    monkeypatch, encryption_secret
) -> None:
    """The operator's shared pool is what the admin panel connects, and
    resolve_subscription_token serves it to every account — so every account
    has to be able to see it. This is the regression that made a Claude
    sign-in in the admin panel unusable: the row existed, the resolver would
    have spent it, and no picker ever offered it."""
    _no_host_subscriptions(monkeypatch)
    _link(OPERATOR, "anthropic-sub", "shared", account_key="acct-operator")
    accounts.ensure_account("user@example.com", display="User")

    with TestClient(_app_for("user")) as client:
        body = client.get("/api/providers").json()

    assert "anthropic-sub" in {row["id"] for row in body["providers"]}
    encoded = json.dumps(body)
    for private in (OPERATOR, "acct-operator@vendor.example",
                    "access-acct-operator-never-leak"):
        assert private not in encoded


def test_catalogue_matches_what_the_turn_resolver_can_serve(
    monkeypatch, encryption_secret
) -> None:
    """The invariant, stated directly against both implementations.

    ``resolve_subscription_token`` takes the wire name a turn selects; the
    catalogue publishes the wire id a client selects. For every storage
    provider, the two must agree on availability — otherwise a picker row is
    dead or a paid-for subscription is invisible.
    """
    _no_host_subscriptions(monkeypatch)
    caller = "user@example.com"
    accounts.ensure_account(caller, display="User")

    listed = {row["id"] for row in providers.list_available_providers(email=caller)["providers"]}
    assert listed == set()
    for storage_id in provider_accounts.SUBSCRIPTION_PROVIDERS:
        assert provider_accounts.resolve_subscription_token(caller, storage_id) is None

    _link(OPERATOR, "anthropic-sub", "shared", account_key="acct-operator")

    listed = {row["id"] for row in providers.list_available_providers(email=caller)["providers"]}
    assert listed == {"anthropic-sub"}
    for wire in provider_accounts.SUBSCRIPTION_PROVIDERS["anthropic-sub"].wire_names:
        grant = provider_accounts.resolve_subscription_token(caller, wire)
        assert grant is not None, f"listed anthropic-sub but wire {wire!r} resolves nothing"
        assert grant["access_token"] == "access-acct-operator-never-leak"
    assert provider_accounts.resolve_subscription_token(caller, "openai") is None


def test_an_unreachable_database_leaves_the_catalogue_serving(monkeypatch) -> None:
    """The endpoint must degrade, not 500: a broken store means "no account
    subscriptions", never a dead provider picker."""
    _no_host_subscriptions(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("store is down")

    monkeypatch.setattr(providers, "list_accounts", _boom)
    with TestClient(_app_for("user")) as client:
        response = client.get("/api/providers")
    assert response.status_code == 200
    assert response.json()["providers"] == []
