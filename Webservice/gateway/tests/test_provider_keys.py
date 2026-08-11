"""Security and billing contracts for account-scoped web BYOK credentials."""

from __future__ import annotations

import base64
import builtins
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway import accounts, app as app_mod, credits, db, engine_bridge, provider_keys
from gateway.config import settings
from gateway.limits import RateLimiter
from gateway.tests.test_engine_bridge import FakeDaemon, _rpc
from services.agent import config as agent_config, worker_rpc

SECRET = "sk-account-secret-never-leak-1234"
A = "alice@example.com"
B = "bob@example.com"


def _seed(email: str, *, plan: str = "studio", ai_grant: bool = True) -> None:
    accounts.ensure_account(email, display=email.split("@", 1)[0].title())
    accounts.set_status(email, "active", decided_by="test")
    with db.tx() as cur:
        cur.execute(
            "UPDATE accounts SET plan = %s, ai_grant = %s WHERE email = %s",
            (plan, ai_grant, email),
        )


def _client_for(email: str) -> TestClient:
    app = FastAPI()
    app.include_router(provider_keys.router)

    class AttachUser:
        def __init__(self, inner: ASGIApp) -> None:
            self.app = inner

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                scope.setdefault("state", {})["user"] = SimpleNamespace(
                    email=email, display=email, role="user", heavy=False
                )
            await self.app(scope, receive, send)

    app.add_middleware(AttachUser)
    return TestClient(app)


@pytest.fixture(autouse=True)
def encryption_secret(monkeypatch: pytest.MonkeyPatch):
    encoded = base64.urlsafe_b64encode(b"K" * 32).decode().rstrip("=")
    monkeypatch.setattr(settings, "provider_key_encryption_key", encoded)
    monkeypatch.setenv("CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY", encoded)
    monkeypatch.setattr(
        provider_keys, "provider_key_validations", RateLimiter(5, 60.0)
    )
    agent_config.clear_pooled_credentials()
    yield
    agent_config.clear_pooled_credentials()
    db.close_pool()


@pytest.fixture
def accept_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def accept(_provider: str, _key: str) -> None:
        return None

    monkeypatch.setattr(provider_keys, "validate_key", accept)


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = FakeDaemon(tmp_path / "webd.sock")
    fake.start()
    monkeypatch.setattr(settings, "webd_socket", fake.sock_path)
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(settings, "share_provider_keys", False)
    yield fake
    fake.stop()


def _raw_provider_row(email: str, provider: str = "openai"):
    with db.tx() as cur:
        cur.execute(
            "SELECT * FROM provider_keys WHERE email = %s AND provider = %s",
            (email, provider),
        )
        return cur.fetchone()


def _balance_and_ledger(email: str) -> tuple[int, list[dict]]:
    with db.tx() as cur:
        cur.execute("SELECT credit_balance FROM accounts WHERE email = %s", (email,))
        balance = int(cur.fetchone()["credit_balance"])
        cur.execute(
            "SELECT kind, credits_delta, model, note FROM credit_ledger "
            "WHERE email = %s ORDER BY id",
            (email,),
        )
        return balance, list(cur.fetchall())


