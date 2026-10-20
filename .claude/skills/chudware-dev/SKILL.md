---
name: chudware-dev
description: Generalist skill for ANY change to the Chudware program in this repo — engine/kernel work, agent harness, MCP server, contracts, scripts, tests, docs, bug fixes, features — whenever the task isn't purely GUI (purely-GUI batches go to gui-orchestrator). Carries the working rules, build/gate/smoke commands, and the load-bearing daemon, document, sim, and manufacturing invariants; read it before searching the repo or asking basics.
---

# Chudware Generalist Dev

Base skill for every non-GUI change. Pointers:
- GUI batches: `.claude/skills/gui-orchestrator/SKILL.md` (panel maps, specialist skills).
- Online agent boundary (sessions, providers, auth/models, memory, compaction, host tools,
  events, control lane, recovery, release artifact): `.claude/skills/cad-chudcode-integration/SKILL.md`
  is canonical — this skill does not repeat it.
- Repo/architecture map: `AGENTS.md` + `ARCHITECTURE.md`. Build/gate truth: `build.sh` and
  `scripts/gate.sh` headers. Task board policy: `work/WORKFLOW.md`.
- Long causal histories live in `docs/REFACTOR_LOG.md` / `docs/refactor-log/` and
  `docs/ops/HOST_CRASH_FORENSICS.md`; rules below carry only the invariant + its pin.

## Working rules

- **Act on evidence.** Resolve ambiguity from the repo first. Ask only about a material
  choice the repo cannot answer, and batch every question into one question-tool call.
- **Master only, no worktrees.** No feature branches or PRs. Never create git worktrees
  (they branched from stale bases and silently reverted master; `.claude/settings.json`
  pins `worktree.bgIsolation: none`). Surface stray branches with unmerged commits; never
  merge them silently.
- **The tree is shared and usually dirty.** Record `git status` once; leave unrelated paths
  untouched; give parallel agents disjoint write-sets in the main tree. Never
  `checkout`/`pull`/`stash`/`reset` over uncommitted work. Stop only when a file you need
  carries conflicting user edits.
- **Commits belong to the integrator.** Subagents never run git. The session driver commits
  only when the user asks or the repo workflow expects it, stages explicit paths/hunks
  (never `git add -A`/`.`), and never commits the user's own edits.
- **Verification is centralized and includes runtime.** Workers return changed files plus
  the exact checks; the integrator builds once across the union (concurrent `./build.sh`
  runs race the Ninja lock) and then runs a runtime smoke — scenario harness, MCP/hostd
  call, or headless `run_mission.py` turn as the change warrants. A green build alone is not
  done. For UI changes, inspect the actual changed surface; add a user acceptance checklist.
- **Locate code:** graphify per `AGENTS.md` workflow item 1, then grep.
- Notable multi-file efforts append a one-paragraph handoff to `docs/REFACTOR_LOG.md`
  (files, deferred items, gotchas). If a change alters structure or conventions, update the
  affected skill alongside it.
- **Browser tenant cells:** web contract 2 admits a method only if
  `contracts/cell_methods.json` classifies it `allow` for the caller's right
  (owner/manager/editor/viewer); unclassified methods are refused and
  `scripts/cell_methods.py` must stay complete. The gateway (`cells.py`) reaches one
  socket-activated, bwrap-isolated cell per workspace (`chudware-cell@<hex>`) with a
  principal hello; actors run in nested `chudware-actor-sandbox` lanes with server-minted
  handles and typed `not_found` for foreign/absent/malformed ids. Never add raw forwarding,
  unsandboxed fallbacks, ambient credentials or an admin bypass. Contract:
  `contracts/worker_rpc.md` cell addendum; acceptance: `Webservice/deploy/tests/test_two_tenant_cells.py`.

## Repo pointers AGENTS.md does not carry

| Path | What |
|---|---|
| `core/device/**` | Device identity/capability truth, transports, Grbl codec/adapter, emulator |
| `core/manufacturing/**` | Journal + durable `seq`, result registry, invalidation, tier0, policy, gates, approval, lease, arbiter |
| `core/twin/**`, `core/print/**` | Manufacturing digital twin; additive-print model (standalone libraries) |
| `services/manufacturing/**` | Gateway + allowlist, printer adapters, slicer (stock PrusaSlicer, vendor presets, G-code verifier), print media, monitor, metrology, PnP, robot |
| `services/dfam/` | Deterministic design-for-additive analysis (walls, overhangs, supports, holes, voids, adhesion, bed fit, orientation) |
| `services/perception/**`, `training/perception/**` | Perception runtime and reproducible training |
| `Webservice/` | chudware-web: browser WASM `host/` over one shared hostd (`gateway/`, `deploy/`, `DESIGN.md`). Secrets never committed |
| `scratch/` | Throwaway output; nothing load-bearing |

