# Web parity contract (aicad.webparity/1)

The web app (chudware.com) is the same `host/src/ui` tree compiled to WASM, talking to the
same `chudware-hostd` daemon. hostd serves methods from `libchudware` and falls back to a lazily
spawned Python worker (`core/hostd/worker_bridge.cpp`) for engine-unknown methods — a
fallback the web deployment must never need. This contract makes the split **explicit and
gate-enforced** so web never silently diverges from desktop:

- `web_required` — representative methods the web client depends on. The probe
  (`scripts/web_parity_probe.py`) spawns a scratch hostd **with the Python bridge disabled**
  (`CHUDWARE_PYTHON=/nonexistent`, exactly what the WASM client faces) and asserts each IS
  served by libchudware (any response except the unknown-method / worker-bridge class).
- `web_unsupported` — known python-only (or unrouted) methods. The probe asserts each DOES
  fail with the unknown/worker-bridge class. An entry that starts succeeding means a
  cutover landed — **remove the entry in the same commit** (the probe fails until you do;
  that is the loud part of "fail loud").

Verified-C++ notes (2026-07-22, probe run): `sim_run_flow` (LBM path), `ecad_fill_zones`,
`ecad_simulate` are already libchudware-dispatched — earlier docs listing them as python-only
were stale. `sim_run_flow` is not live-called here because empty params take the LBM run
path (side-effect risk in a gate); it belongs in spirit to `web_required`.

Rules:

1. Adding a UI/MCP feature that calls a python-only method? It is desktop-only until the
   zero-Python stream ports it — add the method to `web_unsupported` here AND gate the
   ribbon/PM entry per `T-RF12` (no silent dead buttons).
2. Z-stream nodes (`T-RF15`–`T-RF19`) delete their entries from `web_unsupported` as they
   land. At `T-RF21` closeout the list is **empty by construction** (no Python engine
   left); the probe stays as a regression gate.
3. Method names are worker-RPC names (`contracts/worker_rpc.md` + `core/api/libchudware.cpp`
   dispatch table). One representative method per family is enough — full coverage arrives
   for free at zero-Python.

```web-parity
{
  "web_required": [
    {"method": "ping",               "note": "daemon liveness"},
    {"method": "doc_new",            "note": "part docs are C++"},
    {"method": "doc_close",          "note": "param-error is fine; must be dispatched"},
    {"method": "sketch_enter",       "note": "sketch, C++"},
    {"method": "eval_mass",          "note": "evaluate, C++"},
    {"method": "export_file",        "note": "export, C++"},
    {"method": "cam_simulate",       "note": "CAM is C++-only"},
    {"method": "sim_run_static",     "note": "FEA static, C++"},
    {"method": "ecad_fill_zones",    "note": "ECAD, C++ (verified 2026-07-22)"},
    {"method": "ecad_simulate",      "note": "ECAD, C++ (verified 2026-07-22)"},
    {"method": "parts_catalog_list", "note": "parts catalog, C++-only"},
    {"method": "get_prompts_v2",     "note": "agent prompt catalog, C++"},
    {"method": "config_list",        "note": "config, C++"},
    {"method": "sim_cancel",         "note": "sim cancel registry, C++ (T-RF16 landed 2026-07-22)"},
    {"method": "fw_new_project",     "note": "firmware scaffold, C++ (T-RF15 landed 2026-07-22)"},
    {"method": "fw_sim_run",         "note": "firmware emulator, C++ (T-RF15 resume landed 2026-07-22)"},
    {"method": "bom_rollup",         "note": "sourcing/BOM, C++ (T-RF18 landed 2026-07-22)"},
    {"method": "bom_price",          "note": "sourcing/BOM, C++ (T-RF18 landed 2026-07-22)"},
    {"method": "spec_sheet_match",   "note": "sourcing/spec-match, C++ (T-RF18 landed 2026-07-22)"},
    {"method": "hydraulic_new",      "note": "hydraulic circuit, C++ (T-RF19 landed 2026-07-22)"},
    {"method": "piping_solve",       "note": "piping network solve, C++ (T-RF19 landed 2026-07-22)"},
    {"method": "struct_analyze",     "note": "structural frame solve, C++ (T-RF19 landed 2026-07-22)"},
    {"method": "stackup_analyze",    "note": "tolerance stackup, C++ (T-RF19 landed 2026-07-22)"},
    {"method": "part_place",         "note": "parts placement, C++ (T-RF19 landed 2026-07-22; callers pass record)"}
  ],
  "web_unsupported": [
  ]
}
```

`render_part` was removed 2026-07-22: it is an MCP *tool* name, not a worker-RPC method
(the tools route to `doc_render_png`/`doc_render_view`/`doc_render_contact_sheet`/
`drawing_render_png` — all already C++ in `core/doc/session.cpp`). Same class as
`intake_status` and `ecad_import_kicad`.

