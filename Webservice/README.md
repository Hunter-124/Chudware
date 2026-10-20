# Chudware Web (chudware.com)

Browser-native Chudware: the ImGui GUI (`host/`) compiled to WASM/Emscripten, served by the
FastAPI gateway (`gateway/`, uvicorn on 127.0.0.1:8300 behind the `chudware-cloudflared`
tunnel), talking newline JSON-RPC over `WS /engine` to the ONE shared `chudware-hostd`
daemon (`chudware-hostd.service`, instance key `chudwareweb`). Local desktop GUI
and MCP agents retain their daemon workflows. The browser bridge is currently
contained to `hello` and `ping` only, for all account roles; CAD/AI RPCs and global
events are unavailable until tenant ownership and isolated workers are implemented.
External multi-tenant release remains blocked; see
[`SECURITY_AUDIT.md`](../reports/security/2026-10-12/SECURITY_AUDIT.md).

- Deploy: `deploy/install_webd.sh` (atomic cp+mv, never over a live mmap) builds the
  WASM bundle and promotes the engine as one joint release, restarting the daemon only
  when the joint sha changed. `./build.sh wasm` stages the web bundle into
  `~/chudware-web/app/` (served at `/app` with COOP/COEP). Day-to-day entry point is
  `deploy/update.sh`; nothing auto-promotes on rebuild.
- Performance ratchet: `Webservice/gateway/tests/perf_probe.py` (`CHUDWARE_LIVE=1`) —
  baseline in `Webservice/perf/baseline.json`, traces per git SHA in
  `Webservice/perf/traces/`; p95 regressions >15% (and >0.1 ms) fail. Run it
  before committing engine/gateway changes.
- Asset delivery: `./build.sh wasm` (also run by `deploy/install_webd.sh` in a
  private release stage) precompresses the WASM/JS/data bundle as Brotli and
  gzip. `/app` negotiates those siblings with `Vary: Accept-Encoding`, serves
  WASM as `application/wasm` for streaming compilation, and sends COOP/COEP.
  HTML and the fixed-name JS/WASM/data files revalidate (`no-cache`); portal
  assets with content hashes are immutable for one year. Production's
  authenticated `/app` assets require a session to measure remotely; compare
  local delivery with `Webservice/gateway/tests/web_asset_probe.py` via
  `pytest -m perf` (isolated DB and entitled probe account), then
  `scripts/check_web_parity.py --bundle <staged-app-dir>`. The production
  `MinSizeRel` WASM link disables runtime assertions and stack-overflow probes;
  Debug builds retain both. `Release` is not the deploy default: its `wasm-opt
  -O3` exceeded the 16 GiB deploy host's memory budget.
- The streamed-container portal (`sessions.py`, `proxy.py`, `image/`) was **removed**
  on 2026-08-10. `/app` is the only client. `deploy/monitor.py` survives as a host
  RAM/disk/GPU sampler and disk-pressure flag.
- Browser startup never opens the desktop account/provider onboarding wizard;
  the gateway sign-in remains authoritative. Explorer directory/stat reads are
  asynchronous snapshots: pending reads keep the UI responsive, refresh failures
  retain existing rows with an error, and workspace switches/mutations retire
  stale responses. Merely displaying Explorer does not create directories.

## Phone and tablet controls

`/app/` uses compact controls at widths up to 900 CSS pixels or with a coarse
primary pointer. Device pixel ratio affects the rendering buffer, not UI sizing.

- **File** opens/saves documents; **More** contains Settings, Search, Web and the
  project folder. The workspace button selects Model, Drawing, Simulation and the
  other available workspaces.
- The ribbon's tab selector and **Commands** menu expose the full command groups.
  Drag vertically to scroll long command menus.
- **Files** and **Chat** toggle separate drawers; only one is open at a time.
- Tap a text field, then **Keyboard** in the bottom bar to open the phone keyboard.
  **Hide keyboard** dismisses it. Unicode/composition and multiline paste feed the
  selected field; pasting a newline does not submit chat.
- Drag on the model to orbit, or choose **Pan**. Pinch to zoom and drag two fingers
  to pan. After a pinch, lift both fingers before starting another one-finger drag.
  Camera gestures do not start over a drawer or menu.
- The canvas follows the browser's visible viewport and safe-area insets. The
  bottom control strip is reserved space, not an overlay over the composer.

WebAssembly, WebGL2, and cross-origin-isolated shared memory remain required.
Browser/device restrictions can disable them; the startup panel identifies missing
capabilities. Mobile layout and input are exercised in Chromium touch emulation;
physical iOS/Safari keyboard and safe-area behavior still require device checks.

## IP note — the client bundle is downloadable

`app.chudware.com` is public: there is no Cloudflare Access wall and no allowlist.
Anyone can self-register (email + password or Google/GitHub OAuth); an account
activates through email verification or admin approval, and the gateway resolves
the `chudware_sid` server-side session cookie on every request (see `DESIGN.md`,
decision 2 "Auth", and `deploy/PUBLIC_ROLLOUT.md` §2). Any active account can
therefore download `/app`, and the WASM bundle ships the full GUI as downloadable
bytecode. That exposure is unmitigated; the options remain obfuscating/splitting
the bundle or moving rendering server-side. The engine itself stays private on the
server behind the gateway's `/engine` bridge — only the client UI is exposed.

## Future compute offload (unprovisioned)

A second Windows LAN machine (32 GB RAM, RTX 2070 MaxQ) is a candidate host for sim
jobs / heavy builds. Not part of v1; the web client already covers cross-platform use.
