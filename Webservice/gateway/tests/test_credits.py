"""Commercial credit invariants: idempotency, refusal, actuals, and BYOK."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway import accounts, billing, credits, db, plans
from gateway.db import tx
from services.agent import worker_rpc
from services.agent.providers import token_usage


@pytest.fixture(autouse=True)
def _close_package_db_pool():
    yield
    db.close_pool()


def test_server_plan_catalog_matches_source_of_truth_exactly():
    request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(email="admin@example.com")))
    result = asyncio.run(billing.plan_catalog(request))
    assert [entry["id"] for entry in result["plans"]] == list(plans.PLANS)
    assert all(entry["display_name"] for entry in result["plans"])


def _account(email: str = "buyer@example.com", *, balance: int = 0, ai_grant: bool = True):
    accounts.ensure_account(email, "Buyer")
    accounts.set_status(email, "active", decided_by="test")
    with tx() as cur:
        cur.execute(
            "UPDATE accounts SET ai_grant = %s WHERE email = %s",
            (ai_grant, email),
        )
    if balance:
        credits.grant(email, balance, note="test seed")


def _rated_actor(model: str) -> dict:
    actor = credits.actor_for("buyer@example.com")
    prompt_rate, completion_rate = credits.model_rate(model, refresh=False)
    actor["_rate"] = {
        "model": model,
        "prompt_usd_per_mtok": str(prompt_rate),
        "completion_usd_per_mtok": str(completion_rate),
    }
    return actor


def _rates(*rows: tuple[str, str, str]) -> None:
    data = [
        {"id": model, "pricing": {"prompt": prompt, "completion": completion}}
        for model, prompt, completion in rows
    ]
    credits.refresh_model_rates(lambda _url: {"data": data}, now=1_800_000_000)


def _balance(email: str = "buyer@example.com") -> int:
    with tx() as cur:
        cur.execute("SELECT credit_balance FROM accounts WHERE email = %s", (email,))
        return int(cur.fetchone()["credit_balance"])


def _ledger(kind: str | None = None, email: str = "buyer@example.com"):
    with tx() as cur:
        if kind is None:
            cur.execute(
                "SELECT * FROM credit_ledger WHERE email = %s ORDER BY ts, id", (email,)
            )
        else:
            cur.execute(
                "SELECT * FROM credit_ledger WHERE email = %s AND kind = %s ORDER BY ts, id",
                (email, kind),
            )
        return cur.fetchall()


def test_pooled_provider_uses_only_operator_credential(monkeypatch):
    from services.agent import config as agent_config

    captured = {}

    class Provider:
        name = "openrouter"
        model = "vendor/model"

    def resolve(name, cfg, role=None):
        captured.update(cfg)
        return Provider()

    monkeypatch.setattr(agent_config, "_resolve_provider", resolve)
    actor = agent_config.Actor(
        email="buyer@example.com",
        plan="studio",
        credits_remaining=100,
        key_source="pool",
    )
    agent_config.get_provider(
        "openrouter",
        {
            "provider": "openrouter",
            "api_key": "ambient-user-key",
            "keys": {"openrouter": "ambient-map-key"},
            "openrouter": {"api_key": "ambient-section-key"},
            "_chudware_pool_api_key": "operator-pool-key",
        },
        actor=actor,
    )
    assert captured["api_key"] == "operator-pool-key"
    assert captured["keys"]["openrouter"] == "operator-pool-key"
    assert captured["openrouter"]["api_key"] == "operator-pool-key"


def test_pooled_provider_without_operator_credential_fails_closed():
    from services.agent import config as agent_config
    agent_config.clear_pooled_credentials()

    actor = agent_config.Actor(
        email="buyer@example.com",
        plan="studio",
        credits_remaining=100,
        key_source="pool",
    )
    with pytest.raises(ValueError, match="pooled provider credential unavailable"):
        agent_config.get_provider(
            "openrouter",
            {"provider": "openrouter", "api_key": "ambient-user-key"},
            actor=actor,
        )


def test_credit_pack_event_replayed_three_times_applies_once():
    _account()
    outcomes = [
        credits.grant_purchased(
            "buyer@example.com", 8_000, "evt_credit_pack_1", {"pack": "credit_10"}
        )
        for _ in range(3)
    ]
    assert outcomes == [True, False, False]
    assert _balance() == 8_000
    purchases = _ledger("purchase")
    assert len(purchases) == 1
    assert purchases[0]["credits_delta"] == 8_000
    assert purchases[0]["stripe_event_id"] == "evt_credit_pack_1"


def test_pool_preflight_refuses_an_unaffordable_turn():
    _account(balance=100)
    _rates(("vendor/cheap", "0.000001", "0.000002"))  # $1/$2 per MTok
    actor = credits.actor_for("buyer@example.com")
    result = credits.preflight(
        actor,
        model="vendor/cheap",
        prompt_tokens_estimate=100,
        max_output_tokens=100_000,
    )
    assert result["ok"] is False
    assert result["reason"] == "insufficient_credits"
    assert _ledger("spend") == []


def test_zero_ceiling_is_unlimited_but_one_credit_ceiling_throttles():
    _account(balance=100)
    _rates(("vendor/cheap", "0.000001", "0.000002"))
    actor = credits.actor_for("buyer@example.com")
    unlimited = credits.preflight(
        actor,
        model="vendor/cheap",
        prompt_tokens_estimate=0,
        max_output_tokens=1_000,
    )
    assert unlimited["ok"] is True

    with tx() as cur:
        cur.execute(
            "UPDATE accounts SET credit_ceiling_monthly = 1 WHERE email = %s",
            ("buyer@example.com",),
        )
    limited = credits.preflight(
        actor,
        model="vendor/cheap",
        prompt_tokens_estimate=0,
        max_output_tokens=1_000,
    )
    assert limited == {
        "ok": False,
        "reason": "insufficient_credits",
        "estimated_credits": 2,
        "credits_remaining": 1,
    }


def test_worker_refusal_happens_before_mock_provider_call(monkeypatch):
    _account(balance=100)
    _rates(("vendor/expensive", "0.000100", "0.000100"))

    class Provider:
        name = "mock"
        model = "vendor/expensive"

        def __init__(self):
            self.calls = 0

        def complete(self, *_args, **_kwargs):
            self.calls += 1
            return "should not run"

    provider = Provider()
    from services.agent import config as agent_config
    from services.agent import mission_dispatch

    monkeypatch.setattr(agent_config, "get_provider", lambda *a, **k: provider)
    monkeypatch.setattr(
        mission_dispatch,
        "run_linear_role_turn",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("turn dispatched")),
    )
    out = worker_rpc._agent_admin_turn(
        {
            "request": "make a bracket",
            "provider": "mock",
            "model": provider.model,
            "api_key": "operator-key",
            "_chudware_actor": _rated_actor(provider.model),
        },
        lambda _event: None,
    )
    assert out == {"ok": False, "reason": "insufficient_credits"}
    assert provider.calls == 0


def test_ample_pooled_turn_records_reported_actuals(monkeypatch):
    _account(balance=100)
    _rates(("vendor/cheap", "0.000001", "0.000002"))

    class Provider:
        name = "mock"
        model = "vendor/cheap"

        def __init__(self):
            self.calls = 0

        def complete(self, *_args, **_kwargs):
            self.calls += 1
            token_usage.record_usage(
                provider=self.name,
                model=self.model,
                prompt_tokens=100,
                completion_tokens=100,
            )
            return "done"

    provider = Provider()
    from services.agent import config as agent_config
    from services.agent import mission_dispatch

    def get_provider(*_args, **_kwargs):
        return provider

    def one_turn(_request, _role, bound, **_kwargs):
        bound.complete("", [], max_tokens=8)
        return {"success": True, "stop_reason": "confirmed", "result": {}}

    monkeypatch.setattr(agent_config, "get_provider", get_provider)
    monkeypatch.setattr(mission_dispatch, "run_linear_role_turn", one_turn)
    events = []
    out = worker_rpc._agent_admin_turn(
        {
            "request": "cheap turn",
            "provider": "mock",
            "model": provider.model,
            "api_key": "operator-key",
            "_chudware_actor": _rated_actor(provider.model),
        },
        events.append,
    )
    assert out["ok"] is True
    assert provider.calls == 1
    usage = next(event for event in events if event.get("kind") == "agent.provider_usage")
    assert set(usage) == {
        "kind", "provider", "model", "prompt_tokens", "completion_tokens",
        "total_tokens", "context_tokens", "source",
    }
    credits.record_spend(
        _rated_actor(provider.model),
        model=usage["model"],
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
    )
    assert _balance() == 99
    spends = _ledger("spend")
    assert len(spends) == 1
    assert spends[0]["model"] == "vendor/cheap"
    assert spends[0]["prompt_tokens"] == 100
    assert spends[0]["completion_tokens"] == 100
    assert spends[0]["credits_delta"] == -1


def test_byok_usage_writes_nothing_and_leaves_balance_untouched():
    _account(balance=100, ai_grant=False)
    before = list(_ledger())
    row = token_usage.record_usage(
        actor={
            "email": "buyer@example.com",
            "plan": "studio",
            "credits_remaining": 100,
            "key_source": "byok",
        },
        provider="mock",
        model="rate/table/is/irrelevant",
        prompt_tokens=10_000,
        completion_tokens=10_000,
    )
    assert "credits_charged" not in row
    assert _balance() == 100
    assert list(_ledger()) == before


def test_cancelled_stream_still_debits_usage_already_reported():
    _account(balance=100)
    _rates(("vendor/stream", "0.000010", "0.000010"))
    actor = credits.actor_for("buyer@example.com")
    emitted = []
    sink_token = token_usage.bind_usage_sink(emitted.append)

    async def dying_stream():
        token_usage.record_usage(
            actor=actor,
            provider="mock",
            model="vendor/stream",
            prompt_tokens=250,
            completion_tokens=50,
            source="stream",
        )
        raise asyncio.CancelledError

    try:
        try:
            asyncio.run(dying_stream())
        except asyncio.CancelledError:
            pass
    finally:
        token_usage.reset_usage_sink(sink_token)
    assert len(emitted) == 1
    credits.record_spend(
        actor,
        model=emitted[0]["model"],
        prompt_tokens=emitted[0]["prompt_tokens"],
        completion_tokens=emitted[0]["completion_tokens"],
    )
    spends = _ledger("spend")
    assert len(spends) == 1
    assert spends[0]["prompt_tokens"] == 250
    assert spends[0]["completion_tokens"] == 50
    assert _balance() == 97


def test_unknown_model_uses_most_expensive_cached_rates():
    _account(balance=100)
    _rates(
        ("vendor/cheap", "0.000001", "0.000002"),
        ("vendor/expensive", "0.000004", "0.000010"),
    )
    actor = credits.actor_for("buyer@example.com")
    charged = credits.record_spend(
        actor, model="vendor/not-yet-listed", prompt_tokens=1_000, completion_tokens=1_000
    )
    assert charged == 14
    assert _balance() == 86
    assert _ledger("spend")[0]["credits_delta"] == -14


def test_final_overshoot_is_adjusted_and_balance_never_negative():
    _account(balance=3)
    _rates(("vendor/expensive", "0.000010", "0.000010"))
    charged = credits.record_spend(
        credits.actor_for("buyer@example.com"),

        model="vendor/expensive",
        prompt_tokens=500,
        completion_tokens=500,
    )
    assert charged == 3
    assert _balance() == 0
    assert _ledger("spend")[0]["credits_delta"] == -10
    adjustments = _ledger("adjust")
    assert len(adjustments) == 1
    assert adjustments[0]["credits_delta"] == 7
    assert credits.reconcile_balances() == []

def test_reservation_reconciles_down_to_actual_usage():
    _account(balance=100)
    _rates(("vendor/reserve", "0.000010", "0.000010"))
    actor = credits.actor_for("buyer@example.com")
    reservation = credits.reserve_turn(
        actor,
        model="vendor/reserve",
        prompt_tokens_estimate=0,
        max_output_tokens=1_000,
    )
    assert reservation["estimated_credits"] == 10
    assert _balance() == 90
    credits.reconcile_reservation(
        reservation["reservation_id"],
        actual_credits=3,
        prompt_tokens=100,
        completion_tokens=200,
    )
    assert _balance() == 97
    assert _ledger("refund")[0]["credits_delta"] == 7
    reservation_row = next(
        row for row in _ledger("spend") if row["id"] == reservation["reservation_id"]
    )
    assert '"state":"reconciled"' in reservation_row["note"]


def test_hard_killed_turn_leaves_estimate_reserved():
    _account(balance=100)
    _rates(("vendor/reserve", "0.000010", "0.000010"))
    reservation = credits.reserve_turn(
        credits.actor_for("buyer@example.com"),
        model="vendor/reserve",
        prompt_tokens_estimate=0,
        max_output_tokens=1_000,
    )
    assert _balance() == 90
    row = next(
        row for row in _ledger("spend") if row["id"] == reservation["reservation_id"]
    )
    assert row["credits_delta"] == -10
    assert '"state":"reserved"' in row["note"]
    assert _ledger("refund") == []


def test_ten_repeated_kills_drain_balance_then_preflight_refuses():
    _account(balance=10)
    _rates(("vendor/reserve", "0.000001", "0.000001"))
    actor = credits.actor_for("buyer@example.com")
    reservations = [
        credits.reserve_turn(
            actor,
            model="vendor/reserve",
            prompt_tokens_estimate=0,
            max_output_tokens=1_000,
        )
        for _ in range(10)
    ]
    assert all(item["ok"] and item["estimated_credits"] == 1 for item in reservations)
    assert _balance() == 0
    refused = credits.reserve_turn(
        actor,
        model="vendor/reserve",
        prompt_tokens_estimate=0,
        max_output_tokens=1_000,
    )
    assert refused["ok"] is False
    assert refused["reason"] == "insufficient_credits"
    assert len([
        row for row in _ledger("spend") if '"state":"reserved"' in row["note"]
    ]) == 10

