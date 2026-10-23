"""Marketplace orders, Stripe Connect settlement, and seller payouts.

Consumes these signed Stripe event types for marketplace resources:
``checkout.session.completed``, ``checkout.session.async_payment_succeeded``,
``checkout.session.async_payment_failed``, ``checkout.session.expired``,
``payment_intent.payment_failed``, ``charge.refunded``, and
``account.updated``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import httpx
import psycopg
from fastapi import APIRouter, Request
from pydantic import BaseModel

try:
    from .config import settings
    from .db import tx
    from .limits import api_writes
    from .marketplace import bump_counter, get_seller, listing_card, load_listing
    from .marketplace_authz import MarketplaceError, marketplace_enabled_or_404, require_user
    from .marketplace_schema import (
        MAX_CASH_PRICE_CENTS,
        MAX_CREDIT_PRICE,
        MIN_CASH_PRICE_CENTS,
        MIN_CREDIT_PRICE,
        PRICING_KINDS_WITH_CASH,
        PRICING_KINDS_WITH_CREDITS,
        CursorError,
        credits_to_cents,
        decode_cursor,
        encode_cursor,
        split_cash,
        split_credits,
    )
except ImportError:  # uvicorn app:app from the gateway directory
    from config import settings
    from db import tx
    from limits import api_writes
    from marketplace import bump_counter, get_seller, listing_card, load_listing
    from marketplace_authz import MarketplaceError, marketplace_enabled_or_404, require_user
    from marketplace_schema import (
        MAX_CASH_PRICE_CENTS,
        MAX_CREDIT_PRICE,
        MIN_CASH_PRICE_CENTS,
        MIN_CREDIT_PRICE,
        PRICING_KINDS_WITH_CASH,
        PRICING_KINDS_WITH_CREDITS,
        CursorError,
        credits_to_cents,
        decode_cursor,
        encode_cursor,
        split_cash,
        split_credits,
    )

log = logging.getLogger("chudware.gateway.marketplace.commerce")
router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])
studio_router = APIRouter(prefix="/api/studio", tags=["marketplace-studio"])


class CheckoutBody(BaseModel):
    currency: str


class TipBody(BaseModel):
    currency: str
    amount_cents: int | None = None
    amount_credits: int | None = None


class PayoutBody(BaseModel):
    credits: int


def _stripe():
    try:
        import stripe
    except ModuleNotFoundError as exc:
        raise MarketplaceError(503, "stripe_unconfigured", "billing is unavailable") from exc
    if not settings.stripe_secret_key:
        raise MarketplaceError(503, "stripe_unconfigured", "billing is not configured")
    stripe.api_key = settings.stripe_secret_key
    return stripe


def _v2_request(path: str, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
    """Use raw v2 only until the pinned stripe-python exposes core account bindings."""
    if not settings.stripe_secret_key:
        raise MarketplaceError(503, "stripe_unconfigured", "billing is not configured")
    try:
        response = httpx.post(
            f"https://api.stripe.com/v2/core/{path.lstrip('/')}",
            headers={
                "Stripe-Version": "2026-08-26.dahlia",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            auth=(settings.stripe_secret_key, ""),
            json=dict(payload),
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        log.warning("Stripe v2 request failed for %s", path, exc_info=True)
        raise MarketplaceError(502, "stripe_error", "Stripe Connect is unavailable") from exc
    if not 200 <= response.status_code < 300:
        log.warning("Stripe v2 %s returned HTTP %s", path, response.status_code)
        raise MarketplaceError(502, "stripe_error", "Stripe Connect rejected the request")
    data = response.json()
    if not isinstance(data, dict):
        raise MarketplaceError(502, "stripe_error", "Stripe returned an invalid response")
    return data


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text_id(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(_field(value, "id", "") or "") if value else ""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _plain(value: Any) -> Any:
    """Normalise a Stripe SDK object into ordinary nested Python.

    ``StripeObject`` in stripe-python 15 is not a dict and does not register as a
    ``Mapping``: it has no ``.get()``, ``dict(obj)`` fails, and
    ``isinstance(obj, Mapping)`` is False. Its ``__str__`` is its JSON body, which
    is the only public recursive conversion the SDK offers.

    This is called ONCE, on the event, at the top of
    :func:`handle_marketplace_event`, so that everything downstream reasons about
    plain dicts. The alternative -- being careful at each of the dozen read sites
    -- is how the original version of this module shipped a bug that was worse
    than a crash: ``_event_metadata`` guarded on ``isinstance(metadata, Mapping)``,
    a live ``metadata`` is a ``StripeObject``, so it returned ``{}``, so
    ``metadata["chud_kind"] != "marketplace"`` was always true, so EVERY
    marketplace webhook returned False and fell through to the subscription
    handler. No exception, no log line, no settled order. Verified against a real
    signed event before and after.
    """
    if isinstance(value, Mapping):
        return value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (ValueError, TypeError):
        return value


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _uuid() -> str:
    return str(uuid.uuid4())


def _ledger_note(**values: Any) -> str:
    return json.dumps(values, separators=(",", ":"), sort_keys=True)


def _require_studio_user(request: Request) -> tuple[Any, dict[str, Any]]:
    user = require_user(request)
    seller = get_seller(user.email.strip().lower())
    if seller is None:
        raise MarketplaceError(403, "not_a_seller", "open a seller account first")
    return user, seller


def _require_admin_actor(cur: Any, actor_email: str) -> None:
    cur.execute("SELECT role FROM accounts WHERE email = %s", (actor_email.strip().lower(),))
    actor = cur.fetchone()
    if actor is None or actor["role"] != "admin":
        raise MarketplaceError(403, "admin_only", "admin only")


def _listing_for_money(listing_id: str, buyer_email: str) -> dict[str, Any]:
    listing = load_listing(listing_id)
    if listing is None or listing["status"] != "published":
        raise MarketplaceError(404, "not_purchasable", "listing is not available")
    if listing["seller_email"].strip().lower() == buyer_email:
        raise MarketplaceError(409, "self_purchase", "you cannot pay your own listing")
    return listing


def _purchase_price(listing: Mapping[str, Any], currency: str) -> int:
    if currency == "usd" and listing["pricing_kind"] in PRICING_KINDS_WITH_CASH:
        amount = int(listing["price_cents"])
        if amount > 0:
            return amount
    if currency == "credits" and listing["pricing_kind"] in PRICING_KINDS_WITH_CREDITS:
        amount = int(listing["price_credits"])
        if amount > 0:
            return amount
    raise MarketplaceError(409, "not_purchasable", f"this listing is not priced in {currency}")


def _tip_amount(body: TipBody) -> tuple[str, int]:
    currency = body.currency.strip().lower()
    if currency == "usd":
        if body.amount_credits not in (None, 0) or body.amount_cents is None:
            raise MarketplaceError(400, "invalid_tip", "provide only amount_cents for a USD tip")
        amount = int(body.amount_cents)
        if not MIN_CASH_PRICE_CENTS <= amount <= MAX_CASH_PRICE_CENTS:
            raise MarketplaceError(400, "invalid_tip", "USD tip amount is out of range")
        return currency, amount
    if currency == "credits":
        if body.amount_cents not in (None, 0) or body.amount_credits is None:
            raise MarketplaceError(400, "invalid_tip", "provide only amount_credits for a credit tip")
        amount = int(body.amount_credits)
        if not MIN_CREDIT_PRICE <= amount <= MAX_CREDIT_PRICE:
            raise MarketplaceError(400, "invalid_tip", "credit tip amount is out of range")
        return currency, amount
    raise MarketplaceError(400, "invalid_currency", "currency must be usd or credits")


def _current_version_id(cur: Any, listing_id: str) -> str | None:
    cur.execute("SELECT id FROM mk_versions WHERE listing_id = %s AND is_current", (listing_id,))
    row = cur.fetchone()
    return str(row["id"]) if row else None


def _entitlement_exists(cur: Any, listing_id: str, email: str) -> bool:
    cur.execute(
        "SELECT 1 FROM mk_entitlements WHERE listing_id = %s AND email = %s",
        (listing_id, email),
    )
    return cur.fetchone() is not None


def _grant_entitlement(cur: Any, listing_id: str, email: str, order_id: str, source: str) -> None:
    cur.execute(
        """
        INSERT INTO mk_entitlements (listing_id, email, order_id, source)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (listing_id, email) DO UPDATE
        SET order_id = EXCLUDED.order_id, source = EXCLUDED.source, granted_at = now()
        """,
        (listing_id, email, order_id, source),
    )


def _credit_order(
    listing_id: str,
    buyer_email: str,
    *,
    kind: str,
    amount: int | None = None,
) -> dict[str, Any]:
    order_id = _uuid()
    buyer_email = buyer_email.strip().lower()
    with tx() as cur:
        listing = _listing_for_money(listing_id, buyer_email)
        listing_uuid = str(listing["id"])
        if kind == "purchase":
            if _entitlement_exists(cur, listing_uuid, buyer_email):
                raise MarketplaceError(409, "already_owned", "you already own this listing")
            amount_credits = _purchase_price(listing, "credits")
        else:
            if not listing["tips_enabled"]:
                raise MarketplaceError(409, "tips_disabled", "tips are disabled for this listing")
            if amount is None:
                raise MarketplaceError(400, "invalid_tip", "tip amount is required")
            amount_credits = amount
        fee_credits, net_credits = split_credits(
            amount_credits, settings.marketplace_credit_fee_bps
        )
        seller_email = listing["seller_email"].strip().lower()
        locked_emails = sorted((buyer_email, seller_email))
        cur.execute(
            """
            SELECT email, credit_balance FROM accounts
            WHERE email = ANY(%s) ORDER BY email FOR UPDATE
            """,
            (locked_emails,),
        )
        balances = {row["email"]: int(row["credit_balance"]) for row in cur.fetchall()}
        if buyer_email not in balances or seller_email not in balances:
            raise MarketplaceError(409, "account_unavailable", "an account is unavailable")
        if balances[buyer_email] < amount_credits:
            raise MarketplaceError(
                402,
                "insufficient_credits",
                "your credit balance is too low for this payment",
            )
        cur.execute(
            """
            INSERT INTO mk_orders (
                id, listing_id, version_id, buyer_email, seller_email, kind,
                currency, amount_credits, fee_credits, net_credits, status, paid_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'credits', %s, %s, %s, 'paid', now())
            """,
            (
                order_id,
                listing_uuid,
                _current_version_id(cur, listing_uuid),
                buyer_email,
                seller_email,
                kind,
                amount_credits,
                fee_credits,
                net_credits,
            ),
        )
        note = _ledger_note(order_id=order_id, listing_id=listing_uuid, kind=kind)
        cur.execute(
            "INSERT INTO credit_ledger (id, email, kind, credits_delta, note) VALUES (%s, %s, 'market_buy', %s, %s)",
            (_uuid(), buyer_email, -amount_credits, note),
        )
        cur.execute(
            "INSERT INTO credit_ledger (id, email, kind, credits_delta, note) VALUES (%s, %s, 'market_earn', %s, %s)",
            (_uuid(), seller_email, net_credits, note),
        )
        cur.execute(
            "UPDATE accounts SET credit_balance = credit_balance - %s WHERE email = %s",
            (amount_credits, buyer_email),
        )
        cur.execute(
            "UPDATE accounts SET credit_balance = credit_balance + %s WHERE email = %s",
            (net_credits, seller_email),
        )
        if kind == "purchase" or listing["pricing_kind"] == "free":
            _grant_entitlement(cur, listing_uuid, buyer_email, order_id, kind)
        if kind == "purchase":
            bump_counter(listing_uuid, "sales", 1)
        balance_after = balances[buyer_email] - amount_credits
    return {
        "kind": "credits",
        "order_id": order_id,
        "status": "paid",
        "balance_after": balance_after,
    }


def connect_status(seller_row: Any) -> dict[str, Any]:
    account_id = str(_row_get(seller_row, "connect_account_id", "") or "")
    charges_enabled = bool(_row_get(seller_row, "connect_charges_enabled", False))
    payouts_enabled = bool(_row_get(seller_row, "connect_payouts_enabled", False))
    details_submitted = bool(_row_get(seller_row, "connect_details_submitted", False))
    return {
        "connected": bool(account_id),
        "account_id": account_id,
        "charges_enabled": charges_enabled,
        "payouts_enabled": payouts_enabled,
        "details_submitted": details_submitted,
        "refreshed_at": _iso(_row_get(seller_row, "connect_refreshed_at")),
        "needs_onboarding": not (
            account_id and charges_enabled and payouts_enabled and details_submitted
        ),
    }


def _account_cache_values(account: Any) -> tuple[bool, bool, bool]:
    return (
        bool(_field(account, "charges_enabled", False)),
        bool(_field(account, "payouts_enabled", False)),
        bool(_field(account, "details_submitted", False)),
    )


def refresh_connect(email: str) -> dict[str, Any]:
    email = email.strip().lower()
    seller = get_seller(email)
    if seller is None:
        raise MarketplaceError(403, "not_a_seller", "open a seller account first")
    account_id = str(seller["connect_account_id"] or "")
    if not account_id:
        return connect_status(seller)
    stripe = _stripe()
    try:
        account = stripe.Account.retrieve(account_id)
    except Exception as exc:
        log.warning("Stripe Connect refresh failed for %s", email, exc_info=True)
        raise MarketplaceError(
            502, "stripe_error", "Connect status could not be refreshed"
        ) from exc
    charges_enabled, payouts_enabled, details_submitted = _account_cache_values(account)
    with tx() as cur:
        cur.execute(
            """
            UPDATE mk_sellers
            SET connect_charges_enabled = %s, connect_payouts_enabled = %s,
                connect_details_submitted = %s, connect_refreshed_at = now()
            WHERE email = %s AND connect_account_id = %s
            RETURNING *
            """,
            (charges_enabled, payouts_enabled, details_submitted, email, account_id),
        )
        updated = cur.fetchone()
    if updated is None:
        raise MarketplaceError(
            409, "connect_changed", "Connect account changed; retry"
        )
    return connect_status(updated)


def _cash_order(
    listing_id: str,
    buyer_email: str,
    *,
    kind: str,
    amount: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    buyer_email = buyer_email.strip().lower()
    preliminary = _listing_for_money(listing_id, buyer_email)
    seller_email = preliminary["seller_email"].strip().lower()
    # Resolve the price for the requested currency BEFORE touching Stripe.
    #
    # Reversing these made a USD checkout against a FREE listing report
    # `connect_incomplete` whenever the seller had not onboarded, so a buyer
    # clicking a free model was told the seller was "finishing Stripe
    # onboarding" for something that is not sold in dollars at all, and the
    # operator got a Connect-shaped support ticket for a pricing mistake. The
    # refusal reason must depend on the listing, not on unrelated seller state.
    # For a tip the caller supplies the amount, so there is no listing price to
    # resolve and this check does not apply.
    if kind != "tip":
        _purchase_price(preliminary, "usd")
    refreshed = refresh_connect(seller_email)
    if not refreshed["charges_enabled"]:
        raise MarketplaceError(
            409,
            "connect_incomplete",
            "the seller has not completed Stripe Connect onboarding",
        )
    try:
        with tx() as cur:
            listing = _listing_for_money(listing_id, buyer_email)
            listing_uuid = str(listing["id"])
            if listing["seller_email"].strip().lower() != seller_email:
                raise MarketplaceError(
                    409, "listing_changed", "listing changed; retry checkout"
                )
            seller = get_seller(seller_email)
            if seller is None or not seller["connect_charges_enabled"]:
                raise MarketplaceError(
                    409, "connect_incomplete", "Connect onboarding is incomplete"
                )
            if kind == "purchase":
                if _entitlement_exists(cur, listing_uuid, buyer_email):
                    raise MarketplaceError(
                        409, "already_owned", "you already own this listing"
                    )
                amount_cents = _purchase_price(listing, "usd")
                cur.execute(
                    """
                    SELECT * FROM mk_orders
                    WHERE listing_id = %s AND buyer_email = %s
                      AND kind = 'purchase' AND status = 'pending'
                    FOR UPDATE
                    """,
                    (listing_uuid, buyer_email),
                )
                existing = cur.fetchone()
                if existing is not None:
                    return dict(existing), listing, seller
            else:
                if not listing["tips_enabled"]:
                    raise MarketplaceError(
                        409, "tips_disabled", "tips are disabled for this listing"
                    )
                if amount is None:
                    raise MarketplaceError(
                        400, "invalid_tip", "tip amount is required"
                    )
                amount_cents = amount
            fee_cents, net_cents = split_cash(
                amount_cents, settings.marketplace_fee_bps
            )
            order_id = _uuid()
            cur.execute(
                """
                INSERT INTO mk_orders (
                    id, listing_id, version_id, buyer_email, seller_email, kind,
                    currency, amount_cents, fee_cents, net_cents, status
                ) VALUES (%s, %s, %s, %s, %s, %s, 'usd', %s, %s, %s, 'pending')
                RETURNING *
                """,
                (
                    order_id,
                    listing_uuid,
                    _current_version_id(cur, listing_uuid),
                    buyer_email,
                    seller_email,
                    kind,
                    amount_cents,
                    fee_cents,
                    net_cents,
                ),
            )
            order = dict(cur.fetchone())
    except psycopg.errors.UniqueViolation:
        raise MarketplaceError(
            409, "checkout_pending", "a checkout is already pending"
        ) from None
    return order, listing, seller


def _create_cash_checkout(
    listing_id: str,
    buyer_email: str,
    *,
    kind: str,
    amount: int | None = None,
) -> dict[str, Any]:
    order, listing, seller = _cash_order(
        listing_id, buyer_email, kind=kind, amount=amount
    )
    order_id = str(order["id"])
    existing_session = str(order["stripe_checkout_session"] or "")
    if existing_session:
        stripe = _stripe()
        try:
            session = stripe.checkout.Session.retrieve(existing_session)
        except Exception as exc:
            log.warning(
                "Stripe Checkout retrieval failed for %s", order_id, exc_info=True
            )
            raise MarketplaceError(
                502, "stripe_error", "checkout could not be resumed"
            ) from exc
        checkout_url = str(_field(session, "url", "") or "")
        if not checkout_url:
            raise MarketplaceError(
                409, "checkout_expired", "the pending checkout has expired"
            )
        return {
            "kind": "stripe",
            "checkout_url": checkout_url,
            "order_id": order_id,
        }

    stripe = _stripe()
    listing_uuid = str(listing["id"])
    metadata = {
        "chud_kind": "marketplace",
        "chud_order_id": order_id,
        "chud_listing_id": listing_uuid,
    }
    product_name = str(listing["title"])
    if kind == "tip":
        product_name = f"Tip for {product_name}"
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": int(order["amount_cents"]),
                        "product_data": {"name": product_name},
                    },
                    "quantity": 1,
                }
            ],
            payment_intent_data={
                "application_fee_amount": int(order["fee_cents"]),
                "transfer_data": {
                    "destination": seller["connect_account_id"]
                },
                "metadata": metadata,
            },
            client_reference_id=order_id,
            metadata=metadata,
            customer_email=buyer_email,
            success_url=settings.marketplace_success_url,
            cancel_url=settings.marketplace_cancel_url,
            idempotency_key=f"marketplace-checkout-{order_id}",
        )
    except Exception as exc:
        log.warning(
            "Stripe Checkout creation failed for %s", order_id, exc_info=True
        )
        with tx() as cur:
            cur.execute(
                """
                UPDATE mk_orders SET failure_reason = %s
                WHERE id = %s AND status = 'pending'
                """,
                ("checkout creation failed; safe to retry", order_id),
            )
        raise MarketplaceError(
            502, "stripe_error", "checkout could not be created"
        ) from exc
    session_id = _text_id(session)
    checkout_url = str(_field(session, "url", "") or "")
    if not session_id or not checkout_url:
        raise MarketplaceError(
            502, "stripe_error", "Stripe returned an incomplete checkout"
        )
    with tx() as cur:
        cur.execute(
            """
            UPDATE mk_orders
            SET stripe_checkout_session = %s, failure_reason = ''
            WHERE id = %s
            """,
            (session_id, order_id),
        )
    return {
        "kind": "stripe",
        "checkout_url": checkout_url,
        "order_id": order_id,
    }


@router.post("/listings/{listing_id}/checkout")
def checkout_listing(
    listing_id: str, body: CheckoutBody, request: Request
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    email = user.email.strip().lower()
    api_writes.enforce(email, "marketplace checkout")
    currency = body.currency.strip().lower()
    if currency == "credits":
        try:
            return _credit_order(listing_id, email, kind="purchase")
        except psycopg.errors.UniqueViolation:
            raise MarketplaceError(
                409, "checkout_pending", "a checkout is already pending"
            ) from None
    if currency == "usd":
        return _create_cash_checkout(listing_id, email, kind="purchase")
    raise MarketplaceError(
        400, "invalid_currency", "currency must be usd or credits"
    )


@router.post("/listings/{listing_id}/tip")
def tip_listing(
    listing_id: str, body: TipBody, request: Request
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    email = user.email.strip().lower()
    api_writes.enforce(email, "marketplace tip")
    currency, amount = _tip_amount(body)
    if currency == "credits":
        return _credit_order(listing_id, email, kind="tip", amount=amount)
    return _create_cash_checkout(listing_id, email, kind="tip", amount=amount)


def _order_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "currency": row["currency"],
        "amount_cents": int(row["amount_cents"]),
        "amount_credits": int(row["amount_credits"]),
        "fee_cents": int(row["fee_cents"]),
        "fee_credits": int(row["fee_credits"]),
        "net_cents": int(row["net_cents"]),
        "net_credits": int(row["net_credits"]),
        "refunded_cents": int(row["refunded_cents"]),
        "refunded_credits": int(row["refunded_credits"]),
        "status": row["status"],
        "created_at": _iso(row["created_at"]),
        "paid_at": _iso(row["paid_at"]),
        "refunded_at": _iso(row["refunded_at"]),
    }


@router.get("/orders")
def buyer_orders(request: Request, cursor: str = "") -> dict[str, Any]:
    marketplace_enabled_or_404()
    user = require_user(request)
    email = user.email.strip().lower()
    params: list[Any] = [email]
    cursor_clause = ""
    if cursor:
        try:
            keys = decode_cursor(cursor, expect_sort="orders")
        except CursorError as exc:
            raise MarketplaceError(
                400, "invalid_cursor", "invalid order cursor"
            ) from exc
        if len(keys) != 2:
            raise MarketplaceError(
                400, "invalid_cursor", "invalid order cursor"
            )
        cursor_clause = (
            "AND (created_at, id) < (%s::timestamptz, %s::uuid)"
        )
        params.extend(keys)
    params.append(25)
    with tx() as cur:
        cur.execute(
            f"""
            SELECT * FROM mk_orders
            WHERE buyer_email = %s {cursor_clause}
            ORDER BY created_at DESC, id DESC LIMIT %s
            """,
            tuple(params),
        )
        rows = [dict(row) for row in cur.fetchall()]
        items: list[dict[str, Any]] = []
        for row in rows[:24]:
            listing = load_listing(str(row["listing_id"]))
            item = _order_payload(row)
            item["listing"] = (
                listing_card(listing, viewer_email=email) if listing else None
            )
            items.append(item)
    next_cursor = None
    if len(rows) > 24:
        last = rows[23]
        next_cursor = encode_cursor(
            "orders", [last["created_at"], last["id"]]
        )
    return {"items": items, "next_cursor": next_cursor}


@studio_router.get("/connect")
def studio_connect(request: Request) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, seller = _require_studio_user(request)
    if seller["connect_account_id"]:
        return refresh_connect(user.email)
    return connect_status(seller)


@studio_router.post("/connect/onboard")
def onboard_connect(request: Request) -> dict[str, str]:
    marketplace_enabled_or_404()
    user, seller = _require_studio_user(request)
    email = user.email.strip().lower()
    api_writes.enforce(email, "Stripe Connect onboarding")
    account_id = str(seller["connect_account_id"] or "")
    if not account_id:
        account = _v2_request(
            "accounts",
            {
                "contact_email": email,
                "display_name": seller["display_name"],
                "dashboard": "express",
                "identity": {
                    "country": "us",
                    "entity_type": "individual",
                },
                "defaults": {
                    "currency": "usd",
                    "responsibilities": {
                        "losses_collector": "application",
                        "fees_collector": "application",
                    },
                },
                "include": [
                    "configuration.recipient",
                    "requirements",
                ],
                "configuration": {
                    "recipient": {
                        "capabilities": {
                            "stripe_balance": {
                                "stripe_transfers": {
                                    "requested": True
                                }
                            }
                        }
                    }
                },
                "metadata": {
                    "chud_kind": "marketplace_seller",
                    "chud_seller": email,
                },
            },
            f"marketplace-connect-account-{uuid.uuid5(uuid.NAMESPACE_URL, email)}",
        )
        account_id = _text_id(account)
        if not account_id:
            raise MarketplaceError(
                502, "stripe_error", "Stripe returned no Connect account"
            )
        with tx() as cur:
            cur.execute(
                """
                UPDATE mk_sellers
                SET connect_account_id = %s, connect_refreshed_at = now()
                WHERE email = %s AND connect_account_id = ''
                """,
                (account_id, email),
            )
            if cur.rowcount == 0:
                current = get_seller(email)
                if (
                    current is None
                    or current["connect_account_id"] != account_id
                ):
                    raise MarketplaceError(
                        409,
                        "connect_changed",
                        "Connect account changed; retry",
                    )
    # Account Links are single-use. A time bucket can return an already-consumed
    # link to a later onboarding request, while a fresh attempt id still lets
    # retries inside this request use one Stripe idempotency key.
    link_attempt_id = uuid.uuid4()
    link = _v2_request(
        "account_links",
        {
            "account": account_id,
            "use_case": {
                "type": "account_onboarding",
                "account_onboarding": {
                    "configurations": ["recipient"],
                    "refresh_url": settings.marketplace_connect_return_url,
                    "return_url": settings.marketplace_connect_return_url,
                },
            },
        },
        f"marketplace-connect-link-{account_id}-{link_attempt_id}",
    )
    url = str(_field(link, "url", "") or "")
    if not url:
        raise MarketplaceError(
            502, "stripe_error", "Stripe returned no onboarding URL"
        )
    return {"url": url}


def _money_summary(
    row: Mapping[str, Any],
) -> dict[str, dict[str, int]]:
    return {
        "cash": {
            "gross_cents": int(row["gross_cents"] or 0),
            "fee_cents": int(row["fee_cents"] or 0),
            "net_cents": int(row["net_cents"] or 0),
            "orders": int(row["cash_orders"] or 0),
        },
        "credits": {
            "gross_credits": int(row["gross_credits"] or 0),
            "fee_credits": int(row["fee_credits"] or 0),
            "net_credits": int(row["net_credits"] or 0),
            "orders": int(row["credit_orders"] or 0),
        },
    }


_EARNINGS_SUM_SQL = """
    SELECT
        COALESCE(sum(amount_cents)
            FILTER (WHERE currency = 'usd'), 0) AS gross_cents,
        COALESCE(sum(fee_cents)
            FILTER (WHERE currency = 'usd'), 0) AS fee_cents,
        COALESCE(sum(net_cents)
            FILTER (WHERE currency = 'usd'), 0) AS net_cents,
        count(*) FILTER (WHERE currency = 'usd') AS cash_orders,
        COALESCE(sum(amount_credits)
            FILTER (WHERE currency = 'credits'), 0) AS gross_credits,
        COALESCE(sum(fee_credits)
            FILTER (WHERE currency = 'credits'), 0) AS fee_credits,
        COALESCE(sum(net_credits)
            FILTER (WHERE currency = 'credits'), 0) AS net_credits,
        count(*) FILTER (WHERE currency = 'credits') AS credit_orders
    FROM mk_orders
    WHERE seller_email = %s AND status = 'paid'
