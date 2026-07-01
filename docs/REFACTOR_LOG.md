# Refactor Log

Running handoff log for the CAD modular refactor + GUI abstraction. Each node
appends a one-paragraph handoff (files touched, deferred, gotchas). See
`AGENTS.md` and the plan file for context.

## P0 — foundation

- P0 started N0a–N0d (clean start, AGENTS.md, promote contracts, Linux gate).

### N0a — clean starting point
Committed the pre-existing dirty `host/src/ui/*` + `ui_theme.h` diff (opaque
doc-tab band, opaque glass-pane base, relocated context meter) as a WIP commit
so the swarm starts from a clean tree. `git status` clean after commit.

### N0b — AGENTS.md + REFACTOR_LOG scaffold
Added top-level `AGENTS.md` (project blurb, repo map of current on-disk dirs
with plan-target annotations, swarm operating rules copied from the plan,
build/test cadence, pointers to plan + contracts + ARCHITECTURE.md +
CPP_MIGRATION.md) and this `docs/REFACTOR_LOG.md` scaffold.

**Pre-existing gate failure (recorded, not blocking):** `scripts/check.py` is
RED on this box for reasons unrelated to P0 — `feature_surface_audit` reports
~56 missing handlers for the `dwg_*` (Drafting) and `cam_*` (Manufacture)
ribbon commands in `host/src/ui/ribbon_def.cpp`, and `parity_audit` reports 12
SW_PARITY_SPEC done/beyond rows naming absent compilers (assembly, config,
csys, boolean, sheet-metal, weldments). N0a did not touch any audited file, so
this is pre-existing drift, not introduced by the refactor. Per the plan,
`scripts/gate.sh` (N0d) records this as a non-fatal `check.py` finding and
still exits 0 so the swarm can proceed; the underlying ribbon/parity gaps are
real engine work for later phases, not P0 foundation.

### N0c — promote contracts
`git mv docs/contracts contracts` (history preserved). Updated `docs/contracts`
path references → `contracts/` in `docs/**/*.md`, the moved `contracts/**/*.md`
internal cross-refs, and root config (`README.md`, `ARCHITECTURE.md`,
`requirements.txt`). Added `contracts/README.md` listing the frozen seams
(`worker_rpc.md`, `doc_ops.md`, `doc_schema.md`, `selectors.md`,
`mesh_format.md`, `stepplus.md`, `events.md`, `feature_params.md`) plus the
`ecad/`, `fixtures/`, `icons.md`, `licenses.md` subdirs, with the append-only /
integrator-sign-off policy.

**Deferred (per P0 hard constraint — no source edits under
`core/`/`host/`/`app/`/`engine/`/`agent/`/`mcp_server/`):** the `docs/contracts`
path references that remain in code comments / one string literal inside those
trees (e.g. `host/src/ui/viewport_panel.cpp`, `host/src/render/test_aicm_cpp/test_aicm.cpp`
default `"docs/contracts/fixtures/box.aicm"`, `core/CMakeLists.txt`,
`core/doc/*.h`, `engine/**/*.py`, `host/assets/make_icons.py`) were intentionally
left stale. They are non-functional comments or a test fallback default not run
by the gate, and those trees move/reorganize in later phases; the path fixups
land with the per-subsystem moves. `AGENTS.md`'s "promoted from `docs/contracts/`"
line was preserved as historical prose. `scripts/parity_audit.py` references
`docs/SW_PARITY_SPEC.md` (not `docs/contracts`), so no script change was needed.

### N0d — Linux gate + CMake presets
Added `scripts/gate.sh` (executable, `set -euo pipefail`): runs `scripts/check.py`
(recorded; pre-existing red is non-fatal via `CHECK_PY_FATAL=0` until the
ribbon/parity gaps close), then the core CMake/Ninja build with
`-DAICAD_WITH_OCCT=ON` + `ctest` (SKIP if system OCCT cmake config not found),
then engine+agent pytest (SKIP if no venv/pytest). Prints a PASS/SKIP summary
and exits 0 on environment SKIPs. Added `CMakePresets.json` (`linux-core`,
`linux-app`, Ninja) plus a minimal root `CMakeLists.txt` dispatcher so the
presets can wrap the existing `core/` and `app/` builds (the real layered
top-level CMake lands in N1a; the gate does NOT use presets — it drives
`cmake -S core` directly). Added `/build/` to `.gitignore` for preset build dirs.

**Gate result on this box:** `bash scripts/gate.sh` exits 0 — `check.py`
FAIL-RECORDED (pre-existing), core build PASS (system OCCT 7.9.1 at
`/usr/lib64/cmake/opencascade`), ctest PASS (41/41), pytest SKIP (no runtime
venv / pytest installed).