`CHUDWARE_*` env vars are canonical; the MCP id is `chudware`.

## Commands

```bash
./build.sh [app|core|daemon|all|wasm|stage|clean]   # modes/env in build.sh; host/build-linux/bin/chudware
scripts/gate.sh                  # THE gate: check.py → core build+ctest → host build → pytest → shot smoke
python3 scripts/check.py         # source/audit gates; use the provisioned runtime interpreter
scripts/provision_venv.sh        # .venv-mcp; gate.sh resolves its python (override CHUDWARE_GATE_PYTHON)
./build.sh stage                 # dist is FIRST in hostd lookup: stale dist = stale daemon
CHUDWARE_SHOT=<scenario> ./host/build-linux/bin/chudware --ui-screenshot scratch/smoke.png   # headless runtime smoke
CHUDWARE_STARTUP_TRACE=1 ./host/build-linux/bin/chudware   # stderr startup milestones, milliseconds from main entry
scripts/run_mission.py --workspace <project> --sessions-dir ~/Documents/Chudware/Sessions --prompt-file p.md   # headless agent turn; --resume
```

- Gate SKIPs (not failures) when OCCT or the runtime venv is absent. Scripted GUI launches
  use `xvfb-run`; `CHUDWARE_SHOT_HEADED=1` shows a window.
- The `tet10_thin_wall` ctest (RUN_SERIAL) needs a large solve budget. Only when RAM is
  otherwise free may a gate run use `CHUDWARE_HEADROOM_GB=2 CHUDWARE_MEM_FRAC=0.6`.
- Mixed pytest collections: root `conftest.py` isolates non-MCP engine tests per test;
  `services/mcp/conftest.py` opts its daemon tests out at collection.
- Gate Python coverage includes service units, scripts, assets, tools and training;
  the packaging stage covers the entire `packaging/` suite
  using the same selected runtime interpreter. `check.py` also runs the shared-UI
  manifest/asset audit. Astro `.astro/` metadata is ignored local state; do not commit it.
- **Polyhedral-FEA pin:** `CHUDWARE_POLYMESH_ROOT=<tree> ./build.sh core` builds a local
  tree, but before shipping an API change validate the public pin in
  `core/cmake/ProvisionPolymesh.cmake` with no override: the configure log must say
  **PINNED FETCH**, not **LOCAL SOURCE** (clear `POLYMESH_SOURCE_DIR`, reset an overridden
  `POLYMESH_ROOT` to `<CAD>/third_party/polymesh`). There is ONE public repository
  (`Hunter-124/Polyhedral-FEA`, branch `master`); the private fork is retired. Land polymesh
  work on public `master` first, then move the pin to that commit. polymesh must stay
  subproject-safe (`PROJECT_SOURCE_DIR`, CPack only when top-level).

## Resource safety

- **Never** run `ulimit -v/-m/-d`, or `prlimit` against a pid you did not spawn, in an agent
  bash call: that shell is in-process with the agent host, so the cap lands on the host and
  aborts it (`docs/ops/HOST_CRASH_FORENSICS.md`). Cap memory only by wrapping the native run:
  `systemd-run --user --pipe --wait -q --collect -p MemoryMax=12G bash -c 'ulimit -c 0; …'`
  (cgroup kill instead of host abort; `ulimit -c 0` keeps the coredump pipeline out).
- Sim memory budget formula and knobs: `core/util/resource_governor.h`. On a busy box
  `avail − CHUDWARE_HEADROOM_GB` binds, so free RAM (close builds/GUI) before a fine leg
  instead of raising `CHUDWARE_MEM_FRAC`. Agent runs: `CHUDWARE_SIM_THREADS=4`, at most one
  >4 GB solve per agent at a time.

## Engine and contract rules

- `contracts/*`, `core/api/libchudware.h`, top-level CMake, `scripts/gate.sh`, `AGENTS.md`
  are append-only and integrator-owned; subagents request edits instead of making them.
- Additive/removal features in `core/doc/document.cpp` use `geom::fuse_unify`/`cut_unify`,
  never plain `fuse`/`cut` (split coplanar faces read as "not fused").