"""


@studio_router.get("/earnings")
def studio_earnings(
    request: Request, days: int = 30
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, seller = _require_studio_user(request)
    email = user.email.strip().lower()
    days = max(1, min(int(days), 365))
    with tx() as cur:
        cur.execute(
            _EARNINGS_SUM_SQL
            + """
              AND paid_at >=
                  current_date - (%s - 1) * interval '1 day'
              """,
            (email, days),
        )
        period = _money_summary(cur.fetchone())
        cur.execute(_EARNINGS_SUM_SQL, (email,))
        lifetime = _money_summary(cur.fetchone())
        cur.execute(
            """
            SELECT
                d::date AS day,
                COALESCE(sum(o.amount_cents)
                    FILTER (WHERE o.currency = 'usd'), 0) AS gross_cents,
                COALESCE(sum(o.fee_cents)
                    FILTER (WHERE o.currency = 'usd'), 0) AS fee_cents,
                COALESCE(sum(o.net_cents)
                    FILTER (WHERE o.currency = 'usd'), 0) AS net_cents,
                COALESCE(sum(o.amount_credits)
                    FILTER (WHERE o.currency = 'credits'), 0)
                    AS gross_credits,
                COALESCE(sum(o.fee_credits)
                    FILTER (WHERE o.currency = 'credits'), 0)
                    AS fee_credits,
                COALESCE(sum(o.net_credits)
                    FILTER (WHERE o.currency = 'credits'), 0)
                    AS net_credits,
                count(o.id) AS orders
            FROM generate_series(
                current_date - (%s - 1) * interval '1 day',
                current_date,
                interval '1 day'
            ) AS d
            LEFT JOIN mk_orders o
              ON o.seller_email = %s
             AND o.status = 'paid'
             AND o.paid_at >= d
             AND o.paid_at < d + interval '1 day'
            GROUP BY d
            ORDER BY d
            """,
            (days, email),
        )
        series = [
            {
                "date": row["day"].isoformat(),
                "gross_cents": int(row["gross_cents"]),
                "fee_cents": int(row["fee_cents"]),
                "net_cents": int(row["net_cents"]),
                "gross_credits": int(row["gross_credits"]),
                "fee_credits": int(row["fee_credits"]),
                "net_credits": int(row["net_credits"]),
                "orders": int(row["orders"]),
            }
            for row in cur.fetchall()
        ]
        cur.execute(
            "SELECT credit_balance FROM accounts WHERE email = %s",
            (email,),
        )
        account = cur.fetchone()
    balance = int(account["credit_balance"]) if account else 0
    return {
        "days": days,
        "period": period,
        "lifetime": lifetime,
        "series": series,
        "payout": {
            "balance_credits": balance,
            "minimum_credits": settings.marketplace_payout_min_credits,
            "credits_per_dollar": (
                settings.marketplace_credits_per_dollar
            ),
            "estimated_cents": credits_to_cents(
                balance, settings.marketplace_credits_per_dollar
            ),
            "connect_payouts_enabled": bool(
                seller["connect_payouts_enabled"]
            ),
        },
    }


@studio_router.post("/payouts")
def request_payout(
    body: PayoutBody, request: Request
) -> dict[str, Any]:
    marketplace_enabled_or_404()
    user, _seller = _require_studio_user(request)
    email = user.email.strip().lower()
    api_writes.enforce(email, "marketplace payout")
    credits = int(body.credits)
    if credits < settings.marketplace_payout_min_credits:
        raise MarketplaceError(
            400,
            "payout_too_small",
            (
                "minimum payout is "
                f"{settings.marketplace_payout_min_credits} credits"
            ),
        )
    status = refresh_connect(email)
    if not status["payouts_enabled"]:
        raise MarketplaceError(
            409,
            "connect_incomplete",
            "complete Stripe Connect before requesting a payout",
        )
    rate = settings.marketplace_credits_per_dollar
    cents = credits_to_cents(credits, rate)
    if cents <= 0:
        raise MarketplaceError(
            400, "payout_too_small", "payout rounds below one cent"
        )
    request_id = _uuid()
    try:
        with tx() as cur:
            current_seller = get_seller(email)
            if (
                current_seller is None
                or not current_seller["connect_payouts_enabled"]
            ):
                raise MarketplaceError(
                    409,
                    "connect_incomplete",
                    "Connect payouts are disabled",
                )
            cur.execute(
                """
                SELECT credit_balance FROM accounts
                WHERE email = %s FOR UPDATE
                """,
                (email,),
            )
            account = cur.fetchone()
            if (
                account is None
                or int(account["credit_balance"]) < credits
            ):
                raise MarketplaceError(
                    402,
                    "insufficient_credits",
                    "your credit balance is too low for this payout",
                )
            cur.execute(
                """
                INSERT INTO mk_payout_requests (
                    id, seller_email, credits, cents,
                    credits_per_dollar, status
                )
                VALUES (%s, %s, %s, %s, %s, 'pending')
                RETURNING requested_at
                """,
                (request_id, email, credits, cents, rate),
            )
            requested_at = cur.fetchone()["requested_at"]
            cur.execute(
                """
                INSERT INTO credit_ledger (
                    id, email, kind, credits_delta, note
                )
                VALUES (%s, %s, 'market_payout', %s, %s)
                """,
                (
                    _uuid(),
                    email,
                    -credits,
                    _ledger_note(
                        payout_request_id=request_id,
                        cents=cents,
                        rate=rate,
                    ),
                ),
            )
            cur.execute(
                """
                UPDATE accounts
                SET credit_balance = credit_balance - %s
                WHERE email = %s
                """,
                (credits, email),
            )
            balance_after = int(account["credit_balance"]) - credits
    except psycopg.errors.UniqueViolation:
        raise MarketplaceError(
            409, "payout_pending", "a payout request is already open"
        ) from None
    return {
        "id": request_id,
        "status": "pending",
        "credits": credits,
        "cents": cents,
        "credits_per_dollar": rate,
        "balance_after": balance_after,
        "requested_at": _iso(requested_at),
    }


def _payout_payload(
    row: Mapping[str, Any], **extra: Any
) -> dict[str, Any]:
    payload = {
        "id": str(row["id"]),
        "seller_email": row["seller_email"],
        "credits": int(row["credits"]),
        "cents": int(row["cents"]),
        "credits_per_dollar": int(row["credits_per_dollar"]),
        "status": row["status"],
        "stripe_transfer_id": row["stripe_transfer_id"],
        "note": row["note"],
        "requested_at": _iso(row["requested_at"]),
        "decided_at": _iso(row["decided_at"]),
        "decided_by": row["decided_by"],
    }
    payload.update(extra)
    return payload


def execute_payout(
    request_id: str, actor_email: str
) -> dict[str, Any]:
    actor_email = actor_email.strip().lower()
    with tx() as cur:
        cur.execute(
            """
            SELECT seller_email FROM mk_payout_requests
            WHERE id = %s
            """,
            (request_id,),
        )
        initial = cur.fetchone()
    if initial is None:
        raise MarketplaceError(
            404, "payout_not_found", "payout request not found"
        )
    seller_email = initial["seller_email"]
    status = refresh_connect(seller_email)
    if not status["payouts_enabled"]:
        raise MarketplaceError(
            409, "connect_incomplete", "Connect payouts are disabled"
        )

    stripe = _stripe()
    transfer_error: Exception | None = None
    result: dict[str, Any] | None = None
    with tx() as cur:
        _require_admin_actor(cur, actor_email)
        cur.execute(
            """
            SELECT * FROM mk_payout_requests
            WHERE id = %s FOR UPDATE
            """,
            (request_id,),
        )
        payout = cur.fetchone()
        if payout is None:
            raise MarketplaceError(
                404, "payout_not_found", "payout request not found"
            )
        if payout["status"] == "paid":
            return _payout_payload(payout)
        if payout["status"] not in ("pending", "approved", "failed"):
            raise MarketplaceError(
                409,
                "payout_not_open",
                "payout request is not payable",
            )
        seller = get_seller(payout["seller_email"])
        if (
            seller is None
            or not seller["connect_payouts_enabled"]
        ):
            raise MarketplaceError(
                409, "connect_incomplete", "Connect payouts are disabled"
            )
        cur.execute(
            """
            UPDATE mk_payout_requests
            SET status = 'approved',
                decided_at = now(),
                decided_by = %s
            WHERE id = %s
            """,
            (actor_email, request_id),
        )
        try:
            transfer = stripe.Transfer.create(
                amount=int(payout["cents"]),
                currency="usd",
                destination=seller["connect_account_id"],
                metadata={
                    "chud_kind": "marketplace",
                    "chud_payout_request_id": str(payout["id"]),
                    "chud_seller": payout["seller_email"],
                },
                idempotency_key=(
                    f"marketplace-payout-{payout['id']}"
                ),
            )
        except Exception as exc:
            transfer_error = exc
            cur.execute(
                """
                UPDATE mk_payout_requests
                SET status = 'failed', note = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    "Stripe transfer failed; credits remain frozen",
                    request_id,
                ),
            )
            result = _payout_payload(cur.fetchone())
        else:
            transfer_id = _text_id(transfer)
            if not transfer_id:
                raise MarketplaceError(
                    502,
                    "stripe_error",
                    "Stripe returned no transfer id",
                )
            cur.execute(
                """
                UPDATE mk_payout_requests
                SET status = 'paid',
                    stripe_transfer_id = %s,
                    note = ''
                WHERE id = %s
                RETURNING *
                """,
                (transfer_id, request_id),
            )
            result = _payout_payload(cur.fetchone())
    if transfer_error is not None:
        log.warning(
            "Stripe payout transfer failed for %s: %s",
            request_id,
            transfer_error,
        )
        raise MarketplaceError(
            502, "stripe_error", "payout transfer failed"
        ) from transfer_error
    assert result is not None
    return result


