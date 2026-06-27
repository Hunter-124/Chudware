# Engine-Py Track — Parallel Full Codebase Review

Track: `engine-py` — Python engine; script-exec kernel safety; format I/O.
Ownership: `engine/**` **excluding** `engine/dfm/**` and `engine/intake/**` (those belong to the
`engine-new-mcp-agent` track).
Date: 2026-06-27.

Scope reviewed: `engine/kernel.py`, `engine/worker.py`, `engine/render.py`, `engine/run_model.py`,
`engine/paths.py`, `engine/parts_library.py`, `engine/parts_providers.py`, `engine/vendor_parts.py`,
`engine/doc/__init__.py`, `engine/doc/formats.py`, `engine/doc/formats_solidworks.py`,
`engine/doc/mesh_body.py`, `engine/doc/tessellate.py`, `engine/doc/sw_translate.py`,
`engine/sim/worker.py`, `engine/sim/devices.py`, `engine/sim/foam_runner.py`, and engine-level
tests (`test_kernel_aliases.py`, `test_worker_stream.py`, `test_parts_providers.py`,
`doc/test_aicm.py`).
Excluded per lane assignment: `engine/dfm/**`, `engine/intake/**` (logic reviewed read-only;
fixes deferred to `engine-new-mcp-agent`).

Smoke runs (venv `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe`, `python -m <module>`):

| Module | Result |
|--------|--------|
| `engine.test_kernel_aliases` | PASS |
| `engine.test_worker_stream` | PASS |
| `engine.doc.test_aicm` | PASS |
| `engine.ecad.test_formats` | PASS (7/7) |
| `engine.test_parts_library` | PASS |
| `run_code_isolated` multi-part (manual) | PASS — returns `ModelResult`, no `TypeError` on assembly keys |

`engine.test_parts_providers` requires `pytest` (not installed in managed venv); expectation
updated to match `ProviderError` stub path.

## Fixed

Direct, low-risk fixes applied this phase. No public wire-protocol / ABI changes.

### Kernel & worker (prior pass, verified in tree)

- **engine/kernel.py** — `_main_exec()` now calls `run_code_multi()` so the isolated subprocess
  path matches `worker._run_model` for multi-part scripts (per-part exports + fit report).
- **engine/kernel.py** — `_model_result_from_payload()` filters subprocess JSON to `ModelResult`
  field names only, preventing `TypeError` when assembly-only keys (`is_assembly`, `parts`, `fit`)
  appear in the subprocess payload consumed by `run_code_isolated`.
- **engine/render.py** — `try/finally` + `p.close()` on all PyVista plotters (`render_views`,
  `render_grid`, `render_assembly_grid`) to avoid VTK resource leaks on screenshot failure.
- **engine/run_model.py** — Docstring corrected: contact sheet is 2×3 (six views), not 2×2.

### This pass

- **engine/doc/formats.py:22-35** — Rewrote the stale "Worker RPC handlers" docstring. It still
  described `import_file` / `export_file` as Python handlers; only `doc_formats` remains in
  `WORKER_METHODS` (import/export moved to C++ core). Doc-only.
- **engine/parts_providers.py:101-106** — `_call_mcmaster_api` now raises `ProviderError` with an
  actionable message instead of bare `NotImplementedError`. **Why:** configured-but-unwired McMaster
  fetches surfaced as generic `internal` errors through the worker's structured-error duck-typing
  path; `ProviderError` carries a user-facing message consistent with other provider stubs.
- **engine/test_parts_providers.py:52-55** — Updated stub-path assertion to expect `ProviderError`.
- **engine/paths.py:33-46** — `model_dir()` sanitizes the name (`re.sub` + resolve check) so a
  hostile `run_model` / `agent_design` `name` param cannot escape `%APPDATA%\AI-CAD\cache` via
  `..` segments. **Why:** `worker._run_model` and `agent.loop` pass user/model-supplied names
  directly to `paths.model_dir`; previously `CACHE / name` allowed traversal.

## Flagged (engine-new-mcp-agent track)

Issues in excluded packages — noted for the owning track, not fixed here.

