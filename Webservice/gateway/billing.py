"""Stripe Checkout, Customer Portal, and idempotent billing webhooks.

Subscription checkout remains active. Legacy credit-pack prices and webhook
accounting are retained, but customer credit checkout is unavailable unless its
explicit feature gate is enabled. Stripe price ids, amounts, education
eligibility, and entitlement dates remain server-authoritative.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

try:
    from . import accounts as _accounts_mod
    from . import credits
    from . import mailer, deployment_drain
    from .auth import get_current_user
    from .config import settings
    from .db import tx
    from .plans import PLANS, is_purchasable, limits_for
except ImportError:  # uvicorn app:app from the package dir
    import accounts as _accounts_mod
    import credits
    import mailer, deployment_drain
    from auth import get_current_user
    from config import settings
    from db import tx
    from plans import PLANS, is_purchasable, limits_for

log = logging.getLogger("chudware-web.billing")

MAX_WEBHOOK_BYTES = 64 * 1024
_DOWNGRADE_STATUSES = {"past_due", "unpaid", "canceled", "incomplete_expired"}


@dataclass(frozen=True)
class BillingConfig:
    secret_key: str
    webhook_secret: str
    success_url: str
    cancel_url: str
    credit_pack_checkout_enabled: bool
    desktop_price: str
    desktop_edu_price: str
    studio_price: str
    studio_edu_price: str
    credits_starter_price: str
    credits_standard_price: str
    credits_bulk_price: str


def load_config(source: Any = None) -> BillingConfig:
    """Snapshot the validated process settings for one billing operation."""
    source = settings if source is None else source
    return BillingConfig(
        secret_key=source.stripe_secret_key.strip(),
        webhook_secret=source.stripe_webhook_secret.strip(),
        success_url=source.stripe_success_url.strip(),
        cancel_url=source.stripe_cancel_url.strip(),
        credit_pack_checkout_enabled=bool(source.credit_pack_checkout_enabled),
        desktop_price=source.stripe_price_desktop.strip(),
        desktop_edu_price=source.stripe_price_desktop_edu.strip(),
        studio_price=source.stripe_price_studio.strip(),
        studio_edu_price=source.stripe_price_studio_edu.strip(),
        credits_starter_price=source.stripe_price_credits_starter.strip(),
        credits_standard_price=source.stripe_price_credits_standard.strip(),
        credits_bulk_price=source.stripe_price_credits_bulk.strip(),
    )


def billing_configured() -> bool:
    return bool(load_config().secret_key)


def _require_configured(cfg: BillingConfig) -> None:
    if not cfg.secret_key:
        raise HTTPException(503, "billing not configured")


def _stripe():
    try:
        import stripe
    except ImportError as exc:
        raise HTTPException(
            503, "billing not configured: stripe package not installed"
        ) from exc
    return stripe


@runtime_checkable
class AccountStore(Protocol):
    def get_account(self, email: str) -> Any: ...
    def get_account_by_stripe_customer(self, customer_id: str) -> Any: ...
    def ensure_account(self, email: str, display: str) -> Any: ...
    def set_plan(self, email: str, plan: str) -> Any: ...
    def set_stripe_customer(self, email: str, customer_id: str) -> Any: ...
    def set_subscription(
        self,
        email: str,
        *,
        subscription_id: str = "",
        subscription_status: str = "",
        price_id: str = "",
        subscribed_since: float | None = None,
        paid_through: float | None = None,
        is_edu: bool,
    ) -> Any: ...


_injected_store: AccountStore | None = None


def set_account_store(store: AccountStore | None) -> None:
    global _injected_store
    _injected_store = store


def _store() -> AccountStore:
    return _injected_store if _injected_store is not None else _accounts_mod


def _subscription_prices(cfg: BillingConfig) -> dict[tuple[str, bool], str]:
    """The only four recurring prices Checkout and webhooks recognize."""
    return {
        ("desktop", False): cfg.desktop_price,
        ("desktop", True): cfg.desktop_edu_price,
        ("studio", False): cfg.studio_price,
        ("studio", True): cfg.studio_edu_price,
    }


def _credit_pack_prices(cfg: BillingConfig) -> dict[str, str]:
    return {
        "starter": cfg.credits_starter_price,
        "standard": cfg.credits_standard_price,
        "bulk": cfg.credits_bulk_price,
    }


def _is_edu(email: str) -> bool:
    local, separator, domain = (email or "").strip().lower().rpartition("@")
    return bool(local and separator and domain.endswith(".edu"))


def _price_for_plan(cfg: BillingConfig, plan: str, is_edu: bool) -> str:
    return _subscription_prices(cfg).get((plan, is_edu), "")


def _plan_name_for_price(cfg: BillingConfig, price_id: str | None) -> str | None:
    if not price_id:
        return None
    for (plan, _is_education), configured in _subscription_prices(cfg).items():
        if configured and configured == price_id:
            return plan
    return None


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _line_data(obj: Any) -> list[Any]:
    container = (
        _field(obj, "line_items")
        or _field(obj, "lines")
        or _field(obj, "items")
        or {}
    )
    data = _field(container, "data", [])
    return list(data or [])


def _price_id(line: Any) -> str:
    price = _field(line, "price") or {}
    if isinstance(price, str):
        return price
    return str(_field(price, "id", "") or "")


def _subscription_id(obj: Any) -> str:
    direct = _field(obj, "subscription")
    if isinstance(direct, str):
        return direct
    if direct:
        return str(_field(direct, "id", "") or "")
    parent = _field(obj, "parent") or {}
    details = _field(parent, "subscription_details") or {}
    value = _field(details, "subscription")
    if isinstance(value, str):
        return value
    return str(_field(value, "id", "") or "") if value else ""


def _subscription_line(obj: Any, cfg: BillingConfig) -> Any | None:
    lines = _line_data(obj)
    for line in lines:
        if _plan_name_for_price(cfg, _price_id(line)):
            return line
    return lines[0] if lines else None




def _downgrade(account: Any, reason: str) -> None:
    """End a paid plan without changing the human account's approval status."""
    if account is None:
        return
    if getattr(account, "plan", "trial") != "trial":
        log.info("downgrading %s to trial (%s)", account.email, reason)
        _store().set_plan(account.email, "trial")


