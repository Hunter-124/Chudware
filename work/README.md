# work/ — git-tracked task coordination

Lightweight, file-based task tracking so 4 people + AI agents can work in parallel
without overlapping. Everything is markdown in git — no external tracker.

## Layout

```
work/
  tasks/     sprint tasks (T-A1..T-INT) — one file per task
  backlog/   post-sprint items (T-BL*) and future-sprint workstreams
  tools/     work.py CLI (stdlib only)
  DAG.md     generated dependency graph (mermaid) — do not edit by hand
```

## Task lifecycle

`todo → doing → review → done` (plus `blocked`, and `backlog` for unscheduled items).

## How to work

1. **Find a task:** `python3 work/tools/work.py ready --owner <you>` lists todo tasks
   whose dependencies are all done.
2. **Claim it:** edit the task file — set `owner:` to your name and `status: doing` —
   in a tiny commit pushed straight to `master`. Claims never go through branches, so
   two people can't silently grab the same task.
3. **Branch:** create the branch named in the task's `branch:` field
   (`task/<id>-<slug>`), from fresh `master`.
4. **Do the work:** paste the task file into your AI agent as the work order. Stay
   inside the task's "Files / areas touched" list; log discovered work as a new task
   file instead of scope-creeping.
5. **Done means done:** every checkbox in "Definition of done" checks, `bash
   scripts/gate.sh` passes, `graphify update .` run.
6. **Merge:** PR to `master`, set `status: done` in the task file within the same
   merge. Branches live days, not weeks.

After any status change, regenerate the graph: `python3 work/tools/work.py dag`
and commit `work/DAG.md`.

## CLI

```
python3 work/tools/work.py validate   # schema + dependency-cycle check (CI-able)
python3 work/tools/work.py ready      # what can start now
python3 work/tools/work.py board      # full status board by workstream
python3 work/tools/work.py dag        # regenerate work/DAG.md
python3 work/tools/work.py claim --owner NAME [--task ID]  # atomically claim next ready task
python3 work/tools/work.py prompt ID  # emit a self-contained agent work order
```

For running fleets of local agents against this system (autopilot mode, model
tiering, escalation), see [prompts/README.md](prompts/README.md).

## Adding a task

Copy an existing file. Front matter must stay flat (key: value, inline lists):

```
---
id: T-X9
title: Short imperative title
workstream: A|B|C|D|E|G|integration|setup|backlog
owner: unassigned
status: todo
branch: task/T-X9-slug
day: 3            # optional
depends_on: [T-X8]
---
```

Body sections: Context, Files / areas touched, Steps, Definition of done, AI hints.
Run `validate` before committing.

## Owners

| Workstream | Area | Owner |
|---|---|---|
| A | DAG orchestrator + integration | Bruh |
| B | Sourcing / cost / spec-sheet | _teammate 1_ |
| C | Firmware / embedded | _teammate 2_ |
| D | Civil / structural | _teammate 3_ |
| E | Piping / hydraulics (sprint 2) | _unassigned_ |
| G | GUI: polish, ShopCraft rename, ui/imgui migration, host/ deletion | Erik + agents |

## Special tasks: `owner: main-checkout`

A few tasks (T-G1, T-S2) land verified-but-uncommitted work that only exists in the main
checkout's dirty working tree. They keep `owner: main-checkout` so `work.py claim` never
hands them to a worktree agent (a worktree can't see the dirty files). Run them from the
main checkout, human-supervised; their bodies carry the exact commit protocol. The GUI
migration waves (T-G5..T-G9) are integrator-style tasks — one driver orchestrating
subagents per `docs/GUI_MIGRATION_DAG.md` node specs, with that doc as the single source
of truth for write-sets and verify commands.
