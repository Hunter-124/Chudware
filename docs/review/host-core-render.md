# Phase 1 Review — host-core-render

**Track:** `host-core-render`  
**Branch:** `feat/gui-rewrite` (review run on workspace `master`, 2026-06-27)  
**Scope:** `host/src/core/**`, `host/src/render/**`, `host/src/main.cpp`, `host/src/app.cpp`, `host/src/worker_client.cpp`, `host/CMakeLists.txt`

## Architecture (reviewed)

| Layer | Role |
|-------|------|
| **AppState** | Single aggregate for GLFW/Renderer, per-doc viewport fields, `DocMirror`, `Selection`, `WorkerClient`, `AsyncRpc`, multi-doc `DocSession` parking |
| **DocMirror** | Synchronous `doc_*` / `config_*` / export RPC facade; parses ops-responses; owner-map reverse index; tab `activate` / `activateCached` |
| **WorkerClient** | NDJSON-RPC to Python worker **or** in-process `libaicad.dll` via routing seams (`routeDocToCpp`, `routeAgentToCpp`, `routeSimToCpp`, `routeEcadToCpp`) |
| **loadDocMesh** | AICM → shaded `Renderer` + `IdPass` + `Overlays`; builds pick/BBox/normal caches; `renderOnly` for blend live preview |
| **IdPass** | R32UI (or `AI_CAD_IDBUF=rgba8`) offscreen pass; face→edge→vertex priority; hidden-body skip; section clip |
| **Selection** | Header-only pick set with observer; `doc_resolve_pick` filled in `app.cpp` on change |

### Python vs C++ routing seam (`worker_client.cpp`)

- **Docs engine default ON** (`AI_CAD_CPP_DOCS` unset → C++; `=0` forces Python fallback).
- **Per-doc commit:** `doc-cpp…` / `dwg-cpp…` ids bind all subsequent RPCs to C++ for that document’s lifetime (no mid-doc fallback).
- **Minting** (`doc_new`, `doc_open`, `import_file` without `into`): tries C++ first; structured `unsupported` falls back to Python.
- **Agent** coupled to doc engine (`cppAgentFlag()` follows `cppDocsFlag()`); v2 agent RPCs are C++-only when routed (Python worker deleted those methods).
- **Sim FEA** default ON (`AI_CAD_CPP_SIM`); **ECAD** opt-in (`AI_CAD_CPP_ECAD=1`) with documented unported methods.
- **Parity probe** (`AI_CAD_CPP_PARITY`) in `doc_mirror.cpp` is diagnostic-only; never affects production results.

## Fixed (this review pass)

| # | Issue | Fix |
|---|--------|-----|
| 9 | **Tab switch stale mesh** — `switchToTab` skipped `loadDocMesh` when `aicmPath` was empty (sketch-only / zero-body doc) but left `hasDocMesh` and the **leaving tab’s** GPU buffers active | Added `AppState::unloadDocMesh()`; `switchToTab` calls it when `aicmPath` is empty after a successful `activateCached` |
| 10 | **`DocMirror::resyncTree` datum lag** — `doc_tree` resync refreshed owner map but never `refDatums_` when the response carried `datums` | Refresh `refDatums_` from top-level or `rebuild.datums` when present (leave last value when absent) |
| 11 | **`loadAicmFile` path encoding** — `fopen(path)` fails on non-ASCII Windows paths | Read via `std::ifstream(std::filesystem::u8path(path))` |

## Fixed (prior pass, verified in tree)

1. **`addEcadTab` use-after-move** — log uses `docTabs[activeTab].title` after `std::move(t)`.
2. **`DocMirror::reset()` incomplete** — clears owner maps, `refDatums_`, `lastOpenedDrawings_`.
3. **Stale owner map on mint** — `newPart` / `newAssembly` / `open` call `storeOwnerMap({})` and clear `refDatums_`.
4. **Stale datum overlays on tab switch** — `activate` / `activateCached` refresh or clear `refDatums_`.
5. **Stale ID-pass on empty viewport** — `clearPickBuffers()` shared helper; used from `clearViewportForNewDoc` and zero-body `loadDocMesh`.
6. **`IdPass` / `Overlays` GPU leaks** — destructors delete FBO/RBO/program and scratch VAO/VBO after `destroy()`.
7. **`AsyncRpc::shutdown` pending counter** — `pending_.store(0)` after join.
8. **Stale `cpp_core.h` comment** — documents production routing seam.

