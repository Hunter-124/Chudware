---
id: T-U1
title: UI kit: style-conformance scaffold + pattern library for all new feature panels
workstream: U
owner: sonnet-reed
status: done
branch: task/T-U1-ui
day: 1
priority: 1
depends_on: [T-G6]
lane: app-gui
kind: new
---

## Context

PROJECT-LEAD RULING (Fable, 2026-07-03, Erik-directed): many new backends (parts
federation, CAM overhaul, BOM/sourcing, requirements, sim studies, new doc kinds,
model serving) each need UI — built by DIFFERENT agents. Without a conformance kit
they will produce eight styles. T-U1 builds the kit every T-U* task MUST use:
the locked design language is liquid-glass v2 (docs/GLASS_DESIGN_SPEC.md; pinned
glass columns over full-width viewport, dynamic color default ON, docked layout is
DELETED — ratified, do not re-litigate).

## Files / areas touched

- docs/UI_PATTERN_LIBRARY.md — NEW: the panel cookbook (glass column anatomy, tokens.h
  usage, spacing/typography rules, list/detail/table/card/status patterns, empty-state
  + placeholder vocabulary from T-F16's previewStateFromReason precedent, iconography,
  do/don't gallery w/ screenshots)
- ui/imgui/src/ui/panel_scaffold.{h,cpp} — NEW: reusable glass-panel scaffold (header,
  search row, content region, status footer) all new panels instantiate
- ui/abstract/ — view-model-first rule: every panel's state is pure structs here
  (artifact_view.h is the exemplar), ImGui code renders it
- scripts/ui_style_check.py — NEW: greppable conformance checks (no literal colors
  outside tokens/theme, no OS ifdefs outside platform/, view-model header exists per
  panel) wired as an advisory gate stage

## Definition of done

- [x] Pattern library doc w/ at least 5 reusable patterns, each with a code pointer
- [x] panel_scaffold compiles + one demo panel built on it (screenshot, headless)
- [x] ui_style_check.py green on the existing shell; catches a seeded violation in test
- [x] T-G12 inventory script picks up scaffold-built panels automatically
- [x] flock'd build + gate + graphify update .

## Driver notes (Fable)

- READ docs/GLASS_DESIGN_SPEC.md + ui/abstract/artifact_view.h + preview_pane.* (T-F16,
  the best current exemplar) BEFORE writing the doc — codify what exists, don't invent.
- The kit is the coherence contract for T-U2..T-U8: write it for a mid-tier agent
  audience (explicit, example-heavy, zero taste-required decisions left open).
- NO VISIBLE WINDOWS (headless shots only). host/ is deprecated — imgui only.

## Driver report (sonnet-reed)

Kit landed exactly as scoped, derived from the existing T-F16 exemplar (`artifact_view.h` +
`preview_pane.cpp`) rather than inventing new conventions:

- `docs/UI_PATTERN_LIBRARY.md` — 7 patterns (glass column anatomy / view-model-first / row
  pills / cards / typed empty-state / tables / iconography) + a do/don't gallery, each with a
  real code pointer. Explicitly calls out the T-G13/issue #7 step-machine deadlock and directs
  new panels to the one-shot scenario style instead.
- `ui/imgui/src/ui/panel_scaffold.{h,cpp}` — header + optional search row + scrollable content
  + optional status footer, built entirely from existing `theme::` glass APIs (no new theme
  surface). `drawPanelEmptyState()` generalizes the `previewStateFromReason`/`previewStateLabel`
  placeholder precedent for scaffold panels.
- `ui/abstract/scaffold_demo_view.h` + `ui/imgui/src/ui/scaffold_demo_panel.{h,cpp}` — the demo
  panel proving the kit end-to-end (view-model-first, search filter, row pills, empty state,
  status footer). Wired as a one-shot scenario (`CHUDWARE_SHOT=scaffold_demo`, added to
  `ShotConfig`/`configureShot`), not a `ScenarioRunner` step machine, so it's unaffected by the
  T-G13 offscreen-deadlock issue. Floats centered (`ImGuiCond_Appearing`, `equations_dialog.cpp`
  pattern) rather than dock-registered — it's a kit demo, not a shipped feature.
- `scripts/ui_style_check.py` + `scripts/ui_style_check_baseline.json` — three rules (literal
  colors / OS ifdefs / view-model header), baseline-absorbs the 18 genuine pre-existing findings
  (17 literal colors in `evaluate_dialogs.cpp`/`feature_tree.cpp`/`property_manager.cpp`/
  `settings_modal.cpp`, 1 `#ifdef _WIN32` in `preview_pane.cpp`) so it gates only new drift, same
  pattern as `scripts/gui_lint.py`'s baseline. `--selftest` seeds one violation per rule in a
  tempdir plus a conformant control panel and asserts all three fire / zero false positives —
  ran green.
- `scripts/ui_inventory.py` — new `parse_panel_scaffold_panels()` regex-scans every
  `ui/imgui/src/ui/*.cpp` for `PanelScaffoldSpec` instances (field-assignment or aggregate-init
  style) and emits a "Scaffold-built Panels" section — verified it auto-picked up
  `scaffold_demo` with zero manual registration.
- `scripts/gate.sh` — new advisory `stage_ui_style_check` (same non-fatal pattern as the T-Q4
  manual-regen stage), wired into the stage sequence before `stage_core_build`.

**Verified:** `flock /tmp/host-build-linux.lock` build of `build/linux-app` (AI-CAD2) clean;
headless `xvfb-run ... --shot CHUDWARE_SHOT=scaffold_demo` screenshot confirms the scaffold header,
search row, three row-pill rows, and status footer all render correctly (glass row-pill z-order
verified — background drawn before content, per the pitfall documented in the pattern library).
Fixed one runtime bug found via the screenshot: an em-dash in the demo's status string rendered
as `?` (font glyph-range gap) — replaced with a plain hyphen; UI-drawn strings should avoid
non-ASCII punctuation until the font atlas is audited for it (worth a follow-up note for T-U2+
authors, not filing a separate task for one string).

**Gate (`scripts/gate.sh`):** check.py FAIL-RECORDED (pre-existing, unrelated — ribbon/parity
audits), `ui_style_check.py` PASS, core build + ctest 100% PASS, ui/abstract PASS, ui/imgui build
+ `--shot` PASS (`--selftest` FAIL — pre-existing, no runtime venv provisioned on this box, same
env gap as `pytest` below), `pytest` FAIL — pre-existing gate-venv gap (141 deselected, "Python
runtime not installed"), the same issue #6 / T-INF1 gap other drivers have recorded repeatedly
this session; T-U1 touches no engine/Python code. `graphify update .` run (25493 nodes / 53181
edges / 1190 communities).

No advisor questions — this task was fully spec'd (derive-don't-invent from an existing
exemplar), no architectural ambiguity hit.

## Progress log
- 2026-07-02 22:26 UTC [sonnet-reed] REVIEW T-U1 branch=task/T-U1-ui — UI style-conformance kit: docs/UI_PATTERN_LIBRARY.md (7 patterns w/ code pointers, derived from T-F16 artifact_view.h/preview_pane.cpp), panel_scaffold.{h,cpp} (header/search/content/footer on existing theme:: glass APIs), demo panel (ui/abstract/scaffold_demo_view.h + scaffold_demo_panel.{h,cpp}) wired as one-shot CHUDWARE_SHOT=scaffold_demo (not a ScenarioRunner step machine, so unaffected by T-G13/issue #7), scripts/ui_style_check.py (literal-color/OS-ifdef/view-model-header rules, baseline absorbs 18 genuine pre-existing findings, --selftest green), scripts/ui_inventory.py auto-discovers PanelScaffoldSpec panels with zero manual registration (verified against scaffold_demo). Wired ui_style_check.py into gate.sh as an advisory stage (T-Q4 manual-regen pattern). Verified: flock'd build/linux-app build clean, headless --shot screenshot confirms header/search/row-pills/footer render + correct z-order (bg-before-content pitfall documented in the pattern lib); caught+fixed a runtime bug via the screenshot (em-dash rendered as '?' — font glyph-range gap, replaced with hyphen). Gate: core build+ctest 100% PASS, ui/imgui build+shot PASS, ui_style_check.py PASS; pytest+selftest FAIL-RECORDED are the pre-existing issue #6/T-INF1 no-venv gap (task touches no engine/Python code). graphify updated (25493 nodes/53181 edges). T-U2..T-U8 can now build on panel_scaffold.h.
