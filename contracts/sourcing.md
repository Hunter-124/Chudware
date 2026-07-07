# Contract: Part Sourcing Fan-out — `aicad.part_source/1`

> Version 1 (Parts Library V2, workstream 5). The result envelope of the deterministic
> sourcing fan-out (`engine_oracle/parts_hub/source.py`, exposed as the `part_source`
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

Consumers (the `sourcing_specialist` role card, GUI sourcing panes) MUST treat an
unknown `how` value as "no plan" and fall back to `part_get` inspection — new plan
kinds may be added additively.

## 3. Honesty rules

- A vendor site that needs a human login contributes ZERO candidates; the caller
  learns about it from `vendors_health()` (`needs_human_login: true`) and records a
  question for the human. Fabricating candidates for an unreachable site is a
  contract violation.
- Every number in a candidate (price, spec values) is copied from a PartRecord /
  vendor row — the fan-out itself computes only scores and unit conversions.
