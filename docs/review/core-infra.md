# Phase 1 Review — core-infra track

**Branch:** feat/gui-rewrite  
**Ownership:** `core/agent/**`, `core/api/**`, `core/prof/**`, `core/util/**`, `core/CMakeLists.txt`, `core/cmake/**`, `core/docs/**`  
**Focus:** ABI dispatch (`libaicad.cpp`), agent loop, profiler, util expr evaluator, CMake hygiene. `libaicad.h` left unchanged (read-only guardrail).

---

## Fixed

1. **`emit()` allocation failure** (`libaicad.cpp`) — `dup_cstr` could return `nullptr` on OOM while still returning the caller's success/failure code, leaving `*out_json` invalid. Now retries with `{"error":"out of memory"}` and returns `1` when allocation fails.

2. **Non-object `params_json`** (`libaicad.cpp`) — Parsed JSON arrays/scalars caused `params.value(...)` to throw `type_error` inside handlers. After parse, `null` coerces to `{}`; any other non-object type returns a clean `params must be a JSON object` error.

3. **Null `text` in message parts** (`providers.cpp`) — `anthropic_block`, `openai_content`, `openai_message`, and `gemini_part` used `part.value("text", ...)`, which throws when the key exists but is `null`. Switched to the existing `pstr()` tolerant helper.

4. **`aicad_agent` link libs on non-Windows** (`CMakeLists.txt`) — `winhttp` and `bcrypt` gated behind `if(WIN32)`.

5. **Null string params on live agent RPCs** (`libaicad.cpp`) — Added template `jstr()` (tolerates absent/null string fields). Applied to `agent_run`, `agent_run_design`, `agent_run_micro`, `agent_mission_v1`, `agent_answer_question`, `agent_clarify`, and provider-resolution error paths. Prevents `type_error.302` when the host sends `"doc_id": null` or `"request": null` (same class of bug as provider descriptor nulls).

6. **`agent_complete` dead ternary** (`libaicad.cpp`) — `cr.error.empty() ? 0 : 0` simplified to `0` (method is always "handled"; errors live in the JSON `error` field per ABI contract).

7. **`run_assembly_design` silent part failures** (`run.cpp`) — Empty `catch (...)` on `doc_new` / `doc_save` now emits `part_skipped` / `assembly_failed` events with the exception reason so the run timeline shows why parts were dropped (still returns `no_parts_built` when nothing saved, but root cause is visible).

8. **`enable_testing()` ordering** (`CMakeLists.txt`) — Moved before the first `add_test` (`sim`); was after four sim tests (harmless on CMake 3.20+ but non-idiomatic).

9. **Scientific notation coverage** (`test_expr.cpp`) — Added `1.5e2`, `2E-1`, `1e3+2` cases; lexer already supported exponents.

---

## Flagged

| Item | Location | Notes |
|------|----------|-------|
| **Agent HTTP is Windows-only** | `agent/http.cpp`, `providers.cpp` | No non-Win stub; `aicad_agent` compiles only where WinHTTP exists. Cross-platform builds need curl/libcurl or a feature gate. |
| **`prof::start` idempotent** | `prof/sampler.cpp` | Second `start()` while running returns `false` and does not update interval/mode. Documented in header; callers must `stop()` first to retune. |
| **Profiler target race** | `prof/sampler.cpp` | `stop()` clears the duplicated thread handle while in-flight `aicad_call` `TargetScope` may still call `clear_target()`. Depth ref-count prevents double-close today; concurrent stop+dispatch is theoretically racy. Low probability (serialized worker). |
| **SEH OOM path** | `libaicad.cpp` `aicad_call` | `dup_seh_error` can still return `nullptr` on allocation failure after a hardware fault. Extremely rare; host gets `rc=1` with null `out_json`. |
| **Diagnostic RPCs still use `value()`** | `libaicad.cpp` agent probes | Pure/diagnostic handlers (`agent_build_request`, `agent_extract_ops`, …) still use `params.value(key, std::string())`. Lower risk (gate-controlled payloads) but same null-key hazard if extended to live paths. |
| **Mission capabilities deferred** | `agent/run.cpp` `run_mission` | `simulate_fea`, `drawing`, `evaluate` recognized in planner but return "not yet wired". Intentional partial port. |
| **`paths.cpp` platform coupling** | `agent/paths.cpp` | Uses `%APPDATA%` / `USERPROFILE` only; no XDG fallback for Linux agents. |
| **`libaicad.cpp` size** | `api/libaicad.cpp` | ~2800-line monolithic dispatch table. Maintainability / compile-time smell; splitting by domain is a larger refactor (out of scope). |
| **Pre-existing branch diff** | `core/api/libaicad.cpp` | Branch had unstaged selector/session-related dispatch outside this review's lane; not reverted. |
| **`anthropic_parse_response` type field** | `providers.cpp` | `b.value("type", …)` throws if `type` key exists but is null; parity path only, low priority. |

