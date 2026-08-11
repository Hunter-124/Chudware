"""Billing slice tests (dev-auth hermetic; stripe stubbed — no network).

Covers:
- plans.limits_for caps per tier + unknown-plan rejection
- checkout rejects unknown plans and ignores client-supplied price ids/amounts
- webhook: bad/missing signature -> 400 with zero entitlement side effects
- webhook: checkout.session.completed upgrades; replayed event id is a no-op
- webhook: customer.subscription.deleted downgrades to free
- no Stripe key -> 503 on checkout/portal/webhook, and the module still
  imports cleanly with the stripe package absent
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
if str(GATEWAY_DIR.parent) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR.parent))

os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault(
    "CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test")
)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import accounts, billing, plans  # noqa: E402
from gateway.auth import AuthMiddleware  # noqa: E402
from gateway.config import settings  # noqa: E402

DEV_HEADERS = {"X-Dev-User": "demo@example.com"}
VALID_SIG = "t=1,v1=valid"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeAccount:
    email: str
    display: str = ""
    status: str = "active"
    plan: str = "free"
    stripe_customer_id: str = ""


class FakeAccounts:
    """In-memory stand-in for gateway/accounts.py (interface agreed with its owner)."""

    def __init__(self) -> None:
        self.by_email: dict[str, FakeAccount] = {}
        self.status_calls: list[tuple] = []

    def get_account(self, email):
        return self.by_email.get(email)

    def get_account_by_stripe_customer(self, customer_id):
        for acct in self.by_email.values():
            if acct.stripe_customer_id and acct.stripe_customer_id == customer_id:
                return acct
        return None

    def ensure_account(self, email, display):
        acct = self.by_email.get(email)
        if acct is None:
            acct = FakeAccount(email=email, display=display)
            self.by_email[email] = acct
        return acct

    def set_plan(self, email, plan):
        acct = self.by_email.get(email)
        if acct is None:
            return None
        acct.plan = plan
        return acct

    def set_stripe_customer(self, email, customer_id):
        acct = self.by_email.get(email)
        if acct is None:
            return None
        acct.stripe_customer_id = customer_id
        return acct

    def set_status(self, email, status, decided_by="", notes=""):
        acct = self.by_email.get(email)
        if acct is None:
            return None
        acct.status = status
        self.status_calls.append((email, status, decided_by, notes))
        return acct


class SignatureVerificationError(Exception):
    pass


def _install_fake_stripe(monkeypatch, captured: dict, *, valid_sig: str = VALID_SIG):
    """Put a stub `stripe` module into sys.modules — zero network."""
    fake = types.ModuleType("stripe")
    fake.error = types.SimpleNamespace(
        SignatureVerificationError=SignatureVerificationError
    )
    fake.api_key = None

    class CheckoutSession:
        @staticmethod
        def create(**kwargs):
            captured["checkout"] = kwargs
            return types.SimpleNamespace(url="https://checkout.stripe.test/pay_123")

    class PortalSession:
        @staticmethod
        def create(**kwargs):
            captured["portal"] = kwargs
            return types.SimpleNamespace(url="https://billing.stripe.test/portal_123")

    class Webhook:
        @staticmethod
        def construct_event(body, sig, secret):
            if sig != valid_sig:
                raise SignatureVerificationError("bad signature")
            return json.loads(body)

    fake.checkout = types.SimpleNamespace(Session=CheckoutSession)
    fake.billing_portal = types.SimpleNamespace(Session=PortalSession)
    fake.Webhook = Webhook
    monkeypatch.setitem(sys.modules, "stripe", fake)
    return fake


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store():
    s = FakeAccounts()
    s.ensure_account("demo@example.com", "Demo User")
    billing.set_account_store(s)
    yield s
    billing.set_account_store(None)


@pytest.fixture()
def client(pg, monkeypatch, store):
    """Full app: real AuthMiddleware (dev mode) + the billing router.

    Two stores are in play on purpose: AuthMiddleware authorizes from the real
    account store (so the dev identity needs an active row there), while
    billing's entitlement writes go to the injected FakeAccounts so the
    webhook assertions stay hermetic.
    """
    monkeypatch.setattr(settings, "dev_mode", True)
    accounts.ensure_account("demo@example.com", display="Demo User")
    accounts.set_status("demo@example.com", "active", decided_by="test")
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(billing.router)
    return TestClient(app)


@pytest.fixture()
def stripe_env(monkeypatch):
    monkeypatch.setenv("CHUDWARE_WEB_STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET", "whsec_123")
    monkeypatch.setenv(plans.STRIPE_PRICE_PRO_ENV, "price_pro_123")
    monkeypatch.setenv(plans.STRIPE_PRICE_TEAM_ENV, "price_team_123")


def _event(event_id, event_type, obj):
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": obj},
    }


def _post_webhook(client, event, sig=VALID_SIG):
    headers = {"Content-Type": "application/json"}
    if sig is not None:
        headers["Stripe-Signature"] = sig
    return client.post("/api/billing/webhook", content=json.dumps(event), headers=headers)


# ---------------------------------------------------------------------------
# plans.py
# ---------------------------------------------------------------------------


class TestPlans:
    def test_limits_for_each_tier(self):
        free = plans.limits_for("free")
        assert free.storage_quota_bytes == 2 * 1024**3
        assert free.monthly_llm_tokens > 0

        pro = plans.limits_for("pro")
        assert pro.storage_quota_bytes > free.storage_quota_bytes
        assert pro.monthly_llm_tokens > free.monthly_llm_tokens

        team = plans.limits_for("team")
        assert team.storage_quota_bytes > pro.storage_quota_bytes
        assert team.monthly_llm_tokens > pro.monthly_llm_tokens

        # A trial is the full product for its window, so evaluating it means
        # evaluating Pro's caps — not Free's.
        trial = plans.limits_for("trial")
        assert trial.storage_quota_bytes == pro.storage_quota_bytes
        assert trial.monthly_llm_tokens == pro.monthly_llm_tokens

    def test_only_paid_tiers_are_purchasable(self):
        assert plans.is_purchasable("pro")
        assert plans.is_purchasable("team")
        assert not plans.is_purchasable("free")
        # Granted by verifying an address, never sold — Checkout must refuse it.
        assert not plans.is_purchasable("trial")

    def test_unknown_plan_rejected(self):
        with pytest.raises(ValueError):
            plans.limits_for("enterprise-ultra")
        with pytest.raises(ValueError):
            plans.limits_for("")

    def test_no_stripe_import(self):
        assert "stripe" not in sys.modules or sys.modules["stripe"] is None
        assert not hasattr(plans, "stripe")
        src = (GATEWAY_DIR / "plans.py").read_text()
        assert "import stripe" not in src


# ---------------------------------------------------------------------------
# Checkout / portal
# ---------------------------------------------------------------------------


class TestCheckout:
    def test_unknown_plan_rejected(self, client, stripe_env, monkeypatch):
        captured = {}
        _install_fake_stripe(monkeypatch, captured)
        r = client.post(
            "/api/billing/checkout", json={"plan": "platinum"}, headers=DEV_HEADERS
        )
        assert r.status_code == 400
        assert "checkout" not in captured  # no Stripe call happened

    def test_free_plan_not_purchasable(self, client, stripe_env, monkeypatch):
        _install_fake_stripe(monkeypatch, {})
        r = client.post(
            "/api/billing/checkout", json={"plan": "free"}, headers=DEV_HEADERS
        )
        assert r.status_code == 400

    def test_client_price_id_and_amount_ignored(
        self, client, stripe_env, monkeypatch
    ):
        captured = {}
        _install_fake_stripe(monkeypatch, captured)
        r = client.post(
            "/api/billing/checkout",
            json={
                "plan": "pro",
                "price_id": "price_evil_free",
                "amount": 1,
                "price": "price_evil_free",
            },
            headers=DEV_HEADERS,
        )
        assert r.status_code == 200, r.text
        sent = captured["checkout"]
        # The price came from the server-side env, never from the request body.
        assert sent["line_items"] == [{"price": "price_pro_123", "quantity": 1}]
        assert "amount" not in sent
        assert sent["client_reference_id"] == "demo@example.com"
        assert sent["metadata"]["chudware_plan"] == "pro"
        assert r.json()["url"].startswith("https://checkout.stripe.test/")

    def test_existing_customer_reused(self, client, store, stripe_env, monkeypatch):
        captured = {}
        _install_fake_stripe(monkeypatch, captured)
        store.set_stripe_customer("demo@example.com", "cus_existing")
        r = client.post(
            "/api/billing/checkout", json={"plan": "team"}, headers=DEV_HEADERS
        )
        assert r.status_code == 200
        assert captured["checkout"]["customer"] == "cus_existing"
        assert "customer_email" not in captured["checkout"]
        assert captured["checkout"]["line_items"][0]["price"] == "price_team_123"


class TestPortal:
    def test_portal_returns_url_for_own_customer(
        self, client, store, stripe_env, monkeypatch
    ):
        captured = {}
        _install_fake_stripe(monkeypatch, captured)
        store.set_stripe_customer("demo@example.com", "cus_123")
        r = client.post("/api/billing/portal", headers=DEV_HEADERS)
        assert r.status_code == 200
        assert captured["portal"]["customer"] == "cus_123"
        assert r.json()["url"].startswith("https://billing.stripe.test/")

    def test_portal_without_customer_404(self, client, stripe_env, monkeypatch):
        _install_fake_stripe(monkeypatch, {})
        r = client.post("/api/billing/portal", headers=DEV_HEADERS)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


class TestWebhookSecurity:
    def test_missing_signature_400_no_side_effect(
        self, client, store, stripe_env, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        event = _event(
            "evt_nosig",
            "checkout.session.completed",
            {
                "client_reference_id": "demo@example.com",
                "customer": "cus_x",
                "metadata": {"chudware_plan": "pro"},
            },
        )
        r = _post_webhook(client, event, sig=None)
        assert r.status_code == 400
        acct = store.get_account("demo@example.com")
        assert acct.plan == "free"
        assert acct.stripe_customer_id == ""

    def test_bad_signature_400_no_side_effect(
        self, client, store, stripe_env, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        event = _event(
            "evt_badsig",
            "checkout.session.completed",
            {
                "client_reference_id": "demo@example.com",
                "customer": "cus_x",
                "metadata": {"chudware_plan": "team"},
            },
        )
        r = _post_webhook(client, event, sig="t=1,v1=forged")
        assert r.status_code == 400
        acct = store.get_account("demo@example.com")
        assert acct.plan == "free"
        assert acct.stripe_customer_id == ""

    def test_oversized_body_413(self, client, stripe_env, monkeypatch):
        _install_fake_stripe(monkeypatch, {})
        big = b" " * (billing.MAX_WEBHOOK_BYTES + 1)
        r = client.post(
            "/api/billing/webhook",
            content=big,
            headers={"Stripe-Signature": VALID_SIG},
        )
        assert r.status_code == 413

    def test_webhook_needs_no_auth(self, client, store, stripe_env, monkeypatch):
        """The public path: no X-Dev-User, no cookies — signature is the auth."""
        _install_fake_stripe(monkeypatch, {})
        event = _event(
            "evt_anon",
            "customer.subscription.deleted",
            {"customer": "cus_unknown"},
        )
        r = client.post(
            "/api/billing/webhook",
            content=json.dumps(event),
            headers={"Stripe-Signature": VALID_SIG},
        )
        assert r.status_code == 200


class TestWebhookEntitlement:
    def test_checkout_completed_upgrades_and_replay_is_noop(
        self, client, store, stripe_env, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        event = _event(
            "evt_upgrade_1",
            "checkout.session.completed",
            {
                "client_reference_id": "demo@example.com",
                "customer": "cus_new",
                "metadata": {"chudware_plan": "pro"},
            },
        )
        r1 = _post_webhook(client, event)
        assert r1.status_code == 200
        assert r1.json()["status"] == "processed"
        acct = store.get_account("demo@example.com")
        assert acct.plan == "pro"
        assert acct.stripe_customer_id == "cus_new"
        assert acct.status == "active"
        status_calls_after_first = len(store.status_calls)

        # Stripe redelivers the identical event id: nothing changes.
        r2 = _post_webhook(client, event)
        assert r2.status_code == 200
        assert r2.json()["status"] == "duplicate"
        acct = store.get_account("demo@example.com")
        assert acct.plan == "pro"
        assert len(store.status_calls) == status_calls_after_first

    def test_replay_against_the_real_store_mutates_exactly_once(
        self, client, stripe_env, monkeypatch
    ):
        """The money bug, against Postgres rather than the in-memory fake.

        The event-id row and the entitlement mutation share one transaction, so
        a redelivery must leave exactly one processed_events row and exactly one
        plan change — not two, and not one plus a partially applied second.
        """
        import accounts  # type: ignore
        import db  # type: ignore

        billing.set_account_store(accounts)
        try:
            accounts.ensure_account("payer@example.com", display="Payer")
            _install_fake_stripe(monkeypatch, {})
            event = _event(
                "evt_real_1",
                "checkout.session.completed",
                {
                    "client_reference_id": "payer@example.com",
                    "customer": "cus_real",
                    "metadata": {"chudware_plan": "pro"},
                },
            )
            assert _post_webhook(client, event).json()["status"] == "processed"
            assert _post_webhook(client, event).json()["status"] == "duplicate"
            assert _post_webhook(client, event).json()["status"] == "duplicate"

            with db.tx() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM processed_events WHERE event_id = %s",
                    ("evt_real_1",),
                )
                assert cur.fetchone()["n"] == 1
            acct = accounts.get_account("payer@example.com")
            assert (acct.plan, acct.status, acct.stripe_customer_id) == (
                "pro",
                "active",
                "cus_real",
            )
            # One decision recorded, not three.
            assert acct.notes == "checkout.session.completed evt_real_1"
        finally:
            billing.set_account_store(None)

    def test_a_failing_entitlement_rolls_back_the_event_id(self, client, stripe_env, monkeypatch):
        """If the mutation blows up, the event id must NOT be recorded — Stripe's
        retry has to be able to apply it. Same transaction, both or neither."""
        import db  # type: ignore

        class Exploding(FakeAccounts):
            def set_plan(self, email, plan):
                raise RuntimeError("store is down")

        exploding = Exploding()
        exploding.ensure_account("boom@example.com", "Boom")
        billing.set_account_store(exploding)
        try:
            _install_fake_stripe(monkeypatch, {})
            event = _event(
                "evt_boom",
                "checkout.session.completed",
                {
                    "client_reference_id": "boom@example.com",
                    "customer": "cus_boom",
                    "metadata": {"chudware_plan": "pro"},
                },
            )
            with pytest.raises(RuntimeError, match="store is down"):
                _post_webhook(client, event)
            with db.tx() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM processed_events")
                assert cur.fetchone()["n"] == 0
        finally:
            billing.set_account_store(None)

    def test_checkout_completed_adopts_stranger(
        self, client, store, stripe_env, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        event = _event(
            "evt_stranger",
            "checkout.session.completed",
            {
                "customer": "cus_stranger",
                "customer_email": "newbie@example.com",
                "metadata": {
                    "chudware_email": "newbie@example.com",
                    "chudware_plan": "team",
                },
            },
        )
        r = _post_webhook(client, event)
        assert r.status_code == 200
        acct = store.get_account("newbie@example.com")
        assert acct is not None
        assert acct.plan == "team"
        assert acct.status == "active"

    def test_subscription_deleted_downgrades_to_free(
        self, client, store, stripe_env, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        store.set_stripe_customer("demo@example.com", "cus_123")
        store.set_plan("demo@example.com", "pro")
        event = _event(
            "evt_cancel",
            "customer.subscription.deleted",
            {"customer": "cus_123", "status": "canceled"},
        )
        r = _post_webhook(client, event)
        assert r.status_code == 200
        assert store.get_account("demo@example.com").plan == "free"

    def test_subscription_updated_past_due_downgrades(
        self, client, store, stripe_env, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        store.set_stripe_customer("demo@example.com", "cus_123")
        store.set_plan("demo@example.com", "team")
        event = _event(
            "evt_pastdue",
            "customer.subscription.updated",
            {"customer": "cus_123", "status": "past_due"},
        )
        r = _post_webhook(client, event)
        assert r.status_code == 200
        assert store.get_account("demo@example.com").plan == "free"

    def test_subscription_updated_active_syncs_plan_from_price(
        self, client, store, stripe_env, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        store.set_stripe_customer("demo@example.com", "cus_123")
        event = _event(
            "evt_sync",
            "customer.subscription.updated",
            {
                "customer": "cus_123",
                "status": "active",
                "items": {"data": [{"price": {"id": "price_pro_123"}}]},
            },
        )
        r = _post_webhook(client, event)
        assert r.status_code == 200
        assert store.get_account("demo@example.com").plan == "pro"


# ---------------------------------------------------------------------------
# Degraded mode: no Stripe configured
# ---------------------------------------------------------------------------


class TestUnconfigured:
    @pytest.fixture(autouse=True)
    def _no_keys(self, monkeypatch):
        for var in (
            "CHUDWARE_WEB_STRIPE_SECRET_KEY",
            "CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_checkout_503(self, client):
        r = client.post(
            "/api/billing/checkout", json={"plan": "pro"}, headers=DEV_HEADERS
        )
        assert r.status_code == 503
        assert "billing not configured" in r.json()["detail"]

    def test_portal_503(self, client):
        r = client.post("/api/billing/portal", headers=DEV_HEADERS)
        assert r.status_code == 503

    def test_webhook_503(self, client):
        r = client.post("/api/billing/webhook", content=b"{}")
        assert r.status_code == 503

    def test_status_still_works_and_reports_unconfigured(self, client):
        r = client.get("/api/billing/status", headers=DEV_HEADERS)
        assert r.status_code == 200
        body = r.json()
        assert body["billing_configured"] is False
        assert body["plan"] == "free"
        assert body["limits"]["storage_quota_bytes"] == 2 * 1024**3

    def test_module_imports_without_stripe_package(self, monkeypatch):
        """billing must import (and re-import) with no stripe package at all."""
        monkeypatch.delitem(sys.modules, "stripe", raising=False)
        importlib.reload(billing)
        assert "stripe" not in sys.modules
