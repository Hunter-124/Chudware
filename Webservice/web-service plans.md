# Chudware browser-native web UI — plan

> **Superseded 2026-08-10:** this plan has landed. The streamed-container portal
> it proposed replacing was removed; the gateway now serves only the WASM `/app`
> client and the `/engine` bridge. Rows describing the "current portal" below
> describe the *pre-cutover* system and are kept as the record of what was
> migrated. Live architecture: `Webservice/DESIGN.md`.

## Context

Replace the streamed-container portal (`Webservice/`) with a browser-native client: the **existing ImGui GUI compiled to WebAssembly**, running in each teammate's browser, talking over WSS to **one dedicated shared engine daemon (chudware-hostd)** on Hunter's machine. Hunter (local ImGui), MCP agents, and web users + their copilots all attach to that one daemon and one workspace (`/home/hunter/Documents/ME489/Crimson_Crankers_Program/01_Design_Package/CAD_Build/`), seeing each other's edits live. The existing portal/tunnel/whitelist stays up until cutover. Undo/redo gets the human-only + disk-journal + save-policy rework.

Everything below was verified against source this session (2026-07-16). File anchors are re-read before editing — another agent is actively churning `host/src/ui/` and `services/agent/`.

## System map — what actually exists (and where prior understanding was wrong)

| Claim | Reality (verified) |
|---|---|
| "MCP layer was built for me + AI; multi-user probably breaks" | **Mostly wrong.** The daemon already has full multi-client machinery: `core/hostd/server.{h,cpp}` (per-client threads, `hello` handshake with `kind: gui\|mcp\|agent\|test\|control`), `core/hostd/doc_registry.{h,cpp}` (per-doc rw/ro attach table, edit-holds, `base_revision` CAS injection when a doc has >1 rw writer, event fanout `doc.changed/doc.opened/doc.closed/doc.lease_change`, `_foreign_edits` piggyback, origin-kind tracking, doc-GC). The GUI already live-mirrors foreign edits: `host/src/core/engine_events.cpp` handles `doc.opened`/`doc.changed` and refreshes tabs. What's genuinely missing for multi-USER: user identity (hello carries only kind+pid — `services/ipc/ipc_client.h:29-32`), author attribution on events, and undo policy. |
| "Copilot lives inside the core/daemon — verify" | **True.** `agent_run_v2`/`agent_ask_v2`/`pm_assist` are C++-only, dispatched in `core/api/libchudware.cpp:849+` (the Python worker deleted them — `services/worker/worker_client.cpp:691`). Provider config is read by the daemon process from `%APPDATA%/Chudware/config.json` (`core/agent/paths.cpp:70`, `core/agent/config.cpp:36`). Web users share Hunter's provider profile **automatically** in daemon mode; keys never leave the server. |
| Webservice DESIGN.md §8: "WASM: rejected (OCCT/CEF footprint; IP posture)" | **Premise was wrong.** The GUI links **neither OCCT nor libchudware** — "host talks to the worker over IPC and does NOT link libchudware" (`host/CMakeLists.txt:53-56`). Deps: GLFW 3.4, glad GL 3.3 core, ImGui docking 1.91.6, nlohmann_json, stb, miniz; CEF is optional (`CHUDWARE_ENABLE_CEF=OFF` → NullWebView). All shaders are `#version 330 core` (host/src/renderer.cpp, ui/viewport/renderer.cpp, render/id_pass.cpp, overlays.cpp) — clean GLSL ES 3.00 translation for WebGL2. The IP-posture caveat (shipping GUI wasm bytecode to browsers) remains a *decision*, not a blocker — the engine stays server-side. |
| IPC model | Newline-delimited JSON-RPC over AF_UNIX socket `$XDG_RUNTIME_DIR/chudware/hostd-<key>.sock`; id-less `{"event":…}` push lines (`services/ipc/ipc_transport.h`). `CHUDWARE_HOSTD_SOCKET` overrides the path; `CHUDWARE_INSTANCE_KEY` namespaces instances (`services/ipc/ipc_discovery.h:6-14`) — a dedicated web daemon is pure configuration. Backend seam: `services/worker/engine_backend.h` (DaemonConn default since Phase-8 flip; `CHUDWARE_ENGINE_DAEMON=0` = legacy in-proc). |
| Undo/redo today | Per-doc, inside the daemon's libchudware session: `core/doc/session.cpp` — `undo_stack` of **full doc-JSON snapshots**, `kUndoLimit=100` + byte budget, RAM only (`:169-223, 3186-3200`). No author tags; `push_undo` clears redo for everyone; `doc_save` sets `dirty=false` but does **not** clear undo (`:9226`). `doc_checkpoint`/`doc_revert` exist (PM-cancel snapshots). |
| Autosave / rollback for agents | Already exists daemon-side: `core/hostd/autosave.h` — 60 s dirty-doc saver into `<stateDir>/hostd_autosave/<instance-key>/…`, crash-only orphan-tier recovery, `recovery_list/recovery_open`. Plus PLM checkpoints (`plm_checkpoint`/`plm_rollback`, dulwich vaults in `services/plm/remote.py`). Agents' "version-controlled rollback states on disk" = autosave + PLM, already there. |
| Python still in the loop | The GUI spawns `engine.worker` + `engine.sim.worker` Python children (`services/worker/worker_client.h:26-29`). Most methods are migrated to core (drawings, sim FEA/LBM under `CHUDWARE_WITH_NETGEN`, ECAD, agent — all in `core/api/libchudware.cpp` dispatch); router default still sends non-`doc_*` misses to Python (`services/worker/worker_router.cpp`). A WASM client cannot spawn Python → those residual flows must route to the daemon or be disabled in the web build (inventory step P3.1). |
| Portal as of 2026-07-16 (pre-cutover; **removed 2026-08-10**) | `Webservice/gateway/` FastAPI on 127.0.0.1:8300 behind Cloudflare Tunnel + Access (Google allowlist + ua.edu OTP), JWT verified per request (`auth.py`), users in `gateway/users.yaml` with per-user `project`/`doc`, podman session containers streaming ImGui via Selkies, `/transfer` upload/download via `podman cp`, chat WS, admin dashboard + monitor units. **Auth, tunnel, user directory, chat, admin ops were all reused as-is.** |

