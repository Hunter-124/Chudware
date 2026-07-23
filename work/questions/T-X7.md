# T-X7 — advisor questions

## Q1 (2026-07-05, opus-wynn): browser-session prices into the `bom_price` "no-scraping" seam?

**The conflict.** DoD item 4 says: *"Structured price/availability records feed bom_price's
approved-provider seam — numbers copied from vendor pages with provenance URL, never
LLM-generated."* But the `bom_price` price seam — `engine_oracle/parts_providers.py`
(`ProviderChain([Nexar, DigiKey, Mouser])`, consumed by `engine/bom.py:resolve_prices` via
`services/mcp/sourcing_tools.py:bom_price`) — is **deliberately API-key-only and explicitly
"no scraping."** That guarantee is load-bearing: it's stated in the module header, in
`McMasterProvider.fetch`'s user-facing `ProviderError` ("Chudware does NOT scrape
mcmaster.com… download the CAD manually and register it"), and underpins the parts-library
docs. The T-X7 wave-2 adapters, by contrast, are the **user's-own-authenticated-browser-
session** mechanism blessed by Hunter's 2026-07-04 directive (`contracts/license_debt.md`
row 3 — "the browser acts as the user's own browser under the user's own login, not an
anonymous scraper"), which supersedes "no scraping" *for that mechanism specifically*.

So: may browser-session-parsed prices flow into the `bom_price` chain, and if so, how — given
the chain's existing explicit "no scraping" contract and its user-facing promises?

**Recommended answer (a): a distinct, profile-gated `BrowserVendorProvider` tier, behind the
API providers.** Add a provider that wraps a T-X7 browser adapter (`search(mpn)` → top hit →
`acquire()` → price), appended to the chain AFTER Nexar/DigiKey/Mouser so an approved API
always wins. It is **active only when a live Playwright profile exists** (else it's absent,
never an anonymous fetch — same degraded discipline as the catalog providers), **fail-closed**
(`ProviderError` on miss, never a guessed number), and stamps a **`source: "browser-session"`
+ provenance URL** so BOM lines visibly distinguish a user-session price from an approved-API
price. This honors all three constraints: the API "no anonymous scraping" guarantee stays true
(it's the user's own session, already blessed), the DoD is met, and prices remain
copied-with-provenance. It DOES change `parts_providers.py`'s posture (adds a browser tier to a
file that today says "no scraping"), so the module header + the McMaster error text need
rewording — hence this escalation rather than a unilateral edit.

**Alternatives:**
- **(b) Keep `bom_price` strictly API-only; surface browser-vendor prices in a *separate*
  parts-panel sourcing field** (the catalog record's `params`/`sourcing`, already populated by
  T-X7 item 3), NOT the `bom_price` chain. Cleanest contract-wise (zero change to the
  no-scraping seam) but reads DoD item 4 narrowly — bom_price numbers stay API-sourced.
- **(c) Inline the browser price into the chain with no new tier** — rejected: muddiest, erodes
  the API/browser separation the error messages promise.

**What I did in the meantime:** item 3 (SKU/price → catalog `params`, committed `2bca34ba`)
already makes browser-vendor prices available on the part record via the catalog seam, so
alternative (b) is *partially in place today* regardless of the ruling. I did NOT wire anything
into `parts_providers.py` pending this answer. Please rule (a) / (b) / (c); I'll implement the
chosen path.
