# Web client UX audit — first-load compression + frame-pacing diagnosis (2026-07-16)

First perf audit of the **browser (WASM) client** delivery path, complementing the C++
engine audits (`PERF_AUDIT_2026-07-10/11.md`). Covers the shared `billet-webd` daemon at
`cad.scootware.us/app/`. Every number is fresh, measured 2026-07-16.

> **Measurement discipline.** Boot/FPS/latency measured in a real headless Chromium against
> the **dev gateway** (`BILLET_WEB_DEV=1 uvicorn ... :8399`, dev-cookie auth, same appserve.py
> + same staged `~/billet-web/app` bundle as prod — prod itself is CF-Access-gated, unreachable
> headlessly). Two caveats that shape the conclusions:
> 1. **localhost has ~infinite bandwidth** → asset *download* time (41 ms for 9.8 MB) is
>    unrepresentative of remote users. Transfer *size* is the real remote cost; read it off
>    `PerformanceResourceTiming.transferSize`, not wall-clock.
> 2. The headless GPU is an **Intel UHD 620 (ANGLE/D3D11)** — a real GPU but laptop-class,
>    NOT the dev box's RTX 3080 Ti. FPS numbers reflect a weak-iGPU user; treat as the
>    *low end*, not the median.
>
> Reproduce: `python Webservice/gateway/tests/web_asset_probe.py` (asset sizes, no browser);
> `pytest -m perf Webservice/gateway/tests/{perf_probe,web_asset_probe}.py` (RPC + asset ratchet).

## 1. SHIPPED — bundle was served uncompressed; brotli/gzip precompression (first-load 3.6×)

**The finding.** `appserve.py` served the Emscripten bundle raw with no `Content-Encoding`
(`encodedBodySize == decodedBodySize` in Resource Timing). Every cold load shipped:

| asset | raw | brotli | gzip |
|---|---:|---:|---:|
| `Billet.wasm` | 9.37 MB | **2.08 MB (4.5×)** | 3.01 MB |
| `Billet.data` | 2.53 MB | **1.24 MB (2.04×)** | 1.54 MB |
| `Billet.js` | 0.28 MB | **0.06 MB (4.87×)** | 0.07 MB |
| **first-load total** | **12.18 MB** | **3.38 MB (3.6×)** | 4.62 MB |

At 10 Mbit/s that is ~9.7 s → ~2.7 s. `no-cache` already gives cheap repeat loads (304
revalidation via ETag), so this is purely the **cold first-load** win — but that is the
make-or-break first impression, and origin→edge bandwidth benefits regardless of whether
Cloudflare re-compresses `application/wasm` at the edge (unverifiable behind CF Access).

**The fix** (`Webservice/gateway/appserve.py` + `build.sh`):
- `build.sh` precompresses the staged bundle to `.br` (brotli -q 11) and `.gz` (-9) after the
  cache-bust sed, so `index.html` variants carry the versioned URLs.
- `appserve._negotiate` picks the `.br`/`.gz` sibling by `Accept-Encoding` (brotli preferred),
  sets `Content-Encoding` + `Vary: Accept-Encoding`, keeps the **original** media type and the
  original suffix's cache policy (`no-cache` for wasm/js/data).
- **Staleness guard** `_fresh_variant`: a variant is only served when its mtime ≥ the
  original's. A restaged-but-not-recompressed bundle falls back to raw — same js/wasm-skew
  hazard the whole no-cache design fights.

**Verified:** browser (cache disabled) received `Billet.wasm` transfer = 2.18 MB, `enc:
COMPRESSED`, dark/readable UI intact. 6 negotiation unit tests + a `web_asset_probe.py` ratchet
(asserts wasm ≥3×, js ≥3×, data ≥1.5× + gzip fallback). Live: prod gateway restarted, CF purged.

## 2. SHIPPED — web glass-luma readback throttled 8→32 frames

