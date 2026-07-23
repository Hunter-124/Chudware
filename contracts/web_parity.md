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
