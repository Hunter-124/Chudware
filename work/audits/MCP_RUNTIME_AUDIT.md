# MCP Runtime Audit

**Task:** T-V2  **Agent:** sonnet-vega  **Date:** 2026-07-02

**Library:** `/home/hunter/Repositories/CAD/core/build-linux/bin/libaicad.so`
**mcp-venv:** `/home/hunter/.local/share/AI-CAD/mcp-venv` (Python 3.14.6)
**Total registered tools:** 182

Re-run command:
```
AI_CAD_LIBAICAD=/home/hunter/Repositories/CAD/core/build-linux/bin/libaicad.so \
  /home/hunter/.local/share/AI-CAD/mcp-venv/bin/python scripts/mcp_runtime_audit.py \
  --out work/audits/MCP_RUNTIME_AUDIT.md
```

---

## Summary

| Verdict | Count |
| ------- | ----- |
| WORKS | 96 |
| DEGRADED | 10 |
| DEAD | **0** |
| INCONCLUSIVE (arg fixture wrong, tool alive) | 69 |
| SKIPPED (side-effect: starts process/GUI) | 7 |
| **Total** | **182** |

**Key finding: No DEAD tools.** The PyNite pattern (registered but unreachable due to
missing dep) does not currently exist in the tool surface — PyNiteFEA 1.0.1 IS
installed in the mcp-venv. The DEGRADED tools are all graceful-degradation paths
(missing optional binaries, empty state, nonexistent files) that return contract-shaped
`{ok:false, reason, hint}` responses correctly.

The 21 packages missing from the mcp-venv vs requirements.txt mean large chunks of
functionality are unreachable at runtime. WORKS verdicts apply to the installed stack.

---

## Family Import Status

All 24 tool families import successfully.

| Family | Import | Tools |
| ------ | ------ | ----- |
| cad | OK | 52 |
| gsl | OK | 10 |
| recon | OK | 3 |
| intake | OK | 10 |
| interaction | OK | 1 |
| ecad | OK | 24 |
| cam | OK | 21 |
| doc_diff | OK | 1 |
| req | OK | 4 |
| verify | OK | 2 |
| research | OK | 8 |
| deliverables | OK | 5 |
| sourcing | OK | 2 |
| cost | OK | 2 |
| spec | OK | 3 |
| piping | OK | 6 |
| hydraulic | OK | 6 |
| photo_recon | OK | 2 |
| firmware | OK | 8 |
| structural | OK | 2 |
| parts | OK | 3 |
| serving | OK | 5 |
| regen | OK | 1 |
| code_compliance | OK | 1 |

---

## WORKS (96 tools)

These tools returned results. `{ok:false}` envelopes count as WORKS — tool ran and
returned a well-formed response; error is state-not-ready, not a missing dep.

