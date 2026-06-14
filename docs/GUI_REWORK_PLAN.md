# GUI Rework Plan — SolidWorks-Class Professional UI

**Goal:** Reorganize the GUI to match SolidWorks' panel layout, integrate ECAD as a first-class
document type, add file explorer + copy/paste, and polish menus/ribbon for a professional feel.

**Design decisions (locked by user):**
- Left pane: **SW-faithful tabbed** (Explorer / FeatureManager / PropertyManager / Configurations)
- Cross-part copy/paste: **Bulletproof** (full external-reference/derived handling)

---

## Wave 1: Foundation — Layout + Left-Pane Tabs + Unified Dispatcher

**Goal:** Extract layout into its own module, restructure the left column from a single
FeatureManager dock into a SW-faithful tabbed panel, unify the duplicated dispatcher, and replace
magic ribbon-tab indices with named constants.

### 1A. Named ribbon-tab constants + fix PCB index bug

**Problem:** `ribbonTab` is a bare `int` compared against magic numbers scattered across the
codebase. PCB is index 7 but `app.cpp:890` sets it to 5 (lands on Sheet Metal).

**Files:**
- `host/src/ui/widgets/ribbon.cpp:40` — `kRibbonTabs[]` defines the authoritative order
- `host/src/core/app_state.h:577` — `ribbonTab` field declaration
- `host/src/app.cpp:890` — **BUG**: `st.ribbonTab = 5` should be 7 for PCB
- `host/src/app.cpp:902` — Assembly = 4 (correct)
- `host/src/core/doc_tabs.cpp:94,144,145` — tab switch sets ribbonTab by number

**Work:**
1. Add `enum RibbonTab { RT_Features=0, RT_Sketch=1, RT_Evaluate=2, RT_Simulation=3,
   RT_Assembly=4, RT_SheetMetal=5, RT_Weldments=6, RT_PCB=7 }` in `app_state.h`
2. Replace every `ribbonTab = <N>` with the named constant
3. Fix the PCB bug: `app.cpp:890` → `st.ribbonTab = RT_PCB`
4. Grep for `ribbonTab` across all `.cpp` files to catch every site

### 1B. Unify duplicated dispatcher

**Problem:** `app.cpp:642-676` and `app.cpp:678-706` are two near-identical lambdas that must stay
in sync manually — structural hazard.

**Work:**
1. Extract a single `static void dispatchRibbonCommand(AppState&, ui::PropertyManager&,
   ui::SketchMode&, ui::SimPanel&, ui::EvaluateDialogs&, ui::AsmPanels&, const char* cmd_id)`
   free function in `app.cpp` (or a new `host/src/core/dispatch.cpp`)
2. Both `setRibbonCallback` and `st.ribbonDispatch` call through it
3. Remove the duplication

### 1C. Extract layout module

**Problem:** `buildDefaultLayout()` lives in `title_bar.cpp:61-116` — it's not a title-bar concern.

**Work:**
1. Move `buildDefaultLayout()` to a new `host/src/ui/layout.cpp` / `layout.h`
2. Title bar just calls it on reset
3. The new file becomes the owner of dock-slot names and split ratios

### 1D. SW-faithful tabbed left pane

**Problem:** Currently the left column is a single "FeatureManager" dock window. SolidWorks has a
left task pane with tabs: FeatureManager / PropertyManager / ConfigurationManager / File Explorer.
Our PropertyManager already hijacks the FeatureManager dock slot via `Begin("FeatureManager")`.

**Current state:**
- `feature_tree.cpp:1177` — `drawFeatureTree()` renders into `Begin("FeatureManager")`
- PropertyManager forms call `Begin("FeatureManager")` to swap into the same slot
- `config_panel.cpp` — Configurations panel currently docks to the right task pane
- No file explorer panel exists

**Work:**
1. Change the left dock to a single `Begin("LeftPane")` window
2. Inside it, draw a vertical icon-tab strip (SW style: small icons stacked on the left edge)
   with 4 tabs: Explorer (folder icon), FeatureManager (tree icon), PropertyManager (wrench),
   Configurations (gear)
3. Each tab renders its content:
   - **Explorer** → new `file_explorer.cpp` (Wave 3, stub for now)
   - **FeatureManager** → existing `drawFeatureTree()`
   - **PropertyManager** → existing PM forms (auto-switches to this tab when a PM opens)
   - **Configurations** → existing `config_panel.cpp` content (moved from right pane)
