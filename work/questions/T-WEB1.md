# T-WEB1 — Billet web service (per-user streamed sessions) — advisor consult

Driver: web-service session (Hunter away; standing instruction: consult advisor, decide, build).
Research corpus: `Webservice/research/{headless,plm,websurface,streaming,auth}.md`.

## Proposed architecture (v1 "billet-web")

```
Browser ──HTTPS/WSS──> Cloudflare Tunnel ──> Access (Google IdP whitelist + OTP for ua.edu)
                                      │ Cf-Access-Jwt-Assertion
                                      ▼
                        Gateway (FastAPI, host, rootless) — portal page, JWT verify,
                        session manager (podman), chat shoutbox WS, reverse proxy
                                      ▼ per-user
                        Session container (Selkies nvidia-glx-desktop base + Billet,
                        NVENC, --device nvidia.com/gpu=all, mem/cpu caps, per-user HOME vol)
                                      ▼
                        Shared PLM: central bare vault repos on host + per-session sync
```

## Q1 Streaming stack
Selkies (MPL-2.0, WebSocket/WebCodecs, NVENC, single HTTP+WS port, proven per-user container
images) over neko/Wolf/Sunshine/Xpra/VNC. Fallback SELKIES_ENCODER=x264enc. Ratify?

## Q2 Auth
CF Access Entra IdP requires tenant app registration (single-tenant) — students can't register
apps in ua.edu. Proposal: Access with (a) Google IdP + exact-email whitelist policy, (b)
One-Time PIN for whitelisted ua.edu emails (email-verified school identity, zero UA IT
involvement). Upgrade path documented: self-hosted oauth2-proxy multi-tenant
(`login.microsoftonline.com/common/v2.0`) if real Entra OAuth becomes mandatory. Acceptable
reading of "validated by Microsoft OAuth for school accounts"?

## Q3 PLM sharing across session containers
PlmScout: vault is per-process dulwich CAS on HEAD; T-R7 remote/sync tools specified in
contracts/plm.md §6 but UNBUILT. Options:
- (a) bind-mount one shared project dir into all containers; rely on HEAD CAS + retries.
  Zero engine work; races on packed-refs/index; scout says don't.
- (b) implement minimal T-R7 subset now: `services/plm/remote.py` — central bare repo on host
  (`/srv/billet/plm/<project>.git`), per-session clone, `plm_sync` = fetch+ff / CAS push,
  conflict → copy-as-variant per §6; wire `plm_add_remote`/`plm_sync` worker+MCP tools already
  reserved in contract. Additive, no contract bump.
Recommend (b). Ratify scope (no locks UI, no merge)?

## Q4 Session image
Selkies base is Ubuntu 24.04; Billet builds on Fedora 44. Options: (a) run the AppImage
(bundles libaicad/OCCT/hostd/CEF) inside the Ubuntu base; (b) rebuild Billet inside an
Ubuntu image layer; (c) Fedora base + hand-rolled Selkies install. Recommend (a) with (b) as
fallback if glibc/GL deps fail. Ratify?

## Q5 Resource protection (32 GiB RAM / 6C12T / 12 GiB VRAM host that must not die)
max_sessions=4 default; per session: --memory 5g --memory-swap 5g --cpus 2, --pids-limit,
per-user HOME volume with quota; idle reaper (no WS traffic 30 min → stop container);
gateway systemd unit MemoryMax=1G; host keeps ≥8 GiB RAM headroom. NVENC cap 8 sessions OK.
Sane numbers?

## Q6 Chat/shoutbox
Portal-level collapsible sidebar (site chrome around the stream iframe), WS fan-out from
gateway, SQLite persistence, presence list from live sessions. Not in-app. Ratify?

## Q7 Code placement
Everything under `Webservice/` (gateway/, image/, deploy/, research/, DESIGN.md) except the
T-R7 subset which lands in `services/plm/` + `services/mcp/` per repo map. Gate: gateway
pytest + image build + smoke. Ratify?

## Q8 WASM
Reject for v1: OCCT/CUDA/CEF footprint, and WASM ships decompilable bytecode to the client —
worse for IP than server-side streaming. Ratify?

## ANSWER — (advisor fills in)

