"""services/browser/stealth.py — anti-detection hardening for the untrusted browser.

Goal: our headless Chromium (login wizard + scraping) should look like an
ordinary desktop Chrome so vendors don't flag, rate-limit, or block the *user's*
account. We do NOT bypass captchas/Cloudflare — see :mod:`.challenge`; this
module only removes the gratuitous "I am automation" tells that a normal browser
never emits.

The governing principle is **consistency, not mimicry**. An inconsistent
fingerprint (UA header says Chrome but ``navigator.webdriver`` is true, or
``Sec-CH-UA`` still says ``HeadlessChrome``) reads to a bot-scorer as *active
evasion* — worse than a plain automated browser. So every value we present is
derived from the REAL bundled Chromium at its REAL version, with only the
"Headless" label stripped; the OS stays honest (Linux), and we never invent a
GPU/plugin set that would disagree with the rest of the client state.

Applied identically to both browser surfaces so they share one fingerprint:
  * scraping — :mod:`.session` (browser_fetch / browser_login / browser_download)
  * interactive login wizard — :mod:`.embed`

Empirically verified on Chromium 149 (2026-07-07): with the launch flag +
CDP ``Network.setUserAgentOverride`` below, a header reflector sees
``User-Agent: …Chrome/149…`` and ``Sec-CH-UA: "Chromium";v="149", "Google
Chrome";v="149", "Not)A;Brand";v="24"`` — no "Headless" in either, versions
agree, platform "Linux".
"""
from __future__ import annotations

from typing import Any

# Sane fallback used only when live version detection fails (offline, or a test
# fake without a real ``launch()``). Kept current-ish; the live value overrides
# it whenever a real Chromium is present.
FALLBACK_FULL_VERSION = "149.0.7827.55"

# Desktop-Linux Chrome UA. ``{ver}`` is the full Chromium version — matching the
# real binary keeps the UA header consistent with the native Sec-CH-UA brands.
_UA_TEMPLATE = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/{ver} Safari/537.36")

_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
# CDP re-weights this itself — pass it WITHOUT explicit q-values or it emits a
# malformed ``en-US,en;q=0.9;q=0.9`` header. ``en-US,en`` → ``en-US,en;q=0.9``.
_CDP_ACCEPT_LANGUAGE = "en-US,en"

# Process-lifetime cache: detecting the version costs one throwaway launch, so we
# do it at most once per worker.
_VERSION_CACHE: dict[str, str] = {}

# Client-side patches for the tells that survive the launch flag + UA override
# (window.chrome absent, empty plugin list, single-entry languages). Each patch
# is isolated in try/catch so a hardened property on a future Chromium can never
# break page init. webdriver is handled by the launch flag; we only re-assert it
# defensively. We deliberately do NOT spoof WebGL/canvas — the real values are
# honest and inventing them risks a fresh inconsistency.
INIT_SCRIPT = r"""
(() => {
  const patch = (fn) => { try { fn(); } catch (e) { /* keep page init alive */ } };

  patch(() => { if (!window.chrome) window.chrome = { runtime: {} }; });

  patch(() => {
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  });

  patch(() => {
    if (navigator.plugins && navigator.plugins.length) return;
    const mk = (name) => ({ name, description: 'Portable Document Format',
                            filename: 'internal-pdf-viewer', length: 1 });
    const plugins = [mk('PDF Viewer'), mk('Chrome PDF Viewer'), mk('Chromium PDF Viewer')];
    Object.defineProperty(navigator, 'plugins', { get: () => plugins });
    Object.defineProperty(navigator, 'mimeTypes',
                          { get: () => [{ type: 'application/pdf', suffixes: 'pdf' }] });
  });

  patch(() => {
    if (navigator.webdriver) Object.defineProperty(navigator, 'webdriver', { get: () => false });
  });
  // NB: we deliberately do NOT spoof WebGL vendor/renderer. A getParameter
  // override is itself detectable (its .toString() no longer reads "[native
  // code]"), and claiming an Intel GPU while the canvas actually rasterises via
  // SwiftShader is a canvas-readback inconsistency that scores WORSE than the
  // honest software-GL string. Consistency over mimicry (see module header).
})();
"""


def _major(full_version: str) -> str:
    return (full_version or FALLBACK_FULL_VERSION).split(".")[0]


def user_agent(full_version: str) -> str:
    """Desktop Chrome UA string for ``full_version`` (Headless label stripped)."""
    return _UA_TEMPLATE.format(ver=full_version or FALLBACK_FULL_VERSION)


def launch_args(*, headless: bool) -> list[str]:
    """Chromium launch args that remove automation tells. Safe/minimal — nothing
    that would itself look unusual to a fingerprinter."""
    args = [
        "--disable-blink-features=AutomationControlled",  # flips navigator.webdriver
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
    ]
    if headless:
        args.append("--headless=new")  # far less detectable than legacy headless
    return args