4. When a PM form is active, the tab strip auto-selects PropertyManager
5. When the PM closes, it returns to FeatureManager
6. `layout.cpp` docks "LeftPane" instead of "FeatureManager" in `buildDefaultLayout()`
7. Update `app_state.h` — add `int leftPaneTab = 1` (0=Explorer, 1=FM, 2=PM, 3=Config)

**Key constraint:** PropertyManager currently works by calling `ImGui::Begin("FeatureManager")`.
The PM code itself doesn't need to change — the left-pane container can route to it when
`leftPaneTab == 2` or when `propertyManager->active()`.

### 1E. Headless verification

- Build: `build.bat host` must compile clean
- Headless: `AI-CAD.exe --ui-screenshot` exits 0
- Tests: `pytest engine/doc -x` all green (no engine changes in this wave)

---

## Wave 2: Document Unification — ECAD as First-Class Tabs

**Goal:** Promote ECAD documents (Schematic/PCB) from the bolted-on singleton side-channel into
proper document tabs, so opening a schematic or PCB works exactly like opening a Part or Assembly.

### 2A. Add Schematic/PCB TabKinds

**Files:**
- `host/src/core/app_state.h:314` — `DocTab::TabKind { Part, Assembly, Drawing }`
- `host/src/core/doc_tabs.cpp` — `addDocTab`, `switchToTab`, `closeDocTab`

**Work:**
1. Extend `TabKind` → `{ Part, Assembly, Drawing, Schematic, PCB }`
2. Schematic/PCB tabs carry the `ecadDocId` (from worker `ecad_*` registry) instead of a
   `DocMirror` docId. Model on the Drawing tab pattern (separate id, separate renderer).
3. `switchToTab` for Schematic/PCB: set `st.ecadDocId`, bump `st.ecadRevision`, show ECAD panel,
   set `ribbonTab = RT_PCB`, don't touch DocMirror
4. `closeDocTab` for Schematic/PCB: call `ecad_close` RPC

### 2B. Promote ECAD off the singleton

**Files:**
- `host/src/ui/ecad/ecad_panel.cpp:24-57` — file-static `EcadEditorState` singleton
- `host/src/core/app_state.h:278-299` — flat ECAD scalar fields

**Work:**
1. Move `EcadEditorState` fields that are per-document (mirror, canvas, selected, edit buffers)
   into a new struct on `DocTab` (only populated for Schematic/PCB tabs)
2. Keep global ECAD state (tool mode, AI request) on AppState
3. `ecad_panel.cpp` reads from `st.docTabs[st.activeTab].ecadState` instead of `state()`
4. This enables multiple ECAD docs open simultaneously

### 2C. New-Document chooser

**Files:**
- `host/src/ui/doc_tabs.cpp:211-222` — "+" button hardcoded to New Part (in the tab-strip
  `drawDocTabs`)

**Work:**
1. Replace the "+" button with a dropdown: New Part / New Assembly / New Drawing / New Schematic /
   New PCB
2. Each creates the appropriate worker doc and opens a tab of the matching kind
3. Keyboard shortcut: Ctrl+N opens the chooser

### 2D. Contextual ribbon on tab switch

**Work:**
1. `switchToTab` already sets `ribbonTab = RT_Assembly` for assembly docs — extend:
   - Part → RT_Features
   - Assembly → RT_Assembly
   - Drawing → RT_Evaluate (or a future Drawings tab)
   - Schematic/PCB → RT_PCB
2. The ribbon should show/hide tabs based on the active doc type (stretch goal for this wave;
   at minimum the correct tab is auto-selected)

---

## Wave 3: File Explorer Panel

**Goal:** Build the Explorer tab in the left pane — a workspace/file browser with
open/save/recent and file operations.

### 3A. File browser tree

**New file:** `host/src/ui/file_explorer.cpp` / `file_explorer.h`

**Work:**
1. Root = user's workspace directory (configurable in Settings; default = Documents/AI-CAD)
2. Tree view of directories + files, filtered to known extensions:
   `.step+` (our native), `.step`, `.iges`, `.stl`, `.3mf`, `.ecad+`, `.kicad_sch`, `.kicad_pcb`
3. Double-click opens the file (dispatches to the appropriate doc_open / ecad_open path)
4. Right-click context menu: Open, Rename, Copy, Paste, Delete, New Folder
5. Drag-and-drop files into the viewport to open them

### 3B. Save/Open integration

**Work:**
1. File > Save (Ctrl+S): if the active tab has a `path`, overwrite; else Save As
2. File > Save As (Ctrl+Shift+S): native file dialog → save
3. File > Open (Ctrl+O): native file dialog → open
4. Recent files list (persist to config.json, show in File menu + Explorer panel "Recent" section)
5. File copy/paste in Explorer: Ctrl+C on a file copies its path; Ctrl+V in a folder pastes
   (filesystem copy)