## Architecture

```mermaid
flowchart LR
  subgraph browser [Teammate browser]
    W[Chudware WASM client\nImGui + WebGL2 viewport]
  end
  subgraph hunter [Hunter's machine]
    CF[Cloudflare Tunnel + Access] --> GW[Gateway :8300\nauth + /app + /engine WSS bridge\n+ /files/aicm + /workspace]
    GW -- unix socket, hello identity injected --> D[chudware-webd\npinned chudware-hostd + libchudware\nDocRegistry / autosave / in-core copilot]
    L[Hunter local ImGui\nCHUDWARE_WEBD_SOCKET=webd] --> D
    M[MCP servers / agents] --> D
    D --- FS[(CAD_Build workspace\n+ hostd state dirs)]
    GW --- FS
    S[legacy streamed session containers\n(kept until cutover)] -. socket bind-mount (P1) .-> D
  end
  W -- WSS /engine (JSON lines) --> GW
  W -- HTTPS /files/aicm, /workspace --> GW
```

Why this shape:
- **Menus mirror by construction** — it IS the ImGui app, so the "map the menus from code" requirement is satisfied without a parallel web UI; UI dev velocity on the desktop app carries straight to web.
- **Responsiveness**: rendering + ImGui run at native speed client-side; only engine RPCs and mesh fetches cross the network (vs streaming every frame today). Interactive drags (`sketch_drag`, `doc_push_pull` previews) are one small JSON round trip per sample — fine at tunnel RTTs, and infinitely better than video-encode latency for everything else.
- **One daemon = the shared session.** DocRegistry already turns N clients on one doc into a coherent live session.
- **Engine IP stays server-side**; only GUI code ships as wasm, behind the existing Access whitelist.

