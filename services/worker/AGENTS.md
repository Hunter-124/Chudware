# services/worker — engine routing seam

Single dispatch point for all host→engine calls. Routes to in-process
`libaicad` (`core/api/libaicad.h` `aicad_call`) or the Python oracle fallback
(`engine_oracle/` via subprocess JSON-RPC). Mirrors the contract in
`contracts/worker_rpc.md`.

## Contents (the only modules — R0b purged everything else)

- `worker_client.{h,cpp}` — JSON-RPC client to the engine worker subprocess;
  single funnel for every panel RPC regardless of routing.
- `engine_backend.{h,cpp}` — backend seam: InProcCore (libaicad) vs daemon
  connection (`AI_CAD_ENGINE_DAEMON`), IPC discovery/spawn.
- `cpp_core.{h,cpp}` — in-process libaicad via the flat C ABI.
- `worker_router.{h,cpp}` — method/doc routing policy (C++ core vs Python).

The host compiles these four directly (`host/CMakeLists.txt`);
`core/CMakeLists.txt` also compiles `cpp_core.cpp` into engine-adjacent
targets. Everything that used to live here as stale duplicates of the host's
live copies (`host/src/core/*`, `host/src/bootstrap.cpp`) — async_rpc,
doc_mirror, jobs, sim_client, selection, doc_tabs, bootstrap, worker_types,
doc_session, pick_hit, worker_command_dispatcher — was deleted in R0b; the
host copies are canonical.

## Write-set rules

- Append-only shared: routing table in `worker_client.*` (integrator merges).
- Depends on: `core/api/libaicad.h`, `engine_oracle/worker.py`,
  `services/ipc/*` (IPC transport/discovery).
- `worker_client.cpp` includes `bootstrap.h` resolved from `host/src`
  (runtime provisioning lives with the host) — do not reintroduce a local
  copy here.
- Must NOT include ImGui or UI types.
