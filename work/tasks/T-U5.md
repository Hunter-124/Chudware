---
id: T-U5
title: Requirements & verification panel — req status, battery results
workstream: U
owner: unassigned
status: todo
branch: task/T-U5-ui
day: 5
depends_on: [T-U1]
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
