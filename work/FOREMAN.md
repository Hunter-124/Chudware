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
- 2026-07-02 fable-arc DISPATCHED parallel Opus drivers: opus-ivy T-F1, opus-rook T-F3, opus-sage T-F5, opus-pike T-F9 (Erik-authorized widening; T-F3 driver owns agent_panel hot-spot per first-claimer rule, briefed to keep panel edits minimal/append-only)

## For Erik (batched by Fable — answered-where-possible, only true human calls land here)

- (none yet) Fable answers driver questions in work/questions/ directly; only decisions requiring the human (spend, credentials, merge-order exceptions, scope changes) get queued here.
