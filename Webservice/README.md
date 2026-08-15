# Chudware Web (chudware.com)

Browser-native Chudware: the ImGui GUI (`host/`) compiled to WASM/Emscripten, served by the
FastAPI gateway (`gateway/`, uvicorn on 127.0.0.1:8300 behind the `chudware-cloudflared`
tunnel), talking newline JSON-RPC over `WS /engine` to the ONE shared `chudware-hostd`
daemon (`chudware-hostd.service`, instance key `chudwareweb`). Local desktop GUI, MCP agents,
and web users all attach to that daemon and the shared workspace — edits fan out live,
undo/redo is per-document and shared, `doc.changed` events carry `user{id,display}`
attribution.

- Deploy: `deploy/install_webd.sh` (atomic cp+mv, never over a live mmap) builds the
  WASM bundle and promotes the engine as one joint release, restarting the daemon only
  when the joint sha changed. `./build.sh wasm` stages the web bundle into
  `~/chudware-web/app/` (served at `/app` with COOP/COEP). Day-to-day entry point is
  `deploy/update.sh`; nothing auto-promotes on rebuild.
- Performance ratchet: `Webservice/gateway/tests/perf_probe.py` (`CHUDWARE_LIVE=1`) —
  baseline in `Webservice/perf/baseline.json`, traces per git SHA in
  `Webservice/perf/traces/`; p95 regressions >15% (and >0.1 ms) fail. Run it
  before committing engine/gateway changes.
- The streamed-container portal (`sessions.py`, `proxy.py`, `image/`) was **removed**
  on 2026-08-10. `/app` is the only client. `deploy/monitor.py` survives as a host
  RAM/disk/GPU sampler and disk-pressure flag.

## IP note — before any PUBLIC deployment

The WASM bundle ships the full GUI as downloadable bytecode. Behind the current
Cloudflare Access whitelist that is acceptable; **revisit before opening chudware.com
to the general public** (options: keep Access-gated, obfuscate/split, or move rendering
server-side). The engine itself stays on the server — only the client UI is exposed.

## Future compute offload (unprovisioned)

A second Windows LAN machine (32 GB RAM, RTX 2070 MaxQ) is a candidate host for sim
jobs / heavy builds. Not part of v1; the web client already covers cross-platform use.
