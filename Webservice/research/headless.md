# Headless / browser-streaming research (Billet)

Scope: host/, core/hostd/, ui/viewport/, platform/window_linux.cpp, build.sh, scripts/gate.sh, host/build-cef/. Non-goals: PLM, auth, GUI panels.

---

## 1) AI_CAD_SHOT / --ui-screenshot / xvfb-run — real window vs EGL?

### Entry
- `host/src/main.cpp:437-445` — `--ui-screenshot [path]` calls `run_gui(path)` (default PNG next to exe if path omitted).
- `host/src/app.cpp:803-804` — `shot = screenshotPath != nullptr`.
- Scenario drive: env `AI_CAD_SHOT=<mode>` (`app.cpp:1489+`, `app_state.h` want* flags). Modes include m1, sketch, asm, fea, flow, drawing, workspaces, iconpipe, etc.

### Display / window
- **Always creates a real GLFW window + OpenGL context.** Not EGL-only, not OSMesa, not pure FBO without a window.
- Linux: prefers X11 (`glfwInitHint(GLFW_PLATFORM, GLFW_PLATFORM_X11)` then `GLFW_ANY_PLATFORM`) — `app.cpp:808-815`, same in `platform/window_linux.cpp:32-36`.
- Context: **OpenGL 3.3 Core** (`app.cpp:817-819`); ImGui backend `#version 330` (`app.cpp:883`).
- Shot size 1340×838; non-shot 1440×900 maximized (`app.cpp:838`).
- Optional `AI_CAD_HIDDEN=1` in shot mode → `GLFW_VISIBLE=false` but still a window FB; comment says glReadPixels works (`app.cpp:826-830`).
- Capture: after N frames, `glReadBuffer(GL_BACK)` + `glReadPixels` + `stbi_write_png` (`app.cpp:3758-3767`). Scenario path via `scenarios.endFrame` (`app.cpp:3728-3734`).

### Offscreen modes (non-GUI)
- `main.cpp --render / --contact / --build / --design`: `makeContext(..., visible=false)` still uses GLFW window (`main.cpp:200-222`) — hidden window, not EGL.
- Viewport renderer uses FBOs for scene/id-pass/thumbnails (`renderer.h`, `id_pass.h`) **after** a live GL context exists.

### Gate / smoke
- `scripts/gate.sh:469-505` Stage 5: `AI_CAD_SHOT=m1` + `--ui-screenshot` under **`xvfb-run -a`** unless `AI_CAD_SHOT_HEADED=1`.
- AppImage smoke similarly uses xvfb (`gate.sh:267-290`).
- Skill map: same contract (`skill://aicad-dev`).

### GL version / extensions
- **Required: OpenGL 3.3 Core** (glad 3.3 loader, `host/CMakeLists.txt:34-35`, shaders `#version 330 core` in `renderer.cpp`).
- Uses standard core FBOs, textures, R32UI id buffer (optional `AI_CAD_IDBUF=rgba8` fallback — `id_pass.h:4-5`). No exotic extension hard-deps beyond 3.3 core (comment: fwidth is core — `renderer.cpp:93`).
- **No EGL/OSMesa code path** in host/platform.

**Answer:** Headless CI runs a **real X11 GLFW window on Xvfb** and reads the backbuffer. There is **no** true headless EGL/surfaceless path today.

---

## 2) core/hostd — protocol, registry, remote GUI?

### Role
Shared **engine** process: one `libaicad` via dlopen; GUI/MCP/agent attach as IPC clients (`docs/HOSTD.md:1-12`, `main.cpp` header).

### Lifecycle (`core/hostd/main.cpp`)
1. `instanceKey` + `lockPath` + `socketPath` (`138-141`).
2. `SingleInstance::acquire` flock; race loser exits 0 (`152-178`).
3. Bind AF_UNIX socket **before** engine load (`206-214`).
4. Async `cpp_core().ensureLoaded` (`232-243`).
5. Autosave under `stateDir()/hostd_autosave/<key>/` (`247-250`).
6. `run(linger)` default 120s idle (`252-255`).

