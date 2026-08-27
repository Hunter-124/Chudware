---
id: T-R6
title: PLM Project Hub - full-screen master workspace (item grid, activity, release board, where-used, Copilot dock)
workstream: R
owner: unassigned
status: done
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

- [x] Full-screen Hub opens as the landing surface for a selected project;
      thumbnailed item grid, filtered activity, lifecycle release board, and
      cached where-used explorer use the real PLM RPC/cache paths
- [x] Release board preserves plain-language pre-flight + explicit override;
      shared lifecycle chips invalidate/refetch everywhere
- [x] New-user onboarding teaches Project / History / Release, lists recent
      projects, and includes an honest prerequisite-driven guided tour from
      project selection through checkpoint and release
- [x] Per-project AI tier remains switchable in Hub settings; the persistent
      Copilot dock retains the PLM history/tool surface for cited what-changed
      queries
- [x] T-U1/kit conformance; release-board, tour, and thumbnailed-grid headless
      scenarios compile and capture through `CHUDWARE_SHOT=git`
      (`CHUDWARE_PLM_PAGE=releases|tour|graph`); graph updated

## Progress log

- 2026-08-27: Completed on master. Project selection now lands on the Hub and
  persists bounded recent-project folders. Releases is a first-class responsive
  In work / Released / Superseded board; History filters by item/person/AI;
  Files carries a cached direct-consumer where-used explorer; the existing
  Graph grid opens leaf documents and shows available PLM identity/lifecycle
  metadata; onboarding and the guided tour teach the complete workflow without
  claiming unmet prerequisites. Isolated delta TUs compiled, scoped GUI lints
  were clean, the integrated host build passed, and release/tour/grid headless
  captures all exited successfully.