`part_search` was removed 2026-07-23 (T-RF21 resolution): the hub FTS stays a
services-side SQLite orchestration tier by design (evaluation C verdict), and the
Parts workspace is desktop-gated for the web client (`host/src/ui/web_capabilities.cpp`)
— no `parts_local_search` C++ query is built until a real web consumer exists (YAGNI).

`ecad_design` was removed 2026-07-23 (T-RF21 resolution): it is LLM agent-orchestration
(services/ecad_design), not a document-engine method — the same class as `intake_status`.
The web client never drives server-side LLM orchestration; all deterministic ECAD
document methods it CAN call are C++-served (web_required).

`intake_status` was removed from the inventory 2026-07-22: it is an MCP *tool* name, not
a worker-RPC method on either side (violates rule 3) — agent-orchestration state the web
client never calls. Same class of removal as `ecad_import_kicad`.

`ecad_import_kicad` was removed from the inventory 2026-07-22: it is not a real RPC
method on either side (KiCad import is `ecad_open` dispatching on the `.kicad_sch`/
`.kicad_pcb` extension), and that path is C++ since T-RF17 — nothing to track.
`sim_cancel` and `fw_new_project` moved to `web_required` the same day as their
cutovers landed; `fw_sim_run` (Renode emulator leg) is the only firmware remainder.

## Tenant-cell browser protocol (2026-10-12 security cutover)

This addendum supersedes the historical shared-native-only browser topology above.
Browser CAD and AI are served only by OS-enforced tenant cells. Desktop/native
dispatch, filesystem paths and session files retain their existing behaviour.

- Gateway hello advertises `web_rpc_version: 2` and `web_rpc_methods: [<method>, …]`.
  WASM dispatch admits only those exact methods; `hello` and `ping` remain available
  before admission. Missing, older, unknown or malformed versions leave CAD/AI
  disabled with `cell_refused`. There is no browser in-process fallback, native
  read-only auto-upgrade or compatibility rebuild fallback.
- Browser workspace root is `"."`; all file/dialog/attachment/workspace references
  are relative. Absolute paths, Windows paths/backslashes, `..` components and NUL
  are rejected, never stripped or reinterpreted. Invalid legacy persisted roots
  are discarded in favour of `"."`. The cell maps accepted RPC paths below `/work`;
  gateway host roots are never browser paths. `/workspace/list` returns root `"."`;
  upload returns `{ok:true,path:<relative>,workspace_key:<key>,bytes:<size>}`.
- `agent_handles_new {session_id?:<existing-cell-handle>}` returns
  `{ok:true,run_id:<opaque>,session_id:<opaque>}` bound to the cell request's actor
  context. The browser mints neither identifier and never sends its local chat id,
  IDBFS sessions directory or runtime JSONL path as a run/session identity. Handles
  are preserved byte-for-byte, bound to local chats, and persisted explicitly as
  `cell_session_id`; historical native runtime ids are not adopted as cell handles.
  Resume, compaction, voice and recovery actions use this binding. Branch/rewind
  commits replace it with the returned child handle.
- Browser Stop is always per-job and exact-handle. Stop before mint records a
  pending cancellation; on mint completion that exact run is cancelled and its
  turn is not sent. No browser path emits empty-run-id cancellation. Desktop Stop
  semantics remain unchanged.
- Browser run controls carry the provider captured at launch, including pre-mint
  Stop, steering and answers; changing the composer's provider does not retarget
  a live run's credentials. Session close uses the provider stored alongside its
  opaque handle as `cell_session_provider`. Handle minting itself carries no
  provider or provider key. Desktop wrappers retain their existing optional-free
  call shape; browser cells receive BYOK credentials only from the gateway.
- Gateway AICM replies use `aicm-cell:` followed by JSON
  `{workspace_key,doc_id,path}`. Browser mesh fetch is
  `/files/aicm?workspace_key=<key>&doc_id=<id>`, never an absolute path or shared
  memory name. Gateway grants are actor/workspace/document/epoch-bound server
  records and are revalidated on fetch; the reference's `path` is not authority.
  Mesh caching keys the full workspace/document reference.
- Feature history, relations and saved warm caches are unaffected: existing-file
  picks, uploads and reopen paths all feed relative references to the same cell
  document-open path rather than requesting a rebuild.

Regression coverage: `host/tests/test_agent_job_routing.cpp` exercises workspace
normalization and rejected boundaries, opaque mint/resume handles, malformed
mint replies, per-job pending Stop and exact protocol/method admission. Parent
verification includes native host tests and the WASM build.
