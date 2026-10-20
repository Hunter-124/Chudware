"""Storefront, listing lifecycle, assets and community surface.

The commerce half of the marketplace (orders, credit ledger, refunds, Connect,
admin moderation) lives in its own module. Everything here is about the parts a
visitor or a seller touches without money moving, and it is deliberately weighted
toward the failure modes that are SILENT in production:

  * the storefront quietly ceasing to be public (a 401 where a visitor expects a
    page, or a download URL the payload advertised and then refused);
  * ``open_source`` drifting away from its derivation after an edit;
  * a rating aggregate that accumulates instead of tracking its reviews;
  * an attacker-supplied Content-Type reaching a response header;
  * a hostile input reaching PostgreSQL and turning a documented 400 into a 500.

Every marketplace test needs ``marketplace_env``; requesting ``client`` is
enough, because the client fixture depends on it.
"""

from __future__ import annotations

import base64
import json
import re

import pytest

from db import tx

# A minimal STEP file. The upload path hashes and stores bytes without parsing
# them, so a well-formed header is enough and keeps the fixture honest.
STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"

# 1x1 PNGs. Two distinct payloads, because the second-thumbnail guard must trip
# on the one-thumbnail index rather than on content-addressed dedupe.
PNG_A = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
    "q842iQAAAABJRU5ErkJggg=="
)
PNG_B = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAEAAH/"
    "gxRXvgAAAABJRU5ErkJggg=="
)

# Long enough to clear ATTESTATION_NOTE_MIN (120 characters).
PROCESS_NOTE = (
    "Measured the original casting with calipers and a height gauge, then modelled "
    "every feature from those measurements in Chudware. No generative tool produced "
    "any of the geometry at any point."
)


