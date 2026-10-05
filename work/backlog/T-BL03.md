---
id: T-BL03
title: Intake Gates 1-3 (process profiles, photo metrology, fit coupons)
workstream: backlog
owner: unassigned
status: backlog
branch: task/T-BL03-intake-gates
depends_on: []
lane: ai-copilot
kind: new
priority: 4
---

## Context

Implement the remaining gates of docs-era INTAKE_MEASUREMENT_DFM_PLAN: process profile tables, photo metrology with confidence scoring, printable fit coupons.

## Definition of done

- [ ] Intake pause/resume state machine complete. It now lives in
      `services/intake/intake.py` (`IntakeState`, `open|awaiting_user|resolved` resume
      token) behind the `intake_status` MCP tool; the old `engine/intake.py` was retired.
      Whether it is complete against the plan is unreviewed.
- [x] Process profile table with FDM default + CNC/laser alternates — `core/dfm/process_profiles.cpp`
      (`fdm` default; `cnc_mill`, `laser`, `sheet_metal`, `injection`, `lathe`).
- [ ] make_fit_coupon produces a printable coupon part. The tool exists
      (`services/mcp/intake_tools.py::make_fit_coupon`, registered) but no regression test
      of its coupon output was found.
