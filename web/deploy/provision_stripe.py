#!/usr/bin/env python3
"""Declarative, guarded, idempotent Stripe catalog provisioning for Chudware.

Dry-run is deliberately offline: it renders the complete clean-account plan and does
not load a secret or contact Stripe. Apply mode first retrieves ``/v1/account`` and
refuses a mismatched account or an unacknowledged live key before any write.

This uses httpx directly instead of the Stripe SDK so the provisioning boundary is a
small, injectable HTTP transport. That makes every outgoing request observable in
unit tests and avoids coupling this operator tool to SDK resource-version behavior.
Field names and endpoints are the Stripe REST API names; nested dictionaries are
encoded as Stripe's bracketed form fields by :func:`_form_items`.
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlencode

import httpx

API_BASE = "https://api.stripe.com"
TAX_CODE = "txcd_10103001"
# Subscription and credit-pack lifecycle. billing.py's `_apply_event` switches on
# exactly these.
SUBSCRIPTION_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
]

# Marketplace settlement, handled by marketplace_commerce.handle_marketplace_event
# which billing.py's single verified endpoint delegates to. These are listed
# separately from SUBSCRIPTION_EVENTS purely for readability — Stripe takes one
# flat list per endpoint, which is what WEBHOOK_EVENTS below is.
#
# `checkout.session.completed` is deliberately NOT repeated here: it is already in
# SUBSCRIPTION_EVENTS and Stripe rejects a duplicate entry in `enabled_events`.
# The two consumers are told apart by `metadata.chud_kind`, not by event type.
MARKETPLACE_EVENTS = [
    # A buyer abandoned a marketplace checkout; the pending order must not sit
    # forever holding the one-purchase-per-buyer unique index.
    "checkout.session.expired",
    # Delayed payment methods (ACH and friends) complete the SESSION while the
    # payment is still unpaid. Settlement refuses to grant on an unpaid session,
    # so without these two a legitimate delayed purchase would never be granted
    # and a failed one would never be recorded. Omitting them turns the
    # payment_status guard from a safety check into a silent black hole.
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    # A destination charge that failed after the session completed.
    "payment_intent.payment_failed",
    # A refund revokes the buyer's entitlement, and a PARTIAL refund adjusts the
    # order's refunded totals without revoking. Without this the money goes back
    # and the download does not.
    "charge.refunded",
    # A seller's Connect account changed capability. The cached
    # mk_sellers.connect_* columns are how the buy button decides whether cash is
    # accepted, so a restriction we never hear about sells something we cannot
    # settle.
    "account.updated",
]

WEBHOOK_EVENTS = SUBSCRIPTION_EVENTS + MARKETPLACE_EVENTS

# Source of truth for both provisioning and STRIPE_CATALOG.md. Do not duplicate
# catalog values in imperative code or prose; regenerate the operator record with
# ``provision_stripe.py --write-doc`` after changing this data.
CATALOG_SPEC: dict[str, Any] = {
    "products": [
        {
            "id": "chud_desktop",
            "name": "Chudware Desktop",
            "tax_code": TAX_CODE,
            "metadata": {
                "chud_plan": "desktop",
                "chud_audience": "individual",
                "chud_publicly_listed": "true",
            },
        },
        {
            "id": "chud_studio",
            "name": "Chudware Studio",
            "tax_code": TAX_CODE,
            "metadata": {
                "chud_plan": "studio",
                "chud_audience": "individual",
                "chud_publicly_listed": "true",
            },
        },
        {
            "id": "chud_enterprise",
            "name": "Chudware Enterprise",
            "tax_code": TAX_CODE,
            "metadata": {
                "chud_plan": "enterprise",
                "chud_audience": "enterprise",
                "chud_publicly_listed": "false",
            },
        },
        {
            "id": "chud_credits",
            "name": "Chudware Credits",
            "tax_code": TAX_CODE,
            "metadata": {
                "chud_kind": "credits",
                "chud_publicly_listed": "true",
            },
        },
    ],
    "prices": [
        # ``portal_switchable`` controls whether the Customer Portal offers this
        # price as a self-service plan change. Stripe requires the prices listed
        # for one product in a portal configuration to have DISTINCT billing
        # intervals, and each product here has both a standard and an .edu price
        # at the same monthly interval — listing both fails configuration
        # creation with "For each product, its price must have unique billing
        # intervals". Excluding the .edu prices is also the correct product
        # behaviour independently of that: .edu eligibility is decided
        # server-side from the account's verified domain, so it must never be a
        # radio button a customer can select for themselves.
        {
            "lookup_key": "chud_desktop_monthly",
            "product": "chud_desktop",
            "unit_amount": 3000,
            "currency": "usd",
            "recurring": {"interval": "month"},
            "metadata": {},
            "env": "CHUDWARE_STRIPE_PRICE_DESKTOP",
            "note": "$30/month",
            "portal_switchable": True,
        },
        {
            "lookup_key": "chud_desktop_monthly_edu",
            "product": "chud_desktop",
            "unit_amount": 1500,
            "currency": "usd",
            "recurring": {"interval": "month"},
            "metadata": {},
            "env": "CHUDWARE_STRIPE_PRICE_DESKTOP_EDU",
            "note": "50% .edu price",
            "portal_switchable": False,
        },
        {
            "lookup_key": "chud_studio_monthly",
            "product": "chud_studio",
            "unit_amount": 15000,
            "currency": "usd",
            "recurring": {"interval": "month"},
            "metadata": {},
            "env": "CHUDWARE_STRIPE_PRICE_STUDIO",
            "note": "$150/month",
            "portal_switchable": True,
        },
        {
            "lookup_key": "chud_studio_monthly_edu",
            "product": "chud_studio",
            "unit_amount": 7500,
            "currency": "usd",
            "recurring": {"interval": "month"},
            "metadata": {},
            "env": "CHUDWARE_STRIPE_PRICE_STUDIO_EDU",
            "note": "50% .edu price",
            "portal_switchable": False,
        },
        {
            "lookup_key": "chud_credits_starter",
            "product": "chud_credits",
            "unit_amount": 1000,
            "currency": "usd",
            "recurring": None,
            "metadata": {"chud_credits": "8000"},
            "env": "CHUDWARE_STRIPE_PRICE_CREDITS_STARTER",
            "note": "$10 for 8,000 credits",
        },
        {
            "lookup_key": "chud_credits_standard",
            "product": "chud_credits",
            "unit_amount": 2500,
            "currency": "usd",
            "recurring": None,
            "metadata": {"chud_credits": "20000"},
            "env": "CHUDWARE_STRIPE_PRICE_CREDITS_STANDARD",
            "note": "$25 for 20,000 credits",
        },
        {
            "lookup_key": "chud_credits_bulk",
            "product": "chud_credits",
            "unit_amount": 10000,
            "currency": "usd",
            "recurring": None,
            "metadata": {"chud_credits": "80000"},
            "env": "CHUDWARE_STRIPE_PRICE_CREDITS_BULK",
            "note": "$100 for 80,000 credits",
        },
    ],
    "portal": {
        "name": "Chudware Customer Portal",
        "metadata": {"chud_managed": "true"},
        "features": {
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {
                "enabled": True,
                "mode": "at_period_end",
                "proration_behavior": "none",
            },
            "subscription_update": {
                "enabled": True,
                "default_allowed_updates": ["price"],
                "proration_behavior": "create_prorations",
                # ``products`` is populated declaratively from the price spec so
                # product/price IDs cannot drift from the catalog above.
            },
        },
    },
    # One gateway verifies one signing secret, so the production endpoint is
    # live-only: a test-mode twin at the same URL fails signature checks the
    # moment live keys land and Stripe disables it. Staging has no public
    # ingress (loopback only, see RUNBOOK.md) — test-mode delivery uses
    # ``stripe listen --forward-to`` against a local gateway instead.
    "webhooks": [
        {
            "name": "production",
            "url": "https://app.chudware.com/api/billing/webhook",
            "enabled_events": WEBHOOK_EVENTS,
            "modes": ["live"],
            "secret_env": "CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET",
        },
    ],
}


class ProvisioningError(RuntimeError):
    """A safe operator-facing provisioning failure."""


class StripeAPIError(ProvisioningError):
    def __init__(self, method: str, path: str, status: int, detail: str):
        super().__init__(f"Stripe API {method} {path}: HTTP {status}: {detail}")
        self.status = status


class StripeTransport(Protocol):
    """The complete Stripe I/O seam used by the provisioner and its fake."""

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def livemode_hint(self) -> bool | None: ...


def _form_items(values: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Flatten nested values to Stripe's bracket-notation form encoding."""
    items: list[tuple[str, str]] = []

    def add(key: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, Mapping):
            for child_key, child_value in value.items():
                add(f"{key}[{child_key}]", child_value)
        elif isinstance(value, (list, tuple)):
            for index, child_value in enumerate(value):
                suffix = f"[{index}]" if isinstance(child_value, Mapping) else "[]"
                add(f"{key}{suffix}", child_value)
        elif isinstance(value, bool):
            items.append((key, "true" if value else "false"))
        else:
            items.append((key, str(value)))

    for root_key, root_value in values.items():
        add(root_key, root_value)
    return items