def code_of(response) -> str | None:
    """The stable machine-readable code from a MarketplaceError body."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        return None
    return detail.get("code") if isinstance(detail, dict) else None


def create_listing(client, headers: dict[str, str], **over):
    """A free, open-licensed, AI-assisted draft unless overridden."""
    payload = {
        "title": "M3 Bracket",
        "summary": "A right-angle bracket for M3 hardware.",
        "description": "Modelled to fit 20mm extrusion.",
        "category_id": "brackets",
        "license_id": "CC-BY-4.0",
        "pricing_kind": "free",
        "origin": "ai_assisted",
        "tags": ["M3", "Bracket", "m3"],
        "units": "mm",
    }
    payload.update(over)
    return client.post("/api/studio/listings", headers=headers, json=payload)


def upload_asset(
    client,
    headers: dict[str, str],
    listing_id: str,
    filename: str,
    data: bytes = STEP,
    *,
    kind: str = "source",
    role: str = "primary",
    gated: bool = True,
    declared_type: str = "application/octet-stream",
):
    return client.post(
        f"/api/studio/listings/{listing_id}/assets",
        headers=headers,
        files={"file": (filename, data, declared_type)},
        data={"kind": kind, "role": role, "gated": "true" if gated else "false"},
    )


def asset_ids(listing_id: str, kind: str) -> list[str]:
    with tx() as cur:
        cur.execute(
            "SELECT id FROM mk_assets WHERE listing_id=%s AND kind=%s"
            " ORDER BY created_at",
            (listing_id, kind),
        )
        return [str(row["id"]) for row in cur.fetchall()]


def listing_row(listing_id: str) -> dict:
    with tx() as cur:
        cur.execute("SELECT * FROM mk_listings WHERE id=%s", (listing_id,))
        return dict(cur.fetchone())


@pytest.fixture()
def email(request):
    """Per-test addresses.

    ``limits.api_writes`` and ``limits.uploads`` are module-level sliding windows
    keyed by email and are NOT reset between tests, so a shared address would let
    one test's writes throttle a later one into a 429 that looks like a product
    bug.
    """
    stem = re.sub(r"[^a-z0-9]+", "-", request.node.name.lower()).strip("-")[:48]

    def _email(label: str = "user") -> str:
        return f"{label}.{stem}@test.invalid"

    return _email


@pytest.fixture()
def seller(client, activate, as_user, email):
    """An active account enrolled in mk_sellers, with its dev-auth headers.

    Studio routes answer 403 ``not_a_seller`` without the enrolment row, so every
    listing test needs this rather than a bare activated account.
    """
    address = email("seller")
    activate(address)
    response = client.post(
        "/api/studio/seller",
        headers=as_user(address),
        json={"handle": "acme-cad", "display_name": "Acme CAD", "country": "US"},
    )
    assert response.status_code == 200, response.text
    return address, as_user(address)


@pytest.fixture()
def published_free(client, seller):
    """A published free CC-BY listing carrying one gated STEP asset."""
    address, headers = seller
    created = create_listing(client, headers)
    assert created.status_code == 200, created.text
    listing_id = created.json()["listing"]["id"]

    uploaded = upload_asset(client, headers, listing_id, "bracket.step")
    assert uploaded.status_code == 200, uploaded.text

    submitted = client.post(
        f"/api/studio/listings/{listing_id}/submit", headers=headers
    )
    assert submitted.status_code == 200, submitted.text
    listing = submitted.json()["listing"]
    assert listing["status"] == "published", listing["status"]
    return {
        "id": listing_id,
        "slug": listing["slug"],
        "asset_id": uploaded.json()["asset"]["id"],
        "seller": address,
        "headers": headers,
    }


@pytest.fixture()
def published_paid(client, seller, marketplace_env, monkeypatch):
    """A published paid-credits listing carrying one gated STEP asset.

    A seller's FIRST paid listing is normally held in ``in_review`` for a human,
    which would make an access-control assertion pass for the wrong reason: the
    listing would be invisible because it is unpublished, not because the asset is
    paywalled. Switching the hold off is the only way to reach a LIVE paid listing
    without dragging admin moderation into a storefront test.
    """
    monkeypatch.setattr(marketplace_env, "marketplace_review_first_paid", False)
    _, headers = seller
    created = create_listing(
        client,
        headers,
        title="Paid Gearbox",
        category_id="gears",
        license_id="chud-standard-1.0",
        pricing_kind="credits",
        price_credits=5000,
        tags=["gearbox"],
    )
    assert created.status_code == 200, created.text
    listing_id = created.json()["listing"]["id"]
    uploaded = upload_asset(client, headers, listing_id, "gearbox.step")
    assert uploaded.status_code == 200, uploaded.text
    submitted = client.post(
        f"/api/studio/listings/{listing_id}/submit", headers=headers
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["listing"]["status"] == "published", submitted.text
    return {
        "id": listing_id,
        "asset_id": uploaded.json()["asset"]["id"],
        "headers": headers,
    }


# --- 1. the storefront is genuinely public -----------------------------------


def test_meta_is_anonymous_and_ships_the_seeded_reference_data(client):
    response = client.get("/api/marketplace/meta")
    assert response.status_code == 200, response.text
    meta = response.json()

    assert len(meta["licenses"]) == 14
    assert len(meta["categories"]) == 18
    assert len(meta["sources"]) == 11

    # The seller form and the listing page both render the permission matrix, so
    # a license shipped without it silently renders an empty licence panel.
    licenses = {row["id"]: row for row in meta["licenses"]}
    assert licenses["CC-BY-4.0"] == {
        "id": "CC-BY-4.0",
        "name": "CC BY 4.0",
        "url": "https://creativecommons.org/licenses/by/4.0/",
        "is_open_source": True,
        "allows_commercial": True,
        "allows_derivatives": True,
        "requires_attribution": True,
        "share_alike": False,
        "sellable": False,
    }
    # Credits are the price unit the storefront quotes in; 1 credit == $0.001.
    assert meta["fee"]["credits_per_dollar"] == 1000
    assert meta["fee"]["credit_bps"] == 1000
    assert meta["fee"]["cash_bps"] == 1500


def test_anonymous_browse_and_detail_work_with_no_headers(client, published_free):
    browse = client.get("/api/marketplace/listings")
    assert browse.status_code == 200, browse.text
    cards = {item["id"]: item for item in browse.json()["items"]}
    assert published_free["id"] in cards
    card = cards[published_free["id"]]
    assert card["owned"] is False
    assert card["favorited"] is False

    detail = client.get(f"/api/marketplace/listings/{published_free['slug']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == published_free["id"]
    assert body["license"]["id"] == "CC-BY-4.0"
    assert body["license"]["requires_attribution"] is True
    # The payload must not advertise a download the same visitor cannot make.
    assert body["can"]["download"] is True
    urls = [asset["url"] for asset in body["assets"]]
    assert f"/api/marketplace/download/{published_free['asset_id']}" in urls


# --- 2. the feature switch ---------------------------------------------------


def test_marketplace_routes_404_when_the_feature_is_disabled(
    marketplace_env, monkeypatch, reload_settings, activate, as_user
):
    """The switch is enforced per request, not by route registration.

    ``app.py`` registers the marketplace routers unconditionally so that "the
    endpoint 404s" has exactly one cause. That makes ``marketplace_enabled`` the
    only thing standing between a deployment that does not sell anything and a
    live storefront, which is worth an explicit test.
    """
    from starlette.testclient import TestClient

    import app as app_module

    seller = "seller.disabled@test.invalid"
    activate(seller)
    monkeypatch.delenv("CHUDWARE_MARKETPLACE_ENABLED", raising=False)
    settings = reload_settings()
    assert settings.marketplace_enabled is False

    with TestClient(app_module.app) as disabled:
        # Proves the app itself is alive, so the 404s below cannot be a
        # collapsed route table masquerading as a working feature switch.
        assert disabled.get("/healthz").status_code == 200
        assert disabled.get("/api/marketplace/meta").status_code == 404
        assert disabled.get("/api/marketplace/listings").status_code == 404
        # The seller surface is not a public path, so AuthMiddleware answers
        # before the route runs; a signed-in caller is the only way to observe
        # the feature switch there rather than the identity check.
        overview = disabled.get(
            "/api/studio/overview", headers=as_user(seller)
        )
        assert overview.status_code == 404, overview.text


# --- 3. anonymous writes are refused, not crashed ----------------------------


@pytest.mark.parametrize(
    ("suffix", "body"),
    [
        ("favorite", None),
        ("reviews", {"rating": 5}),
        ("report", {"reason": "license"}),
        ("checkout", {"currency": "credits"}),
    ],
)
def test_anonymous_write_in_the_public_subtree_is_401_not_500(
    client, published_free, suffix, body
):
    """Regression guard for the AuthError-on-a-public-path 500.

    ``/api/marketplace`` is in ``auth.PUBLIC_PATHS``, where AuthMiddleware
    short-circuits and wraps nothing. ``auth.AuthError`` is a plain Exception, so
    every authenticated write in this subtree answered 500 until
    ``marketplace_authz.require_user`` translated it. The storefront branches on
    401 to open its sign-in prompt, so a 500 here loses the sign-in flow.

    The listing is real and published: a route that checked the listing before the
    caller would answer 200/404 and fail this test, which is the point.
    """
    url = f"/api/marketplace/listings/{published_free['id']}/{suffix}"
    response = client.post(url, json=body) if body else client.post(url)
    assert response.status_code == 401, f"{response.status_code} {response.text}"


# --- 4. listing lifecycle ----------------------------------------------------


def test_free_listing_creates_normalizes_publishes(client, seller):
    _, headers = seller
    created = create_listing(client, headers)
    assert created.status_code == 200, created.text
    listing = created.json()["listing"]
    listing_id = listing["id"]

    # array_to_tsvector emits tags verbatim as lexemes, so a tag that reaches the
    # database capitalised is simply unsearchable.
    assert listing["tags"] == ["m3", "bracket"]
    assert listing["status"] == "draft"

    assert upload_asset(client, headers, listing_id, "bracket.step").status_code == 200

    submitted = client.post(
        f"/api/studio/listings/{listing_id}/submit", headers=headers
    )
    assert submitted.status_code == 200, submitted.text

    row = listing_row(listing_id)
    assert row["status"] == "published"
    assert row["open_source"] is True
    assert row["published_at"] is not None


def test_published_listing_is_reachable_on_every_discovery_surface(
    client, published_free
):
    listing_id = published_free["id"]

    def ids(query: str) -> set[str]:
        response = client.get(f"/api/marketplace/listings?{query}")
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    assert listing_id in ids("shelf=open_source")
    assert listing_id in ids("shelf=free")
    assert listing_id in ids("q=bracket")
    assert listing_id in ids("tag=m3")
    # ai_assisted origin, so the human shelf must not carry it.
    assert listing_id not in ids("shelf=human")
    assert listing_id not in ids("shelf=paid")

    facets = client.get("/api/marketplace/listings").json()["facets"]
    assert {"id": "brackets", "name": "Brackets & mounts", "count": 1} in (
        facets["categories"]
    )
    assert {"tag": "m3", "count": 1} in facets["tags"]
    assert [row["id"] for row in facets["licenses"]] == ["CC-BY-4.0"]


# --- 5. open_source is derived, never stored by hand -------------------------


def test_open_source_is_rederived_when_the_license_changes(client, published_free):
    """``open_source`` is license.is_open_source AND pricing_kind == 'free'.

    Both the create and the patch path write the column, so the two must agree on
    the derivation forever. A patch that left the old value would keep a
    now-proprietary listing on the open-source shelf, which is the one claim the
    shelf makes.
    """
    listing_id = published_free["id"]
    headers = published_free["headers"]

    # CC-BY-NC-4.0 is a free-download license that is NOT open source.
    patched = client.patch(
        f"/api/studio/listings/{listing_id}",
        headers=headers,
        json={"license_id": "CC-BY-NC-4.0"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["listing"]["open_source"] is False
    assert listing_row(listing_id)["open_source"] is False
    assert listing_id not in {
        item["id"]
        for item in client.get(
            "/api/marketplace/listings?shelf=open_source"
        ).json()["items"]
    }

    restored = client.patch(
        f"/api/studio/listings/{listing_id}",
        headers=headers,
        json={"license_id": "CC-BY-4.0"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["listing"]["open_source"] is True
    assert listing_row(listing_id)["open_source"] is True


# --- 6/7. gated bytes follow the listing's price, not the flag ---------------


def test_anonymous_downloads_a_free_listings_gated_asset(client, published_free):
    """Regression guard: the free shelf's audience has not signed in yet.

    ``asset_visible`` once checked for an anonymous caller BEFORE checking whether
    the listing was free, so a signed-out visitor was refused bytes the detail
    payload had just advertised -- and an anonymous caller can never obtain an
    entitlement, so the file was simply unreachable.
    """
    response = client.get(
        f"/api/marketplace/download/{published_free['asset_id']}"
    )
    assert response.status_code == 200, f"{response.status_code} {response.text}"
    assert response.content == STEP
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_paid_listing_asset_is_refused_without_an_entitlement(
    client, published_paid, activate, as_user, email
):
    asset_id = published_paid["asset_id"]

    # The seller can fetch it, which proves the refusals below are access
    # control and not a missing file or a broken rel_path.
    owner = client.get(
        f"/api/marketplace/download/{asset_id}", headers=published_paid["headers"]
    )
    assert owner.status_code == 200, owner.text
    assert owner.content == STEP

    assert client.get(f"/api/marketplace/download/{asset_id}").status_code == 404

    stranger = email("stranger")
    activate(stranger)
    signed_in = client.get(
        f"/api/marketplace/download/{asset_id}", headers=as_user(stranger)
    )
    assert signed_in.status_code == 404, signed_in.text

    # The ungated public route must never serve a gated asset, whoever asks.
    assert client.get(f"/api/marketplace/assets/{asset_id}").status_code == 404
    assert (
        client.get(
            f"/api/marketplace/assets/{asset_id}", headers=as_user(stranger)
        ).status_code
        == 404
    )


# --- 8. review aggregates ----------------------------------------------------


def test_review_aggregates_follow_every_write(
    client, published_free, activate, as_user, email
):
    """rating_sum/rating_count are denormalised onto mk_listings.

    They are recomputed from mk_reviews on every write rather than incremented,
    because an increment drifts the moment a review is edited or hidden -- and a
    drifted aggregate silently reorders the whole rating shelf.
    """
    listing_id = published_free["id"]
    first, second = email("reviewer-one"), email("reviewer-two")
    activate(first)
    activate(second)

    for reviewer, rating in ((first, 5), (second, 3)):
        response = client.post(
            f"/api/marketplace/listings/{listing_id}/reviews",
            headers=as_user(reviewer),
            json={"rating": rating, "body": "Fits as modelled."},
        )
        assert response.status_code == 200, response.text

    row = listing_row(listing_id)
    assert row["rating_count"] == 2
    assert row["rating_sum"] == 8

    detail = client.get(f"/api/marketplace/listings/{listing_id}").json()
    assert detail["reviews"]["count"] == 2
    assert detail["reviews"]["average"] == 4.0

    # Same author, second write: an UPDATE, so the count must not move.
    updated = client.post(
        f"/api/marketplace/listings/{listing_id}/reviews",
        headers=as_user(first),
        json={"rating": 1, "body": "Revised after printing it."},
    )
    assert updated.status_code == 200, updated.text

    row = listing_row(listing_id)
    assert row["rating_count"] == 2
    assert row["rating_sum"] == 4


def test_seller_cannot_review_their_own_listing(client, published_free):
    response = client.post(
        f"/api/marketplace/listings/{published_free['id']}/reviews",
        headers=published_free["headers"],
        json={"rating": 5},
    )
    assert response.status_code == 400, response.text
    assert code_of(response) == "self_review"
    assert listing_row(published_free["id"])["rating_count"] == 0


# --- 9. provenance -----------------------------------------------------------


def test_human_origin_requires_an_attestation_before_submission(client, seller):
    _, headers = seller
    created = create_listing(
        client, headers, title="Hand Modelled Jig", category_id="tooling",
        origin="human",
    )
    assert created.status_code == 200, created.text
    listing_id = created.json()["listing"]["id"]
    assert upload_asset(client, headers, listing_id, "jig.step").status_code == 200

    refused = client.post(
        f"/api/studio/listings/{listing_id}/submit", headers=headers
    )
    assert refused.status_code == 400, refused.text
    assert code_of(refused) == "attestation_required"
    assert listing_row(listing_id)["status"] == "draft"


def test_attestation_demands_a_substantive_process_note(client, seller):
    _, headers = seller
    created = create_listing(
        client, headers, title="Hand Modelled Jig", category_id="tooling",
        origin="human",
    )
    listing_id = created.json()["listing"]["id"]
    url = f"/api/studio/listings/{listing_id}/attestation"

    # The 120-character floor mirrors mk_attestations_process_note_substantive,
    # restated in the API so a short note is a message and not a constraint crash.
    short = client.post(url, headers=headers, json={"process_note": "too short"})
    assert short.status_code == 400, short.text
    assert listing_row(listing_id)["attestation_status"] == "none"

    accepted = client.post(
        url,
        headers=headers,
        json={
            "process_note": PROCESS_NOTE,
            "tools": ["calipers", "height gauge", "Chudware"],
            "hours": 6.5,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["attestation"]["status"] == "pending"
    assert listing_row(listing_id)["attestation_status"] == "pending"


def test_unverified_human_claim_stays_off_the_human_shelf(client, seller):
    """The human shelf's whole value is that nobody self-certifies onto it."""
    _, headers = seller
    created = create_listing(
        client, headers, title="Hand Modelled Jig", category_id="tooling",
        origin="human", tags=["jig"],
    )
    listing_id = created.json()["listing"]["id"]
    assert upload_asset(client, headers, listing_id, "jig.step").status_code == 200
    assert (
        client.post(
            f"/api/studio/listings/{listing_id}/attestation",
            headers=headers,
            json={"process_note": PROCESS_NOTE, "hours": 6.5},
        ).status_code
        == 200
    )
    submitted = client.post(
        f"/api/studio/listings/{listing_id}/submit", headers=headers
    )
    assert submitted.status_code == 200, submitted.text

    row = listing_row(listing_id)
    assert row["status"] == "in_review"
    assert row["attestation_status"] == "pending"
    shelf = client.get("/api/marketplace/listings?shelf=human").json()["items"]
    assert listing_id not in {item["id"] for item in shelf}