- **Sketch ops never compile uncached.** An op handler that needs compiled geometry (sketch
  plane, external-ref projection) reads it through `apply_ops_locked`'s session apply context
  (session CompileCache + display quality); the associative refresh reuses the session-owned
  external projection cache (per-model-state descriptors + per-ref memo). An uncached
  `compile_document` there made one Convert Entities click cost ~11 s on a 341-edge part
  (now ~0.2 s). Sketch residuals never reward a free line collapsing (normalized
  collinear/point-on-line, angular parallel/perpendicular).
- Any change to compiled geometry/topology bumps `stepplus_cache_engine_key`. After
  `./build.sh stage` an auto-spawned dev hostd serves the old engine until it drains — check
  `chudware-hostd --status` before trusting an MCP smoke.
- A geom op that rebuilds faces of an existing body records history
  (`geom::op_history_record`, or `op_provenance_encode`/`_record` across
  `isolate_shape_build`'s sidecar) or every rebuilt face is attributed to that feature
  (`core/geom/op_history.h`).
- **Never mutate a geom op's input.** Compile checkpoints hold live TShapes and
  NonDestructive booleans share untouched sub-shapes with their argument; OCCT
  UnifySameDomain edits its input's edges in place, so unify runs on a `BRepBuilderAPI_Copy`
  (`geom::unify_same_domain`, `dress.cpp` join) with the copy's history recorded. A cut whose
  exact volume grows fails (`require_cut_removed_material`, `core/doc/document.cpp`).
- `core/agent/run.cpp` calls doc ops in-process; every direct `doc::doc_new`/`doc_apply_ops`
  site invokes the `chudware_set_doc_publish_sink` callback or its result is invisible to
  clients until the run ends. `internal:true` keeps propagating (preflight shadow clone).
- **Preflight is bounded advisory work.** `preflight_ops` spends at most a 10000-ms
  candidate budget via `doc_dry_run_ops(budget_ms=...)`; the whole scratch rebuild
  stays in an expendable process group, never an unbounded parent fallback. Expiry
  is `verdict:"skipped", skipped:"budget_exceeded"`, not failed geometry or a radius
  verdict. Metadata/lock/transport/cleanup are outside that hard candidate bound.

## hostd / daemon lifecycle

- **Never stop a hostd you did not spawn** or one serving the user's GUI/agent children.
  Run `chudware-hostd --status` first. Semantics (`core/hostd/main.cpp`): bare `--stop`
  refuses while agent children run; `--force` stops them (TERM → grace → KILL); a dead
  control socket with a live pidfile escalates SIGTERM → 3 s → SIGKILL.
- **Work, not sockets, keeps hostd alive.** Idle-exit and drain gate on `pinningClients()`,
  never raw `status_.clients`: a `gui` client always pins; others pin only with an engine call
  in flight, an attached doc, or an RPC inside the idle window
  (`test_hostd_registry.py::test_parked_client_does_not_pin_the_daemon` + `…_attached_client_still_pins_the_daemon`).
- **One Python worker = one writer, concurrent leased connections.** Online turns own a
  `daemon_bridge.TurnConnections` pool. A host call leases one independent socket; idle
  sockets survive short-lived RPC threads until the turn ends. Standalone MCP retains its
  per-thread fallback. All sockets send the process `writer_group`; `doc_registry` counts
  a group once for rw/CAS/`_foreign_edits`/holds
  (doc lifetime stays per connection). Events never drop silently (2 s wait, then disconnect for
  resync). `doc_list`/minting never wait on another doc's work (snapshot, `busy:true`). Each
  agent has its own current part (`cad_common.agent_doc_context`). Contract: worker_rpc.md
  "Writer groups…" (2026-10-12).
- **Agents of different chats share a part by arrival order.** `core_bridge` sends implicit
  `doc_apply_ops` with `base_revision: -1` (no hostd CAS between writer groups); the engine
  DocLock applies batches one at a time, each atomic and validated. Rollbacks never wipe a
  foreign commit: `core_bridge.revert_own_batch` reverts only when the checkpoint revision + 1
  is the batch revision, with `doc_revert expect_revision`; probes delete their own feature.
  The end-of-turn save sweep skips untouched agent docs another writer group holds rw
  (`doc_list.other_writer`). No semantic conflict check by design (`test_same_doc_writers`).
- **Background GUI queries stay off the primary socket.** Document-free metadata/worker-
  bridged queries from non-primary threads use per-thread `gui-background` sockets (exact
  allowlist in `services/ipc/ipc_client.cpp`, reached through `services/worker/engine_backend.cpp`);
  document/unknown methods stay primary. Background clients never get the unconditional
  `gui` pin; recent/in-flight RPCs still pin like other non-GUI clients. Pinned by
  `test_worker_long_run`: `backgroundQueriesDoNotBlockPrimary`, `backgroundQueryEofIsIsolated`,
  `backgroundQueryTimeoutIsIsolated`, `disconnectWakesAllBackgroundQueries`.
- **Drains are bounded.** `Server::beginDrain(deadlineSecs, reason)`: `daemon_drain`/`--stop`
  (`stop_requested`), tray Quit (`quit`, 5 s) and SIGTERM are hard stops with a deadline
  (`CHUDWARE_HOSTD_DRAIN_GRACE_S`, default 20 s). The stale-binary self-drain passes `0`
  (`stale_binary`; soft), so a rebuild never kicks a live session.
- **A draining daemon refuses new clients by name.** Non-control clients get a
  `daemon.draining` event plus a `daemon_draining` hello error (`Server::drainRefusal`);
  `--status`/`--stop` are admitted. **No client falls back to an in-process engine** on it:
  Python raises `CoreError(code="daemon_draining")`; the GUI stays in daemon mode, names the
  drain on its unreachable chip (`EngineClient::helloError()`, `engine::daemonRefusal()`), and
  reconnects or spawns the new build after the old daemon exits (`test_worker_long_run`:
  `drainRefusalKeepsDaemonMode`; `docs/reference/HOSTD.md`).
- **Doc-GC** (`core/hostd/doc_registry.*`): `CHUDWARE_DOC_GC_GRACE_S` (90 s); agent/mcp-origin
  docs floor at `CHUDWARE_DOC_GC_AGENT_GRACE_S` (1800 s) so mission parts stay openable while
  the GUI exists; `CHUDWARE_DOC_GC=0` disables. The grace never pins the daemon; a clean exit
  discards — recovery is crash-only (`test_clean_shutdown_discards_docs`).
- **Worker lanes** (`WorkerBridge::forMethod`, `core/hostd/worker_bridge.cpp`): a new stateful
  agent RPC family joins the agent-lane prefix list or lands in a process blind to Chudcode
  sessions. Files shared across worker processes need flocks, not threading locks.
- **Concurrent turns: one turn per agent worker.** `WorkerBridge::dispatch` runs each
  `agent_admin_turn` in a pool worker (primary `agent` + on-demand `agent-turn` extras, cap
  `CHUDWARE_AGENT_WORKERS`, default 4; extras idle 10 min are reaped at the next selection).
  A conversation is owned by one worker: its turns and session-scoped calls go there; the same
  conversation never runs twice at once. Moving it to a free worker first makes the busy owner
  `release` (dispose) its idle Chudcode runtime over the control socket; a refused/failed
  release keeps the conversation waiting for its owner. Two live runtimes of one session fork
  its history: a disposed stale runtime writes `session_exit` at its old leaf and the next
  resume continues from there (`test_busy_owner_releases_a_conversation_before_another_worker_resumes_it`).
  Worker forks run on one daemon-lifetime thread: PDEATHSIG follows the forking THREAD, and
  a worker forked on a connection thread died with that client (same rule as ChildSupervisor).
- **Nothing waits behind a turn.** `WorkerBridge::stop()` cancels the active run over the
  control lane, waits ≤10 s, SIGTERMs, never respawns (`test_sigterm_mid_turn_ends_the_turn_and_the_daemon`).
  A non-turn call behind the primary's `agent_admin_turn` fails at once with `worker_lane_busy`;
  other lane waits end at the call's own timeout (`test_agent_lane_call_behind_a_turn_fails_fast_and_hostd_survives`;
  `provider_usage.cpp` treats busy as silent). hostd never replays an `agent_admin_turn` after
  worker loss (`worker_lost_mid_turn`). Stop/steer/answer route by `run_id` across the pool.
