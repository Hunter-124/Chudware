# Web parity contract (aicad.webparity/1)

The web app (chudware.com) is the same `host/src/ui` tree compiled to WASM, talking to the
same `aicad-hostd` daemon. hostd serves methods from `libaicad` and falls back to a lazily
spawned Python worker (`core/hostd/worker_bridge.cpp`) for engine-unknown methods — a
fallback the web deployment must never need. This contract makes the split **explicit and
gate-enforced** so web never silently diverges from desktop:

- `web_required` — representative methods the web client depends on. The probe
  (`scripts/web_parity_probe.py`) spawns a scratch hostd **with the Python bridge disabled**
  (`AI_CAD_PYTHON=/nonexistent`, exactly what the WASM client faces) and asserts each IS
  served by libaicad (any response except the unknown-method / worker-bridge class).
- `web_unsupported` — known python-only (or unrouted) methods. The probe asserts each DOES
  fail with the unknown/worker-bridge class. An entry that starts succeeding means a
  cutover landed — **remove the entry in the same commit** (the probe fails until you do;
  that is the loud part of "fail loud").

Verified-C++ notes (2026-07-22, probe run): `sim_run_flow` (LBM path), `ecad_fill_zones`,
`ecad_simulate` are already libaicad-dispatched — earlier docs listing them as python-only
were stale. `sim_run_flow` is not live-called here because empty params take the LBM run
path (side-effect risk in a gate); it belongs in spirit to `web_required`.

Rules:

1. Adding a UI/MCP feature that calls a python-only method? It is desktop-only until the
   zero-Python stream ports it — add the method to `web_unsupported` here AND gate the
   ribbon/PM entry per `T-RF12` (no silent dead buttons).
2. Z-stream nodes (`T-RF15`–`T-RF19`) delete their entries from `web_unsupported` as they
   land. At `T-RF21` closeout the list is **empty by construction** (no Python engine
   left); the probe stays as a regression gate.
3. Method names are worker-RPC names (`contracts/worker_rpc.md` + `core/api/libaicad.cpp`
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
    {"method": "config_list",        "note": "config, C++"}
  ],
  "web_unsupported": [
    {"method": "agent_design",       "family": "legacy python agent",  "remove_at": "T-RF5"},
    {"method": "fw_new_project",     "family": "firmware",             "remove_at": "T-RF15"},
    {"method": "sim_cancel",         "family": "sim cancel",           "remove_at": "T-RF16"},
    {"method": "ecad_design",        "family": "ECAD gaps",            "remove_at": "T-RF17"},
    {"method": "ecad_import_kicad",  "family": "ECAD gaps",            "remove_at": "T-RF17"},
    {"method": "bom_rollup",         "family": "sourcing/BOM",         "remove_at": "T-RF18"},
    {"method": "bom_price",          "family": "sourcing/BOM",         "remove_at": "T-RF18"},
    {"method": "spec_sheet_match",   "family": "sourcing/BOM",         "remove_at": "T-RF18"},
    {"method": "hydraulic_new",      "family": "hydraulic",            "remove_at": "T-RF19"},
    {"method": "pipe_network",       "family": "piping",               "remove_at": "T-RF19"},
    {"method": "struct_analyze",     "family": "structural",           "remove_at": "T-RF19"},
    {"method": "tolerance_stack",    "family": "tolerance",            "remove_at": "T-RF19"},
    {"method": "render_part",        "family": "headless render",      "remove_at": "T-RF19"},
    {"method": "intake_status",      "family": "intake",               "remove_at": "T-RF19"}
  ]
}
```
