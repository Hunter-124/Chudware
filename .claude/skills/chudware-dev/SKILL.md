---
name: chudware-dev
description: Generalist skill for ANY change to the Chudware program in this repo — engine/kernel work, agent loop, MCP server, contracts, scripts, tests, docs, bug fixes, features — whenever the task isn't purely GUI (purely-GUI batches go to gui-orchestrator, which this skill points to). Carries the verified repo map, build/test commands, git workflow, and Hunter's working preferences, so read this before searching the repo or asking basics.
---

# Chudware Generalist Dev

How to work in this repo, regardless of which part you're changing. GUI-heavy batches:
hand off to `.claude/skills/gui-orchestrator/SKILL.md` (it has the panel-level map and
specialist worker skills). Everything else: this file is your base.
For the compiled online-agent boundary, read
`.claude/skills/cad-chudcode-integration/SKILL.md` before touching sessions, providers,
auth/models, memory, host tools, events, recovery, or release artifacts.

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

## Repo map (verified against master, 2026-08-31)

**Current GUI ownership:** `host/` is the sole GUI target; `ui/imgui/` and `ui/abstract/` are gone, and T-G11's deletion of `host/` was reversed.

| Dir | What it is |
|---|---|
| `core/` | Pure C++/CUDA kernel → `libchudware`: geom/doc/sketch/solver/cam/sim/ecad/mate/parts/agent/util/api/tests. Public API seam: `core/api/libchudware.h` (append-only) |
| `core/device/**` | Device identity and capability truth; transports, Grbl codec/adapter, and emulator |
| `core/manufacturing/**` | Journal + durable sequence, result registry, invalidation, tier0, policy, gates, approval, lease, and arbiter |
| `core/twin/**` | Manufacturing digital twin |
| `core/print/**` | Additive-print model |
| `host/` | **The production GUI** — C++ GLFW/ImGui desktop app. `host/src/app.cpp` frame loop, `host/src/ui/` panels (52k LOC), `host/src/core/app_state.h` central data model. Details: the gui-* skills |
| `ui/viewport/` | AICM mesh loader (`render/aicm_loader.*`) consumed by `core/`; the host renderer lives under `host/src/render/` |
| `services/agent/` | Chudcode RPC host/adapters plus CAD-owned roles, councils, missions, governance, tool registry projection, and event mapping |
| `services/mcp/` | MCP server exposing the engine to external agents (Claude Code etc.) |
| `services/worker/` | Engine routing seam: WorkerClient / DocMirror / bootstrap |
| `services/installer/` | Provisioning, MCP self-registration, packaging |
| `services/manufacturing/**` | Gateway transports + allowlist, printer adapters, slicer, monitor, metrology, notification, PnP, and robot services |
| `services/perception/**`, `training/perception/**` | Perception model/runtime and reproducible training pipeline |
| `contracts/` | **Frozen interface contracts — THE seams. Append-only.** `worker_rpc.md`, `doc_ops.md`, `feature_params.md`, `icons.md`, `parts.md`, `ecad/`, … (`contracts/README.md`) |
| `corpora/` | Consolidated test fixtures (sketch/render/sw_parity), referenced by canonical paths |
| `scripts/` | Gates & audits: `gate.sh`, `check.py`, `gui_lint.py`, `icon_pipeline.py` |
| `docs/` | Design docs and live handoffs in `docs/REFACTOR_LOG.md`, with older handoffs split under `docs/refactor-log/` |
| `docs/manufacturing/**` | Manufacturing subsystem design corpus |
| `work/` | Task board (`work/tools/work.py`) + backlog cards `work/backlog/T-*.md` |
| `assets/` | Icons + generators (`make_icons.py`, `pack_icons.py`), logo |
| `tools/`, `benchmarks/`, `training/`, `services/dag/`, `services/deliverables/` | Dev tools, benches, ML training, DAG missions, deliverable templates |
| `Webservice/` | chudware-web: browser-native `host/` WASM GUI backed by one shared hostd — `gateway/` (FastAPI portal+auth+engine bridge+chat), `deploy/` (systemd user units, cf_provision.py, RUNBOOK.md), `DESIGN.md`. T-R7 PLM remote subset lives in `services/plm/remote.py`. Secrets NEVER committed (`Cloudflare-info.txt` gitignored) |
| `scratch/` | Throwaway output — never commit anything load-bearing here |

