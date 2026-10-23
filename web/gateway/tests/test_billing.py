"""Commercial plans and Stripe billing contracts (fake transport, real webhook DB)."""

from __future__ import annotations

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
from gateway.config import Settings, settings  # noqa: E402

DEV_HEADERS = {"X-Dev-User": "demo@example.com"}
VALID_SIG = "t=1,v1=valid"


@dataclass
class FakeAccount:
    email: str
    display: str = ""
    status: str = "active"
    plan: str = "trial"
    stripe_customer_id: str = ""
    subscription_id: str = ""
    subscription_status: str = ""
    price_id: str = ""
    subscribed_since: float | None = None
    paid_through: float | None = None
    is_edu: bool = False
    credit_balance: int = 0


class FakeAccounts:
    def __init__(self) -> None:
        self.by_email: dict[str, FakeAccount] = {}
        self.subscription_calls: list[dict] = []

    def get_account(self, email):
        return self.by_email.get(email)

    def get_account_by_stripe_customer(self, customer_id):
        return next(
            (
                account
                for account in self.by_email.values()
                if account.stripe_customer_id == customer_id and customer_id
            ),
            None,
        )

    def ensure_account(self, email, display):
        return self.by_email.setdefault(email, FakeAccount(email=email, display=display))

    def set_plan(self, email, plan):
        account = self.by_email.get(email)
        if account:
            account.plan = plan
        return account

    def set_stripe_customer(self, email, customer_id):
        account = self.by_email.get(email)
        if account:
            account.stripe_customer_id = customer_id
        return account

    def set_subscription(self, email, **values):
        account = self.by_email.get(email)
        if account is None:
            return None
        self.subscription_calls.append(dict(values))
        for name in ("subscription_id", "subscription_status", "price_id", "is_edu"):
            value = values.get(name)
            if value not in (None, ""):
                setattr(account, name, value)
        start = values.get("subscribed_since")
        if account.subscribed_since is None and start is not None:
            account.subscribed_since = start
        through = values.get("paid_through")
        if through is not None and (
            account.paid_through is None or through > account.paid_through
        ):
            account.paid_through = through
        return account


class SignatureVerificationError(Exception):
    pass


def _install_fake_stripe(monkeypatch, captured: dict, *, valid_sig: str = VALID_SIG):
    fake = types.ModuleType("stripe")
    fake.api_key = None
    fake.error = types.SimpleNamespace(
        SignatureVerificationError=SignatureVerificationError
    )

    class CheckoutSession:
        @staticmethod
        def create(**kwargs):
            captured.setdefault("checkouts", []).append(kwargs)
            return types.SimpleNamespace(url="https://checkout.stripe.test/session")

        @staticmethod
        def list_line_items(session_id, **kwargs):
            captured["listed_session"] = session_id
            return {"data": captured.get("line_items", [])}

    class PortalSession:
        @staticmethod
        def create(**kwargs):
            captured["portal"] = kwargs
            return types.SimpleNamespace(url="https://billing.stripe.test/portal")

    class Webhook:
        @staticmethod
        def construct_event(body, signature, secret):
            if signature != valid_sig:
                raise SignatureVerificationError("bad signature")
            return json.loads(body)

    class Price:
        @staticmethod
        def retrieve(price_id):
            captured["retrieved_price"] = price_id
            return {
                "id": price_id,
                "metadata": captured.get("price_metadata", {}),
            }

    class Subscription:
        @staticmethod
        def modify(subscription_id, **kwargs):
            captured.setdefault("subscription_modifications", []).append(
                (subscription_id, kwargs)
            )
            return {"id": subscription_id}

    fake.checkout = types.SimpleNamespace(Session=CheckoutSession)
    fake.billing_portal = types.SimpleNamespace(Session=PortalSession)
    fake.Webhook = Webhook
    fake.Price = Price
    fake.Subscription = Subscription
    monkeypatch.setitem(sys.modules, "stripe", fake)
    return fake


@pytest.fixture()
def store():
    value = FakeAccounts()
    value.ensure_account("demo@example.com", "Demo")
    billing.set_account_store(value)
    yield value
    billing.set_account_store(None)


@pytest.fixture()
def client(pg, monkeypatch, store):
    monkeypatch.setattr(settings, "dev_mode", True)
    for email in ("demo@example.com", "a@school.edu", "a@gmail.com"):
        accounts.ensure_account(email, display=email)
        accounts.set_status(email, "active", decided_by="test")
        store.ensure_account(email, email)
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(billing.router)
    return TestClient(app)


