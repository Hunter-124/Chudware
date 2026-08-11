"""Stripe billing: Checkout, Customer Portal, and webhook-driven entitlement.

Architecture
------------
- plans.py is the source of truth for what a plan grants. Stripe only carries
  the price id, resolved from the process environment via the env-var NAMES in
  plans.py. A client can request a plan NAME; it can never supply a price id
  or amount — those come from the server-side environment only.
- The entitlement lives in the accounts store (gateway/accounts.py, owned by a
  sibling module). This module never touches the accounts SQLite file directly;
  it calls the narrow AccountStore interface below, which is bound either to
  the real ``accounts`` module at import time or injected via
  ``set_account_store()`` (tests / wiring).
- Everything degrades to a clear 503 when Stripe is not configured, and this
  module imports cleanly with neither the ``stripe`` package nor any keys
  present — the gateway keeps booting on a box with no Stripe setup.
- ``stripe`` is imported lazily inside the functions that need it.

Environment (read directly here; the lead should later fold these into
gateway/config.py Settings as ``stripe_*`` fields):
  CHUDWARE_WEB_STRIPE_SECRET_KEY      — sk_live_... / sk_test_...
  CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET  — whsec_... for the signing endpoint
  CHUDWARE_WEB_STRIPE_PRICE_PRO       — price_... for the Pro tier
  CHUDWARE_WEB_STRIPE_PRICE_TEAM      — price_... for the Team tier
  CHUDWARE_WEB_STRIPE_SUCCESS_URL     — Checkout return URL (optional)
  CHUDWARE_WEB_STRIPE_CANCEL_URL      — Checkout abort URL (optional)

SECURITY: POST /api/billing/webhook is a NEW unauthenticated route reachable
from the internet (Stripe calls it). It must be added to auth.PUBLIC_PATHS
(see the wiring snippet in the rollout report). Its only protection is Stripe
signature verification — anything unverified is rejected with 400 before any
parsing or side effect, the body is size-capped, and event ids are persisted in
the same transaction as the entitlement mutation so Stripe's at-least-once
redelivery cannot apply an entitlement twice.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

try:
    from .auth import get_current_user
    from .config import settings
    from .db import tx
    from .plans import PLANS, is_purchasable, limits_for
except ImportError:  # uvicorn app:app from the package dir
    from auth import get_current_user
    from config import settings
    from db import tx
    from plans import PLANS, is_purchasable, limits_for

log = logging.getLogger("chudware-web.billing")

# Stripe webhook payloads for subscription/checkout events are a few KiB;
# 64 KiB is generous headroom and keeps a junk POST from being buffered.
MAX_WEBHOOK_BYTES = 64 * 1024

DEFAULT_SUCCESS_URL = "https://app.chudware.com/?billing=success"
DEFAULT_CANCEL_URL = "https://app.chudware.com/?billing=cancel"

# Subscription states that mean the customer is NOT currently entitled.
_DOWNGRADE_STATUSES = {"past_due", "unpaid", "canceled", "incomplete_expired"}


# ---------------------------------------------------------------------------
# Configuration (env-driven; read per call so tests and ops can rotate keys
# without a module reload)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BillingConfig:
    secret_key: str
    webhook_secret: str
    success_url: str
    cancel_url: str


def load_config(env: Any = None) -> BillingConfig:
    env = os.environ if env is None else env
    return BillingConfig(
        secret_key=(env.get("CHUDWARE_WEB_STRIPE_SECRET_KEY") or "").strip(),
        webhook_secret=(env.get("CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET") or "").strip(),
        success_url=(env.get("CHUDWARE_WEB_STRIPE_SUCCESS_URL") or "").strip()
        or DEFAULT_SUCCESS_URL,
        cancel_url=(env.get("CHUDWARE_WEB_STRIPE_CANCEL_URL") or "").strip()
        or DEFAULT_CANCEL_URL,
    )


def billing_configured() -> bool:
    return bool(load_config().secret_key)


def _require_configured(cfg: BillingConfig) -> None:
    if not cfg.secret_key:
        raise HTTPException(503, "billing not configured")


def _stripe():
    """Import stripe lazily — the module must load without the package installed."""
    try:
        import stripe
    except ImportError as exc:
        raise HTTPException(
            503, "billing not configured: stripe package not installed"
        ) from exc
    return stripe


# ---------------------------------------------------------------------------
# Account store adapter (gateway/accounts.py — interface agreed with its owner)
# ---------------------------------------------------------------------------


@runtime_checkable
class AccountStore(Protocol):
    """The exact helpers billing needs from gateway/accounts.py."""

    def get_account(self, email: str) -> Any: ...
    def get_account_by_stripe_customer(self, customer_id: str) -> Any: ...
    def ensure_account(self, email: str, display: str) -> Any: ...
    def set_plan(self, email: str, plan: str) -> Any: ...
    def set_stripe_customer(self, email: str, customer_id: str) -> Any: ...
    def set_status(
        self, email: str, status: str, decided_by: str = "", notes: str = ""
    ) -> Any: ...


try:  # real store, when the sibling module is present
    from . import accounts as _accounts_mod  # type: ignore[import-not-found]
except ImportError:
    try:
        import accounts as _accounts_mod  # type: ignore[no-redef]
    except ImportError:
        _accounts_mod = None

_injected_store: AccountStore | None = None


def set_account_store(store: AccountStore | None) -> None:
    """Bind the account store explicitly (tests, or wiring before accounts.py lands)."""
    global _injected_store
    _injected_store = store


def _store() -> AccountStore:
    store = _injected_store if _injected_store is not None else _accounts_mod
    if store is None:
        raise HTTPException(503, "account store unavailable")
    return store  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Webhook idempotency: persist processed Stripe event ids (at-least-once
# redelivery must not re-apply an entitlement)
# ---------------------------------------------------------------------------


def _process_event_once(event: Any) -> bool:
    """Apply ``event`` exactly once. Returns False for a redelivery.

    The idempotency record and the entitlement mutation commit in ONE
    transaction: ``tx()`` is re-entrant, so the ``accounts`` writes performed by
    ``_apply_event`` join the transaction opened here instead of running in
    their own. A crash mid-handler therefore rolls BOTH back and Stripe's retry
    is processed normally, and a half-applied entitlement is not reachable.

    Insert first, ask questions later: ``ON CONFLICT DO NOTHING`` takes the row
    lock, so two concurrent deliveries of the same event serialize on it and the
    loser sees zero affected rows. A SELECT-then-INSERT would let both pass the
    check before either wrote.
    """
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


# ---------------------------------------------------------------------------
# Entitlement transitions
# ---------------------------------------------------------------------------


def _plan_name_for_price(price_id: str | None) -> str | None:
    if not price_id:
        return None
    for name, plan in PLANS.items():
        if plan.stripe_price_env and os.environ.get(plan.stripe_price_env) == price_id:
            return name
    return None


def _plan_from_subscription(obj: Any) -> str | None:
    meta = obj.get("metadata") or {}
    name = (meta.get("chudware_plan") or "").strip().lower()
    if name in PLANS:
        return name
    items = (obj.get("items") or {}).get("data") or []
    if items:
        return _plan_name_for_price((items[0].get("price") or {}).get("id"))
    return None


def _downgrade(account: Any, reason: str) -> None:
    """Revoke paid entitlement (cancellation / payment failure) -> free tier.

    The account's approval STATUS is left alone: an expired card does not
    un-approve the human, it only ends what they are paying for.
    """
    if account is None:
        return
    if getattr(account, "plan", "free") != "free":
        log.info("downgrading %s to free (%s)", account.email, reason)
        _store().set_plan(account.email, "free")


def _apply_event(store: AccountStore, event: Any) -> None:
    """Mutate the entitlement for one verified, not-yet-processed event."""
    event_type = str(event["type"])
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        meta = obj.get("metadata") or {}
        email = (
            (obj.get("client_reference_id") or "").strip().lower()
            or (meta.get("chudware_email") or "").strip().lower()
            or (obj.get("customer_email") or "").strip().lower()
            or ((obj.get("customer_details") or {}).get("email") or "").strip().lower()
        )
        plan = (meta.get("chudware_plan") or "").strip().lower()
        if not email or not is_purchasable(plan):
            # Poison event (our own metadata was wrong). Log loud, mark
            # processed — retrying it forever would change nothing.
            log.error(
                "checkout.session.completed %s with unusable email/plan "
                "(email=%r plan=%r) — entitlement NOT applied",
                event["id"],
                email,
                plan,
            )
            return
        account = store.ensure_account(email, display=email)
        customer_id = obj.get("customer") or ""
        if customer_id:
            store.set_stripe_customer(email, customer_id)
        store.set_plan(email, plan)
        store.set_status(
            email,
            "active",
            decided_by="stripe",
            notes=f"checkout.session.completed {event['id']}",
        )
        log.info(
            "entitlement upgraded: %s -> %s (customer %s)",
            account.email,
            plan,
            customer_id,
        )
        return

    if event_type == "customer.subscription.updated":
        account = store.get_account_by_stripe_customer(obj.get("customer") or "")
        if account is None:
            log.warning(
                "subscription.updated for unknown customer %s — ignored",
                obj.get("customer"),
            )
            return
        sub_status = str(obj.get("status") or "")
        if sub_status in _DOWNGRADE_STATUSES:
            _downgrade(account, f"subscription status {sub_status}")
            return
        if sub_status in {"active", "trialing"}:
            plan = _plan_from_subscription(obj)
            if plan and plan != getattr(account, "plan", "free"):
                store.set_plan(account.email, plan)
                log.info("plan synced from subscription: %s -> %s", account.email, plan)
        return

    if event_type == "customer.subscription.deleted":
        account = store.get_account_by_stripe_customer(obj.get("customer") or "")
        if account is None:
            log.warning(
                "subscription.deleted for unknown customer %s — ignored",
                obj.get("customer"),
            )
            return
        _downgrade(account, "subscription deleted")
        return

    log.info("unhandled Stripe event type %s — acknowledged, no action", event_type)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

router = APIRouter()


class CheckoutRequest(BaseModel):
    # The ONLY client input is the plan name. There is deliberately no price
    # id / amount field — extra keys a client sends are ignored by pydantic.
    plan: str


@router.post("/api/billing/checkout")
async def create_checkout(body: CheckoutRequest, request: Request) -> dict[str, str]:
    cfg = load_config()
    _require_configured(cfg)
    user = get_current_user(request)

    name = (body.plan or "").strip().lower()
    if not is_purchasable(name):
        raise HTTPException(400, f"unknown or non-purchasable plan: {body.plan!r}")
    plan = PLANS[name]
    price_id = os.environ.get(plan.stripe_price_env or "", "").strip()
    if not price_id:
        raise HTTPException(503, f"billing not configured: no price for plan {name!r}")

    store = _store()
    account = store.get_account(user.email)
    stripe = _stripe()
    stripe.api_key = cfg.secret_key

    kwargs: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": cfg.success_url,
        "cancel_url": cfg.cancel_url,
        "client_reference_id": user.email,
        "metadata": {"chudware_email": user.email, "chudware_plan": name},
        "subscription_data": {
            "metadata": {"chudware_email": user.email, "chudware_plan": name}
        },
    }
    existing_customer = getattr(account, "stripe_customer_id", None) if account else None
    if existing_customer:
        kwargs["customer"] = existing_customer
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
    customer_id = getattr(account, "stripe_customer_id", None) if account else None
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
    """Stripe -> entitlement. UNAUTHENTICATED (must be in auth.PUBLIC_PATHS).

    Trust model: the Stripe-Signature header over the RAW body, verified
    against the endpoint secret. Anything else is rejected before parsing.
    """
    cfg = load_config()
    _require_configured(cfg)
    if not cfg.webhook_secret:
        raise HTTPException(503, "billing not configured: webhook secret missing")

    # Body size cap — check the declared length before buffering, then the real
    # one after (a liar content-length must not slip an oversized body through).
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "webhook payload too large")
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "webhook payload too large")

    sig = request.headers.get("stripe-signature") or ""
    if not sig:
        raise HTTPException(400, "missing Stripe-Signature header")

    stripe = _stripe()
    try:
        event = stripe.Webhook.construct_event(body, sig, cfg.webhook_secret)
    except stripe.error.SignatureVerificationError:
        log.warning("rejected Stripe webhook: bad signature")
        raise HTTPException(400, "invalid signature") from None

    processed = _process_event_once(event)
    return {"status": "processed" if processed else "duplicate"}


@router.get("/api/billing/status")
async def billing_status(request: Request) -> dict[str, Any]:
    """The caller's own plan and subscription state. Works without Stripe keys."""
    user = get_current_user(request)
    account = _store().get_account(user.email)
    plan = getattr(account, "plan", None) or "free"
    try:
        limits = asdict(limits_for(plan))
    except ValueError:
        plan, limits = "free", asdict(limits_for("free"))
    return {
        "email": user.email,
        "plan": plan,
        "account_status": getattr(account, "status", None) if account else None,
        "stripe_customer_id": (
            getattr(account, "stripe_customer_id", None) if account else None
        ),
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
