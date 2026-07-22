# GUI Overhaul Requirement Register (Hunter, 2026-07-06)

Canonical, durable register of every GUI requirement from the 2026-07-06 direction reset.
The host/ GUI (`host/build-linux/bin/AI-CAD`) is THE product GUI; ui/imgui (AI-CAD2) is deleted
(port ledger: `docs/PORT_LEDGER.md`). **Nothing on this list may be silently dropped** — items
not implemented tonight stay checked-off here and on the work board until done.

Status legend: `[ ]` pending · `[~]` in progress (fleet wf_b556d138-1bf) · `[x]` done+verified.

## 1. Top chrome (title bar)
- [x] Remove the Edit, View, Insert, Tools, Window, Help menus.
  (T-RF10 2026-07-22: title bar shows only chips + tabs + caption buttons — shot /tmp/r2a1-m1.png;
  former View/Help essentials live inside the File chip popup, title_bar.cpp FilMenuDrawer.)
- [x] Left group, visually separated (non-design controls), restyled as chips matching the tabs:
  **File** (exact current menu behavior), **Search**, **Settings**.
  (T-RF10 2026-07-22: drawChipAt glass pills + liquid-glass dividers, title_bar.cpp:342-761;
  verified in /tmp/r2a1-m1.png title-bar crop.)
- [x] Workspace tabs centered in the title bar with the OS window controls:
  **Model, Drawing, Simulate, Electronics, Manufacture, PLM, Parts Library, AI**.
  (T-RF10 2026-07-22: tabs centered between left group and min/max/fullscreen/close,
  title_bar.cpp:790-878; narrow windows compress left-aligned by design. Shot /tmp/r2a1-search.png.)
