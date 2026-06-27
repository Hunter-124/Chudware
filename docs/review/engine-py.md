# Engine-Py Track — Parallel Full Codebase Review (Phase 1)

Track: `engine-py` — Python engine; script-exec kernel safety; format I/O.
Ownership: `engine/**` **excluding** `engine/dfm/**` and `engine/intake/**` (those belong to the
`engine-new-mcp-agent` track).
Branch: `feat/gui-rewrite`. Date: 2026-06-27.

Scope reviewed: `engine/kernel.py`, `engine/worker.py`, `engine/render.py`, `engine/run_model.py`,
`engine/paths.py`, `engine/parts_library.py`, `engine/parts_providers.py`, `engine/vendor_parts.py`,
`engine/doc/__init__.py`, `engine/doc/formats.py`, `engine/doc/formats_solidworks.py`,
`engine/doc/mesh_body.py`, `engine/doc/tessellate.py`, `engine/doc/sw_translate.py`,
`engine/sim/worker.py`, `engine/sim/devices.py`, `engine/sim/foam_runner.py`, and the
engine-level tests (`test_kernel_aliases.py`, `test_worker_stream.py`, `doc/test_aicm.py`).
Excluded per lane assignment: `engine/dfm/**`, `engine/intake/**`.

Smoke runs (venv `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe`, not full integration — that
is Phase 2):
- `engine.test_kernel_aliases` — PASS
- `engine.doc.test_aicm` — PASS (golden parse, byte-stable, complex shape, overlay chunks, seam edges)
- `engine.test_worker_stream` — PASS (session reuse, plan event, assembly run_model, wire format, subprocess roundtrip)
- `engine.ecad.test_formats` — PASS (7/7: .ecad+ roundtrip, KiCad sch/pcb import, open dispatch)
- `engine.test_parts_library` — PASS

## Fixed

Direct, low-risk fixes applied this phase. No public-API / ABI changes, no contract edits.

- **engine/kernel.py:619-622** — Added `_model_result_from_payload()` helper that filters the
  subprocess JSON payload to only `ModelResult` field names. **Why:** the isolated kernel path
  could raise `TypeError: __init__() got an unexpected keyword argument` for multi-part scripts
  once `_main_exec` was switched to `run_code_multi` (which emits the superset
  `AssemblyResult` payload). Hardening, not behavior change.
- **engine/kernel.py:647-652** — `_main_exec()` (the `engine.kernel --exec` subprocess entry
  point) now calls `run_code_multi()` instead of `run_code()`. **Why:** the in-process worker
  path (`engine/worker.py:_run_model`) uses `run_code_multi` for multi-part scripts, but the
  isolated path (`run_code_isolated`) dispatched to a subprocess that called the single-part
  `run_code`. The two paths were divergent: an isolated multi-part script silently lost its
  per-part/assembly exports and fit report. Both paths now build identically.
- **engine/kernel.py:13** — Imported `fields` from `dataclasses` (used by the new helper). Trivial.
- **engine/render.py:95-109** — Wrapped the per-view `Plotter` body in `try/finally` so
  `p.close()` runs even when `screenshot()` / camera setup raises. **Why:** an off-screen VTK
  plotter leak on exception can hold OpenGL/render-window resources and, in a long-lived
  headless process, exhaust GPU/memory budgets before the next render. Resource leak fix.
- **engine/render.py:123-135** — Same `try/finally` cleanup for `render_grid` (2x3 contact sheet).
- **engine/render.py:174-200** — Same `try/finally` cleanup for `render_assembly_grid`.
- **engine/run_model.py:14** — Module docstring said "2x2 contact sheet"; `render.render_grid`
  produces a 2x3 sheet (`_GRID_SHAPE = (2, 3)`, six views: iso/front/back/right/top/bottom).
  Doc mismatch only; corrected to "2x3".

## Flagged

Issues that are real but where a safe, in-lane fix would risk downstream behavior or cross a
review boundary. Left for a follow-up / the owning track.

