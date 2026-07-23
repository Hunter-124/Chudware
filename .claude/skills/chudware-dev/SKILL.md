---
name: chudware-dev
description: Generalist skill for ANY change to the Chudware program (formerly Chudware) in this repo — engine/kernel work, agent loop, MCP server, contracts, scripts, tests, docs, bug fixes, features — whenever the task isn't purely GUI (purely-GUI batches go to gui-orchestrator, which this skill points to). Carries the verified repo map, build/test commands, git workflow, and Hunter's working preferences, so read this before searching the repo or asking basics.
---

# Chudware Generalist Dev (formerly Chudware)

How to work in this repo, regardless of which part you're changing. GUI-heavy batches:
hand off to `.claude/skills/gui-orchestrator/SKILL.md` (it has the panel-level map and
specialist worker skills). Everything else: this file is your base.

## Hunter's working preferences (apply to every task)

- **Clarify first.** If the target, desired behavior, or scope is ambiguous, ask (batch all
  questions into one round) before touching code. A wrong guess wastes a whole cycle.
- **Master-only workflow.** Work directly on `master`; no feature branches or PRs. Start every
  session: `git checkout master && git pull origin master`. Stray branches with unmerged
  commits exist — surface them, don't silently merge.
- **Commit + push as you go.** One commit per completed, building unit of work; push to
  `origin master` immediately. Never leave verified work uncommitted.
- **User verifies visuals/behavior in the running app.** Don't burn tokens screenshotting and
  judging your own UI output; end with a short checklist: "open X, do Y, expect Z."
  Compiling builds and passing gates ARE your job before handing off.
- **Don't search when a map exists.** Use the tables below, then graphify, then grep — in
  that order.
- Solo dev: if asked to "find something to do" / "chip away", use the task board:
  `python3 work/tools/work.py ready` (unblocked tasks) and `... board` (full board). No
  claim step; pick, work, mark done.

## Locating code — graphify first

The repo has a knowledge graph at `graphify-out/` (skill: `.claude/skills/graphify/SKILL.md`):
- `graphify query "<question>"` — scoped subgraph for any codebase question
- `graphify path "<A>" "<B>"` — dependency chain between files
- `graphify explain "<concept>"` — focused concept context
- `graphify-out/GRAPH_REPORT.md` / `graphify-out/wiki/index.md` — broad architecture only
- After meaningful structural changes: `graphify update .` (AST-only, free)

## Repo map (verified against master, 2026-07-10)

⚠️ **AGENTS.md is stale on GUI ownership** — it claims `host/` was deleted (T-G11) and
`ui/imgui/` is the GUI. That was **reverted**: `build.sh` is authoritative — "the ui/imgui /
ui/abstract (AI-CAD2) app is retired; host/ is the single cross-platform GUI target."
Trust build.sh/gate.sh and this map over AGENTS.md's repo-map rows where they conflict.

| Dir | What it is |
|---|---|
| `core/` | Pure C++/CUDA kernel → `libchudware`: geom/doc/sketch/solver/cam/sim/ecad/mate/parts/agent/util/api/tests. Public API seam: `core/api/libchudware.h` (append-only) |
| `engine_oracle/` (symlink `engine/`) | Python parity oracle (build123d on OpenCASCADE) — frozen while `core/` strangles it out; kept in lockstep via parity corpus; deleted last |
| `host/` | **The production GUI** — C++ GLFW/ImGui desktop app. `host/src/app.cpp` frame loop, `host/src/ui/` panels (52k LOC), `host/src/core/app_state.h` central data model. Details: the gui-* skills |
| `ui/viewport/` | Shared OpenGL render module (renderer/mesh/camera, id-pass picking, AICM loader) |
| `services/agent/` (symlink `agent/`) | Built-in AI agent loop (Python): agent_turn, councils, delegation, events |
| `services/mcp/` (symlink `mcp_server/`) | MCP server exposing the engine to external agents (Claude Code etc.) |
| `services/worker/` | Engine routing seam: WorkerClient / DocMirror / bootstrap |
| `services/installer/` (symlink `installer/`) | Provisioning, MCP self-registration, packaging |
| `contracts/` | **Frozen interface contracts — THE seams. Append-only.** `worker_rpc.md`, `doc_ops.md`, `feature_params.md`, `icons.md`, `parts.md`, `ecad/`, … (`contracts/README.md`) |
| `corpora/` | Consolidated test fixtures (sketch/render/sw_parity); root symlinks keep legacy paths |
| `scripts/` | Gates & audits: `gate.sh`, `check.py`, `gui_lint.py`, `icon_pipeline.py` |
| `docs/` | Design docs, `PLAN.md`, `CPP_MIGRATION.md`, `REFACTOR_LOG.md` (one-paragraph handoffs go here) |
| `work/` | Task board (`work/tools/work.py`) + backlog cards `work/backlog/T-*.md` |
| `assets/` | Icons + generators (`make_icons.py`, `pack_icons.py`), logo |
| `tools/`, `benchmarks/`, `training/`, `dag/`, `deliverables/` | Dev tools, benches, ML training, DAG missions, deliverable templates |
| `Webservice/` | chudware-web: self-hosted multi-user streamed sessions — `gateway/` (FastAPI portal+auth+session mgr+chat), `image/` (Fedora 44 session container), `deploy/` (systemd user units, cf_provision.py, RUNBOOK.md), `DESIGN.md`. T-R7 PLM remote subset lives in `services/plm/remote.py`. Secrets NEVER committed (`Cloudflare-info.txt` gitignored) |
| `scratch/` | Throwaway output — never commit anything load-bearing here |