Env/naming: `CHUDWARE_*` env vars are canonical; MCP id is `chudware`

## Commands

```bash
scripts/provision_venv.sh # create/refresh .venv-mcp + audit deps; VENV_DIR and CHUDCODE_RPC_SRC override paths
./build.sh              # default: host GUI (cmake -S host -B host/build-linux + ninja), 2–3 min
./build.sh core         # C++ kernel only (linux-core preset; needs system OCCT)
./build.sh all          # core, then GUI
./build.sh clean        # wipe host/build-linux (core kept — OCCT reconfig is expensive)
./host/build-linux/bin/chudware                     # run the app
CHUDWARE_HEADROOM_GB=2 CHUDWARE_MEM_FRAC=0.6 scripts/gate.sh   # THE gate: check.py → core build+ctest → host build → pytest → shot smoke
#   ^ ctest tet10_thin_wall needs an 11.6 GB solve budget (budget = min(frac×total, avail−headroom));
#     at the box default headroom 6 it is 4–20 % short whenever anything else holds RAM.
python3 scripts/check.py                          # fast stdlib gate (gui_lint, audits) — always runnable
python3 work/tools/work.py ready|board            # task board
CHUDWARE_POLYMESH_ROOT=~/Repositories/Polyhedral-FEA-private ./build.sh core   # build against Hunter's live Polyhedral-FEA tree (else pinned public fetch)
./build.sh stage        # restage libchudware + chudware-hostd + polymesh CLI into services/dist (dist is FIRST in hostd lookup — stale dist = stale daemon)
scripts/run_mission.py --workspace <project> --sessions-dir ~/Documents/Chudware/Sessions --prompt-file p.md --model claude-opus-5   # headless agent turn; --resume for follow-ups
```

Gate semantics: OCCT-absent and runtime-venv-absent are **SKIPs, not failures** — the gate
stays green on an unprovisioned box. Python pytest sweeps run per unit under `services/`,
plus `tools/` and `training/`, when the runtime venv exists.
Scripted GUI launches use `xvfb-run` automatically; set `CHUDWARE_SHOT_HEADED=1` to see a real
window. Headless scenario smoke: `CHUDWARE_SHOT=<m1|sketch|asm|fea|flow|drawing|…>
./host/build-linux/bin/chudware --ui-screenshot scratch/smoke.png` (crash check, not visual
judgment).

## Rules that bind every change

- **Contracts and seam files are append-only** and integrator-owned: `contracts/*`,
  `core/api/libchudware.h`, top-level CMake, `scripts/gate.sh`, `AGENTS.md`. If you're a
  subagent, request these edits from your orchestrator instead of making them.
- **Subagents never run git**; the session driver commits, parallel agents use disjoint
  write-sets, and all run in the main tree because **this repo has no git worktrees** —
  they branch from stale bases and have silently reverted master. On 2026-08-27 the stale
  `~/.cache/wt-cad-code` and `~/.cache/wt-cad-release` worktrees plus 7 stale stashes were
  removed; the stashes remain archived as local refs `refs/archive/stash-0..6` and patches
  in `~/Backups/CAD-stashes-2026-08-28/`.