**Advisor:** Fable (2026-07-14). Evidence: `Webservice/research/{headless,plm,websurface,streaming,auth}.md`, `contracts/plm.md` §6, `services/plm/{vault,gitbind,worker_rpc}.py`, `services/mcp/plm_tools.py`, `packaging/linux/build_appimage.sh`, host glibc 2.43 / Ubuntu 24.04 glibc 2.39, OCCT 7.9.3 on Fedora `/usr/lib64`, dulwich 1.2.7 folder bare clone/push smoke, Selkies GHCR tags (20.04/22.04/24.04 only).

### Q1 Streaming stack — **RATIFIED**
Selkies (MPL-2.0, default WebSocket+WebCodecs, NVENC + `x264enc` fallback, single HTTP/WS port, one-container-per-user, unprivileged design) over neko/Wolf/Sunshine/Xpra/VNC. Aligns with headless fact that production GUI is real X11+GLFW GL 3.3 (no EGL-surfaceless path) and with CF Tunnel (HTTPS/WSS only; public UDP/WebRTC media is not free-tunnel-friendly). Keep KasmVNC-in-image and VirtualGL+noVNC as documented fallbacks, not primaries.

### Q2 Auth — **MODIFIED**
**Change:** Phase-1 ship is **Cloudflare Tunnel + Access** with (a) Google IdP + exact-email allowlist and (b) Access **One-Time PIN** for listed `@ua.edu` addresses. Gateway **must** validate `Cf-Access-Jwt-Assertion` (iss/aud/exp + JWKS) and map `email` → session owner. Document Phase-2a (UA IT registers single-tenant Entra app into CF Access) and Phase-2b (oauth2-proxy `entra-id` + `/common` multi-tenant) as upgrade paths.

**Not acceptable as written:** calling OTP “validated by Microsoft OAuth for school accounts.” OTP is email-verified school *mailbox* identity, not Microsoft OAuth/Entra. Research is explicit that CF Access Entra IdP is tenant-scoped and needs an app registration students cannot create in ua.edu. Keep marketing/docs honest: “Access-gated; Google OAuth and/or email OTP for listed ua.edu; Entra when IT allows.” Free ≤50 Access seats is fine.

### Q3 PLM sharing — **MODIFIED** (direction of (b) ratified)
**Reject (a)** shared bind-mount of one working tree: `gitbind` HEAD CAS + `Vault.save` 6× retry is real, but “No cross-process lock”; working `.step+`, `config.json` RMW, `ai_vc/`, and restore-vs-open races remain. `status()["locks"]=[]` stub; `_user_author()` is always `{id:"user"}`. Contract §6 is **one checkout per user + remote sync**, not N writers on one tree.

**Do (b) with these scope pins (still additive `aicad.plm/1`, no contract bump):**
1. Host central **bare** folder remotes under e.g. `/srv/billet/plm/<project>.git`; per-session private project dir + private `.aicadvault`.
2. Implement `services/plm/remote.py` for **folder** kind first: seed/clone, `plm_add_remote`, `plm_sync` = fetch + linear replay of unpublished locals + expected-old push; same-`item_id` collision → copy-as-variant (§6.4). Wire MCP + `worker_rpc.WORKER_METHODS` (today neither registers remote tools; MCP line 18 defers T-R7).
3. **dulwich is sufficient for folder remotes** (verified: `porcelain.clone` path, bare clone, `push` to `file://` bare). Caveat: `fetch` into a fresh bare may leave symbolic HEAD without resolving branch tips — remote code must set `refs/heads/*` + HEAD explicitly after seed/fetch.
4. **Defer locks UI** for demo if needed, but implement `plm_lock`/`plm_unlock` CAS on `refs/aicad/locks` in the same T-R7 subset if two humans share a project (contract model is locks+sync, not merge). No merge UI — ratified.
5. **Mandatory additive:** inject session author `{kind,id,display}` from Access email into vault saves (replace stub); gateway auto-`plm_sync` on session start/end (T-R7 open Q1 for web). Never confuse `gitscm.py` (project `.git`) with vault remotes.

