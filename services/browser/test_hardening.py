"""Tests for services/browser captcha/2FA hardening.

No real Playwright, no real network, no real captcha: the detection classifier is
pure (faked page HTML), and the session wiring is exercised through each function's
``_sync_playwright`` injection seam (same pattern as test_browser_session.py). The
point is to prove the HARD RULE mechanically — a detected challenge pauses the
headed login and aborts the headless fetch, and we never automate around it.
"""
from __future__ import annotations

import importlib

import pytest

from services.browser import challenge


# --- classifier (pure) ----------------------------------------------------------

@pytest.mark.parametrize("html", [
    '<div class="g-recaptcha" data-sitekey="abc"></div>',
    '<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>',
    '<div class="h-captcha"></div>',
    '<div class="cf-turnstile"></div>',
    "<title>Just a moment...</title>",
    "<p>Checking your browser before accessing the site.</p>",
    "<title>Robot Check</title><p>Enter the characters you see below</p>",
    "<h1>Verify you are human</h1>",
    "<p>Please complete the security check to continue.</p>",
])
def test_classifier_flags_captcha(html):
    assert challenge.classify_challenge(html) == "captcha"
    assert challenge.is_challenge(html) is True


def test_classifier_flags_captcha_from_url_alone():
    # Amazon redirects to /errors/validateCaptcha — the URL itself is the signal.
    assert challenge.classify_challenge("<html>ok</html>",
                                        "https://www.amazon.com/errors/validateCaptcha") == "captcha"


@pytest.mark.parametrize("html", [
    "<h1>Two-factor authentication</h1><p>Enter the code we sent to your phone.</p>",
    "<label>Authentication code</label>",
    "<p>Enter the 6-digit code from your authenticator app.</p>",
    "<h2>2-Step Verification</h2>",
    "<p>We sent a verification code to your email.</p>",
])
def test_classifier_flags_2fa(html):
    assert challenge.classify_challenge(html) == "2fa"


@pytest.mark.parametrize("html", [
    "<html><body>A perfectly ordinary product page. Add to cart.</body></html>",
    "<html><head><title>GrabCAD - Flanged Bushing</title></head><body>Download STEP</body></html>",
    "<p>Sign in with your email and password to continue.</p>",  # a login form is NOT a captcha/2FA
    # Passive reCAPTCHA v3 badge on an ordinary page (TraceParts et al.) — the
    # library/badge is NOT a challenge (live-probe 2026-07-07 false positive).
    '<script src="https://www.google.com/recaptcha/api.js?render=6LcKey"></script>'
    '<div class="grecaptcha-badge"></div>'
    '<iframe src="https://www.google.com/recaptcha/api2/anchor?ar=1&size=invisible"></iframe>'
    "<body>608 bearing — 40 results. Download STEP AP214.</body>",
    "",
    None,
])
def test_classifier_passes_ordinary_pages(html):
    assert challenge.classify_challenge(html) is None
    assert challenge.is_challenge(html) is False


def test_classifier_ignores_passive_widgets_on_auth_login_form():
    html = (
        '<html><body><h1>Sign In to Your Account</h1>'
        '<input id="login-email" type="email">'
        '<input name="password" type="password">'
        '<div class="cf-turnstile"></div>'
        '<div class="g-recaptcha"></div>'
        '<script>challenge-platform</script></body></html>'
    )
    assert challenge.classify_challenge(
        html, "https://www.acehardware.com/user/login") is None


def test_classifier_still_flags_cf_interstitial_on_auth_url():
    html = (
        '<title>Just a moment...</title>'
        '<div class="cf-turnstile"></div>'
        '<p>Checking your browser before accessing the site.</p>'
    )
    assert challenge.classify_challenge(
        html, "https://www.acehardware.com/user/login") == "captcha"


def test_captcha_wins_when_page_has_both():
    html = '<div class="g-recaptcha"></div><p>two-factor authentication</p>'
    assert challenge.classify_challenge(html) == "captcha"


def test_classifier_ignores_invisible_recaptcha_on_magento_catalog():
    # Magento templates an invisible reCAPTCHA login-popup widget into every page's
    # header, so it rides along on ordinary catalog/product pages (anaheimautomation
    # live-probe 2026-07-10). The invisible badge + no bframe popup is not a
    # challenge, even on a non-auth catalog URL carrying real product content.
    html = (
        '<html><body><div class="products-grid">'
        '<a class="product-item-link" href="/23y104s-lw8.html">23Y104S-LW8</a></div>'
        '<div id="recaptcha-popup-login-wrapper"><div class="g-recaptcha" '
        'id="recaptcha-popup-login"><div class="grecaptcha-badge" data-style="inline">'
        '<iframe src="https://www.google.com/recaptcha/api2/anchor?size=invisible">'
        '</iframe></div></div></div></body></html>'
    )
    assert challenge.classify_challenge(
        html, "https://www.anaheimautomation.com/catalogsearch/result/?q=stepper") is None