# --- 10. media types come from the extension, never from the client ----------


def test_stored_content_type_ignores_the_declared_multipart_type(client, seller):
    """A stored 'text/html' was a same-origin stored-XSS primitive.

    A seller could upload two ungated .txt assets, declare one
    application/javascript and one text/html, and `script-src 'self'` would
    execute the pair against a visitor's -- or a reviewing admin's -- session.
    """
    _, headers = seller
    listing_id = create_listing(client, headers).json()["listing"]["id"]
    uploaded = upload_asset(
        client,
        headers,
        listing_id,
        "payload.txt",
        b"<script>alert(1)</script>\n",
        kind="document",
        role="",
        declared_type="text/html",
    )
    assert uploaded.status_code == 200, uploaded.text

    with tx() as cur:
        cur.execute(
            "SELECT content_type FROM mk_assets WHERE id=%s",
            (uploaded.json()["asset"]["id"],),
        )
        stored = cur.fetchone()["content_type"]
    assert stored == "text/plain"


def test_ungated_image_serves_inline_and_everything_else_attaches(client, seller):
    _, headers = seller
    listing_id = create_listing(client, headers).json()["listing"]["id"]
    image = upload_asset(
        client, headers, listing_id, "shot.png", PNG_A,
        kind="image", role="gallery", gated=False,
    )
    assert image.status_code == 200, image.text
    document = upload_asset(
        client, headers, listing_id, "notes.txt", b"assembly notes\n",
        kind="document", role="", gated=False,
    )
    assert document.status_code == 200, document.text
    assert upload_asset(client, headers, listing_id, "bracket.step").status_code == 200
    assert (
        client.post(
            f"/api/studio/listings/{listing_id}/submit", headers=headers
        ).status_code
        == 200
    )

    served_image = client.get(
        f"/api/marketplace/assets/{image.json()['asset']['id']}"
    )
    assert served_image.status_code == 200, served_image.text
    assert served_image.headers["content-type"] == "image/png"
    assert served_image.headers["content-disposition"] == 'inline; filename="shot.png"'
    assert served_image.headers["X-Content-Type-Options"] == "nosniff"
    assert served_image.content == PNG_A

    served_doc = client.get(
        f"/api/marketplace/assets/{document.json()['asset']['id']}"
    )
    assert served_doc.status_code == 200, served_doc.text
    assert served_doc.headers["content-disposition"] == (
        'attachment; filename="notes.txt"'
    )
    assert served_doc.headers["X-Content-Type-Options"] == "nosniff"


