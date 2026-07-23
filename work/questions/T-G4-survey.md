# T-G4 Survey Results

## Q1: Provider/model picker

### Raw grep results

`grep -r "drawProviderModelPicker" host/src/`:
```
host/src/ui/legacy_chat.h:       bool drawProviderModelPicker(AppState& state, bool busy);
host/src/ui/settings_modal.cpp:  #include "legacy_chat.h"  // ui::drawProviderModelPicker (shared with the inline AI tab)
host/src/ui/settings_modal.cpp:  ui::drawProviderModelPicker(st, busy);          // line 568
host/src/ui/legacy_chat.cpp:     // "Manage models" panel inside drawProviderModelPicker can call them).
host/src/ui/legacy_chat.cpp:     bool drawProviderModelPicker(AppState& st, bool busy) {   // line 1734
host/src/ui/legacy_chat.cpp:     if (ui::drawProviderModelPicker(st, busy)) saveSettings(settings);  // line 2164
```

`grep -rn "ai_provider|ai_model|model_picker|providerPicker|modelPicker" host/src/`: no results.

### Analysis

There is a **single implementation** of the provider/model picker (`drawProviderModelPicker`, defined at legacy_chat.cpp:1734), declared in `legacy_chat.h`. It is called in exactly two places:

1. `settings_modal.cpp:568` — inside the Settings modal's "AI" tab. No persistence on this call (Settings modal has its own Save button path).
2. `legacy_chat.cpp:2164` — inside the inline "AI Settings" popover inside the Copilot column. This call wraps the result in `if (...) saveSettings(settings)` so changes persist immediately (no Save button in this inline view).

No divergent copies. No inline `ai_provider`/`ai_model`/`model_picker` variants anywhere. The picker is already unified — this Q1 concern is resolved.


## Q2: Permissions popup

### Raw grep results

`grep -n "ermission" host/src/ui/legacy_chat.cpp`:
```
8:    // toggle + Permissions + context meter; transparent prompt; model/thinking/send toolbar). The AI
2822: // Prelude row above the prompt: Design + Permissions on the left, context meter on the right.
2873:     const char* permLabel = st.settings.offlineMode ? "Permissions: Offline" : "Permissions";
2876:         ImGui::SetTooltip("Privacy and run permissions for the copilot.");
```

### Code section (legacy_chat.cpp:2877–2890)

The permissions popup (`"##cmpperm"`) is a `BeginPopup` triggered by a `SmallButton` labelled "Permissions" (or "Permissions: Offline"). It contains three checkboxes:

1. **Offline mode** (`st.settings.offlineMode`) — writes `saveSettings(st.settings)` immediately on change.
2. **Supervised** (`st.settings.planMode`) — writes `saveSettings(st.settings)` immediately on change.
3. **Show assistant thoughts** (`gShowThinking`) — file-scope `static bool`, **not persisted to config**. It lives only for the session.

### Analysis

- `offlineMode` and `planMode` both save to `config.json` on toggle — correct.
- `gShowThinking` is a static bool in legacy_chat.cpp scope. It is not written to `Settings` and not saved. Whether this is intentional (session-only preference) or a missing persistence hookup is unclear from grep alone, but it's the only checkbox in this popup without persistence.


## Q3: Dead code candidates

### resetLayout

```
host/src/core/app_state.h:950:    bool layoutInit = false, resetLayout = false, openSettings = false;
host/src/ui/layout.h:52:    // "Reset layout" ... must also restore the ...
host/src/ui/title_bar.cpp:61:    // items below just set st.resetLayout; app.cpp calls buildDefaultLayout()
host/src/ui/title_bar.cpp:292:    if (ImGui::MenuItem("Reset layout")) st.resetLayout = true;
host/src/ui/title_bar.h:10:    // state.hitLayout for the native hit-test. Sets state.openSettings / resetLayout / showAbout /
host/src/app.cpp:2679:    if ((!st.layoutInit && ImGui::DockBuilderGetNode(dockId) == nullptr) || st.resetLayout) {
host/src/app.cpp:2682:        st.resetLayout = false;
```

