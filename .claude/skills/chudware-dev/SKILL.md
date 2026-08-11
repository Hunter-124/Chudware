---
name: chudware-dev
description: Generalist skill for ANY change to the Chudware program in this repo — engine/kernel work, agent loop, MCP server, contracts, scripts, tests, docs, bug fixes, features — whenever the task isn't purely GUI (purely-GUI batches go to gui-orchestrator, which this skill points to). Carries the verified repo map, build/test commands, git workflow, and Hunter's working preferences, so read this before searching the repo or asking basics.
---

# Chudware Generalist Dev

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
`ui/imgui/` is the GUI. That was **reverted**: `build.sh` is authoritative — "the legacy `ui/imgui` / `ui/abstract` shells are retired; host/ is the single cross-platform GUI target."
Trust build.sh/gate.sh and this map over AGENTS.md's repo-map rows where they conflict.

| Dir | What it is |
|---|---|
| `core/` | Pure C++/CUDA kernel → `libchudware`: geom/doc/sketch/solver/cam/sim/ecad/mate/parts/agent/util/api/tests. Public API seam: `core/api/libchudware.h` (append-only) |
| `core/device/**` | Device identity and capability truth; transports, Grbl codec/adapter, and emulator |
| `core/manufacturing/**` | Journal + durable sequence, result registry, invalidation, tier0, policy, gates, approval, lease, and arbiter |
| `core/twin/**` | Manufacturing digital twin |
| `core/print/**` | Additive-print model |
| `engine_oracle/` (symlink `engine/`) | Python parity oracle (build123d on OpenCASCADE) — frozen while `core/` strangles it out; kept in lockstep via parity corpus; deleted last |
| `host/` | **The production GUI** — C++ GLFW/ImGui desktop app. `host/src/app.cpp` frame loop, `host/src/ui/` panels (52k LOC), `host/src/core/app_state.h` central data model. Details: the gui-* skills |
| `ui/viewport/` | Shared OpenGL render module (renderer/mesh/camera, id-pass picking, AICM loader) |
| `services/agent/` | Built-in AI agent loop (Python): agent_turn, councils, delegation, events |
| `services/mcp/` | MCP server exposing the engine to external agents (Claude Code etc.) |
| `services/worker/` | Engine routing seam: WorkerClient / DocMirror / bootstrap |
| `services/installer/` | Provisioning, MCP self-registration, packaging |
| `services/manufacturing/**` | Gateway transports + allowlist, printer adapters, slicer, monitor, metrology, notification, PnP, and robot services |
| `services/perception/**`, `training/perception/**` | Perception model/runtime and reproducible training pipeline |
| `contracts/` | **Frozen interface contracts — THE seams. Append-only.** `worker_rpc.md`, `doc_ops.md`, `feature_params.md`, `icons.md`, `parts.md`, `ecad/`, … (`contracts/README.md`) |
| `corpora/` | Consolidated test fixtures (sketch/render/sw_parity), referenced by canonical paths |
| `scripts/` | Gates & audits: `gate.sh`, `check.py`, `gui_lint.py`, `icon_pipeline.py` |
| `docs/` | Design docs, `PLAN.md`, `CPP_MIGRATION.md`, `REFACTOR_LOG.md` (one-paragraph handoffs go here) |
| `docs/manufacturing/**` | Manufacturing subsystem design corpus |
| `work/` | Task board (`work/tools/work.py`) + backlog cards `work/backlog/T-*.md` |
| `assets/` | Icons + generators (`make_icons.py`, `pack_icons.py`), logo |
| `tools/`, `benchmarks/`, `training/`, `services/dag/`, `services/deliverables/` | Dev tools, benches, ML training, DAG missions, deliverable templates |
| `Webservice/` | chudware-web: self-hosted multi-user streamed sessions — `gateway/` (FastAPI portal+auth+session mgr+chat), `image/` (Fedora 44 session container), `deploy/` (systemd user units, cf_provision.py, RUNBOOK.md), `DESIGN.md`. T-R7 PLM remote subset lives in `services/plm/remote.py`. Secrets NEVER committed (`Cloudflare-info.txt` gitignored) |
| `scratch/` | Throwaway output — never commit anything load-bearing here |

Env/naming compat: `CHUDWARE_*` env vars canonical, `CHUDWARE_*` honored; MCP id `chudware`

## Commands

