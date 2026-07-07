"""services/browser/challenge.py — conservative captcha / 2FA detection (T-X6).

The hard rule (contracts/research_tools.md + the oss-first safety posture): this
project NEVER attempts to detect-and-solve, bypass, or evade a captcha or a 2FA
challenge. Detection exists only so we can HAND CONTROL BACK to the human:

- ``browser_login`` is HEADED — the user is already at the keyboard, so we surface
  a clear "complete this yourself" message and keep the window open; we never
  automate a single click of it.
- ``browser_fetch`` is HEADLESS — there is no human in the loop, so a detected
  challenge ABORTS the fetch and returns a "needs manual re-login" result rather
  than returning the challenge page as if it were real content (or retrying, which
  reads as an evasion attempt).

Detection is deliberately CONSERVATIVE: a false positive (pausing / aborting when
it was not strictly necessary) is cheap and safe; a false negative (silently
automating past a real challenge) is exactly the safety violation we must avoid.
So the markers below favour recall, and the classifier matches only distinctive
service names / prompt phrases — never bare words like "code" or "login" that
appear on ordinary pages.

Pure functions over page HTML + URL — no Playwright import, no I/O — so they unit
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
    # Google reCAPTCHA v2 explicit widget / challenge popup, hCaptcha, Turnstile
    "g-recaptcha", "recaptcha/api2/bframe",
    "hcaptcha", "h-captcha",
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


def classify_challenge(html: str | None, url: str = "") -> str | None:
    """Return ``"captcha"`` or ``"2fa"`` if the page looks like a challenge, else ``None``.

    Conservative by design — matches known service markers / distinctive prompt
    phrases only. Captcha is checked before 2FA so a page carrying both classifies
    as the (more automation-sensitive) captcha."""
    u = (url or "").lower()
    if any(m in u for m in _CAPTCHA_URL_MARKERS):
        return "captcha"

    if not html:
        return None
    hay = html.lower()

    if any(m in hay for m in _CAPTCHA_MARKERS):
        return "captcha"
    if "<iframe" in hay and any(s in hay for s in _CAPTCHA_IFRAME_SRC):
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
