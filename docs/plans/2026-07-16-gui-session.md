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

## Final
Full build green, deliver user verification checklist.

## Known pre-existing failures (do NOT fix)
- core/tests/test_document.cpp:2694 eval_mass tessellation volume assert (fails on clean master).

## Status log
- [x] Setup: master pulled, plan written.
- [ ] Wave 1 dispatched / collected
- [ ] Gate 1 green + pushed
- [ ] Wave 2 dispatched / collected
- [ ] Gate 2 green + pushed
- [ ] Checklist delivered