Env/naming compat: `CHUDWARE_*` env vars canonical, `CHUDWARE_*` honored; MCP id `chudware`
with `ai-cad` preserved.

## Commands

```bash
./build.sh              # default: host GUI (cmake -S host -B host/build-linux + ninja), 2–3 min
./build.sh core         # C++ kernel only (linux-core preset; needs system OCCT)
./build.sh all          # core, then GUI
./build.sh clean        # wipe host/build-linux (core kept — OCCT reconfig is expensive)
./host/build-linux/bin/Chudware                     # run the app
scripts/gate.sh         # THE gate: check.py → core build+ctest → host build → pytest → shot smoke
python3 scripts/check.py                          # fast stdlib gate (gui_lint, audits) — always runnable
python3 work/tools/work.py ready|board            # task board
```

Gate semantics: OCCT-absent and runtime-venv-absent are **SKIPs, not failures** — the gate
stays green on an unprovisioned box. Engine+agent pytest runs as
`<venv>/python -X utf8 -m pytest engine/doc agent -q` when the venv exists.
Scripted GUI launches use `xvfb-run` automatically; set `CHUDWARE_SHOT_HEADED=1` to see a real
window. Headless scenario smoke: `CHUDWARE_SHOT=<m1|sketch|asm|fea|flow|drawing|…>
./host/build-linux/bin/Chudware --ui-screenshot scratch/smoke.png` (crash check, not visual
judgment).

## Rules that bind every change

- **Contracts and seam files are append-only** and integrator-owned: `contracts/*`,
  `core/api/libchudware.h`, top-level CMake, `scripts/gate.sh`, `AGENTS.md`. If you're a
  subagent, request these edits from your orchestrator instead of making them.
- **Subagents never run git**; the session driver commits. Parallel agents need disjoint
  write-sets. **No git worktrees in this repo** — they branch from a stale base and have
  silently reverted master before; run subagents in the main tree.
- A change is done when the relevant gate is green: `./build.sh` compiles at minimum;
  `scripts/gate.sh` for anything touching core/engine/agent; `scripts/gui_lint.py --rule
  theme|icons` when styling/icons changed.
- Engine parity: if you change behavior in `core/`, check whether the Python oracle
  (`engine_oracle/`) and parity corpus need the same change — lockstep is the contract.
- Notable multi-file efforts get a one-paragraph handoff appended to `docs/REFACTOR_LOG.md`
  (files touched, deferred items, gotchas).

## Daemon / mission lifecycle (don't regress these)

- **hostd control:** `chudware-hostd --status` / `--stop` / `--stop --force`. Bare `--stop`
  refuses while agent children run; `--force` stops children (TERM → grace → KILL) first.
  If the control socket is dead but the pidfile process still lives, `--stop` escalates
  via pidfile: SIGTERM → 3 s → SIGKILL (`core/hostd/main.cpp`).
- **Doc-GC:** abandoned docs: `CHUDWARE_DOC_GC_GRACE_S` (default 90 s); agent/mcp-origin
  docs use floor `CHUDWARE_DOC_GC_AGENT_GRACE_S` (default 1800 s) so mission-built parts stay
  openable from the GUI after the agent detaches; `CHUDWARE_DOC_GC=0` disables
  (`core/hostd/doc_registry.*`).
- **GUI reconnect:** status bar shows a sticky unreachable chip; `engine_events` reconnects
  with backoff and re-bootstraps doc tabs on `reconnectGeneration` bump
  (`CHUDWARE_SHOT=daemon_h2`). See gui-workspaces for the status-bar surface.
- **Mission terminal:** loop autosaves mission-minted geometry as `.step+` into the run
  `parts/` dir + GUI workspace, then closes those docs (GUI-adopted exempt)
  (`services/agent/loop.py` `_terminal_autosave_close_docs`).
- **config.json keys:** `saveSettings` merge-overlays `keys`/`experimental` — never
  full-replace them (harness `keys.openrouter` must survive a GUI save).
- **agent_turn edits:** standing mega-24 offline planning-node repro gate
  (`FINAL_OFFLINE_OK` / `services/agent/test_mega24_fixes.py`) plus mega regression suites
  before any live mission spend.

## Commit / push template

```bash
./build.sh   # or scripts/gate.sh for engine/agent work — must be green
git add <files> && git commit -m "<area>: <what changed>"   # area: core|engine|agent|mcp|gui|icons|scripts|docs|skills
git push origin master
```

Then hand the user the verification checklist. If a change alters repo structure or
conventions, update this skill (or the affected gui-* skill) in the same commit — these
skills are version-controlled precisely so they stay current for every contributor.
