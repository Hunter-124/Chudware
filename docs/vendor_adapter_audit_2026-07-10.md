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

## HARD TAIL — FINISHED (2026-07-10, 3 parallel subagents; each fix live-verified,
## fixture test added, committed + pushed separately). The daemon returns the live
## post-JS DOM, so the "results render client-side" blockers were mostly the wrong
## endpoint + speculative CSS regexes, not a true JS wall.
| adapter | real endpoint | parse mode | live hits |
|---|---|---|---|
| maxon | `/en-us/search?q=` | Next.js `__NEXT_DATA__` `productSearchData.products[].record` | 10 (PN+USD price) |
| applerubber | `/search.cfm?keyword=` | ColdFusion `<ol id=SearchResults>` rows → `kind="page"` | 10 |
| freecadlibrary | GitHub git-tree API `/git/trees/master?recursive=1` | walk blobs, filter by basename + CAD ext, raw download URLs | 40 (capped) |
| batteryspace | `/search.aspx?find=` | AmeriCommerce rendered product cards | 30 (title+url+price+SKU) |
| wch | `search.wch-ic.com/?q=&t=all` | Vue app, `/downloads/` doc anchors → `kind="document"` | 12 |
| hdsc | `/en/search?type=post&search=` | rendered post rows → `kind="post"` (news index; product-family terms only) | 12 (HC32) |
| acpcomposites | `/?s=<q>&post_type=product` | WooCommerce/FacetWP server-rendered grid `<h3><a href=/shop/..>` | 16 |
| speedymetals | `/search.aspx?SearchTerm=` | ASP.NET server table `<td><a href=p-<id>.aspx>` + Material/Shape → category | 462 |
| meanwell | `/productSearch.aspx?pkeywords=` | series anchors `search.aspx?prod=<MODEL>` → `kind="series"` (model/series-indexed; free text → 0) | 15 (LRS) |
| marcorubber | `/search-results?search=` | Angular SPA route (found in bundle) `<a class=search-header>` | 18 |

**9 fixed + 1 documented-unreachable.** dfrobot is a **Nuxt SPA whose search is
XHR-JSON only** — no SSR route (`/search-adv/result?q=` is a hard 404, sitemap lists
only `/product-<id>.html`), not in static DOM or `window.__NUXT__`. Left degrading
cleanly to `http_error`; docstring records the finding.

Every "false login-wall" (header Sign In / name=password newsletter field on a public
catalog) was dropped again here (batteryspace, acpcomposites, speedymetals, meanwell,
marcorubber, applerubber); challenge markers retained. Cross-check
`[[vendor-adapter-parser-reveng]]`.

## Infra fix landed alongside (reload-safety + canary backfill)
The 10 new reload-fixture parser tests exposed a pre-existing test-isolation bug:
`importlib.reload(services.browser.adapters)` reset `_REGISTRY` to empty (the
bottom-of-file site imports are no-ops on reload), so `all_adapters()`/
`login_wizard_sites()` silently emptied for later tests — the baseline was green only
by collection-order luck. Fixed by re-registering every already-imported adapter
submodule at package load (no-op first time, repopulates on reload). That, in turn,
un-masked `test_health_canaries_cover_every_registered_site`: 35 Wave 3/4/5 adapters
had no `CANARY_QUERIES` entry (hidden by the truncated registry). Backfilled all 35
(live-verified queries for headless-parsing sites, category terms for walled/SPA ones).
**Full suite: 301 passed / 79 skipped.**
</content>