- **engine/intake/__init__.py** — `from engine.intake import intake as ik` works (the pattern used
  by `mcp_server/intake_tools.py` and `engine/intake/test_intake.py`). However,
  `from engine.intake import classify` (and other brain symbols) **fails** with
  `ImportError: cannot import name 'classify'`. `__all__ = ["intake"]` only re-exports the
  submodule, not the public API. Suggested fix (engine-new): re-export key entry points from
  `intake.py` in `__init__.py` so callers can `from engine.intake import classify, build_ledger, …`
  without the `intake as ik` indirection.

## Flagged (follow-up, in-lane but behavior-sensitive)

- **engine/kernel.py:357-361 (`_exec_model`)** — AI-generated build123d code runs via bare
  `exec(compile(...), g)` with the full `build123d` namespace and **no `__builtins__` restriction**.
  Contrast: `engine/sim/devices.py:_SafeExpr` uses a whitelist AST evaluator and rejects
  `__import__('os')`. Risk: untrusted model code can import `os`/`subprocess` and run arbitrary
  commands in the worker process (filesystem write under `%APPDATA%\AI-CAD`, stdio to native host).
  The kernel intentionally exposes the full build123d API; mitigation options: (a) route untrusted
  runs through `run_code_isolated` only, (b) optional builtins allowlist for agent runs. Design
  decision — do not narrow without owner sign-off.
- **engine/kernel.py:147-224** — Forgiving primitive/edge-op wrappers silently rewrite kwargs and
  clamp oversized fillet/chamfer magnitudes (announced via `print` to stdout only). Risk: wrong
  geometry passes validation. Suggested: mirror clamp events into `ModelResult.warnings`.
- **engine/doc/formats.py:346-349 (`_import_step`)** — XDE product names aligned to solids **by
  index**; when name count ≠ solid count, names mis-attribute silently. Suggested: pair labels with
  solids via XDE hierarchy, or fall back to `Body{i}` for all when counts mismatch.
- **engine/doc/formats.py:673-685 (`_IMPORT_CACHE`)** — Unbounded mtime-keyed cache; heavy OCCT
  shapes never evicted in a long-lived worker. Suggested: LRU cap or clear on `shutdown`.
- **engine/sim/foam_runner.py:80-108 (`_pump`)** — On timeout, `proc.kill()` is called but there is
  no `wait()` afterward; hung OpenFOAM children may linger as zombies. Suggested: `terminate` +
  `wait` with `kill` escalation in `finally`.
- **engine/render.py:58-71 (`_prep`)** — `compute_normals` / `extract_feature_edges` failures
  swallowed silently; degraded agent screenshots are invisible. Suggested: debug-level log.
- **engine/worker.py:52-57 (`_ping`)** — Broken build123d install returns `ver=None` with no log.
- **engine/doc/formats_solidworks.py** — `swApp` never `Quit()`ed; repeated COM imports may leak
  hidden SolidWorks instances (Windows/COM-specific).
- **engine/parts_providers.py:74-75** — Abstract `PartProvider.fetch` still raises
  `NotImplementedError` (fine for ABC); all concrete stubs now use `ProviderError`.

## Architecture notes (no action)

- **engine/worker.py** — Slim Python surface: `ping`, `run_model`, `agent_design`, `get_prompts`,
  `doc_formats`, vendor-parts registry, ECAD RPCs. Doc modeling engine is C++ (`libaicad`).
- **engine/doc/formats.py** — Pure `import_path` / `export_document` helpers survive for ECAD 3D
  (`engine.ecad.threed` imports `import_path`). Format matrix exposed via `doc_formats` RPC.
- **engine/sim/worker.py** — Separate stdio JSON-RPC process; crash-isolated from doc state.
  Behavioral sources use `_SafeExpr` (good pattern; kernel does not).

## Tests added

None (per guardrails). Existing smoke tests re-run post-edit; `test_parts_providers` expectation
updated only.

Suggested future test: `run_code_isolated` on a `parts={...}` script asserts `ModelResult` shape
and combined assembly `stl_path` without `TypeError` on assembly-only keys.

## Files touched

Edited this review:
- `engine/doc/formats.py` (docstring)
- `engine/parts_providers.py`
- `engine/paths.py`
- `engine/test_parts_providers.py`

Verified in tree (prior pass, unchanged this turn):
- `engine/kernel.py`
- `engine/render.py`
- `engine/run_model.py`

Report:
- `docs/review/engine-py.md`

No changes to `engine/dfm/**` or `engine/intake/**`. No commits.