def _process_event_once(event: Any) -> bool:
    event_id = str(event["id"])
    event_type = str(event["type"])
    with tx() as cur:
        cur.execute(
            "INSERT INTO processed_events (event_id, event_type, processed_at)"
            " VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
            (event_id, event_type, time.time()),
        )
        if cur.rowcount == 0:
            log.info("duplicate Stripe event %s (%s) — ignored", event_id, event_type)
            return False
        _apply_event(_store(), event)
        return True


def _checkout_email(obj: Any) -> str:
    metadata = _field(obj, "metadata") or {}
    details = _field(obj, "customer_details") or {}
    return str(
        _field(obj, "client_reference_id", "")
        or _field(metadata, "chudware_email", "")
        or _field(obj, "customer_email", "")
        or _field(details, "email", "")
        or ""
    ).strip().lower()


def _checkout_lines(obj: Any, stripe: Any) -> list[Any]:
    lines = _line_data(obj)
    if lines:
        return lines
    session_id = str(_field(obj, "id", "") or "")
    if not session_id:
        raise HTTPException(400, "credit checkout session has no id")
    result = stripe.checkout.Session.list_line_items(
        session_id, limit=2, expand=["data.price"]
    )
    return list(_field(result, "data", []) or [])


def _credit_price_metadata(price: Any, stripe: Any) -> tuple[str, Mapping[str, Any]]:
    if isinstance(price, str):
        price_id = price
        price = {}
    else:
        price_id = str(_field(price, "id", "") or "")
    metadata = _field(price, "metadata") or {}
    if not metadata and price_id:
        price = stripe.Price.retrieve(price_id)
        metadata = _field(price, "metadata") or {}
    return price_id, metadata


def _claim_checkout_settlement(obj: Any, email: str, kind: str) -> bool:
    """Deduplicate paid grants by Checkout/payment identity, not delivery id."""
    session_id = str(_field(obj, "id", "") or "")
    if not session_id:
        raise HTTPException(400, "paid checkout session has no id")
    payment = _field(obj, "payment_intent")
    payment_id = payment if isinstance(payment, str) else str(_field(payment, "id", "") or "")
    with tx() as cur:
        cur.execute(
            "INSERT INTO billing_checkout_settlements"
            " (checkout_session_id,email,kind,stripe_payment_intent)"
            " VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING checkout_session_id",
            (session_id, email, kind, payment_id),
        )
        return cur.fetchone() is not None


