# GUI Overhaul Requirement Register (Hunter, 2026-07-06)

Canonical, durable register of every GUI requirement from the 2026-07-06 direction reset.
The host/ GUI (`host/build-linux/bin/AI-CAD`) is THE product GUI; ui/imgui (AI-CAD2) is deleted
(port ledger: `docs/PORT_LEDGER.md`). **Nothing on this list may be silently dropped** — items
not implemented tonight stay checked-off here and on the work board until done.

Status legend: `[ ]` pending · `[~]` in progress (fleet wf_b556d138-1bf) · `[x]` done+verified.

## 1. Top chrome (title bar)
- [~] Remove the Edit, View, Insert, Tools, Window, Help menus.
- [~] Left group, visually separated (non-design controls), restyled as chips matching the tabs:
  **File** (exact current menu behavior), **Search**, **Settings**.
- [~] Workspace tabs centered in the title bar with the OS window controls:
  **Model, Drawing, Simulate, Electronics, Manufacture, PLM, Parts Library, AI**.
- [~] Each tab replaces the viewport (and toolbar where sensible); views stay attached to the
  main window (no floating top-level windows for tabs).
- [~] Title-bar drag hit-testing stays correct on Windows AND Linux.

## 2. Dialogs
- [~] Settings: title strip ("Settings" + X) fully OPAQUE in light AND dark mode.
- [~] Search: same function as today, but in a settings-style bordered popout (title strip + X).

## 3. Ribbon / toolbars
- [~] Per-tab tool filtering — only tools a user would need in that tab (strict isolation).
  - Manufacture: manufacturing tools + minimal reference-geometry/modeling for stock, tool
    definition, setups.
  - Drawing (= the sketch environment, per Hunter): only required sketch/drawing tools and
    schematic options, contextual to what's being worked on.
  - Simulate: sim tools + shared reference-geometry group only.
  - PLM / Parts Library / AI: no ribbon (full-page bodies).
- [~] Ribbon tool buttons: liquid-glass gradient background/outline so they obviously read as
  clickable; animations consistent with the rest of the app; correct vertical/horizontal
  centering and padding.
- [~] App-wide rule: every "active/on" indicator that is a vertical line becomes a **bold
  outline around the bounding box**.

## 4. Polish
- [~] Regenerate the out-of-place icons: undo/redo buttons + feature-manager icon.
- [~] Feature manager: vertically center rows; proper padding for text and icons (icons too
  close to the edge) — header, filter, section labels, tree rows, bottom buttons, live.step+.
- [~] Axis triad: hug far LEFT under the feature tree when the tree is short; move back toward
  the middle when the tree is long.
- [~] File-name preview / doc tab strip: vertically center its content.

## 5. Simulations (Hunter 2026-07-06 late — FULL implementation required)
- [~] Kinematic/motion studies end-to-end: motor definition → play/pause/scrub timeline →
  viewport bodies animate → trace plots (angle/velocity vs time).
- [ ] **All simulation kinds live in the ONE Simulate tab**: structural FEA (static, modal,
  thermal, buckling, fatigue, frequency response, contact), CFD (fast + accurate), motion,
  **electronics simulation** (SPICE/impedance via the ecad engine) and **firmware emulation**
  (fw_sim/fw_debug) — no separate sim tabs.
- [ ] **Progressive disclosure by open part type**: electronics-sim and emulation study options
  appear ONLY when electronics parts (schematic/PCB/firmware docs) are open; mechanical study
  types for parts/assemblies; motion only when an assembly with mates is open. Users are never
  shown options irrelevant to what they have open.
- [ ] **Simulate view hides the feature/model tree entirely.** The left pane shows ONLY the
  simulation setup tree (study → material / fixtures / loads / mesh / results …) rendered in the
  same visual style as the feature tree.
- [ ] Controls for the different simulation kinds stay separated per kind (a static study shows
  static setup nodes; a flow study shows flow nodes; an electronics sim shows netlist/probe
  nodes) — one setup tree grammar, per-kind vocabularies.

## 6. Workspace bodies
- [~] Model: today's modeling environment; entering a sketch auto-switches to Drawing.
- [~] Drawing: the sketch environment (sketch editing) + 2D drawing sheets.
- [~] Electronics: ECAD editor as the full central body.
- [~] Manufacture: viewport + CAM manager; cross-doc auto-update (part change → CAM stale/regen).
- [~] PLM: vault status, checkpoints, history, release/lock.
- [~] Parts Library: scraped catalog browse/search + vendor login browsers (McMaster-Carr
  first) + spec/doc scraping + download queue.
- [~] AI: dedicated full-screen AI workspace; docked Copilot stays available everywhere.

## 7. Repo unification
- [~] Delete ui/imgui, ui/abstract, app/ (single cross-platform source = host/, Windows+Linux
  from one tree — no dual source files ever again).
- [~] `./build.sh app` builds host/; gate.sh builds + shot-smokes host/.
- [x] host/ CMake points at shared `extern/glad` + top-level `assets/` (single icon atlas).
- [x] Launcher/taskbar pin → host binary.
- [ ] Windows build of restored host/ re-verified (MSVC preset; fix_math.h /FI shim).

## 8. Standing quality bar
- Light theme is the ship look; every change verified in BOTH themes; ui::palette roles only.
- UI professionalism is a hard requirement — SolidWorks-refined, near-shippable.
- Verification shots for Hunter must be light-theme + configured (never first-run/dark).