- **No GUI call waits on a turn client-side.** In daemon mode each `agent_admin_turn` leases its
  own agent run connection (`engine_backend`, ≤8) and takes no client mutex, so turns of
  different chats overlap; native `agent_run_v2`/`agent_mission_v1` still serialize on
  `WorkerClient::longRunMtx_` (`test_worker_long_run`). Daemon turns never fetch
  `worker_control_info`; control goes through hostd.
  An async tab switch stays selected until activation lands or fails (`core::SwitchHold`, `test_switch_hold`).
- **CEF fetch daemon** (`host/src/ui/widgets/cef/cef_daemon.cpp`): `start_idle_watchdog` is
  armed before `initialize()`; every activity update goes through `mark_activity()`. Spawn is
  single-flight with the spawned pid recorded and verified (`services/browser/cef_client.py`
  `_daemon_alive`); concurrent and sequential spawners yield exactly one process. A CEF-OFF
  build claims and refuses `--cef-*` argv with the `std::fflush(nullptr); std::_Exit(n)` tail
  (`host/src/main.cpp`), never a plain `return`. The client watches the child it spawned: a
  missing binary fails at once as `browser_helper_missing`; a helper that exits before it
  listens fails as `browser_helper_exited` (stderr tail from `<sock>.spawn.log`).