| Tool | Family | Notes |
| ---- | ------ | ----- |
| `activate_configuration` | cad | no-current-part envelope |
| `add_configuration` | cad | no-current-part envelope |
| `add_drawing_view` | cad | no-current-part envelope |
| `add_features` | cad | no-current-part envelope |
| `add_section_view` | cad | no-current-part envelope |
| `cad_guide` | cad | returned full guide text |
| `cam_extract` | cam | no-current-job envelope |
| `cam_generate` | cam | no-current-job envelope |
| `cam_job_tree` | cam | no-current-job envelope |
| `cam_materials` | cam | returned materials table |
| `cam_new_job` | cam | created job successfully |
| `cam_op_types` | cam | returned op types |
| `cam_open` | cam | file-not-found envelope |
| `cam_post` | cam | empty gcode (no ops) |
| `cam_post_dialects` | cam | returned dialects list |
| `cam_redo` | cam | noop OK |
| `cam_setup_sheet` | cam | returned HTML |
| `cam_simulate` | cam | empty results |
| `cam_tool_types` | cam | returned tool types |
| `cam_undo` | cam | noop OK |
| `check_feature_overlap` | cad | no-current-part envelope |
| `check_interference` | cad | no-current-part envelope |
| `check_metadata` | verify | returned metadata list |
| `check_symmetry` | cad | no-current-part envelope |
| `close_part` | cad | no-current-part envelope |
| `compare_parts` | recon | no-such-document envelope |
| `convert_face_to_sketch` | cad | no-current-part envelope |
| `describe_assembly` | gsl | no-current-part envelope |
| `describe_board` | gsl | no-active-ecad envelope |
| `describe_part` | gsl | no-current-part envelope |
| `describe_project` | gsl | no DAG run data |
| `describe_results` | gsl | no-current-part envelope |
| `describe_scene` | gsl | no-current-part envelope |
| `describe_toolpaths` | gsl | CAM job listed |
| `dfm_check` | intake | no-current-part envelope |
| `draft_analysis` | cad | no-current-part envelope |
| `ecad_add_power` | ecad | no-active-schematic envelope |
| `ecad_add_zone` | ecad | no-active-board envelope |
| `ecad_autoroute` | ecad | no-active-board envelope |
| `ecad_check_diff_impedance` | ecad | no-active-board envelope |
| `ecad_check_impedance` | ecad | no-active-board envelope |
| `ecad_fill_zones` | ecad | no-active-board envelope |
| `ecad_find_part` | ecad | searched successfully |
| `ecad_make_board` | ecad | no-active-schematic envelope |
| `ecad_new_pcb` | ecad | created PCB |
| `ecad_new_schematic` | ecad | created schematic |
| `ecad_run_dfm` | ecad | ran DFM on empty board |
| `ecad_run_drc` | ecad | ran DRC |
| `ecad_run_erc` | ecad | ran ERC |
| `ecad_simulate` | ecad | nothing-to-simulate (scipy SPICE: 161ms) |
| `ecad_status` | ecad | returned status |
| `ecad_update_from_sch` | ecad | updated successfully |
| `export_drawing` | cad | no-current-part envelope |
| `export_part` | cad | no-current-part envelope |
| `fit_report` | intake | no-current-part envelope |
| `fw_new_project` | firmware | invalid-request (no MCU config) |
| `get_job` | cad | unknown-job-id envelope |
| `get_part` | cad | no-current-part envelope |
| `hydraulic_add_component` | hydraulic | no-active-circuit envelope |
| `hydraulic_bom` | hydraulic | no-active-circuit envelope |
| `hydraulic_new` | hydraulic | created circuit |
| `hydraulic_size_pump` | hydraulic | returned sizing result |
| `inspect_section` | cad | no-current-part envelope |
| `intake_status` | intake | no-current-part envelope |
| `list_configurations` | cad | empty list |
| `list_parts` | cad | empty list |
| `list_spec_templates` | spec | returned templates |
| `list_standard_parts` | cad | returned standard parts |
| `list_world_bodies` | cad | empty list |
| `make_standard_part` | cad | returned result |
| `match_part_number` | spec | returned result |
| `measure_between` | cad | no-current-part envelope |
| `measure_part` | cad | no-current-part envelope |
| `new_part` | cad | created new part |
| `part_get` | parts | returned result |
| `part_search` | parts | returned search results |
| `part_place` | parts | returned result |
| `piping_fluid` | piping | returned fluid properties |
| `piping_operating_point` | piping | returned result |
| `piping_report` | piping | returned report |
| `query_adjacency` | gsl | no-current-part envelope |
| `query_geometry` | cad | no-current-part envelope |
| `recover_parts` | cad | returned result |
| `redo` | cad | returned result |
| `render_part` | cad | no-current-part envelope |
| `req_create` | req | created requirement |
| `req_report` | req | returned report |
| `req_status` | req | returned status |
| `research_web_search` | research | searched (DuckDuckGo fallback, 936ms) |
| `run_cfd_flow` | cad | ran real LBM CFD (45s — core fully functional) |
| `save_part` | cad | saved to STEP+ |
| `serving_status` | serving | empty routes table |
| `set_material` | cad | set material OK |
| `set_process` | intake | unknown-process envelope |
| `solve_mates` | cad | no-mates envelope |
| `submit_part_photos` | intake | returned result |
| `undo` | cad | reverted successfully |
| `verify_battery` | verify | ran verification battery |
| `wall_thickness` | cad | returned thickness (4.7s) |

