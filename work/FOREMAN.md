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