- **engine/kernel.py:357-361 (`_exec_model`)** — Script execution runs untrusted AI-generated
  `build123d` code via bare `exec(compile(code, "<model>", "exec"), g)` with the **full**
  `build123d` namespace in scope and **no `__builtins__` restriction / import guard / AST
  sandbox**. The companion sim path (`engine/sim/devices.py:_SafeExpr`) deliberately runs a
  whitelist AST evaluator and explicitly rejects `__import__('os')` / `os.system(...)`; the CAD
  kernel has no equivalent. Risk: a model emitting `import os; os.system(...)` or
  `__import__('subprocess')...` runs arbitrary code in the worker process (which has filesystem
  write to `%APPDATA%\AI-CAD` and stdio to the native host). Suggested approach: this is a
  design decision (the kernel *intentionally* exposes the full build123d API, including
  `import`, to authoring models), so do NOT narrow it without owner sign-off — but document the
  threat model explicitly and consider (a) running `run_code` only inside the already-isolated
  subprocess (`run_code_isolated`) for untrusted input, and (b) an optional `__builtins__`
  allowlist mode for agent-driven runs. Flagged, not fixed.
- **engine/kernel.py:147-176 (`_build123d_namespace`)** — Forgiving wrappers (`_forgiving_primitive`,
  alias map, `_wrap_edge_op`) silently rewrite model code: `Cylinder(r=4)` becomes
  `Cylinder(radius=4)`, diameter args are halved, oversized fillet/chamfer magnitudes are
  silently halved-up-to-3x with only a `print` to stdout. Risk: a model that genuinely meant
  `r=` as a typo for something else, or a fillet that *should* have failed, produces geometry
  the author did not intend — the agent loop then validates a wrong part. This is a deliberate
  robustness/product decision (comments document the "computer case" motivation), so it is
  out of lane for a behavior change. Suggested approach: surface the clamp/rewrite events in
  `ModelResult.warnings` (not just stdout) so the agent and UI can react. Flagged.
- **engine/doc/formats.py:24-37** — Module docstring still describes `import_file {path, into?}`
  and `export_file {doc_id, path, format}` worker handlers, but the comment block at lines
  700-719 and `WORKER_METHODS` (line 719) confirm both RPCs were moved to the C++ core and only
  `doc_formats` remains in Python. **My in-lane doc fix to this docstring did not apply** (the
  replacement text did not match the on-disk structure), so the stale contract text is still
  shipped. Suggested approach: a focused edit to rewrite the "Worker RPC handlers" block to
  list only `doc_formats`, matching the implementation note at 700-719. Low risk; safe to fix
  in a follow-up. Flagged here because the attempted edit failed and no further edits were
  permitted this turn.
