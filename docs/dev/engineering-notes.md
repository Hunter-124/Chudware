# Engineering notes

Load-bearing conventions and invariants for non-GUI work: engine, daemon, documents,
simulation, manufacturing, scripts and tests. Repository layout and ownership live in
[`AGENTS.md`](../../AGENTS.md) and [`ARCHITECTURE.md`](../../ARCHITECTURE.md); the online
agent boundary is in [chudcode-integration.md](chudcode-integration.md); GUI conventions are
in [gui-architecture.md](gui-architecture.md). Each rule below names the test or code that
pins it, so the rule can be checked against the source rather than trusted.

`CHUDWARE_*` environment variables are the only honored prefix; the MCP server id is
`chudware`.

## Repository pointers not covered by AGENTS.md

| Path | Contents |
|---|---|
| `core/device/` | Device identity/capability truth, transports, Grbl codec/adapter, emulator |
| `core/manufacturing/` | Journal and durable `seq`, result registry, invalidation, policy, gates, approval, lease, arbiter |
| `core/twin/`, `core/print/` | Manufacturing digital twin; additive-print model (standalone libraries) |
| `services/manufacturing/` | Gateway and allowlist, printer adapters, slicer integration, print media, monitor, metrology |
| `services/dfam/` | Deterministic design-for-additive analysis (walls, overhangs, supports, holes, voids, adhesion, bed fit, orientation) |
| `web/` | Experimental self-hostable browser app: WASM host over socket-activated tenant cells (`web/gateway/`, `web/deploy/`). Secrets are never committed |
| `scratch/` | Throwaway output; nothing load-bearing |

## Build, gate and smoke commands

```bash
./build.sh [app|core|daemon|all|wasm|stage|clean]   # modes/env documented in build.sh; binary: host/build-linux/bin/chudware
scripts/gate.sh                  # full gate: check.py -> core build + ctest -> host build -> pytest -> shot smoke
python3 scripts/check.py         # source/audit gates; run with the provisioned runtime interpreter
scripts/provision_venv.sh        # creates .venv-mcp; gate.sh resolves its python (override: CHUDWARE_GATE_PYTHON)
./build.sh stage                 # dist is FIRST in hostd lookup: a stale dist means a stale daemon
CHUDWARE_SHOT=<scenario> ./host/build-linux/bin/chudware --ui-screenshot scratch/smoke.png   # headless runtime smoke
CHUDWARE_STARTUP_TRACE=1 ./host/build-linux/bin/chudware   # startup milestones on stderr, ms since main()
scripts/run_mission.py --workspace <project> --sessions-dir ~/Documents/Chudware/Sessions --prompt-file p.md   # headless agent turn; --resume
```

- A green build is not "done": run the runtime smoke the change warrants (scenario harness,
  MCP/hostd call, or a headless `run_mission.py` turn). For UI changes, inspect the actual
  changed surface, not only the existence of a capture.
- Run one `./build.sh` at a time; concurrent builds race on the Ninja lock.
- The gate reports SKIP (not failure) when OCCT or the runtime venv is absent. Scripted GUI
  launches use `xvfb-run -a`; `CHUDWARE_SHOT_HEADED=1` shows a window.
- Gate Python coverage includes service units, scripts, assets, tools and training; the
  packaging stage covers the whole `packaging/` suite with the same interpreter. `check.py`
  also runs the shared-UI manifest/asset audit. Astro `.astro/` metadata is local state.
- Mixed pytest collections: the root [`conftest.py`](../../conftest.py) isolates non-MCP
  engine tests per test; [`services/mcp/conftest.py`](../../services/mcp/conftest.py) opts
  its daemon tests out at collection.
- The `tet10_thin_wall` ctest (RUN_SERIAL) needs a large solve budget.
- **Polyhedral-FEA pin.** `CHUDWARE_POLYMESH_ROOT=<tree> ./build.sh core` builds a local
  checkout, but an API change must be validated against the public pin in
  `core/cmake/ProvisionPolymesh.cmake` with no override: the configure log must say
  **PINNED FETCH**, not **LOCAL SOURCE** (clear `POLYMESH_SOURCE_DIR`; reset an overridden
  `POLYMESH_ROOT` to `third_party/polymesh`). Land polymesh changes on the public repository's
  `master` first, then move the pin. polymesh stays subproject-safe (`PROJECT_SOURCE_DIR`;
  CPack only when top-level).

## Resource safety