- [x] Each tab replaces the viewport (and toolbar where sensible); views stay attached to the
  main window (no floating top-level windows for tabs).
  (T-RF10 2026-07-22: AI_CAD_SHOT=workspaces 8 captures — scratch/workspace_shots/*.png — each
  workspace swaps the central body in-place; PLM/Parts Library/AI are full-page bodies.)
- [x] Title-bar drag hit-testing stays correct on Windows AND Linux.
  (T-RF10 2026-07-22: Linux functionally verified under Xvfb+xdotool — empty caption drags the
  window, chip/tab spans do not (chrome::tick + HitLayout spans, win_chrome.cpp:164-218);
  Windows path unchanged (WM_NCHITTEST, win_chrome.cpp:48-84).)

## 2. Dialogs
- [x] Settings: title strip ("Settings" + X) fully OPAQUE in light AND dark mode.
  (T-RF10 2026-07-22: TitleBg* pushed opaque from palette.headerBg, settings_modal.cpp:3461-3475;
  shots /tmp/r2a1-settings-light.png + /tmp/r2a1-settings-dark.png — no bleed-through either theme.)
- [x] Search: same function as today, but in a settings-style bordered popout (title strip + X).
  (T-RF10 2026-07-22: command_search.cpp:68-89 pushes opaque WindowBg + TitleBg*; live Ctrl+P
  capture /tmp/r2a1-search.png shows bordered popout, "Search" strip + X, working command list.)

## 3. Ribbon / toolbars
- [x] Per-tab tool filtering — only tools a user would need in that tab (strict isolation).
  (T-RF10 2026-07-22: ribbon_tabVisible, ribbon.cpp:130-159; verified per-workspace in
  scratch/workspace_shots/*.png.)
  - Manufacture: manufacturing tools + minimal reference-geometry/modeling for stock, tool
    definition, setups. (workspace_05_manufacture.png: Reference + Job + 2.5D/Holes/3D/Turning/
    Cutting groups only.)
  - Drawing (= the sketch environment, per Hunter): only required sketch/drawing tools and
    schematic options, contextual to what's being worked on. (workspace_02_drawing.png:
    Sketch / View Layout / Annotation tabs only.)
  - Simulate: sim tools + shared reference-geometry group only. (workspace_03_simulate.png:
    per-study-type tabs Static…EM, each with its tools + shared Reference group.)
  - PLM / Parts Library / AI: no ribbon (full-page bodies). (drawRibbon returns 0 height for
    workspace >= 5, ribbon.cpp:1670-1671; workspace_06/07/08 shots.)
- [x] Ribbon tool buttons: liquid-glass gradient background/outline so they obviously read as
  clickable; animations consistent with the rest of the app; correct vertical/horizontal
  centering and padding.
  (T-RF10 2026-07-22: ribbon_btnRest accent gradient + shine + hairline, ribbon.cpp:911-959;
  native-res crop of /tmp/r2a1-search.png ribbon shows centered, outlined, gradient tiles.)
- [x] App-wide rule: every "active/on" indicator that is a vertical line becomes a **bold
  outline around the bounding box**.
  (T-RF10 2026-07-22: ribbon_btnActive = accentSoft fill + 2px accent box, ribbon.cpp:964-970;
  armed tool "Smart Dim." shows the bold box in scratch/sketch_shots/sketch_01_enter.png;
  no vertical-line indicators remain in ribbon/title-bar surfaces.)

## 4. Polish
- [x] Regenerate the out-of-place icons: undo/redo buttons + feature-manager icon.
  (T-RF10 2026-07-22: FM toolbar undo/redo render as matching line-art curved-arrow glyphs from
  the atlas (native-res crop of /tmp/r2a1-m1.png); feature-manager pane icon consistent;
  `python3 scripts/gui_lint.py` clean.)
- [x] Feature manager: vertically center rows; proper padding for text and icons (icons too
  close to the edge) — header, filter, section labels, tree rows, bottom buttons, live.step+.
  (T-RF10 R2a 2026-07-22: every row centers content via (rowH - content)*0.5f — fmRowPill /
  section dividers / tree rows, feature_tree.cpp:159-615; polish-G2 padding (6dp x / 4dp y)
  matches pill vs row; header chip fmPaneHeaderChip fits its content with the icon fully
  inside (feature_tree.cpp:3070-3109); the old bottom text-button strip is now the centered
  6-icon doc toolbar + glass filter field (drawFeatureManagerTopStrip, feature_tree.cpp:3671+).
  Eyeball: native-res FM crop of /tmp/r2a2-fm-doctabs.png — header, toolbar, filter, Part1 /
  Reference / Features labels, Box1/Fillet1/Shell1 rows + eye glyphs all centered/padded.)
- [x] Axis triad: hug far LEFT under the feature tree when the tree is short; move back toward
  the middle when the tree is long.
- [x] File-name preview / doc tab strip: vertically center its content.
  (T-RF10 R2a 2026-07-22: band hugs the pill row exactly — doc_tabs.cpp:569-572 "Hunter
  centering fix"; icon/label/× centered via (tabH - content)*0.5f, doc_tabs.cpp:187-210;
  "+N" overflow chip centers its label, doc_tabs.cpp:344-347. Eyeball: native-res doc-tab
  crop of /tmp/r2a2-fm-doctabs.png — AI-tab pills + +6 chip all vertically centered.)

## 5. Simulations (Hunter 2026-07-06 late — FULL implementation required)
- [x] Kinematic/motion studies end-to-end: motor definition → play/pause/scrub timeline →
  viewport bodies animate → trace plots (angle/velocity vs time).
- [x] **Per-simulation-type ribbon tabs** (Hunter red-pen 2026-07-07, supersedes the earlier
  "one Simulate tab" lock): the Simulate workspace shows one ribbon tab per study kind —
  Static / Modal / Flow / Thermal / Fatigue / Motion / EM — each carrying ONLY that kind's
  tools ("+ New Study" first, then setup tools, Run/Results, shared Reference group).
  Implemented in ribbon.cpp (tabs 13–19, Simulate-only) + ribbon_def.cpp.
- [x] **EM (electromagnetics) tab**: Eigenmode / Freq Sweep (S-params) / Time Domain study
  buttons + Ports/Boundaries/Freq Range tools, gated on an async `em_status` probe — when the
  em_* backend is missing the whole set renders disabled with an honest "EM solver not
  installed" tooltip (ui::setRibbonDisabledPrefix). Study setup forms wire up once the EM
  backend agent's RPC contract is final (wire request filed in host/WIRE_REQUESTS.md).
  (T-RF10 R2a 2026-07-22 — GUI side DONE, engine dependency noted: the RPC contract is final
  and worker-registered (engine_oracle/worker.py:155, T-SIM11; em_status/em_eigenmode/
  em_driven/em_timedomain in engine_oracle/sim/em/worker_rpc.py). Ribbon rows sim_em_* with
  Eigenmode/Freq Sweep/Time Domain + Ports/Boundaries/Freq Range + Run/Results +
  shared Reference group (ribbon_def.cpp:624-640); SimManager probes em_status once async on
  Simulate entry and greys the set via setRibbonDisabledPrefix when missing
  (sim_manager.cpp:1075-1098); setup forms dispatch the real RPCs (openEmForm →
  em_eigenmode/em_driven/em_timedomain, sim_manager.cpp:1185-1212). EM tab confirmed present
  in the Simulate ribbon tab row — /tmp/r2a2-em-simulate.png (AI_CAD_SHOT=simdisc_mech).
  Remaining dependency is engine-side, NOT GUI: a full-wave openEMS install for non-analytic
  solves (tracked under the sim DAG, T-SIM11; the GUI degrades honestly without it.)
- [x] **Tool-invoked setup panels** (Hunter red-pen 2026-07-07): the numbered always-open
  accordion stack is gone. Setup editors open ONLY when the matching ribbon tool is invoked
  (or a study-tree node is double-clicked), PropertyManager-style — the panel takes over the
  left pane body with an icon+title header and OK to close; the tree keeps "+ New Study",
  the study tree with status dots, and the study switcher.
- [ ] **Progressive disclosure by open part type**: electronics-sim and emulation study options
  appear ONLY when electronics parts (schematic/PCB/firmware docs) are open; mechanical study
  types for parts/assemblies; motion only when an assembly with mates is open. Users are never
  shown options irrelevant to what they have open. (Electronics-circuit sim remains follow-up
  T-SIM10; the EM tab is always present per the red-pen tab list.)
- [x] **Simulate view hides the feature/model tree entirely.** The left pane shows ONLY the
  simulation setup tree (study → material / fixtures / loads / mesh / results …) rendered
  FM-grade via the exported feature_tree.h helpers (fmRowPill / fmTreeIconInArrowSlot /
  fmSectionDivider); the Simulation tree is clamped OUT of Model/Drawing left panes.
- [x] Controls for the different simulation kinds stay separated per kind (a static study shows
  static setup nodes; a flow study shows flow nodes) — one setup tree grammar, per-kind
  vocabularies + per-kind ribbon tabs. (Electronics netlist/probe nodes: T-SIM10.)

## 6. Workspace bodies
- [~] Model: today's modeling environment; entering a sketch auto-switches to Drawing.
  (T-RF10 R2a 2026-07-22 status note: Model body = today's modeling environment, DONE.
  "Entering a sketch auto-switches to Drawing" is NOT implemented and is deliberately
  superseded by the C34 SolidWorks behavior — entering a sketch flips the ribbon to the
  Sketch tab IN the Model workspace (app.cpp:4330) instead of switching workspace; the
  Drawing workspace hosts 2D drawing sheets. Needs Hunter's call: keep C34 behavior or
  implement the workspace hop. Owner: title-bar/workspace seam, not R2a.)
- [~] Drawing: the sketch environment (sketch editing) + 2D drawing sheets.
  (T-RF10 R2a 2026-07-22 status note: 2D drawing sheets DO render in the Drawing workspace —
  opening/activating a drawing tab auto-switches (host/src/core/doc_tabs.cpp:263), and the
  Drawing ribbon carries Sketch/View Layout/Annotation only. Sketch EDITING stays in Model
  per C34 (see Model note above) — the "sketch environment lives in Drawing" half is open by
  the same design decision.)
- [x] Electronics: ECAD editor as the full central body.
- [x] Manufacture: viewport + CAM manager; cross-doc auto-update (part change → CAM stale/regen).
- [x] PLM: vault status, checkpoints, history, release/lock.
  (T-RF10 R2a 2026-07-22: full-page PLM hub with Overview/Files/Planning/Graph/Git/Settings
  pages (plm_workspace.cpp + plm_pages.cpp, 2284 lines) — checkpoint composer + history feed
  (plm_pages.cpp:325-372), release flow via plm_release with release events in history
  (plm_pages.cpp:924-996), vault/lifecycle status via plmLifecycleBadge + the status-bar PLM
  chip. Eyeball: scratch/workspace_shots/workspace_06_plm.png.)
- [x] Parts Library: scraped catalog browse/search + vendor login browsers (McMaster-Carr
  first) + spec/doc scraping + download queue.
  (T-RF10 R2a 2026-07-22: full-page body (parts_workspace.cpp) — Categories pane, federated
  catalog search w/ relevance sort + price/vendor/material/source filters, vendor login
  wizard with headed-fallback + download-queue polling (parts_vendors.cpp:87-110), embedded
  vendor login browsers incl. McMaster modal handling (parts_browser_panel.cpp:473), spec/doc
  scraping via catalog research seeds (parts_workspace.cpp:579). Eyeball:
  scratch/workspace_shots/workspace_07_parts_library.png.)
- [x] AI: dedicated full-screen AI workspace; docked Copilot stays available everywhere.
  (T-RF10 R2a 2026-07-22: ai_workspace.cpp (1693 lines) — Sessions rail, conversation pane,
  Agent Workflow / Session Plan / Leaderboard tabs, workflow knobs; the docked Agent copilot
  renders in every workspace (all T-RF10 shots). Eyeball:
  scratch/workspace_shots/workspace_08_ai.png.)

## 7. Repo unification
- [x] Delete ui/imgui, ui/abstract, app/ (single cross-platform source = host/, Windows+Linux
  from one tree — no dual source files ever again).
  (T-RF10 R2a 2026-07-22: verified on disk — `ls ui/` = viewport/ only (the AICM loader
  consumed by core/); no app/ dir; build.sh header documents the retirement.)
- [x] `./build.sh app` builds host/; gate.sh builds + shot-smokes host/.
  (T-RF10 R2a 2026-07-22: build.sh build_app() = cmake -S host -B host/build-linux +
  ninja Billet (build.sh:42-47); gate.sh stage 2b builds the host GUI and stage 5 runs the
  AI_CAD_SHOT=m1 headless shot smoke (gate.sh:176-187 + stage-5 header).)
- [x] host/ CMake points at shared `extern/glad` + top-level `assets/` (single icon atlas).
- [x] Launcher/taskbar pin → host binary.
- [ ] Windows build of restored host/ re-verified (MSVC preset; fix_math.h /FI shim).

## 8. Standing quality bar
- Light theme is the ship look; every change verified in BOTH themes; ui::palette roles only.
- UI professionalism is a hard requirement — SolidWorks-refined, near-shippable.
- Verification shots for Hunter must be light-theme + configured (never first-run/dark).
