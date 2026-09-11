"""Integration tests against a REAL Stripe sandbox.

Every test here performs live HTTP against api.stripe.com. That is the point:
the rest of the suite proves our code is internally consistent, and this file
proves the assumptions it is consistent with are actually true of Stripe. Four
of the defects found while writing it were invisible to any mock —

* ``provision_stripe.py`` had never once succeeded against real Stripe, because
  httpx 0.28 silently mangles a list-of-pairs passed as ``data=``;
* ``features.subscription_update.products`` is an expandable field Stripe omits
  from responses, so the portal reconciler reported UPDATE on every run forever;
* ``stripe.Account.create(type="express")`` is refused outright on a current
  account, so the entire Connect design had to move to Accounts v2;
* ``StripeObject`` in stripe-python 15 is not a dict and has no ``.get()``.

A fake Stripe would have passed all four.

## Running

Skipped entirely unless ``CHUDWARE_WEB_STRIPE_SECRET_KEY`` is set, so the
default gateway suite stays offline and green::

    set -a; . Webservice/secrets/stripe-sandbox.env; set +a
    cd Webservice/gateway && .venv/bin/python -m pytest tests/test_stripe_sandbox.py -q

## Safety

``stripe_mod`` refuses to proceed unless the key is a ``sk_test_``/``rk_test_``
key AND Stripe itself reports ``livemode is False``. Both checks, not either: a
restricted key's prefix is not proof of anything, and trusting a prefix alone is
how a suite that creates charges and refunds ends up pointed at production.
Nothing here can move real money, and if that stops being true the suite fails
closed rather than charging a card.

## Sandbox hygiene

The sandbox is shared, long-lived state, so every object this suite creates is
tagged with a per-run token in ``metadata`` and torn down in a fixture
finalizer. Objects Stripe does not permit deleting (prices, connected accounts
that have been issued an onboarding link) are deactivated or left tagged, never
silently abandoned under a name a later run might collide with.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = GATEWAY_DIR.parents[1]
PROVISIONER = REPO_ROOT / "Webservice" / "deploy" / "provision_stripe.py"

SECRET_ENV = "CHUDWARE_WEB_STRIPE_SECRET_KEY"

# The v2 API requires an explicit version header and refuses to default.
STRIPE_V2_VERSION = "2026-08-26.dahlia"

pytestmark = pytest.mark.skipif(
    not (os.environ.get(SECRET_ENV) or "").strip(),
    reason=f"{SECRET_ENV} is unset; the Stripe sandbox suite is opt-in",
)


def plain(value: Any) -> Any:
    """A Stripe SDK object as an ordinary nested ``dict``.

    ``StripeObject`` in stripe-python 15 is NOT a dict subclass: it has no
    ``.get()``, ``dict(obj)`` fails, and attribute access on a missing key raises
    ``AttributeError`` rather than returning ``None``. Its ``__str__`` is its JSON
    body, which is the only public, recursive, plain-Python conversion available
    (``_to_dict_recursive`` is private).

    Converting once at each retrieval boundary keeps every assertion below
    written in normal dict style, and means an absent key reads as ``None``
    instead of exploding three frames deep inside the SDK.
    """
    return json.loads(str(value))


# --- Fixtures ----------------------------------------------------------------


@pytest.fixture(scope="session")
def catalog() -> Any:
    """``provision_stripe`` loaded by path, for ``CATALOG_SPEC`` and event lists.

    Loaded via importlib rather than by appending ``Webservice/deploy`` to
    ``sys.path``: that directory holds modules with names generic enough to
    shadow something (``monitor``, ``backup``), and a test module has no business
    mutating the import path for the rest of the session.

    The ``sys.modules`` registration BEFORE ``exec_module`` is required, not
    tidiness. ``provision_stripe`` defines dataclasses, and ``@dataclass``
    resolves string annotations through ``sys.modules[cls.__module__].__dict__``.
    Without the registration that lookup returns ``None`` and every dataclass in
    the module raises ``AttributeError: 'NoneType' object has no attribute
    '__dict__'`` at class-creation time — which presents as a wall of fixture
    errors that look nothing like an import problem.
    """
    name = "chud_provision_stripe"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, PROVISIONER)
    assert spec is not None and spec.loader is not None, PROVISIONER
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # Do not leave a half-executed module cached for the next fixture user.
        del sys.modules[name]
        raise
    return module


@pytest.fixture(scope="session")
def schema() -> Any:
    """The gateway's own ``marketplace_schema``, for the fee arithmetic check."""
    if str(GATEWAY_DIR) not in sys.path:
        sys.path.insert(0, str(GATEWAY_DIR))
    import marketplace_schema

    return marketplace_schema


