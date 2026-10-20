from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import (  # noqa: E402
    accounts,
    app as app_mod,
    authn,
    billing,
    db,
    entitlement,
    oauth,
    portal_serve,
    releases,
)
from gateway.config import settings  # noqa: E402


def _seed(email: str, *, plan: str = "desktop"):
    accounts.ensure_account(email, display="Portal Customer")
    accounts.set_status(email, "active", decided_by="test")
    accounts.set_plan(email, plan)
    return accounts.get_account(email)


def _client(monkeypatch, email: str) -> TestClient:
    monkeypatch.setattr(settings, "dev_mode", True)
    return TestClient(app_mod.app, headers={"X-Dev-User": email})


def test_html_navigation_redirects_to_public_signin_but_api_and_post_keep_401(
    pg, monkeypatch
):
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "session_secret", "portal-test-session-secret")
    monkeypatch.setattr(settings, "cf_team_domain", "")
    monkeypatch.setattr(settings, "cf_aud", "")
    client = TestClient(app_mod.app)

    navigation = client.get(
        "/?from=deep", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert navigation.status_code == 303
    assert navigation.headers["location"] == "/signin?next=%2F%3Ffrom%3Ddeep"
    signin = client.get(navigation.headers["location"])
    assert signin.status_code == 200
    assert signin.headers["content-security-policy"].startswith("default-src 'self'")

    api_response = client.get(
        "/api/me", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert api_response.status_code == 401
    assert api_response.headers["content-type"].startswith("application/json")
    post_response = client.post(
        "/", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert post_response.status_code == 401
    assert "location" not in post_response.headers


def test_portal_has_ordinary_csp_and_hashed_assets_are_immutable(pg, monkeypatch):
    email = "portal-csp@example.com"
    _seed(email)
    client = _client(monkeypatch, email)
    page = client.get("/")
    assert page.status_code == 200
    portal_csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in portal_csp
    assert "wasm-unsafe-eval" not in portal_csp
    assert "worker-src" not in portal_csp
    assert "cross-origin-embedder-policy" not in page.headers
    assert page.headers["cache-control"] == "no-cache"
    assert page.headers["vary"].lower() == "accept-encoding"

    # Derived from the page, not hardcoded: the filename embeds a content hash,
    # so any edit to the portal script renames it. Pinning the literal name made
    # this test fail for the wrong reason every time the portal changed, while
    # proving nothing extra. What matters is that whatever the page asks for is
    # served immutable and its name really is its digest.
    served = re.search(r'src="/portal-assets/(portal\.[0-9a-f]{12}\.js)"', page.text)
    assert served, "portal page must load a content-hashed script"
    name = served.group(1)
    digest = name.split(".")[1]

    asset = client.get(f"/portal-assets/{name}")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert asset.headers["content-type"].startswith("application/javascript")
    assert hashlib.sha256(asset.content).hexdigest().startswith(digest)
    assert asset.headers["vary"].lower() == "accept-encoding"


def test_every_portal_asset_reference_is_hashed_served_and_digest_true(pg, monkeypatch):
    """A stale asset reference ships a portal that 404s its own stylesheet.

    Both the stylesheet and the script are discovered from the served page, so
    renaming an asset without updating index.html — or leaving a reference to a
    deleted hash — fails here rather than in a customer's browser. The filename
    must really be the content digest for the immutable cache header to be safe.
    """
    email = "portal-assets@example.com"
    _seed(email)
    client = _client(monkeypatch, email)
    page = client.get("/")
    assert page.status_code == 200
    refs = re.findall(r'"/portal-assets/([A-Za-z0-9_-]+\.[0-9a-f]{12}\.[a-z]+)"', page.text)
    assert sorted(name.rsplit(".", 1)[1] for name in refs) == ["css", "js"]
    for name in refs:
        digest = name.split(".")[1]
        asset = client.get(f"/portal-assets/{name}")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert hashlib.sha256(asset.content).hexdigest().startswith(digest)


def test_portal_is_destination_oriented_and_keeps_every_legacy_route(pg, monkeypatch):
    """Overview must land first and no legacy task may lose its view.

    The portal is hash-routed static markup: a view that disappears from
    index.html is a silently dead bookmark, and a nav entry without a matching
    view is a dead button. The Team & files page additionally needs its org
    scaffolding (create form, invite banner, upsell, org list) because the
    script renders /api/orgs data into those exact ids.
    """
    email = "portal-markup@example.com"
    _seed(email)
    client = _client(monkeypatch, email)
    html = client.get("/").text

    assert 'class="page active" data-view="overview"' in html
    assert html.count('class="page active"') == 1
    for view in ("overview", "team", "account", "billing", "usage", "ai", "downloads", "devices"):
        assert f'data-view="{view}"' in html, f"missing view: {view}"
        assert f'data-page="{view}"' in html, f"missing nav entry: {view}"

    # Overview offers every prominent destination as an outcome card.
    assert 'id="dest-cloud"' in html
    assert html.count("dest-card") >= 5
    for target in ("team", "downloads", "ai", "billing"):
        assert f'class="dest-card" data-go="{target}"' in html, f"no destination card for {target}"

    # Team & files scaffolding the script renders the org flow into.
    for element_id in ("org-create-form", "org-invite-banner", "org-invitations", "team-upsell", "org-list"):
        assert f'id="{element_id}"' in html, f"missing team page element: {element_id}"

    # Accessibility invariants: skip link, live status region, mobile menu state.
    assert 'class="skip-link"' in html
    assert 'id="global-message" class="message" role="status"' in html
    assert 'id="menu-button"' in html and 'aria-expanded="false"' in html

    # The served script drives the org/team contract, not a placeholder.
    js_name = re.search(r'src="/portal-assets/(portal\.[0-9a-f]{12}\.js)"', html).group(1)
    js = client.get(f"/portal-assets/{js_name}").text
    for marker in ("/api/orgs", "org_invite", "/invitations/accept", "transfer-ownership", "bucket_url"):
        assert marker in js, f"portal script lost the org flow marker: {marker}"


def test_every_oauth_provider_has_a_button_on_the_signin_page(pg, monkeypatch):
    """A provider the gateway can complete but never offers is unreachable.

    ``oauth.PROVIDERS`` is the set of flows the callback route will honour, and
    the sign-in page is hand-written HTML, so adding a provider without adding
    its button is a silent dead end for every user who is not typing the start
    URL by hand.
    """
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "session_secret", "signin-buttons-secret")
    monkeypatch.setattr(settings, "cf_team_domain", "")
    monkeypatch.setattr(settings, "cf_aud", "")
    page = TestClient(app_mod.app).get("/signin")
    assert page.status_code == 200
    missing = [
        name
        for name in oauth.PROVIDERS
        if f"/api/auth/oauth/{name}/start" not in page.text
    ]
    assert not missing, f"sign-in page offers no button for: {missing}"


def test_portal_account_reports_plan_security_and_linked_identities(pg, monkeypatch):
    email = "portal-account@example.com"
    _seed(email, plan="desktop")
    authn.set_password(email, "correct horse battery staple", verified=True)
    authn.link_oauth_identity("github", "subject-123", email)
    response = _client(monkeypatch, email).get("/api/portal/account")
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == email
    assert body["plan"] == "desktop"
    assert body["cloud"] is False
    assert body["desktop"] is True
    assert body["has_password"] is True
    assert body["linked_identities"] == ["github"]
    assert "stripe_customer_id" not in body


def test_password_change_and_signout_everywhere_delete_all_sessions(pg, monkeypatch):
    email = "portal-security@example.com"
    _seed(email)
    old = "correct horse battery staple"
    new = "new correct horse battery staple"
    authn.set_password(email, old, verified=True)
    authn.create_session(email, user_agent="one", ip="203.0.113.1")
    authn.create_session(email, user_agent="two", ip="203.0.113.2")
    client = _client(monkeypatch, email)

    changed = client.post(
        "/api/portal/password",
        json={"current_password": old, "new_password": new},
    )
    assert changed.status_code == 200
    credential = authn.get_credential(email)
    assert credential is not None
    assert authn.verify_password(credential["password_hash"], new)
    assert not authn.verify_password(credential["password_hash"], old)

    signed_out = client.post("/api/portal/signout-everywhere")
    assert signed_out.status_code == 200
    assert signed_out.json()["sessions_deleted"] == 2
    with db.tx() as cur:
        cur.execute("SELECT count(*) AS n FROM sessions WHERE email = %s", (email,))
        assert cur.fetchone()["n"] == 0
    assert "chudware_sid=" in signed_out.headers["set-cookie"]
    assert "Max-Age=0" in signed_out.headers["set-cookie"]


def test_oauth_only_admin_grant_cannot_race_a_portal_password_change(pg, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
    from threading import Event

    email = "portal-security@example.com"
    _seed(email)
    old = "correct horse battery staple"
    new = "new correct horse battery staple"
    authn.set_password(email, old, verified=True)
    client = _client(monkeypatch, email)
    checked = Event()
    release = Event()
    admin_lock_entered = Event()
    real_verify = authn.verify_password
    real_lock = authn._lock_account

    def paused_verify(digest, password):
        result = real_verify(digest, password)
        checked.set()
        assert release.wait(10), "portal password change was not released"
        return result

    def observed_lock(address):
        if checked.is_set() and not release.is_set():
            admin_lock_entered.set()
        return real_lock(address)

    monkeypatch.setattr(authn, "verify_password", paused_verify)
    monkeypatch.setattr(authn, "_lock_account", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        changing = pool.submit(
            client.post, "/api/portal/password",
            json={"current_password": old, "new_password": new},
        )
        granting = None
        try:
            assert checked.wait(10), "portal did not check the current password"
            granting = pool.submit(
                authn._cli, ["grant-admin", "--email", email, "--oauth-only"]
            )
            assert admin_lock_entered.wait(10), "admin grant did not reach the account lock"
            with pytest.raises(FutureTimeout):
                granting.result(timeout=0.1)
        finally:
            release.set()
        assert changing.result(timeout=10).status_code == 200
        assert granting.result(timeout=10) == 0
    assert authn.get_credential(email) is None
    assert accounts.get_account(email).role == "admin"


def test_usage_is_credit_only_and_never_exposes_upstream_cost(pg, monkeypatch):
    email = "portal-usage@example.com"
    _seed(email, plan="studio")
    with db.tx() as cur:
        cur.execute(
            "UPDATE accounts SET credit_balance = 9750, credit_ceiling_monthly = 15000 WHERE email = %s",
            (email,),
        )
        cur.execute(
            """
            INSERT INTO credit_ledger
                (id, email, kind, model, credits_delta, note)
            VALUES ('portal-spend', %s, 'spend', 'provider/model', -250, '')
            """,
            (email,),
        )
    response = _client(monkeypatch, email).get("/api/portal/usage?days=30")
    assert response.status_code == 200
    body = response.json()
    assert body["unit"] == "credits"
    assert body["credit_balance"] == 9750
    assert body["credit_ceiling_monthly"] == 15000
    assert body["history"][0]["credits"] == 250
    encoded = response.text.lower()
    assert "usd" not in encoded
    assert "dollar" not in encoded
    assert "provider_cost" not in encoded


def test_invoice_history_is_scoped_through_billing_owner(pg, monkeypatch):
    email = "portal-invoices@example.com"
    _seed(email)
    monkeypatch.setattr(
        billing,
        "invoice_history",
        lambda requested, limit: [{"id": "in_1", "number": "CH-1", "requested": requested, "limit": limit}],
    )
    response = _client(monkeypatch, email).get("/api/portal/invoices?limit=5")
    assert response.status_code == 200
    assert response.json()["invoices"] == [
        {"id": "in_1", "number": "CH-1", "requested": email, "limit": 5}
    ]


def test_portal_device_list_and_revoke_are_account_scoped(pg, monkeypatch):
    email = "portal-devices@example.com"
    other = "other-devices@example.com"
    _seed(email)
    _seed(other)
    own = entitlement._subject_for(
        email, "Design workstation", entitlement._b64(bytes(range(32)))
    )
    foreign = entitlement._subject_for(
        other, "Other workstation", entitlement._b64(bytes(range(1, 33)))
    )
    client = _client(monkeypatch, email)

    listed = client.get("/api/portal/devices")
    assert listed.status_code == 200
    assert [row["subject"] for row in listed.json()["devices"]] == [own]
    assert client.delete(f"/api/portal/devices/{foreign}").status_code == 404
    revoked = client.delete(f"/api/portal/devices/{own}")
    assert revoked.status_code == 200
    assert entitlement.list_devices(email)[0]["revoked_at"] is not None


# ---------------------------------------------------------------------------
# Download tokens, /install.sh, /brand-assets
# ---------------------------------------------------------------------------


def test_download_token_is_issued_with_the_literal_install_command(pg, monkeypatch):
    email = "portal-download@example.com"
    _seed(email)
    monkeypatch.setattr(settings, "session_secret", "portal-download-secret")
    monkeypatch.setenv("CHUDWARE_RELEASES_PUBLIC_BASE_URL", "https://app.chudware.test")
    client = _client(monkeypatch, email)
    response = client.post("/api/portal/download-token")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"token", "expires_in", "install_command"}
    assert body["expires_in"] == 900
    assert body["install_command"] == (
        "curl -fsSL https://app.chudware.test/install.sh | sh -s -- --token "
        + body["token"]
    )
    # The command must be built from the CONFIGURED origin, never the Host
    # header: this string is one a customer is told to pipe into a shell.
    spoofed = client.post(
        "/api/portal/download-token", headers={"Host": "evil.example.com"}
    )
    assert "evil.example.com" not in spoofed.json()["install_command"]
    assert releases._redeem_download_token(body["token"]) == email


def test_download_token_is_bound_to_its_own_account(pg, monkeypatch):
    monkeypatch.setattr(settings, "session_secret", "portal-download-secret")
    first, second = "portal-dl-a@example.com", "portal-dl-b@example.com"
    _seed(first)
    _seed(second)
    token = _client(monkeypatch, first).post("/api/portal/download-token").json()["token"]
    assert releases._redeem_download_token(token) == first
    assert releases._redeem_download_token(token) != second
    # Cross-signing: B's token minted under B's email must not verify as A's.
    other = _client(monkeypatch, second).post("/api/portal/download-token").json()["token"]
    assert releases._redeem_download_token(other) == second
    assert token != other


def test_download_token_requires_a_session(pg, monkeypatch):
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "session_secret", "portal-download-secret")
    response = TestClient(app_mod.app).post("/api/portal/download-token")
    assert response.status_code == 401
    # A POST is never redirected to sign-in: that would answer HTML to a fetch.
    assert "location" not in response.headers


def test_download_token_is_refused_for_a_suspended_account(pg, monkeypatch):
    email = "portal-dl-suspended@example.com"
    _seed(email)
    monkeypatch.setattr(settings, "session_secret", "portal-download-secret")
    client = _client(monkeypatch, email)
    token = client.post("/api/portal/download-token").json()["token"]
    accounts.set_status(email, "suspended", decided_by="test")
    assert releases._redeem_download_token(token) is None


def test_download_token_expires_after_fifteen_minutes(pg, monkeypatch):
    email = "portal-dl-expiry@example.com"
    _seed(email)
    monkeypatch.setattr(settings, "session_secret", "portal-download-secret")
    token = _client(monkeypatch, email).post("/api/portal/download-token").json()["token"]
    minted_at = int(time.time())
    assert releases._redeem_download_token(token, now=minted_at + 899) == email
    assert releases._redeem_download_token(token, now=minted_at + 900) is None


def test_install_script_is_served_publicly_as_plain_text(pg, monkeypatch):
    """`curl … | sh` has no session, so this must answer a script — not the
    sign-in page — to an anonymous caller."""
    monkeypatch.setattr(settings, "dev_mode", False)
    response = TestClient(app_mod.app).get("/install.sh")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.text
    assert body.startswith("#!/bin/sh")
    assert "--token" in body and "sha256" in body
    # It is the file in the tree, not a copy that can drift from it.
    on_disk = (
        Path(__file__).resolve().parents[3] / "packaging" / "linux" / "install.sh"
    ).read_text()
    assert body == on_disk


def test_brand_assets_serve_the_fonts_anonymously_and_nothing_else(pg, monkeypatch):
    """base.css is public and asks for these families by name. Without a public
    font route the sign-in page and the storefront render in a system font."""
    monkeypatch.setattr(settings, "dev_mode", False)
    anonymous = TestClient(app_mod.app)
    fonts = sorted(
        (Path(app_mod.__file__).resolve().parent / "static" / "fonts").glob("*.woff2")
    )
    assert fonts, "no self-hosted typefaces on disk"
    for font in fonts:
        response = anonymous.get(f"/brand-assets/{font.name}")
        assert response.status_code == 200, f"{font.name}: {response.text[:200]}"
        assert response.headers["content-type"] == "font/woff2"
        assert response.headers["cache-control"] == (
            "public, max-age=31536000, immutable"
        )
        assert hashlib.sha256(response.content).hexdigest().startswith(
            font.name.split(".")[1]
        )

    # The prefix is two typefaces, not a reader over static/. Anything that is
    # not a bare content-hashed *.woff2 basename is a 404, including a directory
    # component that would otherwise reach the same file by a second name.
    real = fonts[0].name
    for name in (
        f"fonts/{real}",
        real.replace(".woff2", ".txt"),
        "base.css",
        "index.html",
        "login.js",
        "Rubik-VF.woff2",
    ):
        assert anonymous.get(f"/brand-assets/{name}").status_code == 404, name

    # Traversal. A normalising client (httpx, every browser) collapses
    # "/brand-assets/../x" to "/x" before it is sent, so that spelling never
    # reaches this route at all — it lands on a non-public path and gets 401.
    # Percent-encoding is what actually delivers the traversal intact: the ASGI
    # server decodes it into scope["path"] AFTER routing, so the handler really
    # does receive "../portal.<hash>.css". That is the case worth asserting.
    _seed("brand@example.com")
    portal_css = re.search(
        r'"/portal-assets/(portal\.[0-9a-f]{12}\.css)"',
        _client(monkeypatch, "brand@example.com").get("/").text,
    )
    assert portal_css, "portal page must load a content-hashed stylesheet"
    monkeypatch.setattr(settings, "dev_mode", False)
    for path in (
        "/brand-assets/%2e%2e/portal-assets/" + portal_css.group(1),
        "/brand-assets/%2e%2e/" + portal_css.group(1),
        "/brand-assets/..%2f..%2f..%2f..%2fetc%2fpasswd",
        f"/brand-assets/%2e%2e/static/fonts/{real}",
    ):
        response = anonymous.get(path)
        assert response.status_code == 404, f"{path} -> {response.status_code}"
        assert "font-face" not in response.text and "root:" not in response.text

    # The same file by its real, non-traversing name is a 404 too: the suffix
    # check is what refuses it, and that is the check a normalising client hits.
    assert anonymous.get(
        f"/brand-assets/{portal_css.group(1)}"
    ).status_code == 404

    # Collapsed spelling: not 200, whichever layer refuses it.
    collapsed = anonymous.get("/brand-assets/../" + portal_css.group(1))
    assert collapsed.status_code in (401, 404)
    assert "font-face" not in collapsed.text

    # And the clamp itself, directly: even a woff2-suffixed traversal that got
    # past the name check could not escape static/.
    with pytest.raises(HTTPException) as escape:
        portal_serve._resolve_under_app("../portal_serve.py")
    assert escape.value.status_code == 400


def test_signin_icons_and_robots_are_crawlable_without_a_session(pg, monkeypatch):
    """Google Search follows app.chudware.com's home page to sign-in and shows
    the icon linked there only if it can fetch it anonymously; a data: URI or a
    401 /favicon.ico made every app result show the generic globe."""
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "session_secret", "portal-test-session-secret")
    monkeypatch.setattr(settings, "cf_team_domain", "")
    monkeypatch.setattr(settings, "cf_aud", "")
    crawler = TestClient(app_mod.app)

    home = crawler.get("/", headers={"Accept": "text/html"})
    assert home.status_code == 200 and home.url.path == "/signin"
    icons = re.findall(r'<link rel="(?:icon|apple-touch-icon)" href="([^"]+)"', home.text)
    assert "/favicon.ico" in icons
    expected_type = {
        ".ico": "image/x-icon", ".png": "image/png", ".svg": "image/svg+xml",
    }
    for href in icons:
        assert href.startswith("/"), f"{href[:40]} is not a crawlable URL"
        response = crawler.get(href)
        assert response.status_code == 200, f"{href}: {response.status_code}"
        assert response.headers["content-type"] == expected_type[Path(href).suffix]
        assert response.content

    robots = crawler.get("/robots.txt")
    assert robots.status_code == 200
    assert robots.headers["content-type"] == "text/plain; charset=utf-8"
    assert "Disallow: /api/" in robots.text

    # Exact paths, not prefixes into static/.
    assert crawler.get("/robots.txt/x").status_code == 404