def context_options(ua: str) -> dict[str, Any]:
    """kwargs for ``launch_persistent_context`` — a consistent, honest identity."""
    return {
        "user_agent": ua,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "extra_http_headers": {"Accept-Language": _ACCEPT_LANGUAGE},
    }


def _ua_metadata(full_version: str) -> dict[str, Any]:
    major = _major(full_version)
    # Mirror what real desktop Chrome sends, swapping the "HeadlessChrome" brand
    # (which the native client-hints emit) for "Google Chrome".
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not)A;Brand", "version": "24"},
    ]
    full_list = [
        {"brand": "Chromium", "version": full_version},
        {"brand": "Google Chrome", "version": full_version},
        {"brand": "Not)A;Brand", "version": "24.0.0.0"},
    ]
    return {
        "brands": brands,
        "fullVersion": full_version,
        "fullVersionList": full_list,
        "platform": "Linux",
        "platformVersion": "",
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }


def cdp_ua_override_params(ua: str, full_version: str) -> dict[str, Any]:
    """Params for CDP ``Network.setUserAgentOverride`` — sets the UA header AND
    the Sec-CH-UA client hints together so they can't drift apart. This is the
    piece that scrubs "HeadlessChrome" from Sec-CH-UA (the context ``user_agent``
    kwarg only touches the UA header)."""
    return {
        "userAgent": ua,
        "acceptLanguage": _CDP_ACCEPT_LANGUAGE,  # unweighted; CDP adds q itself
        "platform": "Linux x86_64",
        "userAgentMetadata": _ua_metadata(full_version),
    }


# ---- version detection (guarded — never raises, cached per process) -----------

def chromium_version_sync(pw: Any) -> str:
    """Full version of the bundled Chromium via a throwaway sync launch (cached).
    Degrades to :data:`FALLBACK_FULL_VERSION` for a fake ``pw`` or offline box."""
    if "full" in _VERSION_CACHE:
        return _VERSION_CACHE["full"]
    ver = FALLBACK_FULL_VERSION
    try:
        browser = pw.chromium.launch(headless=True)
        try:
            ver = browser.version or FALLBACK_FULL_VERSION
        finally:
            browser.close()
    except Exception:  # noqa: BLE001 — fake pw / no browser / offline → fallback
        ver = FALLBACK_FULL_VERSION
    _VERSION_CACHE["full"] = ver
    return ver


async def chromium_version_async(pw: Any) -> str:
    """Async counterpart of :func:`chromium_version_sync`."""
    if "full" in _VERSION_CACHE:
        return _VERSION_CACHE["full"]
    ver = FALLBACK_FULL_VERSION
    try:
        browser = await pw.chromium.launch(headless=True)
        try:
            ver = browser.version or FALLBACK_FULL_VERSION
        finally:
            await browser.close()
    except Exception:  # noqa: BLE001
        ver = FALLBACK_FULL_VERSION
    _VERSION_CACHE["full"] = ver
    return ver


# ---- applying the hardening to a live context/page (guarded) ------------------

def harden_context_sync(context: Any) -> None:
    """Inject the client-side patch script into every page of ``context``.
    No-op (swallowed) if the context can't take init scripts (e.g. a test fake)."""
    try:
        context.add_init_script(INIT_SCRIPT)
    except Exception:  # noqa: BLE001 — degrade gracefully; UA/args still applied
        pass


def harden_page_sync(context: Any, page: Any, ua: str, full_version: str) -> None:
    """Apply the CDP UA/client-hint override to ``page`` (fixes Sec-CH-UA).
    Swallowed if CDP is unavailable — the context ``user_agent`` still covers the
    UA header, so we degrade to 'UA correct, Sec-CH-UA native' rather than crash."""
    try:
        cdp = context.new_cdp_session(page)
        cdp.send("Network.setUserAgentOverride", cdp_ua_override_params(ua, full_version))
    except Exception:  # noqa: BLE001
        pass


async def harden_context_async(context: Any) -> None:
    """Async counterpart of :func:`harden_context_sync`."""
    try:
        await context.add_init_script(INIT_SCRIPT)
    except Exception:  # noqa: BLE001
        pass


async def apply_cdp_ua_async(cdp: Any, ua: str, full_version: str) -> None:
    """Send the UA/client-hint override on an existing async CDP session.
    embed.py already opens a CDP session for its screencast — reuse it."""
    try:
        await cdp.send("Network.setUserAgentOverride", cdp_ua_override_params(ua, full_version))
    except Exception:  # noqa: BLE001
        pass
