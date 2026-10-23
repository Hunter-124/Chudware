"""Commercial credit invariants: idempotency, refusal, actuals, and BYOK."""
from __future__ import annotations

import sys
import hashlib
import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

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
    actor["_pool"] = {"provider": "openrouter", "api_key": "operator-pool-test-key"}
    actor["_pool_key_sha256"] = hashlib.sha256(b"operator-pool-test-key").hexdigest()
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


def test_worker_refusal_happens_before_chudcode_host_call(monkeypatch):
    _account(balance=100)
    _rates(("vendor/expensive", "0.000100", "0.000100"))


    class RefusingHost:
        def run_turn(self, *_args):
            raise AssertionError("unaffordable turn reached Chudcode")

    monkeypatch.setattr(worker_rpc, "get_default_chudcode_host", lambda: RefusingHost())
    out = worker_rpc._agent_admin_turn(
        {
            "request": "make a bracket",
            "provider": "openrouter",
            "model": "vendor/expensive",
            "_chudware_actor": _rated_actor("vendor/expensive"),
        },
        lambda _event: None,
    )
    assert out == {"ok": False, "reason": "insufficient_credits"}


def test_ample_pooled_turn_records_reported_actuals(monkeypatch):
    _account(balance=100)
    _rates(("vendor/cheap", "0.000001", "0.000002"))

    class UsageHost:
        def __init__(self):
            self.turns = []

        def run_turn(self, params, emit):
            self.turns.append(params)
            emit({
                "type": "usage",
                "provider": "openrouter",
                "model": "vendor/cheap",
                "promptTokens": 100,
                "completionTokens": 100,
                "totalTokens": 200,
            })
            terminal = {"type": "agent_end", "isTerminal": True}
            emit(terminal)
            return {"ok": True, "result": "done", "terminal": terminal}

    from services.mcp import cad_common

    monkeypatch.setattr(
        cad_common, "save_touched_docs", lambda: {
            "saved": [], "errors": [], "persistence_failure": None,
        },
    )

    host = UsageHost()
    monkeypatch.setattr(worker_rpc, "get_default_chudcode_host", lambda: host)
    events = []
    out = worker_rpc._agent_admin_turn(
        {
            "request": "cheap turn",
            "provider": "openrouter",
            "model": "vendor/cheap",
            "_chudware_actor": _rated_actor("vendor/cheap"),
        },
        events.append,
    )
    assert out["ok"] is True, out
    assert len(host.turns) == 1
    usage = next(event for event in events if event.get("kind") == "agent.provider_usage")
    assert usage["model"] == "vendor/cheap"
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 100
    credits.record_spend(
        _rated_actor("vendor/cheap"),
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


def test_native_claude_charge_uses_published_rate_not_unrelated_catalog_maximum():
    _account(balance=100)
    _rates(("vendor/expensive", "0.000100", "0.000200"))
    charged = credits.record_spend(
        credits.actor_for("buyer@example.com"),
        model="claude-opus-5-5", prompt_tokens=1_000, completion_tokens=1_000,
    )
    assert charged == 24
    assert _balance() == 76


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



def test_paid_checkout_claim_and_credit_ledger_roll_back_together(monkeypatch):
    monkeypatch.setattr(billing.settings, "stripe_price_credits_starter", "price_atomic")
    monkeypatch.setattr(billing, "_stripe", lambda: SimpleNamespace(api_key=""))
    obj = {
        "id": "cs_atomic", "mode": "payment", "payment_status": "paid",
        "client_reference_id": "buyer@example.com", "payment_intent": "pi_atomic",
        "line_items": {"data": [{
            "price": {"id": "price_atomic", "metadata": {"chud_credits": "8000"}},
        }]},
    }
    event = {"id": "evt_atomic", "type": "checkout.session.completed", "data": {"object": obj}}
    # No account: consumer failure must release both deduplication identities.
    with pytest.raises(LookupError, match="account not found"):
        billing._process_event_once(event)
    with tx() as cur:
        for table in ("billing_checkout_settlements", "credit_ledger", "processed_events"):
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            assert cur.fetchone()["n"] == 0
    _account()
    assert billing._process_event_once(event) is True
    obj["payment_status"] = "paid"
    event["id"] = "evt_atomic_success"
    event["type"] = "checkout.session.async_payment_succeeded"
    assert billing._process_event_once(event) is True
    assert _balance() == 8000
    assert len(_ledger("purchase")) == 1
