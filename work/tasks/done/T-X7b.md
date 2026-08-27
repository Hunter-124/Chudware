---
id: T-X7b
title: Vendor wave-2 follow-ups — McMaster STEP import + structured price to bom_price seam
workstream: X
owner: hunter
status: done
branch: task/T-X7b-vendor-wave2-followups
depends_on: [T-X7]
lane: parts-sourcing
kind: new
priority: 4
---

## Context

T-X7 (merged) shipped the 4 wave-2 vendor adapters (Amazon/McMaster/JLCPCB/PCBWay) as
part_search federation providers with 45 tests + the vendor survey. Two DoD items were
honestly left partial and split here:

- McMaster STEP -> import_part with source metadata: adapter acquire() returns the
  reference but the STEP fetch -> import_part wiring is incomplete.
- Structured price/availability -> bom_price approved-provider seam: NOT DONE.

## Definition of done
- [x] McMaster (and where available) STEP acquire -> import_part with source metadata.
      `import_artifact` resolves the done queue job's provenance envelope by real path (or takes
      an explicit `source=`) and stamps it onto the imported document via
      `doc_set_meta(doc_id, "vendor_source", …)`. A stamp failure degrades to a
      `metadata_warning` and never fails the import.
- [x] Structured price/availability feeds the bom_price approved-provider seam.
      New core RPC `vendor_part_register` (merge-preserving upsert, optional `cad_path` for
      price-only records, `provenance` + `fetched_ts`). New MCP tool of the same name bridges a
      completed scrape into it.
- [x] Tests; gates PASS.

## Latent bug found and fixed

`core/sourcing/vendorreg.cpp` keyed index entries `slug(supplier)::slug(part_number)` while the
production `Seams::vendor_lookup` glue in `core/api/libchudware.cpp` looked up a bare
`slug(mpn)`. The two never agreed, so **no registered vendor part was ever visible to
`bom_rollup` / `bom_price`** — which is why DoD item 2 could not have worked as written. Both
paths now call one shared `sourcing::vendor_index_lookup(index, component)`
(`sourcing.h:167`), so they cannot drift again; the two-supplier regression test fails if they do.

## Anti-fabrication guarantee

A scraped display price is parsed only when unambiguous. `"$12.34"` -> `12.34`;
`"$10.00 / pack of 50"` and `"$5.00 - $9.00"` leave `unit_price` **null** and return the raw
string as `price_unparsed` with a hint to pass `unit_price=` explicitly. Every registered price
carries `provenance.scraped: true`, `provenance.method: "cef_browser_scrape"` and a `fetched_ts`,
and the tool's reply states it is not a vendor quote. A malformed `price_breaks` entry is
refused at the offending index rather than silently dropped (`unit_price_for_qty` would
otherwise skip it and look like a missing price).

## Verification (2026-08-03)

- `test_sourcing` 25 cases/235 assertions -> **28 cases/264 assertions**, all pass.
- `test_parts_tools_v2` 43 -> **50 passed**.
- `scripts/check.py` **all 8 gates PASS**; both core trees rebuilt (`build-occt-linux`, `build-linux`).
- Live end-to-end through the real MCP tools and the real engine:
  - built a box, exported a real `.step`, ran `import_artifact` -> `doc_set_meta` -> read the
    metadata back off the document with `doc_get_meta`;
  - registered a scraped price, confirmed the index key `mcmaster::91290a115`, confirmed a
    price-only upsert preserves `cad_path`/`description`/`price_breaks`, and confirmed a second
    supplier for the same part number coexists;
  - confirmed all three price-parse cases above.

## Locked decisions

- **No McMaster API key (Hunter, 2026-08-03).** McMaster will not issue one, so the
  acquisition path is the **CEF driving method**, not a vendor API: the interactive
  sign-in panel (`host/src/ui/widgets/cef/`, `docs/design/cef_webview.md`) establishes the
  session in the shared warm profile, and `services/browser/cef_client.py` drives that same
  profile headlessly. `services/browser/adapters/mcmaster.py::acquire` already resolves the
  client-minted `.step` download URL by running the page's own JS — the remaining work is
  wiring its result into `import_part` with source metadata, NOT adding an API client.
  Any adapter written against a hypothetical McMaster API is out of scope.
- Structured price/availability still feeds the `bom_price` approved-provider seam from
  whatever the CEF fetch parsed; a scraped price must be labelled with its provenance and
  timestamp rather than presented as a vendor-quoted figure.
