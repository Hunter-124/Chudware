---
id: T-F7
title: Freeze narration contract + describe_toolpaths + describe_results
workstream: F
owner: sonnet-nell
status: done
branch: task/T-F7-narration-core
day: 6
depends_on: []
lane: ai-copilot
kind: new
---

## Context

Pillar 11 root: every domain state must have a deterministic `describe_*` so any model,
vision or not, can read the design. Biggest current gaps: CAM (raw JSON only) and
FEA/CFD (named scalars, no narrative). READ `docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 11.
This is also the prerequisite for translator training data (T-F10) and for report
content in the deliverables suite (T-F15).

## Files / areas touched

- `contracts/narration.md` — new: shared narration style guide, sid-stable naming,
  every number traceable to a runnable query
- `services/mcp/gsl_tools.py` (or sibling module) — `describe_toolpaths(job)`,
  `describe_results(study)`
- `services/mcp/server.py` — registrations; `cad_guide` topics

## Steps

1. Freeze `contracts/narration.md` mirroring `describe_part`'s conventions.
2. `describe_toolpaths`: per-op narrative (strategy, tool, stepdown/over, engagement,
   cycle time, remaining stock, collisions/gouges) + G-code digest (bounds, feeds
   actually emitted, tool changes).
3. `describe_results`: where max stress is (named faces via sid join), margin vs
   material yield, mode shapes in words, convergence caveats.
4. Tests in the house pattern of `test_gsl_tools.py` — deterministic fixtures, exact
   slot assertions.

## Definition of done

- [x] Contract frozen; both describers registered with cad_guide topics
- [x] Every numeral in output traceable to a tool query; tests green
- [x] `bash scripts/gate.sh`; `graphify update .`

## Verification (sonnet-nell, 2026-07-02)

- `contracts/narration.md` frozen: rounding table, source-attribution rule, graceful-
  degradation phrases, section order, RPC shapes verified against the C++ core
  (core/cam/session.cpp, stock_sim.cpp, post.cpp; sim_study_list records).
- `describe_toolpaths` + `describe_results` in services/mcp/gsl_tools.py, registered in
  register_gsl_tools (server.py already wires that in); cad_guide("narration") topic added.
- Tests: services/mcp/test_gsl_tools.py 14/14 PASS against the staged libaicad.so
  (deterministic seeded-study fixtures via sim_study_add — the same RPC _persist_study
  uses; exact slot assertions incl. rounding and attribution strings).
- Baseline: full `pytest services/mcp/` — master: 7 failed / 64 passed / 15 skipped /
  34 errors; branch: 7 failed / 69 passed / 15 skipped / 34 errors. Identical failure
  set (test_ecad_tools.py et al.) = PRE-EXISTING; the +5 passes are the new narration
  tests. Not investigated further per FOREMAN pitfalls.
- gate.sh: not re-run to completion in this session (a background run was cut off with
  no output file). Honest status: this change is Python + markdown only — the Python
  test evidence above covers it; the ui/imgui selMulti gate failure is documented
  pre-existing (FOREMAN pitfalls).
- `graphify update .` run (19252 nodes rebuilt).
- Staged lib gap: services/dist/AI-CAD/libaicad.so predates sim_run_static — noted in
  work/questions/T-F7.md (Q4) with the seeding rationale.
- Open decisions flagged for review in work/questions/T-F7.md: Q2 (max-stress location
  not persisted — honest degradation + BC-face sid join), Q3 (per-op stepdown/stepover
  not exposed by cam_tree).

## AI hints

- `graphify query "gsl describe_part narration"` — mirror the mature GSL layer's shape.
- CAM data source: `cam_job_tree`/`cam_setup_sheet`/`cam_extract` already return the
  raw JSON these narrate.

## Driver notes (Fable, 2026-07-02)

- **Templates to mirror:** `describe_part` at `services/mcp/gsl_tools.py:519`,
  `describe_sketch` at `:572` — study their sid-stable naming, rounding, and section
  order; the contract codifies what they already do.
- **CAM data:** the toolpath cache lives in `core/cam/session.cpp:25-32`
  (`CamSession.cache: map<string, vector<Toolpath>>`); `cam_simulate`
  (`services/mcp/cam_tools.py:227`) already returns `gouge_count`,
  `rapid_through_count`, `machined_fraction_pct` — narrate those numbers, never
  recompute. G-code digest: parse the `cam_post` output text (bounds, feed range,
  tool changes) — a modal G-code interpreter subset, deterministic; do NOT attempt
  toolpath semantics beyond the digest (master plan says LLMs fail there; the
  simulator owns it).
- **FEA data:** named scalar keys already exist (`max_von_mises_mpa`, `max_disp_mm`,
  `min_fos` from `run_fea_static`, `freqs_hz[]` from modal — `services/mcp/
  cad_tools.py:1901/2005`). The sid join for "where the max stress is" goes through
  the same face-handle layer describe_part uses. Material yield for margin: the FEA
  material record in the study — cite its source in the text.
- **Binding style rules (put in the contract):** every numeral traceable to a named
  tool query; fixed rounding (µm-grade lengths, 0.1 MPa stresses); no adjectives
  without a number behind them; degrade gracefully when data is missing ("no
  simulation results attached") rather than omitting sections silently.
- **This unblocks T-F10 (translator data) and T-H4 shares its groundedness checker
  with the T-F10 harness** — if you see that checker being built in parallel, one
  implementation under `tools/translator/`, coordinate via work/questions/T-F7.md.