## Phase 0 — dedicated shared daemon + gateway plumbing (no core changes; all steps independent/parallel)

0.1 **Pinned web daemon install.** Script `Webservice/deploy/install_webd.sh`: copy `chudware-hostd` + `libchudware.so` (+ runtime data it dlopens — mirror what `services/installer/` stages) from a green `./build.sh all` into `~/chudware-web/bin/`. New systemd user unit `Webservice/deploy/chudware-hostd.service`: `ExecStart=%h/chudware-web/bin/chudware-hostd --foreground`, `Environment=CHUDWARE_INSTANCE_KEY=chudwareweb`, `Restart=on-failure`, long linger via `CHUDWARE_HOSTD_LINGER_SECS=86400`. Socket lands at `$XDG_RUNTIME_DIR/chudware/hostd-<fnv(chudwareweb…)>.sock` — record the resolved path into `~/chudware-web/webd.env` for the gateway. Upgrades are a manual re-run of the script (deliberate promotion, never auto-tracking dev builds).
0.2 **Hunter-attach recipe.** Documented launcher `~/chudware-web/chudware-shared.sh`: run the desktop `Chudware` with `CHUDWARE_INSTANCE_KEY=chudwareweb` (and `CHUDWARE_HOSTD_BIN=~/chudware-web/bin/chudware-hostd` so a client-side spawn resurrects the right daemon). Verify: `chudware-hostd --status` under that env shows the GUI as a client.
0.3 **Gateway WSS↔socket bridge.** New `Webservice/gateway/engine_bridge.py`, router mounted in `app.py`: `WS /engine` (auth via existing `AuthMiddleware`). On connect: `asyncio.open_unix_connection(webd_socket)`; pump lines both ways (WS text message = one JSON line each way; backpressure via bounded queues; close both on either EOF). **Identity injection**: intercept the client's first line (`hello`), overwrite/insert `params.user = {"id": user.email, "display": user.display}` and force `params.kind = "gui"` — identity is server-asserted, never client-claimed. Config: socket path + workspace root in `gateway/config.py` (`CHUDWARE_WEBD_SOCKET`, `CHUDWARE_WORKSPACE_ROOT` env, defaults from `webd.env`).
0.4 **Mesh/asset fetch.** `GET /files/aicm?path=…` in the gateway: serve the file iff `realpath(path)` is under an allowlist = {web-daemon state dir (`~/.local/share/Chudware`), workspace root, `/tmp/chudware-…` tess cache — enumerate the actual dirs `tess.aicm_path` values land in by inspecting a live response}; else 403. `Cache-Control: no-store`.
0.5 **Workspace file API** (rooted at `CHUDWARE_WORKSPACE_ROOT`, path-traversal-clamped): `GET /workspace/list?dir=`, `GET /workspace/download?path=`, `POST /workspace/upload` (multipart), `POST /workspace/mkdir|rename|delete`. Reuse the old `/transfer` handlers' shape but direct-FS instead of `podman cp`. *(Landed; `/transfer` was deleted with the streamed-session product on 2026-08-10.)*
0.6 **Bridge smoke client.** `Webservice/gateway/tests/test_engine_bridge.py`: websocket client (dev-bypass auth `CHUDWARE_WEB_DEV=1`) does `hello` → `ping` → `doc_new` → `doc_apply_ops` (one box) → asserts ops-response revision 1 and that a second WS connection subscribed via `subscribe {doc_id:"*"}` received `doc.opened`/`doc.changed`.

**Acceptance P0:** unit runs; two concurrent bridge test clients + Hunter's local GUI all attach to chudware-webd; edits from the test client appear as live tab updates in the local GUI (existing `engine_events` mirror).

## Phase 1 — quick win: shared live session through the EXISTING streamed portal (config-only; ships to the team immediately)