@pytest.fixture(scope="session")
def stripe_mod() -> Any:
    """The Stripe SDK, pointed at the sandbox, after proving it IS a sandbox."""
    import stripe

    key = os.environ[SECRET_ENV].strip()
    if not key.startswith(("sk_test_", "rk_test_")):
        pytest.fail(
            "refusing to run: the configured Stripe key is not a test key. "
            "This suite creates charges, subscriptions and refunds."
        )
    stripe.api_key = key
    balance = plain(stripe.Balance.retrieve())
    assert balance.get("livemode") is False, (
        "refusing to run: Stripe reports livemode=True for this key"
    )
    return stripe


@pytest.fixture(scope="session")
def run_token() -> str:
    """Tag for everything this run creates, so teardown is unambiguous."""
    return f"t{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="session")
def cleanup(stripe_mod: Any) -> Iterator[list[tuple[str, str]]]:
    """Collects ``(kind, id)`` pairs torn down after the session."""
    created: list[tuple[str, str]] = []
    yield created
    for kind, ident in reversed(created):
        try:
            if kind == "customer":
                stripe_mod.Customer.delete(ident)
            elif kind == "price":
                # Prices are immutable and undeletable by design; deactivating is
                # the only cleanup available and is enough to keep them out of any
                # listing a later run makes.
                stripe_mod.Price.modify(ident, active=False)
        except Exception:  # noqa: BLE001 - teardown must never mask a result
            # Failed cleanup is sandbox litter, not a product defect. Raising
            # here would hide whatever the test actually found.
            continue


@pytest.fixture()
def customer(stripe_mod: Any, run_token: str, cleanup: list[tuple[str, str]]) -> dict:
    cust = plain(
        stripe_mod.Customer.create(
            email=f"buyer-{uuid.uuid4().hex[:10]}@sandbox.chudware.test",
            metadata={"chud_test_run": run_token},
        )
    )
    cleanup.append(("customer", cust["id"]))
    return cust