# --- 11. hostile input is a clean 4xx, never a 500 --------------------------


def test_duplicate_asset_bytes_are_refused_as_409(client, seller):
    _, headers = seller
    listing_id = create_listing(client, headers).json()["listing"]["id"]
    assert upload_asset(client, headers, listing_id, "bracket.step").status_code == 200

    again = upload_asset(client, headers, listing_id, "copy.step", role="")
    assert again.status_code == 409, again.text
    assert code_of(again) == "duplicate_asset"
    # The rejected upload must not leave its bytes behind.
    assert len(asset_ids(listing_id, "source")) == 1


def test_second_thumbnail_is_refused_as_409(client, seller):
    _, headers = seller
    listing_id = create_listing(client, headers).json()["listing"]["id"]
    first = upload_asset(
        client, headers, listing_id, "thumb.png", PNG_A,
        kind="image", role="thumbnail", gated=False,
    )
    assert first.status_code == 200, first.text

    second = upload_asset(
        client, headers, listing_id, "other.png", PNG_B,
        kind="image", role="thumbnail", gated=False,
    )
    assert second.status_code == 409, second.text
    assert code_of(second) == "thumbnail_exists"
    assert len(asset_ids(listing_id, "image")) == 1


def test_listing_cannot_be_forked_from_itself(client, seller):
    _, headers = seller
    listing_id = create_listing(client, headers).json()["listing"]["id"]
    response = client.patch(
        f"/api/studio/listings/{listing_id}",
        headers=headers,
        json={"forked_from": listing_id},
    )
    assert response.status_code == 400, response.text
    assert code_of(response) == "invalid_fork"
    assert listing_row(listing_id)["forked_from"] is None