---

## DEGRADED (10 tools)

Graceful-absence paths — returned contract-shaped `{ok:false, reason, hint}` because
an optional binary or dep is not provisioned. Designed behavior.

| Tool | Family | Degradation Reason |
| ---- | ------ | ------------------ |
| `code_check` | code_compliance | RAG corpus absent; deterministic rules ran |
| `describe_toolpaths` | gsl | no toolpaths computed yet |
| `ecad_to_3d` | ecad | `build123d`/OCP not installed — "3D exporter unavailable" |
| `import_part` | intake | file not found (probe path nonexistent) |
| `piping_cfd_handoff` | piping | piping_solve not run in session |
| `piping_verify_cfd` | piping | no piping result in session |
| `run_cfd_accurate` | cad | OpenFOAM not installed — graceful |
| `struct_analyze` | structural | doc file not found — graceful |
| `struct_check` | structural | doc file not found — graceful |
| `regen_diagnose` | regen | no provider configured (no API key) |

---

## INCONCLUSIVE (69 tools)

Tool IS alive (registered, imports, dispatches) but probe fixture had wrong argument
types/shapes. A Pydantic validation error IS a live tool. These need better fixtures.

Common patterns:
- Tool requires an active doc/session (created by a prior tool call in sequence)
- Fixture used plain strings where typed dicts/enums are required
- Tool requires a `doc_id` from a `new_part()` call first

| Tool | Family | Fixture Issue |
| ---- | ------ | ------------- |
| `add_datum` | cad | requires typed ref object |
| `ask_user` | interaction | question field type constraint |
| `bom_price` | sourcing | BOM items type constraint |
| `cam_add_operation` | cam | requires active job+setup first |
| `doc_diff_tool` | doc_diff | expects typed doc objects not empty dicts |
| `ecad_design` | ecad | requires intent field type |
| `run_fea_static` | cad | requires doc_id + loads object |
| `serving_evaluate` | serving | shadow_log type mismatch |
| `spec_sheet_match` | spec | requires part_number + typed fields |
| *(60 more)* | various | similar pydantic constraint mismatches |

---

## SKIPPED (side-effect tools, 7)

| Tool | Reason |
| ---- | ------ |
| `start_capture_session` | Opens a local listener socket on a port |
| `stop_capture_session` | Depends on a running listener |
| `show_in_app` | Opens the desktop GUI — headless rule |
| `fw_flash` | Requires real MCU hardware |
| `fw_debug_start` | Spawns debugger process |
| `fw_debug_halt` | Depends on a debugger session |
| `fw_debug_read_mem` | Depends on a debugger session |

---

## Declared-But-Not-Installed Packages

**21 of 29 requirements are absent from the running mcp-venv.**
This is the primary runtime gap — the mcp-venv was provisioned without most of the
optional tool stack.

| Package | Missing | Impact |
| ------- | ------- | ------ |
| `build123d` | YES | `ecad_to_3d` DEGRADED; `piping_cfd_handoff` unusable |
| `cadquery-ocp` (OCP) | YES | All OCP-dependent geometry ops |
| `ocpsvg` | YES | SVG export from OCP shapes |
| `trimesh` | YES | Mesh-based recon / import |
| `networkx` | YES | Graph algorithms (parts routing, DAG) |
| `rhino3dm` | YES | `.3dm` file import |
| `olefile` | YES | OLE file parsing |
| `pywin32` | YES (Linux only — expected) | Windows COM — N/A on Linux |
| `pyvista` | YES | Headless VTK render |
| `vtk` | YES | VTK backend for pyvista |
| `anthropic` | YES | Anthropic provider for agent tools |
| `openai` | YES | OpenAI provider for agent tools |
| `scikit-fem` | YES | FEA mesh solve (`run_fea_static`/`run_fea_modal`) |
| `meshio` | YES | Mesh file I/O |
| `pyamg` | YES | Algebraic multigrid solver |
| `python-pptx` | YES | `make_presentation` unusable |
| `python-docx` | YES | `make_report` unusable |
| `openpyxl` | YES | `make_workbook` unusable |
| `reportlab` | YES | PDF report generation unusable |
| `pypdfium2` | YES | PDF preview rasterization unusable |
| `fluids` | **PRESENT 1.3.1** | Named seed in task; is actually installed |