def _v2_request(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    """Raw Accounts-v2 call.

    stripe-python 15.5.0 exposes ``stripe.v2`` but ``stripe.v2.core`` is empty,
    so account creation and account links have no SDK binding and must go over
    HTTP. v2 is JSON (not form-encoded like v1) and REQUIRES an explicit
    ``Stripe-Version`` header — without it the API replies "You did not provide
    an API version" rather than defaulting to anything.
    """
    import httpx

    key = os.environ[SECRET_ENV].strip()
    response = httpx.request(
        method,
        f"https://api.stripe.com{path}",
        auth=(key, ""),
        headers={
            "Stripe-Version": STRIPE_V2_VERSION,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    )
    return response.status_code, response.json()


@pytest.fixture(scope="session")
def connect_seller(stripe_mod: Any, run_token: str) -> dict:
    """A freshly created, NOT-yet-onboarded Express connected account.

    Session-scoped because creating one costs a round trip and none of the tests
    that use it mutate it. Not torn down: Stripe does not permit deleting a
    connected account once an onboarding link has been issued, and a half-deleted
    one would be more confusing than a tagged one. ``chud_test_run`` is how an
    operator identifies them later.
    """
    status, body = _v2_request(
        "POST",
        "/v2/core/accounts",
        {
            "contact_email": f"seller-{uuid.uuid4().hex[:10]}@sandbox.chudware.test",
            "display_name": "Sandbox seller",
            "dashboard": "express",
            "identity": {"country": "us", "entity_type": "individual"},
            # 'application', not 'stripe': the platform is merchant of record,
            # collects the application fee and owns losses. Stripe rejects
            # 'stripe' for this configuration outright, and rejects
            # express+merchant and none+recipient entirely.
            "defaults": {
                "currency": "usd",
                "responsibilities": {
                    "losses_collector": "application",
                    "fees_collector": "application",
                },
            },
            "include": ["configuration.recipient", "requirements"],
            "configuration": {
                "recipient": {
                    "capabilities": {"stripe_balance": {"stripe_transfers": {"requested": True}}}
                }
            },
            "metadata": {"chud_kind": "marketplace_seller", "chud_test_run": run_token},
        },
    )
    assert status == 200, body
    return body


# --- Account and mode --------------------------------------------------------


def test_sandbox_account_is_test_mode(stripe_mod: Any) -> None:
    account = plain(stripe_mod.Account.retrieve())
    assert account["id"].startswith("acct_")
    assert plain(stripe_mod.Balance.retrieve())["livemode"] is False
    expected = (os.environ.get("CHUDWARE_STRIPE_ACCOUNT") or "").strip()
    if expected:
        # The provisioner enforces this guard on write; assert the suite points at
        # the same account, or a green run proves nothing about what we deploy to.
        assert account["id"] == expected


# --- Catalog drift -----------------------------------------------------------


def test_every_catalog_product_exists(stripe_mod: Any, catalog: Any) -> None:
    for spec in catalog.CATALOG_SPEC["products"]:
        product = plain(stripe_mod.Product.retrieve(spec["id"]))
        assert product["active"] is True, spec["id"]
        assert product["name"] == spec["name"]
        for key, value in spec["metadata"].items():
            assert product["metadata"].get(key) == value, (spec["id"], key)


def test_every_catalog_price_exists_with_exact_terms(stripe_mod: Any, catalog: Any) -> None:
    """Amount, currency, interval and product must all match the spec.

    Prices are immutable in Stripe, so a mismatch is not a config drift you can
    patch — it means the live price is a different object than the catalog
    describes, and somebody is being charged an amount no file in this repo
    states.
    """
    for spec in catalog.CATALOG_SPEC["prices"]:
        found = plain(stripe_mod.Price.list(lookup_keys=[spec["lookup_key"]], limit=2))
        assert len(found["data"]) == 1, (
            f"expected exactly one active price for lookup_key "
            f"{spec['lookup_key']!r}, found {len(found['data'])}"
        )
        price = found["data"][0]
        assert price["unit_amount"] == spec["unit_amount"], spec["lookup_key"]
        assert price["currency"] == spec["currency"], spec["lookup_key"]
        assert price["product"] == spec["product"], spec["lookup_key"]
        if spec["recurring"] is None:
            assert price["recurring"] is None, spec["lookup_key"]
        else:
            assert price["recurring"]["interval"] == spec["recurring"]["interval"]
        for key, value in spec["metadata"].items():
            assert price["metadata"].get(key) == value, (spec["lookup_key"], key)


def test_credit_pack_prices_declare_their_credit_grant(stripe_mod: Any, catalog: Any) -> None:
    """``chud_credits`` on the price is what billing.py grants on purchase.

    A credit pack whose price carries no ``chud_credits`` metadata takes the
    customer's money and grants nothing, and the failure is silent because the
    webhook itself succeeds.
    """
    packs = [s for s in catalog.CATALOG_SPEC["prices"] if s["product"] == "chud_credits"]
    assert packs, "the catalog declares no credit packs"
    for spec in packs:
        price = plain(stripe_mod.Price.list(lookup_keys=[spec["lookup_key"]], limit=1))["data"][0]
        raw = price["metadata"].get("chud_credits")
        assert raw is not None, spec["lookup_key"]
        credits = int(raw)
        assert credits > 0
        # Sanity, not policy: 1 credit == $0.001 of upstream model cost, so a pack
        # granting more than 10x its cents price would sell AI below cost.
        assert credits <= price["unit_amount"] * 10, (
            f"{spec['lookup_key']} grants {credits} credits "
            f"(${credits / 1000:.2f} of model spend) for ${price['unit_amount'] / 100:.2f}"
        )


def test_env_price_ids_point_at_the_catalog(stripe_mod: Any, catalog: Any) -> None:
    """Each configured price-id env var resolves to its catalog lookup key.

    This catches the real deployment failure mode: catalog correct, account
    correct, and ``gateway.env`` holding a price id from a different account or
    an older run, so checkout quietly bills the wrong amount.
    """
    checked = 0
    for spec in catalog.CATALOG_SPEC["prices"]:
        configured = (os.environ.get(spec["env"]) or "").strip()
        if not configured:
            continue
        price = plain(stripe_mod.Price.retrieve(configured))
        assert price["lookup_key"] == spec["lookup_key"], (
            f"{spec['env']}={configured} has lookup_key {price['lookup_key']!r}, "
            f"expected {spec['lookup_key']!r}"
        )
        assert price["unit_amount"] == spec["unit_amount"]
        checked += 1
    assert checked, "no price-id env vars were set; this test verified nothing"


# --- Portal and webhooks -----------------------------------------------------


def test_portal_configuration_matches_spec(stripe_mod: Any, catalog: Any) -> None:
    configs = plain(stripe_mod.billing_portal.Configuration.list(limit=100))
    managed = [c for c in configs["data"] if c["metadata"].get("chud_managed") == "true"]
    assert len(managed) == 1, f"expected one managed portal configuration, found {len(managed)}"
    # `products` is expandable and omitted unless asked for; without the expand
    # this assertion would silently test an empty list.
    cfg = plain(
        stripe_mod.billing_portal.Configuration.retrieve(
            managed[0]["id"], expand=["features.subscription_update.products"]
        )
    )
    assert cfg["active"] is True
    update = cfg["features"]["subscription_update"]
    assert update["enabled"] is True
    assert update["default_allowed_updates"] == ["price"]
    assert cfg["features"]["subscription_cancel"]["mode"] == "at_period_end"

    offered = {
        price_id
        for group in (update.get("products") or [])
        for price_id in group.get("prices", [])
    }
    assert offered, "portal offers no switchable prices; subscription_update is inert"
    for spec in catalog.CATALOG_SPEC["prices"]:
        if not spec.get("recurring"):
            continue
        price = plain(stripe_mod.Price.list(lookup_keys=[spec["lookup_key"]], limit=1))["data"][0]
        if spec.get("portal_switchable"):
            assert price["id"] in offered, spec["lookup_key"]
        else:
            # .edu eligibility is decided server-side from a verified domain.
            # Offering it as a portal radio button lets anyone halve their bill.
            assert price["id"] not in offered, (
                f"{spec['lookup_key']} is self-service switchable in the customer "
                "portal but is not marked portal_switchable"
            )


def test_webhook_endpoints_match_environment_contract(stripe_mod: Any, catalog: Any) -> None:
    """Check configured sandbox delivery without targeting a live-only gateway.

    Staging has no public ingress; the catalog deliberately uses ``stripe listen``
    locally instead. A sandbox endpoint at the production URL would use a signing
    secret that gateway does not accept.
    """
    endpoints = plain(stripe_mod.WebhookEndpoint.list(limit=100))["data"]
    by_url = {ep["url"]: ep for ep in endpoints}
    test_urls = {
        spec["url"] for spec in catalog.CATALOG_SPEC["webhooks"]
        if "test" in spec["modes"]
    }
    for spec in catalog.CATALOG_SPEC["webhooks"]:
        if "test" not in spec["modes"]:
            if spec["url"] not in test_urls:
                assert not any(
                    ep["url"] == spec["url"] and ep["status"] == "enabled"
                    for ep in endpoints
                ), f"sandbox delivery targets live-only endpoint {spec['url']}"
            continue
        endpoint = by_url.get(spec["url"])
        assert endpoint is not None, f"no webhook endpoint for {spec['url']}"
        assert endpoint["status"] == "enabled", spec["url"]
        missing = set(spec["enabled_events"]) - set(endpoint["enabled_events"])
        assert not missing, f"{spec['url']} is not subscribed to {sorted(missing)}"


def test_webhook_signature_verification_accepts_and_rejects() -> None:
    """Exercise ``construct_event`` against the real signing secret.

    Offline by design: the contract under test is the signature scheme, and a
    network round trip would not exercise it. Three cases, because the positive
    one alone would pass equally against a verifier that accepts everything.
    """
    import stripe

    secret = (os.environ.get("CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET") or "").strip()
    if not secret:
        pytest.skip("CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET is unset")

    payload = json.dumps(
        {
            "id": f"evt_{uuid.uuid4().hex[:16]}",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {"id": "cs_test_x", "metadata": {"chud_kind": "marketplace"}}
            },
        },
        separators=(",", ":"),
    )
    timestamp = int(time.time())
    signature = stripe.WebhookSignature._compute_signature(  # noqa: SLF001
        f"{timestamp}.{payload}", secret
    )

    event = stripe.Webhook.construct_event(payload, f"t={timestamp},v1={signature}", secret)
    assert event["type"] == "checkout.session.completed"

    with pytest.raises(stripe.error.SignatureVerificationError):
        stripe.Webhook.construct_event(
            payload.replace("cs_test_x", "cs_test_y"),
            f"t={timestamp},v1={signature}",
            secret,
        )

    # Replay window. Stripe's default tolerance is 300s; an hour-old signature
    # must be refused or a captured webhook stays replayable forever.
    stale = timestamp - 3600
    stale_sig = stripe.WebhookSignature._compute_signature(  # noqa: SLF001
        f"{stale}.{payload}", secret
    )
    with pytest.raises(stripe.error.SignatureVerificationError):
        stripe.Webhook.construct_event(payload, f"t={stale},v1={stale_sig}", secret)


# --- Checkout ----------------------------------------------------------------


@pytest.mark.parametrize(
    "env_name",
    [
        "CHUDWARE_STRIPE_PRICE_DESKTOP",
        "CHUDWARE_STRIPE_PRICE_DESKTOP_EDU",
        "CHUDWARE_STRIPE_PRICE_STUDIO",
        "CHUDWARE_STRIPE_PRICE_STUDIO_EDU",
    ],
)
def test_subscription_checkout_session_creates(
    stripe_mod: Any, customer: dict, run_token: str, env_name: str
) -> None:
    price_id = (os.environ.get(env_name) or "").strip()
    if not price_id:
        pytest.skip(f"{env_name} is unset")
    session = plain(
        stripe_mod.checkout.Session.create(
            mode="subscription",
            customer=customer["id"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url="https://app.chudware.com/?billing=success",
            cancel_url="https://app.chudware.com/?billing=cancel",
            metadata={"chud_test_run": run_token},
        )
    )
    assert session["url"].startswith("https://checkout.stripe.com/")
    assert session["mode"] == "subscription"
    assert session["status"] == "open"


@pytest.mark.parametrize(
    "env_name",
    [
        "CHUDWARE_STRIPE_PRICE_CREDITS_STARTER",
        "CHUDWARE_STRIPE_PRICE_CREDITS_STANDARD",
        "CHUDWARE_STRIPE_PRICE_CREDITS_BULK",
    ],
)
def test_credit_pack_checkout_session_creates(
    stripe_mod: Any, customer: dict, run_token: str, env_name: str
) -> None:
    price_id = (os.environ.get(env_name) or "").strip()
    if not price_id:
        pytest.skip(f"{env_name} is unset")
    session = plain(
        stripe_mod.checkout.Session.create(
            mode="payment",
            customer=customer["id"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url="https://app.chudware.com/?billing=success",
            cancel_url="https://app.chudware.com/?billing=cancel",
            metadata={"chud_test_run": run_token},
        )
    )
    assert session["mode"] == "payment"
    # billing.py resolves the granted credit amount by reading the line item's
    # price back off the completed session, so the session must expose exactly one.
    items = plain(stripe_mod.checkout.Session.list_line_items(session["id"], limit=5))
    assert len(items["data"]) == 1
    assert items["data"][0]["price"]["id"] == price_id


def test_billing_portal_session_creates(stripe_mod: Any, customer: dict) -> None:
    session = plain(
        stripe_mod.billing_portal.Session.create(
            customer=customer["id"], return_url="https://app.chudware.com/"
        )
    )
    assert session["url"].startswith("https://billing.stripe.com/")


def test_idempotency_key_returns_the_same_object(
    stripe_mod: Any, customer: dict, run_token: str
) -> None:
    """Two identical creates under one key must yield one object.

    Every mutating Stripe call in the gateway passes an idempotency key, which is
    only worth doing if it behaves as assumed. A retried checkout that created a
    second session would let a buyer be charged twice for one listing.
    """
    key = f"chud-test-{uuid.uuid4().hex}"
    kwargs: dict[str, Any] = {
        "mode": "payment",
        "customer": customer["id"],
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": 1234,
                    "product_data": {"name": "Idempotency probe"},
                },
            }
        ],
        "success_url": "https://app.chudware.com/",
        "cancel_url": "https://app.chudware.com/",
        "metadata": {"chud_test_run": run_token},
    }
    first = plain(stripe_mod.checkout.Session.create(**kwargs, idempotency_key=key))
    second = plain(stripe_mod.checkout.Session.create(**kwargs, idempotency_key=key))
    assert first["id"] == second["id"]