### 3C. File operations

**Work:**
1. Rename: F2 on a selected file in the Explorer tree
2. Delete: Del key → confirmation dialog → filesystem delete
3. New Folder: right-click → New Folder → inline rename
4. Copy/Paste: standard filesystem operations (copy file, paste into current folder)

---

## Wave 4: Copy/Paste — Bulletproof Cross-Part Feature Transfer

**Goal:** Full SolidWorks-grade copy/paste for features, including cross-document transfer with
external-reference handling, plus sketch-entity copy/paste.

### 4A. Engine: copy_feature / paste_feature ops

**Files:**
- `engine/doc/ops.py:282` — `_op_add_feature` (template for paste)
- `engine/doc/ops.py:146-184` — `_walk_selector_owners` / `_feature_dependents` (dependency graph)
- `engine/doc/ops.py:1280` — `sketch_move(copy=true)` (existing sketch-entity copy)

**Work:**
1. New op `copy_feature {id, include_dependencies}`:
   - Walk `_feature_dependents` to build the closure (all features this one references)
   - Serialize the closure as a portable JSON bundle (features + their sketches + refs)
   - Remap all internal ids to placeholder `$0`, `$1`, ... for portability
   - Return the bundle (stored on the host clipboard)

2. New op `paste_feature {bundle, target_doc_id}`:
   - Accept a bundle from `copy_feature`
   - If same doc: re-id all features (fresh `idgen.next("f")`) and append
   - If cross-doc: re-id + resolve external refs:
     - **Dangling refs** (ref to a face/edge in the source doc): mark as "external reference" with
       source doc name + feature id, or downgrade to a bare geometric selector if the topology
       exists in the target
     - **Sketch refs**: deep-copy the sketch entities, re-id them
     - **Datum refs**: if the target has the same datum plane (Top/Front/Right), reuse it;
       otherwise create a derived datum
   - Insert via the same `_op_add_feature` path (gets rollback-bar insertion for free)

3. New op `paste_feature_as_derived {bundle, source_doc_id}`:
   - Creates an "external reference" feature that links back to the source doc
   - Updates when the source feature changes (SolidWorks derived-part behavior)

### 4B. Engine: sketch_paste_entities

**Work:**
1. New op `sketch_paste_entities {sketch, entities_bundle, offset_x, offset_y}`:
   - Deep-copy entity geometry with fresh ids
   - Shift by (offset_x, offset_y) from the copy origin
   - Drop constraints that reference entities outside the pasted set
   - Keep internal constraints (e.g. two pasted lines that were coincident stay coincident)

### 4C. Host: clipboard infrastructure

**Files:**
- `feature_tree.cpp:513-601` — context menu (no Copy/Paste currently)
- `feature_tree.cpp:1623` — fillet↔chamfer convert (template: read JSON → `add_feature`)
- `feature_tree.cpp:192` — `s_multiSel` (multi-selection state)

**Work:**
1. Add `AppState::clipboard` — a JSON object holding the last copied feature/entity bundle
2. FeatureManager context menu gains: Copy (Ctrl+C), Cut (Ctrl+X), Paste (Ctrl+V)
   - Copy: call `copy_feature` op, store result in clipboard
   - Cut: Copy + Delete
   - Paste: call `paste_feature` with clipboard bundle
   - Multi-select: copy all selected features (closure includes all their deps)
3. Sketch mode gains Ctrl+C/V for sketch entities:
   - Copy: serialize selected entities to clipboard
   - Paste: enter a "paste placement" mode (entity outlines follow cursor, click to place)
4. Cross-document paste: when pasting into a different doc tab, use the cross-doc path
   with external-reference resolution

### 4D. Host: keyboard shortcut routing

**Work:**
1. Global Ctrl+C/X/V handler in `app.cpp`'s key callback:
   - If sketch mode active → route to sketch copy/paste
   - If FeatureManager focused + feature selected → route to feature copy/paste
   - If Explorer tab active → route to file copy/paste
2. Ctrl+Z/Y already exists for undo/redo — no conflict

---

## Wave 5: Polish — Menus, Ribbon, Theme

**Goal:** Reorganize the menu bar to SW style, clean up ribbon groups/labels, and do a
theme/spacing pass for professional appearance.

### 5A. Menu bar reorganization

**Files:**
- `host/src/ui/title_bar.cpp:168-216` — current menus (AI-CAD / View / Help)

**Work:** Replace the 3-menu bar with SolidWorks' standard 7-menu bar:

