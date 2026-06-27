# Phase 1 Review — host-core-render

**Track:** `host-core-render`  
**Branch:** `feat/gui-rewrite`  
**Scope:** `host/src/core/**`, `host/src/render/**`, `host/src/main.cpp`, `host/src/app.cpp`, `host/src/worker_client.cpp`, `host/CMakeLists.txt`

## Fixed

1. **`addEcadTab` use-after-move** — log line referenced `t.title` after `std::move(t)` into `docTabs`; now uses `docTabs[activeTab].title`.
2. **`DocMirror::reset()` incomplete** — `reset()` now clears `ownerMap_` / reverse indices, `refDatums_`, and `lastOpenedDrawings_` so closing the last tab cannot leave stale topology ownership or datum overlays on the next document.
3. **Stale owner map on mint** — `newPart`, `newAssembly`, and `open` call `storeOwnerMap({})` and clear `refDatums_` so a freshly minted/opened doc never inherits the previous doc's feature→topology map.
4. **Stale datum overlays on tab switch** — `activate` and `activateCached` now refresh or clear `refDatums_` from the worker response (matching the `owner_map` discipline already used on switch).
5. **Stale ID-pass / overlay buffers on empty viewport** — `clearViewportForNewDoc` and the zero-body branch of `loadDocMesh` call a shared `clearPickBuffers()` helper so pick/highlight GPU state cannot outlive `hasDocMesh == false`.
6. **`IdPass` / `Overlays` GPU leaks** — destructors now delete FBO/texture/RBO/program and scratch VAO/VBO resources left alive after `destroy()` (which only drops per-body draws for reload).
7. **`AsyncRpc::shutdown` pending counter** — `pending_` is zeroed after join so `busy()` cannot stay true after teardown (relevant for headless shot gating and session restart).
8. **Stale `cpp_core.h` comment** — header comment updated to reflect that `CppCore` is the production routing seam, not diagnostics-only.

## Flagged

| Area | Severity | Notes |
|------|----------|-------|
| ID-buffer `body_id` encoding | Design limit | Packed pick id uses **8 bits** for `body_id` (`encodeId` masks `0xFF`). Assemblies with >255 bodies or body ids >255 will collide in picking/hidden-body skip. Documented with a new `test_aicm` assertion; needs contract bump to fix properly. |
| `loadAicmFile` path encoding | Portability | Uses `fopen(path)` — non-ASCII paths on Windows may fail; `_wfopen` / `std::filesystem::path` native open would be safer. |
| `kSeededGeminiKey` in source | Security | Default API key is compiled into `app_state.cpp` and written to `%APPDATA%`. Product/security decision — rotate, env-gate, or remove. |
| Python vs C++ routing seam | Architecture | `worker_client.cpp` defaults `AI_CAD_CPP_DOCS=1`, `AI_CAD_CPP_SIM=1`, agent coupled to doc engine. Large surface area; unported methods on `doc-cpp` / `ecadcpp` docs fail loud (by design). ECAD still opt-in with documented gaps (`ecad_simulate`, `ecad_design`, doc-id `ecad_autoroute`, etc.). |
| `DocMirror::resyncTree` | Minor stale risk | `doc_tree` resync refreshes owner map but not `refDatums_`; datum overlays may lag until the next full ops-response. Low impact (datums change on rebuild). |
| `AppState` size | Design smell | 1200+ line aggregate header; intentional for GUI rework but hard to test and reason about. No change in Phase 1. |
| Feature-tree file statics | Cross-panel | `doc_tabs.cpp` still calls `ui::featureTreeResetTransient()` on tab switch because rename/save-path statics are not per-doc. Flag for **host-ui** track. |
| `core/doc/picks.cpp` / selectors | Cross-track | Git shows selector/pick changes outside this track's ownership — verify parity with host `doc_resolve_pick` wire format in Phase 2. |

## Tests added

| Test | Location | CMake |
|------|----------|-------|
| `body_id` 8-bit truncation round-trip (`300 → 44`) | `host/src/render/test_aicm_cpp/test_aicm.cpp` | Already wired as `test_aicm` target — **no CMake edit** |

**Phase 2 suggestion:** run `test_aicm docs/contracts/fixtures/box.aicm` and a GUI pick smoke (`AI_CAD_SHOT=pick`) after full build.

## Files touched

- `host/src/core/doc_tabs.cpp`
- `host/src/core/doc_mirror.h`
- `host/src/core/doc_mirror.cpp`
- `host/src/core/app_state.cpp`
- `host/src/core/async_rpc.cpp`
- `host/src/core/cpp_core.h`
- `host/src/render/id_pass.cpp`
- `host/src/render/overlays.cpp`
- `host/src/render/test_aicm_cpp/test_aicm.cpp`
- `docs/review/host-core-render.md` (this file)

**Reviewed, no edits:** `host/src/main.cpp`, `host/src/app.cpp`, `host/src/worker_client.cpp`, `host/CMakeLists.txt`, `host/src/core/selection.*`, `host/src/core/jobs.*`, `host/src/core/sim_client.*`, `host/src/render/aicm_loader.*`, `host/src/render/id_pass.h`, `host/src/render/overlays.h`, `host/src/render/aicm_loader.h`

## Summary

| Metric | Count |
|--------|------:|
| Fixes | 8 |
| Flagged items | 8 |
| Tests added | 1 |
| Files touched | 10 |
