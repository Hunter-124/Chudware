# Foreman hot state (overwrite each wake — cheap re-orientation after compaction)

Updated: 2026-07-03 by fable-foreman → **WIND-DOWN MODE** (Hunter wants the wheel back).

## MODE: WIND-DOWN (Hunter directive, 2026-07-03)
Hunter is taking direct control of direction for the next stretch to steer the
ambitious, human-review-heavy work. Foreman job now:
- **NO new dispatch.** No wave 2, no new claims, no new task cuts except closeout bookkeeping.
- Let in-flight agents CONVERGE. Merge clean review branches; **park (don't re-dispatch)**
  anything that needs rework or whose driver dies. Goal = a QUIET board handed to Hunter.
- Keep answering questions (advisor role) but open no new fronts.

## Standing orders (still binding)
- Identity: commits author as Hunter-124. NEVER `git config user.*`, NEVER `whoami` — --owner explicit.
- Drivers stop at status: review; foreman merges. Icon atlases DERIVED — regenerate, never hand-merge.
- mooseinatree owns T-BL02 — never release/reassign/auto-claim; may be dark long.

## Merged this session
- **T-BL26** (MCP freshness audit), **T-BL23** (CAM scoring keys + manifold issues[] fix).
- Human-gated closed: T-BL19 (issue #8 filed, T-BL30 cut), T-G20 (KEEP ratified),
  T-G12 round-2 walkthrough (13/15 PASS, 0 regressions; nits→T-BL31), hover residual (6/7 PASS).

## In review (verify subagents running — merge or park on verdict)
- **T-BL20** V&V cross-validation (sonnet-doro, at review) — reviewing benchmark honesty + ccx subprocess.
- **T-BL25** doc IntakeLedger (heath DEAD usage-limit; rework pushed b7eaba0, foreman-owned) — verifying the 3 blockers fixed.
- **T-BL22** live seam wiring (eddy DEAD usage-limit; work pushed d7ab42e, foreman-owned) — verifying seams live + running gate.

## In flight (external drivers — let ride; park if they die)
- T-BL21 hollow sections (sonnet-quorix, active/pushed) · T-BL29 icon recipes (sonnet-lumen) ·
  T-G21 column collapse (sonnet-fenn, fresh redispatch) · T-BL27 replace-face heal (sonnet-lira,
  fix pushed but left at doing — told to flip-or-release; also double-claimed T-BL31) ·
  T-BL30 disjoint-boolean (quorix idle-claim — asked to release).

## Driver attrition this session (no work lost — push discipline held)
mira (T-G21, session limit, nothing pushed → redispatched fenn) · heath (T-BL25, usage limit,
rework pushed) · eddy (T-BL22, usage limit, work pushed). heath/eddy branches complete → verifying.

## AMBITIOUS QUEUE FOR HUNTER (the human-review-heavy work he wants to steer)
- **Workstream R — PLM / Pillar 19** (T-R1..R7): the next big product push. Starts with
  T-R1 (OSS survey + freeze contracts/plm.md — vault schema, lifecycle, lock/remote). Contract
  freeze = heavy human review; Opus-tier by precedent. Everything R2..R7 unblocks off R1.
- **Interop lane** (independent of mooseinatree): openNURBS/.3dm (MIT lib) + DXF (ezdxf) importers
  — clean-room, cuttable as Sonnet tasks when Hunter wants them. T-BL02 itself stays mooseinatree's.
- **Offline ML tracks**: deferred (WORKFLOW.md §3) — capture/eval seams shipped, training parked.

## Human-gated (waiting on Hunter)
- **sketch_new label call** (round-2): keep icon-only [ribbon.cpp ribbon_displayLabel L134 intentional]
  vs restore label — gates T-G12 round-3 convergence.
- T-G12 round-3 (a few live-only checks: Copilot popups, T-G2 click-tests, window borders, NEW-1 live extrude).
- T-G11 Windows build verify + host/ deletion (his Windows box) · T-S1 vendor API applications.
- T-BL28 fix-direction ratified fix-the-forms (opus-vela driving) — no action unless he disagrees.

## Verification recipes
- pytest: `AI_CAD_LIBAICAD=$PWD/core/build-linux/bin/libaicad.so ~/.local/share/AI-CAD/mcp-venv/bin/python -m pytest <dirs> -q`
- core: `flock /tmp/host-build-linux.lock bash -c 'cmake --build core/build-linux -j4 && cd core/build-linux && ctest -R <re>'`
- Full-sweep cross-suite pollution (~27F/44E) PRE-EXISTING — verify per-suite. Ignore collection errors:
  engine_oracle/doc/test_aicm.py, engine_oracle/sim/test_foam_case.py, engine_oracle/sim/test_lbm.py, services/agent/test_loop_mock.py.
- Headless screenshots: `--ui-screenshot` NOT `--shot`. Atlases DERIVED — regenerate via pipeline.
