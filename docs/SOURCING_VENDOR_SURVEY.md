# Manufacturing-Service Vendor Survey (T-X7)

Shortlist of **manufacturing-service bureaus** (make-to-order: CNC, 3D printing, sheet
metal, PCB fab) worth wrapping as sourcing adapters next, ranked by how automatable their
quote flow is (public API vs. browser-only) and their fit for AI-CAD's BOM → quote → order
pipeline. This survey has **no adapter code** — it feeds the ordering work in
[T-X9](../work/tasks/T-X9.md) (BOM → vendor carts / quote submission) and picks the next
adapter targets after the wave-2 commerce/catalog vendors (Amazon, McMaster-Carr, JLCPCB,
PCBWay) shipped in T-X7.

Companion surveys: [CUSTOM_MODEL_INTEGRATION_SURVEY.md](CUSTOM_MODEL_INTEGRATION_SURVEY.md)
(CAD-model communities), [PLM_OSS_SURVEY.md](PLM_OSS_SURVEY.md).

## Methodology & scoring

Each service is scored on two axes that drive adapter cost/value:

- **Quote automation** — can a part file get an instant, structured price back without a
  human? `API` (documented programmatic quote) > `Instant-browser` (deterministic on-page
  quote we can parse) > `Manual/RFQ` (human turnaround, no automatable number).
- **Adapter difficulty** — `Low` (public API or simple upload+parse), `Med` (browser quote
  behind a login/session), `High` (heavy bot defense, multi-step configurators, or
  RFQ-only).

> ⚠️ **Verification note.** The API/endpoint claims below are from public documentation and
> general knowledge, **not verified against a live integration**. Before building any adapter
> here, confirm the current API availability, auth model, and Terms of Service against the
> vendor's live developer docs (same discipline as the T-X2 adapters' "best-effort regex,
> refine under T-X6 integration tests" seam). Do **not** hardcode a quote number or endpoint
> that hasn't been re-checked. Every automated quote number must be copied from the vendor's
> own response with a provenance URL — never generated.

## Shortlist (ranked — build in this order)

| # | Service | Processes | Quote automation | Difficulty | Why this rank |
|---|---------|-----------|------------------|------------|---------------|
| 1 | **Xometry** | CNC, 3DP, sheet, injection, casting | API (instant-quote partner API) | Low–Med | Broadest process coverage + the most mature public instant-quote API of the group; best single "one adapter, many processes" ROI. |
| 2 | **Protolabs Network** (ex-Hubs / 3D Hubs) | CNC, 3DP, sheet | API (instant quote) | Med | Distributed-manufacturing network with a historically documented instant-quote API; strong coverage, account/partner-gated. |
| 3 | **Craftcloud** (by All3DP) | 3DP (aggregator), some CNC | API (multi-provider quote) | Low–Med | Aggregator — one call returns *comparative* prices across many 3DP bureaus. High value for a "cheapest-provider" facet; less control over any single shop. |
| 4 | **SendCutSend** | Laser/waterjet/plasma cut, bending, CNC, tapping | Instant-browser (API emerging) | Med | Best-in-class flat/sheet-metal instant quote; upload DXF (flat) or STEP (bends). Quote is deterministic and parseable; primarily browser-session today. |
| 5 | **JLC3DP** | Resin/SLA, MJF/SLS, CNC | Instant-browser | Med | JLCPCB's manufacturing arm — reuses the JLCPCB profile/session already added in T-X7, so incremental cost is low and it rounds out 3DP/CNC next to the PCB adapter. |
| 6 | **OSH Park** | PCB fab (small-batch, "purple boards") | Instant-browser (fixed $/in²) | Low | Simplest of all: upload Gerber/KiCad/Eagle, price is a published per-square-inch formula. Niche (hobby/proto PCB) but trivial to wrap; overlaps JLCPCB/PCBWay on PCB. |

## Per-service notes

### 1. Xometry — *build first*
- **Processes:** CNC milling/turning, 3D printing (FDM/SLA/SLS/MJF/DMLS), sheet metal,
  injection molding, urethane casting.
- **Quote flow:** Instant Quoting Engine for many geometries; complex parts fall back to
  manual review. Documented **partner/Instant-Quoting API** (OAuth-style key) — the strongest
  automation candidate.
- **Upload formats:** STEP/STP, IGES, Parasolid, STL, plus 2D (DXF/PDF) for sheet.
- **Auth wall:** Business account + API credentials; some quoting requires being logged in.
- **ToS flags:** API use is governed by a developer agreement; scraping the browser quoter
  instead of using the API is likely a ToS violation — **prefer the API**. Add a
  `contracts/license_debt.md` row when the adapter lands.