def test_non_finite_attestation_hours_is_refused(client, seller):
    """json.loads accepts bare NaN, and NUMERIC does not.

    Python's json module emits and accepts ``NaN`` even though JSON does not have
    it, so a body written by hand reaches the handler as a float. Without the
    isfinite guard it reaches a NUMERIC column and answers 500.
    """
    _, headers = seller
    created = create_listing(
        client, headers, title="Hand Modelled Jig", category_id="tooling",
        origin="human",
    )
    listing_id = created.json()["listing"]["id"]
    raw = json.dumps({"process_note": PROCESS_NOTE, "hours": float("nan")})
    assert "NaN" in raw

    response = client.post(
        f"/api/studio/listings/{listing_id}/attestation",
        headers={**headers, "Content-Type": "application/json"},
        content=raw,
    )
    assert response.status_code == 400, f"{response.status_code} {response.text}"
    assert code_of(response) == "invalid_input"
    assert listing_row(listing_id)["attestation_status"] == "none"


def test_unsupported_upload_extension_is_refused(client, seller):
    _, headers = seller
    listing_id = create_listing(client, headers).json()["listing"]["id"]
    response = upload_asset(client, headers, listing_id, "evil.exe", b"MZ\x00\x00")
    assert response.status_code == 400, response.text
    assert code_of(response) == "unsupported_format"
    assert asset_ids(listing_id, "source") == []


