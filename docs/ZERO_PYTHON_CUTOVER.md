# Zero-Python Cutover Runbook

Status as of 2026-06-18 (migration P50–P54b complete). This is the precise, wave-by-wave
plan to route the host fully onto the C++ core (`libaicad.dll`) and then delete the replaced
Python. It is written so the irreversible steps are clear, gated, and reversible until the
final deletion.

**Author's note on safety.** The actual host-routing edits live in `host/**`, which is
actively edited (uncommitted WIP). Deletion of Python is a one-way door. So this document
plans the cutover; it does **not** apply host edits or delete anything. Execute a wave only
after committing your host WIP and confirming its gate is green. Rollback at any point with
`AI_CAD_CPP_DOCS=0` (force Python) or an empty `AI_CAD_LIBAICAD` (don't load the DLL).

---

## 1. What is already routed (the strangler seam)

- `host/src/core/cpp_core.cpp` loads `libaicad.dll` at runtime (`LoadLibraryExW`, resolves
  `aicad_call`/`aicad_free`/`aicad_abi_version`). Override path: env `AI_CAD_LIBAICAD`.
- `host/src/worker_client.cpp` `routeDocToCpp()` (≈lines 42–73) + `call()` (≈232–257):
  - Flag `AI_CAD_CPP_DOCS` (default **ON**). A doc created in the C++ core gets a `doc-cpp…`
    id; every subsequent `doc_*`/`config_*`/`weld_*`/`export_*` call carrying that id routes
    to C++. Assemblies fall back to Python (`unsupported`). `doc_compile_cpp`/`doc_validate`
    stay Python (diagnostics).
- **Net:** new PART documents already run on the C++ doc engine end-to-end.

## 2. What is NOT yet routed (but the C++ methods now EXIST + are gated)

| Host RPC (Python worker) | libaicad method (this rewrite) | Gate | Notes |
|---|---|---|---|
| `sim_mesh` | `sim_mesh` | `cpp_mesh_parity` 8/8 | bit-identical (same libgmsh 4.15.2) |
| `sim_run_static` | `sim_run_static` | `cpp_fea_run_parity` 7/7 | study={step_path,material,fixtures,loads} |
| `sim_run_modal` | `sim_run_modal` | `cpp_fea_run_parity` | |
| `sim_run_flow` (LBM) | `sim_lbm` (lattice only) | `cpp_lbm_parity` 6/6 | **needs host-side voxelization + overlay**; defer |
| `agent_run_v2` | `agent_run_design` / `agent_run_micro` | `cpp_orch_run_parity` 20, `cpp_micro_parity` 65 | pick via `agent_should_engage` |
| `agent_ask_v2` | `agent_answer_question` | `cpp_assist_parity` 87 | |
| `agent_pm_assist_v1` | `agent_pm_assist` | `cpp_assist_parity` | |
| `agent_cancel` | (RunControl) | — | wire a cancel flag into the C++ run |
| `get_prompts_v2` | `agent_get_prompts_v2`* | — | *add a thin libaicad handler if needed |
| `ecad_*` | `ecad_*` (P40–P47) | `cpp_ecad_parity` 225 | container/sync/recipes all ported |

Mesh `.msh` load (meshio) and the result **overlay AICM** (von Mises/displacement surface
map, modal/flow overlays) are **display/persistence glue** — they stay Python until the host
renders results from C++ (the host already renders geometry from AICM, so this is a follow-up).

## 3. Param-shape adaptation (the one real bridge)

The C++ agent methods are pure: they take the doc context explicitly and drive the C++ doc
session by `doc_id`. The host must adapt its current Python-worker call shapes:

- `agent_run_v2 {request, …}` → `agent_run_design {request, doc_id, provider, options}`
  where `doc_id` is the open C++ (`doc-cpp…`) session and `provider` is the resolved
  descriptor from `agent_resolve_provider` (or `{name:"mock",…}` in tests). Choose
  `agent_run_micro` when `agent_should_engage` returns true for the resolved provider.
- `agent_ask_v2 {question}` → `agent_answer_question {question, doc, doc_id, revision,
  feature_status, provider}` — pass the open doc's tree + last build status.
- `agent_pm_assist_v1 {params}` → `agent_pm_assist {params, doc, doc_id, revision,
  feature_status, provider}`.

The agent runs against the **C++-backed doc** (default since P16), so its `doc_apply_ops`
calls hit the same session the viewport shows. Live progress events come back in the
`events` array the C++ methods already return; the host should stream them the same way the
Python worker streamed `{id,event,data}`.

## 4. Wave order (each wave: host route → build → gate → then delete its Python)

- **Wave A — sim.** Route `sim_mesh`/`sim_run_static`/`sim_run_modal` in `worker_client.cpp`
  to `cpp_core().call(...)` (a `routeSimToCpp()` mirroring `routeDocToCpp`). Keep
  `sim_run_flow` on Python until voxelization+overlay are host-wired. Verify in-app FEA
  (mesh a part, static + modal). Then delete `engine/sim/{mesher,fea_static,fea_modal}.py`
  bodies' use *only after* `sim_run_flow` + overlay are also cut or kept behind the flag.
- **Wave B — agent.** Route the three agent methods with the param adaptation in §3. Verify
  the in-app AI design / ask / PM-assist against a local mock and (optionally) a live key.
  Then delete `agent/orchestrator/**`, `agent/providers/**`, `agent/{config,ask,pm_assist}.py`
  — **NOT** `agent/loop.py` (inherent, see §5).
- **Wave C — ecad.** Route `ecad_*` to C++. Then delete the ported `engine/ecad/**` compute
  (keep the KiCad S-expr importers — inherent, §5).
- **Wave D — final sweep.** Once the host spawns no Python for the routed surfaces, remove the
  venv provisioning from the shipped build (`build.bat`, `installer/`) for everything except
  the inherent-keep features (§5). This is the last and most irreversible step.

Delete a Python file only when: (a) its host route is live, (b) its C++ gate is green, and
(c) nothing else imports it. Prefer one PR/commit per file with the gate name in the message.

## 5. INHERENT KEEPS — cannot be zero-Python without an embedded interpreter

These execute **arbitrary build123d Python** or are pure display; porting is impossible
without `libpython` or dropping the feature (a product decision — deferred by the user):

- `engine/kernel.py`, `engine/run_model.py` — build123d code execution.
- `agent/loop.py` — the legacy code-gen agent (writes + runs build123d code).
- `mcp_server/server.py` `run_model`/`run_assembly` — execute build123d code from external
  MCP agents (the server's reason to exist).
- the doc **"script" feature** (`engine/doc/features/script.py` / `script.py`).
- `engine/render.py` — pyvista/VTK display (the host already renders geometry via AICM; this
  is the result-image renderer used by MCP screenshots / review).
- KiCad S-expr **importers** (`ecad import_kicad_sch/pcb`).

To reach *literal* zero-Python, either (1) drop these features, or (2) ship an embedded
Python only for them. Until that decision, the product retains a small Python surface for
code-exec + display, while ALL numeric/modeling/EDA/agent compute runs in C++.

## 6. Build / verify / rollback

- Build host: `build.bat` (Release → `host/build/bin/Release/AI-CAD.exe`). Stage
  `libaicad.dll` + its OCCT/gmsh sibling DLLs next to the exe (the core build already copies
  OCCT DLLs and `gmsh-4.15.dll` next to `libaicad.dll`).
- Verify: run the relevant parity gate(s) for the wave, then exercise the feature in the GUI.
- Rollback: `AI_CAD_CPP_DOCS=0` forces the Python worker for new docs; empty `AI_CAD_LIBAICAD`
  stops the DLL loading entirely. Both restore pre-cutover behavior without a rebuild.

## 7. Parity gates (run under the runtime venv python; some take `--dll <libaicad.dll>`)

`tools/cpp_mesh_parity.py`, `cpp_fea_run_parity.py`, `cpp_sim_parity.py`, `cpp_lbm_parity.py`,
`cpp_spice_parity.py`, `cpp_netlist_parity.py`, `cpp_ecad_parity.py`, `cpp_drawing_parity.py`,
`cpp_parity.py`, `cpp_surf_parity.py`, `cpp_session_parity.py`, `cpp_validate_parity.py`,
`cpp_agent_parity.py`, `cpp_agent_support_parity.py`, `cpp_orch_machine_parity.py`,
`cpp_orch_run_parity.py`, `cpp_micro_parity.py`, `cpp_assist_parity.py`; plus `ctest` in
`core/build-occt` (19/19). All green as of P54b.