### Q4 Session image — **REJECTED (a); MODIFIED → (c) Fedora-native stack**
**Reject (a) AppImage-in-Ubuntu-Selkies as primary.** `build_appimage.sh` leaves **glibc/libstdc++/GL/X11 host-provided**; host build is **glibc 2.43** (Fedora 44). Ubuntu 24.04 container glibc is **2.39**. Fedora-built Billet/libaicad needing `GLIBC_2.43` will not load on 24.04. Selkies GHCR tags are only **20.04 / 22.04 / 24.04** — no 25.x/26.04 base with newer glibc.

**Reject (b) rebuild-inside-Ubuntu-24.04 as primary.** Host engine links **system OCCT 7.9.3** (`/usr/lib64/libTKernel.so.7.9*` from `opencascade-*-7.9.3-2.fc44`). Ubuntu 24.04’s OCCT is older; rebuilding the full OCCT/libaicad/hostd/CEF stack on Ubuntu is a second product build, not a thin layer. High demo risk.

**Ratify (c) — specific combo:**
- Base: **Fedora 44** container (matches host toolchain).
- CAD stack: **dnf opencascade 7.9** + staged/build Billet + hostd + libaicad (same ABI as workstation), or extract AppImage **onto Fedora** (glibc match).
- Display: **Xvfb** (or Xorg dummy) + **VirtualGL / EGL** so NVIDIA GL is available under headless X (matches headless research: real X11 window required).
- Stream: **selkies via pip/official install** into that image (not `FROM nvidia-glx-desktop:24.04`).
- GPU: rootless Podman + CDI `--device nvidia.com/gpu=…`, `NVIDIA_DRIVER_CAPABILITIES` including graphics+video; SELinux `container_use_devices`.
- Fallback if hand-rolled Selkies on Fedora slips schedule: CPU Xvfb + x264enc Selkies or noVNC — still Fedora base, still no Ubuntu AppImage.

Optional later: if Selkies publishes a glibc≥2.43 Ubuntu tag, re-evaluate (a)/(b); today that tag does not exist.

### Q5 Resource protection — **MODIFIED**
Direction is sound (cgroup caps, idle reaper, gateway MemoryMax, NVENC ≤8). Adjust numbers for a **swap-pressured 32 GiB / 12 GiB VRAM** box:

| Knob | Proposal | Verdict |
|------|----------|---------|
| `max_sessions` | 4 | **Demo default 3**; hard cap 4 only with CPU-encode seats |
| mem/session | 5g hard, swap=5g | **RATIFIED** (no extra container swap thrash) |
| cpus/session | 2 | OK with NVENC; if x264enc, budget CPU carefully (≤2 heavy x264 seats) |
| Host RAM math | 4×5 + gateway 1 + OS/desktop/CF ≈ 8 headroom | Tight if host desktop+browser+Podman live — keep **≥8 GiB free** monitor, prefer 3 seats |
| VRAM | implied OK for 4 GPU-GL | **Not OK as default** — CAD viewports + GLX/EGL share 12 GiB; budget ~1.5–2 GiB/GPU seat → **≤2 GPU-GL seats** |
| NVENC cap 8 | OK | Encode slots are not the bottleneck; **VRAM + RAM** are |
| Idle reaper 30 min | OK | Also reclaim on Access session end / WS gone; force stop if VRAM critical |
| pids-limit + HOME quota | OK | Ratify |

### Q6 Chat/shoutbox — **RATIFIED**
Portal chrome + gateway WS fan-out + SQLite + presence from live sessions. Keep out of Billet binary. Reuse org `channel_send` / agent_events shapes as patterns only (websurface research).

### Q7 Code placement — **MODIFIED**
`Webservice/{gateway,image,deploy,research,DESIGN.md}` + T-R7 in `services/plm/` (+ MCP/worker wiring) is correct. Gate: gateway pytest + image build + smoke. **Add:** gitignore secrets under `Webservice/` (Cloudflare-info.txt is a live secret risk per websurface scout); never commit tokens. Prefer `Webservice/config.example.env` only.

### Q8 WASM — **RATIFIED**
Reject for v1. Full OCCT/CUDA-adjacent/CEF/ImGui desktop is not a product WASM path; bytecode on client is worse IP posture than server-side stream.

---

