# Streaming a containerized Chudware (GLFW/OpenGL) session to browsers

**Date:** 2026-07-14  
**Scope:** External research only — rootless Podman on Fedora 44, RTX 3080 Ti (driver 580), 3–6 concurrent users, per-user session containers, OAuth gateway in front.  
**Non-goals:** No repo edits, no installs.

Hostd (in-repo) is an AF_UNIX engine daemon and does **not** replace a remote GUI surface; production GUI is real X11 + GLFW OpenGL 3.3. Streaming must wrap a full X11 (or VirtualGL/EGL) session around the binary.

---

## Decision summary (TL;DR)

| Rank | Stack | Verdict for us |
|------|--------|----------------|
| **1 (winner)** | **Selkies + GLX desktop image (custom Chudware layer)** | Best browser-native WebSocket/WebRTC path, unprivileged-container design, NVENC, single reverse-proxy port, identity via HTTP auth / gateway |
| 2 | **neko (m1k1o) + custom XFCE/KDE image** | Strong multi-viewer WebRTC, Apache-2.0, NVENC images exist; multi-control model is watch-party oriented, not ideal for private CAD sessions |
| 3 | **Wolf / Games-on-Whales** | Excellent multi-tenant Moonlight + Docker orchestration, MIT; **native Moonlight client**, not first-class browser; needs docker.sock / host network — awkward for rootless Podman + our OAuth HTML gateway |
| 4 | **Sunshine + moonlight-web-stream** | Best encode latency for native clients; web path is third-party GPL, pairing UX, multi-port GameStream heritage |
| 5 | **VirtualGL + TurboVNC/x11vnc + noVNC** | Proven OpenGL remote path; high latency vs H.264 game-stream; fine as **CPU/no-NVENC fallback** |
| 6 | **Xpra HTML5** | Great for app persistence; HTML5 client often falls off NVENC/video path to picture encodings — wrong for dense 3D CAD |

**WASM:** Do **not** ship Chudware as WASM for multi-user production. OCCT-in-WASM demos exist; full desktop + CUDA-adjacent stack is not realistic. WASM does **not** protect IP.

---

## (1) Selkies (formerly Selkies-GStreamer)

### What it is
Open-source low-latency HTML5 remote desktop for Linux X11: default **WebSocket + WebCodecs**, optional **WebRTC**. Capture/encode via Rust PyO3 extensions (`pixelflux` NVENC/VA-API/x264/JPEG; `pcmflux` Opus). Orchestration is Python/`aiohttp`. Historical GStreamer path fully removed.

Sources:
- https://selkies-project.github.io/selkies/design/
- https://github.com/selkies-project/selkies (License: **MPL-2.0**)

### Container story
First-class. Official images:
- `ghcr.io/selkies-project/nvidia-glx-desktop` — isolated **X.Org** in-container (no host `/tmp/.X11-unix`), GLX/OpenGL/Vulkan, KDE Plasma, Selkies + optional KasmVNC fallback  
  https://github.com/selkies-project/docker-selkies-glx-desktop  
- `docker-selkies-egl-desktop` — EGL + VirtualGL path, better multi-container sharing of one GPU  

Images designed for **unprivileged Docker/Kubernetes**, no systemd, no privileged mode. Tags: Ubuntu 20.04/22.04/24.04.  
**Per-user model:** one container per session (exactly our tenancy). Bake Chudware + deps into a `FROM ghcr.io/selkies-project/nvidia-glx-desktop:24.04` derivative; launch one Podman container per authenticated user.

### GPU encode (NVENC)
- `SELKIES_ENCODER=nvh264enc` (and related) when GPU lists H.264 under NVENC matrix; else `x264enc` / VP8/VP9 software.  
- RTX 3080 Ti supports H.264 NVENC.  
- Docs: host driver ≥ ~450 (prefer 470+); NVIDIA Container Toolkit required.  
  https://github.com/selkies-project/docker-selkies-glx-desktop

### Latency / quality
Design goal: **≥60 fps @ 1080p**; software encode ~150% CPU or better with GPU. Positioned as open Linux analog of Parsec/Moonlight-in-browser. Single browser client at a time on Selkies interface (fine for private CAD seats).

