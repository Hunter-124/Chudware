# App Track Review — Phase 1

**Branch:** `feat/gui-rewrite`  
**Scope:** `app/**` (GUI shell, engine wrapper, command registry/palette, viewport)  
**Date:** 2026-06-27

## Summary

Reviewed 27 source files under `app/src/`, `app/CMakeLists.txt`, `app/DESIGN.md`, and `app/resources/`. Focus: shell layout (title bar, workspace bar, docking, activity rail), `eng::Engine` save/open bridge, command registry + ⌘K palette, thin-wrapper correctness vs `host/` bridge. **Fifteen safe fixes applied**; **sixteen items flagged** for later phases or other tracks.

| Metric | Count |
|--------|------:|
| Fixes applied | 15 |
| Flagged (deferred) | 16 |
| Files touched (code) | 10 |
| Tests wired | 1 |

**Verification:** `AI-CAD2.exe --selftest` → OK (save/open round-trip, 3 features). `test_command_registry.exe` → pass.

---

## Architecture (as built)

```
main.cpp → Application::frame()
  ├─ ##root (frameless chrome)
  │    ├─ title_bar      (menu, doc tabs, search pill, win buttons)
  │    ├─ workspace_bar  (doc-type workspaces / sketch chip)
  │    ├─ context_toolbar (verb groups from cmd::registry)
  │    ├─ activity rail  (Tree / Bodies / Files / Configs)
  │    ├─ DockSpace      (Browser | Viewport | Assistant)
  │    └─ status_bar
  └─ overlays: command_palette (Ctrl/⌘+K)
```

- **Engine:** `eng::Engine` wraps `core::cpp_core()` flat ABI (`doc_new`, `doc_save`, `doc_open`, `doc_apply_ops`, `doc_undo/redo`, `doc_rebuild`, `doc_resolve_pick`). Split across `engine.cpp`, `engine_pick.cpp`, `engine_sketch.cpp`.
- **Commands:** Single static table in `command_registry.cpp` (~170 cmds). Toolbar shows `Kind::Top`; palette dedupes all kinds including `Variant`/`Flyout`.
- **Viewport:** `viewport::Scene` FBO + reused `host/render/aicm_loader.cpp`. Mesh reload keyed off `viewMeshRev` vs `engine.revision()`.

---

## Fixed

### Engine wrapper (`engine.cpp`)

1. **`open()` error propagation** — Post-open `refreshTree()` / `rebuild()` failures now return `false` and set `lastErr_` instead of silently succeeding with a stale/missing mesh.

2. **`undo()` / `redo()` empty-doc guard** — Returns `"no document"` locally without calling the bridge when `docId_` is empty.

3. **Hole feature wire format** — `addHoleCenter` uses `dim(diameter)` and `"standard":"custom"` to match engine contract (was raw number).

### Shell / state (`app_state.h`, `app_state.cpp`, `application.cpp`, `title_bar.cpp`)

4. **`AppState::doc()` empty-vector UB** — Returns a static fallback when `docs` is empty instead of indexing `[0]`.

5. **Undo/redo shortcut guard** — Ctrl+Z / Ctrl+Y gated on `engine.hasDoc()` (demo-offline edge case).

6. **Viewport mesh staleness** — `buildTreeFromDoc()` sets `viewMeshRev = -1` whenever `engineReady`, so Inspector commits, undo/redo, agent demo ops, and file open/new all trigger a tess reload on the next frame (previously only explicit open/new paths invalidated).

7. **`inSketch` not cleared on file lifecycle** — `doNewPart()` and `doOpen()` now reset `inSketch = false` so sketch toolbar cannot persist after loading a part file.

8. **Doc tab switch context** — Clicking a tab resets `inSketch`, sets `workspace` to the doc type's primary workspace (`workspacesFor(type)[0]`).

9. **Stale comments** — `dispatch` location comment corrected (`app_state.cpp`); viewport panel comment updated (`panels.h`).

### Command palette / registry (`command_palette.cpp`, `app_state.cpp`, `left_dock.cpp`)

10. **Palette empty-results UB** — Zero hits no longer underflow `paletteSel` via `(int)hits.size() - 1`.

11. **Palette state on open** — Ctrl/⌘+K and window appear reset `paletteQuery` and `paletteSel` for a fresh search session.

12. **Variant commands dispatch** — Palette picks like `extrude_cut` now open the parent tool's Inspector (`extrude_boss`) with `toolExtrudeOp` set; previously fell through to a no-op status string because variants were absent from `opensInspector()`.

13. **Inspector op sync** — Extrude Inspector reads `toolExtrudeOp` into the Operation combo when opened from a variant/palette pick.

### Build / tests (`CMakeLists.txt`, `test/test_command_registry.cpp`)

14. **Registry test wired** — `test_command_registry` executable added (links `command_registry.cpp`; no GL/engine deps).

