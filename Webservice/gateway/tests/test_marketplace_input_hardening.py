"""Regression tests for two input-handling defects that answered 500.

Both were found by writing tests for the marketplace rather than by reviewing
it, and both are the same shape: a value that Python accepts, that our
validators accepted, and that blew up below the validator instead of above it.

They are kept in their own file rather than folded into ``test_marketplace.py``
because they are not about marketplace behaviour at all -- they are about the
two shared coercion helpers, ``marketplace._text``/``_int`` and
``marketplace_schema.scrub_text``, and the whole point is that EVERY route
sharing those helpers is covered by them.
"""

from __future__ import annotations

import json

import pytest

# A NUL byte in a bound parameter makes psycopg3 raise DataError *before* the
# statement reaches PostgreSQL, so it never looked like bad input -- it looked
# like a server fault, on routes reachable with no session at all. Every entry
# here 500'd. ?tag= is deliberately absent: normalize_tags already stripped
# control bytes, and its survival is what identified the missing scrub.
NUL_ANONYMOUS_GETS = [
    "/api/marketplace/listings?q=%00ab",
    "/api/marketplace/listings?category=%00ab",
    "/api/marketplace/listings?license=%00ab",
    "/api/marketplace/listings?origin=%00ab",
    "/api/marketplace/listings?source=%00ab",
    "/api/marketplace/listings?pricing=%00ab",
    "/api/marketplace/listings?seller=%00x",
    "/api/marketplace/listings?cursor=%00x",
    "/api/marketplace/suggest?q=%00ab",
    "/api/marketplace/sellers/%00",
    "/api/marketplace/listings/a%00b",
    "/api/marketplace/handle-check?handle=%00",
]


@pytest.mark.parametrize("path", NUL_ANONYMOUS_GETS)
def test_nul_byte_is_never_a_server_error(client, path):
    """A NUL in any anonymous query or path parameter must not 500.

    Asserting ``< 500`` rather than a specific code on purpose: which 4xx (or
    empty 200) is correct differs per route -- a scrubbed slug is an honest 404,
    a scrubbed filter is an empty result -- and pinning each would test the
    routing table rather than the property that matters.
    """
    response = client.get(path)
    assert response.status_code < 500, (path, response.status_code, response.text[:200])


def test_nul_in_a_text_body_field_is_rejected_not_fatal(client, activate, as_user):
    """Same defect on the authenticated side: every text body field shared _text."""
    activate("nul-seller@example.test")
    headers = as_user("nul-seller@example.test")
    enrolled = client.post(
        "/api/studio/seller",
        headers=headers,
        json={"handle": "nulseller", "display_name": "Nul Seller"},
    )
    assert enrolled.status_code == 200, enrolled.text[:200]

    created = client.post(
        "/api/studio/listings",
        headers=headers,
        json={
            "title": "Ti\x00tle with a nul",
            "summary": "summary",
            "category_id": "brackets",
            "license_id": "CC-BY-4.0",
            "pricing_kind": "free",
            "origin": "ai_assisted",
        },
    )
    assert created.status_code < 500, (created.status_code, created.text[:300])
    if created.status_code == 200:
        # Scrubbing is the chosen behaviour, so the NUL must be gone rather than
        # stored -- a text column cannot hold it and a later read would fail.
        title = (created.json().get("listing") or created.json())["title"]
        assert "\x00" not in title, repr(title)


def test_infinite_number_in_an_int_field_is_a_bad_request(client, activate, as_user):
    """``int(float("inf"))`` raises OverflowError, which ``_int`` did not catch.

    Reachable because Python's ``json`` module emits and accepts bare
    ``Infinity``, so a hand-written body (not one a browser would send) arrives
    as a float. The body is built with ``json.dumps`` and asserted to contain the
    literal token, so this test cannot silently stop exercising the path if a
    future serialiser starts rejecting it.
    """
    activate("inf-seller@example.test")
    headers = as_user("inf-seller@example.test")
    client.post(
        "/api/studio/seller",
        headers=headers,
        json={"handle": "infseller", "display_name": "Inf Seller"},
    )

    body = json.dumps(
        {
            "title": "Infinity priced listing",
            "summary": "summary",
            "category_id": "gears",
            "license_id": "chud-standard-1.0",
            "pricing_kind": "cash",
            "price_cents": float("inf"),
            "origin": "ai_assisted",
        }
    )
    assert "Infinity" in body, body

    response = client.post(
        "/api/studio/listings",
        headers={**headers, "Content-Type": "application/json"},
        content=body,
    )
    assert response.status_code == 400, (response.status_code, response.text[:300])