### Auth / reverse proxy
- Single TCP/HTTP(S) port (default 8080); reverse proxy must support **WebSocket**.  
- Built-in HTTP basic auth (`SELKIES_ENABLE_BASIC_AUTH`, user/password envs) **or** terminate auth at gateway and strip basic auth.  
- Gateway injects identity by: (a) only proxying after OAuth, (b) setting container env / JWT sidecar for app, (c) optional basic-auth password as short-lived secret per session.  
- No multi-port GameStream exposure if using default WebSocket mode.

### Audio
Supported (Opus); we can ignore/disable.

### Multi-tenancy
**One container per user/session.** Not a shared desktop for many controllers (contrast neko).

### Maintenance / license
- Active: ~1.9k stars, CI image builds, Discord, docs site.  
- **MPL-2.0** — file-level copyleft; fine as **standalone container process** (not linked into Chudware). Prefer not to link MPL into proprietary binary; run side-by-side.

### Fit score for us
**Best overall.** Matches headless-ish workstation VDI, OpenGL X11, NVENC, browser-only clients, reverse-proxy friendliness, rootless-friendly design.

---

## (2) neko (m1k1o/neko)

### What it is
WebRTC stream of a desktop **inside Docker**. Famous for collaborative virtual browsers; also XFCE/KDE full desktops.

Sources:
- https://github.com/m1k1o/neko (License: **Apache-2.0**)
- https://neko.m1k1o.net/

### Container story
Excellent — many `ghcr.io/m1k1o/neko/*` images (firefox, xfce, kde, …) including **Dockerfile.nvidia** variants.  
**Per-user:** run one room/container per user (also **neko-rooms** for orchestration). Shared multi-user-in-one-room is the *product* feature (watch party) — wrong default for private CAD IP.

### GPU encode
NVENC H.264 called out in release notes; NVIDIA image workflows in CI (`ghcr_nvidia.yml`).

### Latency / quality
Marketed **&lt;300 ms** WebRTC. Solid for interactive use; denser 3D may need bitrate tuning. Multi-participant input is a feature we must **disable** (lock control to owner).

### Auth / reverse proxy
HTTP front + WebRTC media. Works behind reverse proxies with proper WebSocket/TURN config. Custom auth typically at reverse proxy or neko-rooms; not as cleanly “single WebSocket app port” as Selkies default mode. Gateway can gate the room URL; identity header support is DIY.

### Multi-tenancy
Container-per-room. Rooms manager optional.

### Maintenance / license
Very healthy (**21k+** stars), Apache-2.0 (ideal license), frequent Docker builds.

### Fit score
**Strong runner-up** if we already invest in WebRTC/TURN and want Apache-2.0 purity. We must hard-disable collaborative control and chat for CAD sessions. Slightly more “browser-in-a-box” DNA than full CAD workstation images vs Selkies GLX desktops.

---

## (3) Wolf / Games-on-Whales

### What it is
Moonlight-compatible streaming **orchestrator**: one Wolf container spawns per-user app containers (GOW images), virtual Wayland/X desktops, NVENC pipelines, gamepad support.

Sources:
- https://github.com/games-on-whales/wolf (License: **MIT**)
- https://games-on-whales.github.io/wolf/stable/user/quickstart.html

### Container story
Built for multi-tenant game streaming. Wolf holds **docker.sock**, host network, uinput/uhid, broad `/dev` mounts. Podman Quadlets documented; NVIDIA path needs toolkit ≥1.16, driver ≥530, often **nvidia-driver volume** rebuild on driver upgrades.

### GPU encode
First-class NVENC (GStreamer pipelines, configurable).

### Latency / quality
Game-stream class (very good) for **Moonlight native clients**.

### Auth / reverse proxy / browser
Primary client is **Moonlight**, not HTML. Browser requires a separate Moonlight-web bridge (see §4). Pairing codes and Moonlight protocol ports ≠ simple OAuth cookie to one HTTPS path. Host-network + docker.sock is a security smell for multi-user university deployment next to OAuth gateway.

