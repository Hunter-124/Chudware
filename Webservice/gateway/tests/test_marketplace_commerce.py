"""Marketplace money paths: credit double entry, Connect checkout, webhook
settlement, refunds, tips and payouts.

These are the routes where a bug moves real value, and every one of those bugs
answers 200. An entitlement granted for an ACH session that has not cleared, a
Stripe application fee that disagrees with the fee we recorded, a partial refund
that revokes the buyer's access, a payout that leaves the credits it is paying
out still spendable -- none of them are visible in a status code. So the
assertions here are on consequences: credit_ledger rows, account balances,
mk_orders columns, mk_entitlements rows, and the kwargs actually handed to
Stripe.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

import marketplace_commerce
from db import tx
from marketplace_schema import split_cash, split_credits

STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"

# limits.api_writes is module-level state keyed by email that outlives the
# per-test schema reset, so a file that reuses one address across twenty tests
# eventually gets 429 from the limiter instead of the code under test. Every
# test therefore gets its own set of accounts.
_SEQ = itertools.count(1)


def code_of(response: Any) -> str | None:
    """The stable machine-readable code the storefront branches on, or None.

    MarketplaceError serialises as ``{"detail": {"code": ..., "detail": ...}}``
    while a plain HTTPException puts a string there, so a test that asserts a
    code has to tolerate both shapes to fail for the right reason.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail.get("code") if isinstance(detail, dict) else None


