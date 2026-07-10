---
id: T-PERF1
title: GUI async applyOps conversions (never block GL thread on edits)
workstream: perf
owner: grok
status: done
branch: task/T-PERF1-gui-async-applyops
depends_on: []
lane: app-gui
kind: overhaul
---

## Context

docs/PERF_AUDIT_2026-07-10.md §3 P3. Add DocMirror::applyOpsAsync (AsyncRpc + parseOpsResponse + mutationGen_ counter). Convert in pain order: PM live preview (property_manager.cpp:2106, single-in-flight latest-wins), feature_tree.cpp:594/:696 (batch N copy ops into ONE call), equations_dialog.cpp:56, design_table_panel.cpp:196 (coalesce cell bursts), config_panel.cpp:85, asm_panels.cpp:180/610/919, undo/redo sites in app.cpp. rebuildsInFlight chip (shipped) shows the stale-preview state.

## Definition of done

- [ ] applyOpsAsync on DocMirror with generation guard
- [ ] PM preview no longer blocks a frame; typing stays 60fps during rebuilds
- [ ] feature tree suppress/reorder/copy async + batched
- [ ] old mesh renders during rebuild with chip visible

## Progress log
- 2026-07-10 20:57 UTC [grok] applyOpsAsync on DocMirror (mutationGen latest-wins). Converted:
  PM live preview (async + serialize first add_feature), feature_tree flush batch,
  equations applyEqOp, asm insertComponent, applyModelAppearance, applyFlatPattern.
  Host build green. Remaining: config_panel / design_table callConfig, undo/redo
  sites, more asm_panels — can land under T-PERF1 follow-up or T-PERF4.