`resetLayout` is **live** — it's a menu item in the title bar that triggers a dock layout rebuild in `app.cpp`. Not dead.

### Docked/overlay layout remnants

`grep -rn "panelsOverlay|dockedLayout|dockSpitter|docked_layout" host/src/`: **no results** — these symbols are fully gone.

`grep -rn "drawDockedLayout|drawPanelsOverlay" host/src/`: **no results** — draw functions also fully gone.

### TODO/FIXME/DEAD/unused scan (first 60 lines of results)

```
host/src/ui/model_registry.h:3:    // Header-only so both settings_modal.cpp and legacy_chat.cpp can share it without a
host/src/ui/settings_modal.cpp:20:  #include "legacy_chat.h"  // ui::drawProviderModelPicker (shared with the inline AI tab)
host/src/ui/legacy_chat.cpp:36:  #include "legacy_chat.h"
host/src/ui/layout.cpp:31:  // drawAIPanel in legacy_chat.cpp) — no left/right dock splits, no splitter bars anywhere.
host/src/ui/metrics.h:64:  // ---- pinned Copilot column default/min width (PIXELS; consumed by legacy_chat drawAIPanel) -----
host/src/ui/metrics.h:85:  // ---- AI Copilot pane (legacy_chat.cpp) --------------------------------------------------------
host/src/ui/layout.h:8:  // Copilot column (legacy_chat.cpp) mirrors the same pattern on the right edge.
host/src/ui/layout.h:43:  // the panels draw; consumers (drawLeftPane here, the Copilot column in legacy_chat.cpp) read it
host/src/ui/layout.h:55:  // draw. Slots: 0 = FeatureManager column (drawLeftPane), 1 = Copilot column (legacy_chat.cpp).
host/src/ui/viewport_panel.cpp:2172:  s_pp.newValue = probe.old_value;  // value before the drag
```

### Analysis

- The regex matched on `legacy_` in the comment strings (referring to the file `legacy_chat.cpp` by name in comments) and on `old_value` in viewport_panel.cpp — these are **not dead code**, just identifier names.
- No `TODO`, `FIXME`, `DEAD`, `#if 0`, or `_old` suffix hits surfaced. The UI directory appears clean of flagged dead code markers.
- `old_value` at viewport_panel.cpp:2172 is a struct field name for the probe's prior value — live code.


## Q4: Variance-scrim

### Raw grep results

`grep -n "variance|scrim" host/src/ui_theme.cpp`:
```
638:  constexpr float kDynVarBoostGain = 2.2f, kDynVarBoostMax = 0.35f;  // busy backdrop => stronger scrim/halo
755:  // Luminance still picks the base tone + text; the style's hue/sat only colorize the scrim, and
771:      out.scrim         = 0.f;
794:  // Contrast scrim: strongest at mid-luminance (ambiguous backdrop), tapering at the extremes;
796:  float scrim = 0.10f + 0.26f * (1.f - fabsf(l - 0.5f) * 2.f);
797:  scrim = std::min(1.f, scrim * rs.opacityMul + 0.22f * out.contrastBoost);
798:  out.scrim = scrim;
799:  ImVec4 neutral = out.darkText ? ImVec4(0.97f, 0.98f, 1.00f, scrim)
800:                                : ImVec4(0.055f, 0.075f, 0.10f, scrim);
855:  // steers the tint hue/sat and boosts the scrim on busy backdrops.
871:      st.dynVar = approach(st.dynVar, stats.variance, dt, kAdaptiveTau);
880:      // Busy backdrop => stronger scrim + text/icon halo.
885:  // Decisive VIRTUAL luminance for text tone (never mid-gray); REAL smoothed lum drives scrim.
890:  surf.scrim  = realScrim;
961:      // Material wash + the surface's own contrast scrim, capped — the blur already carries
962:      // most legibility so this stays a light finishing tint. surf.scrim already carries the
964:      float tintA = tintAlpha * 0.55f * rs.opacityMul + surf.scrim;
```

### Analysis

