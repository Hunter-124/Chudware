# Supervisor handoff — for the OPUS successor foreman (updated 2026-07-02, Fable v2)

You are likely an Opus session taking over when Erik's Fable tokens ran out. Read this +
[FOREMAN.md](FOREMAN.md) (protocol, pitfalls, discipline rules, full ledger + MERGE PLAN)
before doing anything. Memory dir has `subagent-supervision-lessons`,
`pillar16-flywheel-decisions`, `gate-selmulti-blocker` (now RESOLVED) — recall them.

## Your role

FOREMAN, not builder: dispatch drivers (Sonnet builds; you review), review branches to
REVIEW verdicts, answer `work/questions/<id>.md` with committed `ANSWER (Fable):` blocks
(keep the label for ledger continuity), enforce closeout checklists, batch true
human-only calls into FOREMAN's "For Erik". Audit ground truth via git before believing
anything (`git show origin/<branch>:work/tasks/<id>.md | grep status` — board shows
`doing` until merge).

## State at this handoff

- **~30 branches at status:review.** The complete one-pass **MERGE PLAN for Erik** is in
  FOREMAN.md (orders, the T-F2 FEA-defaults eyeball flag, keep-all-lines server.py rule,
  H5/H7 `tools/flywheel/__init__.py` keep-both, T-G5 status conflict).
- **Master builds clean** since `b569020` (the selMulti break was a half-landed commit,
  fixed; ui/imgui gate failures are now REAL). T-A1 proved a master-based branch passes
  the ui/imgui gate.
- **All wave-2 tasks reviewed LGTM:** F8, H5, H7, E5, F6, A1 (all six survived a host
  process exit via the finisher pattern — see playbook), F16 (Pillar 15 arc complete).
- **In flight, NOT yours:** opus-vex on T-B5, opus-vale on T-G6 (dedicated flock'd GUI
  integrator), Erik's own Opus CLI lanes on T-C2 / T-D2 / T-H8 (prompts already issued —
  transcripts in this session's log if they need reissuing).
- **Next dispatches when deps land:** T-A2 → T-A3 → T-A4 (chain off task/T-A1-dag-schema;
  A3 owns the libaicad.so restage), then T-F4/T-F17/T-F12 (open after A3/A4), T-H6 (after
  H5 merges), T-H9/H10/H11 (after H8). T-G7+ stay with opus-vale's lane. T-F11 is
  ship-gated — Erik call.

## THE POINT OF THIS HANDOFF: every hard task is pre-paid

All nine remaining hard tasks carry a **"Driver notes (Fable)"** section with binding
architecture rulings — dispatch is mechanical; drivers must trust the notes:

- **T-F17**: agent_action bridge = worker-RPC per the T-F16 precedent (NO GUI→core/agent
  linkage); one event vocabulary (`contracts/agent_events.md` binding T-H1 telemetry +
  aicad.dag/1 events); inherits the deferred panel buttons + inline artifact card unless
  T-G7 took them.
- **T-H9**: Erik's ratified promotion gate encoded (shadow-win on verifier-score AND
  accept-rate + Erik manual sign-off per promotion; NO auto-promote; rollback = table
  flip). Fills the reserved `routing` stub in contracts/providers.md.
- **T-H10**: model choices LOCKED (Qwen3-VL-4B primary); v0 = bench+corpus+zero-shot
  ONLY, no training (RECOGNITION_RESIDUAL discipline).
- **T-H11**: frontier-first through T-H7 logging; binary verifier = rebuild passes AND
  compare_parts ≈100% unaffected; migration criteria frozen before training.
- **T-H6**: extend T-H5's docker-compose + dvc_init helpers; work.py claim idiom; no
  orchestrator deps.
- **T-A2/A3/A4, T-F12**: bound to the frozen aicad.dag/1 seams; A4's verify dispatch is
  reused by F12's design_review.

## Dispatch playbook (proven ×2 today)

Spawn general-purpose agents, model sonnet, run_in_background, one per task. Prompt must
include: agent name `sonnet-<syllable>`; work order via `python3 work/tools/work.py
prompt T-XX`; FOREMAN.md binding; manual chained claim when deps are review-not-done
(owner+status:doing on master + CLAIMED check-in with merge order; worktree from the
predecessor branch); **WORK INLINE — foreground subagents one at a time, collected same
turn, NEVER end a turn waiting** (still the #1 failure mode — 4 of 6 drivers stalled
this way at first checkpoint; a firm resume message fixes it every time); commit+push
~20 min; literal closeout checklist (gate → graphify update → honest DoD → status:review
→ push → FOREMAN check-in → report); escalate via question files; never merge/done.

**Finisher pattern (new, proven ×6):** when a driver dies (session limit / process
exit), its pushed commits + worktree survive. Dispatch a FINISHER: assess git log +
uncommitted worktree state, salvage coherent work, fill genuine gaps, run the closeout.
Do NOT restart the task.

**All-skip trap (new):** a suite that skips 100% reads as green. Require REAL executed
test counts at review; in worktrees the .so lives at `core/build-linux/bin/libaicad.so`
— `AI_CAD_LIBAICAD=$PWD/core/build-linux/bin/libaicad.so mcp-venv/bin/pytest ...`
(root cause filed as a GitHub issue; check if fixed).

## Erik's ratified decisions (2026-07-02 — binding, do not re-ask)

1. WS-A opened to agents (Bruh reservation lifted).
2. T-H5/H6 infra: DVC remote = local file:// default; Argilla+MLflow = docker on this PC.
3. T-H9 promotion = shadow-win + Erik sign-off each promotion.
4. T-G12 walkthrough is RECURSIVE until a round yields zero new must-fix findings; T-G9
   sign-off formally gated on convergence.
5. Standing: capture-now-train-later; job-level fleet distribution only; team-dogfood
   corpus; R1 editability parity; Qwen3-VL-4B offline-VLM primary.

## Open for Erik (batch, don't nag)

The merge session (biggest lever — everything compounds on it); T-G12 round 1 after GUI
merges; E3-hooks retarget decision (T-G4 backlog note); DVC NAS path + `docker compose
up` when he wants live Argilla/MLflow; T-F11 ship gate; issue #1 stale-branch cleanup
post-merge.

## Post-merge steps (after Erik merges)

Resolve trivial server.py append conflicts (keep all lines) if asked; then on master:
`python3 work/tools/work.py dag && graphify update .`; dispatch the newly-ready wave
(T-A2 first — it unblocks the most); land the recorded alignment commits (req_join ↔
requirements constants; T-H3 imports unit_ranges.RANGES; T-H4 groundedness conforms to
T-F10's GroundednessChecker protocol per work/questions/T-F10.md).
