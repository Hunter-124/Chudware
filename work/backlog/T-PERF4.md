---
id: T-PERF4
title: Async tab switch, file open/save, sim_mesh, drawing HLR
workstream: perf
owner: unassigned
status: todo
branch: task/T-PERF4-async-tabs-files-longtools
depends_on: [T-PERF1]
lane: app-gui
kind: fix
---

## Context

docs/PERF_AUDIT_2026-07-10.md §3 P7/P8. switchToTab fallback rebuild async (keep sync activateCached fast path); file pump doc_open/doc_save via rpc + progress toast; sim_manager.cpp:1597 (180s sim_mesh!), drawing_panel.cpp 6 HLR sites, doc_tabs.cpp:606-646 → async with busy state.

## Definition of done

- [ ] STEP import no longer freezes the app
- [ ] sim mesh + drawing view creation show busy state, UI stays live