- Never apply `ulimit -v/-m/-d`, or `prlimit` against a process you did not spawn, inside a
  shell that shares a process tree with a long-lived host (an IDE terminal, an agent shell):
  the cap lands on the host and aborts it. Cap only the native run itself:
  `systemd-run --user --pipe --wait -q --collect -p MemoryMax=12G bash -c 'ulimit -c 0; …'`
  (cgroup kill instead of host abort; `ulimit -c 0` keeps the coredump pipeline out).
- The simulation memory budget formula and knobs are in `core/util/resource_governor.h`. On a
  busy machine `available - CHUDWARE_HEADROOM_GB` binds, so free RAM (close builds, the GUI)
  before a fine solve instead of raising `CHUDWARE_MEM_FRAC`. A gate run may use
  `CHUDWARE_HEADROOM_GB=2 CHUDWARE_MEM_FRAC=0.6` only when RAM is otherwise free. Automated
  agent runs: `CHUDWARE_SIM_THREADS=4` and at most one >4 GB solve at a time.

## Engine and contract rules

- `contracts/*` and `core/api/libchudware.h` are append-only: add fields and methods, never
  rename or remove them.
- Additive/removal features in `core/doc/document.cpp` use `geom::fuse_unify`/`cut_unify`,
  never plain `fuse`/`cut` (split coplanar faces read as "not fused"). A cut whose exact
  volume grows fails (`require_cut_removed_material`).
- **Sketch ops never compile uncached.** Handlers that need compiled geometry (sketch plane,
  external-reference projection) read it through `apply_ops_locked`'s session apply context
  (session CompileCache plus display quality); associative refresh reuses the session-owned
  external projection cache. An uncached `compile_document` there turned one Convert Entities
  click on a 341-edge part from ~0.2 s into ~11 s. Sketch residuals never reward a free line
  collapsing (normalized collinear/point-on-line, angular parallel/perpendicular).
- Any change to compiled geometry or topology bumps `stepplus_cache_engine_key`. After
  `./build.sh stage`, an auto-spawned dev hostd serves the old engine until it drains; check
  `chudware-hostd --status` before trusting an MCP smoke.