def test_classifier_ignores_passive_cf_challenge_platform_on_storefront():
    # Cloudflare injects the passive `challenge-platform` script into ordinary
    # BigCommerce/WooCommerce storefront pages (surpluscenter, baileyhydraulics
    # live-probe 2026-07-10) with no interstitial shown. That script alone, on a
    # 200-OK page full of real product cards, is library presence — not a wall.
    html = (
        '<html><body><ul class="productGrid">'
        '<li class="product"><article class="card"><a href="/gear-pump/">Gear Pump</a>'
        '</article></li></ul>'
        '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/jsch/v1"></script>'
        '</body></html>'
    )
    assert challenge.classify_challenge(
        html, "https://www.surpluscenter.com/search.php?search_query=pump") is None


def test_classifier_still_flags_cf_challenge_platform_with_interstitial():
    # A REAL Cloudflare wall carries interstitial text alongside challenge-platform —
    # the passive-script escape above must not swallow it.
    html = ('<html><head><title>Just a moment...</title></head><body>'
            '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/jsch/v1"></script>'
            '</body></html>')
    assert challenge.classify_challenge(html, "https://www.thomsonlinear.com/") == "captcha"


def test_classifier_ignores_shopify_storefront_forms_captcha_tokens():
    # Shopify's storefront-forms captcha library templates the token FIELD names
    # `g-recaptcha-response` / `h-captcha-response` into ordinary pages (incl. its
    # search results — live-probe fibreglast 2026-07-10). Those contain the bare
    # marker substrings but are passive form protection, not an active widget.
    html = (
        '<html><body><h1>Search: 320 results found</h1>'
        '<a href="/products/saertex-830" class="full-unstyled-link">Saertex 830</a>'
        "<script>const f=['g-recaptcha-response','h-captcha-response'];</script>"
        '<script src="//cdn.shopify.com/…/storefront-forms-hcaptcha.v1.5.2.iife.js"></script>'
        '</body></html>'
    )
    assert challenge.classify_challenge(
        html, "https://www.fibreglast.com/search?q=fiberglass+cloth") is None


def test_classifier_still_flags_active_hcaptcha_widget():
    # A real hCaptcha widget (bare class, not the -response token) still aborts.
    assert challenge.classify_challenge('<div class="h-captcha" data-sitekey="x"></div>') == "captcha"


def test_classifier_ignores_magento_refresh_captcha_module():
    # Magento ships `Magento_PaypalCaptcha/js/model/skipRefreshCaptcha.js`, whose name
    # contains the substring "refres·hCaptcha" — it must NOT false-match hCaptcha.
    html = ('<html><body>Add to cart.'
            '<script src="/Magento_PaypalCaptcha/js/model/skipRefreshCaptcha.js"></script>'
            '</body></html>')
    assert challenge.classify_challenge(html) is None


def test_classifier_still_flags_active_recaptcha_bframe():
    # An actual reCAPTCHA challenge popup (bframe) still aborts, even alongside the
    # invisible badge — the invisible-badge escape must not swallow a real challenge.
    html = ('<div class="grecaptcha-badge"></div>'
            '<iframe src="https://www.google.com/recaptcha/api2/bframe?k=6Lc"></iframe>')
    assert challenge.classify_challenge(html) == "captcha"


def test_classifier_ignores_passive_2fa_label_on_lcsc_storefront():
    # LCSC's Nuxt search-results page (live-probe 2026-07-11, HTTP 200, 800KB of
    # real product HTML) ships its login/register i18n string blob into EVERY
    # page — including the bare form label "Verification Code"/"Verification code
    # required". That WEAK marker rides along on a large real content page and
    # must NOT be read as a 2FA wall (it degraded the whole LCSC adapter to
    # challenge_page). A genuine 2FA interstitial is a small, form-only page.
    blob = ('top_login_coupons:B}}}}}("","Required",null,"Email","Contact Us",'
            '"Verification Code","Type","Customer Service","Home",'
            '"Verification code required")')
    html = "<html><body>" + blob + ("<div>product row</div>" * 4000) + "</body></html>"
    assert len(html) >= 50_000
    assert challenge.classify_challenge(
        html, "https://www.lcsc.com/search?q=resistor") is None


def test_classifier_still_flags_genuine_2fa_interstitial():
    # A real 2FA challenge page (strong prompt) still classifies regardless of the
    # weak-marker size escape above — strong markers win unconditionally.
    html = ("<html><body><h1>Verify it's you</h1>"
            "<p>Enter the 6-digit code we sent to your phone.</p>"
            '<input name="code"></body></html>')
    assert challenge.classify_challenge(
        html, "https://vendor.example.com/2fa") == "2fa"


