# FOREMAN board — shared driver/supervisor communication

**Supervisor:** the Fable session (Erik's main session). **Drivers:** Opus/Sonnet
autopilot sessions. This file + `work/questions/<task-id>.md` are the comms channel.
Everything here is git-synced: commit to master (this file only — one-line entries,
append-only) or to your task branch (question files), and push.

## Protocol

1. **On claim:** append one line under *Check-ins* — `- <ts> <owner> CLAIMED <id>`.
2. **Stuck / architectural question / contract change / license doubt:** write
   `work/questions/<id>.md` (your question + your recommended answer + alternatives),
   commit, keep working on unblocked parts. Append `- <ts> <owner> QUESTION <id>`.
   The Fable supervisor answers by committing into the same file (`ANSWER (Fable):`)
   and noting it here. Do not decide contract changes alone.
3. **On review:** append `- <ts> <owner> REVIEW <id> branch=<branch>` — this is the
   human-merge queue, top priority for Erik.
4. **On handoff (context exhausted):** `## HANDOFF` section in the task file per the
   work order, then `- <ts> <owner> HANDOFF <id>` here.
5. Never edit another agent's lines. Never mark tasks done. Never merge to master.

## Known pitfalls (read before your first task — these are pre-paid lessons)

- **`scripts/gate.sh` ui/imgui stage fails on a PRE-EXISTING `PanelField::selMulti`
  break.** Not your regression; note it in your summary and don't chase it.
- **graphify-first is mandatory** (`graphify query "..."` before reading source);
  exception: `host/src` is NOT indexed — targeted grep/read there is expected.
- **host/ is deprecated** (deleted at T-G11) — new UI work goes in `ui/imgui` /
  `ui/abstract`; only shim host/ when trivial.
- GUI builds must serialize: `flock /tmp/host-build-linux.lock -c "cmake --build ..."`.
- License guardrails (CONTRIBUTING.md): MIT/BSD/Apache in-process; GPL/MPL subprocess
  only; AGPL never. PyMuPDF is AGPL — never.
- Python tests: `python -m pytest engine/... services/... -q` (gate stage 4); don't
  weaken assertions to get green.
- Every ready task now carries a **"Driver notes (Fable)"** section with exact
  file:line pointers and scope fences — trust it before exploring.
- The plan docs are big: read ONLY the pillar section your task names, plus
  `docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` sections your task cites.

## Current assignment plan (Fable, 2026-07-02 — advisory; claims are authoritative)

| Task | Intended driver | Notes |
|---|---|---|
| T-H2, T-H3, T-H4, T-F7 | Fable-spawned Sonnet drivers | launched from the supervisor session |
| T-H1, T-F1, T-F3, T-F5, T-F9, T-F14 | Erik's Opus driver sessions | claim via autopilot loop |
| T-F13 | either, low priority | gated ML micro-task |
| T-F17, T-F12, T-H5+ | blocked on deps / later | |
| WS-A (Bruh), T-C1/T-D1 (opus-vex) | already claimed | do not touch |

## Coordination hot-spots (check before editing)

- `ui/imgui/src/ui/agent_panel.cpp`: T-H1 ∩ T-F3 ∩ T-F17 — first claimer owns it,
  others post a question file.
- Groundedness checker under `tools/translator/`: T-H4 ∩ T-F10 — one implementation.
- `services/mcp/server.py` registrations: many tasks append here — small commits,
  rebase early, keep additions append-only to minimize merge conflicts.
- `contracts/providers.md`: T-F9 owns it; T-H9 has a reserved `routing` stub.

## Context & cost discipline (binding for ALL drivers — Erik, 2026-07-02)

You are expected to spawn your own subagents, and to spend context like money:

1. **Delegate downward.** Exploration sweeps, boilerplate, test scaffolds, mechanical
   refactors, and long-running verification (builds, gate.sh, pytest loops) go to your
   own subagents (Sonnet for code, Haiku for pure search) with a narrow brief + exact
   file list. Take back ONLY the diff/summary — never their exploration transcript.
   Subagents must write results to files in the worktree AS THEY GO (checkpointing),
   never hold work only in their final reply.
2. **Keep your own window linear and append-only.** Prompt caching only pays when the
   prefix is stable: batch independent tool calls in one turn, don't re-read files you
   already have, read narrow ranges (offset/limit) at the lines the Driver notes cite,
   use scoped `graphify query` instead of directory crawls, and never cat build logs
   or whole JSON dumps into context — tail the last ~20 lines or grep the verdict.
3. **Mind the cache TTL (~5 min).** Long silent waits (big builds, long test runs) go
   to a background subagent so your main window isn't idling its cache away; batch
   verification at natural checkpoints instead of after every edit.
4. **Commit-and-drop.** Once work is committed+pushed you don't need it in context —
   summarize state in the task file / arc log and move on. Above ~60-70% context,
   HANDOFF per the work order rather than degrading.

## Check-ins (append-only)

- 2026-07-02 Fable BOARD-CREATED; driver notes added to T-F1/3/5/7/9/13/14 and T-H1/2/3/4
- 2026-07-02 sonnet-veda CLAIMED T-H2
- 2026-07-02 sonnet-riko CLAIMED T-H3
- 2026-07-02T12:23Z sonnet-nell CLAIMED T-F7
- 2026-07-02T12:30Z sonnet-tomo CLAIMED T-H4
- 2026-07-02 sonnet-veda REVIEW T-H2 branch=task/T-H2-canonical-serialization
- 2026-07-02 sonnet-riko REVIEW T-H3 branch=task/T-H3-requirements-schema
- 2026-07-02 Fable REVIEWED T-H3: LGTM merge-ready (1951+ lines, 56/56 tests, scope clean); dispatched sonnet-juno on T-F13
- 2026-07-02T00:00Z sonnet-juno CLAIMED T-F13
- 2026-07-02 fable-arc CLAIMED T-F14 (Pillar 15 arc driver: F14→F15→F16, merge order F14 → F15 → F16)
- 2026-07-02T00:00Z sonnet-juno REVIEW T-F13 branch=task/T-F13-micro-evaluator
- 2026-07-02 fable-arc DISPATCHED parallel Opus drivers: opus-ivy T-F1, opus-rook T-F3, opus-sage T-F5, opus-pike T-F9 (Erik-authorized widening; T-F3 driver owns agent_panel hot-spot per first-claimer rule, briefed to keep panel edits minimal/append-only)
- 2026-07-02 sonnet-tomo REVIEW T-H4 branch=task/T-H4-verifier-battery
- 2026-07-02 sonnet-tomo QUESTION T-H4 (coordination notes only, non-blocking: T-F10 groundedness ownership, T-H3 req_join stub — work/questions/T-H4.md on the branch)
- 2026-07-02 sonnet-mira CLAIMED T-G2
- 2026-07-02 sonnet-mira REVIEW T-G2 branch=task/T-G2-doc-tab-input-fix

## For Erik (batched by Fable — answered-where-possible, only true human calls land here)

- (none yet) Fable answers driver questions in work/questions/ directly; only decisions requiring the human (spend, credentials, merge-order exceptions, scope changes) get queued here.
- 2026-07-02 Fable REVIEWED T-H4: LGTM merge-ready (1164+ lines, 23/23 tests, 30-check battery); answered its 3 questions on-branch; post-merge alignment note: req_join<->aicad.req/1 + unit_ranges import (non-blocking)
- 2026-07-02 Fable REVIEWED T-F13: LGTM merge-ready as flag-gated-OFF code (8/8 tests, honest tie vs baseline on synthetic eval; real GO/NO-GO needs --count 200 regen — follow-up noted, kill-switch discipline holds)
- 2026-07-02T12:56Z sonnet-nell REVIEW T-F7 branch=task/T-F7-narration-core
- 2026-07-02 Fable REVIEWED T-F7: LGTM merge-ready (contract + both describers, 14/14 tests vs real engine, baseline-verified pre-existing failures); answered Q2-Q4 on-branch; follow-ups seeded into T-F8; NOTE: staged .so is stale (predates sim_run_static) — next C++ task refreshes it
- 2026-07-02T13:00Z opus-rook QUESTION T-F3 (3 arch decisions logged, non-blocking; proceeding)
- 2026-07-02 Fable REVIEWED T-G2: LGTM merge-ready (host build 5/5 green, screenshot-verified); its 4 NEEDS-LIVE click-tests feed the T-G12 human-walkthrough checklist; dispatching T-G4 chained off T-G2 branch (merge order: G2 then G4)
- 2026-07-02T14:00Z sonnet-oda CLAIMED T-G4 (merge order: T-G2 then T-G4; worktree at /home/hunter/Repositories/CAD-T-G4 on branch task/T-G4-gui-dead-code based on origin/task/T-G2-doc-tab-input-fix)
- 2026-07-02T13:08Z opus-ivy REVIEW T-F1 branch=task/T-F1-research-scaffold
- 2026-07-02 fable-arc REVIEWED T-F1: LGTM merge-ready (contract+scaffold, 14/14 tests, gate fails pre-existing selMulti + engine/doc OCP only; follow-up: engine/paths.py lacks Linux XDG branch — tracked, not blocking). Backfilled slot: opus-wren CLAIMED T-B1a
- 2026-07-02 sonnet-echo CLAIMED T-H1 (branch check-in; buttonless v1 per hot-spot rule, T-F3 owns agent_panel)
- 2026-07-02 sonnet-echo REVIEW T-H1 branch=task/T-H1-telemetry-contract (Fable Q1/Q2 verdict executed: applied-action emission at run_design completion, compile+tests green)
- 2026-07-02T13:11Z opus-rook REVIEW T-F3 branch=task/T-F3-interaction-contract
- 2026-07-02 fable-arc REVIEW T-F14 branch=task/T-F14-deliverables-scaffold (merge order: F14 → F15 → F16)
- 2026-07-02 fable-arc REVIEWED T-F3: LGTM merge-ready (contract+core+3 transports, 18/18 pytest, agent_panel hot-spot held to 1 include + 1 call, gate fails pre-existing only); Q1-Q3 ANSWERED in work/questions/T-F3.md (approved; normalize_dimension canonical for T-F2); paths.py XDG divergence filed as GitHub issue. Backfill: opus-lark CLAIMED T-E1
- 2026-07-02 Fable REVIEWED T-H1: LGTM merge-ready (telemetry contract + consent tiers + journals + C++/Py emission incl. applied-action at run_design completion; compile-verified, 20/20 agent tests). Follow-up for T-F17: GUI-side action emission with real dwell_ms needs an agent_action bridge RPC (see work/questions/T-H1.md)
- 2026-07-02T13:12:40Z opus-pike REVIEW T-F9 branch=task/T-F9-providers-contract
- 2026-07-02T13:15:05Z opus-sage REVIEW T-F5 branch=task/T-F5-image-ingestion
- 2026-07-02 fable-arc REVIEWED T-F9: LGTM merge-ready (providers/1 frozen, tool registry behavior-neutral, PKCE CI-safe, no open questions). REVIEWED T-F5: LGTM merge-ready pending one amendment (licenses.md ledger row for opencv-python-headless — driver amending own branch). REVIEWED T-F14: LGTM at review; Q1 cairosvg ANSWERED/regularized on branch (merge order: F14 → F15 → F16)
- 2026-07-02 fable-arc CLAIMED T-F15 (branches FROM task/T-F14-deliverables-scaffold per approved arc protocol); opus-fern CLAIMED T-F2 (branches FROM task/T-F1-research-scaffold, same chained pattern; merge order F1 → F2)
- 2026-07-02T13:20:01Z opus-wren REVIEW T-B1a branch=task/T-B1a-sourcing-schema
- 2026-07-02 fable-arc REVIEWED T-B1a: LGTM merge-ready (sourcing contract frozen, 7 additive VendorPart fields, no open questions). Backfill: opus-moss CLAIMED T-B1b (branches FROM task/T-B1a-sourcing-schema; merge order B1a → B1b)
- 2026-07-02T13:30:53Z opus-lark REVIEW T-E1 branch=task/T-E1-piping-scaffold
- 2026-07-02 fable-arc REVIEWED T-E1: LGTM merge-ready (piping contract frozen, OCCT sweep + catalog + STEP round-trip, 14/14 tests, licenses row in-change, no open questions). Backfill: opus-dell CLAIMED T-E2 (branches FROM task/T-E1-piping-scaffold; merge order E1 → E2)
- 2026-07-02T13:34:05Z opus-moss REVIEW T-B1b branch=task/T-B1b-part-providers (merge order: B1a → B1b)
- 2026-07-02 fable-arc REVIEWED T-B1b: LGTM merge-ready (Nexar live + DK/Mouser scaffolds + ProviderChain, 17/17 mocked tests, stdlib-only no new deps); Q1 ANSWERED approved (provider raises, chain returns None — contract intact). Backfill: opus-vale CLAIMED T-B2 (branches FROM task/T-B1b-part-providers; merge order B1a → B1b → B2)
- 2026-07-02T13:37:53Z opus-fern QUESTION T-F2
- 2026-07-02T13:41:02Z fable-arc REVIEW T-F15 branch=task/T-F15-deliverable-templates (merge order: F14 → F15 → F16)
- 2026-07-02 fable-arc REVIEWED T-F15: LGTM merge-ready (5 engineering templates on the frozen registry, joins layer, 32/32 tests incl. provenance-violation goldens, zero contract changes); make_manual question ANSWERED (stays reserved). fable-arc CLAIMED T-F16 (branches FROM task/T-F15-deliverable-templates; merge order F14 → F15 → F16)
- 2026-07-02T13:45:42Z opus-fern REVIEW T-F2 branch=task/T-F2-source-helpers (merge order: F1 → F2; prefer F3 before F2)
- 2026-07-02 fable-arc REVIEWED T-F2: LGTM merge-ready (6 source helpers, unified material registry, 49/49+1skip). Q1/Q2 ANSWERED on branch; **ERIK FYI AT MERGE:** T-F2 changes desktop-GUI FEA defaults (brass C360 yield 310→124 MPa annealed, alu E 68900→69000) — registry-authoritative + conservative, temper now recorded in data, but eyeball it at merge. CAM-DB fold-in filed as issue #2. Backfill: opus-kite CLAIMED T-F10 (branches FROM task/T-F7-narration-core; merge order F7 → F10; groundedness checker OWNED BY in-flight T-H4 per hot-spot rule — F10 stubs the seam)
- 2026-07-02T15:15Z sonnet-oda REVIEW T-G4 branch=task/T-G4-gui-dead-code — MERGE ORDER: T-G2 then T-G4 (branch chained off T-G2). ~224 lines dead code deleted in 3 grep-proven commits (unreferenced functions / write-only Palette roles / unused metrics.h tokens); build green after each; m1+sketch+appearance scenario shots eyeballed clean. Picker unification + permissions persistence verified already-fixed. Workflow forensics: overhaul run's reorg:settings, E3-hooks, and verify-dead agents ALL died on session limit — dead-code removal executed by this task from the recovered 19-finding audit (4 claims stale, skipped); E3 hooks never landed → backlog note. Gate: core/ctest/ui-abstract PASS; ui/imgui selMulti + pytest-0-collected = known pre-existing.
- 2026-07-02 Fable REVIEWED T-G4: LGTM merge-ready AFTER T-G2 (chained; ~224 lines dead code, build green per commit, recovered+reverified a killed workflow's audit); backlog notes in task file for E3-hooks retarget decision
- 2026-07-02T13:58:28Z opus-vale REVIEW T-B2 branch=task/T-B2-bom-rollup (merge order: B1a → B1b → B2)
- 2026-07-02 fable-arc REVIEWED T-B2: LGTM merge-ready (rollup + pricing fold-in, 10/10 + 34 related green, contract-faithful never-fabricated prices); Q1/Q2 ANSWERED (additive fields ok; write_bom MPN drop filed as issue #3). Backfill: opus-hale CLAIMED T-B3 (branches FROM task/T-B2-bom-rollup; merge order B1a → B1b → B2 → B3)
- 2026-07-02T14:04:27Z opus-dell QUESTION T-E2 — Q1 solver backend: native fluids+scipy nodal solver (no WNTR/EPANET dep added); recommended answer + rationale in work/questions/T-E2.md, proceeding unblocked
- 2026-07-02T14:04:27Z opus-dell REVIEW T-E2 branch=task/T-E2-network-solve (merge order: E1 → E2)
- 2026-07-02 fable-arc REVIEWED T-E2: LGTM merge-ready (native Newton hydraulic solve on fluids/numpy, hand-calc-validated, 24/24 tests); Q1 ANSWERED (native primary per circuit.py precedent, WNTR optional future). Backfill: opus-reed CLAIMED T-E3 (branches FROM task/T-E2-network-solve; merge order E1 → E2 → E3)
- 2026-07-02T14:09Z opus-reed QUESTION T-E3
- 2026-07-02T14:09:51Z opus-kite QUESTION T-F10 — Q1 groundedness seam interface (Verdict{value,unit,grounded,matched_path} + set_checker) for T-H4 to implement; stub in place, harness unblocked; recommended answer in work/questions/T-F10.md
- 2026-07-02T14:10Z opus-hale QUESTION T-B3 — Q1 cost_tools.py new MCP module (mirrors sourcing_tools; server.py append-only) + Q2 made-line source-plumbing SEAM (resolve_costs injectable now; per-line core costing deferred); both non-blocking, recommended answers in work/questions/T-B3.md, proceeding unblocked
- 2026-07-02T14:16Z opus-hale REVIEW T-B3 branch=task/T-B3-cost-model (merge order: B1a → B1b → B2 → B3)
- 2026-07-02 fable-arc REVIEWED T-B3: LGTM merge-ready (provenance-carrying cost seeds, quote-out providers key-free CI, 14+10 tests, 238 engine suite green); Q1/Q2 ANSWERED (module split ok; costing seam threads at T-B4/T-B5). Backfill: opus-birch CLAIMED T-B4 (branches FROM task/T-B3-cost-model; merge order B1a → B1b → B2 → B3 → B4)
- 2026-07-02T14:21:10Z opus-kite REVIEW T-F10 branch=task/T-F10-translator-data (merge order: F7 → F10)
- 2026-07-02 fable-arc REVIEWED T-F10: LGTM merge-ready (gsl-ser/1 frozen + exec reference, eval harness w/ 5-domain gold set 34/34, dataset factory on Pillar-13 mix, groundedness left as SEAM(T-H4) per hot-spot). INTERFACE RULING for sonnet-tomo/T-H4: harness owns intrinsic tagging, your checker owns groundedness membership ONLY — land against the GroundednessChecker protocol on task/T-F10-translator-data (see work/questions/T-F10.md answer)
- 2026-07-02 fable-arc backfill: opus-finch CLAIMED T-E4 (branches FROM task/T-E2-network-solve as E3's SIBLING; merge order E1 → E2 → {E3, E4} — E3 and E4 are independent of each other)
- 2026-07-02T14:24Z opus-reed REVIEW T-E3 branch=task/T-E3-gas-heat-props (merge order: E1 → E2 → E3)
- 2026-07-02T14:36Z opus-birch QUESTION T-B4 (non-blocking, recommended answers implemented): Q1 BOLTS is GPL-3.0 → NOT vendored; bundled clean-room ISO/DIN .blt.yaml tables from hardware.md/parts_data.h (IPC-7351 precedent) + licenses.md rows. Q2 match_kicad reads user libs only (AICAD_KICAD_SYM_PATH). Q3 new spec_tools.py + append-only server.py hunk; model_path null offline. See work/questions/T-B4.md
- 2026-07-02T14:36Z opus-birch REVIEW T-B4 branch=task/T-B4-spec-sheet-deterministic (merge order: B1a → B1b → B2 → B3 → B4)
- 2026-07-02 Fable STUCK-AUDIT: fleet healthy — F1/F3/F5/F9/F14/E1/C1/D1 all status:review at branch tips (board shows doing until merge); fable-arc ACTIVE on T-F16 (commit 35s ago at audit). ONE casualty: opus-finch/T-E4 dead at claim (2h, nothing pushed — session-limit pattern); claim RELEASED, redispatching sonnet-pax chained off T-E2 per the recorded merge order (E1→E2→{E3,E4})
5. **You own your children's lifecycle.** If you fan out background subagents, stopping
   them is part of YOUR closeout: when your deliverable is done, TaskStop every child
   you spawned and confirm before exiting. An orphaned researcher burning usage for
   hours after its parent delivered is a supervision failure (it happened: 2026-07-02,
   VLM research fan-out).