- GUI reconnect: sticky unreachable chip; `engine_events` re-bootstraps tabs on
  `reconnectGeneration` (`CHUDWARE_SHOT=daemon_h2`).

## Documents, saving, selection

- **hostd is the only autosave authority in daemon mode**; the guard sits at the single write
  in `cad_common._autosave()`, not its callers. The standalone MCP server keeps snapshots.
- **AutoCAD files are exact B-rep both ways.** DWG/DXF/SAT ACIS solids import natively
  (`acis_read.cpp`; `dwg_native.cpp` for every DWG version) — LibreDWG `dwg2dxf` is only for
  2D entities: it drops SAB data storage and SURFACE classes and splits inline SAT, so
  handle-keyed native ACIS always wins. Exports write ACIS 3DSOLID/BODY (SAT 400 in R2000
  DWG/DXF, 700 `.sat`) in the import's `source_units`, and every dxf/dwg/sat export is re-read
  (solids, validity, volume). The ODA File Converter is a scratch validation oracle, never product code.
- **The build state is saved with the file.** `doc_save` writes `cache.json/aicm/brep` plus
  per-feature compile checkpoints `cache.ckpt` (contracts/stepplus.md §9); `doc_open` hydrates
  `compiled` + `compile_cache` from them, so a valid cache opens warm (first op executes 0
  features, first edit only the tail). Every skip is reported (`cache.skipped_reason`,
  `cache_miss_reason`), never swallowed; an oversized cache entry is a miss, never a failed
  open (`test_appearance` "persisted build state" cases). **A rebuild is saved back too**
  (§17): a `doc_open` that missed (engine-key bump, outside edit) and an assembly load of a
  stale component write the fresh build into that file via `stepplus_write_build_state`, which
  replaces only `cache.*`/`geometry.step` (every other member copied byte for byte, refused if
  the file changed since the read). Never route that through `stepplus_save` — it rewrites
  `meta.json`/`thumbnail.png` and the vault shows the file as modified.
- **Mission docs are saved, never closed.** `cad_common.save_touched_docs()` runs at every
  turn terminal (`mission_dispatch.run_linear_role_turn`) and in the crash-path `finally`
  (`worker_rpc._agent_admin_turn`), writing docs hostd would retire into
  `CHUDWARE_PROJECT_SAVE_DIR`. It never touches `origin_kind=gui`, never writes an empty doc,
  skips pristine reproducible imports (`reproducible_from`) and docs recorded in
  `_DISCARDED_DOCS`.
  The per-batch `_project_save` behind `add_features`/`new_part` skips `origin_kind=gui` too:
  agent edits to a user-opened doc stay live (dirty, hostd-autosaved) until an explicit
  save, never auto-written into the user's file (`test_project_save`).
  Study-only detached native imports deliver to an unused project filename.
  Non-study authored imports retain source-save behavior unless another GUI doc
  holds that source (`doc_list.native_source_edited`; worker RPC six-axis addendum).
  Loaded study IDs must not collide with newly minted IDs; `_persist_study` keeps
  the `study_id` returned when a legacy duplicate is disambiguated.
- **One design = one document; names carry no version.** A version token resumes the series
  (`_docs_in_version_series`; `fresh=True` escapes) and `_strip_version_suffix` normalizes all
  three `doc_new` mint payloads. Series lookups see the raw name; the strip is skipped when a
  doc of that exact name is open (`test_exact_name_match_still_goes_through_the_engine_mint_lock`).
  `sanitize_part_name` (`core/agent/run_stop.cpp`) mirrors the token set — keep in lockstep.
