# Workflow v2 — the automated scheme for this project (Hunter, 2026-07-03)

The single source of truth for how work gets planned, claimed, executed, reviewed,
and steered. Supersedes scattered policy notes; autopilot.md and FOREMAN.md defer
here on conflicts. Goal: **spend tokens where they buy product, spend human time
only where a human is genuinely required, and never let a low-priority roadblock
silently stall a high-priority lane.**

## 1. Roles (who runs what model, and why)

| Role | Count | Model tier | Job |
|---|---|---|---|
| Human (Hunter) | 1 | — | Merges to `done`, answers human-only questions, sets priorities/defers. Everything else is delegable. |
| Foreman | 1 | Fable | Cuts/steers tasks, reviews+merges review branches on master, answers `work/questions/`, triages roadblocks, keeps this doc honest. ONE foreman at a time (master writes serialize). |
| Drivers | N | **Sonnet by default** | Claim → execute work order on a branch → `status: review` → next. Opus only for gnarly C++/kernel/geometry tasks the foreman flags; never Fable. |
| Subagents | per driver | Sonnet/Haiku | Exploration, bulk reads, builds/tests, mechanical refactors. Drivers take back diffs/summaries only. |

Token rules (binding, from FOREMAN.md "Context & cost discipline" — still in force):
delegate downward, keep the main window linear/append-only for cache hits, push long
waits to background subagents, commit-and-drop, HANDOFF at ~60-70% context.

## 2. Board vocabulary (task frontmatter)

- `status`: todo | doing | review | done | blocked | backlog — unchanged; `done` is
  human-merge-time only.
- `priority`: 1 (drop everything) … 5 (default). Claim order.
- `lane`: user-facing feature area — the dashboard swim-row. One of: modeling-core,
  assemblies-drawings, cam, ecad-firmware, simulation, parts-sourcing, ai-copilot,
  app-gui, plm-projects, platform-infra, ml-flywheel.
- `kind`: **fix** (issue with an existing feature) | **overhaul** (redesign of
  existing) | **new** (not built yet). Within the same priority+day, claim order is
  fix → overhaul → new: *we repair what users already touch before adding surface.*
- `deferred: yes`: the plan is parked — invisible to `ready`/`claim` and hidden on
  the dashboard by default. Un-park with `work.py set <id> --deferred no`.

Steering (human or foreman, no ownership needed — each writes one commit):
`python3 work/tools/work.py set T-XX --owner <name> [--priority N] [--lane L]
[--kind K] [--deferred yes|no] [--reason "..."]` — or the dashboard's detail
drawer, or the `work_set` MCP tool.

## 3. Deferred plans registry (current)

| Plan | What's parked | Seam that stays |
|---|---|---|
| ML / custom-model training (Hunter 2026-07-03) | training/fine-tuning tasks (e.g. T-BL14); no new ML-track tasks cut | telemetry capture, eval benches, adapter serving stay as shipped; capture-now-train-later stands |
| SaaS / cloud hosting (Hunter 2026-07-03) | any server build-out | AI-CAD Cloud stays a config-only provider/remote seam behind the Pillar-12 human gate (providers, PLM T-R7) |

When cutting new tasks in a parked plan: create them `deferred: yes` so the ready
queue stays honest.

## 4. Roadblock triage (the anti-stall protocol)

`python3 work/tools/work.py roadblocks` (or the dashboard Mission view) computes,
for every review/blocked/human-owned task, what it transitively holds up and a
verdict:

- **HIGH-IMPACT** (blocks P≤2 work): top of the human queue; the foreman may
  fast-track the merge or `relink` dependents around it the same day.
- **normal**: batch-merge in dependency-chain order (FOREMAN.md merge plan).
- **low-impact** (P≥4 blocking nothing important): *do not let it queue* — the
  foreman defers it (`set --deferred yes --reason`) or drops the dependency edge
  (`relink`). A roadblock that isn't a big deal gets skipped, not waited on.
  Deferring is reversible; stalling a lane isn't retroactively fixable.

Questions in `work/questions/`: drivers answer driver-answerable ones best-effort
inline; only genuinely human questions wait for Hunter (existing policy). The
foreman sweeps open questions every session.

## 5. Human session protocol (minimize Hunter's minutes)

When you sit down:
1. `work/run_version_control.sh` → **Mission view**: the roadblocks strip is
   ordered by impact — work top-down. Low-impact cards have one-click Defer.
2. Merge the review queue in the listed chain order (or tell the foreman to).
3. Answer only the questions marked human-only.
4. Adjust priorities/lanes/defers directly in the UI (or `work.py set`) — that IS
   the steering interface; no meeting with the foreman needed.
Everything else (claiming, building, reviewing branches for merge-readiness,
question-answering) should already have happened without you.

## 6. MCP freshness rule (standing, binding)

The MCP surfaces ARE the product's AI harness. Any task that adds or changes an
agent-facing workflow, tool, prompt, or context pack MUST, in the same branch:
- update the relevant MCP server instructions (`services/mcp/` for ai-cad,
  `work/tools/team_mcp.py` for team coordination),
- update `cad_guide` topics if modeling/CAM/ECAD-facing,
- refresh the touched `work/context/` section pack (the `/ctx-*` skills read them),
- run `graphify update .`.
Tracked as a DoD convention; T-BL26 audits the current drift and adds a
consistency check so this stays true mechanically.

## 7. Command crib sheet

```
work.py ready [--include-deferred]      # what's claimable, in claim order
work.py claim --owner NAME [--task ID]  # atomic claim (priority→day→fix-first)
work.py roadblocks [--json]             # what's waiting on a human, with impact
work.py lanes [--json]                  # ship-readiness per feature lane
work.py set ID --owner NAME ...         # steer priority/lane/kind/deferred
work.py board --lane app-gui --kind fix # filtered board
work/run_version_control.sh             # the web dashboard (Mission/DAG/Graphify)
```
MCP equivalents on aicad-team: work_ready, claim, work_roadblocks, work_lanes,
work_set, work_board.
