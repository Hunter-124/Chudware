---
id: T-U5
title: Requirements & verification panel — req status, battery results
workstream: U
owner: sonnet-dove
status: done
branch: task/T-U5-ui
day: 5
priority: 3
depends_on: [T-U1]
lane: app-gui
kind: new
---

## Context

T-H3 req/1 + T-H4 30-check battery are merged; design-to-requirements needs a surface:
see reqs, their status, and verification verdicts inline.

## Files / areas touched

- ui/abstract/req_view.h + ui/imgui/src/ui/requirements_panel.* (scaffolded): req list
  w/ status pills (met/unmet/waived/unknown per aicad.req/1 VALID_STATUSES), detail w/
  quantity+range, verify battery run action + per-check verdicts, groundedness verdicts
  where present
- Worker-RPC to req_create/req_update/req_status/req_report + verify_battery

## Definition of done

- [ ] Req CRUD through the panel lands as doc-ops (R1); status pills track req_status live
- [ ] Run-verification renders per-check pass/fail w/ evidence strings from the battery
- [ ] Waived status requires a reason (contract rule) — enforced in the form
- [ ] Headless screenshots; ui_style_check green; gate; graphify update .

## Driver notes (Fable)

- Status vocabulary comes from engine_oracle.requirements VALID_STATUSES (post-alignment,
  canonical) — import, don't restate. This panel is ALSO T-F17's design-brief anchor
  later; keep req_view.h reusable (mission control will embed it).

## Progress log
- 2026-07-03 00:56 UTC [sonnet-dove] REVIEW: T-U5 requirements & verification panel complete.

Branch: task/T-U5-ui (commit 54c6471)
Build: clean (0 errors, 0 warnings on requirements_panel.cpp)
Style: ui_style_check.py 0 findings
Shot: AI_CAD_SHOT=requirements captured (1600x1000) — panel renders behind first-run dialog (pre-existing behavior, same as bom/ecad shots in same env)

Files:
- ui/abstract/req_view.h — pure view-model: ReqRow, BatteryCheckRow, ReqPanelModel, filterReqRows()
- ui/imgui/src/ui/requirements_panel.{h,cpp} — panel on panel_scaffold; 5-status pills; waive form gated on non-empty reason + who="user"; battery results section
- ui/imgui/src/app/application.cpp — include + centered window block appended after bom block
- ui/imgui/src/ui/scenarios.{h,cpp} — wantRequirements/requirementsShotDone flags; captureFrameBudget 150; configureShot entry; tickSimpleShots seed with fixture covering all 5 VALID_STATUSES + 2 battery checks

Status: colors use TVEC(good)/TVEC(bad)/TVEC(accent)/TVEC(txt2) — no literal ImVec4/IM_COL32. Raw string delimiter R"json(...)json" used to avoid )"-collision in fixture JSON.