- **Save paths.** `save_part`/`export_part` with no path go to `CHUDWARE_PROJECT_SAVE_DIR`
  (temp only for headless external MCP clients with no project); a doc already in a real
  `.step+` saves in place; an explicit path removes the harness's own stale root copy
  (`_HARNESS_SAVED_PATHS`, `removed_stale_copy`). `save_part` refuses
  `path_open_in_user_tab` over a file a user-opened doc holds (`cad_common.user_doc_open_at`):
  the agent edits that doc in place. Empty docs refuse save/export unless `allow_empty`.
  Explicit relative destinations resolve against `CHUDWARE_PROJECT_DIR`, then
  `CHUDWARE_FS_ROOT`, never worker cwd or an ancestor repository. A selected nested
  directory stays the project root; an invalid explicit root refuses rather than
  falling through. Absolute/`~` exports still enforce credential-path checks.
- **PLM tools.** `plm_item_add` is for non-design files (`.step+`/`.ecad+` auto-register on
  save → `plm_checkpoint`); its `path` is the project root, the tracked item goes in
  `file_path`/`folder_path`. `plm_*` default `path` to `CHUDWARE_PROJECT_DIR` →
  `CHUDWARE_FS_ROOT` → `project_context.resolve_project_root()`, else
  `project_root_unavailable`; document-scoped AI-VC ops take no project root.