class HttpxStripeTransport:
    """Minimal synchronous transport for Stripe's form-encoded REST API."""

    def __init__(self, secret_key: str):
        if not secret_key:
            raise ProvisioningError("STRIPE_SECRET_KEY is required with --apply")
        self.secret_key = secret_key
        self.client = httpx.Client(
            base_url=API_BASE,
            auth=(secret_key, ""),
            timeout=30.0,
            headers={"User-Agent": "chudware-stripe-provisioner/1"},
        )

    def livemode_hint(self) -> bool | None:
        if self.secret_key.startswith(("sk_live_", "rk_live_")):
            return True
        if self.secret_key.startswith(("sk_test_", "rk_test_")):
            return False
        return None

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        # The body is encoded here rather than handed to httpx's `data=`.
        # Stripe's bracket notation legitimately repeats a key
        # (`enabled_events[]=a&enabled_events[]=b`), so the payload has to stay a
        # LIST of pairs and cannot be collapsed into a dict. httpx 0.28 types
        # `data=` as a Mapping and, given a list of tuples, passes the tuples
        # through to h11 unencoded — which fails deep in the connection layer
        # with `TypeError: sequence item 1: expected a bytes-like object, tuple
        # found` and no hint that the request body is at fault. urlencode with an
        # explicit content type is unambiguous and does not depend on how a
        # future httpx interprets a non-Mapping.
        body = urlencode(_form_items(data or {}))
        headers = (
            {"Content-Type": "application/x-www-form-urlencoded"} if body else None
        )
        response = self.client.request(
            method,
            path,
            # `params=` genuinely does accept a list of pairs (httpx builds
            # QueryParams from it), and needs to for the same repeated-key
            # reason, so it is left as-is.
            params=_form_items(params or {}),
            content=body.encode("utf-8") if body else None,
            headers=headers,
        )
        if response.is_error:
            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except ValueError:
                detail = response.text
            raise StripeAPIError(method, path, response.status_code, detail)
        result = response.json()
        if not isinstance(result, dict):
            raise ProvisioningError(f"Stripe returned a non-object for {method} {path}")
        return result