```bash
./build.sh              # default: host GUI (cmake -S host -B host/build-linux + ninja), 2–3 min
./build.sh core         # C++ kernel only (linux-core preset; needs system OCCT)
./build.sh all          # core, then GUI
./build.sh clean        # wipe host/build-linux (core kept — OCCT reconfig is expensive)
./host/build-linux/bin/chudware                     # run the app
scripts/gate.sh         # THE gate: check.py → core build+ctest → host build → pytest → shot smoke
python3 scripts/check.py                          # fast stdlib gate (gui_lint, audits) — always runnable
python3 work/tools/work.py ready|board            # task board
```

Gate semantics: OCCT-absent and runtime-venv-absent are **SKIPs, not failures** — the gate
stays green on an unprovisioned box. Engine+agent pytest runs as
`<venv>/python -X utf8 -m pytest engine/doc services/agent -q` when the venv exists.
Scripted GUI launches use `xvfb-run` automatically; set `CHUDWARE_SHOT_HEADED=1` to see a real
window. Headless scenario smoke: `CHUDWARE_SHOT=<m1|sketch|asm|fea|flow|drawing|…>
./host/build-linux/bin/chudware --ui-screenshot scratch/smoke.png` (crash check, not visual
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

### Manufacturing rules that bind every change

- The `manufacture` MCP family is default-OFF.
- The arbiter is the only approval validator; the device allowlist denies by default.
- `FreshValue<T>` is the sole external freshness shape, and the journal is the sole source of durable `seq`.
- Nothing may advertise physical `execute` or `safety_integration` before the E4–E6 pilot.
- `mfg_run_resume` exists only to refuse.

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
- **Mission docs are never CLOSED, only SAVED** (2026-08-05). T-RF5 deleted
  `services/agent/loop.py`'s `_terminal_autosave_close_docs` and C++ never re-implemented
  it: `core/agent/run.cpp` only `doc_save`s the doc a DAG node is on, with zero
  `doc_close`/`doc_discard`. So every doc a run builds stays open, the GUI only *detaches*
  un-adopted AI mirror tabs on exit (`host/src/app.cpp` ~7069), and hostd's retire tier
  turned each unsaved one into a "Recover unsaved parts?" row (eight in one second on a
  real machine). Closing them is NOT the fix — it destroys the mirror tab the user browses
  after a run, which is why agent-origin docs get the 30-min GC floor. Instead
  `cad_common.save_touched_docs()` runs at every turn terminal
  (`mission_dispatch.run_linear_role_turn`) and in a `finally` on the crash path
  (`worker_rpc._agent_admin_turn`), writing anything hostd would retire
  (`path.empty() || dirty || autosave path`) into `CHUDWARE_PROJECT_SAVE_DIR`. It NEVER
  touches `origin_kind=gui` and never writes a file for an empty doc. Only `new_part` and
  `add_features` project-save on their own — every assembly and every `edit_feature` /
  `set_material` / `asm_*` mutation depends on this sweep.
- **Refine cap (WI-10) is live** for non-frontier tiers: `scaffolds.should_stop_refine` is
  called at the tail of `agent_turn.run_turn`'s loop and stops with the `refine_cap`
  stop_reason. A "round" is geometry re-worked AFTER the model itself declared the build
  finished (`_GEOMETRY_SETTLERS` = verify_battery / save_part / export_part), so a first
  build is never counted however many features it takes. Do NOT add describe_part /
  measure_part / render_part to the settlers — models interleave them, and A's limit of 1
  would then cap a normal part at one feature.
- **One design is one document, and its name never carries a version.** An iteration is a
  REVISION, never another doc. A version bump (`part_v4`, `-rev2`, `gen3`) RESUMES the
  series in `new_part` / `new_assembly` / the `add_features` `{"op":"new_part"}` batch head
  (`_docs_in_version_series`); `fresh=True` is the only escape. And since 2026-08-05 the
  token never reaches a document at all: `cad_common._strip_version_suffix` normalizes the
  name in all three `doc_new` MINT payloads, so `new_part("holster_v1")` mints `holster`
  even on a first-ever call and even with `fresh=True`. Two rules bind any change here:
  the resume/series lookups must keep seeing the RAW requested name, and the strip is
  SKIPPED when a doc of that EXACT name is already open — that call is the engine's own
  `resume_if_open` match, which keys on the name verbatim, so stripping there mints a
  duplicate instead of resuming (pinned by
  `test_exact_name_match_still_goes_through_the_engine_mint_lock` and two twins).
  `core/agent/run.cpp`'s `sanitize_part_name` (in `run_stop.cpp`) mirrors the same token
  set for the mission lane — keep the two in lockstep. Every extra mint costs a tab, a
  project `.step+`, its own AI-VC chain (keyed by `doc_id`), its own auto-registered vault
  item (`services/plm/vault.py` rglobs the project on save), and a recovery row.
- **The main chat has NO tool-call budget** (2026-08-05). It had one — 30 — and it killed a
  finished build one dispatch before the `plm_checkpoint` the model had composed, then
  reported the forced stop as `stop_reason=confirmed`. `agent_turn`'s enforcement site
  branches on `subagent_id is None`: main chat gets only a runaway backstop, subagents get a
  ONE-SHOT injected soft warning plus a separate hard ceiling. The threshold is
  config.json `agent.subagent_tool_call_budget` (`0` = derive from the token grant), read by
  `config.get_subagent_tool_call_budget()`, set by a Settings slider. Do NOT reintroduce a
  main-chat cap, and do NOT let a forced stop claim success.
- **Delegated subagents need an explicit `max_tokens`.** `agent_turn`'s default completion
  cap is 2048; `_run_subagent_turn` omitted the kwarg and every delegate silently inherited
  it, so a `feature_modeler` burned a 60,000-token grant on two `reply_truncated` turns with
  zero tool calls. It now passes `LINEAR_ROLE_MAX_OUTPUT_TOKENS` like the parent lane.
  `services/dag/node_runner.py` still reaches the 2048 default when no tier profile exists.
- **`plm_item_add` is for NON-design files only.** `.step+`/`.ecad+` are auto-registered by
  `vault._explode_project_into_shadow` on every save/checkpoint, so `Vault.add_item` can
  never succeed on one — that rejection now runs FIRST and points at `plm_checkpoint`. Its
  `path` is the PROJECT ROOT (unlike every sibling tool, where `path` is the target); the
  thing being tracked goes in `file_path`/`folder_path`. Role cards must not tell a model to
  register "what you changed" without that carve-out; that phrasing cost four dead calls.
- **The agent's in-process doc ops publish** (2026-08-05). `core/agent/run.cpp` calls
  `doc::doc_new`/`doc::doc_apply_ops` directly, bypassing `Server::dispatch` and therefore
  `DocRegistry` — so nothing it built was visible to any client until the run ended. Every
  such call site now invokes the `chudware_set_doc_publish_sink` callback (armed by
  `Server::dispatch` for `agent_*`), which drives the same `registry_.postCall` a socket
  `doc_new` would. Add a publish call beside any NEW direct doc op you introduce there, or
  it is invisible again. `internal:true` must keep propagating (preflight shadow clone).
- **The delta coalescer exists TWICE and drifted into the same bug.**
  `agent_turn._AssistantDeltaCoalescer` and `core/agent/run.cpp`'s `DeltaCoalescer` both
  buffer `text`/`thinking`. Each MUST flush the active channel before switching and drain
  the active channel first, and `run_turn` must flush before every non-delta event —
  otherwise a tail crosses a channel switch and glues onto the next completion's opening
  (live seams: `batch them.The user`, `saved file.The harness`). Change one, change the
  other; the comments name each other for that reason. GUI side: `feed.cpp` must NOT treat
  a hidden text-channel flush as a visible thinking boundary, or one reasoning stream
  renders as several boxes.
- **Streamed deltas are a recovery source, not just display.** A provider can emit hundreds
  of deltas and then an EMPTY final envelope (or no final response line at all). The
  resample used to inspect only the final result, so paid output was discarded and the turn
  died `provider_error`. `omp_bridge_provider.complete_ex` now retains per-attempt fragments
  and recovers streamed text or a fenced directive before spending another completion — but
  it still forwards each fragment to the display sink exactly once. NEVER concatenate deltas
  into the displayed reply; that contract is unchanged and text double-prints if you break
  it. An empty `length` reply with `reasoningEffort` set retries once WITHOUT the effort.
- **`plm_*` tools default their `path` to the resolved project root** (2026-08-05):
  `CHUDWARE_PROJECT_DIR` → `CHUDWARE_FS_ROOT` → `project_context.resolve_project_root()`,
  explicit argument still wins, `project_root_unavailable` when nothing resolves. Three
  separate live runs died at the tail because the model had no way to know that root. The
  document-scoped AI-VC ops (`plm_timeline`/`plm_rollback`/`plm_rollback_check`) take no
  project root and are deliberately excluded.
- **`save_part`/`export_part` with no path go to `CHUDWARE_PROJECT_SAVE_DIR`.** The temp
  fallback survives ONLY for headless external MCP clients with no project. A
  `chudware_mcp_*` basename is also dropped by the artifact allowlist as internal scratch,
  so a scratch save is invisible in the GUI as well as misplaced.
- **A failed `delegate` must lead with its cause.** The ledger clips tool results at ~999
  characters; a failed child's `ok`/`reason`/`error` are emitted FIRST so the reason
  survives that clip and reaches the model and the GUI card. Before this, a child that died
  on a provider fault surfaced as its own brief echoed back.
- **config.json keys:** `saveSettings` merge-overlays `keys`/`experimental` — never
  full-replace them (harness `keys.openrouter` must survive a GUI save).
- **agent_turn edits:** standing mega-24 offline planning-node repro gate
  (`FINAL_OFFLINE_OK` / `services/agent/test_mega24_fixes.py`) plus mega regression suites
  before any live mission spend.
- **Tool floors (2026-08-07):** preserve allowlist tri-state: `None` means open,
  while explicit `[]` means restricted to that lane's floor. Main linear chat uses
  derived `MAIN_CHAT_DEFAULT_TOOLS` (agent fs/shell/fetch + child fs read/glob/grep);
  DAG nodes and delegated children use only `SUBAGENT_DEFAULT_TOOLS` and always pass
  a `subagent_id`. Finalize this effective set BEFORE schema rendering, then pass the
  same set to dispatch. Rare CAD families stay behind
  `enable_tools(categories=[...])`/`list_tool_categories`; never teach positional
  `enable_tools([...])` or bloat the floors.
- **Routing / execution structure (model-driven 2026-07):** there is NO
  deterministic keyword/regex classifier deciding execution structure. The
  `general_agent` role (`roles/general_agent.yaml`) is the UNIVERSAL entry: every
  request runs ONE free linear turn, and the MODEL decides whether to finish
  inline, `delegate` a scoped build, or `mission_start` the full build swarm. It
  starts with the derived main-chat floor + `enable_tools`/`list_tool_categories` +
  `delegate`/`mission_start`, so it sees the tool CATEGORY packs (workspaces) and
  pulls modeling/sketch/sim/… on demand (verified:
  `enable_tools(categories=["modeling"])` unlocks `new_part` mid-turn) — never a
  multi-step plan forced onto basic work.
  Python path: `mission_dispatch.route_request` always runs `general_agent`;
  `workflow_router.route`/`classify_task`/`structure_for_class` survive ONLY as
  advisory Routing-card telemetry (`agent.classification`), never a gate. C++ live
  path (the single in-app agent entry per `loop.py`): `run.cpp`'s pre-flight hands
  every ONLINE top-level run to the `general_agent` turn via the `agent_admin_turn`
  worker RPC (pass `role` to override, e.g. project_organizer). OFFLINE is the lone
  exception — it keeps the deterministic split (`domain.is_general_work_request`):
  general work blocked (no provider), offline-safe local modeling falls through to
  the C++ pipeline. Explicit mission mode (`run_mission`) still runs the swarm.
- **Prompt override isolation (2026-08-07):** native/core C++ prompts consume
  `appdata/prompts/<id>.md`; YAML mission roles consume
  `appdata/prompts/<id>.mission.md`. Settings catalogs both as `core:<id>` and
  `mission:<id>`. Never collapse them: overlapping ids such as reviewer/fixer have
  incompatible output contracts. The top-level `system_prompt` setting intentionally
  replaces role prose but still receives harness base, capability, tools, skills, and
  runtime output enforcement.
- **Sidecar shutdown:** OMP bun bridge + python worker bridge spawn with
  setpgid+PDEATHSIG+close_range; hostd's clean-shutdown tail calls
  `WorkerBridge::shutdownIfCreated()` and the `chudware_agent_sidecars_shutdown` C-ABI
  (dlsym, missing-symbol tolerant). Keep new sidecars on this pattern.

## Commit / push template

```bash
./build.sh   # or scripts/gate.sh for engine/agent work — must be green
git add <files> && git commit -m "<area>: <what changed>"   # area: core|engine|agent|mcp|gui|icons|scripts|docs|skills
git push origin master
```

Then hand the user the verification checklist. If a change alters repo structure or
conventions, update this skill (or the affected gui-* skill) in the same commit — these
skills are version-controlled precisely so they stay current for every contributor.