### Multi-tenancy
**Excellent** (designed for it) — but orchestration model assumes Wolf as control plane, not our gateway.

### Maintenance / license
Active, MIT, good docs. Still gaming-centric.

### Fit score
**Great if clients were Moonlight apps.** Misaligned with “browser + our auth gateway” and rootless Podman (socket + devices). Keep as reference architecture for virtual display + per-session containers.

---

## (4) Sunshine + Moonlight-web

### Sunshine
Self-hosted GameStream host; NVENC/VAAPI/AMF; web UI for pairing (port 47990).  
https://github.com/LizardByte/Sunshine — **GPL-3.0**  
Docker image exists but “not intended as standalone” experimental packaging.

### moonlight-web-stream
Unofficial browser Moonlight client: WebRTC (or WebSocket+WebCodecs) bridge to Sunshine.  
https://github.com/MrCreativ3001/moonlight-web-stream — **GPL-3.0**  
Documents reverse-proxy auth, HTTPS/secure-context requirements, TURN, port ranges.

### Container / multi-tenant
Sunshine-per-user or Sunshine capturing one desktop is awkward: GameStream pairing, multiple ports, not born as “spin N unprivileged CAD pods.” Steam-Headless-style stacks exist but are heavy.

### Latency
Best-in-class for native Moonlight; web path adds another hop and WebCodecs constraints.

### Auth
Web UI pairing is not OAuth. moonlight-web has its own users; reverse-proxy auth documented but stack is two GPL services + pairing.

### Fit score
**Reject as primary** for browser+OAuth multi-tenant VDI. Optional later for power users with native Moonlight clients.

---

## (5) VirtualGL + noVNC / x11vnc (or TurboVNC)

### What it is
Classic path: VirtualGL redirects GLX to NVIDIA; 2D X on Xvnc/x11vnc; **noVNC** websockify to browser.

Sources (patterns):
- https://wiki.archlinux.org/title/VirtualGL  
- NVIDIA sample Turbovnc+VirtualGL Dockerfiles historically  
- Community: nvidia-ros-vnc, docker-ubuntu-vnc-desktop-opengl  

### Container story
DIY images; common base `nvidia/opengl` + VirtualGL + desktop. Works rootless with CDI if devices mapped. No single “product” image for our stack — we own the glue.

### GPU encode
**Usually none** for the RFB path (pixel/tight/JPEG). Some TurboVNC builds have better codecs; still not NVENC WebRTC. GPU used for **3D render**, not stream encode.

### Latency / quality
Adequate for CAD if LAN and moderate resolution; worse than H.264 game stream on WAN. Known good for correctness of OpenGL.

### Auth / reverse proxy
noVNC is trivial behind HTTPS reverse proxy; password or gateway-only exposure. Identity headers easy.

### Multi-tenancy
One container per user (Xvnc display per session).

### License
VirtualGL (typically LGPL), noVNC (MPL/others), stack is composition of mature tools.

### Fit score
**Fallback path** when NVENC/WebRTC fails: still deliver GL-accelerated CAD over browser with simpler networking.

---

## (6) Xpra HTML5

### What it is
Persistent X11 session server; HTML5 client via built-in web server.

Sources:
- https://github.com/Xpra-org/xpra — **GPLv2+**  
- HTML5 client MPL-2.0: https://github.com/Xpra-org/xpra-html5  
- NVENC docs: https://github.com/Xpra-org/xpra/blob/master/docs/Usage/NVENC.md (consumer GeForce historically needed license key / patch for context limits)  
- HTML5 video: discussions show HTML5 often stuck on **picture** encodings, not h264/nvenc  
  https://github.com/orgs/Xpra-org/discussions/4422  

### Container story
Possible (community GPU HTML5 images exist) but less turnkey than Selkies/neko for K8s-style VDI.

### Fit score
**Not recommended** for interactive full-viewport OpenGL CAD in browser: encoding path for HTML5 is the weak link.

---

## (7) Rootless Podman + NVIDIA CDI / NVENC / EGL-GLX (Fedora 44, driver 580)