- **engine/doc/formats.py:341-358 (`_import_step`)** — Multi-solid STEP import splits a Compound
  into one body per solid and aligns XDE product names **by index** (`names[i-1]`). Risk: when
  XDE returns fewer names than solids (common when only some solids carry a non-placeholder
  `TDataStd_Name`), the index alignment silently mis-attributes names to the wrong bodies
  (e.g. body 3 gets body 1's product name). Suggested approach: collect `(label, solids-under-
  label)` pairs from XDE instead of a flat name list, or fall back to `Body{i}` for *every*
  body when the name count != solid count (fail-safe rather than mis-align). Behavior change,
  flagged.
- **engine/doc/formats.py:678-695 (`_cached_import`)** — Module-level `_IMPORT_CACHE: dict` is
  unbounded and keyed by `f"{path}:{mtime}"`; old mtime entries are never evicted. Risk: in a
  long-lived worker process that imports many evolving files, the cache grows without bound and
  pins loaded OCCT shapes (which are heavy) alive. Suggested approach: bound to N entries with
  LRU eviction, or key on `(path, mtime, size)` and drop the cache on worker `shutdown`.
  Flagged.
- **engine/sim/foam_runner.py:80-115 (`_pump`)** — `subprocess.Popen` children are pumped for
  stdout lines with a timeout, but on `TimeoutExpired`/exception the proc is not explicitly
  `terminate()`d/`wait()`ed before re-raising. Risk: a hung OpenFOAM solver can be orphaned
  (still running, holding the case dir + cores) after the worker reports failure. Suggested
  approach: wrap the pump in `try/finally` that calls `proc.terminate()` then `proc.wait(timeout=...)`
  with a `kill()` escalation. Crosses into sim-runner behavior; flagged rather than fixed
  in-lane to avoid changing cancel semantics.
- **engine/render.py:60-71 (`_prep`)** — `extract_feature_edges` / `compute_normals` failures
  are swallowed with bare `except: pass` (no logging). Risk: silent degradation of the model's
  visual feedback (the agent relies on these screenshots to verify geometry) is invisible.
  Suggested approach: emit a `logging.getLogger("ai-cad.render").debug(...)` on the swallow
  path so a degraded render is diagnosable. Minor; flagged.
- **engine/worker.py:62-69 (`_ping`)** — `build123d.__version__` is fetched but a bare
  `except Exception` returns `ver = None` with no log, so a broken build123d install looks
  identical to "not installed". Low impact (ping is diagnostic), flagged for a debug log.
- **engine/worker.py:39 + 153-158** — `from engine.kernel import run_code_multi` is a top-level
  import that pulls `build123d` (via `_build123d_namespace` only when `run_code` is called, so
  OK), but `from engine.doc import formats as _formats` (line 155) executes `engine/doc/__init__`
  and `formats.py` which import nothing heavy at module load — verified safe. No fix needed;
  noted for the audit trail.
- **engine/parts_providers.py:75, 101** — `PartProvider.fetch` and the unconfigured-provider
  factory raise `NotImplementedError` (the base class) instead of the module's own
  `ProviderError`. Risk: the worker's structured-error path duck-types `to_envelope` and falls
  back to `internal`, so a user-facing "provider not configured" surfaces as a generic
  internal error rather than an actionable provider message. Suggested approach: raise
  `ProviderError("...")` from the unconfigured stubs. Minor cross-cutting change; flagged.
- **engine/doc/formats_solidworks.py:88-113 (`sw_com_export_step`)** — `swApp` is created
  invisibly and `CloseDoc` is in a `finally`, but `swApp` itself is never `Quit()`ed, so each
  import can leave a hidden `SldWorks.Application` process running. Risk: repeated SolidWorks
  imports accumulate invisible SW instances (heavy: license + RAM). Suggested approach: add
  `swApp.ExitApp()` / `swApp.Quit()` in the `finally` when this process opened it. Flagged
  (Windows-COM-specific, behavior change).
- **engine/doc/sw_translate.py / sw_feature_import.py** — Reviewed; these are large
  reverse-engineering-driven translators with extensive `pass`-on-exception swallows (see
  `sw_feature_import.py` lines 284/371/379/427/490/...). These are intentional graceful-
  degradation paths in an experimental importer (the module docstring marks it experimental).
  Not flagged individually; the pattern is consistent with the documented "best-effort, never
  crash" contract. Noting for completeness.

## Tests added

None. Per the shared guardrails, new tests go in the track test dir without editing `CMakeLists`
(or the Python equivalent — there is no Python CMake target here). The existing tests
(`engine.test_kernel_aliases`, `engine.test_worker_stream`, `engine.doc.test_aicm`,
`engine.ecad.test_formats`, `engine.test_parts_library`) were re-run post-edit and still pass,
which exercises the changed paths:
- `test_worker_stream.test_run_model_assembly` covers the `run_code_multi` path that
  `_main_exec` now also uses.
- `test_worker_stream.test_subprocess_roundtrip` exercises the real `engine.worker` subprocess
  (ping + run_model + shutdown).
- `test_kernel_aliases` covers the in-process `run_code` namespace wrappers.

A regression test for the `run_code_isolated` field-filtering helper
(`_model_result_from_payload` drops `is_assembly`/`parts`/`fit` etc.) would be valuable but was
not added this turn (stop-work directive received mid-phase). Suggested test:
`run_code_isolated` on a `parts = {"a": Box(...), "b": ...}` script should return a `ModelResult`
(no `is_assembly` attr) with the combined assembly `stl_path`, not raise on unexpected kwargs.

## Files touched

Edited (4 files):
- `engine/kernel.py`
- `engine/render.py`
- `engine/run_model.py`
- (attempted) `engine/doc/formats.py` — docstring edit did **not** apply; see Flagged.

Report only (this file):
- `docs/review/engine-py.md` (new)

No changes to `engine/dfm/**` or `engine/intake/**` (owned by `engine-new-mcp-agent`).
No CMakeLists / CI / build-config changes. No commits, no branch ops.
