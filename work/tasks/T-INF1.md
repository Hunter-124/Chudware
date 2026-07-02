---
id: T-INF1
title: Fix gate pytest stage (issue 6) - wire mcp-venv + AI_CAD_LIBAICAD into scripts/gate.sh
workstream: V
owner: sonnet-orin
status: review
branch: task/T-INF1-gate-pytest
day: 3
priority: 2
depends_on: []
---

## Context

GitHub issue #6: gate stage 4 discovers the system python (no deps) so everything
deselects/0-collects and the stage FAILs while the REAL suites are green in the
mcp-venv. Every driver has been trained to shrug at a red gate stage — that
normalization is how real breakage will eventually slip through. The manual
workaround every agent types by hand:
`AI_CAD_LIBAICAD=$PWD/core/build-linux/bin/libaicad.so ~/.local/share/AI-CAD/mcp-venv/bin/python -m pytest ...`

## Driver notes (foreman)

- Make scripts/gate.sh stage 4 use the mcp-venv python when present (fall back to
  current behavior + a loud SKIP note when absent) and export AI_CAD_LIBAICAD to
  the built core .so.
- Scope the target dirs to match the foreman sweep: services engine_oracle tools
  training, with the four known pre-existing collection-error files ignored VIA
  EXPLICIT --ignore flags and a comment naming the tracking audit (T-V2), so the
  ignores are visible debt, not silence.
- Also fix core_bridge._lib_candidates to include core/build-linux/bin/ (the
  root cause of the T-F8 all-skip, filed on the same issue).
- DoD: `bash scripts/gate.sh` on a clean master shows the pytest stage GREEN with
  a real executed count (>1400 collected), and the gate summary line documents
  what was ignored. check.py stage reds stay as-is (separate pre-existing).
- Do not "fix" the sweep-pollution failures here (that's T-V2's audit) — if the
  stage can only go green suite-by-suite, run it as a small ordered set of pytest
  invocations, one per top-level dir, and record that choice in the script.

## Definition of done

- [ ] Gate pytest stage runs the real venv + engine and goes green on master
- [ ] core_bridge finds core/build-linux/bin/libaicad.so without the env var
- [ ] Issue #6 closed with the root-cause writeup
- [ ] `graphify update .` run
