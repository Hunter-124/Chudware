# Vendor adapter live-search audit — 2026-07-10

Live-verified every adapter in `services/browser/adapters/` by running its
`search()` end-to-end headless through the CEF daemon (`services.browser.session.browser_fetch`),
with a category-appropriate query. Baseline before: 250 passed / 79 skipped.

Method: `mcp-venv` python + repo on `sys.path`; for each suspect, raw-fetch via
`services.browser.cef_client.client().fetch(url)` to read the true status + HTML,
find the site's real search endpoint + product-card markup, fix `SEARCH_URL_TMPL`
+ the result regexes, add a verbatim-fixture parser test. NEVER `AI_CAD_CEF_GPU=1`
(SIGTRAP). The CEF daemon is slow to spawn under a concurrent GUI — fetch
sequentially with retry.

## Already parsing headless — LEFT ALONE (verified working live)
misumi(30), digikey(22), amazon(22), grabcad(24), fastenal(12) — prior work.
Newly confirmed working this pass (were login-wizard-listed but parse fine headless):
**igus(16), thk(10), cadenas(19), componentsearchengine(25)**, anaheimautomation(12).

## FIXED + committed + pushed this pass (each live-verified, fixture test added)
| adapter | platform | old URL | real endpoint | live hits |
|---|---|---|---|---|
| rockwest | Salesforce Commerce Cloud (Demandware) | /search?q= | /on/demandware.store/Sites-RWC-Site/default/Search-Show?q= | 60 |
| surpluscenter | BigCommerce | /search?q= | /search.php?search_query= | 24 (sku+price) |
| baileyhydraulics | BigCommerce | /search?q= | /search.php?search_query= | 12 (sku+price) |
| globaloring | WooCommerce | /search?q= | /?s=<q>&post_type=product | 29 |
| seeedstudio | Magento + Algolia InstantSearch | (url ok) | ais_p_title/ais_p_sku parse | 10 (sku) |
| cui | custom SiteSearch | /search?q= | /SiteSearch/SearchByPartialMatch?partnumber= | 12 (pn+cat) |
| datasheet4u | custom datasheet DB | /search?q= | /share_search.php?sWord= | 10 (mfr+desc) |
| fibreglast | Shopify | (url ok) | full-unstyled-link card parse | 22 |

**8 adapters fixed.** Plus **two cross-cutting classifier fixes** (`challenge.py`):
1. A lone passive Cloudflare `challenge-platform` script (no interstitial) is no
   longer misread as a captcha wall (rode BigCommerce/WooCommerce storefronts).
2. Shopify's storefront-forms `g-recaptcha-response`/`h-captcha-response` token field
   names no longer false-flag as an active widget (passive form protection).
Real CF/reCAPTCHA/hCaptcha walls still classify. These unblocked surpluscenter,
baileyhydraulics, globaloring, fibreglast. Also, once challenge false-positives were
cleared, several Magento/Woo sites fell to a FALSE login-wall (header Sign In/Log In/
name=password widget) — those markers were dropped for the public catalogs
(globaloring, seeedstudio, cui, datasheet4u, fibreglast). Suite: 273 passed / 79 skipped.

## GENUINELY WALLED — LEAVE (correct degradation, confirmed real this pass)
- **parker** — HTTP 403 (login-wizard).
- **thomson** — HTTP 403 Cloudflare "Just a moment" interstitial (login-wizard).
- **orientalmotor** — HTTP 403 (login-wizard).
- **futureelectronics** — HTTP 403 on the real `/search?text=` endpoint (bot-walled).
- **acehardware** — HTTP 403 real Cloudflare Turnstile interstitial ("Just a moment", cf_chl_) on `/search?query=`.
- **coremarkmetals** — HTTP 200 but a real reCAPTCHA `api2/bframe` challenge popup on `/search?q=`, zero products.

## REMAINING — HARD TAIL (real endpoint found, but per-product results are NOT in the
## fetched HTML — client-side JS/Nuxt/Next/AJAX). Need browser-rendered extraction
## (execute the page's search XHR) or the site's JSON API, not a regex on the static
## shell. Real endpoints discovered (query param in parens):
- **meanwell** → `/productSearch.aspx?pkeywords=` (200; page shows series *categories*, product models render client-side)
- **hdsc** → `/en/search?search=` (200; results JS-rendered)
- **applerubber** → `/search.cfm?keyword=` (200; returns product *category* links only, no per-product SKUs — a kind="category" case if wanted)
- **speedymetals** → `search.aspx?SearchTerm=` (200; aspx SPA, results via XHR)
- **acpcomposites** → `/?s=<q>&post_type=product` (200 "Search Results for:" but WooCommerce grid is AJAX)
- **dfrobot** (Nuxt SPA), **wch**, **batteryspace**, **marcorubber** (JS)
- **maxon** → Next.js `__NEXT_DATA__` blob (login-wizard; parse the JSON, mirror digikey_web.py)
- **freecadlibrary** → GitHub repo tree search (special-case)

These are best finished with parallel subagents each executing the page's search XHR
(or hitting the discovered JSON API) and reading the rendered DOM. Cross-check every
fix against `[[vendor-adapter-parser-reveng]]`; never fake-fix a genuinely walled site.
NB: after the challenge false-positive fixes, Magento/Woo/Shopify/BigCommerce sites can
fall through to a FALSE login-wall (header "Sign In"/"Log In"/name=password widget) —
drop those markers for public catalogs (done for the 8 fixed above).
</content>