### Official position
- NVIDIA Container Toolkit install (RPM/dnf) + **CDI** is the recommended Podman path.  
  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html  
  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html  
- Podman ≥4.1.0: `--device nvidia.com/gpu=all`  
- CDI improves **rootless** compatibility vs classic hook.

### Install sketch (Fedora)
```bash
# Host already has proprietary driver 580.x
curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
  | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo
sudo dnf install -y nvidia-container-toolkit

# Generate CDI (system or user)
sudo mkdir -p /etc/cdi
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
# Rootless alternate:
# mkdir -p ~/.config/cdi && nvidia-ctk cdi generate --output=$HOME/.config/cdi/nvidia.yaml

nvidia-ctk cdi list   # expect nvidia.com/gpu=all, nvidia.com/gpu=0

# SELinux (Fedora)
sudo setsebool -P container_use_devices 1

# Optional: user in video/render groups; --group-add keep-groups
```

Smoke:
```bash
podman run --rm --device nvidia.com/gpu=all --security-opt label=disable \
  nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi
```

### GLX / EGL / NVENC inside container
1. Prefer **Selkies GLX image model**: container-local Xorg + NVIDIA modesetting (image installs matching user-space libs against host driver).  
2. Set `NVIDIA_DRIVER_CAPABILITIES=all` (or `compute,utility,graphics,video,display`) where env-based runtime is used; with pure CDI, capabilities come from the CDI YAML (regenerate after driver upgrades).  
3. **NVENC** needs `video` capability / encoder libs (`libnvidia-encode.so.*`) from host driver — included when CDI is generated from a full driver install.  
4. **EGL** headless: works with EGL device platform / VirtualGL EGL desktop variant; **GLFW + GL 3.3** typically wants **GLX on Xorg** → prefer GLX desktop image (matches HeadlessScout: production GUI is real X11, not pure EGL).  
5. Rootless caveats:  
   - SELinux denials → `container_use_devices` or `--security-opt label=disable` (lab only).  
   - cgroup device access / no-cgroups settings for older stacks.  
   - `/dev/dri/renderD*` must exist and be group-accessible.  
   - Avoid conflicting OCI nvidia hook + CDI double-injection.  
6. Driver 580: CDI regenerate after every driver upgrade; toolkit docs require respec after driver/CUDA config changes.  
7. **Concurrent NVENC:** GeForce consumer limit raised to **8** concurrent encode sessions (2024) — enough for 3–6 users.  
   https://videocardz.com/newz/nvdia-geforce-gpus-now-support-up-to-8-concurrent-nvenc-encoding-sessions  
   https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html  

### Packages / modules on Fedora host
- `akmod-nvidia` / proprietary 580 stack already present.  
- `nvidia-container-toolkit` (+ base).  
- Ensure `nvidia`, `nvidia_uvm`, `nvidia_drm` (prefer `nvidia-drm.modeset=1` if any DRM/Wayland capture path used).  
- Optional: `xorg-x11-drv-nvidia` userspace already paired with driver.  
- **Do not** need full cloudflared/nvidia-toolkit yet for lab; for production install toolkit + CDI as above.

### Expected resource footprint (order-of-magnitude per session)

| Resource | Per active CAD session (estimate) | 6 users (upper) |
|----------|-----------------------------------|-----------------|
| RAM | 2–6 GiB (desktop + Chudware + OCC model) + ~200–400 MiB streamer | 12–36 GiB (host has 32 GiB → **cap ~4–5 heavy sessions** or limit model size) |
| CPU | 0.5–2 cores interactive; software encode +2–4 cores if no NVENC | Prefer NVENC always |
| VRAM | 1–3 GiB viewport/meshes + small NVENC context; shared 12 GiB | **Budget ~1.5–2 GiB/user** → 6 users tight; enforce resolution/AA limits |
| Encode | NVENC session ×1 per user (≤8 OK) | |

Host: 6C/12T Ryzen — fine for 3–6 light sessions with NVENC; CPU encode fallback only for 1–2 users.

---

## (8) WASM verdict (short)

