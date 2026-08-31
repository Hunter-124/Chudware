# chudware-hostd — the shared engine host daemon (operator reference)

`chudware-hostd` (sources: `core/hostd/`, transport: `services/ipc/`) is a small
binary that owns the ONE shared `libchudware` engine instance on a machine. The GUI
and every MCP server session attach to it as IPC clients instead of each loading
their own copy of the engine. It exists to kill the orphaned-engine /
autosave-timer-leak bug class and to make agent edits appear in the GUI viewport
live (no `.step+` file handoff).

The daemon itself does NOT link the engine — it dlopens `libchudware` exactly the
way the GUI's in-process path does (`services/worker/cpp_core.cpp`), so
`CHUDWARE_LIBCHUDWARE` / dist resolution behaves identically.

## Lifecycle

- **Spawn on demand.** No service/systemd/autostart. The first client (GUI or an
  MCP session — daemon mode is the default) tries to connect; on refuse/absent
  it spawns the daemon detached and retries (50 ms doubling backoff, ~15 s
  budget). Simultaneous spawns race on a lock; the loser exits 0 and both
  clients land on the winner.
- **Single instance per key.** Linux: `flock` on `hostd-<key>.lock` held for the
  daemon's lifetime (evaporates on death — no stale-lock heuristics). Windows:
  named mutex `Global\chudware-hostd-<SID>-<key>`.
- **Startup order**: acquire lock → remove stale socket → bind/listen → THEN
  load libchudware asynchronously. While OCCT loads, the handshake answers
  `state: loading`, so racing clients never time out.
- **Idle exit.** The daemon exits after `CHUDWARE_HOSTD_LINGER_SECS` (default
  **120**) once ALL of: 0 clients, 0 agent children, 0 in-flight RPCs, no active
  mission. Dirty docs do not pin it forever — it writes autosave recovery
  snapshots, logs, and exits.
- **CLI**:
  - `chudware-hostd --foreground` — run serving in the current terminal (dev).
  - `chudware-hostd --status` — JSON status of the running instance (`state`,
    `pid`, `children`) on **stdout**, preceded by the product version banner on
    **stderr**; exits 1 with "no running instance" if none. The two streams are
    split deliberately so `--status | jq` keeps working.
  - `chudware-hostd --stop` — drain (autosave dirty docs) and exit. **Refused
    while agent children / a mission are active.**
  - `chudware-hostd --stop --force` — stop children (TERM → grace → KILL) first,
    then drain and exit. This is what installers/uninstallers call.
  - `chudware-hostd --version` — prints the git-describe build id.
    **Exactly one line, and the build id must be the last whitespace-separated
    token.** Two independent staleness checks parse this and disagree about the
    shape: `services/ipc/ipc_client.cpp` `diskBuildId()` takes the last token of
    the FIRST line, while `services/mcp/daemon_bridge.py` `_disk_build_id()`
    takes the last token of the WHOLE output. Adding a second line makes one of
    them compare garbage against `hello.git_rev` and declare every daemon stale.

## Environment variables

| Variable | Meaning |
|---|---|
| `CHUDWARE_ENGINE_DAEMON` | Unset/`1` = **default**: clients route engine calls through the daemon. `0` = explicit opt-out to the legacy in-process load (kept at least one release; removal is a named N+1 task). |
| `CHUDWARE_HOSTD_BIN` | Path to the daemon binary a client should spawn. Unset → resolution order: dist → `core/build-occt-linux/bin` → `core/build-linux/bin`. Installed/AppImage deployments pin this. |
| `CHUDWARE_INSTANCE_KEY` | 12-hex instance key override. Unset → hash of the realpath of the resolved hostd binary (Windows: lowercased path). Same key = same daemon; different keys coexist. |
| `CHUDWARE_HOSTD_SOCKET` | Explicit socket path override (Linux). |
| `CHUDWARE_HOSTD_PIPE` | Explicit pipe name override (Windows; `CHUDWARE_HOSTD_SOCKET` also accepted). |
| `CHUDWARE_HOSTD_LINGER_SECS` | Idle-exit linger, default 120. Tests/smokes use small values (5). |
| `CHUDWARE_HOSTD_STALECHECK_S` | Stale-binary self-drain check interval, default 60. Tests use 1. |
| `CHUDWARE_LIBCHUDWARE` | Engine .so/.dll the daemon loads (same variable the in-process path uses). |

## Paths (per platform)

| Thing | Linux | Windows |
|---|---|---|
| Socket / pipe | `$XDG_RUNTIME_DIR/chudware/hostd-<key>.sock` (fallback `/tmp/chudware-<uid>/…`, 0700, ownership-verified) | `\\.\pipe\chudware-hostd-<SID>-<key>` |
| Lock / single instance | `<runtime dir>/hostd-<key>.lock` (flock) | named mutex `Global\chudware-hostd-<SID>-<key>` |
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

On start the daemon recovers the most recent prior session's autosaves. Because
recovered docs get fresh ids, the `hello` handshake returns an `id_remap`
(old→new); reconnecting clients rebind their references (MCP `_CUR`, GUI tabs)
transparently. Loss bound after a hard kill (SIGKILL/crash) = one autosave
interval; undo/redo stacks are not persisted. Clean shutdown (idle-exit,
`--stop`, SIGTERM) always saves dirty docs first.