- **Agent edit lock (interim #55).** A doc written by a non-`gui` client locks every human
  mutation path (`AppState::docLockedByAgent`, banner + Stop) while the writing run lives, or
  for 8 s. Sessions on it keep-exit and never `doc_revert`. A new human mutation path must check
  the predicate (`CHUDWARE_SHOT=agentlock`).
- **The user's view.** An agent-opened doc never takes focus from the user's tab
  (`openMirrorTab`). Agent edits reach the active tab live: `engine_events.cpp` defers
  `doc.changed` only while `userMidEdit`; a revision without an `aicm_path` key keeps the mesh
  (only an empty `aicm_path` means empty) and the active view catches up via
  `core/tess_catchup.h` (`test_tess_catchup`). A part the agent is editing stays selected.
- **One working-directory authority.** `AppState::workspaceDir` drives the top-left
  chip, `plmProjectPath()` and `convProjectDir()`. Opening or switching to an external
  document never changes it; only an explicit working-directory selection does.
- **Chat sessions follow the workspace of their latest turn.** `bindActiveConversationProject`
  restamps `Conversation.projectDir`; `index.json` v3 keeps `active_by_project`; Sessions
  filters the stored key (never a document-directory inference). Startup and shared-store
  reload reconcile the restored chat to the selected root, validating remembered
  sessions against their own stored project key. A root switch restores its last chat
  or creates an empty one, parking active runs rather than stopping them. Explicit
  cross-project history viewing does not rewrite another root's remembered session.
  `CHUDWARE_SHOT=projectchat` exercises these transitions and real outside-file opening.
- **Steers split the running bubble** (`composer.cpp::appendSteerToTranscript`, persisted
  `steerSplit`/`"split"`): append-only, so AI-VC `msg_idx` anchors never shift. The steer's
  user bubble carries `steerId`/`steerState` (persisted `"sid"`/`"steer"`; open states load as
  failed) and a caption under it: queued / read / unread-sends-next / not delivered
  (`CHUDWARE_SHOT=steerstatus`).
- **Question cards draw where asked:** `agent.question` appends a rowKind-20 timeline marker
  (`__ask__:<batch_id>`) and the card renders at it; restored pre-marker batches anchor after
  their `ask_user` tool row on load. Never draw a marked batch after the turn again.
- The model picker has no default-hiding pass: `hidden_models` is only the user's own choice;
  `loadSettings` drops a list carrying `model_defaults_seeded`.

## CAD-side agent policy

- **Headless turns are `do_it`:** `resolve_turn_preferences(cfg, headless=True)` is the one
  forcing site (mission roles, `chudcode_roles._assemble_role_runtime`, `run_mission.py
  --mode agent`). The interactive `check_with_me` default never governs unattended callers.
- **Auto never changes the mode.** `TurnPreferences.mode` (user pick) is the only mode source;
  a chat-classified prompt gets cheaper settings, not a read-only roster
  (`test_chat_classified_question_keeps_the_agent_mode_the_user_picked`).
- **Refine cap** (`scaffolds.should_stop_refine`, tail of `agent_turn.run_turn`, stop reason
  `refine_cap`) counts only geometry reworked after a settler (`_GEOMETRY_SETTLERS` =
  verify_battery/save_part/export_part). Never add describe/measure/render to the settlers.
- **Prompt overrides stay split:** core prompts read `appdata/prompts/<id>.md`, YAML mission
  roles `<id>.mission.md` (`core:`/`mission:` in Settings); overlapping ids have incompatible
  output contracts. `system_prompt` replaces role prose but keeps harness base/capability/
  tools/skills/output enforcement.
- CAD skill packs live in `services/agent/skills/builtin/<name>/SKILL.md` and reach agents
  only via `cad_skill(name)` (e.g. `simulate`, `assembly-merge`). Agents never hand-edit
  STEP/JSONL text.
- **Visual review is numbers first, then one planner grid.** `render_part` without `view`
  returns one 2×2 grid chosen by `services/mcp/shot_planner.py` from native
  `doc_view_metrics` (x-ray/section tiles for hidden targets; SID face tags accepted
  verbatim); explicit `view=` stays one shot. Keep image blocks end to end
  (`services/agent/tool_images.py`); never re-add render-after-every-mutation. Append new
  candidate kinds inside `plan_shots`, never in `candidate_cameras`/`section_candidates`
  (replayed by index).
- **ViewNet training is LLM-free** (owner decision): no VLM/LLM calls in
  `training/viewpoint/**`; observer queries carry no geometry (`blind`/`qvec_probe` rows sit
  at majority). Launch via `campaign.sh service [stages]`, never edit `campaign.sh` while it
  runs, write artifacts via `training/viewpoint/durable.py` (fsync before rename).

## Simulation and assemblies

- **Evidence-grade or not a result.** Sim outputs carry top-level `mesh_quality`, `reactions`
  (static/adaptive), `provenance` (`engine_build_id`, `polymesh_commit`, `governor`) as
  siblings of `summary`; ladder rows carry `delivered_size_mm` and a named reason
  (`degenerate_ladder`); `size_delivery` names legs outside 0.8–1.25× (the `.vtu` name keeps the
  requested `h`); under-resolved walls get `mesh_quality.underresolved_wall` + caveat;
  `min_size_mm` lowers the size-band floor when a refusal names a smaller size. Release path:
  `simulate` CAD skill (`run_fea_convergence` → `sim_evidence_packet` → `polymesh_cli`).
- **The governor measures, never extrapolates:** element counts come from probes
  (`provenance.governor`), shared across ladder legs; explicit-h bbox forecasts are
  diagnostics, never coarsening orders.
- **Refuse before meshing; never go silent.** Contact preflights (interface classification,
  selector span, `normal_cone_excludes_load`) run before any fill; every sim job writes phase
  events + a 30 s heartbeat to `partial.json`; silence >60 s is a defect; delayed refusals
  keep diagnostics; job `_reconcile` observes exit before reading `result.json`.
- Contact/static studies model joints: per-body `material`, `interfaces` (press fits bonded,
  running clearances contact; declared pairs stay unfused; only contact pairs refine);
  `closed_fit` applies below h/4 clearance unless disabled. Buckling refuses by name at
  `max_load_factor`; steady thermal is native (`fea_thermal_solve`; ccx is V&V only).
  `boundary_faceset_membership` admits a facet only when every corner passes.
- Multi-body figures (`render_study_setup`) offset per-body AICM face ids via
  `_mesh_topo_index`; never alias one body's faces onto another.
- **An agent's sim is a study the user can open:** `run_fea_*`/`simulate_motion` persist via
  `_persist_study` and hostd fans `doc.studies_changed`; new agent sim tools do the same.
  Piping/hydraulic/industrial work persists the same way: `piping_network`, `pipe_flow`,
  `hydraulic_circuit`, `vessel`, `duct_system` studies, shared by the GUI Piping/Hydraulics
  panels and the `piping_*`/`hydraulic_*` tools (`piping_load`/`hydraulic_load` restore).
  Civil/site work (`services/civil/`, `contracts/civil_schema.md`, SI metres) persists one
  `civil_site` study per model through `services/mcp/civil_common.py` (a changed model drops
  its results; `civil_load` restores); the `civil_*` tools have no GUI panel yet.
- **Assemblies report honestly:** bbox/mass/import gather world solids (`measure_part` is the
  MCP mass tool; without `density_g_cm3`/`material=` each body weighs at its applied material —
  body override, part material, component source material — and 7.85 steel only where none
  resolves; `material.source` says which); `check_interference` refuses an empty doc and reports `pairs_checked`;
  a complete check is `ok:true` with the verdict in `clear` (interface_check: `status`), so
  a found clash is a finding, not a failed tool; `ok:false` means refused or incomplete;
  `validated: true` needs every component's solids and an exact round trip; a no-op
  `doc_solve_mates` is not an edit (no revision/dirty/undo). Importing a `.step+` is a fresh
  detached doc (`resume=True` opts in). Merges use the `assembly-merge` CAD skill
  (`keep_bodies`/`exclude_bodies`, `fit_cylinder_axis` on the true surface); exports warn
  `placement_unverified` until `check_interference` runs.
- Mates: hinge-line angle mates are revolutes; a throwing residual builder is
  `reason:mate_unresolved`, never converged; `sweep_envelope` transports world-shape bounds.
  Native mates persist engine-owned `reference_frames`; scalar edits and save/reopen never
  redefine zero, exact-current-value sweeps never apply the angle twice, and legacy binding
  keeps its first-observed interpretation.
- `compile_document(..., with_display=false)` is the assembly-only lane: keep the initial
  per-body meshing; compile-cache identity includes the mode; selective
  `stepplus_read_document`/`stepplus_open_component` skip unrelated payloads while editable
  `stepplus_open` preserves them; a `preserve_path` save invalidates the live display cache
  without a revision change.
- `add_features mode:"new"`/`merge:false` is an independent body; `add`/boss fuse only with
  touching bodies; unknown modes refuse by name; `centered:false` primitives are not offset;
  `transform` moves (`copy:true` duplicates); `project_external_refs` resolves against the
  whole document.

## Manufacturing and print

- The `manufacture` MCP family is default-OFF; the arbiter is the only approval validator;
  the device allowlist denies by default.
- `FreshValue<T>` is the sole external freshness shape; the journal is the sole source of
  durable `seq`.
- Nothing advertises physical `execute` or `safety_integration` before the E4–E6 pilot;
  `mfg_run_resume` exists only to refuse.
- 3D printing goes through `slice_part` (`services/mcp/print_tools.py`, family `cam`, not the
  gated `manufacture` family): stock PrusaSlicer via `prusaslicer.discover()` (flatpak always
  `--command=prusa-slicer`; `scripts/provision_slicer.py --install`), vendor presets read at
  runtime (never copied), orientation/supports from `services/dfam`, then `gcode_check`.
  `print_ready` requires a passing check (`floating_extrusion` catches mid-air perimeters).
  Agents never download a slicer or hand-write slicer keys. PrusaSlicer 2.9.6 CLI (re-verify
  on upgrade): no `--arrange`, bare `--merge` segfaults headless (use `--dont-arrange --merge`),
  and no sequential-clearance check.
- **Slicer input preserves topology.** Single-part STL placement is origin-centred with a
  float32-grid-aligned shift; PrusaSlicer places it on the bed in double precision.
  Newly merged vertices or zero-area triangles refuse as `slicer_mesh_precision_loss`,
  never heal silently. Requested 3MF evidence must report zero load-time repairs.
  DFAM tool reach is Euclidean; trapped-support failure applies per connected cluster,
  not the sum of unrelated warning crumbs. Narrow and sealed cavities still fail.
- Multi-part plates (`complete_individual_objects=True`, `slicer/plate.py`): hulls spaced by
  `extruder_clearance_radius` + brims + 2 mm, ascending height, at most one object above
  `extruder_clearance_height` (printed last); `gcode_check`'s `sequential_clearance` re-proves
  it from the toolpath; one G-code + manifest is one `publish_print_media` job;
  `complete_objects` in `overrides` is refused.
- **Printer media is published, never copied:** `print_media_inventory` (read) and
  `publish_print_media` (mutating; Ask/Plan refuse via `contracts/copilot_modes.json`) wrap
  `services/manufacturing/print_media.py`; humans use `scripts/print_media.py`. The printer UI
  shows no timestamps, so stale duplicates are ambiguous. Publish takes the complete job set,
  the whole-drive `inventory_token` and explicit `media_idle=True`, reads back hashes,
  and permanently deletes deprecated managed toolpaths after verified commit; no G-code
  archives on media or host. Jobs must be `print_ready` unless the job carries the user's
  explicit `user_override`: it then publishes with `failed_checks` recorded in the media
  manifest, README, inventory and a `published_not_print_ready` warning. Agents never refuse
  that instruction or hand-copy around it. Default `layout="root"` writes raw stable part filenames at the
  drive root; `layout="project"` permits one top-level project folder, never nested delivery
  trees. Tracking metadata stays in hidden root files. Legacy managed trees migrate and
  obsolete managed archives are deleted; unknown files and other projects remain protected.
  Temporary non-printable staging exists only for rollback/recovery and is deleted after
  current files and metadata pass readback. Report every remaining unmanaged printable.
  An agent with a shell can still `cp` — the guarantee is policy + tool, not prevention.