| Question | Answer |
|----------|--------|
| Realistic to compile full Chudware (C++/OCC/OpenGL/CUDA-adjacent, ImGui/GLFW desktop) to WASM? | **No** as a product path. OCCT **samples** and community ports (`opencascade.js`, occt-wasm ~4–5 MB brotli kernels, Cascade Studio) prove **kernel subsets** work under Emscripten. Full parametric app + native UI + CUDA/GPU compute + hostd/PLM integration does not. |
| Sources | https://dev.opencascade.org/doc/occt-7.7.0/overview/html/occt_samples_webgl.html · https://github.com/donalffons/opencascade.js · https://github.com/andymai/occt-wasm |
| Does WASM protect IP? | **No.** Clients download the `.wasm` module; it is reverse-engineerable (wasm-decompile, WABT, dynamic tracing). Complexity is only an obstacle, not protection. OCCT itself is **LGPL-2.1** — shipping modified kernel has obligations. Proprietary Chudware logic in WASM is still on the attacker’s machine. Real IP control = **server-side execution** (our container sessions), licenses, and not shipping kernels to untrusted clients. |

**Decision:** stream native Linux sessions; keep engine on the workstation.

---

## RECOMMENDATION

### Winner
**Selkies (WebSocket/WebCodecs default) on a custom image derived from `ghcr.io/selkies-project/nvidia-glx-desktop` (Ubuntu 24.04 tag), one rootless Podman container per user, NVENC H.264, Cloudflare Tunnel or Caddy/nginx reverse proxy with OAuth (Entra + Google allowlist) in front.**

Why this wins (≤3 bullets):
1. **Browser-native, single HTTP(S)+WebSocket port** — maps cleanly to Cloudflare/our gateway without Moonlight pairing or multi-port GameStream.  
2. **Built for unprivileged GPU containers + isolated Xorg** — matches GLFW/OpenGL 3.3 X11 app and rootless Podman/CDI; official GLX desktop is the closest turnkey to “CAD workstation in a box.”  
3. **NVENC with software fallback**, MPL-2.0 standalone process (not linked into Chudware), active maintenance; multi-tenancy = container-per-user by design.

### Exact container approach
```text
FROM ghcr.io/selkies-project/nvidia-glx-desktop:24.04
# Install Chudware runtime deps, copy chudware binary + OCC libs
# ENTRYPOINT: existing supervisord → Xorg + Selkies + start chudware (or session script)
ENV SELKIES_ENCODER=nvh264enc
ENV SELKIES_FRAMERATE=60
ENV SELKIES_VIDEO_BITRATE=8000
ENV SELKIES_ENABLE_BASIC_AUTH=true   # or false if gateway-only
# PASSWD / SELKIES_BASIC_AUTH_PASSWORD set per-session by orchestrator
```

Orchestrator (our gateway service):
1. OAuth completes → mint session id.  
2. `podman run -d --name chudware-$user --device nvidia.com/gpu=0 --security-opt label=disable \`  
   `-e PASSWD=... -e SELKIES_BASIC_AUTH_PASSWORD=$token -p 127.0.0.1:$port:8080 chudware-selkies:latest`  
3. Reverse proxy `https://cad.example/$user/` → `localhost:$port` with auth cookie; optional disable Selkies basic auth if proxy is sole gate.  
4. Idle timeout → `podman stop/rm`.  
5. Mount shared PLM git-vault read/write via bind mount (`:U`/uidmap as needed).

Alternative if many containers share one GPU poorly on GLX: evaluate **egl-desktop** variant + VirtualGL for denser packing (trade GLFW/GLX testing).

### NVENC / CDI setup steps (Fedora 44)
1. Confirm `nvidia-smi` driver 580 + NVENC.  
2. Install `nvidia-container-toolkit` from NVIDIA RPM repo (see §7).  
3. `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` (or user CDI path for pure rootless).  
4. `sudo setsebool -P container_use_devices 1`.  
5. Validate: `podman run --rm --device nvidia.com/gpu=all --security-opt label=disable ... nvidia-smi`.  
6. Validate encode: run Selkies image with `SELKIES_ENCODER=nvh264enc`, check `nvidia-smi pmon` / encoder utilization.  
7. Regenerate CDI after every driver upgrade.  
8. Cap concurrent sessions ≤6 and ≤8 NVENC; monitor VRAM.