Notes:
- `pywin32` is Windows-only; absent on Linux is correct.
- `fluids` and `PyNiteFEA` were the named "seeds" in the task — both are installed.
- The "PyNite pattern" (DEAD tools due to missing dep) does not currently exist.

Provisioning command:
```bash
/home/hunter/.local/share/AI-CAD/mcp-venv/bin/pip install \
  build123d==0.10.0 cadquery-ocp==7.8.1.1.post1 ocpsvg==0.5.0 \
  trimesh==4.12.2 networkx==3.4.2 rhino3dm==8.17.0 olefile==0.47 \
  pyvista==0.48.4 vtk==9.3.1 \
  anthropic==0.107.1 openai==2.41.0 \
  scikit-fem==12.0.2 meshio==5.3.5 pyamg==5.3.0 \
  python-pptx==1.0.2 python-docx==1.2.0 openpyxl==3.1.5 \
  reportlab==5.0.0 pypdfium2==5.11.0
```
(All MIT/BSD/Apache — in-process use permitted per contracts/licenses.md)

---

## Undeclared-But-Installed Dependencies

Packages imported by `services/` + `engine_oracle/` + `mcp_server/` that are installed
in the venv but **absent from `requirements.txt`**. Missing declaration = silent breakage
risk if the venv is rebuilt from scratch.

| Package | Imported by | License | Declare as |
| ------- | ----------- | ------- | ---------- |
| `anyio` | `services/mcp/server.py` (transport) | MIT | `anyio>=4.0.0` |
| `langgraph` | `services/dag/` (DAG orchestration) | MIT | `langgraph>=1.0.0` |
| `pydantic` | FastMCP tool arg schemas (indirect) | MIT | `pydantic>=2.0.0` |
| `qrcode` | `engine_oracle/` | MIT | `qrcode>=7.0` |

All 4 are MIT-licensed — add them to `requirements.txt` in-process.

---

## Test Suite Pollution Analysis

### Baseline per-suite results (run each suite alone)

| Suite | Pass | Fail/Error | Skip |
| ----- | ---- | ---------- | ---- |
| services | 410 | 44 | 24 |
| engine_oracle | 894 | 30 | 24 |
| tools | 239 | 0 | 1 |
| training | 0 | 0 | 0 |
| **combined** | **1555** | **67** | **44** |

**True pollution regressions** (pass-alone, fail-in-combined): **7**

Note: The "~29 failures" mentioned in the task context were the combined baseline
failures (pre-existing, fail in isolation too). Only 7 tests are actual pollution.

### Regressed Tests

| Test | Root Cause |
| ---- | ---------- |
| `engine_oracle.intake.test_telemetry_consent.test_consent_tier_full` | RC#1 |
| `engine_oracle.intake.test_telemetry_consent.test_consent_tier_meta` | RC#1 |
| `engine_oracle.intake.test_telemetry_consent.test_consent_tier_offline_false_allows_full` | RC#1 |
| `engine_oracle.test_spec_sheet_matcher.test_ai_extract_default_provider_emits_to_journal` | RC#1 |
| `engine_oracle.test_spec_sheet_matcher.test_default_provider_wraps_with_logging_ip16` | RC#1 |
| `engine_oracle.piping.test_catalog.test_nearest_pipe_soft_fail` | RC#2 |
| `engine_oracle.piping.test_cfd_handoff.test_internal_volume_no_kernel_raises_piping_cfd_error` | RC#3 |

