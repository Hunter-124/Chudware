# Workflow — task board policy (Hunter, solo dev)

The single source of truth for how work gets planned and tracked. This project
is worked solo now (main WIP on this machine, remote is mainly a backup), so the
board is a personal backlog, not a multi-agent claim queue: no ownership/claiming
step, no foreman, no team-coordination MCP. Pick a task, do it, mark it done.

## 1. Board vocabulary (task frontmatter)

- `status`: todo | doing | review | done | blocked | backlog.
- `priority`: 1 (drop everything) … 5 (default).
- `lane`: user-facing feature area. One of: modeling-core, assemblies-drawings,
  cam, ecad-firmware, simulation, parts-sourcing, ai-copilot, app-gui,
  plm-projects, platform-infra, ml-flywheel.
- `kind`: **fix** (issue with an existing feature) | **overhaul** (redesign of
  existing) | **new** (not built yet). Prefer fixing what's already touched
  before adding new surface.
- `deferred: yes`: the plan is parked — invisible to `ready` by default.
  Un-park with `work.py set <id> --deferred no`.

Steering: `python3 work/tools/work.py set T-XX [--priority N] [--lane L]
[--kind K] [--deferred yes|no] [--reason "..."]`.

## 2. Deferred plans registry (current)

| Plan | What's parked | Seam that stays |
|---|---|---|
| ML / custom-model training (Hunter 2026-07-03) | training/fine-tuning tasks (e.g. T-BL14); no new ML-track tasks cut | telemetry capture, eval benches, adapter serving stay as shipped; capture-now-train-later stands |
| SaaS / cloud hosting (Hunter 2026-07-03) | any server build-out | AI-CAD Cloud stays a config-only provider/remote seam behind the Pillar-12 human gate (providers, PLM T-R7) |

When cutting new tasks in a parked plan: create them `deferred: yes` so the
ready queue stays honest.

## 3. MCP freshness rule (standing, binding)

The MCP surface IS the product's AI harness. Any task that adds or changes an
agent-facing workflow, tool, prompt, or context pack MUST, in the same branch:
- update the relevant MCP server instructions (`services/mcp/` for ai-cad),
- update `cad_guide` topics if modeling/CAM/ECAD-facing,
- refresh the touched `work/context/` section pack (the `/ctx-*` skills read them),
- run `graphify update .`.

## 3b. Derived-asset honesty (DoD convention, Hunter 2026-07-03)

Screenshot-derived icons must show REAL engine output, never a stylized stand-in.
In particular **CAM / Manufacture icons must render the real `cam_simulate` toolpath
over the stock** — so each CAM icon doubles as a smoke check that the op chain
(`cam_new_job` → `cam_add_tool` → `cam_add_operation` → `cam_simulate`) actually
produces a valid toolpath. If an op won't produce a renderable toolpath, that is a
**CAM bug to FILE (issue + backlog task), never an icon to skip or fake.**

## 3c. GUI ownership policy (updated 2026-07-22 — T-G11 direction REVERSED)

`host/` is the **sole active GUI target**. The `ui/imgui` + `ui/abstract` (AI-CAD2)
shell is retired and deleted; the former T-G11 "delete host/" plan was reversed.

- **New GUI work goes in `host/src/ui/` only.** There is no second GUI tree.
- The same `host/src/ui` tree compiles to WASM for the web app
  (`Webservice/README.md`) — a desktop UI change ships to web by construction;
  never fork a web-only copy of a panel.
- Scripted shot launches (gate.sh, tools/gui_parity/run_all.py) use `xvfb-run` unless
  `AI_CAD_SHOT_HEADED=1` is set, even when `DISPLAY` is present. Set that env var for
  interactive/walkthrough runs.

## 4. Command crib sheet

```
work.py ready [--include-deferred]      # what's unblocked, in priority order
work.py roadblocks [--json]             # what's stuck / needs a decision
work.py lanes [--json]                  # ship-readiness per feature lane
work.py set ID ...                      # steer priority/lane/kind/deferred
work.py board --lane app-gui --kind fix # filtered board
```