### Protocol (`server.cpp` dispatch ~264+)
Newline-delimited JSON-RPC over local stream (`services/ipc/ipc_transport.h`).

Daemon methods:
- `hello` → proto, abi, git_rev, engine_build_id, daemon_pid, state (loading|ready|failed), id_remap, recovery_pending
- `ping`, `daemon_status`, `daemon_drain` (+ force)
- `recovery_list|open|discard`, `doc_discard`
- `doc_close` may degrade to detach if other rw writer
- agent child: spawn/stop/list via ChildSupervisor
- **else** passthrough to `aicad_call` / libaicad after DocRegistry preCall/postCall

Client kinds: `gui | mcp | agent | test` (`server.h:30`).

### Doc registry (`doc_registry.h`)
- attach_doc / detach_doc / edit_hold / edit_release / subscribe
- ro vs rw; multi-writer CAS via base_revision; events: doc.changed, doc.opened, doc.closed, doc.lease_change
- GC orphan docs after grace (`AICAD_DOC_GC_GRACE_S`)

### GUI integration
- Default daemon mode: `AI_CAD_ENGINE_DAEMON` unset or `1` (`engine_backend.cpp:100-123`).
- GUI connects clientKind=`gui`, drains events each frame (`engine_events.cpp`, `app.cpp:2019-2022`).

### Can it host docs for a remote GUI?
**Local multi-client only** (same machine/user runtime dir). Transport is **AF_UNIX** (`ipc_discovery.h:12-14`), not TCP/WebSocket. Design is shared engine + event mirror for co-located GUI/MCP/agents — **not** a remote display or network doc server. A remote GUI would need a new network front-end; hostd itself is not that.

---

## 3) host/build-cef / extern/cef

- `host/cmake/cef.cmake`: CEF off-screen browser backend for **in-panel vendor sign-in** (McMaster, GrabCAD, …), Chromium ~149 to match Playwright cookies.
- Build: `cmake -S host -B host/build-cef -DAI_CAD_ENABLE_CEF=ON -DCEF_ROOT=extern/cef/...`
- `host/CMakeLists.txt:23` — `AI_CAD_ENABLE_CEF` **default ON** (NullWebView if OFF).
- Sources: cef_web_view, cef_osr_shot, cef_headless, cef_daemon, helper `ai_cad_cef_helper`.
- CLI: `--cef-shot`, `--cef-fetch`, `--cef-download`, `--cef-daemon` (`main.cpp:365-406`) — headless **page** render/fetch for scrapers, not CAD desktop streaming.
- `docs/cef_webview.md`: OSR paints BGRA → GL texture inside ImGui; solves native `<select>`/clipboard vs old screencast webview.
- `host/build-cef/` in tree = CEF-enabled build artifacts (bin/AI-CAD, locales, paks) — **not** a separate product.

**Not** a path to stream the CAD UI to a browser.

---

## 4) Existing remote-display / VNC / streaming / web code

Repo grep (host, core, ui, services, platform, scripts, docs): **no matches** for vnc, webrtc, x11vnc, selkies, sunshine, noVNC, websockify, pixel-stream, remote-desktop.

Related but different:
- CEF OSR / `--cef-daemon` — embedded browser pixels into ImGui / scraper RPC.
- Historical ScreencastWebView (JPEG from worker Chromium) — vendor panel only; CEF superseded (`cef_webview.md`, CMake comments).
- PLM/session websocket mentions in work tasks — collab product, not display streaming.
- `glReadPixels` / PNG shots — CI verification only.

**No in-tree remote desktop or browser CAD stream.**

---

## 5) Runtime deps (from packaging + build)

Source: `packaging/linux/build_appimage.sh`.