---

## Tests

| Test | File | CMakeLists |
|------|------|------------|
| Message parts tolerate `text: null` | `core/tests/test_agent.cpp` | `test_agent` (existing) |
| `err_sig` exception-line detection | `core/tests/test_machine.cpp` | `test_machine` (registered) |
| Scientific notation literals | `core/tests/test_expr.cpp` | `test_expr` (existing) |
| Sampling profiler stack capture | `core/tests/test_prof.cpp` | `test_prof` (existing) |

**Verified this pass:** `test_agent` 20/20, `test_machine` 4/4, `test_expr` 6/6, `test_prof` 1/1; `libaicad.dll` rebuild OK.

---

## Files touched

| File | Change |
|------|--------|
| `core/api/libaicad.cpp` | OOM-safe `emit()`; params object validation; `jstr()`; live agent RPC null-string hardening; `agent_complete` return cleanup |
| `core/agent/providers.cpp` | Null-safe text extraction in request builders (prior pass) |
| `core/agent/run.cpp` | `part_skipped` / `assembly_failed` events; local `jstr()` for `doc_id` extraction |
| `core/CMakeLists.txt` | WIN32-gate agent libs; `enable_testing()` ordering; `test_machine` target |
| `core/tests/test_agent.cpp` | Null `text` regression test (prior pass) |
| `core/tests/test_machine.cpp` | `err_sig` unit tests (prior pass) |
| `core/tests/test_expr.cpp` | Scientific notation cases |
| `docs/review/core-infra.md` | This report |

**Read-only reviewed (no edits):** `libaicad.h`, `aicad_probe.cpp`, `agent/machine.cpp`, `agent/config.cpp`, `agent/state.*`, `agent/http.*`, `agent/ops_emit.*`, `agent/events.*`, `agent/micro.cpp`, `agent/ask.cpp`, `agent/clarify.cpp`, `agent/pm_assist.*`, `prof/sampler.*`, `util/expr.*`, `core/cmake/*.cmake`, `core/docs/*`.

---

## Architecture notes (reviewed, stable)

- **ABI surface:** Flat C (`aicad_call` / `aicad_free` / `aicad_abi_version`). Windows SEH wrapper releases orphaned doc/drawing/ecad locks on access violation; inner dispatch uses C++ try/catch. Return codes: `0` = handled, `1` = error payload, `2` = unknown method (Python fallback), `3` = ecad OpError envelope.
- **Agent loop:** `run_machine` drives INTAKE→PLAN→MODEL→REVIEW→FIX→MEMORY with budgets, `err_sig` identical-failure cutoff, stagnation guard. `run.cpp` wires live provider calls + doc session ops; `run_assembly_design` multi-part path delegates to `run_design` per part.
- **Profiler:** Windows-only sampling via RtlVirtualUnwind while suspended; DbgHelp symbolization deferred to `report()`. Scoped via `TargetScope` around modeling RPCs in `aicad_call_inner`.
- **Util expr:** Recursive-descent evaluator matching Python `equations.evaluate` grammar/precedence; degrees-in/out trig; `^` → `**` normalization.
- **CMake:** FetchContent for eigen/json/miniz/spectra/doctest; wave-1 libs always built; OCCT-gated doc mesh + `libaicad.dll`; agent/prof/util are OCCT-free static libs linked into the DLL.

---

## Summary counts

| Metric | Count |
|--------|------:|
| **Fixes** | 9 |
| **Flags** | 10 |
| **Tests added/extended** | 3 (1 new file + 2 extended) |
| **Files touched** | 8 |