15. **Comment fix in test file** — Updated to reflect CMake wiring.

---

## Flagged (deferred)

### Shell / UX

1. **Edit menu undo/redo/cut/copy/paste inert** (`title_bar.cpp`) — Keyboard undo works; menu items are placeholders.

2. **`dispatch()` mostly stub** (`app_state.cpp`) — ~170 cmds set status or open Inspector; only primitives, extrude-sketch, shell/fillet/chamfer-with-pick, and demo agent hit the engine. Expected for vertical-slice phase.

3. **Multi-doc tabs share one engine** (`title_bar.cpp`, `app_state.h`) — Tabs are UI-only; one `eng::Engine`, one `savePath`. Needs core session mirroring (cross-track: `host/`).

4. **No layout persistence** (`application.cpp`, `main.cpp`) — `io.IniFilename = nullptr`; DockBuilder layout rebuilt once per run.

5. **File dialogs use `GetActiveWindow()`** (`file_dialog.cpp`) — Borderless GLFW window may not be Win32 active window; dialogs can appear behind the app. Needs `glfwGetWin32Window` owner shim (DESIGN.md §10).

6. **Linux/macOS file dialogs no-op** (`file_dialog.cpp`) — Save/Open silently cancel on non-Windows.

### Engine wrapper

7. **`Engine::newPart` ignores `name`** — `doc_new` only passes `kind: part`; tab title managed in UI.

8. **`Engine::open` replaces doc without `doc_close`** — Prior server session may leak (core track).

9. **Extrude Intersect mode** (`left_dock.cpp`) — Combo offers Intersect but wire still sends `mode: "boss"` for all non-Cut ops.

10. **Demo agent `bolt_circle` uses raw numbers** (`app_state.cpp`) — Sketch entities expect raw coords (correct per cheatsheet); feature params could use `dim()` for consistency.

### Command registry / palette

11. **Registry queries O(N) per frame** — Fine at N≈170; cache in perf phase if needed.

12. **Palette search is substring-only** — No token/fuzzy scoring beyond label/id/keywords position sort. DESIGN.md implies richer fuzzy search.

13. **Revolve/Sweep/Loft variants** — Parent Inspector op selector not yet implemented (Extrude only).

### Viewport / rendering

14. **`Scene::render` uniform lookup per draw** (`scene.cpp`) — Cache `glGetUniformLocation` in init.

### Icons / assets

15. **Monogram placeholder icons** — By design per DESIGN.md §8; SVG subset exists but atlas still uses monograms.

### Build / deps

16. **Configure requires pre-built `host/build/_deps`** (`CMakeLists.txt`) — FATAL_ERROR if host FetchContent deps missing. Documented.

---

## Tests

| Target | Command | Result |
|--------|---------|--------|
| Engine save/open | `AI-CAD2.exe --selftest` | OK |
| Pick + shell | `AI-CAD2.exe --shelltest` | (requires scene; run in CI with DLL) |
| Pick + fillet | `AI-CAD2.exe --edgetest` | (same) |
| Sketch extrude | `AI-CAD2.exe --extrudetest` | (same) |
| Registry smoke | `test_command_registry.exe` | pass |

Headless modes in `main.cpp`: `--selftest`, `--picktest`, `--shelltest`, `--edgetest`, `--extrudetest`, `--shot`.

---

## Files touched

| File | Change |
|------|--------|
| `app/src/engine/engine.cpp` | open() errors, undo/redo guard, hole dim format |
| `app/src/app/app_state.h` | Safe `doc()`, comment fix |
| `app/src/app/app_state.cpp` | viewMeshRev via buildTreeFromDoc, variant dispatch, inSketch reset |
| `app/src/app/application.cpp` | Undo/redo hasDoc gate, palette reset on ⌘K |
| `app/src/ui/command_palette.cpp` | Empty-results guard, reset on appear |
| `app/src/ui/left_dock.cpp` | Extrude op sync from toolExtrudeOp |
| `app/src/ui/title_bar.cpp` | Doc tab → workspace/sketch sync |
| `app/src/ui/panels.h` | Stale comment |
| `app/CMakeLists.txt` | `test_command_registry` target |
| `app/test/test_command_registry.cpp` | Registry smoke tests |
| `docs/review/app.md` | This report |

---

## Cross-track notes

- **Core / `libaicad`:** Verify `doc_undo`/`doc_redo` always return `tess.aicm_path` (they call `warm_and_respond(..., with_tess=true)` — app now reloads mesh on any tree rebuild). `doc_close` on open/replace is a core hygiene item.
- **Host bridge:** Multi-doc, agent `events.jsonl`, property editors remain in `host/` per DESIGN.md §9.
- **DESIGN.md alignment:** Shell IA (verb toolbar, workspaces, palette, agent timeline, frameless chrome) matches spec phases 1–4. Missing: layout persistence, nfd dialogs, real icon SVG atlas, full dispatch wiring.
