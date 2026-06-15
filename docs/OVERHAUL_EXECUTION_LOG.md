# AI-opencad Usability Overhaul Execution Log

## 2026-06-15

### Coordination
- Main thread owned PropertyManager chrome/visibility, feature reorder safety, extrusion reference semantics, PM form wiring, and contract updates.
- Subagent `Kierkegaard` owned renderer first pass: resolved final-size viewport texture, box resolve shader, `Renderer::Timing`, and viewport display switch.
- Subagent `Galileo` owned AI Help/settings/menu scaffolding: Help tab in the AI pane, async `agent_ask_v2` help calls, Help menu entry, and clearer settings grouping.
- Rebased the working tree from the original detached `master` base onto current `master` tip `79c161e` after the user clarified to work from `master`.

### Implemented
- PropertyManager:
  - OK/Cancel now render on a dedicated row using full button-frame sizing.
  - OK/Cancel icon buttons now use compact scaled frame padding, so HiDPI image-button frames match the reserved row and stop clipping past the right edge.
  - Follow-up: OK/Cancel row now reserves the left-pane scrollbar gutter and shrinks safely in narrow PM panes, fixing the Extrude PM button spill shown in the user screenshot.
  - Active PropertyManager draws/ticks even when the FeatureManager tree is hidden.
  - Conditional PM fields are hidden and omitted from preview ops when their controlling combo does not make them relevant.
  - Ordinary active PM labels now use primary text color instead of disabled-looking grey; conditional fields remain hidden until relevant.
  - Shortened active selector hint text to avoid clipping in the narrow left pane.
  - Added `Select with AI` through structured worker method `agent_pm_assist_v1`; host applies only validated visible field patches and activates real selector wells for references.
- Feature reorder:
  - `reorder_feature` validates the entire post-drop feature list so parents cannot be dragged after dependents.
  - Added tests for child-before-parent refusal, parent-after-child refusal, and valid independent reorders.
- Extrude:
  - Added `start` modes: `sketch_plane`, `offset`, `from_reference`, `offset_from_reference`.
  - Added reference end conditions: `up_to_reference`, `offset_from_reference` for Direction 1 and Direction 2.
  - Added planar `start_ref`, `end_ref`, `end2_ref` handling with offset params shown only when relevant.
  - V1 reference targets are planar face/datum planes parallel to the profile plane; non-planar refs fail with a contained error.
- AI Help:
  - Added Help tab in the AI pane with question input, suggestion buttons, selected-feature helper prompt, async ask call, status, errors, and answer display.
  - Added Help > AI Help menu item.
  - Added provider-backed/fallback PM assist module with strict JSON validation and tests.
  - Tightened the AI task pane's mode/provider controls with compact per-row padding so `Design new`, `Edit part`, `Ask`, model selection, and `Supervised` stay readable at 175% scale.
  - Follow-up: AI pane example prompts now use full-width rows with shorter labels, provider/model row handles all four providers, and the supervised/autopilot toggle reserves width instead of crowding the model label.
- Default layout/menu:
  - Switched normal startup scale to the larger monitor axis instead of only X scale and made the status bar metric-driven, which makes Windows mixed-DPI startup less brittle.
  - Added explicit View menu gates for Simulation tools and PCB/electronics tools.
  - Hidden Simulation and PCB ribbon tabs by default; shot modes and direct commands enable their modules before use.
  - ECAD editor window no longer opens by default, so the center dock starts on the 3D viewport.
  - Bounded the Settings modal to the visible viewport so HiDPI scale cannot push the header/footer off-screen.
  - Renamed Settings tabs from ambiguous `Provider / Agents / Agent` to `AI / Prompts / Advanced`.
  - Converted Settings from brittle popup state to a centered, dock-independent Settings window with pinned Save/Cancel controls.
  - Light mode follow-up: ImGui frame controls now get a 1px border in light mode so checkboxes, inputs, combo boxes, and PM controls do not disappear into white panels.
  - Dampened default side-pane DPI growth so FeatureManager and AI remain usable without crushing the central viewport.
  - Fixed ribbon width/row math for HiDPI: big buttons no longer double-scale label width, small-button measurement matches draw width, degraded rows advance by their real height, and overflow reserves chevron space.
  - Kept the current `master` tabbed left pane (`LeftPane`) and moved the improved dock-width math into `ui/layout.cpp`.
  - Shortened long Extrude PM combo display labels (`From Ref.`, `Offset Ref.`, `Offset Surface`) so reference modes fit the normal left pane.
- Renderer:
  - Added renderer-owned final-size resolved texture and shader box resolve from the supersampled color target.
  - Live viewport and screenshot readback now use the same resolved image path.
  - Added lightweight render/resolve timing fields.

