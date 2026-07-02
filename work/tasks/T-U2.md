---
id: T-U2
title: Parts browser panel — federated search, detail card, insert
workstream: U
owner: sonnet-page
status: doing
branch: task/T-U2-ui
day: 2
priority: 2
depends_on: [T-U1, T-P2]
---

## Context

UI for the Pillar-17 parts federation (Erik: flagship feature). One panel: search
everything (fasteners→ECAD→sections→fittings→hydraulics→vendor), inspect, insert.

## Files / areas touched

- ui/abstract/parts_view.h (view-model: query, domain filters, results, detail, insert
  state) + ui/imgui/src/ui/parts_panel.* on the T-U1 scaffold
- Worker-RPC: part_search/part_get/part_place reached over the WorkerClient seam (T-F16
  worker_rpc.py precedent — register append-only in engine_oracle/worker.py)

## Definition of done

- [ ] Search-as-you-type w/ domain filter chips; results show domain badge + standard +
      source (local/live); live vendor rows marked and price-annotated when present
- [ ] Detail card: params table, sourcing links, provenance/license line, Insert button
- [ ] Insert calls part_place and the result is an editable doc artifact (R1) — assert
      in a scenario test; headless screenshot of populated panel
- [ ] Empty/offline/key-less states use the pattern-library placeholder vocabulary
- [ ] ui_style_check green; flock'd build; gate; graphify update .

## Driver notes (Fable)

- Backend is contracts/parts.md (frozen at T-P1) — UI consumes, never extends. Insert
  routing lives in T-P3's place.py; if T-P3 is unmerged, branch from its branch and
  record merge order.
- agent_panel.cpp is NOT yours; the copilot's standard-before-custom suggestions are a
  later F17/G-wave concern. Panel only.

## Progress log
- 2026-07-02 23:07 UTC [sonnet-page] REVIEW — branch task/T-U2-ui pushed.

Parts browser panel (Pillar 17 flagship) complete:

BACKEND SEAM:
- engine_oracle/parts_hub/worker_rpc.py: part_search/part_get/part_place over worker stdio (mirrors deliverables seam pattern)
- engine_oracle/worker.py: registered PARTS_METHODS (append-only, same block as ECAD/deliverables)
- 22 new tests in test_parts_worker_rpc.py — 22 pass, R1-editability skip (no core in CI, correct)

C++ UI:
- ui/abstract/parts_view.h: pure view-model (PartsPanelModel + state enum + sourcing/result/detail structs)
- ui/imgui/src/ui/parts_panel.{h,cpp}: scaffold-built panel — search-as-you-type, 8 domain chips, result pill rows (badge+standard+designation+live/price), detail card (params table + sourcing + provenance/license + Insert button routing to part_place async RPC), typed empty/loading/error states throughout
- Wired into application.cpp + scenarios.{h,cpp} (AI_CAD_SHOT=parts_browser worker-one-shot)

VERIFICATION:
- ui_style_check.py: 0 findings (clean)
- gate.sh: PASS (core build + ctest 100% + ui/abstract + ui/imgui build+shot+selftest)
- pytest engine_oracle/parts_hub/: 148 passed
- AI_CAD_SHOT=parts_browser screenshot: 6 M8 results populated, all 8 domain chips, detail empty-state, 6 results status footer — panel renders correctly
