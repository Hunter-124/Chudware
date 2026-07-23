# PORT_LEDGER.md — ui/imgui retirement reference

**Purpose:** A permanent record of every significant feature that existed ONLY in
`ui/imgui/` (AI-CAD2) before its deletion. If any of these need porting to the
`host/` GUI in the future, use `git show <commit>` on the commits listed here to
recover the implementation.

**Deletion date:** 2026-07-06  
**Active GUI going forward:** `host/` — binary at `host/build-linux/bin/Chudware`

---

## Features unique to ui/imgui (not in host/)

### Workspace framework (tabbed workspace switcher)
Multi-workspace architecture: Model, Simulate, Manufacture, Electronics, PLM,
PartsLibrary, AI workspaces — each with its own full-viewport panel, title-bar
chrome strip, and section-switcher. The workspace chip strip lived in the title bar.

Key commits:
- `8ae88186` wip(sim): sim-manager WIP (last buildout checkpoint)
- `c8245197` wip(integrate): workspace-bar edits checkpoint
- `e9089739` fix(integrate): workspace-tab scenarios for all buildout tracks
- `fc13d35a` feat(chrome): workspace chip strip in title bar, centered
- `53c2c114` feat(workspace): per-workspace stub files + viewport.cpp wiring
- `496c497c` feat(workspace): section-view switcher in doc tab strip
- `e976a5eb` feat(workspace): Simulate ref-geom, globalWorkspaces strip
- `ce9a4326` feat(workspace): add Manufacture + PartsLibrary to Workspace enum

### Kinematic Motion Study (Simulate workspace)
Full end-to-end kinematic simulation: sim manager, body-transform keyframes,
timeline scrubber, workspace overlay chrome.

Key commits:
- `507a444a` feat(sim): kinematic motion study — full end-to-end (b)+(c)
- `da474934` feat(workspace): viewportSetBodyTransforms for motion study
- `99c6a226` feat(workspace): Simulate title-bar chrome (workspace_sim.cpp)
- `8ae88186` wip(sim): sim-manager WIP

### Parts Browser / Vendor Pane (PartsLibrary workspace)
Category nav rail, two-column layout, background download queue, Import button,
spec-extraction results view, vendor sources panel.

Key commits:
- `b0f85cfb` feat(parts): category nav rail + two-column layout
- `cd03a678` feat(parts): queue Import button + spec-extraction results
- `46ac6ba9` feat(xdoc): wire postToastAction for CAM stale-part notification

### Manufacture (CAM) Workspace
Full CAM workspace: header strip, op editor, setup sheet, part↔CAM section
switcher resolver, cross-doc dependency registry, stale-part detection.

Key commits:
- `ffe49dc0` feat(cam): fill Manufacture workspace — header strip, op editor, setup sheet
- `9e1acc2e` feat(cam): part↔CAM section switcher resolver (c)
- `9bc94190` feat(xdoc): cross-doc dependency registry + CAM stale detection
- `0a571728` feat(workspace): section switcher — CAM chip + Board chip + camActivateForPart

### ECAD Workspace (Electronics workspace)
ECAD workspace landing page, header chrome, Board-to-3D handler.

Key commits:
- `1fcbb950` feat(ecad): ECAD workspace landing page + header chrome + Board-to-3D handler
- `8352394a` fix(ecad): 4 post-commit review fixes

### PLM In-Editor Strip (PLM workspace)
In-editor PLM strip at the bottom of the Feature Manager.

Key commits:
- `c51030e7` feat(plm): in-editor PLM strip (T-R5)
- `f5a7b876` Merge T-R5 PLM in-editor GUI + wiring
- `19c10666` fix(T-R5): icon-arg hoist for gui_lint

### 3D Free-Space Sketcher (ws_sketch3d workspace)
3D sketching outside of a flat plane — new workspace, first slice.

Key commits:
- `22ca622d` feat(sketch3d): new 3D free-space sketch workspace (first slice)

### AI-Agent Full-Viewport Workspace (ws_ai_agent)
Mission DAG centerpiece, agent inspector, watch grid, agent settings panels,
multi-file agent panel, design pipeline streaming panel.

Key commits:
- `b5cc11e8` feat(T-DP16): AI-Agent full-viewport workspace shell + mission-DAG centerpiece

