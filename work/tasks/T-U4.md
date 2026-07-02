---
id: T-U4
title: BOM & sourcing panel — rollup, prices, quote-out
workstream: U
owner: opus-iron
status: doing
branch: task/T-U4-ui
day: 4
priority: 3
depends_on: [T-U1]
---

## Context

Workstream B (merged) built rollup/pricing/quote backends; the user needs the table.

## Files / areas touched

- ui/abstract/bom_view.h + ui/imgui/src/ui/bom_panel.* (scaffolded): rollup table
  (qty/part/source/unit price/ext price, provenance-safe blanks where no price),
  vendor links (open_external), quote-out action + status, spec-sheet match review
  row-actions (accept/reject a match)
- Worker-RPC to bom_rollup/bom_price/request_quote/match_part_number

## Definition of done

- [ ] Rollup renders live from a multi-part assembly; NEVER fabricates prices (blank +
      tooltip per contract; the never-fabricate rule is contract-level)
- [ ] Quote-out fires and status lands in the panel; key-less mode degrades gracefully
- [ ] Match review: accept writes the match to the doc (editable, R1)
- [ ] Headless screenshots; ui_style_check green; gate; graphify update .

## Driver notes (Fable)

- MPN column: issue #3's fix is merged — surface MPN. Sourcing schema is aicad.sourcing/1;
  UI consumes as-is. T-U2's parts detail card and your row-detail should share the
  PartRecord/VendorPart rendering helpers — put them in ui/abstract, one implementation.