Both `variance` and `scrim` are **fully implemented** in ui_theme.cpp:

- `stats.variance` drives `st.dynVar` (exponential approach, line 871), which feeds `kDynVarBoostGain`/`kDynVarBoostMax` to boost the scrim on busy (high-variance) backdrops (line 638, 880).
- `scrim` is computed from luminance (mid-luminance = strongest scrim, line 796), multiplied by `rs.opacityMul`, boosted by `contrastBoost`, and stored in `AdaptiveSurface::scrim` (line 798/890).
- `surf.scrim` is consumed in the material wash tint blend (line 964).

No dead or stub variance/scrim code. This is live and functioning.


## Q5: Hardcoded colors

### Raw grep results — sim_manager.cpp

```
sim_manager.cpp:2602:  ImU32 col = IM_COL32(90, 170, 250, 255);   // waveform trace hue (same family as the graphs)
sim_manager.cpp:2769:  // semantic accents from palette roles — the old dark-theme IM_COL32 light-greys were unreadable
sim_manager.cpp:2778:  const ImU32 yieldCol = Palette::toU32(warnA), bandCol = IM_COL32(90, 170, 250, 55);
sim_manager.cpp:2779:  const ImU32 tcol[5] = {IM_COL32(90, 170, 250, 255), IM_COL32(120, 210, 120, 255),
sim_manager.cpp:2780:                         IM_COL32(240, 140, 60, 255), IM_COL32(200, 120, 230, 255),
sim_manager.cpp:2781:                         IM_COL32(230, 90, 90, 255)};
sim_manager.cpp:2782:  const ImU32 tdim[5] = {IM_COL32(90, 170, 250, 120), IM_COL32(120, 210, 120, 120),
sim_manager.cpp:2783:                         IM_COL32(240, 140, 60, 120), IM_COL32(200, 120, 230, 120),
sim_manager.cpp:2784:                         IM_COL32(230, 90, 90, 120)}};
```

Note: line 2769 itself comments that `yieldCol` was migrated to `Palette::toU32(warnA)` — the `tcol`/`tdim` multi-line arrays remain hardcoded.

### Broader IM_COL32 survey (first 30 hits across host/src)

Hardcoded `IM_COL32` calls appear in:
- `doc_tabs.cpp:139` — semantic red close button dot (comment says "kept literal")
- `drawing_panel.cpp` — 20+ hits: dimension/annotation colors (red for critical, orange for selected, blue for normal), paper white, ink colors, hatch fill, mark color
- `sim_manager.cpp` — 9 hits: waveform and stress-plot trace palette arrays

### Analysis

`drawing_panel.cpp` has the largest cluster of hardcoded colors — these appear to be intentional engineering-drawing ink colors (paper is always white, dimension lines are always blue/orange/red by convention). The `doc_tabs.cpp` red dot is also explicitly kept literal by comment.

`sim_manager.cpp:2778–2784` contains the most actionable P2 item: the `tcol[5]`/`tdim[5]` trace-color arrays are still literal RGB values rather than palette roles, while `yieldCol` was already migrated. These 5 hues (blue, green, orange, purple, red) could map to palette semantic roles.


## Q6: E3 hooks status

### Raw grep results

`grep -rn "ai_reverse_engineer|ai_explain_part|reverse_engineer|import.*toast|importSummary" host/src/`: **no results**.

### Analysis

The E3 hooks (`ai_reverse_engineer`, `ai_explain_part`, `importSummary`) are **not implemented** in `host/src/`. No palette commands, no toast calls, no function stubs. If these were planned for the reverse-engineer import flow, they have not been wired in.


## Q7: Scenarios

### Raw grep results

