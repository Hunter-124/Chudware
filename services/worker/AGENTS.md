# services/worker — engine routing seam

**Owner:** N4a (collapse from `host/src/core/*`).

Single dispatch point for all host→engine calls. Routes to in-process
`libaicad` (`core/api/libaicad.h` `aicad_call`) or the Python oracle fallback
(`engine_oracle/` via subprocess JSON-RPC). Mirrors the contract in
`contracts/worker_rpc.md`.

## Write-set rules
- Append-only shared: routing table in `worker_client.*` (integrator merges).
- Depends on: `core/api/libaicad.h`, `engine_oracle/worker.py`.
- Must NOT include ImGui or UI types — binds to `ui/abstract` view-model at N4a.

## Migration source (host/ → here, N4a)
- `host/src/worker_client.*`
- `host/src/core/app_state.*`, `doc_mirror.*`, `async_rpc.*`, `sim_client.*`