@pytest.fixture()
def stripe_config(monkeypatch):
    values = {
        "stripe_secret_key": "sk_test_123",
        "stripe_webhook_secret": "whsec_123",
        "stripe_success_url": "https://app.test/success",
        "stripe_cancel_url": "https://app.test/cancel",
        "stripe_price_desktop": "price_desktop",
        "stripe_price_desktop_edu": "price_desktop_edu",
        "stripe_price_studio": "price_studio",
        "stripe_price_studio_edu": "price_studio_edu",
        "stripe_price_credits_starter": "price_credits_starter",
        "stripe_price_credits_standard": "price_credits_standard",
        "stripe_price_credits_bulk": "price_credits_bulk",
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    return values


def _event(event_id, event_type, obj, *, created=1_700_000_000):
    return {
        "id": event_id,
        "type": event_type,
        "created": created,
        "data": {"object": obj},
    }


def _post_webhook(client, event, signature=VALID_SIG):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["Stripe-Signature"] = signature
    return client.post(
        "/api/billing/webhook", content=json.dumps(event), headers=headers
    )


class TestPlans:
    def test_locked_plan_limits(self):
        assert set(plans.PLANS) == {"trial", "desktop", "studio", "enterprise"}
        trial = plans.limits_for("trial")
        desktop = plans.limits_for("desktop")
        studio = plans.limits_for("studio")
        enterprise = plans.limits_for("enterprise")
        assert (trial.desktop, trial.cloud, trial.storage_quota_bytes) == (
            True,
            True,
            5 * 1024**3,
        )
        assert (desktop.desktop, desktop.cloud, desktop.storage_quota_bytes) == (
            True,
            False,
            5 * 1024**3,
        )
        assert studio.cloud and studio.storage_quota_bytes == 100 * 1024**3
        assert enterprise.cloud and enterprise.storage_quota_bytes == 1024**4
        assert all(p.limits.included_credits_monthly == 0 for p in plans.PLANS.values())
        assert plans.is_purchasable("desktop")
        assert plans.is_purchasable("studio")
        assert not plans.is_purchasable("trial")
        assert not plans.is_purchasable("enterprise")
        with pytest.raises(ValueError):
            plans.limits_for("free")

    def test_plans_module_never_imports_stripe(self):
        assert "import stripe" not in (GATEWAY_DIR / "plans.py").read_text()

    def test_authenticated_plan_catalog_is_ordered_from_server_table(self, client):
        response = client.get("/api/plans", headers=DEV_HEADERS)
        assert response.status_code == 200
        assert response.json()["plans"] == [
            {"id": "trial", "display_name": "Trial", "purchasable": False},
            {"id": "desktop", "display_name": "Desktop", "purchasable": True},
            {"id": "studio", "display_name": "Studio", "purchasable": True},
            {"id": "enterprise", "display_name": "Enterprise", "purchasable": False},
        ]


class TestCheckout:
    def test_server_selects_different_edu_price_for_same_plan(
        self, client, stripe_config, monkeypatch
    ):
        captured = {}
        _install_fake_stripe(monkeypatch, captured)
        for email in ("a@school.edu", "a@gmail.com"):
            response = client.post(
                "/api/billing/checkout",
                json={"plan": "studio", "price_id": "price_evil", "amount": 1},
                headers={"X-Dev-User": email},
            )
            assert response.status_code == 200, response.text
        edu, regular = captured["checkouts"]
        assert edu["line_items"] == [{"price": "price_studio_edu", "quantity": 1}]
        assert regular["line_items"] == [{"price": "price_studio", "quantity": 1}]
        assert edu["metadata"]["chudware_is_edu"] == "true"
        assert regular["metadata"]["chudware_is_edu"] == "false"

    def test_unknown_and_nonpurchasable_plans_never_call_stripe(
        self, client, stripe_config, monkeypatch
    ):
        captured = {}
        _install_fake_stripe(monkeypatch, captured)
        for plan in ("trial", "enterprise", "free", "platinum"):
            response = client.post(
                "/api/billing/checkout", json={"plan": plan}, headers=DEV_HEADERS
            )
            assert response.status_code == 400
        assert "checkouts" not in captured

    def test_credit_pack_checkout_refuses_when_legacy_prices_are_configured(
        self, client, stripe_config, monkeypatch
    ):
        captured = {}
        monkeypatch.setattr(settings, "credit_pack_checkout_enabled", False)
        _install_fake_stripe(monkeypatch, captured)
        response = client.post(
            "/api/billing/credits/checkout",
            json={"pack": "starter", "amount": 1, "price": "price_evil"},
            headers=DEV_HEADERS,
        )
        assert response.status_code == 503
        assert response.json()["detail"] == (
            "credit pack purchases are not available; connect your own AI provider"
        )
        assert "checkouts" not in captured

    def test_existing_customer_reused(self, client, store, stripe_config, monkeypatch):
        captured = {}
        _install_fake_stripe(monkeypatch, captured)
        store.set_stripe_customer("demo@example.com", "cus_existing")
        assert client.post(
            "/api/billing/checkout", json={"plan": "desktop"}, headers=DEV_HEADERS
        ).status_code == 200
        sent = captured["checkouts"][0]
        assert sent["customer"] == "cus_existing"
        assert "customer_email" not in sent


class TestWebhook:
    def test_signature_and_size_checks_precede_mutation(
        self, client, store, stripe_config, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        event = _event(
            "evt_bad",
            "checkout.session.completed",
            {"client_reference_id": "demo@example.com", "metadata": {"chudware_plan": "studio"}},
        )
        assert _post_webhook(client, event, None).status_code == 400
        assert _post_webhook(client, event, "forged").status_code == 400
        assert store.get_account("demo@example.com").plan == "trial"
        oversized = b" " * (billing.MAX_WEBHOOK_BYTES + 1)
        assert client.post(
            "/api/billing/webhook",
            content=oversized,
            headers={"Stripe-Signature": VALID_SIG},
        ).status_code == 413

    def test_checkout_replay_mutates_real_store_exactly_once(
        self, client, stripe_config, monkeypatch
    ):
        from gateway import db

        billing.set_account_store(accounts)
        try:
            accounts.ensure_account("payer@example.com", "Payer")
            _install_fake_stripe(monkeypatch, {})
            event = _event(
                "evt_replay",
                "checkout.session.completed",
                {
                    "id": "cs_subscription",
                    "mode": "subscription",
                    "payment_status": "paid",
                    "client_reference_id": "payer@example.com",
                    "customer": "cus_real",
                    "subscription": "sub_real",
                    "metadata": {"chudware_plan": "studio"},
                },
            )
            assert _post_webhook(client, event).json()["status"] == "processed"
            assert _post_webhook(client, event).json()["status"] == "duplicate"
            assert _post_webhook(client, event).json()["status"] == "duplicate"
            with db.tx() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM processed_events WHERE event_id = %s",
                    ("evt_replay",),
                )
                assert cur.fetchone()["n"] == 1
            account = accounts.get_account("payer@example.com")
            assert (
                account.plan,
                account.status,
                account.stripe_customer_id,
                account.subscription_id,
            ) == ("studio", "pending", "cus_real", "sub_real")
            assert account.notes == ""
            assert account.decided_by == ""
            assert account.decided_at is None
        finally:
            billing.set_account_store(None)

    @pytest.mark.parametrize("status", ["active", "pending", "denied", "suspended"])
    def test_billing_events_preserve_operator_status_and_decision(
        self, client, stripe_config, monkeypatch, status
    ):
        email = "operator-owned@example.com"
        accounts.ensure_account(email, "Operator-owned")
        before = accounts.set_status(
            email, status, decided_by="operator@example.com", notes="reviewed decision"
        )
        decision = (before.status, before.notes, before.decided_by, before.decided_at)
        captured = {"price_metadata": {"chud_credits": "8000"}}
        _install_fake_stripe(monkeypatch, captured)
        sent = []
        monkeypatch.setattr(billing.mailer, "send", lambda *args: sent.append(args))
        billing.set_account_store(accounts)
        try:
            events = [
                (
                    "checkout.session.completed",
                    {
                        "id": "cs_operator",
                        "mode": "subscription",
                        "payment_status": "paid",
                        "client_reference_id": email,
                        "customer": "cus_operator",
                        "subscription": "sub_operator",
                        "metadata": {"chudware_plan": "studio"},
                    },
                    "studio",
                    "active",
                ),
                (
                    "customer.subscription.updated",
                    {
                        "id": "sub_operator",
                        "customer": "cus_operator",
                        "status": "active",
                        "items": {"data": [{"price": {"id": "price_desktop"}}]},
                    },
                    "studio",
                    "active",
                ),
                (
                    "customer.subscription.updated",
                    {
                        "id": "sub_operator",
                        "customer": "cus_operator",
                        "status": "trialing",
                        "items": {"data": [{"price": {"id": "price_studio"}}]},
                    },
                    "studio",
                    "trialing",
                ),
                (
                    "invoice.paid",
                    {
                        "customer": "cus_operator",
                        "subscription": "sub_operator",
                        "lines": {"data": [{
                            "price": {"id": "price_desktop"},
                            "period": {"start": 1_700_000_000, "end": 1_702_592_000},
                        }]},
                    },
                    "desktop",
                    "active",
                ),
                (
                    "invoice.payment_failed",
                    {"customer": "cus_operator", "amount_due": 15000, "currency": "usd"},
                    "desktop",
                    "active",
                ),
                (
                    "checkout.session.completed",
                    {
                        "id": "cs_operator_credits",
                        "mode": "payment",
                        "client_reference_id": email,
                        "payment_status": "paid",
                        "line_items": {"data": [{
                            "price": {"id": "price_credits_starter"},
                        }]},
                    },
                    "desktop",
                    "active",
                ),
                *[
                    (
                        "customer.subscription.updated",
                        {
                            "id": "sub_operator",
                            "customer": "cus_operator",
                            "status": subscription_status,
                            "items": {"data": [{"price": {"id": "price_desktop"}}]},
                        },
                        "trial",
                        subscription_status,
                    )
                    for subscription_status in (
                        "past_due", "unpaid", "canceled", "incomplete_expired"
                    )
                ],
                (
                    "customer.subscription.deleted",
                    {"id": "sub_operator", "customer": "cus_operator"},
                    "trial",
                    "canceled",
                ),
            ]
            for index, (event_type, obj, plan, subscription_status) in enumerate(events):
                event = _event(f"evt_operator_{status}_{index}", event_type, obj)
                response = _post_webhook(client, event)
                assert response.status_code == 200, response.text
                assert response.json()["status"] == "processed"
                after = accounts.get_account(email)
                assert (after.status, after.notes, after.decided_by, after.decided_at) == decision
                assert after.plan == plan
                assert after.stripe_customer_id == "cus_operator"
                assert after.subscription_id == "sub_operator"
                assert after.subscription_status == subscription_status
                assert after.price_id == (
                    "price_studio" if index in (0, 2) else "price_desktop"
                )
                if index >= 3:
                    assert after.subscribed_since == 1_700_000_000
                    assert after.paid_through == 1_702_592_000
                if index >= 5:
                    assert after.credit_balance == 8000
            assert sent[0][0:2] == (email, "payment_failed")
        finally:
            billing.set_account_store(None)

    def test_credit_grant_uses_price_metadata_and_replay_is_noop(
        self, client, stripe_config, monkeypatch
    ):
        from gateway import db

        captured = {"price_metadata": {"chud_credits": "8000"}}
        _install_fake_stripe(monkeypatch, captured)
        event = _event(
            "evt_credits",
            "checkout.session.completed",
            {
                "id": "cs_credits",
                "mode": "payment",
                "payment_status": "paid",
                "client_reference_id": "demo@example.com",
                "metadata": {"chudware_pack": "starter"},
                "line_items": {"data": [{"price": {"id": "price_credits_starter"}}]},
            },
        )
        assert _post_webhook(client, event).json()["status"] == "processed"
        assert _post_webhook(client, event).json()["status"] == "duplicate"
        assert _post_webhook(client, event).json()["status"] == "duplicate"
        with db.tx() as cur:
            cur.execute(
                "SELECT credit_balance FROM accounts WHERE email = %s",
                ("demo@example.com",),
            )
            assert cur.fetchone()["credit_balance"] == 8000
            cur.execute(
                """
                SELECT COUNT(*) AS n, max(credits_delta) AS delta
                  FROM credit_ledger
                 WHERE email = %s AND kind = 'purchase'
                """,
                ("demo@example.com",),
            )
            row = cur.fetchone()
            assert (row["n"], row["delta"]) == (1, 8000)
        assert captured["retrieved_price"] == "price_credits_starter"

    def test_credit_price_without_metadata_is_rejected_and_not_recorded(
        self, client, stripe_config, monkeypatch
    ):
        from gateway import db

        _install_fake_stripe(monkeypatch, {"price_metadata": {}})
        event = _event(
            "evt_bad_pack",
            "checkout.session.completed",
            {
                "id": "cs_bad_pack",
                "mode": "payment",
                "payment_status": "paid",
                "client_reference_id": "demo@example.com",
                "line_items": {"data": [{"price": {"id": "price_credits_starter"}}]},
            },
        )
        assert _post_webhook(client, event).status_code == 400
        with db.tx() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM processed_events WHERE event_id = %s",
                ("evt_bad_pack",),
            )
            assert cur.fetchone()["n"] == 0

    def test_invoice_paid_advances_dates_and_removes_stale_edu_price(
        self, client, store, stripe_config, monkeypatch
    ):
        captured = {}
        _install_fake_stripe(monkeypatch, captured)
        store.set_stripe_customer("demo@example.com", "cus_demo")
        store.set_plan("demo@example.com", "studio")
        event = _event(
            "evt_invoice",
            "invoice.paid",
            {
                "customer": "cus_demo",
                "subscription": "sub_demo",
                "lines": {
                    "data": [
                        {
                            "id": "si_demo",
                            "subscription_item": "si_demo",
                            "price": {"id": "price_studio_edu"},
                            "period": {"start": 1_700_000_000, "end": 1_702_592_000},
                        }
                    ]
                },
            },
        )
        assert _post_webhook(client, event).status_code == 200
        account = store.get_account("demo@example.com")
        assert account.subscribed_since == 1_700_000_000
        assert account.paid_through == 1_702_592_000
        assert account.is_edu is False
        assert account.price_id == "price_studio"
        assert captured["subscription_modifications"] == [
            (
                "sub_demo",
                {
                    "items": [{"id": "si_demo", "price": "price_studio"}],
                    "proration_behavior": "none",
                },
            )
        ]

    def test_payment_failed_sends_mail_without_downgrade(
        self, client, store, stripe_config, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        store.set_stripe_customer("demo@example.com", "cus_demo")
        store.set_plan("demo@example.com", "studio")
        sent = []
        monkeypatch.setattr(billing.mailer, "send", lambda *args: sent.append(args))
        event = _event(
            "evt_failed",
            "invoice.payment_failed",
            {
                "customer": "cus_demo",
                "amount_due": 15000,
                "currency": "usd",
                "next_payment_attempt": 1_700_086_400,
                "hosted_invoice_url": "https://invoice.test/update",
            },
        )
        assert _post_webhook(client, event).status_code == 200
        assert store.get_account("demo@example.com").plan == "studio"
        assert sent[0][0:2] == ("demo@example.com", "payment_failed")

    def test_subscription_deletion_changes_plan_only(
        self, client, store, stripe_config, monkeypatch
    ):
        _install_fake_stripe(monkeypatch, {})
        account = store.get_account("demo@example.com")
        store.set_stripe_customer(account.email, "cus_demo")
        store.set_plan(account.email, "desktop")
        event = _event(
            "evt_deleted",
            "customer.subscription.deleted",
            {"id": "sub_demo", "customer": "cus_demo"},
        )
        assert _post_webhook(client, event).status_code == 200
        account = store.get_account("demo@example.com")
        assert account.plan == "trial"
        assert account.status == "active"


class TestDegradedMode:
    def test_unconfigured_gateway_boots_and_billing_returns_503(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "stripe_secret_key", "")
        monkeypatch.setattr(settings, "stripe_webhook_secret", "")
        assert client.post(
            "/api/billing/checkout", json={"plan": "studio"}, headers=DEV_HEADERS
        ).status_code == 503
        assert client.post("/api/billing/portal", headers=DEV_HEADERS).status_code == 503
        assert client.post("/api/billing/webhook", content=b"{}").status_code == 503
        status = client.get("/api/billing/status", headers=DEV_HEADERS)
        assert status.status_code == 200
        assert status.json()["billing_configured"] is False

    def test_partial_stripe_configuration_fails_at_startup(self, monkeypatch):
        names = (
            "CHUDWARE_WEB_STRIPE_SECRET_KEY",
            "CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET",
            "CHUDWARE_STRIPE_PRICE_DESKTOP",
            "CHUDWARE_STRIPE_PRICE_DESKTOP_EDU",
            "CHUDWARE_STRIPE_PRICE_STUDIO",
            "CHUDWARE_STRIPE_PRICE_STUDIO_EDU",
        )
        for name in names:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CHUDWARE_WEB_STRIPE_SECRET_KEY", "sk_test_partial")
        with pytest.raises(ValueError, match="Stripe billing is partially configured"):
            Settings()


@pytest.mark.parametrize("role", ["user", "admin"])
@pytest.mark.parametrize("status", ["suspended", "denied"])
def test_paid_checkout_after_operator_revocation_cannot_restore_access(
    client, stripe_config, monkeypatch, role, status
):
    from gateway import db

    _install_fake_stripe(monkeypatch, {})
    billing.set_account_store(accounts)
    email = f"revoked-{role}-{status}@example.com"
    accounts.ensure_account(email, email)
    with db.tx() as cur:
        cur.execute("UPDATE accounts SET role=%s WHERE email=%s", (role, email))
    before = accounts.set_status(email, status, decided_by="operator", notes="revoked")
    event = _event("evt_revoked", "checkout.session.completed", {
        "id": "cs_revoked", "mode": "subscription", "payment_status": "paid",
        "client_reference_id": email, "customer": "cus_revoked",
        "subscription": "sub_revoked", "metadata": {"chudware_plan": "studio"},
    })
    assert _post_webhook(client, event).status_code == 200
    after = accounts.get_account(email)
    assert (after.status, after.role, after.notes, after.decided_by, after.decided_at) == (
        status, role, before.notes, before.decided_by, before.decided_at
    )
    assert after.plan == "studio"
    assert client.get("/api/billing/status", headers={"X-Dev-User": email}).status_code == 403


@pytest.mark.parametrize("mode", ["payment", "subscription"])
def test_delayed_checkout_grants_only_once_after_paid_settlement(
    client, stripe_config, monkeypatch, mode
):
    from gateway import db

    _install_fake_stripe(monkeypatch, {"price_metadata": {"chud_credits": "8000"}})
    billing.set_account_store(accounts)
    email = "demo@example.com"
    obj = {
        "id": "cs_delayed", "mode": mode, "payment_status": "unpaid",
        "payment_intent": "pi_delayed", "client_reference_id": email,
        "customer": "cus_delayed", "subscription": "sub_delayed",
        "metadata": {"chudware_plan": "studio", "chudware_pack": "starter"},
        "line_items": {"data": [{"price": {"id": "price_credits_starter"}}]},
    }
    for index, event_type in enumerate((
        "checkout.session.completed", "checkout.session.async_payment_failed",
    )):
        assert _post_webhook(client, _event(f"evt_wait_{index}", event_type, obj)).status_code == 200
        account = accounts.get_account(email)
        assert account.credit_balance == 0
        assert account.plan == "trial"
    if mode == "subscription":
        updated = {
            "id": "sub_delayed", "customer": "cus_delayed", "status": "active",
            "items": {"data": [{"price": {"id": "price_studio"}}]},
        }
        assert _post_webhook(client, _event("evt_active_unpaid", "customer.subscription.updated", updated)).status_code == 200
        assert accounts.get_account(email).plan == "trial"
        assert accounts.get_account(email).subscription_status == "incomplete"
    obj["payment_status"] = "paid"
    for index, event_type in enumerate((
        "checkout.session.async_payment_succeeded", "checkout.session.completed",
        "checkout.session.async_payment_succeeded", "checkout.session.async_payment_failed",
    )):
        assert _post_webhook(client, _event(f"evt_paid_{index}", event_type, obj)).status_code == 200
    account = accounts.get_account(email)
    assert (account.plan, account.credit_balance) == (
        ("trial", 8000) if mode == "payment" else ("studio", 0)
    )
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM billing_checkout_settlements")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM credit_ledger WHERE kind='purchase'")
        assert cur.fetchone()["n"] == (1 if mode == "payment" else 0)


def test_paid_credit_payment_cannot_bind_multiple_checkout_grants(
    client, stripe_config, monkeypatch
):
    _install_fake_stripe(monkeypatch, {"price_metadata": {"chud_credits": "8000"}})
    billing.set_account_store(accounts)
    for index in range(2):
        event = _event(f"evt_binding_{index}", "checkout.session.completed", {
            "id": f"cs_binding_{index}", "mode": "payment", "payment_status": "paid",
            "payment_intent": "pi_same", "client_reference_id": "demo@example.com",
            "line_items": {"data": [{"price": {"id": "price_credits_starter"}}]},
        })
        assert _post_webhook(client, event).status_code == 200
    assert accounts.get_account("demo@example.com").credit_balance == 8000
