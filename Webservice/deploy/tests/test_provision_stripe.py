"""Hermetic Stripe catalog reconciliation tests; no request reaches Stripe."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

DEPLOY_DIR = Path(__file__).resolve().parents[1]
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import provision_stripe as ps  # noqa: E402


class FakeStripeTransport:
    """Stateful Stripe REST fake that records every read and write request."""

    def __init__(self, *, account_id: str = "acct_TEST", livemode: bool = False):
        self.account = {"id": account_id, "livemode": livemode}
        self.products: dict[str, dict[str, Any]] = {}
        self.prices: dict[str, dict[str, Any]] = {}
        self.portal_configs: dict[str, dict[str, Any]] = {}
        self.webhooks: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        self._counters = {"price": 0, "bpc": 0, "we": 0}

    @property
    def writes(self):
        return [request for request in self.requests if request[0] == "POST"]

    def livemode_hint(self) -> bool | None:
        return self.account["livemode"]

    def _id(self, kind: str) -> str:
        self._counters[kind] += 1
        return f"{kind}_{self._counters[kind]:06d}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        query = copy.deepcopy(dict(params or {}))
        body = copy.deepcopy(dict(data or {}))
        self.requests.append((method, path, query, body))

        if method == "GET" and path == "/v1/account":
            return copy.deepcopy(self.account)

        if path.startswith("/v1/products/"):
            product_id = path.rsplit("/", 1)[1]
            if method == "GET":
                if product_id not in self.products:
                    raise ps.StripeAPIError(method, path, 404, "No such product")
                return copy.deepcopy(self.products[product_id])
            product = self.products[product_id]
            self._merge(product, body)
            return copy.deepcopy(product)
        if method == "POST" and path == "/v1/products":
            product = copy.deepcopy(body)
            product.setdefault("active", True)
            self.products[product["id"]] = product
            return copy.deepcopy(product)

        if method == "GET" and path == "/v1/prices":
            lookup_keys = set(query.get("lookup_keys", []))
            active = query.get("active")
            rows = [
                copy.deepcopy(price)
                for price in self.prices.values()
                if (active is None or price.get("active", True) is active)
                and (not lookup_keys or price.get("lookup_key") in lookup_keys)
            ]
            return {"data": rows, "has_more": False}
        if method == "POST" and path == "/v1/prices":
            if body.get("transfer_lookup_key"):
                for prior in self.prices.values():
                    if prior.get("lookup_key") == body["lookup_key"]:
                        prior["lookup_key"] = None
            price = copy.deepcopy(body)
            price.pop("transfer_lookup_key", None)
            price["id"] = self._id("price")
            price.setdefault("active", True)
            price.setdefault("metadata", {})
            price.setdefault("recurring", None)
            self.prices[price["id"]] = price
            return copy.deepcopy(price)
        if method == "POST" and path.startswith("/v1/prices/"):
            price_id = path.rsplit("/", 1)[1]
            price = self.prices[price_id]
            self._merge(price, body)
            return copy.deepcopy(price)

        if method == "GET" and path == "/v1/billing_portal/configurations":
            return {"data": list(copy.deepcopy(self.portal_configs).values()), "has_more": False}
        if method == "GET" and path.startswith("/v1/billing_portal/configurations/"):
            configuration_id = path.rsplit("/", 1)[1]
            return copy.deepcopy(self.portal_configs[configuration_id])
        if method == "POST" and path == "/v1/billing_portal/configurations":
            configuration = copy.deepcopy(body)
            configuration["id"] = self._id("bpc")
            configuration.setdefault("is_default", not self.portal_configs)
            self.portal_configs[configuration["id"]] = configuration
            return copy.deepcopy(configuration)
        if method == "POST" and path.startswith("/v1/billing_portal/configurations/"):
            configuration_id = path.rsplit("/", 1)[1]
            configuration = self.portal_configs[configuration_id]
            self._merge(configuration, body)
            return copy.deepcopy(configuration)

        if method == "GET" and path == "/v1/webhook_endpoints":
            return {"data": list(copy.deepcopy(self.webhooks).values()), "has_more": False}
        if method == "POST" and path == "/v1/webhook_endpoints":
            webhook = copy.deepcopy(body)
            webhook["id"] = self._id("we")
            webhook["secret"] = f"whsec_{webhook['id']}"
            webhook["status"] = "enabled"
            self.webhooks[webhook["id"]] = webhook
            return copy.deepcopy(webhook)
        if method == "POST" and path.startswith("/v1/webhook_endpoints/"):
            webhook_id = path.rsplit("/", 1)[1]
            webhook = self.webhooks[webhook_id]
            self._merge(webhook, body)
            webhook["status"] = "enabled"
            return copy.deepcopy(webhook)

        raise AssertionError(f"unhandled fake request: {method} {path}")

    @staticmethod
    def _merge(target: dict[str, Any], update: Mapping[str, Any]) -> None:
        for key, value in update.items():
            if key == "metadata":
                target.setdefault("metadata", {}).update(copy.deepcopy(value))
            else:
                target[key] = copy.deepcopy(value)


def _post_paths(fake: FakeStripeTransport, path: str) -> list[tuple]:
    return [request for request in fake.writes if request[1] == path]


def test_clean_account_creates_exact_catalog_and_second_run_is_noop():
    fake = FakeStripeTransport()

    first = ps.provision(fake, expect_account="acct_TEST")

    assert len(_post_paths(fake, "/v1/products")) == 4
    assert len(_post_paths(fake, "/v1/prices")) == 7
    assert len(_post_paths(fake, "/v1/billing_portal/configurations")) == 1
    assert _post_paths(fake, "/v1/webhook_endpoints") == []
    assert len(fake.products) == 4
    assert len(fake.prices) == 7
    assert first.write_count == 12
    assert set(first.price_ids) == {
        price["lookup_key"] for price in ps.CATALOG_SPEC["prices"]
    }
    assert first.webhook_secrets == {}

    fake.requests.clear()
    second = ps.provision(fake, expect_account="acct_TEST")

    assert fake.writes == []
    assert second.write_count == 0
    assert all(operation.status == "NO-OP" for operation in second.operations)
    assert second.price_ids == first.price_ids


def test_changed_amount_replaces_price_transfers_lookup_key_then_deactivates_old():
    fake = FakeStripeTransport()
    initial = ps.provision(fake, expect_account="acct_TEST")
    lookup_key = "chud_desktop_monthly"
    old_id = initial.price_ids[lookup_key]
    changed = copy.deepcopy(ps.CATALOG_SPEC)
    target = next(price for price in changed["prices"] if price["lookup_key"] == lookup_key)
    target["unit_amount"] = 3500

    fake.requests.clear()
    result = ps.provision(fake, expect_account="acct_TEST", spec=changed)

    price_writes = [
        request
        for request in fake.writes
        if request[1] == "/v1/prices"
        or request[1].startswith("/v1/prices/")
    ]
    assert len(price_writes) == 2
    create = price_writes[0]
    deactivate = price_writes[1]
    assert create[1] == "/v1/prices"
    assert create[3]["unit_amount"] == 3500
    assert create[3]["lookup_key"] == lookup_key
    assert create[3]["transfer_lookup_key"] is True
    assert deactivate == ("POST", f"/v1/prices/{old_id}", {}, {"active": False})
    new_id = result.price_ids[lookup_key]
    assert new_id != old_id
    assert fake.prices[new_id]["lookup_key"] == lookup_key
    assert fake.prices[old_id]["lookup_key"] is None
    assert fake.prices[old_id]["active"] is False
    assert "unit_amount 3000 -> 3500" in next(
        operation.detail for operation in result.operations if operation.status == "REPLACE"
    )
    assert all("unit_amount" not in request[3] for request in price_writes[1:])
    # The managed portal must also move to the replacement price id.
    assert len(
        [
            request
            for request in fake.writes
            if request[1].startswith("/v1/billing_portal/configurations/")
        ]
    ) == 1


def test_wrong_account_aborts_after_identity_read_and_before_any_write():
    fake = FakeStripeTransport(account_id="acct_WRONG")

    with pytest.raises(ps.ProvisioningError, match="wrong Stripe account"):
        ps.provision(fake, expect_account="acct_EXPECTED")

    assert fake.requests == [("GET", "/v1/account", {}, {})]
    assert fake.writes == []


def test_live_account_requires_explicit_live_flag_before_any_write():
    fake = FakeStripeTransport(livemode=True)

    with pytest.raises(ps.ProvisioningError, match="without explicit --live"):
        ps.provision(fake, expect_account="acct_TEST")

    assert fake.requests == [("GET", "/v1/account", {}, {})]
    assert fake.writes == []


def test_live_mode_creates_production_webhook_and_returns_its_secret():
    fake = FakeStripeTransport(livemode=True)

    result = ps.provision(fake, expect_account="acct_TEST", allow_live=True)

    assert set(result.webhook_ids) == {"production"}
    assert len(_post_paths(fake, "/v1/webhook_endpoints")) == 1
    assert result.webhook_secrets.keys() == {"CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET"}


def test_dry_run_is_offline_and_emits_all_price_env_assignments():
    result = ps.dry_run_result(expect_account="acct_TEST", live=False)

    assert len([op for op in result.operations if op.resource == "product"]) == 4
    assert len([op for op in result.operations if op.resource == "price"]) == 7
    fragment = ps.env_fragment(result)
    assert fragment.count("CHUDWARE_STRIPE_PRICE_") == 7
    assert "CHUDWARE_STRIPE_PRICE_DESKTOP=<price_for_chud_desktop_monthly>" in fragment


def test_checked_in_operator_record_is_generated_from_spec():
    record = DEPLOY_DIR / "STRIPE_CATALOG.md"
    assert record.read_text(encoding="utf-8") == ps.render_catalog_markdown()
