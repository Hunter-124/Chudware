---
id: T-U4
title: BOM & sourcing panel — rollup, prices, quote-out
workstream: U
owner: sonnet-ash
status: done
branch: task/T-U4-ui
day: 4
priority: 3
depends_on: [T-U1]
lane: app-gui
kind: new
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

## Progress log
- 2026-07-02 23:57 UTC [sonnet-ash] REVIEW sonnet-ash branch=task/T-U4-ui

Delivered:
- ui/abstract/bom_view.h: pure view-model (BomRow, BomPanelModel, QuoteStatus, filterBomRows); BOM_UNPRICED "—" sentinel for null unit_cost/ext_cost — never a fabricated number per aicad.sourcing/1 §0
- ui/imgui/src/ui/bom_panel.{h,cpp}: panel_scaffold-based panel, 6-column ImGui table (Qty/Description/MPN/Supplier/Unit Price/Ext Price), async bom_price via s.rpc.call (AsyncRpc), quote-out fires request_quote (degrades gracefully keyless), match review accept writes via applyOps (R1 editable), cache invalidates on doc change
- engine_oracle/sourcing_worker_rpc.py: new worker-RPC bridge (bom_rollup/bom_price/request_quote/match_part_number) over stdio seam — mirrors deliverables/worker_rpc.py pattern; keyless request_quote returns {ok:false, reason, hint} not a fabricated price
- engine_oracle/worker.py: registers sourcing seam (try/except non-fatal)
- application.cpp: BOM window wired after scaffold_demo block
- scenarios.{h,cpp}: wantBom/bomShotDone worker one-shot; CHUDWARE_SHOT=bom seeds fixture rows with null prices; captureFrameBudget=150
- ui_style_check.py: 0 findings (all 3 rules clean)
- gate.sh: PASS (build + --shot + --selftest + 100% ctest + FAIL-RECORDED entries are all pre-existing T-V2 owned)
- CHUDWARE_SHOT=bom headless shot: wrote PNG exit:0
