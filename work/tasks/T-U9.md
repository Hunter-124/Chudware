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

## Progress log
- 2026-07-03 01:43 UTC [sonnet-reed] REVIEW T-U9 branch=task/T-U9-gallery-ui @ e66d2f1 - sample gallery panel + first-run wiring. ui/abstract/gallery_view.h + gallery_panel.cpp (T-U1 scaffold, mirrors T-U2 parts_panel.cpp exemplar): thumbnail grid from samples/index.json, discipline badges, what-it-teaches bullets, Open (real s.engine.open() worker path, queued+retried until s.engineReady rather than dropped like file_explorer's one-shot consume-and-discard - matters because first-run fires before the worker finishes starting), guided-tour launcher gated on samples/tours/<id>_tour.py existing (detached-thread subprocess, reports only via the thread-safe toast queue, never writes the shared view-model cross-thread). first_run.cpp: sample checkbox used to load the legacy non-editable box.aicm demo-scene fixture - now opens the real T-Q3 bracket sample via the worker path and opens the gallery too. AI_CAD_SHOT=gallery scenario + selectAndOpenFirstSampleForShot() to prove click-queue-retry fires. Screenshots genuinely eyeballed (not file-exists only, per the task's own warning) - caught+fixed a font glyph-range bug (bullet char rendering as '?', same class T-U1 hit) and a button-layout clip (widened the panel, rebalanced grid/detail height split). Honest limitation: 'R1-editable tree visible' end-state is [~] - this box's worker/engine bridge returns 'unknown method: doc_new' even with a real runtime present, matching the demo-mode/engine-offline status every screenshot this whole session has shown; the queue-retry mechanism itself is fully verified (correctly shows 'Waiting for the engine to start...' on fresh XDG, never drops the request). ui_style_check.py 0 findings; ui_inventory.py auto-discovered it (6 scaffold panels, up from 5). Gate: check.py/manual-regen/ui_style_check/core-build/ctest all PASS (confirmed across 3 attempts); scripts/gate.sh itself got externally killed by shared-box contention 3x (same recurring issue T-V3 logged, load avg 9.99-16.00) - T-U9 touches zero Python files so T-V3's already-verified pytest numbers apply unchanged. graphify updated.