def test_classifier_still_flags_weak_2fa_label_on_small_page():
    # The size escape is narrow: a SMALL page whose only 2FA marker is the bare
    # "verification code" label is still a challenge (the code prompt IS the page).
    # Without this the weak-marker exclusion would be secretly too broad.
    html = ('<html><body><form><label>Verification code</label>'
            '<input name="otp"></form></body></html>')
    assert challenge.classify_challenge(
        html, "https://vendor.example.com/verify") == "2fa"


# --- fakes (page carries a .url, unlike test_browser_session's minimal one) ------

class _FakePage:
    def __init__(self, html, url, status=200):
        self._html, self.url, self._status = html, url, status
        self.solve_calls = 0  # would increment if anything tried to interact with a challenge

    def wait_for_event(self, name, timeout=None):
        return None  # user closes the login window immediately

    def goto(self, url, timeout=None):
        self.url = url
        return _FakeResponse(self._status)

    def content(self):
        return self._html


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakeContext:
    def __init__(self, html, url, status):
        self.pages, self.closed = [], False
        self._html, self._url, self._status = html, url, status

    def new_page(self):
        p = _FakePage(self._html, self._url, self._status)
        self.pages.append(p)
        return p

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, html, url, status):
        self.launched, self.last_context = [], None
        self._html, self._url, self._status = html, url, status

    def launch_persistent_context(self, user_data_dir, headless=True, **_kwargs):
        self.launched.append((user_data_dir, headless))
        self.last_context = _FakeContext(self._html, self._url, self._status)
        return self.last_context


class _FakePlaywright:
    def __init__(self, html, url, status):
        self.chromium = _FakeChromium(html, url, status)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _factory(html, url="https://example.com/", status=200):
    pw = _FakePlaywright(html, url, status)

    def _f():
        return pw
    _f.instance = pw
    return _f


class _FakeCef:
    """Stand-in for services.browser.cef_client's daemon client — browser_fetch /
    browser_download now drive CEF, not Playwright."""
    def __init__(self, html, url, status):
        self._html, self._url, self._status = html, url, status
        self.calls = []

    def fetch(self, url, *, settle_ms=1200, profile=""):
        self.calls.append(url)
        return {"ok": True, "status": self._status, "final_url": self._url, "html": self._html}

    def download(self, url, dest, *, profile=""):
        from pathlib import Path
        Path(dest).write_bytes(self._html.encode())
        return {"ok": True, "dest": dest}


def _cef(html, url="https://example.com/", status=200):
    return _FakeCef(html, url, status)