### Fallback (CPU encode / simple path)
1. **Same Selkies image** with `SELKIES_ENCODER=x264enc` (or OpenH264/JPEG) — no NVENC dependency; expect +2–4 CPU cores/session, drop to 30 fps if needed.  
2. If Selkies/WebRTC networking blocked: **KasmVNC mode** already in GLX image (`KASMVNC_ENABLE=true`) — higher latency, no audio, still browser.  
3. Last resort: **TurboVNC/x11vnc + noVNC + VirtualGL** minimal image — maximum compatibility, worst latency.

### Runner-up
**neko Apache-2.0** if legal prefers Apache over MPL and we accept building a locked-down single-user XFCE image + TURN operational cost.

### Explicit non-choices
- Wolf/Sunshine as primary browser path.  
- Xpra HTML5 for 3D.  
- WASM rewrite for IP or full app.

---

## OPEN QUESTIONS

1. **VRAM packing:** Real Chudware viewport VRAM at 1080p/1440p with typical student assemblies — measure before promising 6 concurrent seats on 12 GiB.  
2. **Rootless + Xorg inside container:** Confirm Selkies GLX image under rootless Podman on Fedora SELinux without `label=disable`; may need custom SELinux module.  
3. **TURN vs WebSocket-only:** Default Selkies WebSocket mode may avoid TURN for same-site reverse proxy; validate WAN users through Cloudflare (WebSocket OK; UDP WebRTC may need separate path).  
4. **Clipboard / file upload:** Selkies supports clipboard; do we expose folder mounts only via PLM, or also container home bind mounts?  
5. **Idle reclaim & GPU reset:** Policy when user disconnects but container holds VRAM.  
6. **EGL vs GLX final pick** after one spike: run Chudware under both Selkies GLX and EGL images once toolkit is installed.  
7. **License distribution:** Shipping Selkies MPL-2.0 in image requires providing MPL corresponding source for modified MPL files — process checklist.  
8. **Audio:** Keep disabled to save cycles?  
9. **Cloudflare Tunnel:** Single tunnel to gateway vs per-session ports — recommend gateway multiplex only.  
10. **Student multi-device:** Selkies one-browser-at-a-time — confirm product expectation.

---

## Source index (load-bearing)

| Topic | URL |
|-------|-----|
| Selkies design | https://selkies-project.github.io/selkies/design/ |
| Selkies repo / MPL-2.0 | https://github.com/selkies-project/selkies |
| Selkies GLX desktop | https://github.com/selkies-project/docker-selkies-glx-desktop |
| Selkies EGL desktop | https://github.com/selkies-project/docker-selkies-egl-desktop |
| neko / Apache-2.0 | https://github.com/m1k1o/neko |
| neko docs | https://neko.m1k1o.net/ |
| Wolf MIT | https://github.com/games-on-whales/wolf |
| Wolf quickstart | https://games-on-whales.github.io/wolf/stable/user/quickstart.html |
| Sunshine GPL-3 | https://github.com/LizardByte/Sunshine |
| moonlight-web GPL-3 | https://github.com/MrCreativ3001/moonlight-web-stream |
| NVIDIA CDI | https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html |
| NVIDIA toolkit install | https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html |
| NVENC session limits | https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html |
| GeForce 8-session news | https://videocardz.com/newz/nvdia-geforce-gpus-now-support-up-to-8-concurrent-nvenc-encoding-sessions |
| OCCT WebGL/WASM sample | https://dev.opencascade.org/doc/occt-7.7.0/overview/html/occt_samples_webgl.html |
| opencascade.js | https://github.com/donalffons/opencascade.js |
| Xpra | https://github.com/Xpra-org/xpra |
| Xpra HTML5 video issues | https://github.com/orgs/Xpra-org/discussions/4422 |
| VirtualGL concept | https://wiki.archlinux.org/title/VirtualGL |

---

*End of report.*
