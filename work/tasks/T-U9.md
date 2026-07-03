---
id: T-U9
title: Gallery panel + first-run sample wiring
workstream: U
owner: sonnet-reed
status: review
branch: task/T-U9-gallery-ui
day: 9
priority: 3
depends_on: [T-Q3]
---

## Context

T-Q3 delivered the data layer: samples/index.json (3 real engine-built samples w/
engine-rendered thumbnails), regen tests, a 9-step headless tour script. T-Q1's
first-run dialog offers a "sample part" checkbox that currently has nothing to open.

## Driver notes (foreman)

- Panel on ui/imgui panel_scaffold (T-U2/U4 exemplars): thumbnail grid from
  samples/index.json, discipline badge, "what it teaches" line, Open action loading
  the sample doc via the worker; tour launcher for samples with a tour script.
- Wire first_run.h's sample checkbox → open gallery (or directly the bracket sample).
- ui_style_check.py stays clean; one-shot AI_CAD_SHOT=gallery scenario; headless
  --shot verify with EYEBALLED png (not just file-exists — see T-Q3 thumbnail lesson).

## Definition of done

- [ ] Gallery panel renders the 3 samples w/ real thumbnails headless
- [ ] Open loads the sample document via the worker (R1-editable tree visible)
- [ ] First-run checkbox path opens a sample on a fresh XDG profile
- [ ] Style check clean; graphify updated