### Verification
- `python -m py_compile engine\doc\features\extrude.py engine\doc\ops.py engine\doc\schema.py` passed under the default Python.
- `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe -m engine.doc.test_ops` passed.
- `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe -m engine.doc.test_extrude_ends` passed.
- `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe -m agent.test_pm_assist` passed.
- `.\build.bat host` passed.
- `.\host\build\bin\Release\AI-CAD.exe --ui-screenshot scratch\overhaul_smoke.png` passed after final integration.
- `AI_CAD_SHOT=settings AI_CAD_SCALE=1.75 .\host\build\bin\Release\AI-CAD.exe --ui-screenshot scratch\ui_settings_scale175_after3.png` passed; modal stays inside the window with pinned actions visible.
- `AI_CAD_SHOT=ribbon AI_CAD_SCALE=1.75 .\host\build\bin\Release\AI-CAD.exe --ui-screenshot scratch\ui_ribbon_scale175_after.png` passed; ribbon overflows into the chevron and the center viewport remains usable.
- `AI_CAD_SCALE=1.75 .\host\build\bin\Release\AI-CAD.exe --ui-screenshot scratch\ui_default_scale175_final.png` passed; default FeatureManager/viewport/AI pane proportions are usable at high DPI.
- `AI_CAD_SHOT=settings AI_CAD_SCALE=1.75 .\host\build\bin\Release\AI-CAD.exe --ui-screenshot scratch\ui_settings_scale175_final.png` passed; Settings remains bounded with Save/Cancel visible.
- `AI_CAD_SHOT=ribbon AI_CAD_SCALE=1.75 .\host\build\bin\Release\AI-CAD.exe --ui-screenshot scratch\ui_ribbon_scale175_final.png` passed; ribbon rows and overflow remain inside the viewport.
- `AI_CAD_SHOT=m1 AI_CAD_SCALE=1.75 .\host\build\bin\Release\AI-CAD.exe` passed through `scratch\m1_shots\m1_01_newpart.png` to `m1_06_reopen.png`; PropertyManager OK/Cancel stay in-bounds during edit workflows.
- Subagent renderer smoke: `.\host\build\bin\Release\AI-CAD.exe --ui-screenshot scratch\renderer_resolve_smoke.png` passed before final integration.
- After rebasing onto current `master`, `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe -m agent.test_pm_assist`, `engine.doc.test_ops`, and `engine.doc.test_extrude_ends` passed.
- After rebasing onto current `master`, `.\build.bat host` passed.
- Normal-scale screenshot smoke passed:
  - `scratch\ui_default_rebased.png`: default layout uses left Feature/PM pane, central viewport, and right AI pane with Simulation/PCB hidden.
  - `scratch\m1_shots\m1_05_edit.png`: PropertyManager OK/Cancel, `Select with AI`, and grouped fields fit inside the left pane during edit workflow.
  - `scratch\ui_pm_extrude_default_rebased.png`: default Extrude PM hides start/end reference and offset-only fields.
  - `scratch\ui_pm_extrude_refs_rebased2.png`: reference/offset Extrude PM reveals only the relevant reference wells/offset controls with compact labels.
  - `scratch\ui_settings_rebased_window.png`: Settings opens as a centered, bounded window with `General / AI / Prompts / Advanced` tabs and pinned Save/Cancel controls.
- Follow-up screenshot smoke after user-reported PM clipping:
  - `scratch\ui_light_pm_extrude_fix4.png`: Extrude PM OK/Cancel stay inside the left pane, default reference/offset-only fields stay hidden, and the active selector hint no longer clips.
  - `scratch\ui_light_ai_fix4.png`: AI pane examples, provider/model control, and light-mode frame borders render cleanly in the normal right task pane.
  - `scratch\ui_light_settings_fix3.png`: Settings remains readable in light mode with visible controls and pinned actions.
- Dark-theme regression follow-up:
  - Made `ui::applyTheme()` idempotent by resetting the ImGui style before applying the selected theme, preventing runtime toggles/repair calls from compounding sizes or carrying stale light colors.
  - Added a pre-frame theme guard in `app.cpp` that reapplies the saved/env theme if `WindowBg`, `ChildBg`, or `FrameBg` drift away from the active palette.
  - `scratch\ui_dark_sketch_ribbon_after_themefix.png`: Sketch ribbon, left pane, and AI task pane stay dark.
  - `scratch\ui_dark_ecad_after_themefix.png`: ECAD editor and AI chat stay dark, including assistant message surfaces.
  - `scratch\ui_dark_pm_extrude_after_themefix.png`: Extrude PM stays dark with OK/Cancel contained and conditional reference/offset rows hidden by default.
  - `scratch\ui_saved_theme_ecad_after_themefix.png` and `scratch\ui_saved_theme_pm_after_themefix.png`: persisted dark config path matches forced dark mode.
  - `scratch\ui_light_pm_extrude_after_themefix.png` and `scratch\ui_light_ecad_after_themefix.png`: light-mode PM/ECAD smoke still renders after the theme reset change.
- Follow-up verification:
  - `.\build.bat host` passed.
  - `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe -m agent.test_pm_assist` passed.
  - `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe -m engine.doc.test_ops` passed.
  - `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe -m engine.doc.test_extrude_ends` passed.
  - `git diff --check` passed with only existing CRLF conversion warnings.
  - The default system Python does not include `OCP`, so geometry tests need the app runtime venv.
- High-DPI screenshot chasing was deprioritized after user direction to focus on normal modes.

### Known Remaining Work
- Replace driver `GL_LINES` with analytic/screen-space line rendering.
- Add deeper UI screenshot gates for PropertyManager narrow/default/high-DPI states and default/opt-in Simulation/PCB visibility.
- Add Help Agent UI actions beyond answer text: temporary reveal of hidden PM rows, command/control highlighting, and menu opening.
- Plan and branch the later C++/CUDA kernel/core experiment after this usability tranche is stable.
