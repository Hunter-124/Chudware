"""Operator billing, Stripe health, revenue, and Connect administration."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

try:
    from . import db
    from .config import settings
    from .limits import api_writes
    from .marketplace_authz import require_admin
    from .marketplace_schema import (
        CursorError,
        LEDGER_MARKET_BUY,
        LEDGER_MARKET_EARN,
        LEDGER_MARKET_PAYOUT,
        decode_cursor,
        encode_cursor,
    )
except ImportError:  # uvicorn app:app from the package dir
    import db
    from config import settings
    from limits import api_writes
    from marketplace_authz import require_admin
    from marketplace_schema import (
        CursorError,
        LEDGER_MARKET_BUY,
        LEDGER_MARKET_EARN,
        LEDGER_MARKET_PAYOUT,
        decode_cursor,
        encode_cursor,
    )


router = APIRouter(prefix="/api/admin/billing", tags=["admin-billing"])
_PAGE_SIZE = 50
_MAX_STRIPE_PAGES = 100
_SUBSCRIPTION_LIVE = ("active", "trialing")
_SUBSCRIPTION_STATUSES = ("active", "trialing", "past_due", "canceled")
_REFUND_REASONS = {"duplicate", "fraudulent", "requested_by_customer"}


class RefundBody(BaseModel):
    payment_intent: str = ""
    order_id: str = ""
    amount_cents: int | None = None
    amount_credits: int | None = None
    reason: str
    confirm: str


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stripe_module() -> tuple[Any | None, str]:
    if not settings.stripe_secret_key.strip():
        return None, "Stripe secret key is not configured"
    try:
        import stripe
    except ImportError:
        return None, "Stripe SDK is not installed"
    stripe.api_key = settings.stripe_secret_key.strip()
    return stripe, ""


def _stripe_call(label: str, call: Callable[[], Any]) -> tuple[Any | None, str]:
    try:
        return call(), ""
    except Exception as exc:  # Stripe exposes several transport-specific exception classes.
        return None, f"{label}: {type(exc).__name__}: {exc}"


def _stripe_pages(
    label: str,
    call: Callable[[str | None], Any],
) -> tuple[list[Any], str]:
    rows: list[Any] = []
    cursor: str | None = None
    for _ in range(_MAX_STRIPE_PAGES):
        page, reason = _stripe_call(label, lambda cursor=cursor: call(cursor))
        if reason:
            return rows, reason
        data = list(_field(page, "data", []) or [])
        rows.extend(data)
        if not bool(_field(page, "has_more", False)) or not data:
            return rows, ""
        cursor = str(_field(data[-1], "id", "") or "")
        if not cursor:
            return rows, f"{label}: Stripe returned a page without a continuation id"
    return rows, f"{label}: stopped after {_MAX_STRIPE_PAGES} pages"


def _load_provision_contract() -> tuple[dict[str, Any] | None, list[str], str]:
    path = Path(__file__).resolve().parents[1] / "deploy" / "provision_stripe.py"
    if not path.is_file():
        return None, [], f"catalog contract unavailable at {path}"
    name = "_chudware_admin_provision_stripe"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError("could not create module spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        catalog = getattr(module, "CATALOG_SPEC")
        events = list(getattr(module, "WEBHOOK_EVENTS"))
        if not isinstance(catalog, dict):
            raise TypeError("CATALOG_SPEC is not a mapping")
        return catalog, events, ""
    except Exception as exc:
        sys.modules.pop(name, None)
        return None, [], f"catalog contract unavailable: {type(exc).__name__}: {exc}"


def _month_bounds(now: datetime) -> tuple[datetime, datetime, datetime]:
    current = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    previous_last = current - timedelta(days=1)
    previous = datetime(previous_last.year, previous_last.month, 1, tzinfo=timezone.utc)
    if current.month == 12:
        following = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        following = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
    return previous, current, following


def _money_by_currency(rows: list[Any], amount_field: str) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for row in rows:
        currency = str(_field(row, "currency", "usd") or "usd").lower()
        totals[currency] += int(_field(row, amount_field, 0) or 0)
    return dict(sorted(totals.items()))


def _credit_sessions(
    stripe: Any,
    start: datetime,
    end: datetime,
) -> tuple[list[Any], str]:
    params = {
        "created": {"gte": int(start.timestamp()), "lt": int(end.timestamp())},
        "limit": 100,
    }

    def page(starting_after: str | None) -> Any:
        args = dict(params)
        if starting_after:
            args["starting_after"] = starting_after
        return stripe.checkout.Session.list(**args)

    sessions, reason = _stripe_pages("credit checkout sessions", page)
    credit_prices = {
        settings.stripe_price_credits_starter.strip(),
        settings.stripe_price_credits_standard.strip(),
        settings.stripe_price_credits_bulk.strip(),
    } - {""}
    filtered = []
    for session in sessions:
        metadata = _field(session, "metadata", {}) or {}
        if str(_field(session, "mode", "") or "") != "payment":
            continue
        if str(_field(session, "payment_status", "") or "") not in {"paid", "no_payment_required"}:
            continue
        if str(_field(metadata, "chudware_pack", "") or ""):
            filtered.append(session)
            continue
        line_price = str(_field(metadata, "price_id", "") or "")
        if line_price and line_price in credit_prices:
            filtered.append(session)
    return filtered, reason


def _paid_invoices(stripe: Any, start: datetime, end: datetime) -> tuple[list[Any], str]:
    params = {
        "created": {"gte": int(start.timestamp()), "lt": int(end.timestamp())},
        "status": "paid",
        "limit": 100,
    }

    def page(starting_after: str | None) -> Any:
        args = dict(params)
        if starting_after:
            args["starting_after"] = starting_after
        return stripe.Invoice.list(**args)

    return _stripe_pages("paid invoices", page)


def _price_monthly_cents(price: Any) -> int | None:
    amount = _field(price, "unit_amount")
    if amount is None:
        return None
    recurring = _field(price, "recurring", {}) or {}
    interval = str(_field(recurring, "interval", "") or "")
    count = max(1, int(_field(recurring, "interval_count", 1) or 1))
    amount = int(amount)
    if interval == "month":
        return amount // count
    if interval == "year":
        return amount // (12 * count)
    if interval == "week":
        return round(amount * 52 / (12 * count))
    if interval == "day":
        return round(amount * 365 / (12 * count))
    return None


def _decode_single_cursor(raw: str, sort: str) -> str:
    if not raw:
        return ""
    try:
        keys = decode_cursor(raw, expect_sort=sort)
    except CursorError as exc:
        raise HTTPException(400, "invalid cursor") from exc
    if len(keys) != 1 or not isinstance(keys[0], str):
        raise HTTPException(400, "invalid cursor")
    return keys[0]


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    require_admin(request)
    with db.tx() as cur:
        cur.execute(
            "SELECT plan, subscription_status, price_id, is_edu, count(*) AS count "
            "FROM accounts GROUP BY plan, subscription_status, price_id, is_edu"
        )
        subscription_rows = [dict(row) for row in cur.fetchall()]
        cur.execute(
            "SELECT currency, COALESCE(sum(amount_cents), 0) AS gmv_cents, "
            "COALESCE(sum(amount_credits), 0) AS gmv_credits, "
            "COALESCE(sum(fee_cents) FILTER (WHERE status='paid'), 0) AS fee_cents, "
            "COALESCE(sum(fee_credits) FILTER (WHERE status='paid'), 0) AS fee_credits "
            "FROM mk_orders WHERE status IN ('paid','refunded') GROUP BY currency ORDER BY currency"
        )
        marketplace_rows = [dict(row) for row in cur.fetchall()]
        cur.execute(
            "SELECT count(*) AS count FROM ("
            "SELECT email FROM accounts WHERE subscribed_since IS NOT NULL "
            "UNION SELECT buyer_email FROM mk_orders WHERE status = 'paid'"
            ") paying"
        )
        unique_paying = int((cur.fetchone() or {}).get("count") or 0)
        cur.execute(
            "SELECT count(*) AS count FROM processed_events "
            "WHERE event_type = 'invoice.payment_failed' AND processed_at >= %s",
            (time.time() - 30 * 86400,),
        )
        failed_payments = int((cur.fetchone() or {}).get("count") or 0)

    counts: dict[str, dict[str, int]] = {}
    active_account_count = 0
    for row in subscription_rows:
        plan = str(row["plan"] or "trial")
        status = str(row["subscription_status"] or "none")
        plan_counts = counts.setdefault(plan, {name: 0 for name in _SUBSCRIPTION_STATUSES})
        if status in plan_counts:
            plan_counts[status] += int(row["count"] or 0)
        if status in _SUBSCRIPTION_LIVE:
            active_account_count += int(row["count"] or 0)

    stripe, stripe_reason = _stripe_module()
    reasons: dict[str, str] = {}
    price_cache: dict[str, Any] = {}
    mrr_cents: int | None = 0 if stripe is not None else None
    if stripe is None:
        reasons["mrr"] = stripe_reason
    else:
        for row in subscription_rows:
            if str(row["subscription_status"] or "") not in _SUBSCRIPTION_LIVE:
                continue
            price_id = str(row["price_id"] or "")
            if not price_id:
                reasons.setdefault("mrr", "one or more active subscriptions have no Stripe price id")
                continue
            if price_id not in price_cache:
                price, reason = _stripe_call(
                    f"price {price_id}", lambda price_id=price_id: stripe.Price.retrieve(price_id)
                )
                if reason:
                    reasons.setdefault("mrr", reason)
                    price_cache[price_id] = None
                else:
                    price_cache[price_id] = price
            monthly = _price_monthly_cents(price_cache[price_id]) if price_cache[price_id] else None
            if monthly is None:
                reasons.setdefault("mrr", f"price {price_id} has no supported recurring amount")
                continue
            mrr_cents = int(mrr_cents or 0) + monthly * int(row["count"] or 0)
        if "mrr" in reasons:
            mrr_cents = None

    previous, current, following = _month_bounds(datetime.now(timezone.utc))
    credit_revenue: dict[str, Any] = {
        "this_month": None,
        "last_month": None,
        "this_month_by_currency": {},
        "last_month_by_currency": {},
    }
    if stripe is None:
        reasons["credit_pack_revenue"] = stripe_reason
    else:
        current_sessions, current_reason = _credit_sessions(stripe, current, following)
        previous_sessions, previous_reason = _credit_sessions(stripe, previous, current)
        if current_reason or previous_reason:
            reasons["credit_pack_revenue"] = current_reason or previous_reason
        else:
            current_totals = _money_by_currency(current_sessions, "amount_total")
            previous_totals = _money_by_currency(previous_sessions, "amount_total")
            credit_revenue = {
                "this_month": current_totals.get("usd", 0),
                "last_month": previous_totals.get("usd", 0),
                "this_month_by_currency": current_totals,
                "last_month_by_currency": previous_totals,
            }

    marketplace = {
        str(row["currency"]): {
            "gmv_cents": int(row["gmv_cents"] or 0),
            "gmv_credits": int(row["gmv_credits"] or 0),
            "platform_fee_cents": int(row["fee_cents"] or 0),
            "platform_fee_credits": int(row["fee_credits"] or 0),
        }
        for row in marketplace_rows
    }
    arpu = None
    if mrr_cents is not None and active_account_count:
        arpu = round(mrr_cents / active_account_count)
    return {
        "stripe_configured": stripe is not None,
        "reasons": reasons,
        "mrr_cents": mrr_cents,
        "arr_cents": None if mrr_cents is None else mrr_cents * 12,
        "subscription_counts": counts,
        "active_subscription_accounts": active_account_count,
        "credit_pack_revenue_cents": credit_revenue,
        "marketplace": marketplace,
        "unique_paying_accounts": unique_paying,
        "arpu_mrr_cents": arpu,
        "failed_payments_30d": failed_payments,
    }


@router.get("/timeseries")
async def timeseries(request: Request, days: int = Query(90, ge=7, le=366)) -> dict[str, Any]:
    require_admin(request)
    with db.tx() as cur:
        cur.execute(
            "WITH days AS ("
            "  SELECT generate_series(current_date - (%s::int - 1), current_date, interval '1 day')::date AS day"
            "), market AS ("
            "  SELECT paid_at::date AS day, "
            "    sum(amount_cents) FILTER (WHERE currency='usd') AS cash_gmv, "
            "    sum(amount_credits) FILTER (WHERE currency='credits') AS credit_gmv, "
            "    sum(fee_cents) AS fee_cents, sum(fee_credits) AS fee_credits "
            "  FROM mk_orders WHERE status IN ('paid','refunded') "
            "    AND paid_at >= current_date - (%s::int - 1) GROUP BY paid_at::date"
            "), first_paid AS ("
            "  SELECT email, min(day) AS day FROM ("
            "    SELECT email, to_timestamp(subscribed_since)::date AS day FROM accounts "
            "      WHERE subscribed_since IS NOT NULL "
            "    UNION ALL SELECT buyer_email, paid_at::date FROM mk_orders WHERE paid_at IS NOT NULL"
            "  ) all_paid GROUP BY email"
            "), new_payers AS (SELECT day, count(*) AS count FROM first_paid GROUP BY day) "
            "SELECT days.day, COALESCE(market.cash_gmv,0) AS marketplace_cash_gmv_cents, "
            "COALESCE(market.credit_gmv,0) AS marketplace_credit_gmv_credits, "
            "COALESCE(market.fee_cents,0) AS platform_fee_cents, "
            "COALESCE(market.fee_credits,0) AS platform_fee_credits, "
            "COALESCE(new_payers.count,0) AS new_paying_accounts "
            "FROM days LEFT JOIN market USING(day) LEFT JOIN new_payers USING(day) ORDER BY days.day",
            (days, days),
        )
        rows = [dict(row) for row in cur.fetchall()]

    start = datetime.combine(date.today() - timedelta(days=days - 1), datetime.min.time(), timezone.utc)
    end = datetime.combine(date.today() + timedelta(days=1), datetime.min.time(), timezone.utc)
    subscription_by_day: dict[str, int] = defaultdict(int)
    credits_by_day: dict[str, int] = defaultdict(int)
    reasons: dict[str, str] = {}
    stripe, stripe_reason = _stripe_module()
    if stripe is None:
        reasons["subscription_revenue"] = stripe_reason
        reasons["credit_pack_revenue"] = stripe_reason
    else:
        invoices, invoice_reason = _paid_invoices(stripe, start, end)
        sessions, session_reason = _credit_sessions(stripe, start, end)
        for invoice in invoices:
            created = int(_field(invoice, "created", 0) or 0)
            if created:
                key = datetime.fromtimestamp(created, timezone.utc).date().isoformat()
                subscription_by_day[key] += int(_field(invoice, "amount_paid", 0) or 0)
        for session in sessions:
            created = int(_field(session, "created", 0) or 0)
            if created:
                key = datetime.fromtimestamp(created, timezone.utc).date().isoformat()
                credits_by_day[key] += int(_field(session, "amount_total", 0) or 0)
        if invoice_reason:
            reasons["subscription_revenue"] = invoice_reason
        if session_reason:
            reasons["credit_pack_revenue"] = session_reason

    items = []
    for row in rows:
        key = row["day"].isoformat()
        items.append(
            {
                "date": key,
                "subscription_revenue_cents": subscription_by_day.get(key, 0),
                "credit_pack_revenue_cents": credits_by_day.get(key, 0),
                "marketplace_cash_gmv_cents": int(row["marketplace_cash_gmv_cents"] or 0),
                "marketplace_credit_gmv_credits": int(row["marketplace_credit_gmv_credits"] or 0),
                "platform_fee_cents": int(row["platform_fee_cents"] or 0),
                "platform_fee_credits": int(row["platform_fee_credits"] or 0),
                "new_paying_accounts": int(row["new_paying_accounts"] or 0),
            }
        )
    return {
        "days": days,
        "credits_per_dollar": 1000,
        "stripe_configured": stripe is not None,
        "reasons": reasons,
        "items": items,
    }


@router.get("/subscriptions")
async def subscriptions(
    request: Request,
    cursor: str = "",
    status: str = "",
) -> dict[str, Any]:
    require_admin(request)
    after = _decode_single_cursor(cursor, "admin-subscriptions")
    status = status.strip().lower()
    if status and status not in {*_SUBSCRIPTION_STATUSES, "unpaid", "incomplete", "none"}:
        raise HTTPException(400, "invalid subscription status")
    clauses = ["email > %s"]
    params: list[Any] = [after]
    if status == "none":
        clauses.append("subscription_status = ''")
    elif status:
        clauses.append("subscription_status = %s")
        params.append(status)
    params.append(_PAGE_SIZE + 1)
    with db.tx() as cur:
        cur.execute(
            "SELECT email, plan, subscription_status, price_id, subscribed_since, paid_through, "
            "credit_balance, is_edu FROM accounts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY email LIMIT %s",
            tuple(params),
        )
        rows = [dict(row) for row in cur.fetchall()]
    has_more = len(rows) > _PAGE_SIZE
    rows = rows[:_PAGE_SIZE]
    return {
        "items": rows,
        "next_cursor": encode_cursor("admin-subscriptions", [rows[-1]["email"]]) if has_more else None,
    }


def _invoice_card(invoice: Any) -> dict[str, Any]:
    customer = _field(invoice, "customer")
    customer_id = customer if isinstance(customer, str) else _field(customer, "id", "")
    payment_intent = _field(invoice, "payment_intent")
    payment_intent_id = (
        payment_intent if isinstance(payment_intent, str) else _field(payment_intent, "id", "")
    )
    return {
        "id": str(_field(invoice, "id", "") or ""),
        "number": str(_field(invoice, "number", "") or ""),
        "customer": str(customer_id or ""),
        "customer_email": str(_field(invoice, "customer_email", "") or ""),
        "status": str(_field(invoice, "status", "") or ""),
        "created": int(_field(invoice, "created", 0) or 0),
        "due_date": int(_field(invoice, "due_date", 0) or 0),
        "amount_due": int(_field(invoice, "amount_due", 0) or 0),
        "amount_paid": int(_field(invoice, "amount_paid", 0) or 0),
        "amount_remaining": int(_field(invoice, "amount_remaining", 0) or 0),
        "currency": str(_field(invoice, "currency", "usd") or "usd").lower(),
        "attempt_count": int(_field(invoice, "attempt_count", 0) or 0),
        "payment_intent": str(payment_intent_id or ""),
        "hosted_invoice_url": str(_field(invoice, "hosted_invoice_url", "") or ""),
    }


@router.get("/invoices")
async def invoices(request: Request, cursor: str = "", status: str = "") -> dict[str, Any]:
    require_admin(request)
    after = _decode_single_cursor(cursor, "admin-invoices")
    status = status.strip().lower()
    if status and status not in {"draft", "open", "paid", "uncollectible", "void"}:
        raise HTTPException(400, "invalid invoice status")
    stripe, reason = _stripe_module()
    if stripe is None:
        return {"items": [], "next_cursor": None, "stripe_configured": False, "reason": reason}
    params: dict[str, Any] = {"limit": _PAGE_SIZE}
    if after:
        params["starting_after"] = after
    if status:
        params["status"] = status
    page, reason = _stripe_call("invoices", lambda: stripe.Invoice.list(**params))
    if reason:
        return {"items": [], "next_cursor": None, "stripe_configured": True, "reason": reason}
    data = list(_field(page, "data", []) or [])
    has_more = bool(_field(page, "has_more", False)) and bool(data)
    return {
        "items": [_invoice_card(row) for row in data],
        "next_cursor": encode_cursor("admin-invoices", [str(_field(data[-1], "id", ""))]) if has_more else None,
        "stripe_configured": True,
        "reason": "",
    }


@router.get("/credits")
async def credit_activity(request: Request, days: int = Query(90, ge=7, le=366)) -> dict[str, Any]:
    require_admin(request)
    with db.tx() as cur:
        cur.execute(
            "WITH days AS ("
            " SELECT generate_series(current_date-(%s::int-1), current_date, interval '1 day')::date AS day"
            "), kinds AS ("
            " SELECT unnest(ARRAY['purchase','spend','grant','adjust','refund',%s,%s,%s]) AS kind"
            "), totals AS ("
            " SELECT ts::date AS day, kind, sum(credits_delta) AS amount FROM credit_ledger "
            " WHERE ts >= current_date-(%s::int-1) GROUP BY ts::date, kind"
            ") SELECT days.day, kinds.kind, COALESCE(totals.amount,0) AS amount "
            "FROM days CROSS JOIN kinds LEFT JOIN totals USING(day,kind) ORDER BY days.day,kinds.kind",
            (days, LEDGER_MARKET_BUY, LEDGER_MARKET_EARN, LEDGER_MARKET_PAYOUT, days),
        )
        raw = [dict(row) for row in cur.fetchall()]
    grouped: dict[str, dict[str, int]] = {}
    for row in raw:
        key = row["day"].isoformat()
        grouped.setdefault(key, {})[str(row["kind"])] = int(row["amount"] or 0)
    items = []
    for key, kinds in grouped.items():
        issuance = sum(max(0, kinds.get(kind, 0)) for kind in ("purchase", "grant", "adjust", "refund"))
        items.append(
            {
                "date": key,
                "by_kind": kinds,
                "issuance": issuance,
                "ai_consumption": max(0, -kinds.get("spend", 0)),
                "market_buy": kinds.get(LEDGER_MARKET_BUY, 0),
                "market_earn": kinds.get(LEDGER_MARKET_EARN, 0),
                "market_payout": kinds.get(LEDGER_MARKET_PAYOUT, 0),
            }
        )
    totals: dict[str, int] = defaultdict(int)
    for row in raw:
        totals[str(row["kind"])] += int(row["amount"] or 0)
    return {"days": days, "totals_by_kind": dict(totals), "items": items}


@router.get("/webhooks")
async def webhooks(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    require_admin(request)
    catalog, expected_events, contract_reason = _load_provision_contract()
    stripe, stripe_reason = _stripe_module()
    endpoints: list[dict[str, Any]] = []
    endpoint_reason = stripe_reason
    if stripe is not None:
        page, endpoint_reason = _stripe_call(
            "webhook endpoints", lambda: stripe.WebhookEndpoint.list(limit=100)
        )
        if page is not None:
            for endpoint in list(_field(page, "data", []) or []):
                enabled = sorted(str(item) for item in (_field(endpoint, "enabled_events", []) or []))
                wildcard = "*" in enabled
                expected = set(expected_events)
                actual = set(enabled)
                endpoints.append(
                    {
                        "id": str(_field(endpoint, "id", "") or ""),
                        "url": str(_field(endpoint, "url", "") or ""),
                        "status": str(_field(endpoint, "status", "") or ""),
                        "livemode": bool(_field(endpoint, "livemode", False)),
                        "enabled_events": enabled,
                        "missing_events": [] if wildcard else sorted(expected - actual),
                        "unexpected_events": [] if wildcard else sorted(actual - expected),
                    }
                )
    coverage: dict[str, int] = defaultdict(int)
    for endpoint in endpoints:
        enabled = set(endpoint["enabled_events"])
        for event_type in expected_events:
            if "*" in enabled or event_type in enabled:
                coverage[event_type] += 1
    with db.tx() as cur:
        cur.execute(
            "SELECT event_id, event_type, processed_at FROM processed_events "
            "ORDER BY processed_at DESC, event_id DESC LIMIT %s",
            (limit,),
        )
        events = [dict(row) for row in cur.fetchall()]
    for event in events:
        event["configured_endpoint_count"] = coverage.get(str(event["event_type"]), 0)
    return {
        "stripe_configured": stripe is not None,
        "reason": endpoint_reason,
        "contract_reason": contract_reason,
        "expected_events": expected_events,
        "endpoints": endpoints,
        "events": events,
        "catalog_webhooks": list((catalog or {}).get("webhooks", [])),
    }


def _catalog_drift(
    catalog: dict[str, Any], products: list[Any], prices: list[Any]
) -> dict[str, Any]:
    live_products = {str(_field(item, "id", "") or ""): item for item in products}
    live_prices = {
        str(_field(item, "lookup_key", "") or ""): item
        for item in prices
        if _field(item, "lookup_key", "")
    }
    missing_products: list[str] = []
    product_mismatches: list[dict[str, Any]] = []
    for expected in catalog.get("products", []):
        product_id = str(expected["id"])
        live = live_products.get(product_id)
        if live is None:
            missing_products.append(product_id)
            continue
        mismatches = {}
        for key in ("name", "tax_code"):
            if str(_field(live, key, "") or "") != str(expected.get(key, "") or ""):
                mismatches[key] = {"expected": expected.get(key), "actual": _field(live, key, "")}
        if mismatches:
            product_mismatches.append({"id": product_id, "fields": mismatches})
    missing_prices: list[str] = []
    price_mismatches: list[dict[str, Any]] = []
    for expected in catalog.get("prices", []):
        lookup = str(expected["lookup_key"])
        live = live_prices.get(lookup)
        if live is None:
            missing_prices.append(lookup)
            continue
        live_product = _field(live, "product", "")
        if not isinstance(live_product, str):
            live_product = _field(live_product, "id", "")
        recurring = _field(live, "recurring", {}) or {}
        expected_recurring = expected.get("recurring") or {}
        actual = {
            "product": str(live_product or ""),
            "unit_amount": _field(live, "unit_amount"),
            "currency": str(_field(live, "currency", "") or ""),
            "interval": str(_field(recurring, "interval", "") or ""),
        }
        wanted = {
            "product": expected.get("product"),
            "unit_amount": expected.get("unit_amount"),
            "currency": expected.get("currency"),
            "interval": str(expected_recurring.get("interval", "") or ""),
        }
        mismatches = {
            key: {"expected": wanted[key], "actual": actual[key]}
            for key in wanted
            if wanted[key] != actual[key]
        }
        if mismatches:
            price_mismatches.append({"lookup_key": lookup, "fields": mismatches})
    return {
        "ok": not (missing_products or product_mismatches or missing_prices or price_mismatches),
        "missing_products": missing_products,
        "product_mismatches": product_mismatches,
        "missing_prices": missing_prices,
        "price_mismatches": price_mismatches,
        "expected_product_count": len(catalog.get("products", [])),
        "expected_price_count": len(catalog.get("prices", [])),
    }


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    require_admin(request)
    catalog, _events, contract_reason = _load_provision_contract()
    stripe, stripe_reason = _stripe_module()
    if stripe is None:
        return {
            "stripe_configured": False,
            "reason": stripe_reason,
            "contract_reason": contract_reason,
            "account": None,
            "livemode": None,
            "catalog": None,
        }
    account, account_reason = _stripe_call("Stripe account", lambda: stripe.Account.retrieve())
    product_ids = [str(item["id"]) for item in (catalog or {}).get("products", [])]
    products: Any | None = None
    product_reason = ""
    if product_ids:
        products, product_reason = _stripe_call(
            "Stripe products",
            lambda: stripe.Product.list(limit=100, active=True, ids=product_ids),
        )
    elif catalog is not None:
        products = {"data": []}
    lookup_keys = [str(item["lookup_key"]) for item in (catalog or {}).get("prices", [])]
    prices: Any | None = None
    price_reason = ""
    if lookup_keys:
        prices, price_reason = _stripe_call(
            "Stripe prices", lambda: stripe.Price.list(limit=100, active=True, lookup_keys=lookup_keys)
        )
    elif catalog is not None:
        prices = {"data": []}
    reasons = [reason for reason in (account_reason, product_reason, price_reason) if reason]
    account_summary = None
    if account is not None:
        account_summary = {
            "id": str(_field(account, "id", "") or ""),
            "country": str(_field(account, "country", "") or ""),
            "default_currency": str(_field(account, "default_currency", "") or ""),
            "type": str(_field(account, "type", "") or ""),
            "business_type": str(_field(account, "business_type", "") or ""),
            "charges_enabled": bool(_field(account, "charges_enabled", False)),
            "payouts_enabled": bool(_field(account, "payouts_enabled", False)),
            "details_submitted": bool(_field(account, "details_submitted", False)),
        }
    drift = None
    if catalog is not None and products is not None and prices is not None:
        drift = _catalog_drift(
            catalog,
            list(_field(products, "data", []) or []),
            list(_field(prices, "data", []) or []),
        )
    return {
        "stripe_configured": True,
        "reason": "; ".join(reasons),
        "contract_reason": contract_reason,
        "account": account_summary,
        "livemode": bool(_field(account, "livemode", settings.stripe_secret_key.startswith("sk_live_"))) if account is not None else settings.stripe_secret_key.startswith("sk_live_"),
        "catalog": drift,
    }


@router.get("/connect")
async def connect(request: Request, cursor: str = "") -> dict[str, Any]:
    require_admin(request)
    after = _decode_single_cursor(cursor, "admin-connect")
    with db.tx() as cur:
        cur.execute(
            "SELECT email, handle, display_name, country, connect_account_id, "
            "connect_charges_enabled, connect_payouts_enabled, connect_details_submitted, "
            "connect_refreshed_at, publishing_blocked, blocked_reason "
            "FROM mk_sellers WHERE email > %s ORDER BY email LIMIT %s",
            (after, _PAGE_SIZE + 1),
        )
        rows = [dict(row) for row in cur.fetchall()]
    has_more = len(rows) > _PAGE_SIZE
    rows = rows[:_PAGE_SIZE]
    stripe, stripe_reason = _stripe_module()
    items = []
    for row in rows:
        account_id = str(row["connect_account_id"] or "")
        live = None
        reason = ""
        if not account_id:
            reason = "Connect account has not been created"
        elif stripe is None:
            reason = stripe_reason
        else:
            live, reason = _stripe_call(
                f"Connect account {account_id}",
                lambda account_id=account_id: stripe.Account.retrieve(account_id),
            )
        requirements = _field(live, "requirements", {}) or {}
        disabled_reason = str(_field(requirements, "disabled_reason", "") or "")
        currently_due = list(_field(requirements, "currently_due", []) or [])
        eventually_due = list(_field(requirements, "eventually_due", []) or [])
        charges_enabled = bool(_field(live, "charges_enabled", row["connect_charges_enabled"]))
        payouts_enabled = bool(_field(live, "payouts_enabled", row["connect_payouts_enabled"]))
        details_submitted = bool(_field(live, "details_submitted", row["connect_details_submitted"]))
        payable = bool(live is not None and charges_enabled and payouts_enabled and details_submitted and not disabled_reason)
        why_not = reason or disabled_reason
        if not why_not and not details_submitted:
            why_not = "account details are incomplete"
        if not why_not and currently_due:
            why_not = "requirements currently due"
        if not why_not and not charges_enabled:
            why_not = "charges are disabled"
        if not why_not and not payouts_enabled:
            why_not = "payouts are disabled"
        items.append(
            {
                **row,
                "payable": payable,
                "reason": "" if payable else why_not,
                "live": {
                    "charges_enabled": charges_enabled,
                    "payouts_enabled": payouts_enabled,
                    "details_submitted": details_submitted,
                    "disabled_reason": disabled_reason,
                    "currently_due": currently_due,
                    "eventually_due": eventually_due,
                },
            }
        )
    return {
        "items": items,
        "next_cursor": encode_cursor("admin-connect", [rows[-1]["email"]]) if has_more else None,
        "stripe_configured": stripe is not None,
        "reason": stripe_reason,
    }


@router.post("/refund")
async def refund(request: Request, body: RefundBody) -> dict[str, Any]:
    user = require_admin(request)
    api_writes.enforce(user.email, "billing writes")
    payment_intent = body.payment_intent.strip()
    order_id = body.order_id.strip()
    if bool(payment_intent) == bool(order_id):
        raise HTTPException(
            400, "provide exactly one of payment_intent or order_id"
        )
    target = order_id or payment_intent
    if body.confirm != target:
        raise HTTPException(
            400, "confirm must exactly match the refund target"
        )
    if order_id:
        try:
            order_uuid = str(uuid.UUID(order_id))
        except ValueError as exc:
            raise HTTPException(400, "order_id must be a UUID") from exc
    else:
        order_uuid = ""
    if (
        body.amount_cents is not None
        and body.amount_credits is not None
    ):
        raise HTTPException(
            400, "provide only one refund amount"
        )
    if body.amount_cents is not None and body.amount_cents <= 0:
        raise HTTPException(400, "amount_cents must be positive")
    if body.amount_credits is not None and body.amount_credits <= 0:
        raise HTTPException(400, "amount_credits must be positive")
    reason = body.reason.strip()
    if reason not in _REFUND_REASONS:
        raise HTTPException(
            400,
            "reason must be duplicate, fraudulent, or requested_by_customer",
        )
    with db.tx() as cur:
        if order_id:
            cur.execute(
                "SELECT id FROM mk_orders WHERE id = %s",
                (order_uuid,),
            )
        else:
            cur.execute(
                """
                SELECT id FROM mk_orders
                WHERE stripe_payment_intent = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (payment_intent,),
            )
        order = cur.fetchone()
    if order is not None:
        try:
            try:
                from .marketplace_commerce import refund_order
            except ImportError:
                from marketplace_commerce import refund_order
        except ImportError as exc:
            raise HTTPException(
                503, "marketplace commerce is unavailable"
            ) from exc
        return {
            "kind": "marketplace",
            "refund": refund_order(
                str(order["id"]),
                user.email,
                amount_cents=body.amount_cents,
                amount_credits=body.amount_credits,
                reason=reason,
            ),
        }
    if order_id:
        raise HTTPException(404, "marketplace order not found")
    if body.amount_credits is not None:
        raise HTTPException(
            400, "amount_credits requires a marketplace order_id"
        )

    stripe, stripe_reason = _stripe_module()
    if stripe is None:
        raise HTTPException(503, stripe_reason)
    key_material = (
        f"{payment_intent}:{body.amount_cents or 'full'}:{reason}"
    ).encode("utf-8")
    idempotency_key = (
        "chud-admin-refund-" + hashlib.sha256(key_material).hexdigest()
    )
    params: dict[str, Any] = {
        "payment_intent": payment_intent,
        "reason": reason,
    }
    if body.amount_cents is not None:
        params["amount"] = body.amount_cents
    result, stripe_error = _stripe_call(
        "refund",
        lambda: stripe.Refund.create(
            **params, idempotency_key=idempotency_key
        ),
    )
    if stripe_error:
        raise HTTPException(502, stripe_error)
    return {
        "kind": "stripe",
        "refund": {
            "id": str(_field(result, "id", "") or ""),
            "status": str(_field(result, "status", "") or ""),
            "amount": int(_field(result, "amount", 0) or 0),
            "currency": str(_field(result, "currency", "") or ""),
            "payment_intent": str(
                _field(result, "payment_intent", payment_intent)
                or payment_intent
            ),
        },
    }