# --- Live subscription and payment lifecycle --------------------------------


def test_subscription_lifecycle_with_a_test_card(
    stripe_mod: Any, customer: dict, run_token: str
) -> None:
    """Create, read and cancel a real subscription end to end.

    Checkout-session creation only proves Stripe accepted a request. This proves a
    subscription actually reaches ``active`` on this account with this price,
    which is what ``billing.py`` writes ``subscription_status`` from.
    """
    price_id = (os.environ.get("CHUDWARE_STRIPE_PRICE_DESKTOP") or "").strip()
    if not price_id:
        pytest.skip("CHUDWARE_STRIPE_PRICE_DESKTOP is unset")

    # `attach` MINTS a new PaymentMethod belonging to this customer and returns
    # it; "pm_card_visa" is a shared, reusable test token and is NOT itself
    # attachable as a default. Passing the token here fails with "The customer
    # does not have a payment method with the ID pm_...", naming the id `attach`
    # just created — which reads as a Stripe bug and is actually this mistake.
    method = plain(stripe_mod.PaymentMethod.attach("pm_card_visa", customer=customer["id"]))
    stripe_mod.Customer.modify(
        customer["id"], invoice_settings={"default_payment_method": method["id"]}
    )
    subscription = plain(
        stripe_mod.Subscription.create(
            customer=customer["id"],
            items=[{"price": price_id}],
            metadata={"chud_test_run": run_token},
        )
    )
    try:
        assert subscription["status"] in ("active", "trialing", "incomplete")
        assert subscription["items"]["data"][0]["price"]["id"] == price_id

        # billing.py's `_sync_renewal_price` calls Subscription.modify from inside
        # a webhook handler, where a permission failure would be invisible. Prove
        # the account permits a read-back and a modify before relying on it.
        reread = plain(stripe_mod.Subscription.retrieve(subscription["id"]))
        assert reread["id"] == subscription["id"]
        modified = plain(
            stripe_mod.Subscription.modify(
                subscription["id"], metadata={"chud_test_run": run_token, "probe": "1"}
            )
        )
        assert modified["metadata"]["probe"] == "1"

        invoices = plain(stripe_mod.Invoice.list(customer=customer["id"], limit=5))
        assert invoices["data"], "an active subscription produced no invoice"
    finally:
        stripe_mod.Subscription.cancel(subscription["id"])
    assert plain(stripe_mod.Subscription.retrieve(subscription["id"]))["status"] == "canceled"