def deny_payout(
    request_id: str,
    actor_email: str,
    note: str = "",
) -> dict[str, Any]:
    actor_email = actor_email.strip().lower()
    note = note.strip()[:4000]
    with tx() as cur:
        _require_admin_actor(cur, actor_email)
        cur.execute(
            """
            SELECT * FROM mk_payout_requests
            WHERE id = %s FOR UPDATE
            """,
            (request_id,),
        )
        payout = cur.fetchone()
        if payout is None:
            raise MarketplaceError(
                404, "payout_not_found", "payout request not found"
            )
        if payout["status"] == "denied":
            cur.execute(
                """
                SELECT credit_balance FROM accounts
                WHERE email = %s
                """,
                (payout["seller_email"],),
            )
            account = cur.fetchone()
            return _payout_payload(
                payout,
                balance_after=(
                    int(account["credit_balance"])
                    if account
                    else None
                ),
            )
        if payout["status"] == "paid":
            raise MarketplaceError(
                409,
                "payout_already_paid",
                "a paid payout cannot be denied",
            )
        cur.execute(
            """
            SELECT credit_balance FROM accounts
            WHERE email = %s FOR UPDATE
            """,
            (payout["seller_email"],),
        )
        account = cur.fetchone()
        if account is None:
            raise MarketplaceError(
                409,
                "account_unavailable",
                "seller account is unavailable",
            )
        credits = int(payout["credits"])
        cur.execute(
            """
            INSERT INTO credit_ledger (
                id, email, kind, credits_delta, note
            )
            VALUES (%s, %s, 'market_earn', %s, %s)
            """,
            (
                _uuid(),
                payout["seller_email"],
                credits,
                _ledger_note(
                    payout_request_id=str(payout["id"]),
                    reason="payout_denied",
                    actor=actor_email,
                ),
            ),
        )
        cur.execute(
            """
            UPDATE accounts
            SET credit_balance = credit_balance + %s
            WHERE email = %s
            """,
            (credits, payout["seller_email"]),
        )
        cur.execute(
            """
            UPDATE mk_payout_requests
            SET status = 'denied',
                note = %s,
                decided_at = now(),
                decided_by = %s
            WHERE id = %s
            RETURNING *
            """,
            (note, actor_email, request_id),
        )
        updated = cur.fetchone()
        balance_after = int(account["credit_balance"]) + credits
    return _payout_payload(
        updated, balance_after=balance_after
    )