## Mode matrix (`CHUDWARE_ENGINE_DAEMON` × client)

| | GUI | MCP |
|---|---|---|
| unset/`=1` (**default**) | Engine calls go through `DaemonConn` → daemon; viewport gets live `doc.changed` events | `core_bridge` routes via `daemon_bridge.py`; docs shared with GUI + other sessions; NO `.step+` handoff files are written |
| `=0` (explicit legacy opt-out) | In-process dlopen (legacy) | In-process ctypes load (legacy); doc sharing with the GUI is OFF; `.step+` handoff files remain the preview path |

Mixing modes on the same docs is guarded: a legacy in-process load warns loudly
when a live daemon socket for the same install exists ("mode divergence" —
two engines would be two sources of truth). The N+1 release upgrades the
warning to a refusal, alongside deleting the legacy path.

## Version staleness (drain-replace)

The daemon and the engine embed the same configure-time git-describe build id
(`chudware-hostd --version`, `chudware_build_id()`; the `hello` handshake carries both
as `git_rev` and `engine_build_id`). Two mechanisms keep a running daemon from
outliving its binaries:

- **Client-side drain-replace**: on connect, clients compare `hello.git_rev`
  (the RUNNING daemon) against the build id of the hostd binary ON DISK
  (`--version` output, cached per binary mtime — one subprocess per rebuild per
  client process). Mismatch → the client sends `daemon_drain` (refused while a
  mission is active — the client warns and stays on the old daemon), waits for
  the old pid to exit, then respawns/reconnects the new build.
- **Daemon-side self-drain backstop**: the accept loop stats its own binary and
  the loaded `libchudware` every `CHUDWARE_HOSTD_STALECHECK_S` (default 60 s); an
  mtime change flips it to draining — existing clients finish, new connects get
  a `daemon.draining` event and are refused, and the daemon exits once idle.
  Deploys must still be temp-file + RENAME (never `cp` over a live .so).

## Installer integration

- **AppImage** (`packaging/linux/build_appimage.sh`): stages
  `usr/bin/chudware-hostd`, the full-FEA `usr/lib/libchudware.so`, optional lib3mf,
  and the Polyhedral-FEA advisor package from `core/build-occt-linux`.
  The full OCCT 7.9 closure (34 `libTK*.so.7.9` + TBB + the engine's non-system
  dep tail) is bundled into `usr/lib` via an ldd walk filtered by the AppImage
  excludelist, so the target machine needs **no** OCCT install (verified: the
  engine `ctypes.CDLL`-loads in a clean `fedora:rawhide` container with no OCCT).
  Caveat: glibc/libstdc++ stay host-provided (excludelist), so an AppImage built
  on glibc 2.43 requires host glibc ≥ 2.43 — build on an older base for wider
  distro coverage. freetype/fontconfig/libX11 are likewise host-provided (they
  interoperate with the host's fonts/X server; present on any GUI machine).
- **Windows** (`services/installer/` + `packaging/windows/chudware.iss`):
  `build_release.py` stages `chudware-hostd.exe` (+ `.pdb`) into the payload;
  `install.ps1`/`uninstall.ps1` first run `<InstallDir>\chudware-hostd.exe --stop`
  and then a taskkill scoped to processes whose image path is under the install
  dir (a dev-tree daemon is never touched); MCP registration pins
  `CHUDWARE_HOSTD_BIN` at the installed exe. Inno has `CloseApplications=yes` as a
  Restart-Manager backstop. Behavioral verification lives on the
  `docs/reference/PACKAGING_WINDOWS.md` manual checklist.
- **Upgrades**: installers stop the daemon before replacing binaries; deploys
  must always be temp-file + RENAME, never `cp` over a live .so (SIGBUS).
- **Dev tree (Linux)**: `./build.sh stage` deploys `libchudware.so`, optional lib3mf/
  advisor artifacts, and `chudware-hostd` from `core/build-occt-linux/bin` into
  `services/dist/Chudware/`, the directory that `core_bridge` and
  `daemon_bridge.resolve_hostd_bin` try first. Each file uses copy-to-temp plus
  atomic rename. Always stage the pair together: the daemon instance key is
  the fnv of the hostd binary's realpath, so a dist libchudware.so with only a
  build-dir hostd silently splits clients across two engines. On Windows,
  `build.bat` stages `chudware-hostd.exe` into `dist\Chudware` alongside the core
  DLLs (warn+skip when not built, like `build_release.py`).

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
- **Engine load FAILED in hostd.log** — the daemon resolves the engine like the
  GUI does; set `CHUDWARE_LIBCHUDWARE` to a full `core/build-occt-linux` build (the
  slim build has no FEA — see memory of the MCP "FEA solver unavailable" trap).

## Related

- Plan: `~/.claude/plans/plan-out-massive-changes-proud-snowflake.md` (Design A/B)
- Smoke: `scripts/hostd_smoke.py` (gate stage `hostd smoke`), gate stage
  `appimage smoke` (`CHUDWARE_GATE_APPIMAGE=1`)
- Tests: `services/mcp/test_daemon_bridge.py`, `test_hostd_registry.py`,
  `test_hostd_supervision.py`, ctest `test_hostd`
- Windows validation: `docs/reference/PACKAGING_WINDOWS.md`