def test_forged_pagination_cursor_is_400(client, published_free):
    """A cursor is spliced in as bound parameters, but binding is not validation.

    The keys are compared against typed columns, so a forged cursor with a string
    where the ``downloads`` sort expects a bigint would reach PostgreSQL, fail the
    cast, and answer 500 where the documented answer is 400.
    """
    payload = json.dumps(
        {"s": "downloads", "k": ["not-an-int", "not-a-uuid"]}, separators=(",", ":")
    )
    forged = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    response = client.get(
        f"/api/marketplace/listings?sort=downloads&cursor={forged}"
    )
    assert response.status_code == 400, f"{response.status_code} {response.text}"
    assert code_of(response) == "invalid_cursor"


# --- 12. a filename that would violate the rel_path CHECK -------------------


def test_filename_with_dot_dot_stores_a_safe_rel_path(client, seller):
    """mk_assets_rel_path_relative refuses any rel_path containing '..'.

    So a filename the seller's OS accepts must be collapsed before it is stored,
    or an ordinary upload becomes a CheckViolation and a 500.
    """
    _, headers = seller
    listing_id = create_listing(client, headers).json()["listing"]["id"]
    response = upload_asset(client, headers, listing_id, "gear..step")
    assert response.status_code == 200, f"{response.status_code} {response.text}"

    with tx() as cur:
        cur.execute(
            "SELECT filename, rel_path FROM mk_assets WHERE id=%s",
            (response.json()["asset"]["id"],),
        )
        row = cur.fetchone()
    assert ".." not in row["rel_path"]
    assert ".." not in row["filename"]
    assert row["rel_path"].endswith("/gear.step")


# --- 13. favourites and collections round-trip ------------------------------


def test_favorite_round_trip_leaves_no_counter_drift(
    client, published_free, activate, as_user, email
):
    """mk_listings.favorites is a denormalised counter.

    Un-favouriting has to decrement exactly what favouriting incremented, and
    only when a row actually changed, or the number drifts upward every time a
    visitor toggles the heart.
    """
    listing_id = published_free["id"]
    fan = email("fan")
    activate(fan)
    headers = as_user(fan)

    added = client.post(
        f"/api/marketplace/listings/{listing_id}/favorite", headers=headers
    )
    assert added.status_code == 200, added.text
    assert added.json() == {"favorited": True, "favorites": 1}

    # Idempotent: a double tap must not double the count.
    again = client.post(
        f"/api/marketplace/listings/{listing_id}/favorite", headers=headers
    )
    assert again.status_code == 200, again.text
    assert again.json()["favorites"] == 1

    removed = client.delete(
        f"/api/marketplace/listings/{listing_id}/favorite", headers=headers
    )
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"favorited": False, "favorites": 0}
    assert listing_row(listing_id)["favorites"] == 0

    # A delete with nothing to delete must not push the counter negative.
    assert (
        client.delete(
            f"/api/marketplace/listings/{listing_id}/favorite", headers=headers
        ).json()["favorites"]
        == 0
    )