@dataclass
class Operation:
    status: str
    resource: str
    key: str
    object_id: str
    detail: str


@dataclass
class ProvisionResult:
    account_id: str
    livemode: bool
    operations: list[Operation] = field(default_factory=list)
    product_ids: dict[str, str] = field(default_factory=dict)
    price_ids: dict[str, str] = field(default_factory=dict)
    portal_id: str = ""
    webhook_ids: dict[str, str] = field(default_factory=dict)
    webhook_secrets: dict[str, str] = field(default_factory=dict)

    @property
    def write_count(self) -> int:
        return sum(op.status != "NO-OP" for op in self.operations)


def _metadata_contains(actual: Any, expected: Mapping[str, str]) -> bool:
    return isinstance(actual, Mapping) and all(actual.get(k) == v for k, v in expected.items())


def _get_or_none(transport: StripeTransport, path: str) -> dict[str, Any] | None:
    try:
        return transport.request("GET", path)
    except StripeAPIError as exc:
        if exc.status == 404:
            return None
        raise


def _list_all(
    transport: StripeTransport, path: str, params: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    query = dict(params or {})
    query["limit"] = 100
    found: list[dict[str, Any]] = []
    while True:
        page = transport.request("GET", path, params=query)
        rows = page.get("data", [])
        if not isinstance(rows, list):
            raise ProvisioningError(f"Stripe returned invalid list data for GET {path}")
        found.extend(row for row in rows if isinstance(row, dict))
        if not page.get("has_more"):
            return found
        if not rows or not rows[-1].get("id"):
            raise ProvisioningError(f"Stripe pagination for GET {path} has no cursor")
        query["starting_after"] = rows[-1]["id"]


def _account_livemode(account: Mapping[str, Any], transport: StripeTransport) -> bool:
    value = account.get("livemode")
    if isinstance(value, bool):
        return value
    hint = transport.livemode_hint()
    if hint is None:
        raise ProvisioningError(
            "cannot determine whether the Stripe key is live; use a standard "
            "sk_test_/sk_live_ or rk_test_/rk_live_ key"
        )
    return hint


def _verify_account(
    transport: StripeTransport, expect_account: str, allow_live: bool
) -> tuple[str, bool]:
    if not expect_account:
        raise ProvisioningError(
            "refusing --apply without --expect-account or CHUDWARE_STRIPE_ACCOUNT"
        )
    # This read is intentionally the first network request. No provisioning write is
    # reachable until both account identity and key mode have passed these guards.
    account = transport.request("GET", "/v1/account")
    account_id = str(account.get("id", ""))
    if account_id != expect_account:
        raise ProvisioningError(
            f"wrong Stripe account: key belongs to {account_id or '<unknown>'}; "
            f"expected {expect_account}"
        )
    livemode = _account_livemode(account, transport)
    if livemode and not allow_live:
        raise ProvisioningError(
            "refusing to provision a live-mode Stripe account without explicit --live"
        )
    return account_id, livemode


def _product_payload(product: Mapping[str, Any], *, creating: bool) -> dict[str, Any]:
    payload = {
        "name": product["name"],
        "active": True,
        "tax_code": product["tax_code"],
        "metadata": product["metadata"],
    }
    if creating:
        payload["id"] = product["id"]
    return payload


def _product_differences(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    differences = [
        field
        for field in ("name", "tax_code")
        if actual.get(field) != expected.get(field)
    ]
    if actual.get("active") is not True:
        differences.append("active")
    if not _metadata_contains(actual.get("metadata"), expected["metadata"]):
        differences.append("metadata")
    return differences


def _price_payload(price: Mapping[str, Any], *, transfer: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "product": price["product"],
        "unit_amount": price["unit_amount"],
        "currency": price["currency"],
        "lookup_key": price["lookup_key"],
        "metadata": price["metadata"],
        "active": True,
    }
    if price.get("recurring") is not None:
        payload["recurring"] = price["recurring"]
    if transfer:
        payload["transfer_lookup_key"] = True
    return payload


def _immutable_price_differences(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> list[str]:
    differences: list[str] = []
    for field in ("unit_amount", "currency", "product"):
        if actual.get(field) != expected.get(field):
            differences.append(field)
    actual_recurring = actual.get("recurring")
    expected_recurring = expected.get("recurring")
    if expected_recurring is None:
        if actual_recurring is not None:
            differences.append("recurring")
    elif not isinstance(actual_recurring, Mapping) or any(
        actual_recurring.get(k) != v for k, v in expected_recurring.items()
    ):
        differences.append("recurring")
    return differences


def _portal_payload(
    spec: Mapping[str, Any], price_ids: Mapping[str, str]
) -> dict[str, Any]:
    portal = copy.deepcopy(spec["portal"])
    groups: list[dict[str, Any]] = []
    for product_id in ("chud_desktop", "chud_studio"):
        prices = [
            price_ids[price["lookup_key"]]
            for price in spec["prices"]
            if price["product"] == product_id and price.get("portal_switchable")
        ]
        # A product group with no switchable price is not a degenerate group to
        # send with an empty list; Stripe rejects that. Omit the product instead,
        # which correctly means "not self-service switchable".
        if not prices:
            continue
        groups.append(
            {
                "product": product_id,
                "prices": prices,
                "adjustable_quantity": {"enabled": False},
            }
        )
    portal["features"]["subscription_update"]["products"] = groups
    return portal


def _portal_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if actual.get("name") != expected["name"]:
        return False
    if not _metadata_contains(actual.get("metadata"), expected["metadata"]):
        return False
    actual_features = actual.get("features")
    if not isinstance(actual_features, Mapping):
        return False
    for feature_name in ("payment_method_update", "subscription_cancel"):
        expected_feature = expected["features"][feature_name]
        actual_feature = actual_features.get(feature_name)
        if not isinstance(actual_feature, Mapping) or any(
            actual_feature.get(k) != v for k, v in expected_feature.items()
        ):
            return False
    expected_update = expected["features"]["subscription_update"]
    actual_update = actual_features.get("subscription_update")
    if not isinstance(actual_update, Mapping):
        return False
    for key in ("enabled", "default_allowed_updates", "proration_behavior"):
        if actual_update.get(key) != expected_update[key]:
            return False
    normalize = lambda rows: sorted(  # noqa: E731 - compact comparison helper
        (
            row.get("product"),
            tuple(sorted(row.get("prices", []))),
            bool(row.get("adjustable_quantity", {}).get("enabled")),
        )
        for row in rows
    )
    return normalize(actual_update.get("products", [])) == normalize(
        expected_update["products"]
    )


def _webhook_payload(endpoint: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "url": endpoint["url"],
        "enabled_events": endpoint["enabled_events"],
        "description": f"Chudware {endpoint['name']} billing webhook",
        "metadata": {
            "chud_managed": "true",
            "chud_environment": endpoint["name"],
        },
    }


def _webhook_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return (
        actual.get("url") == expected["url"]
        and set(actual.get("enabled_events", [])) == set(expected["enabled_events"])
        and actual.get("description") == expected["description"]
        and _metadata_contains(actual.get("metadata"), expected["metadata"])
        and actual.get("status", "enabled") == "enabled"
    )


def provision(
    transport: StripeTransport,
    *,
    expect_account: str,
    allow_live: bool = False,
    spec: Mapping[str, Any] = CATALOG_SPEC,
) -> ProvisionResult:
    """Reconcile ``spec`` into Stripe after enforcing account and mode guards."""
    account_id, livemode = _verify_account(transport, expect_account, allow_live)
    result = ProvisionResult(account_id=account_id, livemode=livemode)

    for product in spec["products"]:
        product_id = product["id"]
        existing = _get_or_none(transport, f"/v1/products/{product_id}")
        if existing is None:
            created = transport.request(
                "POST", "/v1/products", data=_product_payload(product, creating=True)
            )
            result.operations.append(
                Operation("CREATE", "product", product_id, created["id"], "stable product id")
            )
            result.product_ids[product_id] = created["id"]
            continue
        differences = _product_differences(existing, product)
        if differences:
            updated = transport.request(
                "POST",
                f"/v1/products/{product_id}",
                data=_product_payload(product, creating=False),
            )
            result.operations.append(
                Operation(
                    "UPDATE",
                    "product",
                    product_id,
                    updated["id"],
                    "fields: " + ", ".join(differences),
                )
            )
        else:
            updated = existing
            result.operations.append(
                Operation("NO-OP", "product", product_id, existing["id"], "already exact")
            )
        result.product_ids[product_id] = updated["id"]

    for price in spec["prices"]:
        lookup_key = price["lookup_key"]
        matches_by_id: dict[str, dict[str, Any]] = {}
        # Stripe's price list defaults to active prices. Query both states so an
        # inactive price that still owns the lookup key is reconciled rather than
        # causing a duplicate-key failure on create.
        for active in (True, False):
            for match in _list_all(
                transport,
                "/v1/prices",
                {"lookup_keys": [lookup_key], "active": active},
            ):
                matches_by_id[match["id"]] = match
        if len(matches_by_id) > 1:
            raise ProvisioningError(
                f"multiple prices unexpectedly hold lookup_key {lookup_key}"
            )
        existing = next(iter(matches_by_id.values()), None)
        if existing is None:
            created = transport.request(
                "POST", "/v1/prices", data=_price_payload(price)
            )
            result.operations.append(
                Operation("CREATE", "price", lookup_key, created["id"], "new lookup_key")
            )
            result.price_ids[lookup_key] = created["id"]
            continue

        immutable_changes = _immutable_price_differences(existing, price)
        if immutable_changes:
            created = transport.request(
                "POST", "/v1/prices", data=_price_payload(price, transfer=True)
            )
            transport.request(
                "POST", f"/v1/prices/{existing['id']}", data={"active": False}
            )
            changes = ", ".join(
                f"{field} {existing.get(field)!r} -> {price.get(field)!r}"
                for field in immutable_changes
            )
            result.operations.append(
                Operation(
                    "REPLACE",
                    "price",
                    lookup_key,
                    created["id"],
                    f"{changes}; transfer_lookup_key=true; deactivated {existing['id']}",
                )
            )
            result.price_ids[lookup_key] = created["id"]
            continue

        mutable: dict[str, Any] = {}
        mutable_fields: list[str] = []
        if not _metadata_contains(existing.get("metadata"), price["metadata"]):
            mutable["metadata"] = price["metadata"]
            mutable_fields.append("metadata")
        if existing.get("active") is not True:
            mutable["active"] = True
            mutable_fields.append("active")
        if mutable:
            updated = transport.request(
                "POST", f"/v1/prices/{existing['id']}", data=mutable
            )
            result.operations.append(
                Operation(
                    "UPDATE",
                    "price",
                    lookup_key,
                    updated["id"],
                    "mutable fields: " + ", ".join(mutable_fields),
                )
            )
        else:
            updated = existing
            result.operations.append(
                Operation("NO-OP", "price", lookup_key, existing["id"], "already exact")
            )
        result.price_ids[lookup_key] = updated["id"]

    portal_payload = _portal_payload(spec, result.price_ids)
    configurations = _list_all(transport, "/v1/billing_portal/configurations")
    managed = next(
        (
            item
            for item in configurations
            if _metadata_contains(item.get("metadata"), {"chud_managed": "true"})
        ),
        None,
    )
    existing_portal = managed or next(
        (item for item in configurations if item.get("is_default") is True), None
    )
    if existing_portal is not None:
        # ``features.subscription_update.products`` is an EXPANDABLE field: Stripe
        # accepts and validates it on write but omits it from every response
        # unless it is explicitly expanded. Comparing against the unexpanded list
        # response therefore always saw `products: []`, never matched the spec,
        # and reported UPDATE on every single run — a provisioner that never
        # converges, rewriting the live portal configuration on each deploy. Re-
        # retrieve the one candidate with the expansion so the comparison has the
        # field it is comparing.
        existing_portal = transport.request(
            "GET",
            f"/v1/billing_portal/configurations/{existing_portal['id']}",
            params={"expand": ["features.subscription_update.products"]},
        )
    if existing_portal is None:
        portal = transport.request(
            "POST", "/v1/billing_portal/configurations", data=portal_payload
        )
        result.operations.append(
            Operation("CREATE", "portal", spec["portal"]["name"], portal["id"], "configuration")
        )
    elif not _portal_matches(existing_portal, portal_payload):
        portal = transport.request(
            "POST",
            f"/v1/billing_portal/configurations/{existing_portal['id']}",
            data=portal_payload,
        )
        result.operations.append(
            Operation("UPDATE", "portal", spec["portal"]["name"], portal["id"], "configuration")
        )
    else:
        portal = existing_portal
        result.operations.append(
            Operation("NO-OP", "portal", spec["portal"]["name"], portal["id"], "already exact")
        )
    result.portal_id = portal["id"]

    existing_webhooks = _list_all(transport, "/v1/webhook_endpoints")
    target_mode = "live" if livemode else "test"
    for endpoint in spec["webhooks"]:
        if target_mode not in endpoint["modes"]:
            continue
        payload = _webhook_payload(endpoint)
        existing = next(
            (item for item in existing_webhooks if item.get("url") == endpoint["url"]),
            None,
        )
        if existing is None:
            webhook = transport.request(
                "POST", "/v1/webhook_endpoints", data=payload
            )
            result.operations.append(
                Operation("CREATE", "webhook", endpoint["name"], webhook["id"], endpoint["url"])
            )
            secret = webhook.get("secret")
            if secret:
                result.webhook_secrets[endpoint["secret_env"]] = secret
        elif not _webhook_matches(existing, payload):
            update_payload = dict(payload)
            # ``disabled`` is accepted only by the update endpoint. Sending false
            # makes a previously disabled managed endpoint usable again.
            update_payload["disabled"] = False
            webhook = transport.request(
                "POST",
                f"/v1/webhook_endpoints/{existing['id']}",
                data=update_payload,
            )
            result.operations.append(
                Operation("UPDATE", "webhook", endpoint["name"], webhook["id"], endpoint["url"])
            )
        else:
            webhook = existing
            result.operations.append(
                Operation("NO-OP", "webhook", endpoint["name"], webhook["id"], endpoint["url"])
            )
        result.webhook_ids[endpoint["name"]] = webhook["id"]

    return result


def dry_run_result(
    *, expect_account: str, live: bool, spec: Mapping[str, Any] = CATALOG_SPEC
) -> ProvisionResult:
    """Return the offline clean-account plan; this function performs zero I/O."""
    mode = "live" if live else "test"
    result = ProvisionResult(account_id=expect_account or "<required-on-apply>", livemode=live)
    for product in spec["products"]:
        result.product_ids[product["id"]] = product["id"]
        result.operations.append(
            Operation("CREATE", "product", product["id"], product["id"], "if absent; otherwise reconcile")
        )
    for price in spec["prices"]:
        placeholder = f"<price_for_{price['lookup_key']}>"
        result.price_ids[price["lookup_key"]] = placeholder
        result.operations.append(
            Operation(
                "CREATE",
                "price",
                price["lookup_key"],
                placeholder,
                "if absent; replace immutable mismatch, update mutable mismatch, otherwise no-op",
            )
        )
    result.portal_id = "<bpc_created_or_reused>"
    result.operations.append(
        Operation("CREATE", "portal", spec["portal"]["name"], result.portal_id, "if no managed/default configuration")
    )
    for endpoint in spec["webhooks"]:
        if mode not in endpoint["modes"]:
            continue
        placeholder = f"<we_for_{endpoint['name']}>"
        result.webhook_ids[endpoint["name"]] = placeholder
        result.operations.append(
            Operation("CREATE", "webhook", endpoint["name"], placeholder, endpoint["url"])
        )
    return result


def env_fragment(result: ProvisionResult, spec: Mapping[str, Any] = CATALOG_SPEC) -> str:
    return "\n".join(
        f"{price['env']}={result.price_ids[price['lookup_key']]}"
        for price in spec["prices"]
    )


def format_result(result: ProvisionResult, *, dry_run: bool) -> str:
    mode = "live" if result.livemode else "test"
    title = "DRY RUN — zero Stripe API calls" if dry_run else "APPLIED"
    lines = [
        title,
        f"Account: {result.account_id}",
        f"Stripe mode: {mode}",
        "",
        f"{'STATUS':<8} {'RESOURCE':<9} {'KEY':<32} {'ID':<38} DETAIL",
        f"{'-' * 8} {'-' * 9} {'-' * 32} {'-' * 38} {'-' * 20}",
    ]
    for op in result.operations:
        lines.append(
            f"{op.status:<8} {op.resource:<9} {op.key:<32} {op.object_id:<38} {op.detail}"
        )
    lines.extend(
        [
            "",
            "Resulting price IDs",
            f"{'ENVIRONMENT VARIABLE':<48} {'LOOKUP KEY':<32} PRICE ID",
        ]
    )
    for price in CATALOG_SPEC["prices"]:
        lines.append(
            f"{price['env']:<48} {price['lookup_key']:<32} "
            f"{result.price_ids[price['lookup_key']]}"
        )
    lines.extend(["", ".env fragment", env_fragment(result)])
    if result.webhook_secrets:
        lines.extend(
            [
                "",
                "NEW WEBHOOK SIGNING SECRETS — Stripe reveals each only once; save now",
            ]
        )
        lines.extend(f"{key}={value}" for key, value in result.webhook_secrets.items())
    return "\n".join(lines)


def _metadata_text(metadata: Mapping[str, str]) -> str:
    return "<br>".join(f"`{key}={value}`" for key, value in metadata.items()) or "—"


def render_catalog_markdown(spec: Mapping[str, Any] = CATALOG_SPEC) -> str:
    """Generate the checked-in operator record directly from ``CATALOG_SPEC``."""
    lines = [
        "# Chudware Stripe catalog",
        "",
        "<!-- Generated by provision_stripe.py --write-doc. Do not hand-edit catalog tables. -->",
        "",
        "This is the sandbox/live duplication record for the catalog managed by",
        "`Webservice/deploy/provision_stripe.py`. Amounts are in Stripe's smallest currency",
        "unit. Fill the final ID columns from the apply output after each account is provisioned.",
        "",
        "## Products",
        "",
        "| Stable product id | Name | Tax code | Metadata | Sandbox `prod_`/stable id | Live `prod_`/stable id |",
        "|---|---|---|---|---|---|",
    ]
    for product in spec["products"]:
        lines.append(
            f"| `{product['id']}` | {product['name']} | `{product['tax_code']}` | "
            f"{_metadata_text(product['metadata'])} | — | — |"
        )
    lines.extend(
        [
            "",
            "## Prices",
            "",
            "| Lookup key | Product | USD amount | Billing | Price metadata | Gateway env var | Sandbox `price_` id | Live `price_` id |",
            "|---|---|---:|---|---|---|---|---|",
        ]
    )
    for price in spec["prices"]:
        billing = "monthly recurring" if price["recurring"] else "one-time"
        lines.append(
            f"| `{price['lookup_key']}` | `{price['product']}` | `{price['unit_amount']}` "
            f"(${price['unit_amount'] / 100:,.2f}) | {billing} | "
            f"{_metadata_text(price['metadata'])} | `{price['env']}` | — | — |"
        )
    portal = spec["portal"]
    subscription_update = portal["features"]["subscription_update"]
    lines.extend(
        [
            "",
            "## Customer Portal configuration",
            "",
            f"Managed configuration name: **{portal['name']}**; metadata "
            f"{_metadata_text(portal['metadata'])}.",
            "",
            "| Capability | Declarative setting |",
            "|---|---|",
            f"| Payment-method updates | `enabled={str(portal['features']['payment_method_update']['enabled']).lower()}` |",
            f"| Cancellation | `enabled=true`, `mode={portal['features']['subscription_cancel']['mode']}`, `proration_behavior={portal['features']['subscription_cancel']['proration_behavior']}` |",
            f"| Plan switching | `enabled=true`, `default_allowed_updates={subscription_update['default_allowed_updates']}`, `proration_behavior={subscription_update['proration_behavior']}`; products `chud_desktop` and `chud_studio` with their four recurring prices |",
            "| Quantity changes | Disabled: `quantity` is absent from `default_allowed_updates`, and every product has `adjustable_quantity.enabled=false` |",
            "",
            "Configuration IDs: sandbox `________________`; live `________________`.",
            "",
            "## Webhook endpoints",
            "",
            "| Environment | URL | Stripe modes | Enabled events (exact) | Signing-secret env var | Sandbox `we_` id | Live `we_` id |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for endpoint in spec["webhooks"]:
        events = "<br>".join(f"`{event}`" for event in endpoint["enabled_events"])
        modes = ", ".join(endpoint["modes"])
        lines.append(
            f"| {endpoint['name']} | `{endpoint['url']}` | {modes} | {events} | "
            f"`{endpoint['secret_env']}` | — | — |"
        )
    lines.extend(
        [
            "",
            "Stripe returns a webhook signing secret only when the endpoint is created. Copy the",
            "printed secret immediately into the production `gateway.env`; a no-op rerun cannot",
            "recover it. The endpoint is live-only: one gateway verifies one secret, and staging",
            "has no public ingress. Sandbox delivery uses `stripe listen --forward-to` locally.",
            "",
            "## Sandbox provisioning checklist",
            "",
            "1. Create the separate Chudware Stripe account and obtain its **test** secret key.",
            "2. In the Stripe Dashboard, enable Stripe Tax for the account. Products are assigned",
            f"   the SaaS tax code `{TAX_CODE}` by the provisioner.",
            "3. Run the offline preview, then apply to the exact expected account:",
            "",
            "```bash",
            "export STRIPE_SECRET_KEY='sk_test_...'",
            "export CHUDWARE_STRIPE_ACCOUNT='acct_...'",
            "python3 Webservice/deploy/provision_stripe.py --dry-run --expect-account \"$CHUDWARE_STRIPE_ACCOUNT\"",
            "python3 Webservice/deploy/provision_stripe.py --apply --expect-account \"$CHUDWARE_STRIPE_ACCOUNT\"",
            "```",
            "",
            "4. Paste the emitted `CHUDWARE_STRIPE_PRICE_*` fragment into the sandbox",
            "   `gateway.env`. Fill the sandbox ID columns above from the result table. Rerun",
            "   `--apply`; every row must report `NO-OP`.",
            "",
            "## Live provisioning checklist (phase-7 cutover only)",
            "",
            "1. Complete the launch smoke in sandbox. Obtain the same Chudware account's **live**",
            "   secret key; never reuse a sandbox price or endpoint id.",
            "2. Confirm Stripe Tax is enabled in live mode, then explicitly acknowledge live mode:",
            "",
            "```bash",
            "export STRIPE_SECRET_KEY='sk_live_...'",
            "export CHUDWARE_STRIPE_ACCOUNT='acct_...'",
            "python3 Webservice/deploy/provision_stripe.py --dry-run --live --expect-account \"$CHUDWARE_STRIPE_ACCOUNT\"",
            "python3 Webservice/deploy/provision_stripe.py --apply --live --expect-account \"$CHUDWARE_STRIPE_ACCOUNT\"",
            "```",
            "",
            "3. Store the live production webhook secret and live `CHUDWARE_STRIPE_PRICE_*` IDs",
            "   in the live `gateway.env`; fill the live ID columns. Rerun and require all no-ops.",
            "",
            "## Regenerating this record",
            "",
            "After an intentional spec change, regenerate rather than editing tables by hand:",
            "",
            "```bash",
            "python3 Webservice/deploy/provision_stripe.py --write-doc",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="offline clean-account plan (default; zero API calls)"
    )
    mode.add_argument("--apply", action="store_true", help="reconcile the catalog through Stripe")
    parser.add_argument(
        "--expect-account",
        default=os.environ.get("CHUDWARE_STRIPE_ACCOUNT", ""),
        help="required account id guard (or CHUDWARE_STRIPE_ACCOUNT)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly permit a live-mode key and render/apply the live endpoint set",
    )
    parser.add_argument(
        "--write-doc",
        action="store_true",
        help="regenerate deploy/STRIPE_CATALOG.md from the declarative spec and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.write_doc:
        destination = Path(__file__).with_name("STRIPE_CATALOG.md")
        destination.write_text(render_catalog_markdown(), encoding="utf-8")
        print(f"wrote {destination}")
        return 0
    try:
        if args.apply:
            transport = HttpxStripeTransport(os.environ.get("STRIPE_SECRET_KEY", ""))
            result = provision(
                transport,
                expect_account=args.expect_account,
                allow_live=args.live,
            )
            print(format_result(result, dry_run=False))
        else:
            result = dry_run_result(
                expect_account=args.expect_account, live=args.live
            )
            print(format_result(result, dry_run=True))
    except ProvisioningError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