`grep -n "appearance|CHUDWARE_SHOT" host/src/ui/scenarios.cpp` (first 20 lines):
```
52:     // ---------- C17 M1 integration scenario (CHUDWARE_SHOT=m1) ----------
158:                             mop.push_back({{"op","set_appearance"},{"id","b1"},
268:     // ---------- C18 sketch scenario (CHUDWARE_SHOT=sketch) ----------
397:     // ---------- C20b assembly scenario (CHUDWARE_SHOT=asm) ----------
587:     // ---------- C19 FEA scenario (CHUDWARE_SHOT=fea) ----------
675:     // ---------- CAM copilot fast-path scenario (CHUDWARE_SHOT=cam) ----------
725:     // ---------- external-flow scenario (CHUDWARE_SHOT=flow) ----------
783:     // ---------- C19 evalmass scenario (CHUDWARE_SHOT=evalmass) ----------
815:     // --- C22b activity scenario (CHUDWARE_SHOT=activity) ---
883:     // ---------- multi-document scenario (CHUDWARE_SHOT=multidoc) ----------
886:     // ---------- W17 engineering-drawing scenario (CHUDWARE_SHOT=drawing) ----------
889:     // ---------- appearance scenario (CHUDWARE_SHOT=appearance) ----------
893:  // Appearance scenario (CHUDWARE_SHOT=appearance). ...
897:  // scenarios this one has no AppState want-flag: the runner reads CHUDWARE_SHOT itself
902:         const char* sm = std::getenv("CHUDWARE_SHOT");
903:         apWant = st.shot && sm && std::strcmp(sm, "appearance") == 0;
910:         apSavePath = base + "/scratch/appearance_shots";
920:                 std::printf("appearance: new_part FAILED: %s\n", err.c_str());
941:             std::printf("appearance: settings modal opened on Appearance tab\n");
```

### Analysis

The `appearance` scenario (`CHUDWARE_SHOT=appearance`) is **fully implemented** in `ScenarioRunner::runAppearance` (scenarios.cpp:899). It:
1. Creates a new part with a box (60×40×10) so the Appearance tab previews real geometry.
2. Opens Settings modal jumped to the Appearance tab (index 0).
3. Settles for `kSettle*2` frames, then captures `appearance_01_settings.png` to `$CHUDWARE_REPO/scratch/appearance_shots/`.

This scenario differs from older ones in that it reads `CHUDWARE_SHOT` directly rather than using an `AppState` want-flag. The scenario is wired into the main `run()` dispatch at line 890 (`runAppearance(st)`).

Line 158 (`set_appearance` op in the m1 scenario) is unrelated — it's the m1 scenario calling the CAD `set_appearance` operation, not the appearance screenshot scenario.


## Summary: What needs doing

- **Q1 (picker):** Nothing. `drawProviderModelPicker` is already unified — one implementation, two call sites, no divergent copies.

- **Q2 (permissions popup):** Investigate whether `gShowThinking` (the "Show assistant thoughts" checkbox, `legacy_chat.cpp` file-scope static) should be persisted to `Settings`/`config.json`. The other two checkboxes in the same popup already call `saveSettings`. If persistence is desired, add a field to `Settings`, wire the checkbox, and call `saveSettings`.

- **Q3 (dead code):** No dead code markers (`TODO`/`FIXME`/`#if 0`) found in `host/src/ui/`. The docked-layout and panelsOverlay symbols are fully gone. `resetLayout` is live. `old_value` in viewport_panel.cpp is a live struct field. Nothing to remove.

- **Q4 (variance-scrim):** Fully implemented and live. No action needed.

- **Q5 (hardcoded colors):** The `tcol[5]`/`tdim[5]` trace-color arrays in `sim_manager.cpp:2779–2784` are the most actionable P2 cleanup — migrate the 5 hues to palette semantic roles (the same migration that already moved `yieldCol`). `drawing_panel.cpp` hardcoded colors appear intentional (engineering ink conventions); low priority. `doc_tabs.cpp:139` red dot is explicitly kept literal by comment.

- **Q6 (E3 hooks):** `ai_reverse_engineer` and `ai_explain_part` palette commands are **not implemented**. If E3 hooks are on the roadmap, these need to be wired: palette command registration, a toast/import-summary display, and (optionally) a `drawImportSummary` widget in the copilot column post-import flow.

- **Q7 (scenarios):** `CHUDWARE_SHOT=appearance` scenario is fully implemented and wired. No action needed.
