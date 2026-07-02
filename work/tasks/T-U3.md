---
id: T-U3
title: CAM workspace UI — job tree, tool table, sim results, post/setup-sheet
workstream: U
owner: sonnet-quinn
status: done
branch: task/T-U3-ui
day: 3
priority: 2
depends_on: [T-U1]
---

## Context

The CAM backend (28 op types, 8 post dialects, dexel sim, T-M overhaul in flight) has
no coherent workspace UI. This is the Manufacture tab's real panel set.

## Files / areas touched

- ui/abstract/cam_view.h + ui/imgui/src/ui/cam_panel.* (scaffolded): job/setup/op tree,
  tool table editor, op parameter forms (REUSE panel_spec forms — the 44-form table
  already includes CAM ops; extend there, not bespoke), sim results strip (removed
  volume, gouges, rapid hits, holder collisions when T-M3 lands), post dialect picker +
  setup-sheet/export actions
- Worker-RPC to the existing cam_* surfaces (append-only registration)

## Definition of done

- [ ] Job tree reflects cam_job_tree live; op add/edit through panel_spec forms
- [ ] Tool table CRUD persisted via cam_add_tool/update paths
- [ ] Simulate button renders SimResult summary w/ warnings (incl. holder_collisions
      field — degrade gracefully if T-M3 unmerged)
- [ ] Post + setup-sheet actions produce files; dialect picker lists cam_post_dialects
- [ ] Headless screenshots: empty state + populated job; ui_style_check green; gate;
      graphify update .

## Driver notes (Fable)

- Coordinate with the T-M wave via the board — backend contracts are stable MCP tools;
  UI must not block on M2-M5. panel_spec.cpp is shared infrastructure: additions
  append-only (44-form precedent, b569020).
- describe_toolpaths (T-F7) gives you the narration string for the sim strip — reuse.

## Progress log
- 2026-07-02 23:18 UTC [sonnet-quinn] REVIEW: branch task/T-U3-ui pushed (7f45a3f). Gate PASS. ui_style_check 0 findings. Headless AI_CAD_SHOT=cam_workspace wrote 1600x1000 PNG — empty-state panel renders (3 tabs, op-add row, Generate/Simulate buttons, "Press 'New Job' to start" empty state). AI_CAD_SHOT=cam (existing ScenarioRunner) also PASS. Files: ui/abstract/cam_view.h (pure view-model), ui/imgui/src/ui/cam_panel.{h,cpp} (350+ lines: job tree + glass rows + arc_blend_linking badges + engagement E/W warnings + tool table + sim results strip with gouge/collision semantics + post/setup-sheet + dialect picker), ui/abstract/panel_spec.cpp (+210 lines: 20 CAM op PanelSpecs), scenarios.h/cpp + application.cpp wired. holder_collisions degrades to "not checked" when T-M3 unmerged.
