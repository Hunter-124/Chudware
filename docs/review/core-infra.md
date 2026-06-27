# Phase 1 Review — core-infra track

**Branch:** feat/gui-rewrite  
**Ownership:** `core/agent/**`, `core/api/**`, `core/prof/**`, `core/util/**`, `core/CMakeLists.txt`, `core/cmake/**`, `core/docs/**`  
**Focus:** ABI dispatch (`libaicad.cpp`), agent loop, profiler, util expr evaluator, CMake hygiene. `libaicad.h` left unchanged (read-only guardrail).

---

## Fixed

1. **`emit()` allocation failure** (`libaicad.cpp`) — `dup_cstr` could return `nullptr` on OOM while still returning the caller's success/failure code, leaving `*out_json` invalid. Now retries with `{"error":"out of memory"}` and returns `1` when allocation fails.

2. **Non-object `params_json`** (`libaicad.cpp`) — Parsed JSON arrays/scalars caused `params.value(...)` to throw `type_error` inside handlers (e.g. mistyped host payloads). After parse, `null` coerces to `{}`; any other non-object type returns a clean `params must be a JSON object` error.

3. **Null `text` in message parts** (`providers.cpp`) — `anthropic_block`, `openai_content`, `openai_message`, and `gemini_part` used `part.value("text", ...)`, which throws when the key exists but is `null` (common in resolved provider descriptors). Switched to the existing `pstr()` tolerant helper, matching `str_or()` usage elsewhere.

4. **`aicad_agent` link libs on non-Windows** (`CMakeLists.txt`) — `winhttp` and `bcrypt` were linked unconditionally; gated behind `if(WIN32)` so the agent static lib can at least configure on other platforms (HTTP implementation remains Windows-only).

---

## Flagged

| Item | Location | Notes |
|------|----------|-------|
| **Agent HTTP is Windows-only** | `agent/http.cpp`, `providers.cpp` | No non-Win stub; `aicad_agent` compiles only where WinHTTP exists. Cross-platform builds need a curl/libcurl backend or feature gate. Product/architecture decision. |
| **`prof::start` idempotent** | `prof/sampler.cpp` | Second `start()` while running returns `false` and does not update interval/mode. Documented in header; callers must `stop()` first to retune. |
| **Profiler target race** | `prof/sampler.cpp` | `stop()` clears the duplicated thread handle while in-flight `aicad_call` `TargetScope` may still call `clear_target()`. Depth ref-count prevents double-close today; concurrent stop+dispatch is theoretically racy. Low probability (serialized worker). |
| **SEH OOM path** | `libaicad.cpp` `aicad_call` | `dup_seh_error` can still return `nullptr` on allocation failure after a hardware fault. Extremely rare; would leave host without an error JSON. |
| **`enable_testing()` ordering** | `CMakeLists.txt` | `add_test` for `sim`/`modal`/`tet10` appears before `enable_testing()` at line 200. Harmless on current CMake but non-idiomatic; consider moving `enable_testing()` earlier. |
| **`run_assembly_design` silent failures** | `agent/run.cpp` | `doc_new` / `doc_save` failures use empty `catch (...)` and skip parts with no surfaced error. Assembly path can return `no_parts_built` without the root cause. |
| **Mission capabilities deferred** | `agent/run.cpp` `run_mission` | `simulate_fea`, `drawing`, `evaluate` recognized in planner but return "not yet wired". Intentional partial port. |
| **`paths.cpp` platform coupling** | `agent/paths.cpp` | Uses `%APPDATA%` / `USERPROFILE` only; no XDG fallback for Linux agents. Host concern for now. |
| **`libaicad.cpp` size** | `api/libaicad.cpp` | ~2800-line monolithic dispatch table. Maintainability / compile-time smell; splitting by domain is a larger refactor (out of scope). |
| **`util/expr` scientific notation** | `util/expr.cpp` | Lexer supports `e`/`E` exponents; existing `test_expr` does not cover them. Parity with Python likely OK; add harness case in Phase 2. |
| **Pre-existing branch diff in `libaicad.cpp`** | `core/api/libaicad.cpp` | Branch already had unstaged selector/session-related dispatch additions outside this review's edits; not reverted. |

---

## Tests added

| Test | File | CMakeLists |
|------|------|------------|
| Message parts tolerate `text: null` | `core/tests/test_agent.cpp` | Existing `test_agent` target — **no CMakeLists edit** |
| `err_sig` exception-line detection | `core/tests/test_machine.cpp` | **New file — register in integration phase** (`add_executable(test_machine …)` + link `aicad_agent`) |

**Phase 2:** run `test_agent`, `test_expr`, `test_prof`, and register/run `test_machine`.

---

## Files touched

| File | Change |
|------|--------|
| `core/api/libaicad.cpp` | OOM-safe `emit()`; params object validation |
| `core/agent/providers.cpp` | Null-safe text extraction in request builders |
| `core/CMakeLists.txt` | WIN32-gate `winhttp`/`bcrypt` on `aicad_agent` |
| `core/tests/test_agent.cpp` | Null `text` regression test |
| `core/tests/test_machine.cpp` | New `err_sig` unit tests |
| `docs/review/core-infra.md` | This report |

**Read-only reviewed (no edits):** `libaicad.h`, `aicad_probe.cpp`, `agent/run.cpp`, `agent/machine.cpp`, `agent/config.cpp`, `agent/state.*`, `agent/http.*`, `agent/ops_emit.*`, `agent/events.*`, `agent/paths.*`, `prof/sampler.*`, `util/expr.*`, `core/cmake/*.cmake`, `core/docs/*`, existing `test_expr.cpp`, `test_prof.cpp`.

---

## Summary counts

| Metric | Count |
|--------|------:|
| **Fixes** | 4 |
| **Flags** | 11 |
| **Tests added** | 2 (1 in existing target, 1 new file pending CMake) |
| **Files touched** | 6 |
