# chudware-hostd — the shared engine host daemon (operator reference)

`chudware-hostd` (sources: `core/hostd/`, transport: `services/ipc/`) is a small
binary that owns one shared `libchudware` engine instance per instance key. In daemon
mode the GUI and MCP sessions for that key attach as IPC clients instead of loading
their own copy of the engine. It exists to kill the orphaned-engine /
autosave-timer-leak bug class and to make agent edits appear in the GUI viewport
live (no `.step+` file handoff).

The daemon itself does NOT link the engine — it dlopens `libchudware` exactly the
way the GUI's in-process path does (`services/worker/cpp_core.cpp`), so
`CHUDWARE_LIBCHUDWARE` / dist resolution behaves identically.

## Lifecycle

- **Spawn on demand.** Clients try to connect and, when permitted, spawn a detached
  daemon if absent. Simultaneous spawns race on a lock; the loser exits 0 and clients
  attach to the winner. The retry layers have different budgets:
  - MCP `DaemonClient.connect()` (`services/mcp/daemon_bridge.py`): **15 s** default,
    50 ms doubling backoff capped at 500 ms; override with
    `CHUDWARE_HOSTD_CONNECT_BUDGET_S`.
  - GUI `engine_backend.cpp`: an outer retry schedule of **100, 250, 500, 1000,
    1000 ms** (**2.85 s of retry waits**), then in-process fallback if connection
    still fails. `CHUDWARE_ENGINE_DAEMON_RETRY_MS` overrides that wait budget
    (`0` disables outer retries). Each underlying `EngineClient::connectOrSpawn()`
    has its own **15 s** spawn/connect budget (`Options::spawnBudgetMs`); 2.85 s is
    not a total GUI startup deadline. A `daemon_draining` refusal (see
    [New connects to a draining daemon](#new-connects-to-a-draining-daemon)) stops the
    outer retry at once and never selects the in-process fallback.
- **Single instance per key.** Linux: `flock` on `hostd-<key>.lock` held for the
  daemon's lifetime (evaporates on death — no stale-lock heuristics). Windows:
  an exclusive handle to a current-user/SYSTEM-only lock file under `%APPDATA%\Chudware`.
  Public named mutexes are not singleton authority. New private objects explicitly use
  the current user's SID as owner, including elevated launches; foreign-owned state is refused.
- **Startup order**: acquire lock → remove stale socket → bind/listen → THEN
  load libchudware asynchronously. While OCCT loads, the handshake answers
  `state: loading`; an engine call can still fail or time out waiting for readiness.
- **Idle exit is work-based.** `Server::pinningClients()` counts every GUI client,
  and non-GUI clients only while they have an attached doc, an in-flight call or
  recent RPC activity. A parked socket alone does not pin the daemon. With no
  pinning client, in-flight RPC or agent child, it exits after
  `CHUDWARE_HOSTD_LINGER_SECS` (default **120 s**). After the last GUI detaches,
  `CHUDWARE_HOSTD_GUI_EXIT_GRACE_S` (default **10 s**) replaces the linger.
- **Bounded explicit drains.** `daemon_drain` / `--stop` use
  `CHUDWARE_HOSTD_DRAIN_GRACE_S` (default **20 s**); tray Quit uses **5 s**.
  At the deadline stragglers are disconnected. Stale-binary self-drain is soft:
  it refuses new connections but lets active sessions finish.
- **Doc GC is separate.** Unattached docs have a **90 s** base grace; agent/MCP
  origins have a **1800 s** floor. That grace keeps finished mission parts openable
  while the GUI exists, but does not pin the daemon through shutdown.
- **GUI background queries.** Document-free metadata/worker-bridged queries from non-primary
  GUI threads use per-thread `gui-background` connections via the exact IPC allowlist;
  document-scoped and unknown methods retain the primary socket. Side-connection EOF/timeouts
  do not invalidate the primary connection. Background sockets do not receive the unconditional
  `gui` pin; recent/in-flight RPCs still count as work under the ordinary non-GUI rules.
- **CLI**:
  - `chudware-hostd --foreground` — run serving in the current terminal (dev).
  - `chudware-hostd --status` — JSON status of the running instance (`state`,
    `pid`, `children`) on **stdout**, preceded by the product version banner on
    **stderr**; exits 1 with "no running instance" if none. The two streams are
    split deliberately so `--status | jq` keeps working.
  - `chudware-hostd --stop` — request bounded drain and exit. **Refused while
    agent children / a mission are active.** Clean exit discards session autosaves;
    it is not a save-to-project command.
  - `chudware-hostd --stop --force` — stop children (TERM → grace → KILL on POSIX)
    first, then drain and exit. Never use this on an instance serving someone
    else's GUI or agent work.
  - `chudware-hostd --version` — prints the git-describe build id.
    **Exactly one line, and the build id must be the last whitespace-separated
    token.** Two independent staleness checks parse this and disagree about the
    shape: `services/ipc/ipc_client.cpp` `diskBuildId()` takes the last token of
    the FIRST line, while `services/mcp/daemon_bridge.py` `_disk_build_id()`
    takes the last token of the WHOLE output. Adding a second line makes one of
    them compare garbage against `hello.git_rev` and declare every daemon stale.

## Windows daemon transport

Native daemon reads and writes use separate overlapped completion events, with a
sticky shutdown event to cover cancellation before an operation is submitted.
Blocking reads wait for completion rather than `PeekNamedPipe`/`Sleep(1)` polling.
Timeout cancellation drains the operation before its buffer retires; partial
frames survive a timeout, zero-timeout reads poll without waiting, and lines over
64 MiB fail closed. Concurrent event writes cannot reset the reader's event.
Python discovery shares the typed, pointer-width-correct process TokenUser lookup
with the private control-pipe implementation.

`ipc_transport_windows` in core CTest exercises real native idle/pre-submit
cancellation, simultaneous blocked reads/writes, duplex traffic, partial frames,
EOF and the 64 MiB boundary. Performance measurements are recorded separately in
the native Windows evidence archive; VM measurements are not GPU/UI benchmarks.

## Windows worker lanes

The daemon starts native workers with `CreateProcessW`, an explicit inherited-handle
list and a kill-on-close Job Object assigned before the child runs. Workers use
`DETACHED_PROCESS`: their private stdio needs no console, whereas `CREATE_NO_WINDOW`
still creates a hidden `conhost.exe` child and prevents an authoritative idle census.
Standard and session-owned agent lanes share routing, control, activity census and idle
reaping; an interrupted agent turn is never replayed. Windows stdout reads wait on overlapped
I/O completion or child exit instead of millisecond polling. Cancellation drains the
pending operation before its buffer/event can be reused, and final output survives EOF.
Negative read deadlines remain unbounded: transport timeouts are not agent lifetime caps.

Control named pipes likewise use event waits and absolute request/response deadlines.
Failed response writes do not add a second disconnect grace period. Windows desktop
workers do not accept Linux tenant-cell execution contexts.

Python worker activity uses a native Toolhelp parent snapshot and exact process
creation FILETIMEs rather than `/proc`. It opens handles only within the worker's
descendant tree, not unrelated protected system processes. Unknown descendants are
counted and prevent an idle verdict; inaccessible or incomplete observations fail
closed. Observed desktop SDK launches still require containment attestation:
this census does not turn an escaped SDK/tool into trusted idle work.

Windows cached saves publish the staged file without deleting the renamed result
during cleanup. Both normal saves and cache-only writeback provide a terminated
UTF-16 rename buffer; native Windows consumed characters past `FileNameLength` without
that terminator. Archive readers use wide filenames and close before replacement.
API paths remain UTF-8, including Unicode save/overwrite/relocation and mixed separators;
unrelated NTFS names are compared as native code units, not narrowed through the ANSI code page.
Cache provenance keys and save stages use explicit TokenUser ownership with the protected
user/SYSTEM ACL, including elevated launches. The native runtime smoke checks verified
checkpoint hydration with zero feature execution and no pending verification rebuild,
then a tail edit reusing earlier checkpoints.

## Environment variables

| Variable | Meaning |
|---|---|
| `CHUDWARE_ENGINE_DAEMON` | Unset/`1` = default daemon routing; `0` = explicit in-process opt-out. Desktop GUI startup can also fall back if daemon connection fails, but never on a `daemon_draining` refusal. |
| `CHUDWARE_HOSTD_BIN` | Explicit daemon binary to spawn. Developer MCP discovery tries `CHUDWARE_DIST`, then Linux full/slim build trees (Windows: `core/build/bin`); packaged C++ clients resolve the manifest-owned binary. Pin this explicitly in portable examples. |
| `CHUDWARE_INSTANCE_KEY` | 12-hex instance key override. Unset → hash of the realpath of the resolved hostd binary (Windows: lowercased path). Same key = same daemon; different keys coexist. |
| `CHUDWARE_HOSTD_SOCKET` | Explicit socket path override (Linux). |
| `CHUDWARE_HOSTD_PIPE` | Explicit pipe name override (Windows; `CHUDWARE_HOSTD_SOCKET` also accepted). |
| `CHUDWARE_HOSTD_LINGER_SECS` | Idle-exit linger, default 120. Tests/smokes use small values (5). |
| `CHUDWARE_HOSTD_STALECHECK_S` | Stale-binary self-drain check interval, default 60. Tests use 1. |
| `CHUDWARE_HOSTD_GUI_EXIT_GRACE_S` | Idle grace after the last GUI detaches, default 10 s. |
| `CHUDWARE_HOSTD_DRAIN_GRACE_S` | Explicit drain deadline, default 20 s, minimum 1 s. |
| `CHUDWARE_HOSTD_CONNECT_BUDGET_S` | MCP bridge connect/spawn budget, default 15 s. |
| `CHUDWARE_ENGINE_DAEMON_RETRY_MS` | GUI outer retry-wait budget, default 2850 ms; separate from the underlying IPC spawn budget. |
| `CHUDWARE_STARTUP_TRACE` | Exact `1` enables desktop stderr startup milestones in milliseconds from main entry, through first presentation, daemon readiness and starter visibility. Unset by default. |
| `CHUDWARE_PING_BENCH` | Exact `1` enables the desktop daemon-connect diagnostic of 200 synchronous pings and logs mean round-trip latency. Disabled during normal startup. |
| `CHUDWARE_DOC_GC_GRACE_S` | Base unattached-document grace, default 90 s. |
| `CHUDWARE_DOC_GC_AGENT_GRACE_S` | Agent/MCP-origin grace floor, default 1800 s. |
| `CHUDWARE_DOC_GC` | `0` disables doc GC, not daemon idle-exit. |
| `CHUDWARE_AUTOSAVE_INTERVAL_S` | Dirty-document snapshot interval, default 60 s; `0` disables periodic snapshots. |
| `CHUDWARE_LIBCHUDWARE` | Engine .so/.dll the daemon loads (same variable the in-process path uses). |

## Paths (per platform)

| Thing | Linux | Windows |
|---|---|---|
| Socket / pipe | `$XDG_RUNTIME_DIR/chudware/hostd-<key>.sock` (fallback `/tmp/chudware-<uid>/…`, 0700, ownership-verified) | `\\.\pipe\chudware-hostd-<SID>-<key>` |
| Lock / single instance | `<runtime dir>/hostd-<key>.lock` (flock) | `%APPDATA%\Chudware\hostd-<fnv1a(key)>.lock` (exclusive file handle) |
| Logs | `~/.local/share/Chudware/logs/hostd.log` (5 MB × 3 rename rotation) | `%APPDATA%\Chudware\logs\hostd.log` |
| Autosave / recovery | `~/.local/share/Chudware/hostd_autosave/<key>/` | `%APPDATA%\Chudware\hostd_autosave\<key>\` |

Nothing daemon-related is ever written next to the binary — important for the
AppImage, whose mount is read-only.

## Instance keys: dev vs installed vs AppImage

The key is what keeps independent installs from cross-talking:

- **Dev tree**: key = hash of the realpath of the built hostd binary
  (`core/build-occt-linux/bin/chudware-hostd`). Rebuilds keep the same path/key.
- **Installed (Windows)**: key = hash of
  `%LOCALAPPDATA%\Programs\Chudware\chudware-hostd.exe` (pinned via
  `CHUDWARE_HOSTD_BIN` in the registered MCP server env — see
  `services/installer/register_mcp.py --hostd-bin`).
- **AppImage**: the squashfs mount point is **randomized per launch**, so
  hashing the resolved binary path would mint a new daemon every run. AppRun
  therefore exports `CHUDWARE_INSTANCE_KEY = sha256($APPIMAGE)[0:12]` — the stable
  path of the AppImage *file* — plus `CHUDWARE_HOSTD_BIN` at the bundled binary.
  Two different AppImage versions have different `$APPIMAGE` paths → different
  keys → they coexist safely.
- **MCP with an AppImage install**: register the MCP server env with the same
  `CHUDWARE_INSTANCE_KEY` (hash of your AppImage path) so agent sessions attach to
  the GUI's daemon. Note the bundled hostd binary only exists while the
  AppImage is mounted, so headless MCP sessions cannot *spawn* it — start the
  GUI (or any AppImage invocation) first, then MCP sessions attach to the
  running daemon. Example server env block:

  ```json
  "env": {
    "CHUDWARE_ENGINE_DAEMON": "1",
    "CHUDWARE_INSTANCE_KEY": "<first 12 hex of sha256 of the AppImage path>"
  }
  ```

## Recovery semantics (id_remap)

Recovery is **crash-only**, not automatic document reopening. A hard-killed
daemon leaves a session autosave directory; on restart its eligible snapshots
are registered in the orphan recovery tier. They wait for explicit
`recovery_list` → `recovery_open`. Only that open mints a fresh doc id and records
the old→new mapping returned by `hello.id_remap`. Undo/redo stacks are not persisted;
recoverable geometry is limited to the last successful snapshot, not a guaranteed
one-interval loss bound if saving was disabled or failed.

Clean shutdown (idle-exit, drain, SIGTERM) removes the current session's autosaves.
Explicit discard purges that doc's artifacts. While the daemon is serving, GC retires
eligible abandoned unsaved docs to the orphan tier before closing them; already-saved
clean docs, empty/internal docs and pristine reproducible imports do not create recovery
rows. These behaviours live in `core/hostd/autosave.*` and `doc_registry.*`.

Durable mission delivery is a different boundary:
`services/mcp/cad_common.py::save_touched_docs()` saves eligible agent-authored docs to
`CHUDWARE_PROJECT_SAVE_DIR` at turn termination
(`services/agent/mission_dispatch.py::run_linear_role_turn`,
`services/agent/worker_rpc.py::_agent_admin_turn`). It **saves, never closes**, skips user-origin documents,
empty/discarded docs and pristine reproducible imports, and reports persistence errors.
Do not substitute daemon shutdown or crash recovery for that save.

For an isolated Linux development run, from the repository root:

```bash
export CHUDWARE_HOSTD_BIN="$PWD/core/build-occt-linux/bin/chudware-hostd"
export CHUDWARE_LIBCHUDWARE="$PWD/core/build-occt-linux/bin/libchudware.so"
export CHUDWARE_INSTANCE_KEY="reference-$$"
"$CHUDWARE_HOSTD_BIN" --foreground
```

Use the same exported key and paths for that run's clients. This starts a separate
instance; it does not attach to, stop or replace an already-open user session.

## Mode matrix (`CHUDWARE_ENGINE_DAEMON` × client)

| | GUI | MCP |
|---|---|---|
| unset/`=1` (default) | Attempts `DaemonConn` → daemon; viewport receives live `doc.changed` events. Startup failure can select in-process fallback, except a `daemon_draining` refusal: the GUI stays in daemon mode and waits for the draining daemon to exit. | `core_bridge` routes through `daemon_bridge.py`; docs are shared with daemon-mode clients. A `daemon_draining` refusal raises `CoreError(code="daemon_draining")`, never an in-process fallback. |
| `=0` (explicit opt-out) | In-process dlopen | In-process ctypes load; no shared live document registry with the GUI. |

Mixing modes on the same docs is guarded: a legacy in-process load warns loudly
when a live daemon socket for the same install exists ("mode divergence" —
two engines would be two sources of truth). Do not mix modes when shared live
documents are required.

## Version staleness (drain-replace)

The daemon and engine embed a build-time git-describe id
(`chudware-hostd --version`, `chudware_build_id()`; `hello` carries `git_rev` and
`engine_build_id`). Two mechanisms detect stale binaries:

- **Client-side drain-replace**: on connect, clients compare `hello.git_rev`
  (the RUNNING daemon) against the build id of the hostd binary ON DISK
  (`--version` output, cached per binary mtime — one subprocess per rebuild per
  client process). Mismatch → the client sends `daemon_drain` (refused while a
  mission is active — the client warns and stays on the old daemon), waits for
  the old pid to exit, then respawns/reconnects the new build.
- **Daemon-side self-drain backstop**: the accept loop stats its own binary and
  the loaded `libchudware` every `CHUDWARE_HOSTD_STALECHECK_S` (default 60 s); an
  mtime change starts a soft drain (`beginDrain(0, "stale_binary", path)`). Existing
  clients finish on the old engine, and the daemon exits when no client pins it.
  Deploys must still be temp-file + RENAME (never `cp` over a live .so).

### New connects to a draining daemon

Every drain refuses new clients by name: `stale_binary` (soft, above),
`stop_requested` (`daemon_drain` / `--stop`) and `quit` (tray Quit) — see
`Server::admitWhileDraining` / `drainRefusal` in `core/hostd/server.cpp`.

- An operator `control` client (`chudware-hostd --status` / `--stop`) is admitted. It
  reports the drain, or `--stop` turns a soft drain into a hard stop.
- Any other client gets a `{"event":"daemon.draining","reason":…}` line, then an error
  reply to its `hello`, then a clean close. The error object:
  `{code:"daemon_draining", reason, daemon_pid, clients, active_clients, children,
  hard_stop, changed_path?, message, hint}` (`changed_path` only for `stale_binary`).
- One daemon serves each hostd binary path, so no successor can start beside a
  draining daemon. No client falls back to an in-process engine on this refusal:
  - Python: `daemon_bridge.DaemonDraining`; `core_bridge` raises
    `CoreError(code="daemon_draining")`.
  - GUI: `EngineClient::helloError()` keeps the error object;
    `engine::daemonRefusal()` exposes it. The GUI stays in daemon mode and names the
    drain on the sticky unreachable chip (tooltip: `message` + `hint`). The frame-loop
    reconnect (`maintainConnection`) does not spawn while the daemon refuses. After the
    old daemon exits, it spawns the new build and connects.
- `daemon_status` (and `--status`) carry `drain_reason` (null while not draining)
  and, while draining, `drain`: the refusal object without `code`.

## Installer integration

- **AppImage** (`packaging/linux/build_appimage.sh`): stages
  `usr/bin/chudware-hostd`, the full-FEA `usr/lib/libchudware.so`, optional lib3mf,
  and the Polyhedral-FEA advisor package beside the supplied engine,
  plus its non-system dependency closure via an `ldd` walk filtered by the
  AppImage excludelist. The build script probes the staged engine's `dlopen`;
  this is build-time validation, not proof that every target distro runs it.
  Excluded system libraries (including glibc/libstdc++, graphics and font libraries)
  remain host-provided; compatibility depends on the build base and target system.
- **Windows** (`services/installer/` + `packaging/windows/chudware.iss`):
  `build_release.py` requires `chudware-hostd.exe` and rejects `.pdb` in the customer
  payload. Inno installs the verified payload and invokes the hash-checked native
  uninstall helper, not `install.ps1`/`uninstall.ps1` provisioning. Its
  `CloseApplications=yes` enables Restart Manager. Separately, the developer
  `install.ps1` drains known install-directory daemons with `--stop --force`,
  then uses image-path-scoped `taskkill`; MCP registration pins `CHUDWARE_HOSTD_BIN`.
  The isolated native Windows developer checks are recorded in the remediation
  evidence ledger. They do not qualify the installed payload, Restart Manager or
  upgrade/uninstall behavior; use the manual checklist in `PACKAGING_WINDOWS.md`.
- **Upgrades**: the developer installer has an explicit daemon stop path; the packaged
  installer uses Restart Manager. The Windows checklist must establish the runtime
  behaviour before claiming safe replacement of a running daemon.
  POSIX deployment keeps copy-to-temp plus atomic rename, never overwriting a live
  mapped engine library.
- **Dev tree (Linux)**: `./build.sh stage` deploys `libchudware.so`, optional lib3mf/
  advisor artifacts, and `chudware-hostd` from `core/build-occt-linux/bin` into
  `services/dist/Chudware/`, the directory that `core_bridge` and
  `daemon_bridge.resolve_hostd_bin` try first. Each file uses copy-to-temp plus
  atomic rename. Always stage the pair together: the daemon instance key is
  the fnv of the hostd binary's realpath, so a dist libchudware.so with only a
  build-dir hostd silently splits clients across two engines. On Windows,
  `build.bat` stages `chudware-hostd.exe` into `dist\Chudware` alongside the core
  DLLs (warn+skip when not built); production `build_release.py` instead requires it.

## Troubleshooting

- **"no running instance" but the app hangs connecting** — stale socket file
  after a hard machine crash: the daemon removes it at startup, but if spawn is
  failing check `~/.local/share/Chudware/logs/hostd.log` and delete
  `$XDG_RUNTIME_DIR/chudware/hostd-<key>.sock` manually as a last resort. The lock
  file needs no cleanup (flock dies with the process).
- **Mode-divergence warning** ("legacy in-process engine while a daemon is
  live") — you launched one client with `CHUDWARE_ENGINE_DAEMON=0` and another
  with `=1`. Pick one mode per install; the legacy client sees none of the
  daemon's docs.
- **Two daemons after moving/renaming an install** — expected: the instance key
  follows the binary/AppImage path. The old daemon idle-exits after its linger.
- **Windows: "job breakaway denied" warning** — the spawning MCP client sits in
  a `KILL_ON_JOB_CLOSE` Job Object without breakaway permission (Claude Code
  does this). The daemon still starts but dies with that job; work-loss is
  bounded by autosave and the next client respawns it. Durable fix: start the
  GUI (never jobbed) first so IT owns the daemon.
- **Daemon exits "too soon" during tests** — you inherited a small
  `CHUDWARE_HOSTD_LINGER_SECS`; unset it (default 120 s).
- **Engine load FAILED in hostd.log** — inspect the resolved engine path and build
  capabilities. For a Linux development checkout, set
  `CHUDWARE_LIBCHUDWARE="$PWD/core/build-occt-linux/bin/libchudware.so"` for the
  OCCT/Polyhedral-FEA engine rather than an OCCT-free test tree.

## The `polymesh` CLI beside the daemon

Every OCCT core build with `CHUDWARE_WITH_POLYMESH=ON` also builds and stages the
Polyhedral-FEA driver `polymesh` (`check|mesh|solve|diag|render`, plus
`--fix-box`/`--load-box`) into the SAME `bin/` directory as `chudware-hostd` and
`libchudware.so` — for example `core/build-occt-linux/bin/polymesh` in a Linux
development checkout. Build/runtime staging places the linked ONNX Runtime and
advisor package beside the engine. The CLI is the reproducible mesh/solve path for an
FEA evidence packet: the same mesher and solver the engine links, driven from a shell.
`./build.sh core` rebuilds it alongside the engine (it is a dependency of the `chudware`
target); slim non-OCCT trees declare no such target and stage nothing. `polymesh-gui`,
`apps/bench`, `apps/testlab` and Polyhedral-FEA's own tests are never built.

By default the dependency is the pinned public fetch configured by
`core/cmake/ProvisionPolymesh.cmake`. To use a local Polyhedral-FEA working tree,
export `CHUDWARE_POLYMESH_ROOT="$HOME/src/Polyhedral-FEA"`.
This is a development override, not evidence that the public release pin works.
`build.sh` forwards it as
`-DPOLYMESH_SOURCE_DIR`, re-configures each consumed core tree so the cached value flips,
and `ProvisionPolymesh.cmake` neither fetches nor patches nor pin-checks that directory —
it records `git -C <dir> rev-parse HEAD` as `POLYMESH_COMMIT` and prints
`Polyhedral-FEA: LOCAL SOURCE <dir> @ <sha>`. Building with the variable unset clears the
cache entry and returns to the pin, which prints
`Polyhedral-FEA: PINNED FETCH <repo> @ <sha>`. Always read that line before trusting a
mesh result: it is the only statement of which mesher source the engine actually contains.

## Related

- Implementation: `core/hostd/{main,server,doc_registry,autosave,worker_bridge}.*`.
- Smoke: `scripts/hostd_smoke.py` (gate stage `hostd smoke`), gate stage
  `appimage smoke` (`CHUDWARE_GATE_APPIMAGE=1`). Its leftover-daemon check and cleanup
  use `scripts/hostd_scope.py`, which matches only this user's `chudware-hostd` whose
  environment carries exactly the run's `CHUDWARE_INSTANCE_KEY` and which started after
  the stage's mark, and signals it through a re-validated pidfd — never a name/path
  `pgrep`/`pkill`, so another run's or the user's daemon is never touched.
- Runtime regression suites: `services/mcp/test_daemon_bridge.py`,
  `services/mcp/test_hostd_registry.py`, `services/mcp/test_hostd_supervision.py`,
  `services/mcp/test_hostd_worker_bridge.py`. They use private scratch instance keys;
  running them must not stop a user-owned daemon. Registry coverage includes
  `test_parked_client_does_not_pin_the_daemon`,
  `test_attached_client_still_pins_the_daemon`,
  `test_drain_exits_on_its_deadline_despite_a_stuck_client`,
  `test_clean_shutdown_discards_docs` and `test_crash_recovery_prompts_via_orphan_tier`.
  Worker coverage includes fail-fast calls behind an active turn, no turn replay after
  worker loss and bounded SIGTERM teardown. These are test references, not a current
  pass report.
- Durable save scope tests: `services/mcp/test_terminal_save_sweep.py`;
  standalone server shutdown hooks: `services/mcp/test_server_shutdown_save.py`.
  These use fake save/engine boundaries; a real stdio mission followed by disconnect
  still needs an isolated runtime smoke for end-to-end durable-file proof.
- Native watchdog policy: CTest **`engine_watchdog`**
  (`core/hostd/test_engine_watchdog.cpp`), not a `test_hostd` CTest target.
- Windows validation: `PACKAGING_WINDOWS.md`.
