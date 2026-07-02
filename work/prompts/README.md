# Agent orchestration playbook

How to run the sprint with a fleet of local Claude CLI agents.

## Model tiering (token economics)

| Role | Model | Count | Does |
|---|---|---|---|
| Driver | Opus | one per active task (4-6 max) | owns a task end-to-end: design, core logic, reviews subagent diffs, DoD gate |
| Grunt | Sonnet 5 | spawned by drivers as subagents | searches, test scaffolds, boilerplate, mechanical refactors, lint — the bulk of tokens |
| Advisor | Fable | exactly one long-lived session | answers escalations, guards contracts, reviews seams, re-plans the DAG |

Drivers are told to delegate aggressively and to take back diffs, not transcripts —
that's what keeps Opus context (and cost) small. Configure your Claude Code subagents
(`.claude/agents/` or `/agents`) with `model: sonnet` for the grunt roles.

## Easiest: autopilot

Open Claude Code (Opus) in the main checkout and say **"start working"** — CLAUDE.md
routes that to work/prompts/autopilot.md. The agent claims an unclaimed ready task
atomically via `work.py claim` (lock + claim-commit on master, race-safe), works it
on its own branch/worktree, stops at review, and claims the next (2-task/session cap).
Start as many instances as you like — claims can't collide, and `work.py board` shows
every agent's holdings. Or launch explicitly:

```bash
claude --model opus "$(cat work/prompts/autopilot.md)"
```

## Starting a task manually (one driver)

```bash
# from the main checkout, see what's unblocked:
python3 work/tools/work.py ready

# launch a driver in its own worktree (never share a checkout between drivers):
git worktree add ../CAD-T-A1 -b task/T-A1-dag-contract master
cd ../CAD-T-A1
claude --model opus "$(python3 work/tools/work.py prompt T-A1)"
```

`work.py prompt <id>` assembles driver.md + the task file into one self-contained
work order (and warns if dependencies aren't done). The driver claims the task on
master, works on its branch, escalates questions to `work/questions/`, and stops at
`status: review` for a human merge.

## Starting the advisor (once)

```bash
cd <main checkout>
claude --model fable "$(cat work/prompts/advisor.md)"
```

Keep it running all sprint. When a driver writes a `work/questions/T-*.md`, paste it
(or its path) into the advisor session; commit the `## ANSWER` back and tell the
blocked driver to re-read the file.

## Launch waves

Wave 0 (human, this morning): T-S1 (API key applications) and resolve the
uncommitted docs/ deletions in the working tree. Everything hangs off T-S1.

Wave 1 (4 drivers in parallel — all contracts/scaffolds, zero file overlap):
T-A1, T-B1a, T-C1, T-D1.

After that, don't think in waves — run `work.py ready` whenever a driver finishes
and launch what it lists. The DAG serializes what must be serial (each workstream's
day-N depends on day-N-1); everything else runs in parallel. T-INT is last and
should be driven by whoever owns workstream A, with all four humans watching.

## Context-window management (why the system looks like this)

- **Task files are context packs.** Each contains everything a driver needs — context,
  file list, steps, DoD, AI hints — so drivers never read the master plan docs.
- **Worktrees isolate checkouts;** disjoint "Files / areas touched" lists isolate
  diffs; claim-commits on master serialize ownership. Merge conflicts should be rare
  and small.
- **HANDOFF protocol:** a driver near context exhaustion writes `## HANDOFF` into its
  task file, commits, exits. Relaunch the same `work.py prompt <id>` command — the new
  driver resumes from the task file, not from a transcript.
- **Escalations are files, not chats,** so decisions survive every context window and
  are reviewable in git history.

## Human loop (4 people)

Each person shepherds their workstream's drivers: relay escalations to the advisor,
merge `review` branches (read the driver's summary, spot-check the diff, run
`bash scripts/gate.sh`), flip tasks to done, launch the next ready task. Expect to
run 1-2 drivers each. You are the integrator of your lane — the agents do the
typing, you do the accepting.