- A change is done when the relevant gate is green: `./build.sh` compiles at minimum;
  `scripts/gate.sh` for anything touching core/engine/agent; `scripts/gui_lint.py --rule
  theme|icons` when styling/icons changed.
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
- **What keeps hostd alive is WORK, not sockets** (2026-08-27). `Server::idleExpired` and the
  drain both gate on `pinningClients()`, never `status_.clients`: a `gui` client always pins
  (the app being open IS the reason the daemon exists), any other client pins only while it
  has an engine call in flight, an attached doc, or an RPC inside the idle window. Before
  this, one connected-but-silent socket vetoed every exit path — a daemon sat "draining
  (1 client)" in the tray for 5.5 h with an undismissable icon. Pinned by
  `test_hostd_registry.py::test_parked_client_does_not_pin_the_daemon` plus its control
  `…_attached_client_still_pins_the_daemon`. Do NOT re-add a raw client-count gate.
- **A drain is bounded.** `Server::beginDrain(deadlineSecs)`: `daemon_drain` / `--stop` /
  tray Quit / SIGTERM are HARD stops and carry a deadline (`CHUDWARE_HOSTD_DRAIN_GRACE_S`,
  default 20 s; tray Quit uses 5 s) after which remaining connections are `shutdown()` and
  the daemon exits regardless. The stale-binary self-drain passes `0` — soft, cooperative,
  refuse new connects only, so a rebuild never kicks a live session.
- **The doc-GC grace does NOT pin the daemon.** It used to, and with the 1800 s agent floor
  that kept hostd (and its tray icon) alive for half an hour after the user closed the app —
  while protecting a doc nobody could open, since the check is only reached when nothing is
  attached. The floor's job is app-runtime: keep a just-finished mission's part openable
  *while the GUI exists*. A grace that expires while the daemon serves is still retired by
  `tick()`; a clean exit still discards (crash-only recovery,
  `test_hostd_registry.py::test_clean_shutdown_discards_docs`).
- **The CEF fetch daemon has a watchdog thread** (`host/src/ui/widgets/cef/cef_daemon.cpp`).
  Its serve loop is single-threaded and also drives `CefDoMessageLoopWork`, and
  `initialize()` can block on Chromium's ProcessSingleton — so the loop's own idle check is
  not a termination guarantee. `start_idle_watchdog` is armed BEFORE `initialize()` and
  `_Exit(0)`s after `idle_secs` of no activity. Every `last = now` in that loop must go
  through `mark_activity()` or a live fetch looks idle. Spawning is single-flight
  (`services/browser/cef_client.py`): an flock alone is not enough because the spawns are
  SEQUENTIAL, minutes apart, each re-probing a socket a wedged daemon will never create — so
  the lock file also records the spawned pid (`_daemon_alive` verifies it via
  `/proc/<pid>/cmdline`) and a spawn is refused while that process lives. Without this, every
  turn that found nothing listening minted another daemon: 193 were found alive at once, 34 h
  old. Probe shape: 8 concurrent and 6 sequential spawners against a never-listening binary
  must both yield exactly ONE process, and killing the incumbent must allow a replacement.
  The biggest single contributor was upstream of all that: `--cef-*` argv lived entirely
  inside `#ifdef CHUDWARE_ENABLE_CEF` in `host/src/main.cpp`, so in a CEF-OFF build
  `chudware --cef-daemon <sock> 120` FELL THROUGH to the normal GUI launch — a whole desktop
  app that never listened on the socket and never exited. There is now an `#else` branch that
  claims those argv forms and refuses. It must use the `std::fflush(nullptr); std::_Exit(n)`
  tail (like `--version`): a plain `return` deadlocks in global destruction on a detached
  worker parked in `cv.wait` — verified, it printed the message and hung forever.
- **GUI reconnect:** status bar shows a sticky unreachable chip; `engine_events` reconnects
  with backoff and re-bootstraps doc tabs on `reconnectGeneration` bump
  (`CHUDWARE_SHOT=daemon_h2`). See gui-workspaces for the status-bar surface.