def _apply_refund_total(
    cur: Any,
    order: Mapping[str, Any],
    *,
    refunded_cents: int | None = None,
    refunded_credits: int | None = None,
    actor_email: str = "",
    reason: str = "",
) -> Mapping[str, Any]:
    currency = str(order["currency"])
    if currency == "usd":
        total = int(order["amount_cents"])
        previous = int(order["refunded_cents"])
        target = previous if refunded_cents is None else int(refunded_cents)
    elif currency == "credits":
        total = int(order["amount_credits"])
        previous = int(order["refunded_credits"])
        target = previous if refunded_credits is None else int(refunded_credits)
    else:
        raise MarketplaceError(409, "not_refundable", "order currency is not refundable")

    if target < previous:
        return order
    if target > total:
        raise MarketplaceError(
            400,
            "refund_exceeds_remaining",
            "refund exceeds the remaining refundable amount",
        )
    if target == previous:
        return order

    if currency == "credits":
        buyer_email = str(order["buyer_email"]).strip().lower()
        seller_email = str(order["seller_email"]).strip().lower()
        cur.execute(
            """
            SELECT email, credit_balance FROM accounts
            WHERE email = ANY(%s) ORDER BY email FOR UPDATE
            """,
            (sorted((buyer_email, seller_email)),),
        )
        balances = {
            str(row["email"]): int(row["credit_balance"])
            for row in cur.fetchall()
        }
        if buyer_email not in balances or seller_email not in balances:
            raise MarketplaceError(
                409, "account_unavailable", "an account is unavailable"
            )

        buyer_return = target - previous
        net_credits = int(order["net_credits"])
        seller_reversed_before = net_credits * previous // total
        seller_reversed_after = net_credits * target // total
        seller_debit = seller_reversed_after - seller_reversed_before
        seller_balance = max(0, balances[seller_email])
        seller_debit_applied = min(seller_debit, seller_balance)
        seller_shortfall = seller_debit - seller_debit_applied
        note = _ledger_note(
            order_id=str(order["id"]),
            listing_id=str(order["listing_id"]),
            refund_credits=buyer_return,
            refunded_credits=target,
            reason=reason or "refund",
            actor=actor_email,
        )
        # Reversals retain the original movement's semantic kind and invert its
        # sign: the buyer gets a positive market_buy, never a market_earn.
        cur.execute(
            """
            INSERT INTO credit_ledger (
                id, email, kind, credits_delta, note
            ) VALUES (%s, %s, 'market_buy', %s, %s)
            """,
            (_uuid(), buyer_email, buyer_return, note),
        )
        if seller_debit:
            cur.execute(
                """
                INSERT INTO credit_ledger (
                    id, email, kind, credits_delta, note
                ) VALUES (%s, %s, 'market_earn', %s, %s)
                """,
                (_uuid(), seller_email, -seller_debit, note),
            )
        if seller_shortfall:
            # Match record_spend: preserve the full economic reversal in the
            # ledger, then absorb only the unavailable balance so it stays zero.
            cur.execute(
                """
                INSERT INTO credit_ledger (
                    id, email, kind, credits_delta, note
                ) VALUES (%s, %s, 'adjust', %s, %s)
                """,
                (
                    _uuid(),
                    seller_email,
                    seller_shortfall,
                    _ledger_note(
                        order_id=str(order["id"]),
                        reason="absorbed_refund_shortfall",
                        actor=actor_email,
                    ),
                ),
            )
        cur.execute(
            """
            UPDATE accounts SET credit_balance = credit_balance + %s
            WHERE email = %s
            """,
            (buyer_return, buyer_email),
        )
        cur.execute(
            """
            UPDATE accounts SET credit_balance = credit_balance - %s
            WHERE email = %s
            """,
            (seller_debit_applied, seller_email),
        )

    status = "refunded" if target == total else "paid"
    new_refunded_cents = (
        target if currency == "usd" else int(order["refunded_cents"])
    )
    new_refunded_credits = (
        target if currency == "credits" else int(order["refunded_credits"])
    )
    cur.execute(
        """
        UPDATE mk_orders
        SET refunded_cents = %s,
            refunded_credits = %s,
            status = %s,
            refunded_at = COALESCE(refunded_at, now()),
            failure_reason = ''
        WHERE id = %s
        RETURNING *
        """,
        (
            new_refunded_cents,
            new_refunded_credits,
            status,
            order["id"],
        ),
    )
    updated = cur.fetchone()
    if status == "refunded":
        cur.execute(
            "DELETE FROM mk_entitlements WHERE order_id = %s",
            (order["id"],),
        )
    return updated


