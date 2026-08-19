"""test_web_subscription_turn.py — a web account's own AI subscription running a turn.

The gateway (Webservice/gateway/engine_bridge.py) resolves an authenticated account's
Claude Pro/Max or ChatGPT Plus/Pro grant, injects it as ``params["subscription"]`` and
stamps ``key_source: "subscription"`` into ``options["_chudware_actor"]``. These tests
pin the worker half of that contract:

* ``subscription`` is a legal ``key_source`` and, like ``byok``, spends no credits;
* the injected grant selects the native sub-provider carrying THAT bearer, with the
  desktop credential store never read and never written;
* the grant is run-scoped, so the next RPC cannot inherit the previous caller's token
  (the cross-tenant leak this whole seam exists to prevent);
* a malformed grant refuses the turn instead of falling through to whatever
  subscription this machine's own user happens to be logged into.

``pool`` / ``byok`` / actor-free (desktop) resolution must be untouched, so the
identity of those paths is asserted here too.
"""
from __future__ import annotations

import base64
import json

import pytest

from services.agent import config as agent_config
from services.agent import worker_rpc
from services.agent.providers import sub_auth


def _actor(source: str, **extra) -> agent_config.Actor:
    payload = {
        "email": "user@example.com",
        "plan": "studio",
        "credits_remaining": 500,
        "key_source": source,
    }
    payload.update(extra)
    return agent_config.Actor.from_mapping(payload)


def _codex_jwt(account_id: str) -> str:
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


@pytest.fixture(autouse=True)
def _no_credential_store(monkeypatch):
    """Any read or write of the desktop credential store fails these tests loudly."""
    def _forbidden(*_a, **_k):
        raise AssertionError("a web subscription turn must not touch sub_auth")

    monkeypatch.setattr(sub_auth, "load_credential", _forbidden)
    monkeypatch.setattr(sub_auth, "save_credential", _forbidden)
    agent_config.clear_pooled_credentials()
    yield
    agent_config.clear_pooled_credentials()


# ──── the actor contract ────

def test_actor_accepts_subscription_key_source():
    actor = _actor("subscription")
    assert actor.key_source == "subscription"
    assert actor.email == "user@example.com"


def test_actor_still_rejects_an_unknown_key_source():
    for bogus in ("", "sub", "subscriptions", "free", "trust-me"):
        with pytest.raises(ValueError, match="key_source"):
            _actor(bogus)


def test_subscription_turn_spends_no_credits():
    """Unmetered exactly like byok: the account pays the vendor directly."""
    check = agent_config.pooled_credit_preflight(
        _actor("subscription"),
        prompt_tokens_estimate=250_000,
        max_output_tokens=8192,
    )
    assert check == {"ok": True, "max_output_tokens": 8192, "estimated_credits": 0}


# ──── grant validation ────

@pytest.mark.parametrize("bad", [
    None,
    "anthropic-sub",
    {},
    {"provider": "anthropic-sub"},
    {"provider": "anthropic-sub", "access_token": ""},
    {"provider": "anthropic-sub", "access_token": "   "},
    {"provider": "anthropic-sub", "access_token": 12345},
    {"provider": "anthropic", "access_token": "tok"},
    {"provider": "codex-sub", "access_token": "tok"},
    {"provider": "openai", "access_token": "tok"},
    {"provider": "", "access_token": "tok"},
])
def test_malformed_grant_is_refused_and_binds_nothing(bad):
    with pytest.raises(ValueError):
        agent_config.bind_subscription_grant(bad)
    assert agent_config._subscription_grant.get() is None


def test_grant_provider_is_exactly_the_two_gateway_ids():
    assert set(agent_config.SUBSCRIPTION_GRANT_PROVIDERS) == {
        "anthropic-sub", "openai-codex"}


# ──── provider resolution ────

def test_anthropic_grant_builds_the_sub_provider_with_the_injected_bearer():
    from services.agent.providers.anthropic_sub_provider import AnthropicSubProvider

    agent_config.bind_subscription_grant(
        {"provider": "anthropic-sub", "access_token": "acct-a-bearer"})
    # The browser selected the wire provider name; the grant decides the adapter.
    provider = agent_config.get_provider(
        name="anthropic",
        cfg={"provider": "anthropic", "model": "claude-opus-4-8"},
        actor=_actor("subscription"),
    )
    assert type(provider) is AnthropicSubProvider
    assert provider._injected is True
    assert provider._headers("00000")["Authorization"] == "Bearer acct-a-bearer"
    assert provider.model == "claude-opus-4-8"


