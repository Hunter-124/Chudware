# Contract: Part Sourcing Fan-out — `aicad.part_source/1`

> Version 1 (Parts Library V2, workstream 5). The result envelope of the deterministic
> sourcing fan-out (`services/parts/source.py`, exposed as the `part_source`
> MCP tool and the copilot's mission-scoped `part_source` wrapper). Append-only per
> `contracts/README.md`: new OPTIONAL fields may be added additively without a bump.
>
> Related contracts: `contracts/parts.md` (PartRecord, `aicad.parts/1`),
> `contracts/sourcing_schema.md` (VendorPart / priced BOM lines, `aicad.sourcing/1`).
> There is NO LLM inside `part_source` — it is pure taxonomy routing + facet
> extraction + hub search + deterministic scoring. Same input, same output.

## 1. Result envelope

```json
{
  "ok": true,
  "interpreted": {
    "category_id": "structural_stock.metal_tube",
    "categories": ["structural_stock.metal_tube"],
    "facets": {"alloy": "4130", "od_mm": 25.4, "wall_mm": 1.25},
    "terms": ["4130", "chromoly", "od", "tube", "wall", "..."]
  },
  "candidates": [
    {
      "part_id": "catalog:onlinemetals::https://...",
      "title": "4130 Chromoly Round Tube 1\" OD x .049 wall",
      "vendor": "onlinemetals",
      "price": 18.4,
      "specs": {"od_mm": 25.4, "wall_mm": 1.25, "alloy": "4130"},
      "source": "live",
      "score": 3.9,
      "rationale": "spec fit: od_mm=25.4, wall_mm=1.25, alloy=4130; priced (18.4); ...",
      "artifact_plan": {"how": "scrape_import", "site": "onlinemetals", "query": "..."}
    }
  ],
  "coverage": {
    "categories_matched": 1, "records_considered": 8, "raw_hits": 12,
    "local_candidates": 2, "live_candidates": 3, "facets_queried": 3
  }
}
```

Failure is contract-shaped: `{"ok": false, "reason": <str>, "hint": <str>}` — never a
traceback, never a network requirement (CI-safe).

- `interpreted.facets` — the facet query extracted from the need text, keyed by the
  matched category's facet keys (`taxonomy.json` vocabulary). Numeric hints
  ("OD 25.4", "12mm bore", "wall 1.25") convert to mm; enum hints match the facet's
  `values[]` (with a small colloquial alias table, e.g. "chromoly" → "4130").
- `candidates[].price` — `float` in USD or `null` when unknown (`null` ≠ `0`,
  same rule as `aicad.sourcing/1`). Prices come from the record's `sourcing[]`
  rows or params — NEVER invented.
- `candidates[].score` — deterministic: facet-fit dominates (3.0 × matched/queried),
  plus small terms for title overlap, known price, local-vs-live source, and
  artifact availability. Ties break on `part_id` (stable ordering).
- `candidates[].rationale` — human-auditable trace of exactly which facets matched
  and what contributed to the score.

## 2. `artifact_plan` vocabulary

How the caller obtains REAL, editable geometry for a candidate. `how` is a closed
enum; `site` / `query` appear only for `scrape_import`.

| how                  | when                                                        | follow-up |
|----------------------|-------------------------------------------------------------|-----------|
| `part_place`         | local library record with a usable `geometry_ref`           | `part_place(part_id)` |
| `scrape_import`      | browser-catalog record (`part_id` = `catalog:<site>::…`) or no local geometry; `site` + `query` provided | `scrape_enqueue(site, query)` → poll `scrape_queue_status()` → `import_artifact(artifact_path)` |
| `make_standard_part` | fastener-ish standard hardware family without a direct geometry_ref | `make_standard_part(family, size, …)` |
| `find_model`         | a **discrete manufactured product** (has an MPN/brand identity) with NO geometry, that is NOT standard hardware and NOT raw stock — e.g. an Amazon/retail listing. The product likely has a model in a CAD repo we can match. | `match_model(part_id)` → attach an exact match, or confirm/pick a candidate |
| `reverse_engineer`   | terminal fallback surfaced by `match_model` when NO confident repo match exists — build the geometry from the product's photos/datasheet | `reverse_engineer(source)` / `reveng_start_session` (seeded with scraped photos+datasheet) |

Consumers (the `sourcing_specialist` role card, GUI sourcing panes) MUST treat an
unknown `how` value as "no plan" and fall back to `part_get` inspection — new plan
kinds may be added additively.

**Boundary (do not regress):** `find_model` fires ONLY for records carrying a
discrete-product identity signal (a normalized MPN/part-number OR brand/manufacturer)
that lack geometry and generatable params. Raw stock (metals/plastics with
`od_mm`/`wall_mm`/`alloy` params) and standard hardware keep their existing
`scrape_import` / `make_standard_part` plans — they are modeled parametrically, never
"found" or reverse-engineered.

## 2a. Model matching — `match_model` (`aicad.match_model/1`)

Given a product that lacks a 3D model, fan out across the **CAD-model-yielding** source
adapters (grabcad, traceparts, content3d/3dcontentcentral, freecadlibrary,
ultralibrarian, componentsearchengine, snapeda, cadenas + native-CAD vendors) and rank
candidate models by IDENTITY confidence. Deterministic — no LLM inside.

```json
{
  "ok": true,
  "product": {"title": "…", "mpn": "1LNK-4B", "brand": "AcmeCorp"},
  "matches": [
    {"part_id": "catalog:grabcad::https://…", "title": "…", "site": "grabcad",
     "mpn": "1LNK-4B", "confidence": "exact", "score": 4.0, "attach": true,
     "artifact_plan": {"how": "scrape_import", "site": "grabcad", "query": "1LNK-4B"}}
  ],
  "best": { "…the top match or null…" },
  "fallback": {"how": "reverse_engineer", "reason": "no exact/strong match",
               "seed": {"title": "…", "mpn": "…", "brand": "…", "source_url": "…",
                        "images": ["https://…/photo1.jpg", "…"],
                        "documents": ["https://…/datasheet.pdf", "…"]}},
  "coverage": {"sites_searched": 9, "raw_hits": 14}
}
```

**Confidence tiers + auto-attach policy** (a wrong model silently corrupts a design, so
the bar is deliberately high):

| confidence | condition | `attach` |
|------------|-----------|----------|
| `exact`  | normalized-MPN equality (`spec_sheet_matcher.normalize_mpn`) between product and candidate | `true` — the ONLY tier that auto-attaches |
| `strong` | brand/manufacturer match + product part-number is a substring of the candidate's (or vice-versa) | `false` — surfaced as a candidate for one-click user confirmation |
| `weak`   | title-token / spec overlap only, no part-number agreement | `false` — tentative; shown but must never auto-attach |

- If NO `exact` match exists, `best` is the highest-confidence non-exact candidate (for
  the user to confirm) and `fallback` carries a `reverse_engineer` plan seeded with the
  product's scraped photos/datasheet. `reverse_engineer` is OFFERED, never auto-started.
- Reuse `spec_sheet_matcher.match_part_number(mpn)` FIRST: if the MPN resolves to a
  standard-hardware family, that is the answer (no repo search needed).
- Honesty: a CAD site needing a human login contributes ZERO matches (learn via
  `vendors_health`), never a fabricated match.

## 2b. Reverse-engineer hand-off — product media harvest

When the user accepts the `reverse_engineer` fallback, the RE suite must start
already loaded with everything scrapable about the product. A **media-harvest** step
(`services/parts/product_media.py :: harvest_product_media(url|part_id)`)
fetches the product page under the user's own browser session and collects EVERY
candidate image (product photos, gallery `<img>`/`srcset`, og:image) and document
(datasheet/manual/spec `.pdf` and doc links), returning
`{ok, source_url, images:[...], documents:[...]}` — reference-only URLs, nothing
redistributed. The harvest feeds one call that stands up the session:

`reveng_start_from_product(part_id|url)` → `reveng_start_session(seed)` +
`reveng_add_images(images)` + `reveng_ingest_datasheet(documents)` (the existing
`services/reveng/worker_rpc.py` worker methods), returning the new `session_id` +
`activeWorkspace = ReverseEng` so the GUI opens the RE tab pre-populated. The harvest
is best-effort: zero images/documents still starts a session (the user adds their own
photos), never a traceback.

## 3. Honesty rules

- A vendor site that needs a human login contributes ZERO candidates; the caller
  learns about it from `vendors_health()` (`needs_human_login: true`) and records a
  question for the human. Fabricating candidates for an unreachable site is a
  contract violation.
- Every number in a candidate (price, spec values) is copied from a PartRecord /
  vendor row — the fan-out itself computes only scores and unit conversions.