def refund_order(
    order_id: str,
    actor_email: str,
    amount_cents: int | None = None,
    *,
    amount_credits: int | None = None,
    reason: str = "requested_by_customer",
) -> dict[str, Any]:
    actor_email = actor_email.strip().lower()
    reason = reason.strip()
    with tx() as cur:
        _require_admin_actor(cur, actor_email)
        cur.execute(
            "SELECT * FROM mk_orders WHERE id = %s FOR UPDATE",
            (order_id,),
        )
        order = cur.fetchone()
        if order is None:
            raise MarketplaceError(
                404, "order_not_found", "order not found"
            )
        if order["status"] == "refunded":
            if amount_cents is not None or amount_credits is not None:
                raise MarketplaceError(
                    400,
                    "refund_exceeds_remaining",
                    "the order has no refundable amount remaining",
                )
            return _order_payload(order)
        if order["status"] != "paid":
            raise MarketplaceError(
                409, "not_refundable", "order is not paid"
            )

        if order["currency"] == "credits":
            if amount_cents is not None:
                raise MarketplaceError(
                    400,
                    "invalid_refund",
                    "amount_cents cannot refund a credits order",
                )
            full_amount = int(order["amount_credits"])
            refunded_before = int(order["refunded_credits"])
            remaining = full_amount - refunded_before
            requested = remaining if amount_credits is None else int(amount_credits)
            if requested <= 0 or requested > remaining:
                raise MarketplaceError(
                    400,
                    "refund_exceeds_remaining",
                    "refund amount is out of the remaining refundable range",
                )
            updated = _apply_refund_total(
                cur,
                order,
                refunded_credits=refunded_before + requested,
                actor_email=actor_email,
                reason=reason,
            )
            return _order_payload(updated) | {
                "refunded_amount_credits": requested
            }

        if (
            order["currency"] != "usd"
            or not order["stripe_payment_intent"]
            or amount_credits is not None
        ):
            raise MarketplaceError(
                409,
                "not_refundable",
                "only settled cash or credit orders are refundable",
            )
        full_amount = int(order["amount_cents"])
        refunded_before = int(order["refunded_cents"])
        remaining = full_amount - refunded_before
        requested = remaining if amount_cents is None else int(amount_cents)
        if requested <= 0 or requested > remaining:
            raise MarketplaceError(
                400,
                "refund_exceeds_remaining",
                "refund amount is out of the remaining refundable range",
            )

        stripe = _stripe()
        refund_kwargs: dict[str, Any] = {
            "payment_intent": order["stripe_payment_intent"],
            "amount": requested,
            "reverse_transfer": True,
            "refund_application_fee": True,
            "metadata": {
                "chud_kind": "marketplace",
                "chud_order_id": str(order["id"]),
                "chud_actor": actor_email,
            },
            "idempotency_key": (
                f"marketplace-refund-{order['id']}-"
                f"to-{refunded_before + requested}"
            ),
        }
        if reason:
            refund_kwargs["reason"] = reason
        try:
            refund = stripe.Refund.create(**refund_kwargs)
        except Exception as exc:
            log.warning(
                "Stripe refund failed for order %s",
                order_id,
                exc_info=True,
            )
            raise MarketplaceError(
                502, "stripe_error", "refund failed"
            ) from exc
        refund_id = _text_id(refund)
        updated = _apply_refund_total(
            cur,
            order,
            refunded_cents=refunded_before + requested,
            actor_email=actor_email,
            reason=reason,
        )
    return _order_payload(updated) | {
        "stripe_refund_id": refund_id,
        "refunded_amount_cents": requested,
    }