def test_collection_round_trip(client, published_free, activate, as_user, email):
    listing_id = published_free["id"]
    curator = email("curator")
    activate(curator)
    headers = as_user(curator)

    created = client.post(
        "/api/marketplace/collections",
        headers=headers,
        json={"name": "Bench Fixtures", "blurb": "Things I keep reprinting."},
    )
    assert created.status_code == 200, created.text
    collection = created.json()["collection"]
    assert collection["slug"] == "bench-fixtures"
    collection_id = collection["id"]

    added = client.put(
        f"/api/marketplace/collections/{collection_id}/items/{listing_id}",
        headers=headers,
    )
    assert added.status_code == 200, added.text

    listed = client.get("/api/marketplace/collections", headers=headers)
    assert listed.status_code == 200, listed.text
    stored = listed.json()["items"][0]
    assert stored["item_count"] == 1
    assert [item["id"] for item in stored["items"]] == [listing_id]

    removed = client.delete(
        f"/api/marketplace/collections/{collection_id}/items/{listing_id}",
        headers=headers,
    )
    assert removed.status_code == 200, removed.text
    after = client.get("/api/marketplace/collections", headers=headers).json()
    assert after["items"][0]["item_count"] == 0
    assert after["items"][0]["items"] == []

    # Another account's collection is not addressable, not merely empty.
    intruder = email("intruder")
    activate(intruder)
    assert (
        client.put(
            f"/api/marketplace/collections/{collection_id}/items/{listing_id}",
            headers=as_user(intruder),
        ).status_code
        == 404
    )


def test_fork_write_authorization_and_read_visibility_follow_upstream(
    client, seller, activate, as_user, email
):
    _, owner_headers = seller
    upstream = create_listing(client, owner_headers, title="PRIVATE CANARY").json()["listing"]["id"]
    stranger = email("forker")
    activate(stranger)
    headers = as_user(stranger)
    assert client.post("/api/studio/seller", headers=headers, json={
        "handle": "forker", "display_name": "Forker",
    }).status_code == 200
    assert client.get(f"/api/marketplace/listings/{upstream}", headers=headers).status_code == 404
    denied = create_listing(client, headers, forked_from=upstream)
    assert denied.status_code == 400
    assert code_of(denied) == "invalid_fork"
    child = create_listing(client, headers, title="Child Listing").json()["listing"]["id"]
    assert client.patch(f"/api/studio/listings/{child}", headers=headers, json={
        "forked_from": upstream,
    }).status_code == 400
    assert client.post(f"/api/studio/listings/{upstream}/submit", headers=owner_headers).status_code == 200
    assert client.patch(f"/api/studio/listings/{child}", headers=headers, json={
        "forked_from": upstream,
    }).status_code == 200
    assert client.post(f"/api/studio/listings/{child}/submit", headers=headers).status_code == 200
    assert client.get(f"/api/marketplace/listings/{child}").json()["forked_from"]["title"] == "PRIVATE CANARY"
    assert client.post(f"/api/studio/listings/{upstream}/unlist", headers=owner_headers).status_code == 200
    for viewer_headers in ({}, headers):
        detail = client.get(f"/api/marketplace/listings/{child}", headers=viewer_headers)
        assert detail.status_code == 200
        assert detail.json()["forked_from"] is None
        assert "PRIVATE CANARY" not in detail.text
    owner_detail = client.get(f"/api/marketplace/listings/{child}", headers=owner_headers)
    assert owner_detail.json()["forked_from"]["id"] == upstream