def _grant_credit_checkout(obj: Any, event: Any, cfg: BillingConfig, stripe: Any) -> None:
    email = _checkout_email(obj)
    lines = _checkout_lines(obj, stripe)
    if not email or len(lines) != 1:
        raise HTTPException(400, "invalid credit checkout identity or line items")
    price = _field(lines[0], "price") or {}
    price_id, metadata = _credit_price_metadata(price, stripe)
    if price_id not in set(_credit_pack_prices(cfg).values()):
        raise HTTPException(400, "unrecognized credit pack price")
    raw = str(_field(metadata, "chud_credits", "") or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise HTTPException(400, "credit pack price missing valid chud_credits metadata")
    if not _claim_checkout_settlement(obj, email, "payment"):
        return
    session_meta = _field(obj, "metadata") or {}
    credits.grant_purchased(
        email,
        int(raw),
        str(event["id"]),
        {
            "checkout_session_id": str(_field(obj, "id", "") or ""),
            "pack": str(_field(session_meta, "chudware_pack", "") or ""),
            "price_id": price_id,
        },
    )


def _period_bounds(
    obj: Any, cfg: BillingConfig
) -> tuple[float | None, float | None]:
    """Paid service period from the recurring plan line, never a one-off item."""
    line = _subscription_line(obj, cfg)
    period = _field(line, "period") or {} if line is not None else {}
    start = _field(period, "start")
    end = _field(period, "end")
    return (
        float(start) if isinstance(start, (int, float)) else None,
        float(end) if isinstance(end, (int, float)) else None,
    )


def _sync_renewal_price(
    account: Any, obj: Any, cfg: BillingConfig, stripe: Any
) -> tuple[str, str, bool]:
    line = _subscription_line(obj, cfg)
    actual_price = _price_id(line) if line is not None else ""
    plan = _plan_name_for_price(cfg, actual_price) or getattr(account, "plan", "trial")
    education = _is_edu(account.email)
    expected_price = _price_for_plan(cfg, plan, education)
    subscription_id = _subscription_id(obj) or getattr(account, "subscription_id", "")
    if expected_price and actual_price and expected_price != actual_price and subscription_id:
        parent = _field(line, "parent") or {}
        item_details = _field(parent, "subscription_item_details") or {}
        item_id = str(
            _field(line, "subscription_item", "")
            or _field(item_details, "subscription_item", "")
            or ""
        )
        if not item_id:
            raise HTTPException(400, "renewal line missing subscription item id")
        stripe.Subscription.modify(
            subscription_id,
            items=[{"id": item_id, "price": expected_price}],
            proration_behavior="none",
        )
        log.info(
            "renewal price corrected for %s: %s -> %s",
            account.email,
            actual_price,
            expected_price,
        )
        actual_price = expected_price
    return plan, actual_price, education


def _payment_failed_mail(account: Any, invoice: Any, cfg: BillingConfig) -> None:
    amount_minor = int(_field(invoice, "amount_due", 0) or 0)
    currency = str(_field(invoice, "currency", "usd") or "usd").upper()
    amount = f"{currency} {amount_minor / 100:.2f}"
    retry_at = _field(invoice, "next_payment_attempt")
    if isinstance(retry_at, (int, float)):
        retry_on = datetime.fromtimestamp(float(retry_at), timezone.utc).strftime(
            "%B %d"
        ).replace(" 0", " ")
    else:
        retry_on = "the date shown in your billing portal"
    update_url = str(
        _field(invoice, "hosted_invoice_url", "")
        or f"{settings.public_base_url}/account"
    )
    mailer.send(
        account.email,
        "payment_failed",
        {
            "amount": amount,
            "email": account.email,
            "retry_on": retry_on,
            "update_payment_url": update_url,
        },
    )


def _apply_event(store: AccountStore, event: Any) -> None:
    event_type = str(event["type"])
    obj = event["data"]["object"]
    cfg = load_config()
    stripe = _stripe()
    stripe.api_key = cfg.secret_key

    # Marketplace settlement gets first refusal on every event.
    #
    # Deliberately inside `_apply_event`, which `_process_event_once` calls after
    # inserting the `processed_events` row and inside the SAME `tx()`. Because
    # `db.tx()` is re-entrant, the marketplace handler's own transaction joins
    # this one, so "event recorded as processed" and "order settled, credits
    # moved, entitlement granted" commit or roll back together. A second webhook
    # route would have been the obvious design and would have had its own
    # idempotency table, its own signature verification, and no such guarantee.
    #
    # It returns True only for events it recognises as its own — marketplace
    # events carry `metadata.chud_kind == "marketplace"`, and `account.updated`
    # is matched against `mk_sellers.connect_account_id` because that is
    # authoritative where dashboard-editable metadata is not. Everything else
    # falls through to the subscription and credit-pack handling below, including
    # `checkout.session.completed`, which both consumers legitimately receive.
    #
    # Imported lazily: marketplace_commerce reads plan and account state and a
    # module-scope import here would make the two modules mutually dependent at
    # import time for no benefit.
    try:
        from marketplace_commerce import handle_marketplace_event
    except ImportError:  # pragma: no cover - marketplace is an optional module
        handle_marketplace_event = None  # type: ignore[assignment]
    if handle_marketplace_event is not None and handle_marketplace_event(event):
        return

    if event_type in {
        "checkout.session.completed", "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
    }:
        mode = str(_field(obj, "mode", "") or "")
        paid = str(_field(obj, "payment_status", "") or "") == "paid"
        if event_type == "checkout.session.async_payment_failed":
            return  # No unsettled value was granted; a later paid event may settle.
        if mode == "payment":
            if paid:
                _grant_credit_checkout(obj, event, cfg, stripe)
            return
        if mode != "subscription":
            return
        metadata = _field(obj, "metadata") or {}
        email = _checkout_email(obj)
        plan = str(_field(metadata, "chudware_plan", "") or "").strip().lower()
        if not email or not is_purchasable(plan):
            log.error(
                "checkout.session.completed %s has unusable email/plan (%r, %r)",
                event["id"],
                email,
                plan,
            )
            return
        account = store.ensure_account(email, display=email)
        customer_id = str(_field(obj, "customer", "") or "")
        if customer_id:
            store.set_stripe_customer(email, customer_id)
        education = _is_edu(email)
        if not paid:
            # Bind the provider identity so a paid invoice can find this account.
            # Neither completion nor an active subscription proves settlement.
            if not getattr(account, "subscription_id", ""):
                store.set_subscription(
                    email, subscription_id=_subscription_id(obj),
                    subscription_status="incomplete", is_edu=education,
                )
            return
        if not _claim_checkout_settlement(obj, email, "subscription"):
            return
        store.set_plan(email, plan)
        store.set_subscription(
            email,
            subscription_id=_subscription_id(obj),
            subscription_status="active",
            price_id=_price_for_plan(cfg, plan, education),
            is_edu=education,
        )
        # Billing owns payment entitlements, not account approval or operator
        # decision metadata. Even a paid checkout must not activate an account.
        log.info("entitlement upgraded: %s -> %s", account.email, plan)
        return

    if event_type == "customer.subscription.updated":
        account = store.get_account_by_stripe_customer(_field(obj, "customer", "") or "")
        if account is None:
            log.warning("subscription.updated for unknown customer — ignored")
            return
        status = str(_field(obj, "status", "") or "")
        if status in {"active", "trialing"} and not (
            getattr(account, "subscription_id", "") == str(_field(obj, "id", "") or "")
            and (
                getattr(account, "subscription_status", "") in {"active", "trialing"}
                or (getattr(account, "paid_through", None) or 0) > time.time()
            )
        ):
            status = "incomplete"
        price_id = _price_id(_subscription_line(obj, cfg) or {})
        store.set_subscription(
            account.email,
            subscription_id=str(_field(obj, "id", "") or ""),
            subscription_status=status,
            price_id=price_id,
            is_edu=_is_edu(account.email),
        )
        if status in _DOWNGRADE_STATUSES:
            _downgrade(account, f"subscription status {status}")
            return
        return

    if event_type == "customer.subscription.deleted":
        account = store.get_account_by_stripe_customer(_field(obj, "customer", "") or "")
        if account is None:
            log.warning("subscription.deleted for unknown customer — ignored")
            return
        store.set_subscription(
            account.email,
            subscription_id=str(_field(obj, "id", "") or ""),
            subscription_status="canceled",
            is_edu=_is_edu(account.email),
        )
        _downgrade(account, "subscription deleted")
        return

    if event_type == "invoice.paid":
        account = store.get_account_by_stripe_customer(_field(obj, "customer", "") or "")
        if account is None:
            log.warning("invoice.paid for unknown customer — ignored")
            return
        plan, price_id, education = _sync_renewal_price(account, obj, cfg, stripe)
        if is_purchasable(plan) and plan != getattr(account, "plan", "trial"):
            store.set_plan(account.email, plan)
        period_start, period_end = _period_bounds(obj, cfg)
        store.set_subscription(
            account.email,
            subscription_id=_subscription_id(obj),
            subscription_status="active",
            price_id=price_id,
            subscribed_since=period_start or float(_field(event, "created", time.time())),
            paid_through=period_end,
            is_edu=education,
        )
        return

    if event_type == "invoice.payment_failed":
        account = store.get_account_by_stripe_customer(_field(obj, "customer", "") or "")
        if account is None:
            log.warning("invoice.payment_failed for unknown customer — ignored")
            return
        _payment_failed_mail(account, obj, cfg)
        return

    log.info("unhandled Stripe event type %s — acknowledged, no action", event_type)


def invoice_history(email: str, limit: int = 12) -> list[dict[str, Any]]:
    """Customer-visible Stripe invoices, without exposing provider-cost data."""
    cfg = load_config()
    if not cfg.secret_key:
        return []
    account = _store().get_account(email)
    customer_id = getattr(account, "stripe_customer_id", "") if account else ""
    if not customer_id:
        return []
    stripe = _stripe()
    stripe.api_key = cfg.secret_key
    page = stripe.Invoice.list(customer=customer_id, limit=max(1, min(int(limit), 100)))
    rows = _field(page, "data", []) or []
    return [
        {
            "id": str(_field(invoice, "id", "") or ""),
            "number": str(_field(invoice, "number", "") or ""),
            "status": str(_field(invoice, "status", "") or ""),
            "created": int(_field(invoice, "created", 0) or 0),
            "amount_paid": int(_field(invoice, "amount_paid", 0) or 0),
            "currency": str(_field(invoice, "currency", "usd") or "usd").lower(),
            "hosted_invoice_url": str(_field(invoice, "hosted_invoice_url", "") or ""),
            "invoice_pdf": str(_field(invoice, "invoice_pdf", "") or ""),
        }
        for invoice in rows
    ]


router = APIRouter()
router.include_router(credits.router)


@router.get("/api/plans")
async def plan_catalog(request: Request) -> dict[str, list[dict[str, Any]]]:
    """Authenticated commercial plan catalog for every plan-selecting UI."""
    get_current_user(request)
    return {
        "plans": [
            {
                "id": name,
                "display_name": entry.display_name,
                "purchasable": is_purchasable(name),
            }
            for name, entry in PLANS.items()
        ]
    }


class CheckoutRequest(BaseModel):
    plan: str


class CreditCheckoutRequest(BaseModel):
    pack: str


@router.post("/api/billing/checkout")
async def create_checkout(body: CheckoutRequest, request: Request) -> dict[str, str]:
    cfg = load_config()
    _require_configured(cfg)
    user = get_current_user(request)
    plan = (body.plan or "").strip().lower()
    if not is_purchasable(plan):
        raise HTTPException(400, f"unknown or non-purchasable plan: {body.plan!r}")
    education = _is_edu(user.email)
    price_id = _price_for_plan(cfg, plan, education)
    if not price_id:
        raise HTTPException(503, f"billing not configured: no price for plan {plan!r}")
    account = _store().get_account(user.email)
    stripe = _stripe()
    stripe.api_key = cfg.secret_key
    metadata = {
        "chudware_email": user.email,
        "chudware_plan": plan,
        "chudware_is_edu": "true" if education else "false",
    }
    kwargs: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": cfg.success_url,
        "cancel_url": cfg.cancel_url,
        "client_reference_id": user.email,
        "metadata": metadata,
        "subscription_data": {"metadata": metadata},
    }
    customer_id = getattr(account, "stripe_customer_id", "") if account else ""
    if customer_id:
        kwargs["customer"] = customer_id
    else:
        kwargs["customer_email"] = user.email
    session = stripe.checkout.Session.create(**kwargs)
    return {"url": session.url}


