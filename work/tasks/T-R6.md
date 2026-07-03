---
id: T-R6
title: PLM Project Hub - full-screen master workspace (item grid, activity, release board, where-used, Copilot dock)
workstream: R
owner: unassigned
status: todo
branch: task/T-R6-plm-project-hub
depends_on: [T-R4, T-R5]
lane: plm-projects
kind: new
priority: 3
---

## Context

The master GUI ([docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md)
§4): a full-screen workspace — its own doc-tab kind, like the CAM workspace — and
the app's landing surface when a project is open. This is where "manage the entire
project" lives, and where the three-noun mental model teaches itself (empty states
explain Project/History/Release in one sentence each). Introduces the Project
concept to the shell: open-folder = open-project, recent projects on the start
surface.

## Files / areas touched

- ui/ Project Hub workspace (T-U1 kit): item grid (live thumbnails, part number,
  rev + lifecycle chips, click → opens the doc tab), project activity timeline
  (all items interleaved, filter by item/person/AI), release board (In work /
  Released / Superseded columns, one-click release with T-R4 pre-flight), where-
  used explorer (plm_where_used graph), team presence + lock chips and Sync
  button (T-R7 — render only when a remote is configured; degrade to hidden)
- Shell: open-project flow (folder picker + recent projects), Hub as landing tab
  when a project opens, per-project AI-tier setting surfaced here (manual /
  assisted / managed — T-R3)
- Copilot dock: AI-managed actions log + natural queries ("what changed since
  Tuesday?" → plm_history + summaries)
- Onboarding: guided tour via T-Q3 machinery; user-manual section via T-Q4
  auto-generation

## Definition of done

- [ ] Full-screen Hub opens as landing surface for a project; item grid, activity
      timeline, release board, where-used explorer all live against a real
      multi-doc test project
- [ ] One-click release from the board (pre-flight surfaced in plain language);
      lifecycle chips update everywhere (grid, tabs, editor badges)
- [ ] New-user path proven: from empty app → create project → save a part →
      checkpoint → release, guided only by empty-state text + tour (record a
      scripted walkthrough; no manual reading required)
- [ ] Per-project AI tier switchable in Hub settings; Copilot dock answers a
      what-changed query with cited history entries
- [ ] T-U1 conformance + AI_CAD_SHOT scenarios (hub grid, release board, tour);
      gate + graphify update .