@pytest.fixture()
def sess(tmp_path, monkeypatch):
    """Reload services.browser.* under an isolated APPDATA base (mirrors test_browser_session)."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from services import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, ratelimit, session
    importlib.reload(profiles)
    importlib.reload(ratelimit)
    importlib.reload(session)
    yield profiles, session
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


# --- browser_fetch: HEADLESS skip-parse on a challenge --------------------------

def test_fetch_skips_captcha_page(sess):
    profiles, session = sess
    profiles.profile_dir("amazon").mkdir(parents=True)
    cef = _cef('<title>Robot Check</title><div class="g-recaptcha"></div>',
               url="https://www.amazon.com/dp/B000")
    result = session.browser_fetch("https://www.amazon.com/dp/B000", "amazon",
                                   _cef=cef, min_interval=0.0)
    assert result["ok"] is False
    # Detection is a data-quality signal: skip parsing, never ingest the markup.
    assert result["reason"] == "challenge_page"
    assert "content" not in result
    assert profiles.load_meta("amazon").get("last_used_ts") is None


def test_fetch_skips_2fa_page(sess):
    profiles, session = sess
    profiles.profile_dir("grabcad").mkdir(parents=True)
    cef = _cef("<h1>Two-factor authentication</h1><p>Enter the code we sent.</p>",
               url="https://grabcad.com/parts")
    result = session.browser_fetch("https://grabcad.com/parts", "grabcad",
                                   _cef=cef, min_interval=0.0)
    assert result["ok"] is False
    assert result["reason"] == "challenge_page"


def test_fetch_passes_ordinary_page(sess):
    profiles, session = sess
    profiles.profile_dir("grabcad").mkdir(parents=True)
    cef = _cef("<html><body>Flanged Bushing — download STEP</body></html>",
               url="https://grabcad.com/library/part-1")
    result = session.browser_fetch("https://grabcad.com/library/part-1", "grabcad",
                                   _cef=cef, min_interval=0.0)
    assert result["ok"] is True
    assert "Flanged Bushing" in result["content"]


# --- browser_login: HEADED detect + surface, never automate ---------------------

def test_login_detects_and_reports_captcha(sess, capsys):
    _profiles, session = sess
    fac = _factory('<div class="h-captcha"></div>', url="https://www.pcbway.com/login")
    result = session.browser_login("pcbway", timeout=5.0, _sync_playwright=fac)
    assert result["ok"] is True
    assert result["challenge_detected"] == "captcha"
    # A clear, user-facing instruction was surfaced; no bypass was attempted.
    out = capsys.readouterr().out
    assert "CAPTCHA" in out and "not attempt to solve" in out
    # The headed window was the only actor — the fake page recorded zero solve calls.
    page = fac.instance.chromium.last_context.pages[0]
    assert page.solve_calls == 0


def test_login_ordinary_page_has_no_challenge(sess):
    _profiles, session = sess
    fac = _factory("<html><body>Signed in as user</body></html>",
                   url="https://grabcad.com/dashboard")
    result = session.browser_login("grabcad", timeout=5.0, _sync_playwright=fac)
    assert result["ok"] is True
    assert result["challenge_detected"] is None


# --- stealth: consistent, honest fingerprint (no "look at me, a robot" tells) ----

def test_stealth_user_agent_is_desktop_chrome_no_headless():
    from services.browser import stealth
    ua = stealth.user_agent("149.0.7827.55")
    assert "Chrome/149.0.7827.55" in ua
    assert "Headless" not in ua        # the whole point
    assert "Linux x86_64" in ua        # OS stays honest


def test_stealth_launch_args_disable_automation_flag():
    from services.browser import stealth
    args = stealth.launch_args(headless=True)
    assert "--disable-blink-features=AutomationControlled" in args  # flips webdriver
    assert "--headless=new" in args
    assert "--headless=new" not in stealth.launch_args(headless=False)


def test_stealth_context_options_consistent_identity():
    from services.browser import stealth
    ua = stealth.user_agent("149.0.7827.55")
    opts = stealth.context_options(ua)
    assert opts["user_agent"] == ua
    assert opts["locale"] == "en-US"
    assert opts["timezone_id"] == "America/New_York"
    assert opts["extra_http_headers"]["Accept-Language"] == "en-US,en;q=0.9"


def test_stealth_cdp_override_scrubs_headless_from_client_hints():
    from services.browser import stealth
    ua = stealth.user_agent("149.0.7827.55")
    params = stealth.cdp_ua_override_params(ua, "149.0.7827.55")
    brands = params["userAgentMetadata"]["brands"]
    names = [b["brand"] for b in brands]
    assert "HeadlessChrome" not in names        # the Sec-CH-UA tell we remove
    assert "Google Chrome" in names and "Chromium" in names
    # UA header and client-hint versions must agree (consistency > mimicry).
    assert all(b["version"] == "149" for b in brands if b["brand"] != "Not)A;Brand")
    assert params["userAgentMetadata"]["platform"] == "Linux"
    # Unweighted — CDP appends the q itself; passing q here double-appends it.
    assert params["acceptLanguage"] == "en-US,en"


def test_stealth_version_detection_degrades_without_real_browser():
    from services.browser import stealth
    stealth._VERSION_CACHE.pop("full", None)

    class _NoLaunch:
        chromium = object()  # no .launch → detection must fall back, not raise

    assert stealth.chromium_version_sync(_NoLaunch()) == stealth.FALLBACK_FULL_VERSION
    stealth._VERSION_CACHE.pop("full", None)


def test_stealth_harden_helpers_never_raise_on_bare_objects():
    from services.browser import stealth
    # A context/page without add_init_script/new_cdp_session must degrade silently
    # (real browsers get hardened; fakes/old playwright just skip it).
    stealth.harden_context_sync(object())
    stealth.harden_page_sync(object(), object(), "ua", "149.0.7827.55")


def test_stealth_init_script_covers_the_known_tells():
    from services.browser import stealth
    js = stealth.INIT_SCRIPT
    for tell in ("webdriver", "chrome", "plugins", "languages"):
        assert tell in js
    # WebGL vendor/renderer spoof masks the --use-angle=swiftshader software-GL
    # tell (the top residual sannysoft flag). Kept in lockstep with
    # cef_stealth.cpp; the hook reads native via toString so it isn't itself a tell.
    assert "37445" in js and "37446" in js  # UNMASKED_VENDOR/RENDERER_WEBGL
    assert "getParameter" in js


def test_stealth_headed_init_script_skips_webgl_hook():
    # reCAPTCHA treats a hooked getParameter as a bot tell on a real GPU
    # (Anaheim Magento). Headed login must leave WebGL native.
    from services.browser import stealth
    headed = stealth.INIT_SCRIPT_HEADED
    for tell in ("webdriver", "chrome", "plugins", "languages"):
        assert tell in headed
    assert "37445" not in headed and "getParameter" not in headed
    assert "37445" in stealth.INIT_SCRIPT  # headless still spoofs
