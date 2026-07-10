"""services/browser/challenge.py — captcha / 2FA / interstitial detection (T-X6).

Detection is a **data-quality signal**, not a stop sign. The browser fetches the
user's OWN authenticated sessions with a consistent, non-automation fingerprint
(services/browser/stealth.py + the CEF stealth layer), which clears the vast
majority of *false* bot flags. When a page nonetheless resolves to a challenge or
interstitial rather than real content, the classifiers below let the caller
recognise that and **skip parsing it** — so a challenge/interstitial page is never
ingested into the parts store as if it were product data. That is the whole job:
tell "this is a challenge page" apart from "this is a catalog page".

- ``browser_login`` is HEADED — the user is at the keyboard and completes any
  interactive step themselves in the window.
- ``browser_fetch`` is HEADLESS — a detected challenge means "no usable content
  here"; the fetch reports that distinctly instead of returning the challenge
  markup as a successful result.

Detection favours recall and matches only distinctive service names / prompt
phrases — never bare words like "code" or "login" that appear on ordinary pages —
so it doesn't misclassify a normal catalog page as a challenge.

Pure functions over page HTML + URL — no browser import, no I/O — so they unit
-test against faked page content with no browser.
"""
from __future__ import annotations

# Distinctive captcha service markers / challenge-page phrases (substring,
# case-insensitive). Kept specific enough not to fire on ordinary pages.
#
# IMPORTANT nuance (live-probe 2026-07-07): many ordinary pages ship the PASSIVE
# reCAPTCHA v3 badge (api.js?render=…, grecaptcha-badge, an invisible anchor
# iframe) with no challenge presented — e.g. every TraceParts page.  Library
# presence is NOT a challenge; we match only ACTIVE widget markup (the v2
# checkbox div, the bframe challenge popup) and challenge-page phrases, plus a
# size=invisible exclusion for the anchor iframe below.  This keeps the hard
# rule intact — anything that IS a challenge still aborts and hands control to
# the human — while no longer aborting successful fetches of normal pages.
_CAPTCHA_MARKERS: tuple[str, ...] = (
    # Google reCAPTCHA v2 explicit widget / challenge popup, hCaptcha, Turnstile.
    # hCaptcha must be matched by its API domain / hyphenated widget class, NOT a
    # bare "hcaptcha" substring — that false-matches Magento's stock
    # `Magento_PaypalCaptcha/.../skipRefreshCaptcha.js` ("refres·hCaptcha").
    "g-recaptcha", "recaptcha/api2/bframe",
    "hcaptcha.com", "h-captcha",
    "cf-turnstile",
    # Cloudflare interstitial / challenge pages
    "cf-challenge", "cf_chl_", "challenge-platform",
    "checking your browser before", "just a moment...",
    "attention required! | cloudflare",
    # Amazon "Robot Check"
    "enter the characters you see", "type the characters you see",
    "/errors/validatecaptcha", "robot check",
    # Generic human-verification prompts
    "i'm not a robot", "i am not a robot",
    "verify you are human", "verify you're human", "are you a human",
    "please complete the security check",
    # PerimeterX / Distil bot walls
    "px-captcha", "perimeterx", "distil_r_captcha",
)

# Distinctive 2FA / MFA prompt phrases.
_TWO_FACTOR_MARKERS: tuple[str, ...] = (
    "two-factor", "two factor authentication",
    "2-step verification", "two-step verification",
    "multi-factor authentication",
    "authentication code", "verification code",
    "one-time password", "one time passcode",
    "enter the code we sent", "enter the 6-digit", "6-digit code",
    "authenticator app", "security code we sent",
    "confirm your identity to continue",
)

# iframe src fragments that indicate an embedded captcha widget.  "recaptcha"
# is deliberately absent: the v3 badge injects an invisible anchor iframe on
# ordinary pages — the anchor is handled separately with a size=invisible check.
_CAPTCHA_IFRAME_SRC: tuple[str, ...] = ("hcaptcha", "turnstile", "/captcha")

# URL fragments that themselves signal a captcha challenge redirect.
_CAPTCHA_URL_MARKERS: tuple[str, ...] = ("validatecaptcha", "/captcha", "/errors/validate")