| Menu | Contents |
|------|----------|
| **File** | New (Part/Asm/Drawing/Sch/PCB), Open, Save, Save As, Save All, Export (STEP/STL/3MF/DXF), Recent Files, Close, Quit |
| **Edit** | Undo, Redo, Cut, Copy, Paste, Delete, Select All |
| **View** | Ribbon, FeatureManager, Configurations, Design Table, Cut List, AI Panel, Viewport, Theme, Reset Layout |
| **Insert** | Feature (Extrude/Revolve/Sweep/Loft/...), Sketch, Reference Geometry, Pattern/Mirror, Hole Wizard, Assembly (Insert Component/Mate) |
| **Tools** | Evaluate (Mass/Measure/Section/...), Simulation, Sheet Metal tools, Weldment tools, ECAD tools, Equation-Driven tools |
| **Window** | Tile Horizontal, Tile Vertical, Cascade, Close All (document management) |
| **Help** | About AI-CAD, Documentation, Keyboard Shortcuts |

### 5B. Ribbon group/label cleanup

**Files:**
- `host/src/ui/ribbon_def.cpp:12+` — `kCmds[]` table

**Work:**
1. Audit every group label for consistency (capitalize, abbreviate consistently)
2. Ensure icons exist for every command (placeholder = generic feature icon)
3. Add missing group separators where SolidWorks has them
4. Review big vs. small button assignments against SW's layout

### 5C. Theme/spacing pass

**Work:**
1. Audit padding/margins in all panels for consistency
2. Ensure all panels respect `ui::uiScale` for DPI scaling
3. Review font sizes: tree, ribbon labels, status bar, tooltips
4. Check light theme for contrast issues (SW's default is light)
5. Status bar: add document units indicator (already exists), view style, selection info

---

## Execution Notes for Dispatch Agent

### Build & test cadence
Each wave: build (`build.bat host`) → headless verify (`AI-CAD.exe --ui-screenshot`) → engine
tests (`pytest engine/doc -x`) → commit on master → hand user a manual checklist.

### File ownership (parallel-safe)
- **Append-only shared files** (multiple agents may touch): `ribbon_def.cpp kCmds[]`,
  `widgets/ribbon.cpp kRibbonTabs[]`, `app_state.h`, `worker.py _METHODS`,
  `features/__init__.py`
- **Single-owner files** (one agent at a time): `app.cpp`, `feature_tree.cpp`, `ecad_panel.cpp`,
  `doc_tabs.cpp`, `title_bar.cpp`

### Subagent instructions (CRITICAL)
- **Do NOT run git** — the dispatch agent handles all commits
- **Do NOT modify files outside your assigned scope**
- **Test your changes**: `build.bat host` must compile; `pytest engine/doc -x` must pass
- **Use `-X utf8`** when running pytest on Windows
- **ribbon has TWO sources**: `ribbon_def.cpp kCmds[]` (commands) AND `widgets/ribbon.cpp
  kRibbonTabs[]` (tab strip) — a new tab needs BOTH

### Handoff notes
Between waves, agents should write a brief handoff note to
`docs/gui_rework_handoff.md` recording:
1. What was done (files changed, functions added/modified)
2. What was deferred or partially done
3. Any gotchas the next wave needs to know
4. Build/test status at handoff time

### Parallelism opportunities
- **Wave 1:** 1A (constants) and 1B (dispatcher) and 1C (layout extraction) can run in parallel
  (different files). 1D (tabbed pane) depends on 1C.
- **Wave 2:** 2A (TabKind) must come first; 2B (singleton removal) and 2C (chooser) can parallel
  after 2A; 2D depends on 2A.
- **Wave 3:** Fully independent of Wave 2 engine work; can overlap with Wave 2 host work if
  file ownership is respected.
- **Wave 4:** 4A (engine ops) and 4C (host clipboard UI) can parallel. 4B depends on 4A.
  4D depends on 4C.
- **Wave 5:** Independent of all others; can start as soon as Wave 1 menus are stable.

### Key patterns to follow
- **fillet↔chamfer convert** (`feature_tree.cpp:1623`): read feature JSON → delete + add_feature.
  This is the exact template for feature copy/paste.
- **Drawing tab** (`doc_tabs.cpp:88-98`, `app_state.h:325-363`): separate doc id, separate
  renderer, no DocMirror touch. This is the template for ECAD tabs.
- **PM form swapping** (`property_manager.h`): PM calls `Begin("FeatureManager")` to hijack the
  dock. The new tabbed pane replaces this with tab selection.
- **`ecadDispatch`** (`app.cpp:391-419`): route ECAD commands. Template for any new command domain.