def test_codex_grant_builds_the_sub_provider_with_the_injected_bearer():
    from services.agent.providers.codex_sub_provider import CodexSubProvider

    token = _codex_jwt("acct-9")
    agent_config.bind_subscription_grant(
        {"provider": "openai-codex", "access_token": token})
    provider = agent_config.get_provider(
        name="openai",
        cfg={"provider": "openai", "model": "gpt-5-codex"},
        actor=_actor("subscription"),
    )
    assert type(provider) is CodexSubProvider
    assert provider._injected is True
    headers = provider._headers()
    assert headers["Authorization"] == f"Bearer {token}"
    # Derived from the bearer's own claims, so the request shape matches a desktop turn.
    assert headers["chatgpt-account-id"] == "acct-9"


def test_injected_credential_never_refreshes_or_persists():
    from services.agent.providers.anthropic_sub_provider import AnthropicSubProvider
    from services.agent.providers.codex_sub_provider import CodexSubProvider

    for provider in (AnthropicSubProvider(access_token="tok"),
                     CodexSubProvider(access_token=_codex_jwt("a"))):
        provider._ensure_auth()  # a live injected bearer needs no refresh at all
        with pytest.raises(RuntimeError, match="account portal"):
            provider.refresh()   # and the store is never written for someone else's grant


def test_rejected_injected_credential_names_the_portal():
    from services.agent.providers.anthropic_sub_provider import AnthropicSubProvider

    provider = AnthropicSubProvider(access_token="tok")
    provider._cred = {"access_token": ""}
    with pytest.raises(RuntimeError, match="re-connect Claude"):
        provider._ensure_auth()


def test_only_an_injected_grant_bypasses_the_omp_bridge_shadow(monkeypatch):
    """The id "anthropic-sub" is also an OMP-sidecar id, and the sidecar resolves
    auth from its OWN store — this machine's login. A desktop turn keeps routing there;
    an injected per-account bearer must reach the native adapter that can carry it."""
    from services.agent.providers import anthropic_sub_provider, omp_bridge_provider

    class _Sidecar:
        def __init__(self, **kwargs):
            self.name = "omp-bridge"
            self.kwargs = kwargs

    monkeypatch.setattr(omp_bridge_provider, "OmpBridgeProvider", _Sidecar)

    desktop = agent_config._resolve_provider(
        "anthropic-sub", {"provider": "anthropic-sub", "model": "claude-opus-4-8"})
    assert type(desktop) is _Sidecar

    web = agent_config._resolve_provider("anthropic-sub", {
        "provider": "anthropic-sub",
        "model": "claude-opus-4-8",
        "_chudware_subscription_access_token": "acct-a-bearer",
    })
    assert type(web) is anthropic_sub_provider.AnthropicSubProvider
    assert web._headers("00000")["Authorization"] == "Bearer acct-a-bearer"


def test_subscription_actor_without_a_bound_grant_refuses_rather_than_falling_back():
    """No ambient fallback: without a grant there is no provider, full stop."""
    with pytest.raises(ValueError, match="subscription provider credential unavailable"):
        agent_config.get_provider(
            name="anthropic",
            cfg={"provider": "anthropic"},
            actor=_actor("subscription"),
        )


# ──── the cross-tenant leak ────

def test_clear_pooled_credentials_drops_the_grant():
    agent_config.bind_subscription_grant(
        {"provider": "anthropic-sub", "access_token": "tenant-a"})
    assert agent_config._subscription_grant.get() == {
        "provider": "anthropic-sub", "access_token": "tenant-a"}

    agent_config.clear_pooled_credentials()

    assert agent_config._subscription_grant.get() is None
    with pytest.raises(ValueError, match="subscription provider credential unavailable"):
        agent_config.get_provider(
            name="anthropic",
            cfg={"provider": "anthropic"},
            actor=_actor("subscription"),
        )


