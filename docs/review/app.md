# App Track Review — Phase 1

**Branch:** `feat/gui-rewrite`  
**Scope:** `app/**` (GUI shell, engine wrapper, command registry/palette, viewport)  
**Date:** 2026-06-27

## Summary

Reviewed 27 source files under `app/src/`, `app/CMakeLists.txt`, `app/DESIGN.md`, and `app/resources/`. Focus areas: shell layout, `eng::Engine` save/open bridge, command registry + ⌘K palette, thin-wrapper correctness. Eight safe fixes applied; sixteen items flagged for later phases or other tracks.

| Metric | Count |
|--------|------:|
| Fixes | 8 |
| Flagged | 16 |
| Files touched (code) | 6 |
| New test files | 1 |

---

## Fixed

1. **Command palette empty-results UB** (`command_palette.cpp`) — When fuzzy search returned zero hits, `paletteSel` was clamped with `(int)hits.size() - 1`, underflowing `size_t` and producing a garbage selection index. Guarded with `nHits == 0` branch before keyboard navigation.

2. **Engine `open()` error propagation** (`engine.cpp`) — `doc_open` always returned `true` after calling `refreshTree()` / `rebuild()` even when those post-open calls failed, leaving a loaded doc with no mesh and no error surfaced to the UI. Now returns `false` and sets `lastErr_` on post-open failure.

3. **Engine `undo()` / `redo()` empty-doc guard** (`engine.cpp`) — Called `doc_undo` / `doc_redo` with an empty `doc_id` when no document was loaded. Now returns a local `"no document"` error without hitting the bridge.

4. **Hole feature wire format** (`engine.cpp`) — `addHoleCenter` sent a raw JSON number for `diameter` while all other builders use `dim()`. Aligned with the engine contract (`{"value":…,"expr":null}`) and added `"standard":"custom"` per agent cheatsheet / host property-manager expectations.

5. **Undo/redo shortcut guard** (`application.cpp`) — Ctrl+Z / Ctrl+Y fired against the engine even when `hasDoc()` was false (demo-offline edge case). Gated on `engine.hasDoc()`.

6. **`AppState::doc()` empty-vector UB** (`app_state.h`) — `docs[0]` was reachable when `docs` was empty. Returns a static fallback `DocTabState` instead.

7. **Stale `dispatch` location comment** (`app_state.h`) — Comment said `application.cpp`; implementation lives in `app_state.cpp`.

8. **Stale viewport panel comment** (`panels.h`) — Comment still said “placeholder gradient”; viewport now renders FBO mesh via `viewport::Scene`.

---

## Flagged

### Shell / UX (do not fix in Phase 1)

1. **Edit menu undo/redo/cut/copy/paste are inert** (`title_bar.cpp`) — Menu items render but do not call `engine.undo()` / `dispatch` / clipboard. Keyboard undo works; menu wiring is incomplete.

2. **`dispatch()` is mostly a stub** (`app_state.cpp`) — ~170 registry commands set status text or open the Inspector; only primitives + a handful of modify tools hit the engine. Expected for vertical-slice phase but means palette/toolbar dispatch is UI-only for most cmds.

3. **Doc tab switching does not swap per-doc engine state** (`title_bar.cpp`, `app_state.h`) — Multiple tabs exist in UI but share one `eng::Engine` instance and one `savePath`. Multi-doc needs core session mirroring (cross-track: `host/` bridge).

4. **No layout persistence** (`application.cpp`) — `io.IniFilename = nullptr`; DockBuilder layout is rebuilt once per run. DESIGN.md calls for per-user layout persistence.

5. **File dialogs use `GetActiveWindow()`** (`file_dialog.cpp`) — Borderless GLFW window may not be the Win32 active window; dialogs can appear behind the app or without a proper owner. Needs `glfwGetWin32Window` shim (DESIGN.md §10).

6. **Linux/macOS file dialogs are no-ops** (`file_dialog.cpp`) — `openCadFile` / `saveCadFile` return `{}` on non-Windows. Save/Open menu items silently cancel.

### Engine wrapper

7. **`Engine::newPart` ignores `name`** (`engine.cpp`) — Parameter is `(void)name`; `doc_new` only passes `kind: part`. Doc tab title is managed separately in UI.

8. **Undo/redo may not refresh viewport mesh** (`engine.cpp`, `viewport.cpp`) — `ingest()` updates `aicmPath` only when the RPC returns `tess`. If undo/redo responses omit tess, `viewMeshRev` may not trigger a reload. Needs core-track verification of `doc_undo` / `doc_rebuild` payloads.

9. **Demo agent bolt_circle params use raw numbers** (`app_state.cpp`) — Same raw-vs-`dim()` inconsistency as the old hole helper; works today but diverges from wire convention.

10. **`Engine::open` replaces in-memory doc without closing prior** (`engine.cpp`) — Opening a file while a doc is loaded adopts the new `doc_id` without explicit `doc_close`. May leak server-side sessions (core track).

### Command registry / palette

11. **Registry query helpers are O(N) per call** (`command_registry.cpp`) — Acceptable at N≈170 but `groupsFor` / `topCommands` run every toolbar frame. Not a bug; note for perf phase.

12. **Palette search is substring-only** (`command_palette.cpp`) — No fuzzy/token scoring beyond label/id/keywords substring match. DESIGN.md implies fuzzy search; current impl is basic filter + sort by match position.

13. **Variant commands reachable in palette but not toolbar** — By design (`Kind::Variant`), but Extrude Cut etc. dispatch without setting `toolExtrudeOp`; Inspector op selector is not synced from palette picks.

### Viewport / rendering

14. **`Scene::render` calls `glGetUniformLocation` every draw** (`scene.cpp`) — Uniform location lookup per body per frame. Perf smell; cache locations in `init` or first use.

### Icons / assets

15. **Most CAD icons are monogram placeholders** (`icons.h`, `resources/icons/`) — By design per DESIGN.md §8; real SVG assets exist for a subset but are not loaded yet.

### Build / deps

16. **App configure requires pre-built `host/build/_deps`** (`CMakeLists.txt`) — Documented FATAL_ERROR if host FetchContent deps missing. Not a code bug.

---

## Tests added

| File | Purpose | CMake |
|------|---------|-------|
| `app/test/test_command_registry.cpp` | Smoke tests: `find`, `workspacesFor`, `palette` dedup, `flyoutChildren`, `groupsFor` | **Not wired** — add a small test executable in `app/CMakeLists.txt` during Phase 2 |

Existing headless coverage: `main.cpp --selftest` exercises engine save/open round-trip when `libaicad.dll` is available.

---

## Files touched

| File | Change |
|------|--------|
| `app/src/ui/command_palette.cpp` | Fix empty-results selection underflow |
| `app/src/engine/engine.cpp` | open() errors, undo/redo guard, hole dim format |
| `app/src/app/app_state.h` | Safe `doc()`, comment fix |
| `app/src/app/application.cpp` | Undo/redo `hasDoc()` gate |
| `app/src/ui/panels.h` | Stale comment |
| `app/test/test_command_registry.cpp` | **New** (not in build) |
| `docs/review/app.md` | **New** — this report |

---

## Cross-track notes

- **Core / `libaicad`:** `doc_open` post-steps (`doc_tree`, `doc_rebuild`) and undo/redo tess payloads should be verified by the core track; app wrapper now surfaces failures correctly.
- **Host bridge:** Multi-doc, agent `events.jsonl` stream, and re-hosted property editors remain in `host/` per DESIGN.md §9.
- **No changes** to `docs/contracts/*.md`, CI, or CMake build graph (per track guardrails).
