---
id: T-U2
title: Parts browser panel — federated search, detail card, insert
workstream: U
owner: unassigned
status: todo
branch: task/T-U2-ui
day: 2
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