def test_tenancy_at_rest_round_trip_and_no_leakage(
    accept_validation: None, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(A)
    _seed(B)
    alice = _client_for(A)
    bob = _client_for(B)

    caplog.set_level(logging.DEBUG)
    add = alice.post("/api/providers/keys", json={"provider": "openai", "key": SECRET})
    assert add.status_code == 200
    assert add.json()["hint"] == "1234"
    assert SECRET not in add.text

    row = _raw_provider_row(A)
    assert row is not None
    stored = bytes(row["encrypted_key"])
    assert SECRET.encode() not in stored
    assert stored != SECRET.encode()
    assert provider_keys.decrypt_key(A, "openai", stored) == SECRET

    # B cannot enumerate, resolve/use, or delete A's row. The failed delete is
    # indistinguishable from there being no such provider at all for B.
    assert bob.get("/api/providers/keys").json() == {"keys": []}
    assert provider_keys.resolve_validated_key(B, "openai") is None
    bob_delete = bob.delete("/api/providers/keys/openai")
    assert bob_delete.status_code == 404
    assert _raw_provider_row(A) is not None

    byok_b = agent_config.Actor(B, "studio", 100, "byok")
    with pytest.raises(ValueError, match="BYOK provider credential unavailable"):
        agent_config.get_provider("openai", {"provider": "openai"}, actor=byok_b)

    captured: dict = {}

    class Provider:
        name = "openai"
        model = "test"

    def resolve(_name, cfg, role=None):
        captured.update(cfg)
        return Provider()

    original = agent_config._resolve_provider
    agent_config._resolve_provider = resolve
    try:
        byok_a = agent_config.Actor(A, "studio", 100, "byok")
        gateway_key = provider_keys.resolve_validated_key(A, "openai")
        agent_config.get_provider(
            "openai",
            {
                "provider": "openai",
                "api_key": "ambient-must-not-win",
                "_chudware_pool_api_key": gateway_key,
            },
            actor=byok_a,
        )
    finally:
        agent_config._resolve_provider = original
    assert captured["api_key"] == SECRET
    assert provider_keys.list_keys(A)[0]["last_used_at"] is not None

    listed = alice.get("/api/providers/keys")
    assert listed.status_code == 200
    assert listed.json()["keys"][0]["hint"] == "1234"
    assert SECRET not in listed.text
    deleted = alice.delete("/api/providers/keys/openai")
    assert deleted.json() == {"ok": True}
    assert SECRET not in deleted.text
    assert _raw_provider_row(A) is None

    response_corpus = add.text + bob_delete.text + listed.text + deleted.text
    assert SECRET not in response_corpus
    assert SECRET not in caplog.text


def test_bad_key_surfaces_provider_error_and_is_not_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(A)
    actual = '{"error":{"message":"Incorrect API key provided"}}'

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str, *, headers: dict[str, str]):
            assert SECRET in headers["Authorization"]
            return httpx.Response(
                401,
                text=actual,
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(provider_keys.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    response = _client_for(A).post(
        "/api/providers/keys", json={"provider": "openai", "key": SECRET}
    )
    assert response.status_code == 400
    assert "Incorrect API key provided" in response.json()["detail"]
    assert SECRET not in response.text
    assert _raw_provider_row(A) is None


def test_validation_endpoint_is_rate_limited(
    accept_validation: None,
) -> None:
    _seed(A)
    client = _client_for(A)
    for index in range(5):
        response = client.post(
            "/api/providers/keys",
            json={"provider": "openai", "key": f"sk-valid-{index:04d}"},
        )
        assert response.status_code == 200
    limited = client.post(
        "/api/providers/keys", json={"provider": "openai", "key": "sk-sixth"}
    )
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_worker_clears_byok_key_between_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent import mission_dispatch

    captured: list[str] = []

    class Provider:
        name = "openai"
        model = "test"

    def resolve(_name, cfg, role=None):
        captured.append(str(cfg.get("api_key") or ""))
        return Provider()

    monkeypatch.setattr(agent_config, "_resolve_provider", resolve)
    monkeypatch.setattr(agent_config, "load_config", lambda: {
        "provider": "openai",
        "api_key": "ambient-desktop-key-must-not-win",
    })
    monkeypatch.setattr(
        mission_dispatch,
        "run_linear_role_turn",
        lambda *_args, **_kwargs: {
            "success": True,
            "stop_reason": "confirmed",
            "result": {},
        },
    )
    actor_a = {
        "email": A,
        "plan": "studio",
        "credits_remaining": 100,
        "key_source": "byok",
    }
    first = worker_rpc._agent_admin_turn(
        {
            "request": "first tenant",
            "provider": "openai",
            "api_key": SECRET,
            "_chudware_actor": actor_a,
        },
        lambda _event: None,
    )
    assert first["ok"] is True
    assert captured == [SECRET]

    actor_b = {**actor_a, "email": B}
    second = worker_rpc._agent_admin_turn(
        {
            "request": "unrelated next tenant",
            "provider": "openai",
            "_chudware_actor": actor_b,
        },
        lambda _event: None,
    )
    assert second["ok"] is False
    assert "BYOK provider credential unavailable" in second["reason"]
    assert captured == [SECRET]


def test_desktop_turn_never_imports_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent import mission_dispatch

    captured: dict = {}

    class Provider:
        name = "openai"
        model = "desktop-model"

    def resolve(_name, cfg, role=None):
        captured.update(cfg)
        return Provider()

    monkeypatch.setattr(agent_config, "_resolve_provider", resolve)
    monkeypatch.setattr(
        agent_config,
        "load_config",
        lambda: {"provider": "openai", "api_key": "desktop-owned-key"},
    )
    monkeypatch.setattr(
        mission_dispatch,
        "run_linear_role_turn",
        lambda *_args, **_kwargs: {
            "success": True,
            "stop_reason": "confirmed",
            "result": {},
        },
    )
    real_import = builtins.__import__

    def no_gateway(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "gateway" or name.startswith(("gateway.", "Webservice.gateway")):
            raise AssertionError(f"desktop imported forbidden module {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_gateway)
    result = worker_rpc._agent_admin_turn(
        {
            "request": "desktop-shaped turn",
            "provider": "openai",
            "api_key": "desktop-owned-key",
        },
        lambda _event: None,
    )
    assert result["ok"] is True
    assert captured["api_key"] == "desktop-owned-key"


def test_byok_precedes_credits_for_x_and_y_still_pools(
    daemon: FakeDaemon, accept_validation: None
) -> None:
    email = "demo@example.com"
    _seed(email, ai_grant=True)
    client = _client_for(email)
    assert client.post(
        "/api/providers/keys", json={"provider": "openai", "key": SECRET}
    ).status_code == 200

    credits.grant(email, 10, note="BYOK precedence test")
    credits.refresh_model_rates(
        lambda _url: {
            "data": [{
                "id": "vendor/cheap",
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            }]
        },
        now=1_800_000_000,
    )
    before_byok = _balance_and_ledger(email)

    with TestClient(app_mod.app).websocket_connect(
        "/engine", headers={"X-Dev-User": email}
    ) as ws:
        _rpc(ws, 1, "hello")
        byok = _rpc(ws, 2, "agent_ask_v2", {
            "question": "BYOK X",
            "provider": "openai",
            "model": "vendor/cheap",
            "api_key": "browser-forgery-must-be-stripped",
            "_test_emit_usage": True,
        })
    assert byok["result"]["ok"] is True
    assert _balance_and_ledger(email) == before_byok
    byok_call = [c for c in daemon.calls if c.get("method") == "agent_ask_v2"][-1]
    byok_actor = byok_call["params"]["options"]["_chudware_actor"]
    assert byok_actor["key_source"] == "byok"
    assert "_reservation_credits" not in byok_actor
    assert byok_call["params"]["api_key"] == SECRET
    assert byok_call["params"]["api_key"] != "browser-forgery-must-be-stripped"

    # The same account has no OpenRouter key. Provider Y deliberately falls
    # through to the normal pool reservation/reconciliation path.
    before_pool = _balance_and_ledger(email)
    with TestClient(app_mod.app).websocket_connect(
        "/engine", headers={"X-Dev-User": email}
    ) as ws:
        pooled = _rpc(ws, 3, "agent_ask_v2", {
            "question": "pooled Y",
            "provider": "openrouter",
            "model": "vendor/cheap",
            "max_tokens": 8,
            "_test_emit_usage": True,
        })
    assert pooled["result"]["ok"] is True
    after_pool = _balance_and_ledger(email)
    assert after_pool[0] == before_pool[0] - 1
    assert len(after_pool[1]) == len(before_pool[1]) + 1
    spends = [row for row in after_pool[1] if row["kind"] == "spend"]
    assert len(spends) == 1
    reservation = json.loads(spends[0]["note"])["reservation"]
    assert reservation["state"] == "reconciled"
    pool_call = [c for c in daemon.calls if c.get("method") == "agent_ask_v2"][-1]
    assert pool_call["params"]["options"]["_chudware_actor"]["key_source"] == "pool"