def test_a_following_rpc_cannot_inherit_the_previous_callers_bearer(monkeypatch):
    """Two RPCs through the real worker entry point, back to back, same process."""
    seen: list[str] = []

    def _capture(*_a, **kwargs):
        grant = agent_config._subscription_grant.get() or {}
        seen.append(str(grant.get("access_token") or ""))
        raise RuntimeError("stop here: provider construction is not under test")

    monkeypatch.setattr(agent_config, "get_provider", _capture)
    actor = {
        "email": "a@example.com", "plan": "studio",
        "credits_remaining": 0, "key_source": "subscription",
    }

    first = worker_rpc._agent_admin_turn(
        {
            "request": "model a bracket",
            "provider": "anthropic",
            "_chudware_actor": {
                **actor,
                "_subscription": {
                    "provider": "anthropic-sub", "access_token": "tenant-a-bearer"},
            },
        },
        lambda _e: None,
    )
    assert first["ok"] is False and "no provider" in first["reason"]
    assert seen == ["tenant-a-bearer"]

    # Tenant B's turn arrives with no grant of its own. It must be refused before any
    # provider is built, and tenant A's bearer must already be gone.
    second = worker_rpc._agent_admin_turn(
        {
            "request": "model a flange",
            "provider": "anthropic",
            "_chudware_actor": {**actor, "email": "b@example.com"},
        },
        lambda _e: None,
    )
    assert second == {"ok": False, "reason": "invalid_subscription_grant"}
    assert seen == ["tenant-a-bearer"]  # get_provider was never reached
    assert agent_config._subscription_grant.get() is None


def test_worker_refuses_a_malformed_grant_before_building_a_provider(monkeypatch):
    def _never(*_a, **_k):
        raise AssertionError("a malformed grant must not reach provider construction")

    monkeypatch.setattr(agent_config, "get_provider", _never)
    actor = {
        "email": "a@example.com", "plan": "studio",
        "credits_remaining": 0, "key_source": "subscription",
    }
    for grant in ({"provider": "nope", "access_token": "t"},
                  {"provider": "anthropic-sub", "access_token": ""},
                  {"provider": "anthropic-sub"},
                  "anthropic-sub"):
        out = worker_rpc._agent_admin_turn(
            {"request": "model a bracket", "provider": "anthropic",
             "_chudware_actor": {**actor, "_subscription": grant}},
            lambda _e: None,
        )
        assert out == {"ok": False, "reason": "invalid_subscription_grant"}


# ──── pool / byok / desktop are untouched ────

def test_non_subscription_actors_pass_name_and_cfg_through_unchanged():
    cfg = {"provider": "openai", "api_key": "ambient"}
    for source in ("pool", "byok"):
        for name in (None, "", "openai"):
            out_name, out_cfg = agent_config._subscription_provider_config(
                name, cfg, _actor(source))
            assert out_name is name
            assert out_cfg is cfg


def test_a_bound_grant_does_not_leak_into_a_pool_turn():
    """Only the actor's own key_source may select the subscription path."""
    agent_config.bind_subscription_grant(
        {"provider": "anthropic-sub", "access_token": "tenant-a"})
    out_name, out_cfg = agent_config._subscription_provider_config(
        "openai", {"provider": "openai"}, _actor("pool"))
    assert out_name == "openai"
    assert "_chudware_subscription_access_token" not in out_cfg


def test_pool_and_byok_turns_still_resolve_with_their_gateway_key():
    from services.agent.providers.openai_provider import OpenAIProvider

    for source in ("pool", "byok"):
        agent_config.clear_pooled_credentials()
        provider = agent_config.get_provider(
            name="openai",
            cfg={
                "provider": "openai",
                "_chudware_pool_api_key": f"sk-{source}",
                "model": "gpt-4o",
                "openai": {"base_url": "http://127.0.0.1:1234/v1"},
            },
            actor=_actor(source),
        )
        assert type(provider) is OpenAIProvider
        assert provider.client.api_key == f"sk-{source}"


def test_a_desktop_turn_has_no_actor_and_ignores_any_bound_grant():
    """No actor == the local user's own machine: none of this code path runs."""
    agent_config.bind_subscription_grant(
        {"provider": "anthropic-sub", "access_token": "someone-elses"})
    provider = agent_config.get_provider(name="mock", cfg={"provider": "mock"})
    assert type(provider).__name__ == "MockProvider"
