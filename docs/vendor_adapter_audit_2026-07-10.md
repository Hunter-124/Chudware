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

Plus a **cross-cutting classifier fix** (`challenge.py`): a lone passive Cloudflare
`challenge-platform` script (no interstitial) is no longer misread as a captcha wall
— it rode on ordinary BigCommerce/WooCommerce storefronts and forced parse-to-zero.
Real CF walls (interstitial text) still classify. This unblocked surpluscenter,
baileyhydraulics, globaloring.

## GENUINELY WALLED — LEAVE (correct degradation; all are login-wizard sites)
- **parker** — HTTP 403.
- **thomson** — HTTP 403 Cloudflare "Just a moment" interstitial (real).
- **orientalmotor** — HTTP 403.

## REMAINING SUSPECTS — still to fix (real endpoint or parse mode noted)
Wrong-endpoint / needs per-site form-param discovery (raw fetch shows the search form action):
- **applerubber** → ColdFusion `/search.cfm` (WordPress front) — get the form's query param
- **cui** → `/SiteSearch/SearchByPartialMatch`
- **meanwell** → `productSearch.aspx`
- **futureelectronics** → `/search`
- **hdsc** → `/en/search`
- **datasheet4u** → `/share_search.php`
- **fibreglast** → Shopify `/search?q=` (was a passive g-recaptcha false-challenge; recheck classify now)
- **acehardware** → `/search` (retail, g-recaptcha — check real vs passive)
- **seeedstudio** → Magento `/catalogsearch/result/?q=` (was challenge — recheck)
- **maxon** → Next.js `__NEXT_DATA__` JSON (parse the blob, mirror digikey_web.py)
- **freecadlibrary** → GitHub repo tree search (special-case)
- **coremarkmetals** → intermittent 0-len fetch; retry

JS-rendered SPA — results injected client-side, not in fetched HTML (harder):
- **speedymetals** (aspx SPA), **acpcomposites** (WooCommerce AJAX grid),
  **dfrobot** (Nuxt), **wch**, **batteryspace**, **marcorubber**.

Cross-check every fix against the walled-vs-parsable memory note
`[[vendor-adapter-parser-reveng]]`; never fake-fix a genuinely login-walled site.
</content>