@router.post("/api/billing/credits/checkout")
async def create_credit_checkout(
    body: CreditCheckoutRequest, request: Request
) -> dict[str, str]:
    cfg = load_config()
    if not cfg.credit_pack_checkout_enabled:
        raise HTTPException(
            503,
            "credit pack purchases are not available; connect your own AI provider",
        )
    _require_configured(cfg)
    user = get_current_user(request)
    pack = (body.pack or "").strip().lower()
    prices = _credit_pack_prices(cfg)
    if pack not in prices:
        raise HTTPException(400, f"unknown credit pack: {body.pack!r}")
    price_id = prices[pack]
    if not price_id:
        raise HTTPException(503, f"billing not configured: no price for credit pack {pack!r}")
    account = _store().get_account(user.email)
    stripe = _stripe()
    stripe.api_key = cfg.secret_key
    metadata = {"chudware_email": user.email, "chudware_pack": pack}
    kwargs: dict[str, Any] = {
        "mode": "payment",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": cfg.success_url,
        "cancel_url": cfg.cancel_url,
        "client_reference_id": user.email,
        "metadata": metadata,
        "payment_intent_data": {"metadata": metadata},
    }
    customer_id = getattr(account, "stripe_customer_id", "") if account else ""
    if customer_id:
        kwargs["customer"] = customer_id
    else:
        kwargs["customer_email"] = user.email
    session = stripe.checkout.Session.create(**kwargs)
    return {"url": session.url}