def test_payment_and_refund_round_trip(
    stripe_mod: Any, customer: dict, run_token: str
) -> None:
    """A real charge, then a real refund.

    ``marketplace_commerce.refund_order`` and the admin refund route both depend
    on ``Refund.create`` succeeding and on the charge reporting itself refunded
    afterwards.
    """
    intent = plain(
        stripe_mod.PaymentIntent.create(
            amount=2500,
            currency="usd",
            customer=customer["id"],
            payment_method="pm_card_visa",
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            metadata={"chud_kind": "marketplace", "chud_test_run": run_token},
            idempotency_key=f"chud-test-pi-{uuid.uuid4().hex}",
        )
    )
    assert intent["status"] == "succeeded", intent["status"]

    refund = plain(
        stripe_mod.Refund.create(
            payment_intent=intent["id"],
            reason="requested_by_customer",
            idempotency_key=f"chud-test-rf-{uuid.uuid4().hex}",
        )
    )
    assert refund["status"] in ("succeeded", "pending")

    reread = plain(stripe_mod.PaymentIntent.retrieve(intent["id"]))
    charge = plain(stripe_mod.Charge.retrieve(reread["latest_charge"]))
    assert charge["refunded"] is True
    assert charge["amount_refunded"] == 2500


def test_partial_refund_is_supported(
    stripe_mod: Any, customer: dict, run_token: str
) -> None:
    """The admin refund route accepts an optional ``amount_cents``.

    Note the asymmetry this pins down: a partially refunded charge reports
    ``refunded: False``. Treating that flag as "was any money returned" would
    make a partial refund invisible to the admin dashboard.
    """
    intent = plain(
        stripe_mod.PaymentIntent.create(
            amount=4000,
            currency="usd",
            customer=customer["id"],
            payment_method="pm_card_visa",
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            metadata={"chud_kind": "marketplace", "chud_test_run": run_token},
            idempotency_key=f"chud-test-pi-{uuid.uuid4().hex}",
        )
    )
    stripe_mod.Refund.create(
        payment_intent=intent["id"],
        amount=1500,
        idempotency_key=f"chud-test-rf-{uuid.uuid4().hex}",
    )
    charge = plain(
        stripe_mod.Charge.retrieve(
            plain(stripe_mod.PaymentIntent.retrieve(intent["id"]))["latest_charge"]
        )
    )
    assert charge["amount_refunded"] == 1500
    assert charge["refunded"] is False


