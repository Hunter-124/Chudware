# GUI/Core Session — 2026-07-16 (Wave 4)

Orchestration plan for Hunter's 15-item feedback batch + native file-format design.
Recovery doc: if the session is interrupted, resume from the first unchecked gate.
Branch: master (push after each green wave). Baseline: 45106832.

## Items (user numbering)
- 1, 12: OK — no work.
- 2: Axes join planes/origin auto-hide. Refs hide on first SOLID/3D feature (Features/
  Surfaces/Sheet-Metal/Weldments/Mold ribbon tools), NOT on first sketch. Opening a file
  containing such features starts with all references hidden.
- 3: Axis drawn length must equal reference-plane extent (style already approved).
- 4: Axes must be selectable inside an active sketch (as reference).
- 5a: Ref-plane PM parameter slider misbehaving.
- 5b: Unit-system-aware defaults everywhere (imperial: 0.25 in extrude, 50 lbf force;
  metric: 10 mm, 100 N). Never metric-converted uglies like 0.3937.
- 6a: Revolve performance investigation + optimizations.
- 6b: PM UX: "Direction 2"/"Thin Feature"/"Options" collapsed by default; "Select with AI"
  padding tightened; "Pick in viewport" hint moved above the well box, right-aligned with
  box edge, vertically aligned with the well label. Apply across ALL PM forms.
- 7: Washer (two concentric circles, one sketch profile) extrude STILL broken.
- 8: Sketch select-all + Delete key / RMB Delete deletes everything at once. RMB context
  menus (sketch / viewport / feature tree) reworked to be selection-dependent.
- 9: Imported .step+ assemblies (Place_* features): group as expandable component nodes
  with nested part history; viewport click resolves to the component, not Place_X; fix
  duplicated names. Test target: ~/Documents/ME489/.../CD-000_GA_Deployed_v6_WIP.step+
- 16 (interjection): stray UI artifacts to fix in Wave 2 — (a) broken icon left of the
  "Model" workspace switcher button; (b) small red rectangle artifacts at top corners of
  ribbon buttons (Draft/Hole Wizard/Thread); (c) doc-tab icons/close-[x] render broken on
  part tabs. Scout GlitchScout diagnoses during Wave 1; fixed by a Wave-2 worker (icons/
  ribbon.cpp/doc_tabs). Integrator-owned ribbon.cpp coordinated with G's tab-hide patch.
- 10a: Ribbon: Split -> multi-body section (out of Curves); Helix -> Reference section
  (left of Ref Plane); Assembly tools merged into right side of Features tab; Assembly
  tab removed.
- 10b: Insert Comp. popup: list open + recent parts (no bare path textbox). Std. Parts
  popup: route user to Parts Library tab. Parts Library "Insert Component" flow: user can
  target the intended doc; add automated test. Audit remaining asm tools + spacing.
- 11: RMB "Open Part" broken from viewport AND feature manager.
- 13: Zoom-to-fit must fire on first actual DISPLAY of a doc in the viewport (e.g. opened
  from Parts Library while another tab focused, then switched to).
- 14: Origin ring screen-constant sizing wrong at zoom extremes; preserve pick hitbox.
- 15: Startup perf is bad; add loading indicators (left side of bottom status bar, like
  sim progress); move "Rebuilding..." chip out from behind PLM/Explorer/FM buttons into
  that same spot with real rebuild progress detail.
- extra: docs/design/native_format.md — unified native format for part/assembly/drawing/
  sim (ECAD separate), layout, load-time optimization, VC/PLM strategy, disk-bloat control.

## Wave 1 (9 parallel, disjoint write-sets)
- A RefVisibilityViewport: items 2, 8(viewport RMB), 9(viewport click routing), 11(viewport), 13
  -> viewport_panel.cpp, app.cpp, app_state.{h,cpp}   [sole owner of app.cpp this wave]
