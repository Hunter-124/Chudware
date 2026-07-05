---
id: T-X7
title: Vendor adapter wave 2 - Amazon, McMaster-Carr, JLCPCB, PCBWay + manufacturing-service discovery
workstream: X
owner: opus-wynn
status: doing
branch: task/T-X7-vendor-adapters-wave2
depends_on: [T-X2, T-X3]
lane: parts-sourcing
kind: new
priority: 2
---

## Context

Hunter directive 2026-07-05 (priority #4): the parts library pivots to full web
acquisition across REAL vendors, not just CAD-model communities. T-X2's adapter
framework (services/browser/adapters/) + T-X3's part_search federation are the seam;
this task adds the commerce-vendor adapters:

- **Amazon** — rapid prototyping + common parts fast, everywhere. Search, product page
  parse (price/availability/Prime), link-out + structured part record for BOM lines.
- **McMaster-Carr** — the canonical MRO/hardware catalog; has real CAD downloads (STEP)
  per SKU behind the user's session. Search + CAD fetch + price/spec parse.
- **JLCPCB / PCBWay** — PCB fab + assembly quotes; parts (JLC parts library) for ECAD
  BOM matching (ties into ecad_find_part).
- **Discovery survey** (in-repo doc, no adapter yet): metal 3DP / CNC / FDM service
  bureaus worth adapters next — evaluate at least Xometry, Protolabs/Hubs, SendCutSend,
  Craftcloud, OSH Park, JLC3DP; rank by API-vs-browser difficulty and quote automation
  potential. Output: docs/SOURCING_VENDOR_SURVEY.md feeding T-X9 (ordering).

## Definition of done

- [ ] Adapter per vendor above following the T-X2 AdapterBase contract (rate-limited,
      profile-scoped, terms-wall detection, NEVER auto-accepting agreements, NEVER
      storing credentials — cookies live only in Playwright profiles).
- [ ] Registered as part_search federation providers (T-X3 seam) with provider tags so
      the parts panel can facet by vendor; degraded/CI-safe when profiles are absent
      (same skip discipline as GrabCAD/TraceParts adapters).
- [ ] McMaster STEP download lands through the standard import_part path with source
      metadata (vendor, SKU, URL, fetched-at) on the part record for BOM/sourcing.
- [ ] Structured price/availability records feed bom_price's approved-provider seam —
      numbers copied from vendor pages with provenance URL, never LLM-generated.
- [ ] docs/SOURCING_VENDOR_SURVEY.md — manufacturing-service shortlist with per-service
      notes (quote flow, upload formats, auth walls, ToS flags for license_debt.md).
- [ ] Selector fixtures + unit tests (mocked HTML) green in gate venv; live-session
      integration tests gated behind T-X6's env flag; license_debt.md rows appended per
      vendor as needed.

## Driver notes

- graphify rule: `graphify query "<question>"` before raw-file reads (subagents too).
- Branch task/T-X7-vendor-adapters-wave2, worktree ../CAD-T-X7. Stop at status: review.
- Amazon has aggressive bot detection — keep fetch cadence human-scale (T-X2 ratelimit),
  and on any captcha/challenge surface `login_required`/`challenge` to the user, never
  attempt bypass. Same for every vendor.
- Python venv for gate: `~/.local/share/AI-CAD/mcp-venv/bin/python`.