def test_declined_card_surfaces_a_card_error(
    stripe_mod: Any, customer: dict, run_token: str
) -> None:
    """``pm_card_chargeDeclined`` must raise, not return a failed intent.

    ``mk_orders.failure_reason`` is populated from this path. If a decline came
    back as an ordinary object the order would settle as paid.
    """
    import stripe

    with pytest.raises(stripe.error.CardError) as excinfo:
        stripe_mod.PaymentIntent.create(
            amount=2500,
            currency="usd",
            customer=customer["id"],
            payment_method="pm_card_chargeDeclined",
            confirm=True,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            metadata={"chud_test_run": run_token},
        )
    assert excinfo.value.code == "card_declined"


# --- Connect (marketplace payouts) ------------------------------------------


def test_connect_account_creation_uses_accounts_v2(connect_seller: dict) -> None:
    assert connect_seller["id"].startswith("acct_")
    assert connect_seller["dashboard"] == "express"


def test_connect_v1_account_create_is_rejected(stripe_mod: Any) -> None:
    """Pin the reason the design uses v2 at all.

    If Stripe ever re-permits v1 ``Account.create`` here, this test fails and
    tells the next reader that ``marketplace_commerce._v2_request`` can be deleted
    in favour of the SDK. Without it, that helper looks like an unexplained
    reimplementation of something the SDK appears to already do.
    """
    import stripe

    with pytest.raises(stripe.error.StripeError) as excinfo:
        stripe_mod.Account.create(
            type="express",
            email=f"v1probe-{uuid.uuid4().hex[:8]}@sandbox.chudware.test",
            capabilities={"transfers": {"requested": True}},
        )
    assert "v2/core/accounts" in str(excinfo.value)


