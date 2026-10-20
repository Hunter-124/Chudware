"""Web provider catalogue must agree with the credential source a turn can use."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway import accounts, db, provider_accounts, provider_keys, providers
from gateway.config import settings

OPERATOR = "operator@example.com"
CUSTOMER = "customer@example.com"


@pytest.fixture(autouse=True)
def encryption_secret(monkeypatch):
    monkeypatch.setattr(
        settings, "provider_key_encryption_key",
        base64.urlsafe_b64encode(b"S" * 32).decode().rstrip("="),
    )
    monkeypatch.delenv("CHUDWARE_WEB_POOL_API_KEY", raising=False)
    yield
    db.close_pool()


def _account(email: str, *, grant: bool = False) -> None:
    accounts.ensure_account(email, display=email.split("@", 1)[0].title())
    accounts.set_status(email, "active", decided_by="test")
    with db.tx() as cur:
        cur.execute("UPDATE accounts SET ai_grant = %s WHERE email = %s", (grant, email))


def _link(email: str, provider: str, scope: str, account_key: str) -> None:
    provider_accounts._persist_account(
        email, provider, scope,
        {
            "account_key": account_key,
            "label": f"{account_key}@vendor.example",
            "plan": "plus",
            "tokens": {
                "access_token": f"access-{account_key}-secret",
                "refresh_token": f"refresh-{account_key}-secret",
                "expires_at_ms": 4_000_000_000_000,
            },
        },
    )


def _client(email: str, role: str = "user") -> TestClient:
    app = FastAPI()
    app.include_router(providers.router)

    class AttachUser:
        def __init__(self, inner: ASGIApp) -> None:
            self.app = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                scope.setdefault("state", {})["user"] = SimpleNamespace(
                    email=email, display=email, role=role, heavy=False,
                )
            await self.app(scope, receive, send)

    app.add_middleware(AttachUser)
    return TestClient(app)


def test_live_chudcode_catalog_overrides_static_presets_without_waiting(monkeypatch) -> None:
    cli = {"models": [
        {"provider": "anthropic", "id": "claude-opus-6",
         "name": "Claude Opus 6"},
        {"provider": "anthropic", "id": "claude-opus-5-5",
         "name": "Claude Opus 5.5"},
    ]}
    catalog = providers._live_catalog_rows(cli, {})
    assert catalog["anthropic"][0]["id"] == "claude-opus-6"
    monkeypatch.setattr(providers, "_catalog_rows", catalog)
    monkeypatch.setattr(providers, "_catalog_next_refresh", float("inf"))
    assert providers.default_model_for("anthropic") == "claude-opus-6"
    assert providers.default_model_for("openai") == providers._MODEL_PRESETS["openai"][0]["id"]


def test_public_discovery_keeps_chat_models_and_drops_non_chat_capabilities() -> None:
    chat = {"name": "Claude Sonnet 5.5", "tool_call": True,
            "modalities": {"input": ["text", "image"], "output": ["text"]}}
    public = {
        "anthropic": {"models": {
            "claude-sonnet-5-5": chat,
            "claude-sonnet-5-6": {"name": "Claude Sonnet 5.6"},  # older feed: family fallback
            "claude-sonnet-5-7": {**chat, "tool_call": False},
            "claude-opus-6-image": chat,
            "claude-opus-6": {**chat, "modalities": {"output": ["image"]}},
            "claude-opus-7": {**chat, "deprecated": True},
        }},
        "openai": {"models": {
            "gpt-6-sol": {"tool_call": True, "modalities": {"output": ["text"]}},
            "gpt-6-audio-preview": {"tool_call": True, "modalities": {"output": ["text"]}},
            "gpt-image-1": {"tool_call": True, "modalities": {"output": ["image"]}},
            "text-embedding-4": {"tool_call": True, "modalities": {"output": ["text"]}},
            "gpt-6-moderation": {"tool_call": True, "modalities": {"output": ["text"]}},
        }},
        "google": {"models": {
            "gemini-3.5-flash": {"tool_call": True, "modalities": {"output": ["text"]}},
            "gemini-3.5-flash-live": {"tool_call": True, "modalities": {"output": ["text"]}},
        }},
        "unsupported-provider": {"models": {"chat-model-1": chat}},
    }
    cli = {"models": [
        {"provider": "anthropic", "id": "claude-opus-5-5", "name": "Claude Opus 5.5"},
        {"provider": "openai", "id": "gpt-6-tts", "name": "Speech synthesis"},
    ]}
    rows = providers._live_catalog_rows(cli, public)
    assert {row["id"] for row in rows["anthropic"]} == {
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-sonnet-5-6",
    }
    assert [row["id"] for row in rows["openai"]] == ["gpt-6-sol"]
    assert [row["id"] for row in rows["gemini"]] == ["gemini-3.5-flash"]
    assert "anthropic-sub" not in rows
    assert "unsupported-provider" not in rows


def test_ambient_host_credentials_are_never_web_availability(monkeypatch) -> None:
    _account(CUSTOMER)
    monkeypatch.setenv("OPENAI_API_KEY", "operator-secret")
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "operator-oauth-secret")
    for role in ("user", "admin"):
        with _client(CUSTOMER, role) as client:
            body = client.get("/api/providers").json()
        assert body == {
            "providers": [], "default_provider": None,
            "default_model": None, "shared_host_accounts": False,
        }
        assert "operator-secret" not in json.dumps(body)


def test_validated_byok_is_listed_only_to_its_owner() -> None:
    _account(CUSTOMER)
    _account(OPERATOR)
    provider_keys._persist_validated_key(CUSTOMER, "openai", "customer-key-secret")
    own = providers.list_available_providers(email=CUSTOMER)
    other = providers.list_available_providers(email=OPERATOR)
    assert [(row["id"], row["auth"]) for row in own["providers"]] == [("openai", "api_key")]
    assert other["providers"] == []
    assert provider_keys.resolve_validated_key(CUSTOMER, "openai") == "customer-key-secret"
    assert "customer-key-secret" not in json.dumps(own)


def test_personal_and_shared_subscriptions_are_not_advertised() -> None:
    _account(CUSTOMER)
    _account(OPERATOR)
    _link(OPERATOR, "anthropic-sub", "shared", "operator-claude")
    assert providers.list_available_providers(email=CUSTOMER)["providers"] == []
    assert provider_accounts.resolve_subscription_token(CUSTOMER, "anthropic") is None

    _link(CUSTOMER, "anthropic-sub", "personal", "customer-claude")
    _link(CUSTOMER, "openai-codex", "personal", "customer-codex")
    listed = providers.list_available_providers(email=CUSTOMER)
    assert listed["providers"] == []
    assert provider_accounts.resolve_subscription_token(CUSTOMER, "anthropic")["access_token"] == (
        "access-customer-claude-secret"
    )
    assert "customer-claude" not in json.dumps(listed)
    assert "customer-codex" not in json.dumps(listed)


def test_verified_trial_subscription_is_still_unavailable_to_cells() -> None:
    trial_user = "student@dept.example.com"
    _account(trial_user)
    _account(OPERATOR)
    accounts.set_role(OPERATOR, "admin")
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO credentials(email, password_hash, email_verified) "
            "VALUES (%s, 'unused', TRUE)", (trial_user,),
        )
        cur.execute(
            "INSERT INTO trial_domains(domain, trial_days, ai_credits) "
            "VALUES ('example.com', 14, 0)"
        )
    _link(OPERATOR, "anthropic-sub", "personal", "admin-claude")
    with _client(trial_user) as client:
        listed = client.get("/api/providers").json()
    assert listed["providers"] == []
    assert "access-admin-claude-secret" not in json.dumps(listed)


def test_pool_never_enters_catalogue_even_with_grant_and_credentials(monkeypatch) -> None:
    _account(CUSTOMER, grant=True)
    _account(OPERATOR, grant=False)
    assert providers.list_available_providers(email=CUSTOMER)["providers"] == []
    monkeypatch.setenv("CHUDWARE_WEB_POOL_API_KEY", "operator-pool-secret")
    monkeypatch.setenv("CHUDWARE_WEB_POOL_PROVIDER", "openrouter")
    monkeypatch.setenv("CHUDWARE_WEB_POOL_MODEL", "vendor/chosen-model")
    listed = providers.list_available_providers(email=CUSTOMER)
    assert listed["providers"] == []
    assert listed["default_model"] is None
    assert providers.list_available_providers(email=OPERATOR)["providers"] == []
    assert "operator-pool-secret" not in json.dumps(listed)

    provider_keys._persist_validated_key(CUSTOMER, "openrouter", "customer-key")
    own = providers.list_available_providers(email=CUSTOMER)
    assert [(row["id"], row["auth"]) for row in own["providers"]] == [("openrouter", "api_key")]


def test_subscription_store_is_not_consulted_for_cell_availability(monkeypatch) -> None:
    _account(CUSTOMER)
    def unavailable(*_args):
        raise RuntimeError("store down")
    monkeypatch.setattr(provider_accounts, "resolve_subscription_token", unavailable)
    with _client(CUSTOMER) as client:
        response = client.get("/api/providers")
    assert response.status_code == 200
    assert response.json()["providers"] == []
