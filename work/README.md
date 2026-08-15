# work/ — git-tracked task board

Lightweight, file-based personal task tracking (solo dev). Everything is markdown
in git — no external tracker.

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

1. **Find a task:** `python3 work/tools/work.py ready` lists todo tasks whose
   dependencies are all done.
2. **Start it:** set `status: doing` in the task file.
3. **Branch (optional):** create the branch named in the task's `branch:` field
   (`task/<id>-<slug>`) if you want the work isolated; fine to work on `master`
   directly for small stuff.
4. **Do the work:** paste the task file into your AI agent as the work order. Stay
   inside the task's "Files / areas touched" list; log discovered work as a new task
   file instead of scope-creeping.
5. **Done means done:** every checkbox in "Definition of done" checks, `bash
   scripts/gate.sh` passes, `graphify update .` run.
6. **Merge:** set `status: done` in the task file within the same commit/merge.

After any status change, regenerate the graph: `python3 work/tools/work.py dag`
and commit `work/DAG.md`.

## CLI

```
python3 work/tools/work.py validate   # schema + dependency-cycle check (CI-able)
python3 work/tools/work.py ready      # what can start now
python3 work/tools/work.py board      # full status board by workstream
python3 work/tools/work.py dag        # regenerate work/DAG.md
python3 work/tools/work.py prompt ID   # emit a self-contained agent work order
```

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
| F | AI experience: research/interaction/vision/narration/providers/translator/deliverables (docs/AI_TOOLING_MASTER_PLAN.md) | _agents_ |
| G | GUI: unified `host/src/ui/` desktop + web surface | Erik + agents |

## Special tasks: `owner: main-checkout`

A few tasks (T-G1, T-S2) land verified-but-uncommitted work that only exists in the main
checkout's dirty working tree. They keep `owner: main-checkout` so `work.py claim` never
hands them to a worktree agent (a worktree can't see the dirty files). Run them from the
main checkout, human-supervised; their bodies carry the exact commit protocol.