### A) Demo-embarrassment gaps (must plan before live colleagues)
1. **First login:** Access policy miss → opaque CF error; need a portal “you’re not on the list” page after JWT fail + Hunter runbook to add emails.
2. **Session reconnect:** refresh/WS drop must reattach to **same** container (session id cookie/map), not spawn a second 5 GiB seat; Selkies one-browser-at-a-time messaging.
3. **Project bootstrap:** empty seat with no project is a fail — control plane seeds bare remote + private clone, or “Open demo project” one-click; auto-sync on start/end.
4. **File in/out:** browser upload/download of STEP/step+ (Selkies clipboard/files or gateway `/transfer`) — PLM alone is not enough for “I brought a file on my laptop.”
5. **Cold start latency:** Fedora image pull + Xvfb + Billet first launch can be minutes; pre-pull image, warm pool of 1 idle seat, healthcheck before iframe.
6. **CEF in container:** may need `--no-sandbox` / disable vendor browser for demo stability (`AI_CAD` CEF abort without icudtl already documented).
7. **Identity on timeline:** without author injection every commit is “User” — looks broken in shared PLM.
8. **Secret/gitignore** and tunnel hostname ready before demo day.

### B) Encoder / GPU seat policy — **YES, default CPU x264; reserve NVENC+GPU-GL**
Default every session: `SELKIES_ENCODER=x264enc` (or software) + software/llvmpipe **or** light VirtualGL only if measured safe. **Reserve ≤2 “heavy” seats** with GPU GL + `nvh264enc` for demos that need real viewport feel. Rationale: 12 GiB VRAM shared across KDE/Xorg/Billet/NVENC contexts; streaming research estimates 1–3 GiB/session GPU path; 4 full GPU seats on one 3080 Ti is the most likely live OOM. CPU encode costs cores (7600X3D 6C/12T) — cap concurrent x264 interactive seats at ~3 with reaper.

### C) Minimum viable monitoring (Hunter away)
1. **systemd user/system units** for gateway + cloudflared: `Restart=on-failure`, `MemoryMax` already on gateway.
2. **Host watchdogs (cron or simple timer every 1–5 min):** free RAM ≥8 GiB, load, disk on `/srv/billet` + container storage; if free RAM &lt;4 GiB or disk &lt;10% → stop oldest idle session, optionally refuse new starts.
3. **`nvidia-smi` poll:** VRAM used, GPU util, encoder sessions; alert/log if VRAM &gt;10 GiB or process count anomaly.
4. **Podman:** list billet-* containers; kill any without recent gateway WS heartbeat; enforce max_sessions.
5. **Logs:** gateway access + session start/stop + OOM kills to a rotating file; optional Discord notify on OOM/disk (existing notify pattern).
6. **Health URL** behind Access: `/healthz` shows session count, free RAM, VRAM, image pre-pulled bit — for remote glance.
7. **No** full Prometheus required for v1; a 50-line Python/bash sampler writing JSONL is enough so the box does not melt unattended.

---

### Verdict table (one line each)
| Q | Verdict |
|---|--------|
| Q1 Selkies | **RATIFIED** |
| Q2 CF Access Google+OTP | **MODIFIED** (honest non-Entra wording; JWT verify mandatory) |
| Q3 PLM | **MODIFIED** (b) folder bare + sync; no shared mount; author inject; auto-sync |
| Q4 Image | **REJECT (a)/(b) primary → RATIFY (c)** Fedora+OCCT+Xvfb+VirtualGL+Selkies pip |
| Q5 Resources | **MODIFIED** (3 default seats; ≤2 GPU-GL; 5g hard mem OK) |
| Q6 Chat | **RATIFIED** |
| Q7 Layout | **MODIFIED** (+ secret gitignore) |
| Q8 WASM | **RATIFIED** reject |

### Top 5 risks
1. **Q4 glibc/OCCT trap** if anyone still ships Fedora AppImage into Ubuntu Selkies — instant binary load failure.
2. **VRAM exhaustion** with multi GPU-GL seats on 12 GiB (worse than NVENC session limits).
3. **PLM shared-mount temptation** under schedule pressure — silent working-tree corruption / lost edits despite HEAD CAS.
4. **CF free edge** long-lived WSS + upload limits / idle timeouts under multi-user CAD streams — need reconnect + pilot soak.
5. **ua.edu Entra expectation** vs OTP reality — colleagues may believe they “signed in with Microsoft” when they did not; plus first-start image cold time embarrassing the demo.