def _event_metadata(obj: Any) -> Mapping[str, Any]:
    metadata = _field(obj, "metadata", {}) or {}
    if isinstance(metadata, Mapping):
        return metadata
    return {}


def _event_order(
    cur: Any,
    obj: Any,
    metadata: Mapping[str, Any],
) -> Any:
    order_id = str(
        metadata.get("chud_order_id", "") or ""
    )
    if order_id:
        cur.execute(
            "SELECT * FROM mk_orders WHERE id = %s FOR UPDATE",
            (order_id,),
        )
        return cur.fetchone()
    payment_intent = _text_id(
        _field(obj, "payment_intent")
    )
    if payment_intent:
        cur.execute(
            """
            SELECT * FROM mk_orders
            WHERE stripe_payment_intent = %s
            FOR UPDATE
            """,
            (payment_intent,),
        )
        return cur.fetchone()
    return None


def _settle_checkout(
    event_id: str,
    obj: Any,
    metadata: Mapping[str, Any],
) -> None:
    try:
        with tx() as cur:
            order = _event_order(cur, obj, metadata)
            if order is None:
                log.error(
                    "marketplace checkout event %s has no matching order",
                    event_id,
                )
                return
            session_id = _text_id(obj)
            if (
                not session_id
                or session_id != str(order["stripe_checkout_session"] or "")
                or order["currency"] != "usd"
            ):
                log.error("marketplace settlement refused: Checkout/order binding mismatch")
                return
            if order["status"] not in {"pending", "failed"}:
                return
            if str(_field(obj, "payment_status", "") or "") != "paid":
                log.info(
                    "marketplace order %s is waiting on an async payment "
                    "after Stripe event %s (payment_status=%s)",
                    order["id"],
                    event_id,
                    _field(obj, "payment_status", ""),
                )
                return
            try:
                amount_total = int(_field(obj, "amount_total"))
            except (TypeError, ValueError):
                amount_total = -1
            currency = str(_field(obj, "currency", "") or "")
            if (
                amount_total != int(order["amount_cents"])
                or currency != "usd"
            ):
                log.error(
                    "marketplace checkout event %s amount/currency mismatch "
                    "for order %s: session=%s %s order=%s usd",
                    event_id,
                    order["id"],
                    amount_total,
                    currency,
                    order["amount_cents"],
                )
                # Refusing to grant is the security property, but the event is
                # acknowledged so Stripe never retries: without a durable marker
                # the buyer has paid Stripe and the order sits 'pending' with an
                # empty failure_reason forever, visible only in a log line. Record
                # it so the admin order view and any reconciliation sweep can see
                # "settled at Stripe, refused here" instead of an abandoned cart.
                cur.execute(
                    "UPDATE mk_orders SET failure_reason=%s, stripe_event_id=%s"
                    " WHERE id=%s AND status='pending'",
                    (
                        f"amount_mismatch: session={amount_total} {currency}"
                        f" order={order['amount_cents']} usd",
                        event_id,
                        order["id"],
                    ),
                )
                return
            payment_intent = _text_id(
                _field(obj, "payment_intent")
            )
            if not payment_intent:
                log.error(
                    "paid marketplace checkout event %s has no payment intent",
                    event_id,
                )
                cur.execute(
                    "UPDATE mk_orders SET failure_reason=%s"
                    " WHERE id=%s AND status='pending'",
                    ("paid session carried no payment intent", order["id"]),
                )
                return
            if (
                order["stripe_payment_intent"]
                and str(order["stripe_payment_intent"]) != payment_intent
            ):
                log.error("marketplace settlement refused: payment binding mismatch")
                return
            cur.execute(
                """
                UPDATE mk_orders
                SET status = 'paid',
                    paid_at = now(),
                    stripe_payment_intent = %s,
                    stripe_event_id = %s,
                    failure_reason = ''
                WHERE id = %s AND status IN ('pending', 'failed')
                """,
                (payment_intent, event_id, order["id"]),
            )
            if cur.rowcount == 0:
                return
            listing = load_listing(str(order["listing_id"]))
            if order["kind"] == "purchase" or (
                listing is not None
                and listing["pricing_kind"] == "free"
            ):
                _grant_entitlement(
                    cur,
                    str(order["listing_id"]),
                    order["buyer_email"],
                    str(order["id"]),
                    order["kind"],
                )
            if order["kind"] == "purchase":
                bump_counter(
                    str(order["listing_id"]), "sales", 1
                )
    except psycopg.errors.UniqueViolation:
        log.info(
            "duplicate marketplace Stripe event %s ignored",
            event_id,
        )


