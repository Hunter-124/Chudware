# Chudware Web (chudware.com)

Browser-native Chudware: the ImGui GUI (`host/`) compiled to WASM/Emscripten, served by the
FastAPI gateway (`gateway/`, uvicorn on 127.0.0.1:8300 behind the `chudware-cloudflared`
tunnel), talking newline JSON-RPC over `WS /engine` to the ONE shared `chudware-hostd`
daemon (`chudware-hostd.service`, instance key `chudwareweb`). Local desktop GUI, MCP agents,
and web users all attach to that daemon and the shared workspace — edits fan out live,
undo/redo is per-document and shared, `doc.changed` events carry `user{id,display}`
attribution.

- Deploy/auto-update: `deploy/install_webd.sh` (atomic cp+mv, never over a live mmap);
  `chudware-webd-sync.path` watches the master build output and reinstalls+restarts the
  daemon on every green rebuild. `./build.sh wasm` stages the web bundle into
  `~/chudware-web/app/` (served at `/app` with COOP/COEP).
- Performance ratchet: `gateway/tests/perf_probe.py` (CHUDWARE_LIVE=1) — baseline in
  `perf/baseline.json`, traces per git SHA in `perf/traces/`; p95 regressions
  >15% (and >0.1 ms) fail. Run it before committing engine/gateway changes.
  Browser boot/FPS beacons append to `perf/traces/client.jsonl` via `POST /perf/client`.
- The old streamed-container portal (`sessions.py`, `proxy.py`, `image/`, `monitor.py`)
  is DEPRECATED and unlinked from the landing page; do not build on it.

## IP note — before any PUBLIC deployment

The WASM bundle ships the full GUI as downloadable bytecode. Behind the current
Cloudflare Access whitelist that is acceptable; **revisit before opening chudware.com
to the general public** (options: keep Access-gated, obfuscate/split, or move rendering
server-side). The engine itself stays on the server — only the client UI is exposed.

## Future compute offload (unprovisioned)

A second Windows LAN machine (32 GB RAM, RTX 2070 MaxQ) is a candidate host for sim
jobs / heavy builds. Not part of v1; the web client already covers cross-platform use.