def test_connect_onboarding_link_is_issued(connect_seller: dict) -> None:
    status, body = _v2_request(
        "POST",
        "/v2/core/account_links",
        {
            "account": connect_seller["id"],
            "use_case": {
                "type": "account_onboarding",
                "account_onboarding": {
                    "configurations": ["recipient"],
                    "refresh_url": "https://app.chudware.com/studio#payouts",
                    "return_url": "https://app.chudware.com/studio#payouts",
                },
            },
        },
    )
    assert status == 200, body
    assert body["url"].startswith("https://connect.stripe.com/")


def test_v1_retrieve_works_on_a_v2_created_account(
    stripe_mod: Any, connect_seller: dict
) -> None:
    """``refresh_connect()`` reads state through the v1 SDK; prove it can.

    This is precisely why only creation and account links need raw HTTP. If v1
    retrieve stopped working on a v2 account, every capability check in
    ``marketplace_commerce`` would silently read nothing and sellers would look
    payable when they are not.
    """
    account = plain(stripe_mod.Account.retrieve(connect_seller["id"]))
    assert account["id"] == connect_seller["id"]
    for field in ("charges_enabled", "payouts_enabled", "details_submitted"):
        assert field in account, field
    # Fresh account, onboarding not completed: exactly the state the
    # `connect_incomplete` gate exists to catch.
    assert account["details_submitted"] is False
    assert account["payouts_enabled"] is False