def test_scrub_text_keeps_the_whitespace_a_description_needs():
    """The scrub must not eat tab, newline or carriage return.

    A listing description is markdown; stripping newlines would silently flatten
    every one of them. This is the guard that keeps the fix from over-reaching.
    """
    from marketplace_schema import scrub_text

    assert scrub_text("a\x00b") == "ab"
    assert scrub_text("a\x07b\x1fc\x7fd") == "abcd"
    assert scrub_text("line1\nline2\ttabbed\r\n") == "line1\nline2\ttabbed\r\n"
    assert scrub_text(None) == ""
    assert scrub_text(123) == "123"


def test_description_link_cannot_spend_credits_but_trusted_buy_button_can(
    client, activate, as_user, grant_credits, marketplace_env, monkeypatch
):
    """Run the shipped renderer/CSP against real PostgreSQL purchase consumers.

    Chromium requests are fulfilled exclusively through TestClient. No request
    (including the description's external link) reaches an external server.
    """
    from urllib.parse import urlsplit

    from playwright.sync_api import expect, sync_playwright
    from db import tx

    seller, buyer = "description-seller@test.invalid", "description-buyer@test.invalid"
    activate(seller)
    activate(buyer)
    grant_credits(buyer, 1000)
    monkeypatch.setattr(marketplace_env, "marketplace_review_first_paid", False)
    seller_headers, buyer_headers = as_user(seller), as_user(buyer)
    assert client.post("/api/studio/seller", headers=seller_headers, json={
        "handle": "description-seller", "display_name": "Description Seller",
    }).status_code == 200
    description = '[Read drawing](https://example.invalid/"data-buy="credits)'
    created = client.post("/api/studio/listings", headers=seller_headers, json={
        "title": "Description purchase boundary", "description": description,
        "license_id": "chud-standard-1.0", "pricing_kind": "credits",
        "price_credits": 500, "origin": "ai_assisted",
    })
    assert created.status_code == 200, created.text
    listing_id = created.json()["listing"]["id"]
    assert client.post(f"/api/studio/listings/{listing_id}/submit", headers=seller_headers).status_code == 200
    listing = client.get(f"/api/marketplace/listings/{listing_id}", headers=buyer_headers).json()
    assert listing["description"] == description
    assert not listing["preview_urls"]
    checkouts = []

    def balance():
        with tx() as cur:
            cur.execute("SELECT credit_balance FROM accounts WHERE email=%s", (buyer,))
            return cur.fetchone()["credit_balance"]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()

            def route_fixture(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.hostname != "marketplace-fixture.test":
                    route.abort()
                    return
                path = parsed.path + (("?" + parsed.query) if parsed.query else "")
                if parsed.path.endswith("/checkout"):
                    checkouts.append(json.loads(request.post_data))
                response = client.request(
                    request.method, path, content=request.post_data_buffer,
                    headers={**buyer_headers, "Content-Type": "application/json"},
                )
                # TestClient has already decoded compression; preserve CSP.
                headers = {
                    key: value for key, value in response.headers.items()
                    if key.lower() not in {"content-length", "content-encoding", "transfer-encoding"}
                }
                route.fulfill(status=response.status_code, body=response.content, headers=headers)

            context.route("**/*", route_fixture)
            page = context.new_page()
            page.goto(f"http://marketplace-fixture.test/marketplace#/m/{listing_id}")
            link = page.locator("#mk-description a")
            link.wait_for()
            assert link.get_attribute("data-buy") is None
            assert page.locator("#mk-description [data-buy]").count() == 0
            with page.expect_popup() as popup:
                link.click()
            popup.value.close()
            assert checkouts == []
            assert balance() == 1000
            with tx() as cur:
                cur.execute("SELECT count(*) AS n FROM mk_orders WHERE buyer_email=%s", (buyer,))
                assert cur.fetchone()["n"] == 0
            page.locator('#mk-buy-actions > button[data-buy="credits"]').click()
            expect(page.locator("#mk-buy-message")).to_contain_text("Purchased.")
            assert checkouts == [{"currency": "credits"}]
            assert balance() == 500
            with tx() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM mk_entitlements WHERE listing_id=%s AND email=%s",
                    (listing_id, buyer),
                )
                assert cur.fetchone()["n"] == 1
        finally:
            browser.close()