`Renderer::issueLumaPboRead` (web branch, `#ifdef __EMSCRIPTEN__`) does a **synchronous
`glReadPixels`** (WebGL2 can't map a PBO for read-back), which forces a GPU pipeline flush. It
ran every 8th frame; backdrop luma changes slowly (camera/panel motion) and the tone hysteresis
smooths it, so it is now every **32nd** frame (~1 read/s) — 4× fewer sync stalls. The
`lumaEverRead_` guard still reads immediately at boot so glass text is never washed. Native PBO
path (non-Emscripten) untouched. **Verified:** dark text intact on the fresh bundle. Note: this
is a correct micro-opt but did **not** move FPS (§3) — the luma read is not the frame-pacing
bottleneck.

## 3. DIAGNOSED (not yet fixed) — viewport frame pacing is GPU-fill-bound on weak iGPUs

**Measured** (UHD 620, visible/focused tab, rAF-throttling ruled out): mean **23–25 FPS**,
p50 **33.3 ms** (exactly 2× vsync), p95/max **116–133 ms**, ~50% of frames >20 ms. The clean
vsync multiples are the classic **vsync cliff**: per-frame work sits just over the 16.6 ms
budget, so the compositor drops to half-rate. This means shaving a few ms/frame could flip it
back to 60.

**WS op latency is NOT the problem** (ruled out): `doc_new` round-trip over `/engine` measured
p50 **0.34 ms**, mean 4.9 ms. The daemon/bridge is snappy; jank is local GPU fill.

**Prime suspects** (per-frame GPU cost, in likely order):
1. **Full-res 4× MSAA** scene target + `glBlitFramebuffer` resolve (`renderer.cpp:581`) —
   bandwidth-heavy on an iGPU. Biggest lever, but adaptive change needs mid-run FBO realloc.
2. **Liquid-glass panels** each sampling the blur texture + refraction math (many panels).
3. Kawase blur — `kBlurPasses=4` at quarter-res (`renderer.cpp:2231`); cheap relative to #1,
   but a low-risk lever (loop count; FBOs already allocated).

**Recommended follow-up — adaptive quality (web-gated, no native parity risk):**
- Read the existing per-phase GPU timings (`Renderer::glassTimings()`, `renderer.h:379`;
  scene-draw / blur / luma) to confirm which of #1–#3 dominates before touching anything.
- Add a frame-time EMA + hysteresis; when a device sustains p50 >~20 ms, step quality down
  (blur passes 4→2 first — low risk; MSAA 4→2→1 only if the FBO-realloc path is made safe),
  and step back up when it recovers. Self-tunes: weak iGPUs hit 60, the 3080 Ti keeps full
  quality. The weak headless UHD 620 is the ideal bed to validate the *degrade* path.
- Do NOT ship a static MSAA/blur reduction — it degrades quality for every web user (incl.
  strong GPUs) and was explicitly declined here as an unvalidated, quality-regressing guess.

## 4. Verified-clean / already-good (don't re-audit)
- WS RPC round-trip latency (p50 0.34 ms) — daemon + bridge are not a UX bottleneck.
- Repeat-load caching — `no-cache` + ETag → 304 revalidation; only cold load pays full size.
- Boot compute — ~0.8 s to daemon-connected on localhost (wasm compile/instantiate + init);
  acceptable, and dwarfed by download on real networks (now 3.6× smaller).

## 5. Prerequisite bug fixed this session (see git 0d8fb76f)
`agent_run_v2` was **instant-fail on the live daemon** — `role prompt unreadable/empty:
agent/prompts/intake.md`. The systemd unit had no `WorkingDirectory`, so `roles.cpp`'s relative
`agent/prompts` fallback resolved against nothing. `install_webd.sh` now deploys the prompt tree
to `$DST/agent/prompts` and stamps `AI_CAD_PROMPTS_DIR` into the unit. Verified: agent built a
20 mm cube in 51.8 s (was instant-fail). The web/local/agent part-builder works end-to-end again.

## 6. Execution order
1. ✅ Bundle compression (§1) — shipped, tested, live (prod restarted, CF purged).
2. ✅ Luma readback throttle (§2) — shipped, verified no text regression.
3. ✅ Agent prompts deploy (§5) — shipped, agent build verified.
4. Frame pacing (§3) — profile with `glassTimings()` first, then adaptive quality. Its own task;
   needs representative hardware for the full-quality path and the FBO-realloc safety work.

## 7. Web reliability (2026-07-16 addendum — Chudware rebrand session)

**SHIPPED — daemon restart SIGABRT hang.** `hostd::Server::~Server` closed each client conn
then joined its thread, but Linux `close()` does not wake a thread already parked in `poll()`
inside `IpcConn::readLine`, so `join()` hung to `TimeoutStopSec` → SIGABRT on every
binary-triggered restart with a client connected. Fix: `IpcConn::shutdown()` (POSIX
`shutdown(SHUT_RDWR)` / Win `CancelIoEx` / wasm WS teardown) called before join. Verified: stop
with a parked client is now instant + clean (was 30 s + `status=6/ABRT`). This was the primary
cause of the user's "crashed on the other end" + multi-second modelling stalls.

**SHIPPED — boot "worker not started" race.** On web `workerStarted` is set synthetically at
boot to unlock the UI, so `ensureStarterDoc` fired `doc_new` before the daemon WS connected →
transient toast. Gated the starter on a new `daemonEverConnected` latch (set after the first
successful bootstrap, which also runs the orphan-recovery check) + the recovery modal not being
open. Verified: clean boot log, no error toast.

**DEFERRED — recovery_open is a synchronous GL-thread call** (`app.cpp:4063`,
`c->call("recovery_open", {all:true})`). The acute "insane latency" was the daemon shutdown
hang above (now fixed) + the boot race; the sync call itself only blocks briefly for a healthy
daemon rebuilding a couple of parts. A true async refactor is deferred: on web there are no
threads (single-thread + ASYNCIFY), so it needs a request/poll-across-frames pattern, not a
worker thread. Do it if recovery of many/large parts still stalls the frame loop.