# Cloudflare loads challenge-platform on many retail login pages without showing
# an active interstitial — treat it as passive unless interstitial text appears.
_PASSIVE_CF_MARKERS: frozenset[str] = frozenset({"challenge-platform"})
_INTERSTITIAL_CF_MARKERS: tuple[str, ...] = (
    "checking your browser before", "just a moment...",
    "attention required! | cloudflare", "cf-challenge", "cf_chl_",
)
# Widget/library markup embedded in a normal login form — not a blocking interstitial.
_PASSIVE_WIDGET_MARKERS: frozenset[str] = frozenset({
    "g-recaptcha", "recaptcha/api2/bframe", "cf-turnstile", "challenge-platform",
})
_AUTH_URL_FRAGMENTS: tuple[str, ...] = (
    "/sign-in", "/signin", "/sign-up", "/signup", "/login", "/register",
    "/account/login", "/user/login", "/auth/",
)
_LOGIN_FORM_MARKERS: tuple[str, ...] = (
    'id="login-email"', 'name="password"', 'type="password"',
    "sign in to your account", "log in to your account",
)


def _is_auth_url(url: str) -> bool:
    u = (url or "").lower()
    return any(frag in u for frag in _AUTH_URL_FRAGMENTS)


def _login_form_on_page(hay: str) -> bool:
    return any(m in hay for m in _LOGIN_FORM_MARKERS)


def classify_challenge(html: str | None, url: str = "") -> str | None:
    """Return ``"captcha"`` or ``"2fa"`` if the page looks like a challenge, else ``None``.

    Conservative by design — matches known service markers / distinctive prompt
    phrases only. Captcha is checked before 2FA so a page carrying both classifies
    as the (more automation-sensitive) captcha."""
    u = (url or "").lower()
    url_markers = [m for m in _CAPTCHA_URL_MARKERS if m in u]
    if url_markers:
        return "captcha"

    if not html:
        return None
    hay = html.lower()

    hit_markers = [m for m in _CAPTCHA_MARKERS if m in hay]
    # SnapEDA home can include recaptcha widget markup after a successful login
    # (observed live) without presenting an active challenge. If we only see
    # recaptcha library/widget markers on /home, don't classify as challenge.
    if "snapeda.com/home" in u:
        non_recaptcha_hits = [
            m for m in hit_markers if m not in ("g-recaptcha", "recaptcha/api2/bframe")
        ]
        if not non_recaptcha_hits:
            return None
    if hit_markers:
        has_interstitial = any(m in hay for m in _INTERSTITIAL_CF_MARKERS)
        substantive = [m for m in hit_markers if m not in _PASSIVE_WIDGET_MARKERS]
        passive_widgets = [m for m in hit_markers if m in _PASSIVE_WIDGET_MARKERS]
        # A purely PASSIVE, INVISIBLE reCAPTCHA badge (the v3-style grecaptcha-badge,
        # or an anchor iframe marked size=invisible) with NO bframe challenge popup is
        # library presence, not a challenge. Magento (and others) template an invisible
        # login-popup widget into every page's header, so it rides along on ordinary
        # catalog/product pages too — those must not be misread as a captcha wall
        # regardless of URL. An active challenge always brings a bframe popup or a
        # visible (non-invisible) anchor checkbox, neither of which this branch matches.
        recaptcha_only = all(m in ("g-recaptcha", "recaptcha/api2/bframe") for m in hit_markers)
        invisible_badge = ("grecaptcha-badge" in hay
                           or ("recaptcha/api2/anchor" in hay and "size=invisible" in hay))
        if (recaptcha_only and invisible_badge and "recaptcha/api2/bframe" not in hay
                and not has_interstitial):
            return None
        # Login forms often embed Turnstile/reCAPTCHA widgets — not a full-page CF wall.
        if (_is_auth_url(u) and _login_form_on_page(hay) and not has_interstitial
                and not substantive and passive_widgets):
            return None
        if (not substantive and passive_widgets and not has_interstitial):
            # Passive CF script on a login page (Ace et al.) — not an active wall.
            if "acehardware.com" in u and ("/login" in u or "/user/login" in u):
                return None
        return "captcha"
    iframe_hits = [s for s in _CAPTCHA_IFRAME_SRC if s in hay]
    if "<iframe" in hay and iframe_hits:
        return "captcha"
    # reCAPTCHA anchor iframe: a visible checkbox is a challenge; the v3 badge's
    # size=invisible anchor on an ordinary page is not.
    if "recaptcha/api2/anchor" in hay and "size=invisible" not in hay:
        return "captcha"
    if any(m in hay for m in _TWO_FACTOR_MARKERS):
        return "2fa"
    return None


def is_challenge(html: str | None, url: str = "") -> bool:
    """True when the page appears to present a captcha or 2FA challenge."""
    return classify_challenge(html, url) is not None