## Flagged (no change)

| Area | Severity | Notes |
|------|----------|-------|
| ID-buffer `body_id` encoding | Design limit | `encodeId` masks `body_id` to **8 bits** (`0xFF`). Bodies with id >255 collide in picking and hidden-body skip. Asserted in `test_aicm`; needs AICM contract bump to widen. |
| `kSeededGeminiKey` in source | Security | Default API key compiled into `app_state.cpp` and seeded to `%APPDATA%`. Product decision — rotate, env-gate, or remove. |
| Python vs C++ routing surface | Architecture | Large opt-in/default matrix; unported methods on committed `doc-cpp` / `ecadcpp` docs fail loud by design. ECAD gaps: `ecad_simulate`, `ecad_design`, doc-id `ecad_autoroute`, etc. |
| `AppState` size | Design smell | 1200+ line header; intentional for GUI rework. |
| Feature-tree file statics | Cross-panel (**host-ui**) | `featureTreeResetTransient()` on tab switch; rename/save-path not per-doc. |
| `switchToTab` failure path | Minor | On `activateCached` failure, viewport may not refresh; mirror re-syncs `activeTab` index only. |
| Agent drain empty mesh | Minor (**app.cpp**) | Post–v2-run `doc_rebuild` path skips mesh refresh when `aicm_path` empty; same class as #9 — consider `unloadDocMesh()` there in a follow-up. |
| `core/doc/picks.cpp` selectors | Cross-track | Verify parity with host `doc_resolve_pick` wire format in Phase 2. |

## Reviewed, no edits

- `host/src/main.cpp` — crash handler, offscreen render modes, `ensurePromptsDir()` on all launch paths; sound.
- `host/src/app.cpp` — `DocMirror` listener, selection observer (`resolvePick` / assembly branch / owner-map → tree highlight), stale-resync toast, job drain; routing stays in `worker_client.cpp`.
- `host/src/worker_client.cpp` — four routing seams + `doc_list` merge; agent hard-fail vs Python fallback documented inline.
- `host/CMakeLists.txt` — `test_aicm` target wired; glad vendored; Release `/Zi` on Windows.
- `host/src/core/selection.*` — header-only model; observer single-slot is sufficient.
- `host/src/core/jobs.*`, `host/src/core/sim_client.*` — out of core/render focus; no lane bugs found.
- `host/src/render/id_pass.h`, `host/src/render/overlays.h` — contracts clear; section clip + prefer-edge pick API documented.

## Tests

| Test | Result |
|------|--------|
| `test_aicm contracts/fixtures/box.aicm` | **PASS** (20 checks incl. 8-bit `body_id` truncation) |

**Phase 2:** GUI pick smoke (`AI_CAD_SHOT=pick`), multi-doc mid-edit (`AI_CAD_SHOT=multidoc`), tab switch to empty sketch-only doc after a meshed part.

## Files touched (this pass)

- `host/src/core/app_state.h` — `unloadDocMesh()` declaration
- `host/src/core/app_state.cpp` — `unloadDocMesh()` implementation
- `host/src/core/doc_tabs.cpp` — empty-`aicmPath` branch on tab switch
- `host/src/core/doc_mirror.cpp` — `resyncTree` datum refresh
- `host/src/render/aicm_loader.cpp` — UTF-8 path open
- `docs/review/host-core-render.md` (this file)

## Summary

| Metric | Count |
|--------|------:|
| Fixes (this pass) | 3 |
| Fixes (verified prior) | 8 |
| Flagged items | 8 |
| Tests run | 1 (pass) |
| Files touched (this pass) | 6 |

**Lane health:** Core state, doc mirror, mesh load, ID-pass picking, and WorkerClient routing are coherent. The main correctness risks are contract limits (8-bit `body_id`), routing coverage gaps on opt-in ECAD, and the intentional monolithic `AppState`. Tab switching to tessellation-less documents no longer shows the previous tab’s mesh.
