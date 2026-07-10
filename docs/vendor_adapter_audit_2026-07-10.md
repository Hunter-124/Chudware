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

Also **seeedstudio** — FIXED this pass after the classifier fix: `/catalogsearch/result/?q=`
was right but results are Algolia InstantSearch (`ais_hit_product_bar`→`ais_p_title`/`ais_p_sku`),
not JSON-LD; dropped false "Sign In" header markers. Live 10/10.

## GENUINELY WALLED — LEAVE (correct degradation)
- **parker** — HTTP 403 (login-wizard).
- **thomson** — HTTP 403 Cloudflare "Just a moment" interstitial (login-wizard).
- **orientalmotor** — HTTP 403 (login-wizard).
- **futureelectronics** — HTTP 403 on the real `/search?text=` endpoint (bot-walled).

## REMAINING — HARD TAIL (real endpoint found, but per-product results are NOT in the
## fetched HTML — client-side JS/Algolia/private-JSON APIs). Need browser-rendered
## extraction (execute the page's search XHR) or the site's JSON API, not a regex on
## the static shell. Real endpoints discovered (query param in parens):
- **cui** → `/SiteSearch/SearchByPartialMatch?partnumber=` (200, 1.6MB shell; results JS)
- **meanwell** → `/productSearch.aspx?pkeywords=` (200; page shows series *categories*, product models render client-side)
- **hdsc** → `/en/search?search=` (200; results JS-rendered)
- **datasheet4u** → `/share_search.php?sWord=` (200; datasheet result rows not in static HTML)
- **applerubber** → `/search.cfm?keyword=` (200; returns product *category* links, no per-product SKUs — a kind="category" case if wanted)
- **speedymetals** → `search.aspx?SearchTerm=` (200; aspx SPA, results via XHR)
- **acpcomposites** → `/?s=<q>&post_type=product` (200 "Search Results for:" but WooCommerce grid is AJAX)
- **dfrobot** (Nuxt SPA), **wch**, **batteryspace**, **marcorubber** (JS)
- **maxon** → Next.js `__NEXT_DATA__` blob (login-wizard; parse the JSON, mirror digikey_web.py)
- **fibreglast** → Shopify `/search?q=` (was a g-recaptcha false-challenge — recheck classify + parse Shopify cards)
- **acehardware** → `/search` (retail, g-recaptcha — recheck)
- **coremarkmetals** → intermittent 0-len fetch; retry
- **freecadlibrary** → GitHub repo tree search (special-case)

These are best finished with parallel subagents each executing the page's search XHR
(or hitting the discovered JSON API) and reading the rendered DOM. Cross-check every
fix against `[[vendor-adapter-parser-reveng]]`; never fake-fix a genuinely login-walled
site. NB: after the passive-CF challenge fix, several Magento/WooCommerce/BigCommerce
sites now fall through to a FALSE login-wall (header "Sign In"/"Log In"/name=password
widget) — drop those markers for public catalogs (done for globaloring, seeedstudio).
</content>