**GUI cleanup — Feature Manager + Copilot (both shells):** Reworked the Copilot pane in `host/src/ui/legacy_chat.cpp` with a **Sessions** toggle (replaces New chat/Chats), per-session state (`Conversation.lastContextTokens`, `queuedPrompts`), a top bar for agent mode / permissions (offline + supervised + thoughts) / model-specific thinking effort, a single canonical model picker in the composer (footer provider chip removed), and `defaultContextWindowForModel()` for the context meter. Added `glassRowBackground()` in `host/src/ui_theme.*`, increased panel transparency (glass alpha 0.38), nested default planes under **Origin** in `host/src/ui/feature_tree.cpp`, and unified datum-plane icon mapping to `ref_plane` (feature tree, ribbon, PM forms, `surf_planar`). Mirrored the glass + Sessions UX in `app/src/ui/agent_panel.cpp`, `app/src/theme/theme.*`, and `app/src/ui/left_dock.cpp`. Regenerated procedural icon atlas via `make_icons.py` (refined `origin` + `ref_plane` glyphs; atlas PNGs updated). **Deferred:** true multi-worker concurrent agent runs (UI shows per-session running state but one worker job), Windows visual verify, full AI PNG icon set (`pack_icons.py` still 4/189 sources). **Gotcha:** `scripts/check.py` may FAIL-RECORD pre-existing ribbon/parity audits.

**FM + Copilot GUI rectification (integrator wave):** Completed row-only transparency across host + `ui/imgui/`: removed full-panel `windowBg`+`glassBackground` washes from `layout.cpp` and Copilot (`legacy_chat.cpp`); added `glassRowAtCursor()` / `fmRowPill()` row pills on FM toolbar, tree chrome (Origin/planes/bodies/assembly), Explorer file rows, left-pane title + tab rail, and Copilot composer prelude/toolbar rows. Dock overlap fix: transparent `DockingEmptyBg` in `app.cpp`. Agent 2 delivered composer prelude (Design|Permissions + context meter) and toolbar (model|thinking|gear|send) in host + `agent_panel.cpp`. Agent 3 refreshed 68 FM/ribbon glyphs via `FM_REFRESH_MANIFEST` in `make_icons.py`, regenerated atlas PNGs/JSON, deduped `featureIcon` datum aliases, and nested Origin→planes in `app_state.cpp`. Added `docs/GUI_FM_COPILOT_OBJECTIVES.md` (acceptance criteria A–E). **Gate:** `bash scripts/gate.sh` PASS (check.py FAIL-RECORDED pre-existing). **Build:** `cmake -S host -B build/host && cmake --build build/host` PASS → `build/host/bin/AI-CAD`. **Deferred:** manual visual screenshot checklist, AI Settings pane full-panel wash (sibling tab; not in scope A).

**FM + Copilot follow-up (viewport HUD + readability):** Replaced the viewport heads-up toolbar's single `glassBackground` bar with per-button `glassRowBackground` pills, moved it to the top-right, shrunk icons (22dp), and added idle auto-fade (ramps on hover / zoom-area / open popups) in `host/src/ui/viewport_panel.cpp`. Boosted row readability: `glassRowBackground` default wash 0.28→0.42 with a faint border, brighter dark-theme `textDim`/`proseText`/`assistantName`, and assistant chat body copy now sits on a frosted row pill in `legacy_chat.cpp`. Mirrored wash/border in `app/src/theme/theme.*` + `ui/imgui/src/theme/theme.*`. **Build:** host `AI-CAD` PASS. **Check:** `scripts/check.py gui_lint theme` PASS. **Deferred:** manual screenshot verify of fade + chat contrast on bright viewport bleed.

**FM glass regression fix (undo/redo + rail + tree pills):** Root cause of broken undo/redo: `glassRowAtCursor()` before the FM toolbar is draw-only and never advanced the layout cursor, so the full-width `##fm_filter` InputText was placed at the same Y and stole hit-tests from the toolbar (undo/redo last). Fixed in `host/src/ui/feature_tree.cpp` by reserving the toolbar row with explicit screen-space button placement + `Dummy` advance; filter row gets the same reserved-height pattern. `fmRowPill()` now uses `GetFrameHeightWithSpacing()` + explicit `glassRowBackground()` wash (0.26 idle / 0.42 selected) so tree icon rows read consistently. Toolbar icon buttons always show a subtle pill (stronger on hover). Left tab rail (`layout.cpp`) and `ui/imgui` activity rail now draw persistent frosted pills on every icon (0.20 idle, 0.32 hover, 0.40 selected). **Build:** `cmake --build build/host` PASS. **Check:** `gui_lint --rule theme` PASS.

### G0 — GUI unification integrator prep
Local baseline commit before Wave 1: added `ui/imgui/build/` to `.gitignore`; committed untracked `ui/viewport/**` scaffold (`renderer.h`, `mesh.*`, `camera_anim.*`, `render/{aicm_loader,id_pass,overlays}.*`, `vec_math.h`) plus MSVC 14.51 `/FI extern/fix_math.h` fix in `host/CMakeLists.txt` and incidental tools/docs path edits. Wrote monolithic swarm spec `docs/GUI_MIGRATION_DAG.md` (all nodes G0→X1 with write-sets, sources, verify commands) and `docs/GLASS_DESIGN_SPEC.md` (4 material levels, 8pt grid, `glassPanel`/`adaptiveSurface`, per-panel rules, P1 acceptance criteria). Captured 21/21 host `AI_CAD_SHOT` baselines into `scratch/gui_baseline/*.png` via `host/build-linux/bin/AI-CAD` (`flow` needs ~240s timeout for CFD frames; all others <120s). **Deferred:** Wave 1 leaves (F1–F4), `ui/viewport/renderer.cpp` + CMakeLists, Kawase blur. **Gotcha:** `ui/viewport/render/id_pass.*` and `overlays.*` still `#include` host `renderer.h` — F1 must break this first; `ui/imgui` still leeches `host/build-linux/_deps`.
