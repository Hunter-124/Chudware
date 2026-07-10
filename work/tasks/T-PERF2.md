---
id: T-PERF2
title: Async latest-wins drag pipelines (sketch/3D/push-pull/asm/explode)
workstream: perf
owner: grok
status: done
branch: task/T-PERF2-async-drag-pipelines
depends_on: []
lane: app-gui
kind: overhaul
---

## Context

docs/PERF_AUDIT_2026-07-10.md §3 P4. Single-in-flight solve RPC per drag; each mouse sample overwrites pendingTarget; drain applies + submits next; viewport renders last SOLVED state. Convert: sketch_mode.cpp:6095/2754 (replace dragFrame_ throttle), sketch3d_mode.cpp:866, viewport_panel.cpp:1394 push-pull (revert+pushPull as queued async pair; AsyncRpc FIFO keeps checkpoint envelope safe), viewport_panel.cpp:1487 asm drag, asm_panels.cpp:1055 explode (coalesce to latest factor). Engine-side drag System caching already shipped (kernel package).

## Definition of done

- [ ] no sync worker.call in any drag path (AI_CAD_RPC_TRACE clean)
- [ ] frame rate independent of solve latency; drag generation counter drops stale responses