- **NEVER run `ulimit -v/-m/-d` (or `prlimit` against a pid you did not spawn) in an agent bash
  call (2026-09-05).** The agent host's bash tool is an in-process shell (`$$` is the omp pid),
  so an address-space cap lands on the host itself — with its ~75 GB of virtual reservations every
  later `mmap` fails with ENOMEM while RAM is free, and the host aborts (three crashes traced in
  `docs/ops/HOST_CRASH_FORENSICS.md`). Cap memory ONLY by wrapping the native run:
  `systemd-run --user --pipe --wait -q --collect -p MemoryMax=12G bash -c 'ulimit -c 0; …'`
  (cgroup kill instead of abort; `ulimit -c 0` keeps the coredump pipeline out of it). Box policy:
  `CHUDWARE_MEM_FRAC=0.5 CHUDWARE_HEADROOM_GB=6 CHUDWARE_SIM_THREADS=4`, one >4 GB run per agent,
  announced on hub, aggregate under ~24 GB.
- **A mission turn lives for hours; nothing in the lane may cap it silently (2026-09-05).**
  `chudcode_host` builds `RpcClient(max_event_history=CHUDWARE_CHUDCODE_EVENT_HISTORY,
  default 1e6)` — the package default of 10 000 frames killed a 65-minute turn with "Event
  history limit was exceeded". A turn whose last real event is a failed provider retry
  (`auto_retry_end success:false` with no assistant output after it) returns
  `ok:false, stop_reason:"provider_error"` even though Chudcode emitted a terminal
  `agent_end`; `scripts/run_mission.py` exits non-zero on it. Hunter's `chud config` carries
  `retry.maxDelayMs=1800000, retry.maxRetries=4` so a 429 with a 17-minute retry-after is
  waited out instead of ending the mission; a retry-after above it ends the turn. Sim
  ladders from one call are sequential, and the memory governor budgets
  `min(CHUDWARE_MEM_FRAC×total, avail−CHUDWARE_HEADROOM_GB)` at solve time — an explicit
  `sizes_mm` leg is coarsened to that budget, so free RAM (close builds/GUI) before a fine
  leg rather than raising the fraction. The coarsening is a number, not prose (2026-09-07):
  every result carries `mesh_quality.char_len_mm` (delivered size, null when no mesh
  length exists), ladder rows carry `delivered_size_mm` beside the requested `size_mm`,
  and the reply's `size_delivery` names legs outside 0.8–1.25× — the `.vtu` name still
  carries the REQUESTED `h`. `min_size_mm` on `run_fea_static|convergence|contact` lowers
  the size-band floor when a feature-resolution refusal names a size below it.
- **The turn budget is the caller's (2026-09-07).** `agent_admin_turn` forwards `timeout`
  to `chudcode_host.wait_for_idle`; without it the host's 2-hour absolute cap kills a long
  turn with `Timed out waiting for agent_end` regardless of `run_mission.py --timeout-s`
  (the driver now sends it and waits `--timeout-s + 60`). OAuth account balancing is
  sticky on the **Chudware session id**, not the resumed Chudcode history: a session id
  pinned to an exhausted account keeps 429-ing after `chud usage` shows a fresh account —
  resume the same `.jsonl` under a new `--session-id` (`chud dry-balance <model>` shows
  where fresh ids land).