- A geom op that rebuilds faces of an existing body records history
  (`geom::op_history_record`, or `op_provenance_encode`/`_record` across
  `isolate_shape_build`'s sidecar); otherwise every rebuilt face is attributed to that feature
  (`core/geom/op_history.h`).
- **Never mutate a geom op's input.** Compile checkpoints hold live TShapes, and
  non-destructive booleans share untouched sub-shapes with their arguments. OCCT
  UnifySameDomain edits its input's edges in place, so unify runs on a `BRepBuilderAPI_Copy`
  (`geom::unify_same_domain`, the `dress.cpp` join) with the copy's history recorded.
- Embedded BREP base64 decoding uses an unsigned rolling bit accumulator. Signed
  overflow/left shifts are undefined in C++17 even when only the low output byte
  is retained; preserve the unsigned accumulator when changing persistence code.
- `core/agent/run.cpp` calls doc ops in-process; every direct `doc::doc_new`/`doc_apply_ops`
  site invokes the `chudware_set_doc_publish_sink` callback, or its result stays invisible to
  clients until the run ends. `internal:true` keeps propagating (preflight shadow clone).
- **Preflight is bounded advisory work.** `preflight_ops` spends at most a 10 000 ms candidate
  budget via `doc_dry_run_ops(budget_ms=...)`; the scratch rebuild runs in an expendable
  process group, never an unbounded parent fallback. Expiry reports
  `verdict:"skipped", skipped:"budget_exceeded"`, not failed geometry.

## hostd and daemon lifecycle

Reference: [`docs/reference/HOSTD.md`](../reference/HOSTD.md); contract:
[`contracts/worker_rpc.md`](../../contracts/worker_rpc.md).

- **Never stop a hostd you did not spawn** or one serving a GUI or agent children. Run
  `chudware-hostd --status` first. Bare `--stop` refuses while agent children run; `--force`
  stops them (TERM, grace, KILL); a dead control socket with a live pidfile escalates
  SIGTERM, 3 s, SIGKILL (`core/hostd/main.cpp`).
- **Work, not sockets, keeps hostd alive.** Idle-exit and drain gate on `pinningClients()`,
  never raw client counts: a `gui` client always pins; others pin only with an engine call in
  flight, an attached doc, or an RPC inside the idle window
  (`test_hostd_registry.py::test_parked_client_does_not_pin_the_daemon`).
- **One Python worker is one writer with concurrent leased connections.** Online turns own a
  `TurnConnections` pool (`services/mcp/daemon_bridge.py`); each host call leases its own
  socket. All sockets send the process `writer_group`, and `doc_registry` counts a group once
  for rw/CAS/foreign-edit/hold accounting. Events never drop silently (2 s wait, then
  disconnect for resync). `doc_list` and minting never wait on another doc's work. Each agent
  has its own current part (`cad_common.agent_doc_context`).
- **Agents in different chats share a part by arrival order.** `core_bridge` sends implicit
  `doc_apply_ops` with `base_revision: -1`; the engine DocLock applies batches one at a time,
  each atomic and validated. Rollbacks never wipe a foreign commit: `revert_own_batch` reverts
  only when checkpoint revision + 1 equals the batch revision (`doc_revert expect_revision`).
  There is no semantic conflict check by design
  (`services/mcp/test_multiagent_overlap.py::test_overlapping_edits_same_doc_both_land`).
- **Background GUI queries stay off the primary socket.** Document-free metadata queries
  from non-primary threads use per-thread `gui-background` sockets (exact allowlist in
  `services/ipc/ipc_client.cpp`, reached through `services/worker/engine_backend.cpp`).
  Background clients never get the unconditional `gui` pin (`test_worker_long_run`).
- **Drains are bounded.** `Server::beginDrain(deadlineSecs, reason)`: `daemon_drain`/`--stop`,
  tray Quit (5 s) and SIGTERM are hard stops with a deadline (`CHUDWARE_HOSTD_DRAIN_GRACE_S`,
  default 20 s). The stale-binary self-drain is soft, so a rebuild never kicks a live session.
- **A draining daemon refuses new clients by name** (`daemon.draining` event plus a
  `daemon_draining` hello error); `--status`/`--stop` are still admitted. No client falls back
  to an in-process engine: Python raises `CoreError(code="daemon_draining")`; the GUI stays in
  daemon mode, names the drain on its unreachable chip, and reconnects to the new build.
- **Doc GC** (`core/hostd/doc_registry.cpp`): `CHUDWARE_DOC_GC_GRACE_S` (90 s); agent/MCP-origin
  docs floor at `CHUDWARE_DOC_GC_AGENT_GRACE_S` (1800 s); `CHUDWARE_DOC_GC=0` disables. The
  grace never pins the daemon; a clean exit discards, so recovery is crash-only.
- **Worker lanes** (`WorkerBridge::forMethod`, `core/hostd/worker_bridge.cpp`): a new stateful
  agent RPC family must join the agent-lane prefix list or it lands in a process blind to
  Chudcode sessions. Files shared across worker processes need flocks, not threading locks.
- **One turn per agent worker.** `WorkerBridge::dispatch` runs each `agent_admin_turn` in a
  pool worker (cap `CHUDWARE_AGENT_WORKERS`, default 4; idle extras reaped after 10 min). A
  conversation is owned by one worker; moving it requires the owner to release its idle
  runtime first (see [chudcode-integration.md](chudcode-integration.md#sessions-and-lifecycle)).
  Worker forks run on one daemon-lifetime thread: PDEATHSIG follows the forking thread.
- **Nothing waits behind a turn.** `WorkerBridge::stop()` cancels the active run over the
  control lane, waits up to 10 s, SIGTERMs, never respawns. A non-turn call queued behind a
  turn fails at once with `worker_lane_busy`. hostd never replays an `agent_admin_turn` after
  worker loss (`worker_lost_mid_turn`).
- In daemon mode each GUI `agent_admin_turn` leases its own connection (up to 8) and takes no
  client mutex, so turns of different chats overlap; native `agent_run_v2`/`agent_mission_v1`
  still serialize on `WorkerClient::longRunMtx_`.
- **CEF fetch daemon** (`host/src/ui/widgets/cef/cef_daemon.cpp`): the idle watchdog is armed
  before `initialize()`; activity goes through `mark_activity()`. Spawn is single-flight with
  the pid verified (`services/browser/cef_client.py`). A CEF-off build claims and refuses
  `--cef-*` argv with `std::fflush(nullptr); std::_Exit(n)` (`host/src/main.cpp`). A missing
  helper fails as `browser_helper_missing`; an early exit as `browser_helper_exited`.

## Documents, saving and selection

- **hostd is the only autosave authority in daemon mode**; the guard sits at the single write
  in `cad_common._autosave()`. The standalone MCP server keeps snapshots.
- **AutoCAD files are exact B-rep both ways.** DWG/DXF/SAT ACIS solids import natively
  (`acis_read.cpp`, `dwg_native.cpp`); LibreDWG `dwg2dxf` is used only for 2D entities.
  Exports write ACIS 3DSOLID/BODY in the import's `source_units`, and every dxf/dwg/sat export
  is re-read (solids, validity, volume).
- **Build state is saved with the file.** `doc_save` writes the compiled cache plus per-feature
  checkpoints (`contracts/stepplus.md` §9); `doc_open` hydrates them, so a valid cache opens
  warm. Every skip is reported (`cache.skipped_reason`, `cache_miss_reason`). A rebuild after
  a miss is written back through `stepplus_write_build_state` (§17), which replaces only the
  cache members and `geometry.step`; never route that through `stepplus_save`, which rewrites
  metadata and marks the file modified.
- **Agent mission docs are saved, never closed.** `cad_common.save_touched_docs()` runs at every
  turn terminal and on the crash path, writing into `CHUDWARE_PROJECT_SAVE_DIR`. It never
  touches `origin_kind=gui` docs, never writes an empty doc, and skips pristine reproducible
  imports. Agent edits to a user-opened doc stay live (dirty, hostd-autosaved) until an
  explicit save (`test_project_save`).
- **One design is one document; names carry no version.** A version token resumes the series
  (`fresh=True` escapes); `sanitize_part_name` (`core/agent/run_stop.cpp`) mirrors the token
  set and must stay in lockstep with the Python strip.
- **Save paths.** `save_part`/`export_part` without a path go to `CHUDWARE_PROJECT_SAVE_DIR`; a
  doc already backed by a `.step+` saves in place; `save_part` refuses
  `path_open_in_user_tab`. Empty docs refuse save/export unless `allow_empty`. Relative
  destinations resolve against `CHUDWARE_PROJECT_DIR`, then `CHUDWARE_FS_ROOT`, never the worker
  cwd or an ancestor repository; absolute and `~` exports still pass credential-path checks.
- **PLM tools.** `.step+`/`.ecad+` auto-register on save; `plm_item_add` is for non-design files
  (`path` is the project root, the tracked item goes in `file_path`/`folder_path`). Project
  roots default to `CHUDWARE_PROJECT_DIR`, then `CHUDWARE_FS_ROOT`, else `project_root_unavailable`.
- **Agent edit lock.** A doc written by a non-`gui` client locks every human mutation path
  (`AppState::docLockedByAgent`, banner plus Stop) while the writing run lives, or for 8 s. A
  new human mutation path must check this predicate (`CHUDWARE_SHOT=agentlock`).
- **The user's view.** An agent-opened doc never takes focus from the user's tab; agent edits
  reach the active tab live, deferred only while the user is mid-edit (`test_tess_catchup`).
- **One working-directory authority.** `AppState::workspaceDir` drives the top-left chip,
  PLM project path and chat project. Opening an external document never changes it. Chats follow
  the workspace of their latest turn (`CHUDWARE_SHOT=projectchat`).

## CAD-side agent policy

- **Headless turns are `do_it`.** `resolve_turn_preferences(cfg, headless=True)` is the one
  forcing site; the interactive `check_with_me` default never governs unattended callers.
- The user-picked mode (`TurnPreferences.mode`) is the only mode source; auto settings may make a
  turn cheaper but never read-only.
- **Refine cap** (`scaffolds.should_stop_refine`, stop reason `refine_cap`) counts only geometry
  reworked after a settler (`verify_battery`, `save_part`, `export_part`). Never add
  describe/measure/render to the settlers.
- Prompt overrides stay split: core prompts read `appdata/prompts/<id>.md`, mission roles
  `<id>.mission.md`. `system_prompt` replaces role prose but keeps harness enforcement.
- CAD skill packs live in `services/agent/skills/builtin/<name>/SKILL.md` and reach agents only
  through `cad_skill(name)`. Agents never hand-edit STEP or JSONL text.
- **Visual review is numbers first, then one planner grid.** `render_part` without `view`
  returns one 2x2 grid chosen by `services/mcp/shot_planner.py` from native `doc_view_metrics`.
  Append new candidate kinds inside `plan_shots` only (candidates are replayed by index).

## Simulation and assemblies

- **Evidence-grade or not a result.** Sim outputs carry top-level `mesh_quality`, `reactions`
  and `provenance` (`engine_build_id`, `polymesh_commit`, `governor`) beside `summary`; ladder
  rows carry `delivered_size_mm` and named reasons; under-resolved walls are flagged. The
  release path is the `simulate` CAD skill (`run_fea_convergence`, `sim_evidence_packet`).
- **The governor measures, never extrapolates:** element counts come from probes; bounding-box
  forecasts are diagnostics only.
- **Refuse before meshing; never go silent.** Contact preflights run before any fill; every sim
  job writes phase events and a 30 s heartbeat to `partial.json` (silence >60 s is a defect).
- Contact/static studies model joints per body (`material`, `interfaces`: press fits bonded,
  running clearances contact). Buckling refuses by name at `max_load_factor`; steady thermal is
  native (`fea_thermal_solve`; CalculiX is used for verification only).
- **An agent's sim is a study the user can open:** sim tools persist through `_persist_study` and
  hostd fans out `doc.studies_changed`. Piping, hydraulic, vessel, duct and civil-site studies
  persist the same way (`services/civil/`, [`contracts/civil_schema.md`](../../contracts/civil_schema.md)).
- **Assemblies report honestly.** `measure_part` weighs each body at its applied material
  (`material.source` says which), falling back to steel only where none resolves.
  `check_interference` refuses an empty doc, reports `pairs_checked`, and returns a clash as a
  finding (`ok:true`, verdict in `clear`); `ok:false` means refused or incomplete. A no-op
  `doc_solve_mates` is not an edit. Exports warn `placement_unverified` until interference runs.
  Details: [evaluate-tools.md](evaluate-tools.md).
- Mates: hinge-line angle mates are revolutes; a throwing residual is `mate_unresolved`, never
  converged. Native mates persist engine-owned `reference_frames`; edits never redefine zero.
- `compile_document(..., with_display=false)` is the assembly-only lane; its mode is part of the
  compile-cache identity.
- `add_features mode:"new"`/`merge:false` is an independent body; unknown modes refuse by name;
  `transform` moves (`copy:true` duplicates).

## Manufacturing and print

- The `manufacture` MCP family is off by default; the arbiter is the only approval validator;
  the device allowlist denies by default. Nothing advertises physical `execute`.
- `FreshValue<T>` is the sole external freshness shape; the journal is the sole source of
  durable `seq`.
- 3D printing goes through `slice_part` (`services/mcp/print_tools.py`): stock PrusaSlicer
  discovered at runtime (`scripts/provision_slicer.py --install`), vendor presets read in place,
  orientation/supports from `services/dfam/`, then `gcode_check`. `print_ready` requires a
  passing check. PrusaSlicer 2.9.6 CLI caveats (re-verify on upgrade): no `--arrange`; bare
  `--merge` segfaults headless (use `--dont-arrange --merge`); no sequential-clearance check.
- **Slicer input preserves topology.** Newly merged vertices or zero-area triangles refuse as
  `slicer_mesh_precision_loss`; they are never healed silently.
- Multi-part plates (`slicer/plate.py`) space hulls by clearance radius plus brims plus 2 mm and
  allow at most one object above the clearance height; `gcode_check`'s `sequential_clearance`
  re-proves it from the toolpath.
- **Printer media is published, never copied:** `print_media_inventory` (read) and
  `publish_print_media` (mutating) wrap `services/manufacturing/print_media.py`; humans use
  `scripts/print_media.py`. Publishing takes the complete job set, the whole-drive
  `inventory_token` and explicit `media_idle=True`, reads back hashes, and removes superseded
  managed toolpaths only after a verified commit. Jobs must be `print_ready` unless the user
  explicitly overrides, in which case failed checks are recorded in the media manifest.

## Browser tenant cells

The experimental web app (`web/`) reaches one socket-activated, sandboxed cell per workspace
(`web/deploy/chudware-cell@.socket`) through `web/gateway/cells.py`. Web contract methods are
admitted only if [`contracts/cell_methods.json`](../../contracts/cell_methods.json) classifies
them `allow` for the caller's right (owner/manager/editor/viewer); unclassified methods are
refused and `scripts/cell_methods.py` must keep the table complete. Actors run in nested
`chudware-actor-sandbox` lanes (`web/deploy/cell/`) with server-minted handles and typed
`not_found` for foreign, absent or malformed ids. Never add raw forwarding, unsandboxed
fallbacks, ambient credentials or an admin bypass. Contract: the cell addendum in
`contracts/worker_rpc.md`; tests: `web/deploy/tests/`.
