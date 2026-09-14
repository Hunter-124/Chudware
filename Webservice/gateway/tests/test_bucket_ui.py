"""The /bucket files workspace: serving, cache, CSP, and markup contracts.

The page is ordinary portal surface — same CSP, revalidated HTML, and hashed
immutable assets as ``/`` — so regressions here are usually someone routing it
through a different pipeline (the old bucket.py HTMLResponse served it with no
CSP and no cache policy at all) or reintroducing an un-hashed script tag that
sticks in browser caches across deploys.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

os.environ["CHUDWARE_WEB_DEV"] = "1"
os.environ.setdefault("CHUDWARE_WEB_DATA_ROOT", str(Path.home() / "chudware-web-data-test"))

from fastapi.testclient import TestClient  # noqa: E402

from gateway import accounts, app as app_mod  # noqa: E402
from gateway.config import settings  # noqa: E402

_STATIC = Path(app_mod.__file__).resolve().parent / "static"


def _seed(email: str, *, plan: str = "desktop"):
    accounts.ensure_account(email, display="Bucket Customer")
    accounts.set_status(email, "active", decided_by="test")
    accounts.set_plan(email, plan)
    return accounts.get_account(email)


def _client(monkeypatch, email: str) -> TestClient:
    monkeypatch.setattr(settings, "dev_mode", True)
    return TestClient(app_mod.app, headers={"X-Dev-User": email})


def test_bucket_page_requires_auth_like_every_portal_page(pg, monkeypatch):
    """/bucket must never be public: anonymous HTML navigation bounces to
    /signin (with the deep link preserved) exactly like the portal index."""
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "session_secret", "bucket-test-session-secret")
    monkeypatch.setattr(settings, "cf_team_domain", "")
    monkeypatch.setattr(settings, "cf_aud", "")
    client = TestClient(app_mod.app)

    navigation = client.get(
        "/bucket?team=personal", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert navigation.status_code == 303
    assert navigation.headers["location"] == "/signin?next=%2Fbucket%3Fteam%3Dpersonal"


def test_bucket_page_has_portal_csp_and_no_cache(pg, monkeypatch):
    email = "bucket-csp@example.com"
    _seed(email)
    client = _client(monkeypatch, email)

    page = client.get("/bucket")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")

    csp = page.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    # The broader WASM-client policy must never leak onto this surface.
    assert "wasm-unsafe-eval" not in csp
    assert "worker-src" not in csp
    assert "cross-origin-embedder-policy" not in page.headers

    # The HTML itself is revalidated on every load so a deploy that renames
    # the hashed assets takes effect immediately.
    assert page.headers["cache-control"] == "no-cache"


def test_bucket_assets_are_content_hashed_and_immutable(pg, monkeypatch):
    """Derived from the page, not hardcoded: the filename embeds a content
    hash, so any edit to the asset renames it. What matters is that whatever
    the page asks for is served immutable and its name really is its digest."""
    email = "bucket-assets@example.com"
    _seed(email)
    client = _client(monkeypatch, email)
    page = client.get("/bucket")
    assert page.status_code == 200

    script = re.search(r'src="/portal-assets/(bucket\.([0-9a-f]{12})\.js)"', page.text)
    assert script, "bucket page must load a content-hashed script"
    style = re.search(r'href="/portal-assets/(bucket\.([0-9a-f]{12})\.css)"', page.text)
    assert style, "bucket page must load a content-hashed stylesheet"

    for name, digest, content_type in (
        (script.group(1), script.group(2), "application/javascript"),
        (style.group(1), style.group(2), "text/css"),
    ):
        asset = client.get(f"/portal-assets/{name}")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert asset.headers["content-type"].startswith(content_type)
        assert hashlib.sha256(asset.content).hexdigest().startswith(digest)


def test_bucket_page_carries_no_unhashed_or_inline_code(pg, monkeypatch):
    """Cache-busting query strings and inline code were the old page's whole
    delivery story; both are banned on the portal pipeline."""
    email = "bucket-legacy@example.com"
    _seed(email)
    client = _client(monkeypatch, email)
    page = client.get("/bucket")
    assert page.status_code == 200

    assert "/static/bucket.js" not in page.text
    assert "?v=" not in page.text
    # Every <script> must be an external hashed asset; no inline JS or CSS.
    for match in re.finditer(r"<script\b[^>]*>", page.text):
        assert re.search(r'src="/portal-assets/bucket\.[0-9a-f]{12}\.js"', match.group(0))
    assert "<style" not in page.text
    assert 'style="' not in page.text

    # The legacy un-hashed file is really gone, not merely unreferenced —
    # and the asset route refuses un-hashed names outright.
    assert not (_STATIC / "bucket.js").exists()
    assert client.get("/portal-assets/bucket.js").status_code == 404