def _cancel_checkout(
    obj: Any, metadata: Mapping[str, Any]
) -> None:
    with tx() as cur:
        order = _event_order(cur, obj, metadata)
        if (
            order is not None
            and order["status"] == "pending"
        ):
            cur.execute(
                """
                UPDATE mk_orders SET status = 'canceled'
                WHERE id = %s
                """,
                (order["id"],),
            )


def _fail_payment(
    obj: Any, metadata: Mapping[str, Any]
) -> None:
    error = _field(obj, "last_payment_error", {}) or {}
    reason = str(
        _field(error, "message", "payment failed")
        or "payment failed"
    )[:2000]
    with tx() as cur:
        order = _event_order(cur, obj, metadata)
        if (
            order is not None
            and order["status"] == "pending"
        ):
            cur.execute(
                """
                UPDATE mk_orders
                SET failure_reason = %s
                WHERE id = %s
                """,
                (reason, order["id"]),
            )


def _fail_async_checkout(
    event_id: str,
    obj: Any,
    metadata: Mapping[str, Any],
) -> None:
    reason = "asynchronous payment failed"
    with tx() as cur:
        order = _event_order(cur, obj, metadata)
        if order is None:
            log.error(
                "marketplace async failure event %s has no matching order",
                event_id,
            )
            return
        if (
            order["status"] == "pending"
            and _text_id(obj) == str(order["stripe_checkout_session"] or "")
        ):
            cur.execute(
                """
                UPDATE mk_orders
                SET status = 'failed',
                    paid_at = NULL,
                    stripe_event_id = NULL,
                    failure_reason = %s
                WHERE id = %s
                """,
                (reason, order["id"]),
            )