def test_upload_yields_without_listing_lock_and_concurrent_patch_progresses(
    client, seller, monkeypatch
):
    import asyncio
    from contextlib import contextmanager
    import httpx
    import marketplace
    from starlette.datastructures import UploadFile

    _, headers = seller
    listing_id = create_listing(client, headers).json()["listing"]["id"]
    original_tx = marketplace.tx

    @contextmanager
    def bounded_tx():
        with original_tx() as cur:
            cur.execute("SET LOCAL lock_timeout='250ms'")
            cur.execute("SET LOCAL statement_timeout='1s'")
            yield cur

    monkeypatch.setattr(marketplace, "tx", bounded_tx)
    original_read = UploadFile.read

    async def exercise():
        reading, release = asyncio.Event(), asyncio.Event()
        ticks = []
        stop = False

        async def heartbeat():
            while not stop:
                ticks.append(asyncio.get_running_loop().time())
                await asyncio.sleep(0.01)

        async def barrier_read(file, size=-1):
            if file.filename == "barrier.step" and not reading.is_set():
                reading.set()
                await asyncio.wait_for(release.wait(), 2)
            return await original_read(file, size)

        monkeypatch.setattr(UploadFile, "read", barrier_read)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as http:
            beat = asyncio.create_task(heartbeat())
            upload = asyncio.create_task(http.post(
                f"/api/studio/listings/{listing_id}/assets", headers=headers,
                files={"file": ("barrier.step", STEP)}, data={"kind": "source", "role": "primary"},
            ))
            try:
                await asyncio.wait_for(reading.wait(), 2)
                # Upload remains suspended while another request commits an edit.
                patched = await asyncio.wait_for(http.patch(
                    f"/api/studio/listings/{listing_id}", headers=headers,
                    json={"summary": "Concurrent patch committed"},
                ), 2)
                assert patched.status_code == 200, patched.text
                assert not upload.done()
                await asyncio.sleep(0.06)
                release.set()
                uploaded = await asyncio.wait_for(upload, 2)
                assert uploaded.status_code == 200, uploaded.text
            finally:
                release.set()
                if not upload.done():
                    upload.cancel()
                await asyncio.gather(upload, return_exceptions=True)
                stop = True
                await beat
        assert len(ticks) >= 5
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.2

    asyncio.run(exercise())
    assert listing_row(listing_id)["summary"] == "Concurrent patch committed"
    assert len(asset_ids(listing_id, "source")) == 1


def test_listing_lock_wait_does_not_block_gateway_heartbeat(client, seller, monkeypatch):
    import asyncio
    from contextlib import contextmanager
    import threading
    import httpx
    import marketplace

    _, headers = seller
    listing_id = create_listing(client, headers).json()["listing"]["id"]
    locked, release = threading.Event(), threading.Event()
    errors = []
    original_tx = marketplace.tx

    @contextmanager
    def bounded_tx():
        with original_tx() as cur:
            cur.execute("SET LOCAL lock_timeout='1s'")
            cur.execute("SET LOCAL statement_timeout='2s'")
            yield cur

    monkeypatch.setattr(marketplace, "tx", bounded_tx)

    def hold_listing():
        try:
            with bounded_tx() as cur:
                cur.execute("SELECT id FROM mk_listings WHERE id=%s FOR UPDATE", (listing_id,))
                locked.set()
                assert release.wait(3), "fixture lock release timed out"
        except BaseException as exc:
            errors.append(exc)

    holder = threading.Thread(target=hold_listing)
    holder.start()
    timer = None
    try:
        assert locked.wait(2), errors

        async def exercise():
            ticks = []
            stop = False

            async def heartbeat():
                while not stop:
                    ticks.append(asyncio.get_running_loop().time())
                    await asyncio.sleep(0.01)

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as http:
                beat = asyncio.create_task(heartbeat())
                try:
                    patched = await asyncio.wait_for(http.patch(
                        f"/api/studio/listings/{listing_id}", headers=headers,
                        json={"summary": "Patched after row lock"},
                    ), 2)
                    assert patched.status_code == 200, patched.text
                finally:
                    stop = True
                    await beat
            assert len(ticks) >= 10
            assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.2

        # Independent wall-clock release bounds even an old blocked event loop.
        timer = threading.Timer(0.3, release.set)
        timer.start()
        asyncio.run(exercise())
    finally:
        release.set()
        if timer is not None:
            timer.cancel()
            timer.join(2)
        holder.join(3)
    assert not holder.is_alive()
    assert errors == []


def test_concurrent_upload_commit_serializes_seller_quota(client, seller, marketplace_env, monkeypatch):
    import asyncio
    import httpx

    _, headers = seller
    listing_ids = [
        create_listing(client, headers, title=f"Quota Listing {index}").json()["listing"]["id"]
        for index in range(2)
    ]
    monkeypatch.setattr(marketplace_env, "marketplace_seller_quota_bytes", len(STEP) + 1)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as http:
            return await asyncio.wait_for(asyncio.gather(*(
                http.post(
                    f"/api/studio/listings/{listing_id}/assets", headers=headers,
                    files={"file": ("quota.step", STEP)}, data={"kind": "source", "role": "primary"},
                ) for listing_id in listing_ids
            )), 3)

    responses = asyncio.run(exercise())
    assert sorted(response.status_code for response in responses) == [200, 413]
    rejected = next(response for response in responses if response.status_code == 413)
    assert code_of(rejected) == "quota_exceeded"
    with tx() as cur:
        cur.execute(
            "SELECT count(*) AS n, sum(bytes) AS used FROM mk_assets WHERE listing_id=ANY(%s::uuid[])",
            (listing_ids,),
        )
        row = cur.fetchone()
        assert (row["n"], row["used"]) == (1, len(STEP))
    stored = [
        path for path in marketplace_env.marketplace_root.rglob("quota.step")
        if path.is_file()
    ]
    assert len(stored) == 1