The session containers currently run Chudware with its own in-container engine. Point them at the shared daemon instead:
1.1 In `Webservice/gateway/sessions.py` container launch: bind-mount the webd socket (`-v <sock>:/run/chudware/hostd.sock`) and set session env `CHUDWARE_HOSTD_SOCKET=/run/chudware/hostd.sock`, `CHUDWARE_ENGINE_DAEMON=1`. The GUI's `EngineClient` connects instead of spawning (spawn fails harmlessly; set `CHUDWARE_HOSTD_BIN=` empty so it never tries).
1.2 Bind-mount the workspace read-write into sessions at a fixed path (`-v $CHUDWARE_WORKSPACE_ROOT:/workspace`) and set the session's file-explorer workspace root to it (env the app already honors for its workspace root — verify in `host/src/ui/file_explorer.cpp` / `app_state` bootstrap; if none exists, pass `--workspace /workspace` or set HOME-relative default).
1.3 Compatibility check: container glibc/paths already match host builds (image stages `host/build-linux/bin`, DESIGN.md §4) — the socket protocol is version-checked by hello (`build_id`), so refresh the image when webd is promoted.

**Acceptance P1:** two teammates in streamed sessions + Hunter local, all on one daemon: teammate A edits a part, teammate B and Hunter see the tab update within an autosave-free second; `--status` shows 3 gui clients. (Known rough edge accepted at this phase: no per-user attribution, shared undo — fixed in P2.)

## Phase 2 — multi-user correctness in the daemon (small, itemized core changes)