def _record_charge_refund(
    obj: Any, metadata: Mapping[str, Any]
) -> None:
    try:
        amount_refunded = int(_field(obj, "amount_refunded", 0) or 0)
    except (TypeError, ValueError):
        log.error("marketplace charge refund has invalid amount_refunded")
        return
    with tx() as cur:
        order = _event_order(cur, obj, metadata)
        if (
            order is None
            or order["currency"] != "usd"
            or order["status"] not in ("paid", "refunded")
        ):
            return
        refunded_before = int(order["refunded_cents"])
        if amount_refunded <= refunded_before:
            return
        if amount_refunded > int(order["amount_cents"]):
            log.error(
                "marketplace charge refund exceeds order %s amount: %s > %s",
                order["id"],
                amount_refunded,
                order["amount_cents"],
            )
            return
        _apply_refund_total(
            cur,
            order,
            refunded_cents=amount_refunded,
            reason="stripe_webhook",
        )


def _known_connect_account(account_id: str) -> bool:
    if not account_id:
        return False
    with tx() as cur:
        cur.execute(
            """
            SELECT 1 FROM mk_sellers
            WHERE connect_account_id = %s
            """,
            (account_id,),
        )
        return cur.fetchone() is not None


def _cache_account_event(obj: Any) -> None:
    account_id = _text_id(obj)
    (
        charges_enabled,
        payouts_enabled,
        details_submitted,
    ) = _account_cache_values(obj)
    with tx() as cur:
        cur.execute(
            """
            UPDATE mk_sellers
            SET connect_charges_enabled = %s,
                connect_payouts_enabled = %s,
                connect_details_submitted = %s,
                connect_refreshed_at = now()
            WHERE connect_account_id = %s
            """,
            (
                charges_enabled,
                payouts_enabled,
                details_submitted,
                account_id,
            ),
        )


def handle_marketplace_event(
    event: Any,
) -> bool:
    # `event` arrives from billing.py as whatever `stripe.Webhook.construct_event`
    # returned: a `stripe.Event`, which is a StripeObject and therefore neither a
    # dict nor a Mapping. Normalise the whole thing ONCE here so every read below,
    # and in every helper this calls, operates on plain nested dicts. See
    # `_plain` for the two distinct bugs this replaced -- an AttributeError on
    # `event.get`, and a silent no-op on `metadata`.
    event = _plain(event)
    event_type = str(_field(event, "type", "") or "")
    data = _field(event, "data", {}) or {}
    obj = _field(data, "object", {}) or {}
    metadata = _event_metadata(obj)
    account_event = (
        event_type == "account.updated"
        and _known_connect_account(_text_id(obj))
    )
    if (
        metadata.get("chud_kind") != "marketplace"
        and not account_event
    ):
        return False
    event_id = str(_field(event, "id", "") or "")
    if event_type in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    ):
        _settle_checkout(event_id, obj, metadata)
    elif event_type == "checkout.session.async_payment_failed":
        _fail_async_checkout(event_id, obj, metadata)
    elif event_type == "checkout.session.expired":
        _cancel_checkout(obj, metadata)
    elif event_type == "payment_intent.payment_failed":
        _fail_payment(obj, metadata)
    elif event_type == "charge.refunded":
        _record_charge_refund(obj, metadata)
    elif event_type == "account.updated":
        _cache_account_event(obj)
    else:
        log.info(
            "unhandled marketplace Stripe event type %s acknowledged",
            event_type,
        )
    return True