### Cross-Doc Dependency Registry
Tracks part↔CAM document links; drives stale-detection toasts.

Key commits:
- `9bc94190` feat(xdoc): cross-doc dependency registry + CAM stale detection
- `46ac6ba9` feat(xdoc): wire postToastAction

### Chrome Title-Bar Chip System
Workspace chips embedded in the title bar (not a sidebar). Includes section
switcher chips for CAM+Board+Simulate.

Key commits:
- `fc13d35a` feat(chrome): workspace chip strip in title bar, centered
- `0a571728` feat(workspace): section switcher chips

### Animated FM Selection Border (T-BL36)
Animated accent border for Feature Manager row selection/hover.

Key commits:
- `6ac4878d` Merge T-BL36 animated selection border
- `e17cad0e` feat(gui): T-BL36 animated accent border

### Headless Screenshot Scenario Harness (21+ CHUDWARE_SHOT scenarios)
The full scenario/shot system: `ui/imgui/src/ui/scenarios.cpp`. Every named
`CHUDWARE_SHOT=<name>` environment variable seeds fixture state and writes a PNG.

Named scenarios (all in `scenarios.cpp`, parseable via `grep "eq("`):
`settings`, `chat`, `assembly`, `plan`, `pick`, `ribbon`, `origin`, `tree`,
`explorer`, `equations`, `m1`, `multidoc`, `iconpipe`, `ecad`, `sketch`,
`sketch_dispatch`, `sketch_autohide`, `sketch_convert`, `fea`, `fea_overlay`,
`sim_material`, `sim_precheck`, `sim_postcheck`, `cam`, `flow`, `drawing`,
`evalmass`, `activity`, `asm`, `pmform`, `preview`, `scaffold_demo`,
`parts_browser`, `bom`, `requirements`, `transparency`, `dp_stream`,
`dp_stream_empty`, `piping_view`, `hydraulic_view`, `structural_view`, `gallery`,
`question`, `mission_control`, `agent_dag`, `agent_dag_compact`,
`ws_overlay_hosting`, `ws_full_viewport_ecad`, `ws_ai_agent`, `ws_plm`,
`ws_sketch3d`, `ws_simulate`, `ws_manufacture`, `ws_parts`, `ws_motion_study`,
`agent_inspector`, `agent_inspector_empty`, `agent_inspector_failure`,
`agent_inspector_delegation`, `watch_grid`, `agent_multifile`, `agent_settings`,
`agent_settings_error`, `sim_studies`, `settings_general`, `settings_ai`,
`settings_prompts`, `settings_advanced`, `composer_dropdown`, `permissions_popup`,
`ai_effort_simple`, `ai_effort_advanced`, `ai_effort_warn`, `ai_effort_auto`,
`glassperf`, `cam_workspace`.

Key commit for scenario harness: `e9089739`, `c8245197`, `8ae88186`

### Simulation Studies Panel (T-U6)
Separate floating sim-studies panel with fixture seeding.

Key commit: `6b0bffc3` feat(sim): transparent per-stage sim progress in status bar

### Windows MSVC Build Support (T-WIN1)
CMake /FI fix_math.h force-include for Ninja+MSVC, WIN32 link block.

Key commits:
- `026c98a6` fix(T-WIN1): unblock ui/imgui windows-app build+runtime on MSVC/Ninja
- `6aa290db` feat(T-WIN1/X1): MSVC /FI fix_math.h parity in CMake
- `88fa73bc` Merge T-WIN1 windows bringup

---

## ui/abstract — also deleted

`ui/abstract/` contained the `command_registry`, `panel_spec`, `artifact_view`
seam headers used by ui/imgui panels. The command registry tests lived here too.

These were a framework-agnostic view-model layer. host/ does not use ui/abstract.

Key commits touching ui/abstract:
- Any commit touching `ui/abstract/command_registry.cpp`, `panel_spec.cpp`,
  `artifact_view.h` — search with `git log --all -- ui/abstract`.

---

## How to recover any feature

```sh
# See full diff for a commit
git show <commit-hash>

# Recover a single file as it was at that commit
git show <commit-hash>:ui/imgui/src/ui/<file>.cpp > /tmp/recovered.cpp

# Browse the deleted tree at its last known state (8ae88186)
git ls-tree -r 8ae88186 -- ui/imgui | head -40
```