- **Adapter shape:** API-key `PartProvider`-style (not a browser adapter) → cleanest fit for
  the `bom_price` approved-provider seam; returns structured `unit_price` + lead time with a
  provenance quote URL.

### 2. Protolabs Network (formerly Hubs / 3D Hubs)
- **Processes:** CNC, 3DP, sheet metal via a vetted manufacturer network.
- **Quote flow:** Instant quotes for supported geometries; **Hubs historically exposed a
  public instant-quote API** — re-verify it survived the Protolabs acquisition/rebrand.
- **Upload formats:** STEP, STL, and process-specific.
- **Auth wall:** Account + API token.
- **ToS flags:** Developer-terms gated; re-confirm current API status before committing.
- **Adapter shape:** API-key provider if the API is live; otherwise browser instant-quote.

### 3. Craftcloud (All3DP)
- **Processes:** 3D printing marketplace/aggregator (dozens of shops, many materials/finishes);
  some CNC.
- **Quote flow:** **Craftcloud API** returns *comparative* quotes across providers for an
  uploaded model — one integration, many prices. Great for a "compare vendors" facet in the
  parts/BOM panel.
- **Upload formats:** STL and common mesh; some solid formats.
- **Auth wall:** API key.
- **ToS flags:** Aggregator ToS; provenance must attribute the *winning sub-provider*, not
  just "Craftcloud".
- **Adapter shape:** API-key provider returning multiple ranked price records.

### 4. SendCutSend
- **Processes:** Laser/waterjet/plasma cutting, press-brake bending, CNC routing, tapping,
  hardware, powder coat — the sheet/flat-metal specialist.
- **Quote flow:** Fast, deterministic **instant browser quote** from an uploaded flat pattern;
  a partner API has been in development — re-check. Today assume a profile-scoped browser
  adapter parsing the on-page price.
- **Upload formats:** DXF/DWG (flat 2D), STEP (for bends/3D), material + thickness selected
  in-flow.
- **Auth wall:** Account for saved quotes/cart; anonymous quoting possible for some flows.
- **ToS flags:** Standard no-abusive-automation; keep human-scale cadence (T-X2 ratelimit),
  surface any login/terms, never auto-submit an order.
- **Adapter shape:** Browser adapter (traceparts terms-wall template) returning a structured
  price record; strong synergy with sheet-metal CAM output.

### 5. JLC3DP
- **Processes:** Resin/SLA, SLS/MJF nylon, CNC — JLCPCB's non-PCB manufacturing arm.
- **Quote flow:** Instant browser quote; **reuses the JLCPCB Playwright profile/session** from
  the T-X7 adapter, so marginal cost is low.
- **Upload formats:** STL, STEP.
- **Auth wall:** Same JLC account as JLCPCB.
- **ToS flags:** JLC platform terms (shared with the JLCPCB adapter's `license_debt` row).
- **Adapter shape:** Browser adapter; share config/session slug with `jlcpcb.py`.

### 6. OSH Park
- **Processes:** Small-batch PCB fabrication (shared-panel "purple boards"), some flex/after-dark.
- **Quote flow:** Deterministic **published pricing** (fixed $/in² per service tier) —
  effectively computable from board area once the Gerber/design is uploaded; minimal parsing.
- **Upload formats:** KiCad, Eagle, Gerber (zip).
- **Auth wall:** Account to order; pricing is public.
- **ToS flags:** Light; hobby-friendly. Still surface login for ordering, never auto-order.
- **Adapter shape:** Lowest-effort browser adapter (or even a pure area×rate calculator with a
  provenance link to the pricing page); overlaps the JLCPCB/PCBWay PCB adapters — build only
  if small-batch/US-fab coverage is wanted.

## Recommendation for T-X9 (ordering)

- **First two adapters:** Xometry (API, widest process coverage) then Protolabs Network —
  both are API-key `PartProvider`-style and drop straight into the `bom_price` approved-provider
  seam with real, provenance-stamped quote numbers.
- **Then** the sheet-metal + 3DP browser adapters (SendCutSend, JLC3DP) using the T-X2
  browser-adapter + terms-wall pattern, reusing sessions where possible.
- **Deprioritize** OSH Park unless US small-batch PCB is explicitly needed (PCB is already
  covered by the T-X7 JLCPCB/PCBWay adapters).
- Every make-to-order adapter must: rate-limit at human cadence, surface `login_required` /
  `terms_confirmation_required` / captcha-challenge rather than bypass, **never auto-accept an
  order or agreement**, and stamp each quote with a provenance URL (numbers copied, never
  generated). Append a `contracts/license_debt.md` row per vendor when its adapter lands.