- **`render_study_setup` figures are load-vector-specific (2026-09-07).** One arrow per
  load entry through the engine frame, a bottom banner with resultant/direction/per-entry
  vectors; multi-body docs paint (AICM face ids are per-body 1-based — `_mesh_topo_index`
  offsets them into `doc_query`'s running numbering; the old last-body map silently
  aliased body A's faces onto body B).
- **Headless turns are `do_it` (2026-09-07).** `services/agent/preferences.py` owns the four
  Copilot preferences (work_style/priority/privacy/…); the interactive default
  `work_style=check_with_me` maps to plan mode and MUST NOT govern unattended callers.
  `resolve_turn_preferences(cfg, headless=True)` is the one place that forces `do_it`; it is
  used by `mission_dispatch.run_linear_role_turn`, `chudcode_roles._assemble_role_runtime`,
  and `scripts/run_mission.py --mode` defaults to `agent`. When it leaked, three ME 490
  mission turns produced only `blocked_by_mode` refusals. Phase 1 of
  `docs/plans/AI_HARNESS_OVERHAUL_2026-09-05.md` deleted `effort_presets.yaml`, `ai_effort.*`,
  `agent.prewalk_target`, `effort.resolve_effort`, `TierProfile.deliberation` and the council
  threshold env readers — the Auto resolver (`preferences.py` §3.1 rules + bundled
  classifier with keyword fallback) replaces the seat lookups; the per-role table stays.
- **Buckling and steady thermal are engine-native (2026-09-07).** `run_fea_buckling` = static
  prestress → geometric stiffness → reciprocal pencil `K_G φ = μ K φ` (Spectra Cholesky on
  the shared modal factor); `max_load_factor` (default 1e4) is an engineering ceiling: the
  LDLT inertia of `K_G + K/λ_max` counts the modes below it exactly, zero → refusal naming
  the ceiling (a clamped tension bar legitimately has local compressive Gauss stresses).
  `run_fea_thermal` steady runs `fea_thermal_solve` on the part mesh (the CalculiX box proxy
  is gone; ccx remains only as V&V cross-check elsewhere). Known mesher defect: a 10×10×200
  bar at h=4 yields 205 504 tet10 (thin_wall_ratio 17.1) vs 2700 structured. `placement_source`
  persists in doc meta `placement_sources` (inside `.step+`); `scripts/provision_venv.sh`
  builds `.venv-mcp` from `requirements.txt` (+ `CHUDCODE_RPC_SRC` editable).
- **Chat sessions are keyed by project (2026-09-04).** `Conversation.projectDir` is stamped
  once at creation from `convProjectDir(st)` (= `st.workspaceDir`, else `plmProjectPath()`) —
  the same value the composer sends Chudcode as `workspace_dir`. `index.json` v3 keeps
  `active_by_project`; the Sessions panel filters on the stored key (never on a live artifact
  scan) and adopts headless Chudcode `*.jsonl` files found in the Sessions dir. Do not
  re-derive project membership at draw time.
- **Sim results are evidence-grade or they are not results.** `sim_run_static|adaptive|modal|
  thermal` emit top-level `mesh_quality`, `reactions` (static/adaptive), `provenance`
  (`engine_build_id`, `polymesh_commit`) and honour `study.vtu_path`; `sim_cache` schema `/3`.
  `run_fea_convergence` → `sim_evidence_packet` → `polymesh_cli` (mm STEP ⇒ `--scale 0.001`)
  is the release path in `simulate/SKILL.md`. Keep new fields TOP-LEVEL siblings of `summary`.
- **Assembly merges go through `assembly-merge/SKILL.md`.** Import features take
  `keep_bodies`/`exclude_bodies`; `fit_cylinder_axis` samples the true surface; components
  carry `placement_source` and exports warn `placement_unverified` until `check_interference`
  runs. Hand-editing STEP/JSONL text is a hard-rule violation for agents.
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
- **In daemon mode hostd is the ONLY autosave authority** (2026-08-27). The daemon-mode
  early-return lives in `cad_common._autosave()` — the single write — not at its callers.
  It used to sit on the two aggregate entry points (`_autosave_all_open`,
  `start_periodic_autosave`) while the ~7 per-tool call sites had none, and each ungated
  write repointed the live doc's `path` outside hostd's recovery tier and cleared `dirty`,
  which silently defeats `gcCloseDoc`'s retire-if-unsaved gate (`isAutosavePath` only
  recognizes hostd's own dir) — an abandoned, never-delivered agent doc got closed without
  ever reaching the orphan tier. Keep the guard at the write; the standalone (non-daemon)
  MCP server still needs the snapshots.
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
- **Chudcode is the only online agent/provider runtime (2026-08-31).** Every online
  top-level `core/agent/run.cpp` request becomes services-worker `agent_admin_turn`;
  `worker_rpc._agent_admin_turn` assembles the CAD role prompt and run-scoped host tools,
  then `ChudcodeSessionHost` drives the persistent protocol-v2 RPC session. Chudcode owns
  nested agents, providers, auth, models, session history, memory, compaction/retry/cache,
  browser/computer, and STT. Native C++ provider execution is only the explicit offline-safe
  local modeling fallback. Never add a second online provider loop or an external MCP hop.
- **One conversation owns one persistent Chudcode process.** A session may be seeded once
  from `chudcode_session_file`; changing that file while the session is live is an error.
  Session count is bounded (`CHUDWARE_CHUDCODE_MAX_SESSIONS`, 8 default/32 hard max), and
  LRU eviction may dispose only an idle session. `chudcode_close_session` is idempotent:
  abort active work, dispose the process, preserve the persisted file. Worker shutdown
  disposes every saved-session and transient client.
- **Control cannot share the blocked stdio lane.** `services/worker_main.py` starts the
  authenticated `chudcode_control.ControlServer` and exposes its endpoint/token only through
  `worker_control_info`. Cancel, steer, and status use that private Unix socket (POSIX) or
  random loopback TCP endpoint (Windows), so Stop remains live during a synchronous
  `agent_admin_turn`. Never log or echo the token; malformed, oversized, repeated-auth, and
  oversized-response paths stay bounded.
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
- **Chudcode event mapping is single-display and terminal-strict.**
  `services/agent/chudcode_events.py` is the only RPC-frame → Chudware-feed mapper.
  Streamed deltas are the only visible assistant text; message-complete envelopes are
  lifecycle records and must never replay final text. Preserve `text` versus `thinking`
  channel order, and do not treat an empty text flush as a thinking boundary.
- **Tool events correlate by the Chudcode call id.** Start/progress map to status only;
  terminal result/cancel maps once to `agent.tool_call`, carrying bounded previews, digests,
  outcome and usage. A synchronous `host_tool_call` with a result is already terminal.
  Unknown frames become deterministic bounded `sub_run` rows rather than being dropped or
  guessed. Provider usage emits both `agent.provider_usage` and `agent.context`.
- **A turn is not complete merely because the process is idle.** `run_turn` requires a
  terminal `agent_end`; ending without it is a protocol error. Event-forwarding failure
  fails the turn. Cancel before prompt is a valid cancellation; cancel after prompt calls
  `abort()` at most once. Identical steering is idempotent, and steering received before
  prompt start queues for immediate delivery after the prompt.
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
- **Nested agents remain inside Chudcode.** CAD owns the role/council/mission/governance
  context and the host-tool surface, but does not run a nested provider loop. Session,
  subagent, retry, compaction, todo, and usage events are mapped back into the existing
  Chudware feed contract; do not synthesize success when Chudcode reports failure.
- **Session recovery is a service boundary, not file surgery.**
  `chudcode_export_session`, `chudcode_branch_session`, `chudcode_repair_session`, and
  `chudcode_close_session` call the live runtime. Export uses Chudcode HTML export;
  branch uses a real entry id; doctor is bounded and `repair:true` uses Chudcode's
  backup-producing repair workflow. Never hand-edit persisted session JSONL.
- **The main chat has a broad core plus a visible catalog (2026-09-03).** The online
  `general_agent` lane advertises `tool_categories.MAIN_CHAT_CORE_TOOLS` (~62 tools, ~27k
  tokens) with native schemas, and `mission_dispatch._apply_session_preferences` appends a
  MORE TOOLS menu naming EVERY other registered tool by group (`render_tool_menu`, ~2.3k
  tokens). Nothing is hidden and nothing is capped: `enable_tools(tools=[...])` /
  `enable_tools(categories=[group])` resolve through `resolve_tool_requests` (group → pack →
  `family:` → exact name) and report `unknown`. The full registry is ~460 tools / ~150k
  tokens, so never "fix" reachability by widening the core — put a tool in the core only if
  ordinary part work needs it every turn. `run_shell` stays on the menu, off the core.
- **Tool floors stay CAD-authoritative.** Preserve allowlist tri-state: `None` is
  unrestricted, explicit `[]` is restricted to live floors, and a non-empty list is an
  ordinary restriction. `chudcode_tools._effective_names` unions on-demand loads BEFORE the
  mode policy so a user-picked Ask/Plan stays read-only whatever the model loads; Plan is
  "the agent set minus mutators and fs_write/fs_edit/run_shell", not the contract's broad
  PLAN_TOOLS. Then meta/bug-report/`verify_battery` floors, then advertise and dispatch the
  exact same effective set.
- **Host-tool registration is an adapter, never a second catalog.**
  `chudcode_tools.build_host_tools` derives names, descriptions, schemas, load mode and
  handlers from the live MCP/mission registries. Do not advertise unbound or async-only
  entries. Chudcode builtin names (`read`, `edit`, `bash`, `browser`, `computer`, `task`,
  etc.) remain Chudcode-owned unless a CAD entry explicitly claims ownership.
- **Every host call still crosses CAD's chokepoints.** Before execution:
  policy refusal → cancellation → mission/run dispatcher → scaffold and guardrail checks →
  `agent_turn._dispatch_tool` loop guard/accounting. Preserve document context injection,
  progress and cancellation callbacks, JSON-safe results, and machine-readable refusal
  shapes. Tool exceptions propagate as Chudcode tool errors; never turn them into a
  successful result.
- **There is no classification step (2026-09-03).** A chat send is ONE Chudcode
  `general_agent` turn with the user-picked mode (Agent | Plan | Ask, `CopilotMode` 0/1/2)
  and `options.scope = {doc_id, feature_ids, selection}` whenever a document is open (rendered
  as the ACTIVE DOCUMENT block by `worker_rpc._editor_context_block`). The composer never
  keyword-routes (no `agent_ask_v2`/`agent_simulate_v1`/auto-mission/edit-scope paths), never
  creates or replaces a document (that is the agent's `new_part` decision), and never inlines
  the transcript into the request (the persistent Chudcode session is the conversation;
  `options.history` seeds a fresh one once). The GUI has no Routing card and no session
  `scopeClass`; do not reintroduce `task_route`/`agent.classification` consumers. The offline
  exception is deterministic: general work refuses without an online provider, while
  offline-safe local modeling may use the native C++ pipeline (which keeps its own
  complexity/domain classification for its stage structure). Explicit Chudware mission/DAG
  policy remains CAD-owned but its provider calls run through Chudcode.
- **Prompt override isolation (2026-08-07):** native/core C++ prompts consume
  `appdata/prompts/<id>.md`; YAML mission roles consume
  `appdata/prompts/<id>.mission.md`. Settings catalogs both as `core:<id>` and
  `mission:<id>`. Never collapse them: overlapping ids such as reviewer/fixer have
  incompatible output contracts. The top-level `system_prompt` setting intentionally
  replaces role prose but still receives harness base, capability, tools, skills, and
  runtime output enforcement.
- **Shutdown follows ownership.** `core/hostd/worker_bridge.cpp` owns the persistent
  services worker. Its clean-shutdown path stops that worker; `worker_main.stop_services`
  first closes the private control server, then `chudcode_shutdown()` disposes all live and
  transient RPC clients. Keep process-tree termination bounded and never leave a session
  process detached from its owning worker.

## Commit / push template

```bash
./build.sh   # or scripts/gate.sh for engine/agent work — must be green
git add <files> && git commit -m "<area>: <what changed>"   # area: core|engine|agent|mcp|gui|icons|scripts|docs|skills
git push origin master
```

Then hand the user the verification checklist. If a change alters repo structure or
conventions, update this skill (or the affected gui-* skill) in the same commit — these
skills are version-controlled precisely so they stay current for every contributor.