### Bundled in AppImage
| Component | Notes |
|---|---|
| `Billet` + assets/fonts | Rubik, JetBrainsMono, CADSymbols under assets/fonts |
| `aicad-hostd` | engine daemon |
| `libaicad.so` + Netgen/lib3mf siblings | full occt tree preferred |
| OCCT 7.9 `libTK*.so` + TBB etc. | ldd closure into usr/lib (~34 TK libs) |
| CEF | libcef, helper, icudtl, paks, locales, libEGL/libGLESv2 (Chromium's), swiftshader |
| Python app/ + wheelhouse | engine/agent/mcp_server/installer |

### Explicitly **host-provided** (excludelist)
- glibc, libstdc++/libgcc
- **GL/X11 graphics-driver stack** (libGL, libX11, …)
- freetype / fontconfig / harfbuzz

AppRun sets `LD_LIBRARY_PATH`, `AI_CAD_LIBAICAD`, `AICAD_HOSTD_BIN`, stable `AICAD_INSTANCE_KEY` from AppImage path hash (`build_appimage.sh:214-234`).

### Practical container needs
- X11 + **OpenGL 3.3** (llvmpipe via Xvfb **or** NVIDIA EGL/GLX passthrough)
- libX11/Xext/Xi/Xcursor/Xinerama/Xrandr, libGL/GLX (or mesa)
- fontconfig + fonts (app embeds UI fonts; system still helps CEF)
- For CEF-on: full staged CEF next to binary; sandbox often needs `--no-sandbox` in containers
- libaicad + OCCT + hostd (or mount from image)

---

## 6) Multi-instance / container collision points

| Resource | Mechanism | Collision if shared host user/home |
|---|---|---|
| hostd singleton | flock `hostd-<key>.lock` | Second client joins **same** engine (often desired on desktop; **bad** if multi-tenant same HOME) |
| hostd socket | `$XDG_RUNTIME_DIR/aicad/hostd-<key>.sock` | Same |
| instance key | `AICAD_INSTANCE_KEY` or FNV hash of hostd realpath (`ipc_discovery.cpp:38-44`) | Same binary path → same key |
| state/logs/autosave | `XDG_DATA_HOME` or `~/.local/share/AI-CAD` (`stateDir`) | Shared config, chat, autosave, imgui.ini |
| GUI config | `appdataAppDir()` AI-CAD/Billet alias (`app_state.cpp:65-78`) | Shared settings |
| Fixed TCP ports | **None** for hostd (AF_UNIX only) | N/A for engine |
| CEF daemon sock | path arg to `--cef-daemon` | Avoid shared sock paths |
| App-level process singleton | **No** GUI single-instance lock found | Multiple Billet processes OK if paths isolated |

### Isolation recipe per container/user session
```text
HOME=/data/users/<id>
XDG_DATA_HOME=$HOME/.local/share
XDG_RUNTIME_DIR=/run/user/<container-uid>   # private
AICAD_INSTANCE_KEY=<user-or-session-id>    # mandatory if hostd binary path identical across containers on shared IPC ns (usually not)
AICAD_HOSTD_BIN=/opt/billet/aicad-hostd
AI_CAD_LIBAICAD=/opt/billet/libaicad.so
# optional: AI_CAD_ENGINE_DAEMON=1 (default)
```
Per-user user namespaces / separate containers already split XDG_RUNTIME_DIR; still set unique HOME/XDG_DATA_HOME so bind-mounted state does not collide.

GPU: 3–6 users on one RTX 3080 Ti — prefer one GLX/Xvfb per container; optional NVIDIA device mount + VirtualGL/EGL later.

---

## RECOMMENDATION

### Winner: **Xvfb (or Xorg dummy) + remote desktop into stock Billet** (least invasive)

Per-user rootless Podman container:
1. Start `Xvfb :1 -screen 0 1920x1080x24` (or `Xorg` + `dummy`/`nvidia`).
2. `DISPLAY=:1` run `/opt/billet/Billet` (AppImage extract or staged tree).
3. Stream with **x11vnc + websockify/noVNC**, or **Selkies-GStreamer**, or **KasmVNC** — pick one stream stack outside the CAD binary.
4. Env-isolate HOME / XDG_* / `AICAD_INSTANCE_KEY` as above.
5. Shared PLM git-vault: bind-mount read/write host path (out of scope detail).

**Why this wins**
- Zero (or near-zero) changes to GLFW/GL path; matches proven `xvfb-run` gate path.
- hostd stays local-per-session; no fake “remote hostd GUI”.
- Fits 3–6 users on one workstation with rootless podman.

**Code changes (minimal, optional but recommended)**
| File | Change |
|---|---|
| *none required for MVP* | Env + container only |
| `host/src/app.cpp` | Optional: default `GLFW_VISIBLE=false` when `AI_CAD_WEB_SESSION=1` / always under `DISPLAY`+env (reuse `AI_CAD_HIDDEN`) to avoid compositor stalls |
| `packaging/linux/` or new `Webservice/session/` | Container entrypoint script: Xvfb, vnc, Billet env, healthcheck |
| `docs/HOSTD.md` | Document multi-tenant env isolation |
| Avoid rewriting | Do **not** build EGL-surfaceless or CEF-as-desktop-stream for MVP |

### Runner-up: GPU-accelerated session (VirtualGL / NVIDIA EGL + Selkies)
- Better viewport perf for FEA/heavy meshes.
- Needs nvidia-container-toolkit (not installed yet) + more ops complexity.
- Same Billet binary; still no app rewrite.

### Reject for MVP: native WebRTC compositor inside Billet / hostd-over-TCP GUI
- No existing code; large surface; hostd is wrong abstraction for pixels.
- CEF is wrong tool (in-panel browser, not desktop).

### Ranked options (3 bullets each)

**1) Xvfb + noVNC/x11vnc (WINNER)**
- Reuses gate’s real X11/GLFW path; no GL rewrite.
- Fastest path to multi-user containers on Fedora/podman.
- CPU GL may be slow for heavy 3D — acceptable for 3–6 light users; upgrade path clear.

