---
id: T-X9
title: In-app ordering - BOM to vendor carts, quote submission for manufactured parts, order tracking records
workstream: X
owner: unassigned
status: todo
branch: task/T-X9-in-app-ordering
depends_on: [T-X7, T-X8]
lane: parts-sourcing
kind: new
priority: 2
---

## Context

Hunter directive 2026-07-05 (priority #4 endgame): "letting them actually order the
parts in our app" — from Amazon prefab parts to sending geometry off for metal 3DP /
PCB fab / FDM / CNC via services like PCBWay, JLCPCB, and the T-X7 survey shortlist.
Builds on: bom_rollup/bom_price (BOM lines with sourcing), request_quote (made-part
cost seam), T-X7 vendor adapters, T-X8 headed sessions.

## Definition of done

- [ ] Ordering seam `services/sourcing/orders.py`: an order draft = BOM lines grouped by
      vendor + made-parts with their export artifacts (STEP/STL/gerbers via existing
      export paths). Draft -> vendor-specific CART/QUOTE actions via browser adapters:
      add-to-cart for catalog vendors (Amazon/McMaster), quote-upload flow for fab
      services (JLCPCB/PCBWay: upload gerber zip / STL, capture quoted price + lead time
      with provenance URL).
- [ ] HARD CONFIRMATION BOUNDARY: the app NEVER clicks a final "place order"/"pay"
      button. It fills carts/quotes and then opens the vendor page (headed, T-X8 window)
      for the USER to review and submit. Every automated cart action is recorded in an
      order ledger (JSON per order under the project dir) for inspectability.
- [ ] Order records: submitted orders get a record (vendor, items, price snapshot,
      order-page URL, status user-editable) surfaced on the BOM panel; no scraping of
      order status in v1 (manual refresh is fine — note as follow-up).
- [ ] GUI: sourcing section on the BOM/parts panel — per-line vendor pick (federated
      search results), "Build cart" per vendor, quote cards for fab services, order
      record list. T-U1 style conformance; lands in the T-G23 chrome.
- [ ] MCP tools so the copilot can assemble an order draft from a BOM and report quotes
      (numbers with provenance only); dag-visible so missions can end at "cart ready".
- [ ] Mocked-flow tests green in gate venv; live tests env-gated (T-X6 discipline);
      license_debt.md updated for any vendor ToS concerns the cart automation raises.

## Driver notes

- graphify rule: `graphify query "<question>"` before raw-file reads (subagents too).
- Branch task/T-X9-in-app-ordering, worktree ../CAD-T-X9. Stop at status: review.
- This is the task that makes the parts library "actually pivoted" to Hunter's #4 —
  if scope forces a cut, cut vendors, not the confirmation boundary or the ledger.
- Python venv for gate: `~/.local/share/Chudware/mcp-venv/bin/python`.