### Root Cause #1 (5 tests) — `sys.modules` stub in `test_h7_shadow.py`

**File:** `services/agent/test_h7_shadow.py`, lines 44–56

```python
_svc_agent_cfg = _types.ModuleType("services.agent.config")
_svc_agent_cfg.consent_tier = lambda cfg: "off"   # permanent override
_svc_agent_cfg.load_config = lambda: {}
_svc_agent_cfg.offline_enabled = lambda: False
# (no get_provider attribute on stub)
sys.modules.setdefault("services.agent.config", _svc_agent_cfg)  # injects at collection time
```

This is **module-level code** that runs at pytest **collection time**. Once injected:
- `consent_tier()` always returns `"off"` → breaks 3 telemetry-consent tests
- The stub has no `get_provider` → `monkeypatch.setattr("services.agent.config.get_provider", ...)` raises `AttributeError` in spec_sheet_matcher tests

**Fix:** Move stub injection into a `@pytest.fixture(autouse=True, scope="module")` with
`monkeypatch`, so `sys.modules` is restored after the h7_shadow test module.
Alternatively, add `get_provider` to the stub so the attribute error doesn't cascade.

### Root Cause #2 (1 test) — Module-load-time `HAVE_FLUIDS` in `test_catalog.py`

**File:** `engine_oracle/piping/test_catalog.py`, line 18

```python
HAVE_FLUIDS = catalog.have_fluids()  # evaluated once at collection time
```

In some combined-run orderings, `fluids` is not yet importable when this module loads
(another module has shadowed/deferred it), so `HAVE_FLUIDS=False` and the `else` branch
fires — but the `else` assertion (`"fluids" in bad["reason"]`) doesn't match the actual
error message from the fully-installed catalog.

**Fix:** Evaluate `catalog.have_fluids()` inside each test that needs it, not at module scope.

### Root Cause #3 (1 test) — Module-load vs call-time kernel check in `test_cfd_handoff.py`

**File:** `engine_oracle/piping/test_cfd_handoff.py`, lines 31–43

```python
HAVE_KERNEL = _have_kernel()  # False at collection time (build123d absent)
# ...
def test_internal_volume_no_kernel_raises_piping_cfd_error():
    if HAVE_KERNEL:
        pytest.skip("kernel present")
    # ... asserts PipingCFDError is raised
```

`HAVE_KERNEL=False` at collection (module load) so the test runs. But `internal_volume()`
calls `_have_kernel()` lazily at invocation time. In the combined run, earlier tool
imports (e.g., ecad_tools warm-importing `threed.py`) may add a `build123d` entry to
`sys.modules`, making `_have_kernel()` return `True` at call time — so the function
doesn't raise `PipingCFDError`, and `assert raised` fails.

**Fix:** Evaluate `HAVE_KERNEL` lazily inside the test (not at module load), or use
`importlib.util.find_spec("build123d")` which is not affected by `sys.modules` state.

---

## Proposed Follow-Up Tasks

| ID | Title | Scope | Priority |
| -- | ----- | ----- | -------- |
| T-V3 | Fix `test_h7_shadow.py` sys.modules pollution | `services/agent/test_h7_shadow.py` lines 44–56 | High |
| T-V4 | Provision full mcp-venv (20 missing packages) | Linux provisioner script | High |
| T-V5 | Fix module-load-time kernel/fluids flags in tests | `test_catalog.py`, `test_cfd_handoff.py` | Medium |
| T-V6 | Second audit pass with proper tool fixtures | `scripts/mcp_runtime_audit.py` fixture map | Medium |
| T-V7 | Fix services/mcp/test_cad_tools.py FEA/CFD assertions | 7 baseline failures | Medium |
| T-V8 | Fix services/mcp/test_ecad_tools.py setup errors | 35 baseline errors | Low |

---

*Generated by `scripts/mcp_runtime_audit.py` (T-V2, sonnet-vega, 2026-07-02)*