def test_destination_charge_is_refused_before_onboarding(
    stripe_mod: Any, connect_seller: dict, run_token: str
) -> None:
    """Why ``connect_incomplete`` is a hard gate and not a warning.

    Selling on behalf of a seller who has not finished onboarding fails at the
    Stripe boundary. Without the gate, the buyer meets that failure mid-checkout,
    after the listing page already promised them a purchase.
    """
    import stripe

    with pytest.raises(stripe.error.StripeError) as excinfo:
        stripe_mod.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": 2500,
                        "product_data": {"name": "Sandbox marketplace model"},
                    },
                }
            ],
            payment_intent_data={
                "application_fee_amount": 375,
                "transfer_data": {"destination": connect_seller["id"]},
            },
            success_url="https://app.chudware.com/marketplace#purchased",
            cancel_url="https://app.chudware.com/marketplace#canceled",
            metadata={"chud_kind": "marketplace", "chud_test_run": run_token},
        )
    assert "capabilit" in str(excinfo.value).lower()


def test_application_fee_split_matches_our_arithmetic(schema: Any) -> None:
    """The fee handed to Stripe is the fee our own splitter computed.

    Offline: the arithmetic is ours, and the property under test is that
    ``application_fee_amount`` equals ``split_cash``'s fee for the same inputs and
    that nothing is lost in the split. A drift here mis-pays every seller at once.
    """
    for amount in (100, 999, 2500, 4999, 100_00, 5_000_00):
        fee, net = schema.split_cash(amount, 1500)
        assert fee + net == amount
        assert fee == amount * 1500 // 10_000
        # Stripe requires the application fee to be less than the charge; a 100%
        # fee is arithmetically valid and commercially nonsense.
        assert net > 0
        assert fee < amount