**2) NVIDIA + Selkies/KasmVNC**
- Hardware GL for CAD feel.
- Requires toolkit + device policy not yet on host.
- Still no Billet code changes beyond packaging.

**3) In-process stream (custom glReadPixels → WebRTC)**
- Lowest latency / integrated UX long-term.
- Largest eng cost; no scaffolding today; couples app to web stack.
- Defer until container path proves product need.

---

## OPEN QUESTIONS

1. Stream stack preference: noVNC (simple) vs Selkies (WebRTC, better) vs commercial Kasm?
2. Install nvidia-container-toolkit for GPU sessions, or ship CPU Xvfb first?
3. CEF in multi-user containers: disable (`AI_CAD_ENABLE_CEF=OFF` build or runtime null) vs full CEF with `--no-sandbox`?
4. Per-session hostd vs one hostd per user with multiple tools — recommend **one hostd per session container** (default spawn) for isolation.
5. Clipboard/file upload bridge between browser and container FS (stream stack feature, not Billet).
6. Wayland: app prefers X11; keep Xvfb/X11 inside containers even on Wayland host.

---

## Evidence index (file:line)

| Claim | Location |
|---|---|
| GL 3.3 + X11 prefer + shot window | `host/src/app.cpp:808-838` |
| ImGui 330 + glReadPixels PNG | `host/src/app.cpp:883`, `3758-3767` |
| --ui-screenshot entry | `host/src/main.cpp:437-445` |
| Hidden GL for --render | `host/src/main.cpp:200-222` |
| xvfb gate | `scripts/gate.sh:494-505` |
| hostd lifecycle | `core/hostd/main.cpp:138-255` |
| hello/dispatch | `core/hostd/server.cpp:264-320` |
| doc registry | `core/hostd/doc_registry.h:1-90` |
| socket/lock/key | `services/ipc/ipc_discovery.cpp:38-94` |
| daemon default GUI | `services/worker/engine_backend.cpp:100-123` |
| CEF purpose | `docs/cef_webview.md:1-40`, `host/cmake/cef.cmake:1-20` |
| AppImage deps | `packaging/linux/build_appimage.sh:68-180, 214-234` |
| appdata paths | `host/src/core/app_state.cpp:52-78` |
| No VNC/WebRTC | repo grep empty |