@router.post("/api/billing/portal")
async def create_portal(request: Request) -> dict[str, str]:
    cfg = load_config()
    _require_configured(cfg)
    user = get_current_user(request)
    account = _store().get_account(user.email)
    customer_id = getattr(account, "stripe_customer_id", "") if account else ""
    if not customer_id:
        raise HTTPException(404, "no billing customer for this account")
    stripe = _stripe()
    stripe.api_key = cfg.secret_key
    session = stripe.billing_portal.Session.create(
        customer=customer_id, return_url=cfg.success_url
    )
    return {"url": session.url}


@router.post("/api/billing/webhook")
async def stripe_webhook(request: Request) -> dict[str, str]:
    cfg = load_config()
    _require_configured(cfg)
    if not cfg.webhook_secret:
        raise HTTPException(503, "billing not configured: webhook secret missing")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "webhook payload too large")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "webhook payload too large")
    signature = request.headers.get("stripe-signature") or ""
    if not signature:
        raise HTTPException(400, "missing Stripe-Signature header")
    stripe = _stripe()
    try:
        event = stripe.Webhook.construct_event(body, signature, cfg.webhook_secret)
    except stripe.error.SignatureVerificationError:
        log.warning("rejected Stripe webhook: bad signature")
        raise HTTPException(400, "invalid signature") from None
    processed = await deployment_drain.run_blocking(_process_event_once, event)
    return {"status": "processed" if processed else "duplicate"}


@router.get("/api/billing/status")
async def billing_status(request: Request) -> dict[str, Any]:
    user = get_current_user(request)
    account = _store().get_account(user.email)
    plan = getattr(account, "plan", None) or "trial"
    try:
        limits = asdict(limits_for(plan))
    except ValueError:
        plan, limits = "trial", asdict(limits_for("trial"))
    return {
        "email": user.email,
        "plan": plan,
        "account_status": getattr(account, "status", None) if account else None,
        "stripe_customer_id": getattr(account, "stripe_customer_id", "") if account else "",
        "subscription_status": getattr(account, "subscription_status", "") if account else "",
        "paid_through": getattr(account, "paid_through", None) if account else None,
        "is_edu": getattr(account, "is_edu", False) if account else False,
        "credit_balance": getattr(account, "credit_balance", 0) if account else 0,
        "limits": limits,
        "billing_configured": billing_configured(),
    }


__all__ = [
    "AccountStore",
    "BillingConfig",
    "MAX_WEBHOOK_BYTES",
    "billing_configured",
    "load_config",
    "router",
    "set_account_store",
]