def checkout_event(
    order_id: str,
    listing_id: str,
    *,
    event_id: str,
    payment_status: str = "paid",
    amount_total: int,
    payment_intent: str = "pi_test_settled",
    currency: str = "usd",
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A plain-dict Stripe event, which is what handle_marketplace_event promises
    to accept alongside a real StripeObject."""
    if metadata is None:
        metadata = {
            "chud_kind": "marketplace",
            "chud_order_id": order_id,
            "chud_listing_id": listing_id,
        }
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_fake",
                "object": "checkout.session",
                "payment_status": payment_status,
                "amount_total": amount_total,
                "currency": currency,
                "payment_intent": payment_intent,
                "metadata": metadata,
            }
        },
    }


class Market:
    """One seller, one buyer, one bystander and one operator, all activated."""

    def __init__(self, client, as_user, activate, grant_credits) -> None:
        self.client = client
        self.h = as_user
        self.grant = grant_credits
        tag = next(_SEQ)
        self.seller = f"seller{tag}@commerce.test"
        self.buyer = f"buyer{tag}@commerce.test"
        self.other = f"other{tag}@commerce.test"
        self.admin = f"admin{tag}@commerce.test"
        for email in (self.seller, self.buyer, self.other):
            activate(email)
        activate(self.admin, role="admin")
        response = client.post(
            "/api/studio/seller",
            headers=self.h(self.seller),
            json={"handle": f"acme-{tag}", "display_name": "Acme CAD"},
        )
        assert response.status_code == 200, response.text

    def listing(self, **over: Any) -> str:
        payload: dict[str, Any] = {
            "title": "Commerce Bracket",
            "summary": "A bracket.",
            "description": "Solid.",
            "category_id": "brackets",
            "license_id": "CC-BY-4.0",
            "pricing_kind": "free",
            "origin": "ai_assisted",
        }
        payload.update(over)
        response = self.client.post(
            "/api/studio/listings", headers=self.h(self.seller), json=payload
        )
        assert response.status_code == 200, response.text
        return response.json()["listing"]["id"]

    def publish(self, listing_id: str) -> None:
        """Take a draft all the way to published through the real routes.

        A seller's first paid listing lands in in_review by design, so publishing
        one needs an operator approval; a free one publishes on submit.
        """
        response = self.client.post(
            f"/api/studio/listings/{listing_id}/submit", headers=self.h(self.seller)
        )
        assert response.status_code == 200, response.text
        if response.json()["listing"]["status"] != "published":
            response = self.client.post(
                f"/api/admin/marketplace/listings/{listing_id}/moderate",
                headers=self.h(self.admin),
                json={"action": "approve", "note": "commerce test"},
            )
            assert response.status_code == 200, response.text
        with tx() as cur:
            cur.execute("SELECT status FROM mk_listings WHERE id = %s", (listing_id,))
            assert cur.fetchone()["status"] == "published"

    def enable_connect(self, fake, *, charges: bool = True, payouts: bool = True) -> None:
        """Give the seller a Connect account the fake Stripe reports as live.

        Both halves are needed: _cash_order re-reads the cached mk_sellers
        columns inside its transaction, and refresh_connect overwrites that cache
        from Stripe first. The stub takes the account id POSITIONALLY because
        refresh_connect passes it that way, which the conftest fake's
        keyword-only recorder cannot accept.
        """
        with tx() as cur:
            cur.execute(
                "UPDATE mk_sellers SET connect_account_id = %s,"
                " connect_charges_enabled = %s, connect_payouts_enabled = %s,"
                " connect_details_submitted = TRUE, connect_refreshed_at = now()"
                " WHERE email = %s",
                ("acct_test_seller", charges, payouts, self.seller),
            )

        def retrieve(account_id: str, **kwargs: Any) -> dict[str, Any]:
            fake.calls.append(("Account.retrieve", {"account": account_id, **kwargs}))
            return {
                "id": account_id,
                "charges_enabled": charges,
                "payouts_enabled": payouts,
                "details_submitted": True,
                "capabilities": {"transfers": "active" if charges else "inactive"},
            }

        fake.Account.retrieve = retrieve

    def cash_listing(self, fake, *, price_cents: int = 2500) -> str:
        listing_id = self.listing(
            title="Cash Model",
            pricing_kind="cash",
            price_cents=price_cents,
            license_id="chud-standard-1.0",
            category_id="gears",
        )
        self.publish(listing_id)
        self.enable_connect(fake)
        return listing_id

    def cash_checkout(self, listing_id: str, *, buyer: str | None = None) -> str:
        response = self.client.post(
            f"/api/marketplace/listings/{listing_id}/checkout",
            headers=self.h(buyer or self.buyer),
            json={"currency": "usd"},
        )
        assert response.status_code == 200, response.text
        return response.json()["order_id"]

    def buy_credits(self, listing_id: str, *, buyer: str | None = None):
        return self.client.post(
            f"/api/marketplace/listings/{listing_id}/checkout",
            headers=self.h(buyer or self.buyer),
            json={"currency": "credits"},
        )

    def tip(self, listing_id: str, credits: int, *, tipper: str | None = None):
        return self.client.post(
            f"/api/marketplace/listings/{listing_id}/tip",
            headers=self.h(tipper or self.buyer),
            json={"currency": "credits", "amount_credits": credits},
        )


@pytest.fixture()
def market(client, as_user, activate, grant_credits) -> Market:
    return Market(client, as_user, activate, grant_credits)


def _balances(*emails: str) -> dict[str, int]:
    with tx() as cur:
        cur.execute(
            "SELECT email, credit_balance FROM accounts WHERE email = ANY(%s)",
            (list(emails),),
        )
        return {row["email"]: int(row["credit_balance"]) for row in cur.fetchall()}


def _entitlement_count(listing_id: str, email: str) -> int:
    with tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM mk_entitlements"
            " WHERE listing_id = %s AND email = %s",
            (listing_id, email),
        )
        return int(cur.fetchone()["n"])


def _order(order_id: str) -> dict[str, Any]:
    with tx() as cur:
        cur.execute("SELECT * FROM mk_orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
    assert row is not None, f"order {order_id} vanished"
    return dict(row)


# --- 1. credit purchase double entry -----------------------------------------


def test_credit_purchase_writes_one_double_entry_and_moves_both_balances(market):
    listing_id = market.listing(
        title="Paid Gearbox",
        pricing_kind="credits",
        price_credits=5000,
        license_id="chud-standard-1.0",
        category_id="gears",
    )
    market.publish(listing_id)
    market.grant(market.buyer, 20000)

    response = market.buy_credits(listing_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "paid"
    assert body["balance_after"] == 15000

    fee, net = split_credits(5000, 1000)
    assert (fee, net) == (500, 4500)

    with tx() as cur:
        cur.execute("SELECT * FROM mk_orders WHERE listing_id = %s", (listing_id,))
        orders = cur.fetchall()
        assert len(orders) == 1
        order = orders[0]
        assert order["currency"] == "credits"
        assert order["kind"] == "purchase"
        assert order["status"] == "paid"
        assert order["paid_at"] is not None
        assert int(order["amount_credits"]) == 5000
        assert int(order["fee_credits"]) == fee
        assert int(order["fee_credits"]) + int(order["net_credits"]) == int(
            order["amount_credits"]
        )
        # A credits order must leave the cash columns at zero or every SUM() over
        # mk_orders silently mixes units.
        assert int(order["amount_cents"]) == 0

        cur.execute(
            "SELECT kind, email, credits_delta FROM credit_ledger"
            " WHERE kind IN ('market_buy', 'market_earn') ORDER BY kind"
        )
        assert [
            (row["kind"], row["email"], int(row["credits_delta"]))
            for row in cur.fetchall()
        ] == [
            ("market_buy", market.buyer, -5000),
            ("market_earn", market.seller, 4500),
        ]

        cur.execute("SELECT sales FROM mk_listings WHERE id = %s", (listing_id,))
        assert int(cur.fetchone()["sales"]) == 1

    balances = _balances(market.buyer, market.seller)
    assert balances[market.buyer] == 15000
    # The seller's cut lands as spendable AI credits, which is the product's
    # central claim about selling here; a ledger row without the balance move
    # would make that claim false while every response still said 'paid'.
    assert balances[market.seller] == 4500
    assert _entitlement_count(listing_id, market.buyer) == 1


# --- 2. purchase refusals -----------------------------------------------------


def test_buying_the_same_listing_twice_is_refused_as_already_owned(market):
    listing_id = market.listing(
        title="Paid Gearbox",
        pricing_kind="credits",
        price_credits=5000,
        license_id="chud-standard-1.0",
        category_id="gears",
    )
    market.publish(listing_id)
    market.grant(market.buyer, 20000)
    assert market.buy_credits(listing_id).status_code == 200

    response = market.buy_credits(listing_id)
    assert response.status_code == 409
    assert code_of(response) == "already_owned"
    with tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM mk_orders WHERE listing_id = %s", (listing_id,)
        )
        assert int(cur.fetchone()["n"]) == 1
    assert _balances(market.buyer)[market.buyer] == 15000


def test_seller_cannot_buy_their_own_listing(market):
    listing_id = market.listing(
        title="Paid Gearbox",
        pricing_kind="credits",
        price_credits=5000,
        license_id="chud-standard-1.0",
        category_id="gears",
    )
    market.publish(listing_id)
    market.grant(market.seller, 20000)

    response = market.buy_credits(listing_id, buyer=market.seller)
    assert response.status_code == 409
    assert code_of(response) == "self_purchase"
    assert _balances(market.seller)[market.seller] == 20000


def test_purchase_over_the_balance_is_402_insufficient_credits(market):
    listing_id = market.listing(
        title="Paid Gearbox",
        pricing_kind="credits",
        price_credits=5000,
        license_id="chud-standard-1.0",
        category_id="gears",
    )
    market.publish(listing_id)
    market.grant(market.other, 100)

    response = market.buy_credits(listing_id, buyer=market.other)
    assert response.status_code == 402
    assert code_of(response) == "insufficient_credits"
    assert _balances(market.other)[market.other] == 100
    assert _entitlement_count(listing_id, market.other) == 0


def test_free_listing_is_not_purchasable_in_either_currency(market, fake_stripe):
    listing_id = market.listing(title="Free Bracket")
    market.publish(listing_id)
    market.grant(market.buyer, 20000)

    credits_response = market.buy_credits(listing_id)
    assert credits_response.status_code == 409
    assert code_of(credits_response) == "not_purchasable"

    # Connect is enabled before the cash attempt on purpose. _cash_order checks
    # the seller's payout readiness BEFORE it checks whether the listing carries
    # a cash price at all, so on a seller without Connect the same refusal
    # arrives as 'connect_incomplete' and this test would be pinning the wrong
    # reason to the wrong cause.
    market.enable_connect(fake_stripe)
    cash_response = market.client.post(
        f"/api/marketplace/listings/{listing_id}/checkout",
        headers=market.h(market.buyer),
        json={"currency": "usd"},
    )
    assert cash_response.status_code == 409
    assert code_of(cash_response) == "not_purchasable"
    assert _balances(market.buyer)[market.buyer] == 20000
    with tx() as cur:
        cur.execute("SELECT count(*) AS n FROM mk_orders")
        assert int(cur.fetchone()["n"]) == 0


# --- 3. cash checkout requires Connect ---------------------------------------


def test_cash_checkout_refused_until_connect_charges_are_enabled(market, fake_stripe):
    listing_id = market.listing(
        title="Cash Model",
        pricing_kind="cash",
        price_cents=2500,
        license_id="chud-standard-1.0",
        category_id="gears",
    )
    market.publish(listing_id)

    response = market.client.post(
        f"/api/marketplace/listings/{listing_id}/checkout",
        headers=market.h(market.buyer),
        json={"currency": "usd"},
    )
    assert response.status_code == 409
    assert code_of(response) == "connect_incomplete"
    # Refusing before Stripe is touched matters: a Checkout Session created for a
    # seller who cannot be paid would take the buyer's money into a dead end.
    assert [name for name, _ in fake_stripe.calls if name.startswith("checkout.")] == []
    with tx() as cur:
        cur.execute("SELECT count(*) AS n FROM mk_orders")
        assert int(cur.fetchone()["n"]) == 0


def test_cash_checkout_passes_an_idempotency_key_and_our_own_fee_to_stripe(
    market, fake_stripe
):
    price_cents = 2500
    listing_id = market.cash_listing(fake_stripe, price_cents=price_cents)
    order_id = market.cash_checkout(listing_id)

    creates = [kw for name, kw in fake_stripe.calls if name == "checkout.Session.create"]
    assert len(creates) == 1
    call = creates[0]
    # Without the key, a retried checkout charges the buyer twice for one order.
    assert call["idempotency_key"] == f"marketplace-checkout-{order_id}"
    expected_fee, expected_net = split_cash(price_cents, 1500)
    assert (expected_fee, expected_net) == (375, 2125)
    # The fee Stripe is told to take must equal the fee we recorded, or our books
    # and the money diverge with nothing to reconcile them against.
    assert call["payment_intent_data"]["application_fee_amount"] == expected_fee
    assert (
        call["payment_intent_data"]["transfer_data"]["destination"]
        == "acct_test_seller"
    )
    assert call["line_items"][0]["price_data"]["unit_amount"] == price_cents
    assert call["metadata"]["chud_order_id"] == order_id
    assert call["metadata"]["chud_kind"] == "marketplace"

    order = _order(order_id)
    assert order["status"] == "pending"
    assert int(order["fee_cents"]) == expected_fee
    assert int(order["net_cents"]) == expected_net
    assert order["stripe_checkout_session"] == "cs_test_fake"
    # Nothing is owned until Stripe says the money arrived.
    assert _entitlement_count(listing_id, market.buyer) == 0


# --- 4. webhook settlement ---------------------------------------------------


def test_unpaid_checkout_session_is_consumed_without_granting_anything(
    market, fake_stripe
):
    """The ACH guard.

    checkout.session.completed fires for delayed payment methods before the
    money moves, with payment_status='unpaid'. Treating 'completed' as 'paid'
    hands over a paid model for a bank debit that may never clear. The event must
    still be consumed (True) so Stripe stops retrying it.
    """
    listing_id = market.cash_listing(fake_stripe)
    order_id = market.cash_checkout(listing_id)

    consumed = marketplace_commerce.handle_marketplace_event(
        checkout_event(
            order_id,
            listing_id,
            event_id="evt_unpaid",
            payment_status="unpaid",
            amount_total=2500,
        )
    )
    assert consumed is True

    order = _order(order_id)
    assert order["status"] == "pending"
    assert order["paid_at"] is None
    assert order["stripe_payment_intent"] == ""
    assert _entitlement_count(listing_id, market.buyer) == 0


def test_paid_checkout_session_settles_the_order_and_grants_the_entitlement(
    market, fake_stripe
):
    listing_id = market.cash_listing(fake_stripe)
    order_id = market.cash_checkout(listing_id)

    assert marketplace_commerce.handle_marketplace_event(
        checkout_event(
            order_id,
            listing_id,
            event_id="evt_paid",
            amount_total=2500,
            payment_intent="pi_test_paid",
        )
    ) is True

    order = _order(order_id)
    assert order["status"] == "paid"
    assert order["paid_at"] is not None
    assert order["stripe_payment_intent"] == "pi_test_paid"
    assert order["stripe_event_id"] == "evt_paid"
    assert _entitlement_count(listing_id, market.buyer) == 1
    with tx() as cur:
        cur.execute("SELECT sales FROM mk_listings WHERE id = %s", (listing_id,))
        assert int(cur.fetchone()["sales"]) == 1


def test_checkout_session_with_a_mismatched_amount_grants_nothing(market, fake_stripe):
    """A session whose total is not the order's total is not this order's payment.

    Trusting the metadata alone would let a cheaper session settle an expensive
    order.
    """
    listing_id = market.cash_listing(fake_stripe)
    order_id = market.cash_checkout(listing_id)

    assert marketplace_commerce.handle_marketplace_event(
        checkout_event(
            order_id,
            listing_id,
            event_id="evt_mismatch",
            amount_total=100,
            payment_intent="pi_test_mismatch",
        )
    ) is True

    order = _order(order_id)
    assert order["status"] == "pending"
    assert order["stripe_payment_intent"] == ""
    assert _entitlement_count(listing_id, market.buyer) == 0


def test_replaying_a_checkout_event_grants_exactly_one_entitlement(market, fake_stripe):
    listing_id = market.cash_listing(fake_stripe)
    order_id = market.cash_checkout(listing_id)
    event = checkout_event(
        order_id,
        listing_id,
        event_id="evt_replayed",
        amount_total=2500,
        payment_intent="pi_test_replayed",
    )

    assert marketplace_commerce.handle_marketplace_event(event) is True
    assert marketplace_commerce.handle_marketplace_event(event) is True

    with tx() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM mk_orders WHERE listing_id = %s", (listing_id,)
        )
        assert int(cur.fetchone()["n"]) == 1
        cur.execute(
            "SELECT count(*) AS n FROM mk_entitlements WHERE listing_id = %s",
            (listing_id,),
        )
        assert int(cur.fetchone()["n"]) == 1
        # A second sale counted for one payment is the visible symptom of a
        # non-idempotent settlement.
        cur.execute("SELECT sales FROM mk_listings WHERE id = %s", (listing_id,))
        assert int(cur.fetchone()["sales"]) == 1


def test_event_without_marketplace_metadata_is_left_for_billing(market, fake_stripe):
    """False, not True: billing.py hands every event here first and uses the
    return value to decide whether its own subscription handling still runs."""
    listing_id = market.cash_listing(fake_stripe)
    order_id = market.cash_checkout(listing_id)

    consumed = marketplace_commerce.handle_marketplace_event(
        checkout_event(
            order_id,
            listing_id,
            event_id="evt_subscription",
            amount_total=2500,
            metadata={},
        )
    )
    assert consumed is False
    assert _order(order_id)["status"] == "pending"
    assert _entitlement_count(listing_id, market.buyer) == 0


# --- 5. partial vs full refund ----------------------------------------------


def _refund(market, *, payment_intent: str, amount_cents: int | None = None):
    body: dict[str, Any] = {
        "payment_intent": payment_intent,
        "confirm": payment_intent,
        "reason": "requested_by_customer",
    }
    if amount_cents is not None:
        body["amount_cents"] = amount_cents
    return market.client.post(
        "/api/admin/billing/refund", headers=market.h(market.admin), json=body
    )


def test_partial_refund_keeps_the_order_paid_and_the_buyer_entitled(market, fake_stripe):
    """A partial refund is a discount, not a revocation.

    Collapsing 'some money returned' into status='refunded' would delete the
    entitlement and take back a model the buyer still paid for.
    """
    listing_id = market.cash_listing(fake_stripe, price_cents=2500)
    order_id = market.cash_checkout(listing_id)
    marketplace_commerce.handle_marketplace_event(
        checkout_event(
            order_id,
            listing_id,
            event_id="evt_refundable",
            amount_total=2500,
            payment_intent="pi_test_refundable",
        )
    )
    assert _entitlement_count(listing_id, market.buyer) == 1

    partial = _refund(market, payment_intent="pi_test_refundable", amount_cents=1000)
    assert partial.status_code == 200, partial.text
    assert partial.json()["kind"] == "marketplace"
    order = _order(order_id)
    assert int(order["refunded_cents"]) == 1000
    assert order["status"] == "paid"
    assert _entitlement_count(listing_id, market.buyer) == 1

    remainder = _refund(market, payment_intent="pi_test_refundable", amount_cents=1500)
    assert remainder.status_code == 200, remainder.text
    order = _order(order_id)
    assert int(order["refunded_cents"]) == 2500
    assert order["status"] == "refunded"
    assert order["refunded_at"] is not None
    assert _entitlement_count(listing_id, market.buyer) == 0

    refund_calls = [kw for name, kw in fake_stripe.calls if name == "Refund.create"]
    assert [kw["amount"] for kw in refund_calls] == [1000, 1500]
    # Distinct keys per cumulative total: reusing one would make the second
    # refund a no-op replay of the first at Stripe.
    assert len({kw["idempotency_key"] for kw in refund_calls}) == 2


def test_refund_beyond_the_remaining_amount_is_a_clean_4xx(market, fake_stripe):
    listing_id = market.cash_listing(fake_stripe, price_cents=2500)
    order_id = market.cash_checkout(listing_id)
    marketplace_commerce.handle_marketplace_event(
        checkout_event(
            order_id,
            listing_id,
            event_id="evt_overrefund",
            amount_total=2500,
            payment_intent="pi_test_overrefund",
        )
    )
    assert _refund(
        market, payment_intent="pi_test_overrefund", amount_cents=1000
    ).status_code == 200

    over = _refund(market, payment_intent="pi_test_overrefund", amount_cents=2000)
    # 400 from the route, not a 500 from mk_orders_refunds_within_amount.
    assert over.status_code == 400, over.text
    assert code_of(over) == "refund_exceeds_remaining"
    order = _order(order_id)
    assert int(order["refunded_cents"]) == 1000
    assert order["status"] == "paid"
    # The refusal must happen before Stripe is asked to move money.
    assert [
        kw["amount"] for name, kw in fake_stripe.calls if name == "Refund.create"
    ] == [1000]


# --- 6. tips -----------------------------------------------------------------


def test_credit_tip_splits_the_fee_and_grants_access(market):
    listing_id = market.listing(title="Free Bracket")
    market.publish(listing_id)
    market.grant(market.buyer, 5000)

    response = market.tip(listing_id, 1000)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "paid"

    fee, net = split_credits(1000, 1000)
    assert (fee, net) == (100, 900)
    order = None
    with tx() as cur:
        cur.execute("SELECT * FROM mk_orders WHERE listing_id = %s", (listing_id,))
        rows = cur.fetchall()
        assert len(rows) == 1
        order = dict(rows[0])
    assert order["kind"] == "tip"
    assert int(order["fee_credits"]) == fee
    assert int(order["net_credits"]) == net

    balances = _balances(market.buyer, market.seller)
    assert balances[market.buyer] == 4000
    assert balances[market.seller] == net
    # A tip on a free listing is also how a supporter takes delivery of it.
    assert _entitlement_count(listing_id, market.buyer) == 1


def test_self_tip_is_refused(market):
    listing_id = market.listing(title="Free Bracket")
    market.publish(listing_id)
    market.grant(market.seller, 5000)

    response = market.tip(listing_id, 1000, tipper=market.seller)
    assert response.status_code == 409
    assert code_of(response) == "self_purchase"
    assert _balances(market.seller)[market.seller] == 5000


def test_tips_repeat_where_a_purchase_cannot(market):
    listing_id = market.listing(title="Free Bracket")
    market.publish(listing_id)
    market.grant(market.buyer, 5000)

    assert market.tip(listing_id, 1000).status_code == 200
    second = market.tip(listing_id, 500)
    assert second.status_code == 200, second.text

    with tx() as cur:
        cur.execute(
            "SELECT amount_credits FROM mk_orders WHERE listing_id = %s"
            " AND kind = 'tip' ORDER BY amount_credits DESC",
            (listing_id,),
        )
        assert [int(row["amount_credits"]) for row in cur.fetchall()] == [1000, 500]
    balances = _balances(market.buyer, market.seller)
    assert balances[market.buyer] == 3500
    assert balances[market.seller] == 900 + 450


# --- 7. payout requests ------------------------------------------------------


def _payout(market, credits: int):
    return market.client.post(
        "/api/studio/payouts", headers=market.h(market.seller), json={"credits": credits}
    )


def test_payout_below_the_minimum_is_refused(market, fake_stripe, marketplace_env):
    market.enable_connect(fake_stripe)
    market.grant(market.seller, 60000)

    response = _payout(market, marketplace_env.marketplace_payout_min_credits - 1)
    assert response.status_code == 400
    assert code_of(response) == "payout_too_small"
    assert _balances(market.seller)[market.seller] == 60000
    with tx() as cur:
        cur.execute("SELECT count(*) AS n FROM mk_payout_requests")
        assert int(cur.fetchone()["n"]) == 0


def test_payout_debits_the_balance_immediately(market, fake_stripe, marketplace_env):
    """The debit happens when the request opens, not when it is approved.

    Leaving the credits spendable while a transfer is pending lets the same
    balance be cashed out and spent on AI usage.
    """
    market.enable_connect(fake_stripe)
    market.grant(market.seller, 60000)
    amount = marketplace_env.marketplace_payout_min_credits

    response = _payout(market, amount)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["credits"] == amount
    assert body["cents"] == amount * 100 // marketplace_env.marketplace_credits_per_dollar
    assert body["balance_after"] == 60000 - amount
    assert _balances(market.seller)[market.seller] == 60000 - amount

    with tx() as cur:
        cur.execute("SELECT * FROM mk_payout_requests")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
        assert int(rows[0]["credits"]) == amount
        cur.execute(
            "SELECT credits_delta FROM credit_ledger WHERE kind = 'market_payout'"
        )
        assert [int(row["credits_delta"]) for row in cur.fetchall()] == [-amount]


def test_second_open_payout_request_is_refused(market, fake_stripe, marketplace_env):
    market.enable_connect(fake_stripe)
    market.grant(market.seller, 60000)
    amount = marketplace_env.marketplace_payout_min_credits
    assert _payout(market, amount).status_code == 200

    response = _payout(market, amount)
    assert response.status_code == 409
    assert code_of(response) == "payout_pending"
    # One open request only, and the refused one must not have debited anything.
    assert _balances(market.seller)[market.seller] == 60000 - amount
    with tx() as cur:
        cur.execute("SELECT count(*) AS n FROM mk_payout_requests")
        assert int(cur.fetchone()["n"]) == 1
        cur.execute(
            "SELECT count(*) AS n FROM credit_ledger WHERE kind = 'market_payout'"
        )
        assert int(cur.fetchone()["n"]) == 1


def test_recoverable_card_failure_then_paid_same_checkout_settles_once(market, fake_stripe):
    listing_id = market.cash_listing(fake_stripe)
    order_id = market.cash_checkout(listing_id)
    failed = {
        "id": "evt_attempt_failed", "type": "payment_intent.payment_failed",
        "data": {"object": {
            "id": "pi_retry", "last_payment_error": {"message": "card declined"},
            "metadata": {"chud_kind": "marketplace", "chud_order_id": order_id},
        }},
    }
    assert marketplace_commerce.handle_marketplace_event(failed)
    assert _order(order_id)["status"] == "pending"
    assert _order(order_id)["failure_reason"] == "card declined"
    assert _entitlement_count(listing_id, market.buyer) == 0
    event = checkout_event(
        order_id, listing_id, event_id="evt_retry_paid",
        amount_total=2500, payment_intent="pi_retry",
    )
    for event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        event["type"] = event_type
        event["id"] = "evt_retry_" + event_type
        assert marketplace_commerce.handle_marketplace_event(event)
    order = _order(order_id)
    assert order["status"] == "paid"
    assert order["stripe_payment_intent"] == "pi_retry"
    assert order["failure_reason"] == ""
    assert _entitlement_count(listing_id, market.buyer) == 1
    with tx() as cur:
        cur.execute("SELECT sales FROM mk_listings WHERE id=%s", (listing_id,))
        assert cur.fetchone()["sales"] == 1
    # A stale attempt/async failure must not destroy a verified paid settlement.
    assert marketplace_commerce.handle_marketplace_event(failed)
    event["type"] = "checkout.session.async_payment_failed"
    event["id"] = "evt_stale_async_failure"
    assert marketplace_commerce.handle_marketplace_event(event)
    assert _order(order_id)["status"] == "paid"
    assert _entitlement_count(listing_id, market.buyer) == 1


def test_paid_checkout_must_match_recorded_checkout_not_just_order_metadata(market, fake_stripe):
    listing_id = market.cash_listing(fake_stripe)
    order_id = market.cash_checkout(listing_id)
    event = checkout_event(
        order_id, listing_id, event_id="evt_wrong_checkout", amount_total=2500,
    )
    event["data"]["object"]["id"] = "cs_other_checkout"
    assert marketplace_commerce.handle_marketplace_event(event)
    assert _order(order_id)["status"] == "pending"
    assert _entitlement_count(listing_id, market.buyer) == 0


def test_legacy_failed_order_recovers_only_from_its_bound_paid_checkout(market, fake_stripe):
    listing_id = market.cash_listing(fake_stripe)
    order_id = market.cash_checkout(listing_id)
    with tx() as cur:
        cur.execute(
            "UPDATE mk_orders SET status='failed', failure_reason='card declined' WHERE id=%s",
            (order_id,),
        )
    event = checkout_event(order_id, listing_id, event_id="evt_legacy_paid", amount_total=2500)
    event["data"]["object"]["id"] = "cs_other"
    assert marketplace_commerce.handle_marketplace_event(event)
    assert _order(order_id)["status"] == "failed"
    event["data"]["object"]["id"] = "cs_test_fake"
    event["id"] = "evt_legacy_bound_paid"
    assert marketplace_commerce.handle_marketplace_event(event)
    assert _order(order_id)["status"] == "paid"
    assert _entitlement_count(listing_id, market.buyer) == 1