- B RendererAxisRing: items 3, 14 -> renderer.cpp (+renderer.h)
- C SketchAxisDelete: items 4, 8(sketch) -> sketch_mode.cpp
- D TreeImportedParts: items 9, 11(FM), 8(FM RMB) -> feature_tree.cpp
- E PmUxSlider: items 5a, 6b -> property_manager.cpp, pm_forms.cpp
- G RibbonRework: item 10a -> ribbon_def.cpp (sole owner)
- H AsmPopups: item 10b -> asm/parts popup files (feature_tree/app.cpp/pm files PROHIBITED)
- I WasherCore: item 7 -> core/geom/profile.cpp, core/doc/document.cpp, core/tests/test_profile_extrude.cpp
- L FormatDesign: extra -> docs/design/native_format.md (new file)

Cross-agent contract: axis half-length == plane half-extent; sketch hitTestDatumAxis pick
length mirrors the drawn length (seam-fixed by integrator at gate if mismatched).

## Gate 1
./build.sh && ./build.sh core && ctest (profile_extrude) && gui_lint --rule theme
-> fix seams inline -> git commit -m "gui+core: wave1 ..." && git push

## Wave 2 (after gate 1)
- F UnitDefaults: item 5b (pm files freed by E)
- J RevolvePerf: item 6a (core files freed by I)
- K StartupStatus: item 15 (app.cpp freed by A)
- Fix-up agents for wave-1 gaps.

## Gate 2
Same gates + any core tests J touches -> commit+push.

## Status log
- [x] Setup: master pulled, plan written.
- [x] Wave 1 dispatched / collected (A B C D E G H I L + glitch item added)
- [x] Gate 1 green + pushed (0d717a1a): host+core build, profile_extrude+insert_component pass,
      theme+icon lint clean. Integrator patches: axesUserToggled eye, Assembly tab hidden,
      RT_Assembly->RT_Features retargets, insert_component test fixes.
- [x] Wave 2 dispatched / collected (F unit defaults, J revolve perf, K startup+status, M glitches)
- [x] Gate 2 green + pushed (ba9b1f77): host+core build, profile_extrude+insert_component pass,
      theme+icon lint clean, m1 boot scenario ok. Integrator patch: sim_manager force/pressure
      unit-native seed (50 lbf / 100 psi). Pre-existing document.cpp:2694 confirmed unrelated.
- [x] Checklist delivered

## Deferred (documented gaps, need engine/contract work — not GUI-fixable this session)
- Item 9 full nested history for imported parts: only native .step+ sources expose feature
  history (read off-disk via source_path). Plain STEP/mesh imports and embedded-brep-only
  imports have NO parametric history to show; a doc_import_feature_tree RPC would be needed.
- Item 5b/6b contract note (feature_params.md) left un-appended (optional; GUI-only defaults).

## Wave 3 (AI copilot overlay + settings + providers + callback) — interjection batch
Live copilot chat overlay is host/src/ui/legacy_chat.cpp (despite name). drawProviderModelPicker
and the Agents/Prompts/Skills/Permissions/Accounts overlay tabs also live there.
- N ChatOverlay legacy_chat.cpp: (1) no-usage-API -> toast not chatbox; (2) context meter inline
  with Design/Edit/Ask; (3) Design/Edit/Ask -> dropdown, vertically centered; (4) remove
  Permissions dropdown from composer (keep as overlay Settings tab); (5) restyle overlay Settings;
  (6) Agents/Prompts/Skills/Permissions/Accounts tabs -> horizontal row on top; (7) fix UI
  scaling (privacy checkbox text cutoff etc.); (8) remove Checkpoints button; (9) model picker
  lists models for signed-in providers (xAI/cursor/claude); (10) rewind+edit buttons below box,
  right-aligned, smaller rewind, Edit message -> pencil icon next to rewind.
- O SettingsModal settings_modal.cpp: (11) sticky tab bar on scroll; (12-display) render provider
  account names/usage the backend provides.
- P ProviderBackend usage.py/xai_sub_provider.py/cursor_sub_provider.py/anthropic_sub_provider.py:
  (12-data) grok real account names (not UUIDs), cursor name+usage, claude usage after restart.
- Q CallbackPage providers/sub_auth.py (CallbackServer do_GET 62-87): (13) brand the
  localhost:54545/callback success/failure HTML.
Gate 3: ./build.sh + agent pytest (subscription/usage) + gui_lint theme -> commit+push.