These are the ONLY core/daemon changes; MCP needs none (each MCP process is just another client; its process-local "current doc" ambiguity doesn't affect other clients because every RPC carries doc_id at the daemon).

2.1 **User identity end-to-end.**
  - `services/ipc/ipc_client.h` `Options` gains `std::string userId, userDisplay;` sent in `hello` params (append-only). `core/hostd/server.cpp` hello handler stores them on `ClientConn` (add fields beside `kind`).
  - `core/hostd/doc_registry.{h,cpp}`: `DocState.lastWriterKind` gains sibling `lastWriterUser`; `doc.changed` fanout payload gains `"user": {"id","display"}`; `preCall/postCall` signatures gain the user (or pass `ClientConn&`).
  - `contracts/worker_rpc.md` + `contracts/doc_ops.md`: append hello `user` param + event field (contracts are append-only; done by the session driver, not a subagent).
  - GUI surfacing (coordinate with the UI agent; keep minimal): status-bar/toast shows "edited by <display>" from the event — `host/src/core/engine_events.cpp` `doc.changed` handler.
2.2 **Interactive undo is human-only.** `DocRegistry::preCall`: `doc_undo`/`doc_redo` from `kind ∈ {mcp, agent}` → structured error `{"code":"undo_denied","message":"agents use doc_checkpoint/doc_revert or PLM rollback"}`. Env kill-switch `CHUDWARE_UNDO_AGENTS=1` restores old behavior. Agents keep `doc_checkpoint`/`doc_revert` + autosave/PLM (already sufficient rollback-on-disk). MCP tools `undo/redo` will now error for agents — update tool docstrings in `services/mcp/cad_tools.py` to say so.
2.3 **Save clears undo for humans, not for AI.** New engine method `doc_clear_history {doc_id}` in `core/doc/session.{h,cpp}` (clear `undo_stack`+`redo_stack`+disk journal; no revision bump) + dispatch in `core/api/libchudware.cpp` (append-only seam). `core/hostd/server.cpp` dispatch: after a successful `doc_save` from a client whose kind is `gui` (covers desktop + web; bridge forces web to gui), call `doc_clear_history` on that doc and fan out `doc.changed`-style event `doc.history_cleared` so other GUIs disable their undo buttons. Saves from `mcp`/`agent` skip it. (Shared stack ⇒ a human save clears undo for all humans — per requirement "clears undo for the user(s)".)
2.4 **Undo journal on disk.** `core/doc/session.cpp`: replace silent eviction in `trim_history` with spill — entries pushed out of the RAM byte budget are written to `<stateDir>/hostd_undo/<instance-key>/<doc_id>/<seq>.json` and replaced in the vector by a `{"@file": path}` stub; `doc_undo`/`doc_redo` load stubs on demand. Journal dir removed on `doc_close`, `doc_clear_history`, and daemon clean shutdown (wire into `Autosave::stop` cleanup or Session dtor). This makes hours-long no-save sessions safe without unbounded RSS, and keeps undo latency smooth (spill is only on the cold end of the stack). Depth stays `kUndoLimit=100` RAM+disk combined ×; if Hunter wants deeper, it's one constant.
2.5 **Multi-writer robustness pass.** Existing CAS covers racing `doc_apply_ops`. Add a regression test in `services/mcp/test_hostd_registry.py` style (`core` ctest or the existing pytest file — follow `test_multiagent_overlap.py` pattern): two clients interleave `doc_apply_ops`/`doc_undo` on one doc; assert no lost updates, correct `stale_revision` retries, events received by both.

**Acceptance P2:** with Hunter + 2 web/streamed users on one part: edits show author display; agent `doc_undo` is refused; user save clears undo everywhere (buttons grey out), a subsequent MCP-agent `doc_save` does not; undo depth survives RAM budget via disk files (observe `hostd_undo/` populating during a large-import edit session); concurrency test green.

## Phase 3 — WASM client (the headline; independent of P2, parallel)

3.1 **Inventory gate (first, cheap).** Enumerate what the GUI still sends to the Python workers at runtime: grep the actual routing in `services/worker/worker_client.cpp` (its internal method tables, not just `worker_router.cpp`) + run desktop app with worker spawn disabled and log misses. Output: list of {method → in-core available? / disable in web v1}. Sim (`engine.sim.worker`, `host/src/core/sim_client.cpp`) is expected to need either in-core `sim_run_*` routing (exists under `CHUDWARE_WITH_NETGEN`) or a disabled Sim workspace in web v1 — decide per method from the inventory, default disable.
3.2 **Toolchain + build target.** `build.sh wasm`: emsdk (pin version in the script; install under `~/.emsdk` if absent), `cmake -S host -B host/build-wasm -DCMAKE_TOOLCHAIN_FILE=$EMSDK/.../Emscripten.cmake -DCHUDWARE_ENABLE_CEF=OFF -DCHUDWARE_WEB=ON`. New cmake branch in `host/CMakeLists.txt` (guard: `if(EMSCRIPTEN)`): use `contrib.glfw3` port (`--use-port=contrib.glfw3`, real GLFW 3.4 API), drop glad (Emscripten GLES3 headers; define `CHUDWARE_GLES3` — see 3.4), link flags `-sWASM=1 -pthread -sPROXY_TO_PTHREAD -sALLOW_MEMORY_GROWTH -sMAX_WEBGL_VERSION=2 -sMIN_WEBGL_VERSION=2 -sWEBSOCKET_SUBPROTOCOL=... -sFETCH`, `--preload-file` for fonts/icon atlases (mirror the asset list `host/src/bootstrap.cpp` loads). Output `Chudware.html/js/wasm/data` → staged to `Webservice/gateway/static/app/`.
3.3 **WebSocket IPC transport.** New `services/ipc/ipc_transport_wasm.cpp` implementing the existing `IpcConn`/`connectTo` contract (`readLine(timeoutMs)` blocking, `writeLine`) over `emscripten_websocket_*`: callbacks (main thread) append to a mutex+condvar line buffer; `readLine` waits on the condvar (we run app logic in a pthread via PROXY_TO_PTHREAD, so blocking is legal). `connectTo(path,…)` ignores the unix path and dials `wss://<location.host>/engine` (path from a JS global set by the loader page). `IpcListener` compiled out. `spawnDetached` → stub returning false. Selection: cmake adds this file instead of `ipc_transport.cpp` under EMSCRIPTEN.
3.4 **GL/GLSL shim.** Small helper `ui/viewport/glsl_compat.h`: `const char* shaderSrc(const char* body)` — under `CHUDWARE_GLES3` rewrites the leading `#version 330 core` to `#version 300 es\nprecision highp float;\nprecision highp int;`. Apply at every `glShaderSource` site: `host/src/renderer.cpp`, `ui/viewport/renderer.cpp`, `host/src/render/id_pass.cpp`, `render/overlays.cpp`, `render/splat_renderer.cpp`, `render/re_points.cpp`, `host/src/ui/icons_sdf.cpp` (if it compiles shaders — check). Audit for ES-illegal constructs while touching each (expected clean: no geometry shaders/compute; `flat uint`, `gl_VertexID`, `fwidth`, `texelFetch` are all ES 3.00). glad calls compile against GLES3 headers; fix the handful of desktop-only enums (e.g. `GL_TEXTURE_MAX_ANISOTROPY_EXT` guards, `glPolygonMode` — replace wireframe uses with existing edge-overlay rendering or #ifdef them out).
3.5 **AICM fetch.** `host/src/render/aicm_loader.cpp` + `core/util/aicm_handoff.cpp`: under EMSCRIPTEN, `loadAicmFile(path)`: if path is an `aicm-shm://` or absolute server path, `emscripten_fetch` `"/files/aicm?path=" + urlencode(path)` (synchronous fetch is allowed off-main-thread) and parse from the memory buffer (loader already parses a byte buffer internally — expose `loadAicmBytes`); shm branch compiled out.
3.6 **Stub the native-only subsystems** (all behind `#ifdef __EMSCRIPTEN__` or the existing seams):
  - Python worker: `services/worker/worker_client.cpp` `start()` returns false with a quiet flag; methods routed to Python return `{"code":"unavailable_web"}`; panels from the 3.1 disable-list hide via a `webBuild()` predicate in `host/src/core/app_state.h`.
  - CEF/web panels: already Null when CEF off. Camera: `camera_stub.cpp` (exists). `win_chrome.cpp`: no-op path exists for non-Windows.
  - Crash/minidump, profiler file writes, `xdg-open` style shells: no-op.
3.7 **Files UX for web.**
  - `host/src/ui/file_explorer.cpp` + `file_dialog.cpp`: introduce `FsBackend` (list/stat/mkdir/rename/delete/read/write) with the current `std::filesystem` impl for desktop and a `RemoteFs` (emscripten_fetch → `/workspace/*` endpoints) for web; workspace root fixed to `workspace://` = server CAD_Build. Open/Save dialogs list via backend; `doc_open`/`doc_save` paths sent to the daemon are **server-side** paths under CAD_Build (the daemon reads/writes them directly — no byte shuttling).
  - **Import (drop-in)**: browser file → `POST /workspace/upload` → then `import_file {path}` RPC. Wire a drag-and-drop handler (emscripten drop events) + an "Upload…" button in the open dialog.
  - **Export/download**: `export_file` RPC to a workspace path → trigger browser download via `/workspace/download?path=`. Add a "Download" action next to export in the relevant menus (`evaluate_dialogs`/export flow — locate exact menu at implementation; the ribbon/menu code is the other agent's area, coordinate).
  - Enforcement is server-side (gateway clamps to root); client clamping is UX only.
3.8 **Settings/config for web users.** `saveSettings`/config.json path: under EMSCRIPTEN, mount IDBFS at the appdata root (persist across reloads, per-browser). AI provider settings hidden (agent runs in-daemon on Hunter's profile — nothing to configure). Verify the `keys`/`experimental` merge-overlay rule stays intact for desktop (`config.json keys` note in chudware-dev skill).
3.9 **Serving.** Gateway `GET /app` serves the bundle with `Cross-Origin-Opener-Policy: same-origin` + `Cross-Origin-Embedder-Policy: require-corp` (needed for pthreads/SharedArrayBuffer; verify early — 3.2 smoke — that Cloudflare Tunnel passes these; it does not strip response headers). Portal `index.html` gets an "Open Chudware (beta)" button beside the streamed-session button.

**Acceptance P3:** in Chrome + Firefox through the tunnel: app boots to the full ImGui shell at 60 fps, connects (status bar shows daemon connected), opens a `.step+` from CAD_Build via the file dialog, orbit/pick/edit a feature via PropertyManager, sketch mode drag works, undo works, a desktop-GUI edit appears live in the browser tab and vice-versa; upload a STEP → import; export STL → browser download. Disabled panels (from 3.1) are hidden, not broken.

## Phase 4 — copilot for web + polish + cutover

4.1 **Web copilot verification.** `agent_run_v2`/`agent_ask_v2` through the bridge from the WASM client: events stream back on the same connection (per-connection event routing in `Server::dispatch` — verify streamed `events` lines reach the right client under concurrency; add a two-client test where both run `agent_ask_v2` simultaneously). Confirm provider resolution in-daemon uses `~/chudware-web` HOME config — the unit's environment must point `APPDATA`-root resolution at Hunter's real config (`core/agent/paths.cpp` uses `~/AppData/Roaming` fallback on POSIX? — confirm `appdata_root()` on Linux and set the unit env accordingly).
4.2 **Presence/attribution UX**: reuse gateway chat sidebar on the portal; in-app "N users on this doc" chip fed from `doc.lease_change`/attach events (coordinate with UI agent).
4.3 **Cutover**: default portal button switches to the WASM app; streamed sessions remain per-user opt-in (`heavy` seats) until confidence, then reap. Tunnel/auth/whitelist untouched throughout.

## Critical files & anchors

- `core/hostd/doc_registry.{h,cpp}` — the multi-client policy layer; P2 changes land here (identity on events, undo gating).
- `core/doc/session.cpp` — `push_undo`/`trim_history` (~:3186-3200) + `doc_save` (:9129) — P2.3/2.4 undo policy + disk journal.
- `services/ipc/ipc_transport.h` + `ipc_client.h` — the exact transport/client contracts the WASM WebSocket transport must satisfy (P3.3) and where hello gains `user` (P2.1).
- `host/CMakeLists.txt` — dependency ground truth for the wasm target; the miniz/json/imgui/glfw set is the whole client footprint.
- `Webservice/gateway/app.py` + `sessions.py` — where the bridge/router/static/workspace endpoints mount (P0) and the container env changes (P1).

## Verification (end-to-end)

- P0: `systemctl --user start chudware-hostd && ~/chudware-web/bin/chudware-hostd --status` (env `CHUDWARE_INSTANCE_KEY=chudwareweb`) → `clients:0, state:ready`. `pytest Webservice/gateway/tests/test_engine_bridge.py` with `CHUDWARE_WEB_DEV=1`.
- P1: start two sessions from the portal as different users; both open the same part from `/workspace`; edit in one → visible in the other and in Hunter's local GUI without refresh.
- P2: `scripts/gate.sh` green (core changes); the new two-client concurrency pytest; manual: MCP `undo` tool returns `undo_denied`; GUI save greys undo buttons on the second GUI.
- P3: `./build.sh wasm` produces the bundle; browser smoke per acceptance list above through `https://<tunnel-host>/app`; regression: `./build.sh` (desktop) still green — every `#ifdef` seam must leave the native build byte-equivalent in behavior.
- Throughout: existing portal flow (streamed session start/stop, transfer, chat) still works after every gateway change.

## Assumptions & contingencies

1. **WASM direction approved despite DESIGN.md §8's earlier rejection** (Hunter's kickoff note overrides it; the footprint premise was factually wrong). If Hunter reinstates the IP concern → stop after P1/P2 (shared-session streaming already delivers the collaboration goals; the WASM phases are additive).
2. **P1 (streamed shared session) is worth shipping first** — it's config-only and gives the team a shared live session this week while P3 builds. If Hunter prefers all effort on WASM, P1 drops with no impact on other phases.
3. **Shared undo stack + human-only interactive undo** satisfies the requirement ("a shared undo/redo is fine as long as everyone's view updates instantly"). Per-user undo lanes were considered and rejected: the engine's snapshot-stack would need per-author branch merging — high risk, not required.
4. **Human save clears undo for all humans** (single shared stack ⇒ "the user(s)"). If Hunter wants save-without-clear for other users, the fallback is: skip 2.3's clear and only emit the event — one-line change.
5. **Web v1 disables Python-worker-only features** (expected: some Sim/RevEng/parts-scrape flows per the 3.1 inventory). Fallback if something critical is Python-only: run one `engine.worker` server-side supervised by the gateway and bridge it as a second WS channel — the wire protocol is identical (newline JSON), seam already exists in `worker_client.cpp`.
6. **Pinned webd promoted manually** from green builds (0.1 script), never auto-synced with Hunter's dev tree — protects the team from dev breakage. Hunter's dev daemon (default instance key) coexists untouched.
7. **Emscripten port risk** (GLFW port quirks, pthread WS proxying, GLES corner cases): timeboxed by the 3.2/3.3 boot-to-triangle smoke before deeper porting; if blocked, P1 remains the serving path and the fallback is revisiting Selkies latency tuning.
8. **Coordination with the concurrently-running UI/harness agent**: P3 touches `host/src` seams (renderer shader sites, file_explorer, bootstrap) — sequence those edits behind a quick sync with that agent's active write-set; P0/P1/P2 don't overlap it.
9. Undo journal files are plain JSON under the daemon state dir; acceptable disk use (bounded by 100 entries/doc), wiped on close/clear/clean-shutdown. If write latency on huge docs ever bites, compress later — not in v1.

## Decisions Hunter should confirm (one pass; defaults proceed if unanswered)

1. WASM-in-browser go/no-go given the IP-posture note (default: **go**).
2. Ship P1 streamed-shared-session first (default: **yes**).
3. Human save clears the shared undo for everyone (default: **yes**, per requirement wording).
4. Web v1 feature cuts from the 3.1 inventory — anything on the disable list you can't live without?
5. Sim in web v1: route `sim_run_*` in-core via daemon where available, else hide the Sim workspace (default: **hide unless in-core**).

my answers to you:
1) go, but make a note in our readme that lists this as a concern for later incase we try to open it up to the broader public in the future.
2) we are getting this up today, they are going to be connecting to my website (scootware.us) hosted on this computer, so that we can edit files and work on projects together, I have another windows machine on my LAN next to me (32gb ddr4 ram, 8gb rtx 2070 maxq) i could provision to help host as well, and offload some of it so we don't overwhelm as many resources and can run faster for a better user experience.
3) for the "shared undo/redo" i only mean for the specific files, the undo/redo cache should be individual for each file and the one your viewing should be the one that gets used via the keyboard short cuts (ctrl z/y) or the gui buttons. you can add onto this as well , we just need to make it intuitive while handling all the edgecases as well and avoiding unwanted actions/events.
4) the way this is going to be done really needs to be thought out well, most of the time its going to be 1 user/agent/webclient user editing/modifying each part at a time, but occasionally they will be working on parts at the same time and it needs to be handled properly and should feel as if they are both driving the same controls while editing the life files in tandem, get instantaneous updates, and share the same undo/redo cache, etc. this needs to be planned out nicely and figured out how to be done for the smoothest ux possible that avoids data loss, unintended actions, and frustrating mistakes.
5) the webclient needs to have access to the exact same set of copilot features & complex custom agentic workflows as the native GUI application, and they should be sharing the same accounts/apitokens/usage since its all being hosted centrally on my computer. don't hide the simulation it needs to be available to us especially in the future, we arent as worried about IP risks right now since its just some simple tests with close friends/colleagues.
6) it should be synched with master, and be up to date with the latest features, instantly getting the updates as im pushing them out.
7) you figure it out, it needs to work on macos, windows, and linux machines via the webclient though.
9) he is finishing up right now and will be done by the time you get started. + his changes are contained to the agent harness & settings + gui elements associated with each of them.
