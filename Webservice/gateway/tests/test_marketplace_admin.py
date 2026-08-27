"""Operator surfaces: moderation gates, provenance, private evidence, and the
admin billing refund door.

Two classes of failure live here and neither is visible in a happy-path click
through the admin panel. The first is an operator being able to do something the
provenance rules forbid -- publishing a human-made claim nobody verified, or
verifying a claim after the evidence behind it was swapped out. The second is a
private file becoming reachable: attestation evidence is the only asset class in
the marketplace that is reviewer-only, and the route that serves it had no test
at all. Both are asserted against the recorded database state and the actual
bytes served, not against status codes alone.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

import marketplace_commerce
from db import tx

STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
EVIDENCE_PNG = b"\x89PNG\r\n\x1a\n evidence photograph bytes"

# A process note long enough to clear mk_attestations_process_note_substantive
# (120 characters), which the API restates so a short note is a 400 rather than a
# constraint violation.
PROCESS_NOTE = (
    "Measured the original casting with calipers and a height gauge, then modelled it "
    "from those measurements by hand, rebuilding every fillet against the part and "
    "checking the printed result twice. No generative tool produced any geometry."
)

# limits.api_writes is module-level state keyed by email that outlives the
# per-test schema reset, so each test gets its own accounts rather than sharing
# one budget across the file.
_SEQ = itertools.count(1)

# Every admin GET the operator console loads. Parametrised as one list because
# the gate is the contract: a new admin read route that forgets require_admin is
# exactly the mistake this catches, and it is invisible while the panel works.
ADMIN_GETS = (
    "/api/admin/marketplace/overview",
    "/api/admin/marketplace/listings",
    "/api/admin/marketplace/attestations",
    "/api/admin/marketplace/reports",
    "/api/admin/marketplace/sellers",
    "/api/admin/marketplace/payouts",
    "/api/admin/marketplace/revenue?days=30",
    "/api/admin/marketplace/imports",
    "/api/admin/billing/overview",
    "/api/admin/billing/timeseries?days=30",
    "/api/admin/billing/subscriptions",
    "/api/admin/billing/invoices",
    "/api/admin/billing/credits?days=30",
    "/api/admin/billing/webhooks",
    "/api/admin/billing/health",
    "/api/admin/billing/connect",
)


def code_of(response: Any) -> str | None:
    """The machine-readable code, tolerating the plain-string detail shape."""
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail.get("code") if isinstance(detail, dict) else None


class Studio:
    """A seller with a studio, a buyer, a bystander and an operator."""

    def __init__(self, client, as_user, activate, grant_credits) -> None:
        self.client = client
        self.h = as_user
        self.grant = grant_credits
        tag = next(_SEQ)
        self.seller = f"seller{tag}@admin.test"
        self.buyer = f"buyer{tag}@admin.test"
        self.other = f"other{tag}@admin.test"
        self.admin = f"admin{tag}@admin.test"
        for email in (self.seller, self.buyer, self.other):
            activate(email)
        activate(self.admin, role="admin")
        response = client.post(
            "/api/studio/seller",
            headers=self.h(self.seller),
            json={"handle": f"jigworks-{tag}", "display_name": "Jigworks"},
        )
        assert response.status_code == 200, response.text

    def listing(self, **over: Any) -> str:
        payload: dict[str, Any] = {
            "title": "Hand Modelled Jig",
            "summary": "A jig.",
            "description": "Solid.",
            "category_id": "tooling",
            "license_id": "CC-BY-4.0",
            "pricing_kind": "free",
            "origin": "human",
        }
        payload.update(over)
        response = self.client.post(
            "/api/studio/listings", headers=self.h(self.seller), json=payload
        )
        assert response.status_code == 200, response.text
        return response.json()["listing"]["id"]

    def upload(
        self,
        listing_id: str,
        name: str,
        data: bytes,
        *,
        kind: str = "source",
        role: str = "",
    ):
        return self.client.post(
            f"/api/studio/listings/{listing_id}/assets",
            headers=self.h(self.seller),
            files={"file": (name, data, "application/octet-stream")},
            data={"kind": kind, "role": role, "gated": "true"},
        )

    def attest(self, listing_id: str) -> str:
        response = self.client.post(
            f"/api/studio/listings/{listing_id}/attestation",
            headers=self.h(self.seller),
            json={
                "process_note": PROCESS_NOTE,
                "tools": ["calipers", "height gauge"],
                "hours": 6.5,
            },
        )
        assert response.status_code == 200, response.text
        return str(response.json()["attestation"]["id"])

    def submit(self, listing_id: str):
        return self.client.post(
            f"/api/studio/listings/{listing_id}/submit", headers=self.h(self.seller)
        )

    def moderate(self, listing_id: str, action: str, note: str = "operator note"):
        return self.client.post(
            f"/api/admin/marketplace/listings/{listing_id}/moderate",
            headers=self.h(self.admin),
            json={"action": action, "note": note},
        )

    def decide(self, attestation_id: str, decision: str, note: str = "reviewed"):
        return self.client.post(
            f"/api/admin/marketplace/attestations/{attestation_id}/decide",
            headers=self.h(self.admin),
            json={"decision": decision, "note": note},
        )

    def flags(self, **body: Any):
        return self.client.post(
            f"/api/admin/marketplace/sellers/{self.seller}/flags",
            headers=self.h(self.admin),
            json=body,
        )

    def enable_connect(self, fake) -> None:
        """Cache a live Connect account and make the fake Stripe agree.

        refresh_connect passes the account id POSITIONALLY, which the conftest
        fake's keyword-only recorder cannot accept, so the stub is written out
        here rather than reusing fake.Account.retrieve.
        """
        with tx() as cur:
            cur.execute(
                "UPDATE mk_sellers SET connect_account_id = 'acct_test_seller',"
                " connect_charges_enabled = TRUE, connect_payouts_enabled = TRUE,"
                " connect_details_submitted = TRUE, connect_refreshed_at = now()"
                " WHERE email = %s",
                (self.seller,),
            )

        def retrieve(account_id: str, **kwargs: Any) -> dict[str, Any]:
            fake.calls.append(("Account.retrieve", {"account": account_id, **kwargs}))
            return {
                "id": account_id,
                "charges_enabled": True,
                "payouts_enabled": True,
                "details_submitted": True,
                "capabilities": {"transfers": "active"},
            }

        fake.Account.retrieve = retrieve


@pytest.fixture()
def studio(client, as_user, activate, grant_credits) -> Studio:
    return Studio(client, as_user, activate, grant_credits)


def _listing_row(listing_id: str) -> dict[str, Any]:
    with tx() as cur:
        cur.execute(
            "SELECT status, published_at, attestation_status FROM mk_listings"
            " WHERE id = %s",
            (listing_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return dict(row)


def _published_human_listing(studio: Studio) -> tuple[str, str]:
    """A free human-made listing, verified and live. Returns (listing, attestation).

    Free on purpose: a paid listing is ALSO held for first-paid commercial
    review, and mixing the two holds makes it impossible to tell which one a
    later assertion is observing.
    """
    listing_id = studio.listing()
    attestation_id = studio.attest(listing_id)
    response = studio.submit(listing_id)
    assert response.status_code == 200, response.text
    assert _listing_row(listing_id)["status"] == "in_review"
    decided = studio.decide(attestation_id, "verified")
    assert decided.status_code == 200, decided.text
    assert decided.json()["listing_published"] is True
    assert _listing_row(listing_id)["status"] == "published"
    return listing_id, attestation_id


# --- 1. every admin route is admin-only --------------------------------------


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_read_routes_are_operator_only(studio, fake_stripe, path):
    allowed = studio.client.get(path, headers=studio.h(studio.admin))
    assert allowed.status_code == 200, f"{path} -> {allowed.status_code} {allowed.text}"

    # An active, plan-holding standard user, i.e. the account most likely to try:
    # a caller who is authenticated for everything else on the site.
    refused = studio.client.get(path, headers=studio.h(studio.other))
    assert refused.status_code == 403, f"{path} -> {refused.status_code}"

    anonymous = studio.client.get(path)
    assert anonymous.status_code == 401, f"{path} -> {anonymous.status_code}"


# --- 2. provenance cannot be bypassed by an operator -------------------------


def test_operator_cannot_publish_an_unverified_human_claim(studio):
    """Approving is a commercial decision; it must not double as a provenance one.

    'Human-made' is a promise to the buyer that no operator convenience may
    launder: the claim has to be reviewed on its own evidence first.
    """
    listing_id = studio.listing()
    attestation_id = studio.attest(listing_id)
    assert _listing_row(listing_id)["attestation_status"] == "pending"

    refused = studio.moderate(listing_id, "approve")
    assert refused.status_code == 409, refused.text
    assert code_of(refused) == "attestation_not_verified"
    assert _listing_row(listing_id)["status"] == "draft"

    assert studio.decide(attestation_id, "verified").status_code == 200
    approved = studio.moderate(listing_id, "approve")
    assert approved.status_code == 200, approved.text
    row = _listing_row(listing_id)
    assert row["status"] == "published"
    assert row["published_at"] is not None
    assert row["attestation_status"] == "verified"


def test_restore_is_gated_on_provenance_too(studio):
    """'restore' publishes by the same statement as 'approve'.

    Gating only 'approve' would leave an equally direct route to a published
    listing whose human-made claim was never verified.
    """
    listing_id = studio.listing()
    studio.attest(listing_id)

    refused = studio.moderate(listing_id, "restore")
    assert refused.status_code == 409, refused.text
    assert code_of(refused) == "attestation_not_verified"
    assert _listing_row(listing_id)["status"] == "draft"


# --- 3. verifying an attestation releases the listing ------------------------


def test_verifying_an_attestation_publishes_the_listing_it_was_holding(studio):
    """A human-made listing sits in in_review BECAUSE the claim was unverified.

    Relabelling the attestation without releasing the listing leaves it in the
    queue with nothing left to review, so a verified human-made model never
    reaches the shelf its verification exists for.
    """
    listing_id, _ = _published_human_listing(studio)
    row = _listing_row(listing_id)
    assert row["status"] == "published"
    assert row["published_at"] is not None
    assert row["attestation_status"] == "verified"


def test_verifying_does_not_release_a_listing_awaiting_first_paid_review(studio):
    """Two independent holds; a provenance decision satisfies only one of them.

    Auto-publishing here would put a seller's first paid listing on sale with no
    commercial review at all.
    """
    listing_id = studio.listing(
        title="Hand Modelled Gearbox",
        pricing_kind="credits",
        price_credits=5000,
        license_id="chud-standard-1.0",
        category_id="gears",
    )
    attestation_id = studio.attest(listing_id)
    assert studio.submit(listing_id).status_code == 200
    assert _listing_row(listing_id)["status"] == "in_review"

    decided = studio.decide(attestation_id, "verified")
    assert decided.status_code == 200, decided.text
    assert decided.json()["listing_published"] is False
    row = _listing_row(listing_id)
    assert row["status"] == "in_review"
    assert row["published_at"] is None
    assert row["attestation_status"] == "verified"


# --- 4. revoking verification withdraws the listing --------------------------


def test_revoking_verification_withdraws_a_published_listing(studio):
    """status and published_at have to move together.

    mk_listings_published_at_matches_status is a CHECK, so a withdrawal that
    updated only one of the pair would surface as a 500 rather than a refusal.
    """
    listing_id, attestation_id = _published_human_listing(studio)

    revoked = studio.decide(attestation_id, "revoked", note="evidence did not hold up")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["listing_moved_to_review"] is True
    row = _listing_row(listing_id)
    assert row["status"] != "published"
    assert row["published_at"] is None
    assert row["attestation_status"] == "revoked"
    with tx() as cur:
        cur.execute(
            "SELECT status FROM mk_attestations WHERE id = %s", (attestation_id,)
        )
        assert cur.fetchone()["status"] == "revoked"


# --- 5. evidence is reviewer-only and attestation-scoped ---------------------


def _evidence_listing(studio: Studio, *, name: str = "evidence.png") -> tuple[str, str, str]:
    """A human listing carrying one evidence asset claimed by one attestation."""
    listing_id = studio.listing()
    uploaded = studio.upload(listing_id, name, EVIDENCE_PNG, kind="evidence")
    assert uploaded.status_code == 200, uploaded.text
    asset = uploaded.json()["asset"]
    assert asset["gated"] is True, "evidence must be gated regardless of the request"
    attestation_id = studio.attest(listing_id)
    with tx() as cur:
        cur.execute(
            "SELECT attestation_id FROM mk_assets WHERE id = %s", (asset["id"],)
        )
        assert str(cur.fetchone()["attestation_id"]) == attestation_id
    return listing_id, attestation_id, asset["id"]


def test_evidence_is_reachable_only_through_the_admin_attestation_route(studio):
    """Evidence is the one asset class no buyer, and not even its own seller,
    may read: a reviewer looks at a person's workshop photographs and identity
    documents, and the storefront routes must have no path to them."""
    _, attestation_id, asset_id = _evidence_listing(studio)

    public = studio.client.get(f"/api/marketplace/assets/{asset_id}")
    assert public.status_code == 404

    seller_download = studio.client.get(
        f"/api/marketplace/download/{asset_id}", headers=studio.h(studio.seller)
    )
    assert seller_download.status_code == 404

    evidence_url = (
        f"/api/admin/marketplace/attestations/{attestation_id}/evidence/{asset_id}"
    )
    served = studio.client.get(evidence_url, headers=studio.h(studio.admin))
    assert served.status_code == 200, served.text
    assert served.content == EVIDENCE_PNG
    # Never inline, even for an image: these bytes are attacker-supplied and the
    # session they would run in is an operator's.
    assert served.headers["content-disposition"].startswith("attachment")
    assert served.headers["cache-control"] == "no-store"
    assert served.headers["x-content-type-options"] == "nosniff"

    standard = studio.client.get(evidence_url, headers=studio.h(studio.other))
    assert standard.status_code in (403, 404)
    assert studio.client.get(evidence_url).status_code == 401


def test_evidence_route_will_not_serve_another_listings_asset(studio):
    """The asset id is not a capability: it has to belong to THIS attestation.

    Without the join on attestation_id an operator handling one review could read
    the private evidence attached to any other.
    """
    _, first_attestation, _ = _evidence_listing(studio)
    _, _, other_asset = _evidence_listing(studio, name="other-evidence.png")

    crossed = studio.client.get(
        f"/api/admin/marketplace/attestations/{first_attestation}"
        f"/evidence/{other_asset}",
        headers=studio.h(studio.admin),
    )
    assert crossed.status_code == 404
    assert code_of(crossed) == "evidence_not_found"


# --- 6. evidence is frozen once claimed --------------------------------------


def test_evidence_cannot_be_swapped_under_a_live_attestation(studio):
    """Verification is a statement about the evidence that was reviewed.

    If evidence stayed mutable, a seller could file a claim, get it verified, and
    then replace the photographs it rested on while keeping the verified badge.
    """
    listing_id, _, asset_id = _evidence_listing(studio)

    deleted = studio.client.delete(
        f"/api/studio/listings/{listing_id}/assets/{asset_id}",
        headers=studio.h(studio.seller),
    )
    assert deleted.status_code == 409, deleted.text
    assert code_of(deleted) == "evidence_locked"

    replaced = studio.upload(
        listing_id, "swapped.png", b"different bytes", kind="evidence"
    )
    assert replaced.status_code == 409, replaced.text
    assert code_of(replaced) == "evidence_locked"

    patched = studio.client.patch(
        f"/api/studio/listings/{listing_id}/assets/{asset_id}",
        headers=studio.h(studio.seller),
        json={"role": "gallery"},
    )
    assert patched.status_code == 409, patched.text
    assert code_of(patched) == "evidence_locked"

    with tx() as cur:
        cur.execute(
            "SELECT filename, role FROM mk_assets WHERE listing_id = %s"
            " AND kind = 'evidence'",
            (listing_id,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    assert rows == [{"filename": "evidence.png", "role": ""}]


# --- 7. seller flags -------------------------------------------------------


def test_publishing_block_stops_the_sellers_next_write_and_lifts_cleanly(studio):
    """A block that only changes a column is decoration.

    The flag exists to stop a seller mid-abuse, so what matters is that the very
    next studio write is refused with the code the studio UI explains.
    """
    assert studio.listing(title="Before The Block")

    blocked = studio.flags(publishing_blocked=True, blocked_reason="scraped uploads")
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["publishing_blocked"] is True

    refused = studio.client.post(
        "/api/studio/listings",
        headers=studio.h(studio.seller),
        json={
            "title": "During The Block",
            "category_id": "tooling",
            "license_id": "CC-BY-4.0",
            "pricing_kind": "free",
            "origin": "human",
        },
    )
    assert refused.status_code == 403, refused.text
    assert code_of(refused) == "publishing_blocked"

    lifted = studio.flags(publishing_blocked=False)
    assert lifted.status_code == 200, lifted.text
    assert lifted.json()["publishing_blocked"] is False
    assert studio.listing(title="After The Block")


def test_flag_update_leaves_absent_keys_untouched(studio):
    """Partial update semantics.

    The console sends one flag at a time, so a full-row write would silently
    clear a block whenever an operator ticked 'verified'.
    """
    assert studio.flags(
        publishing_blocked=True, blocked_reason="scraped uploads"
    ).status_code == 200

    response = studio.flags(verified=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is True
    assert body["publishing_blocked"] is True
    assert body["blocked_reason"] == "scraped uploads"

    with tx() as cur:
        cur.execute(
            "SELECT verified, publishing_blocked, blocked_reason FROM mk_sellers"
            " WHERE email = %s",
            (studio.seller,),
        )
        row = dict(cur.fetchone())
    assert row == {
        "verified": True,
        "publishing_blocked": True,
        "blocked_reason": "scraped uploads",
    }


def test_blocking_without_a_reason_is_refused(studio):
    """The reason is shown to the seller and is the only thing that makes a block
    appealable, so it cannot be optional."""
    response = studio.flags(publishing_blocked=True)
    assert response.status_code == 400, response.text
    assert code_of(response) == "blocked_reason_required"
    with tx() as cur:
        cur.execute(
            "SELECT publishing_blocked FROM mk_sellers WHERE email = %s",
            (studio.seller,),
        )
        assert cur.fetchone()["publishing_blocked"] is False


# --- 8. revenue nets refunds -------------------------------------------------


def _paid_credit_order(studio: Studio) -> str:
    listing_id = studio.listing(
        title="Credit Priced Jig",
        origin="ai_assisted",
        pricing_kind="credits",
        price_credits=5000,
        license_id="chud-standard-1.0",
        category_id="gears",
    )
    assert studio.submit(listing_id).status_code == 200
    if _listing_row(listing_id)["status"] != "published":
        assert studio.moderate(listing_id, "approve").status_code == 200
    studio.grant(studio.buyer, 20000)
    response = studio.client.post(
        f"/api/marketplace/listings/{listing_id}/checkout",
        headers=studio.h(studio.buyer),
        json={"currency": "credits"},
    )
    assert response.status_code == 200, response.text
    return response.json()["order_id"]


def _settled_cash_order(studio: Studio, fake, *, price_cents: int = 2500) -> str:
    listing_id = studio.listing(
        title="Cash Priced Jig",
        origin="ai_assisted",
        pricing_kind="cash",
        price_cents=price_cents,
        license_id="chud-standard-1.0",
        category_id="gears",
    )
    assert studio.submit(listing_id).status_code == 200
    if _listing_row(listing_id)["status"] != "published":
        assert studio.moderate(listing_id, "approve").status_code == 200
    studio.enable_connect(fake)
    response = studio.client.post(
        f"/api/marketplace/listings/{listing_id}/checkout",
        headers=studio.h(studio.buyer),
        json={"currency": "usd"},
    )
    assert response.status_code == 200, response.text
    order_id = response.json()["order_id"]
    marketplace_commerce.handle_marketplace_event(
        {
            "id": "evt_admin_settle",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_fake",
                    "payment_status": "paid",
                    "amount_total": price_cents,
                    "currency": "usd",
                    "payment_intent": "pi_test_admin",
                    "metadata": {
                        "chud_kind": "marketplace",
                        "chud_order_id": order_id,
                        "chud_listing_id": listing_id,
                    },
                }
            },
        }
    )
    with tx() as cur:
        cur.execute("SELECT status FROM mk_orders WHERE id = %s", (order_id,))
        assert cur.fetchone()["status"] == "paid"
    return order_id


def test_revenue_reports_amount_minus_refunds(studio, fake_stripe):
    """Refunded money was not earned.

    Reporting gross would inflate GMV and, worse, inflate the platform fee line
    that is reconciled against what Stripe actually kept.
    """
    _paid_credit_order(studio)
    cash_order = _settled_cash_order(studio, fake_stripe, price_cents=2500)
    refunded = marketplace_commerce.refund_order(cash_order, studio.admin, 1000)
    assert refunded["status"] == "paid"

    response = studio.client.get(
        "/api/admin/marketplace/revenue?days=30", headers=studio.h(studio.admin)
    )
    assert response.status_code == 200, response.text
    totals = response.json()["totals"]
    assert totals["unit_sales"] == 2
    # 2500 charged, 1000 returned.
    assert totals["cash_gmv_cents"] == 1500
    # 15% of 2500 is 375, pro-rated onto the 1500 that stuck.
    assert totals["fee_cents"] == 225
    assert totals["credit_gmv"] == 5000
    assert totals["fee_credits"] == 500

    overview = studio.client.get(
        "/api/admin/marketplace/overview", headers=studio.h(studio.admin)
    )
    assert overview.status_code == 200, overview.text
    money = {row["currency"]: row for row in overview.json()["money"]}
    assert money["usd"]["gmv_cents"] == 1500
    assert money["usd"]["fee_cents"] == 225
    assert money["credits"]["gmv_credits"] == 5000


# --- 9. the refund door ------------------------------------------------------


def test_refund_requires_confirm_to_match_the_payment_intent(studio, fake_stripe):
    """The confirm token is the only thing standing between a mistyped id and
    money leaving the account, so it must be compared exactly and before any
    Stripe call."""
    _settled_cash_order(studio, fake_stripe, price_cents=2500)

    mismatched = studio.client.post(
        "/api/admin/billing/refund",
        headers=studio.h(studio.admin),
        json={
            "payment_intent": "pi_test_admin",
            "confirm": "pi_test_admi",
            "reason": "requested_by_customer",
        },
    )
    assert mismatched.status_code == 400, mismatched.text
    assert [name for name, _ in fake_stripe.calls if name == "Refund.create"] == []

    empty_confirm = studio.client.post(
        "/api/admin/billing/refund",
        headers=studio.h(studio.admin),
        json={
            "payment_intent": "pi_test_admin",
            "confirm": "",
            "reason": "requested_by_customer",
        },
    )
    assert empty_confirm.status_code == 400, empty_confirm.text

    confirmed = studio.client.post(
        "/api/admin/billing/refund",
        headers=studio.h(studio.admin),
        json={
            "payment_intent": "pi_test_admin",
            "confirm": "pi_test_admin",
            "reason": "requested_by_customer",
            "amount_cents": 500,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["kind"] == "marketplace"
    assert [
        kw["amount"] for name, kw in fake_stripe.calls if name == "Refund.create"
    ] == [500]


def test_refund_route_is_operator_only(studio, fake_stripe):
    body = {
        "payment_intent": "pi_test_admin",
        "confirm": "pi_test_admin",
        "reason": "requested_by_customer",
    }
    refused = studio.client.post(
        "/api/admin/billing/refund", headers=studio.h(studio.other), json=body
    )
    assert refused.status_code == 403
    anonymous = studio.client.post("/api/admin/billing/refund", json=body)
    assert anonymous.status_code == 401
    assert [name for name, _ in fake_stripe.calls if name == "Refund.create"] == []
