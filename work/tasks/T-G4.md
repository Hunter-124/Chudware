---
id: T-G4
title: GUI dead-code and cleanliness pass on host/
workstream: G
owner: sonnet-oda
status: done
branch: task/T-G4-gui-dead-code
day: 2
depends_on: [T-G1]
---

## Context

The overhaul left a cleanliness debt list (`docs/HANDOFF_GUI_OVERHAUL.md` §5 items 2, 6,
7): the settings-reorg + dead-code workflow phases may not have run, audit P2 findings
were capped, and the AI provider/model picker is drawn in three places. This pass keeps
`host/` honest while it remains the production GUI — but do NOT gold-plate: `host/` is
EOL after the ui/imgui cutover (T-G9/T-G11), so fix what misleads or double-renders,
delete what's dead, and skip cosmetic refactors that the migration will subsume.

## Files / areas touched

- `host/src/ui/settings_modal.cpp`, `host/src/ui/legacy_chat.cpp` (picker unification check)
- `host/src/ui/**` dead code flagged by the reorg phase spec
- `host/src/ui_theme.cpp` (variance-scrim check only — report, don't implement; see T-BL13)

## Steps

1. Check whether the overhaul workflow's Reorg/dead-code phases ran
   (`docs/HANDOFF_GUI_OVERHAUL.md` §2 tells you how). If they ran, verify; if not,
   execute their specs.
2. Provider/model picker: confirm the three entry points (settings AI tab, AI dock tab,
   composer popover) share `drawProviderModelPicker` with no divergent copies; unify if not.
3. Composer "Permissions" popup checkboxes: verify they SAVE (was a known bug — fixed?).
4. `grep -rc panelsOverlay host/src/` must stay all-zero (docked layout stays deleted).
5. Sweep audit P2 leftovers (triad dot labels, legend captions on pre-v2
   glassBackground, toast level-tag contrast, hardcoded sim/flow/CAM overlay literals) —
   fix cheap ones, file the rest as backlog notes in this task file.
6. Delete dead code found along the way (unused draw paths, orphaned settings keys).
   Rebuild (flock'd) + smoke screenshots after each deletion batch.

## Definition of done

- [x] Reorg/dead-code phase specs verified-or-executed
      — Workflow forensics (run wf_b02e1448-8d5 transcripts): the reorg:settings and E3
        agents DIED on session limit; verify-dead agents died before verdicts; remove-dead
        never ran. Settings-reorg OUTCOME verified in place anyway: modal has the target
        tab set (Appearance/General/AI/Prompts/Agent/Experimental, settings_modal.cpp:272-733).
        Dead-code phase EXECUTED by this task: recovered the 19-finding audit
        (work/questions/T-G4-deadcode-audit.json), re-verified every claim by per-symbol
        grep across host/ ui/ scripts/ CMakeLists against CURRENT code (4 claims found
        stale and skipped: titleFramePad*, dockRightTarget/Min now live; dockLeft* already
        gone; drawRunModeMenu already deleted), deleted the confirmed-dead in 3 commits
        (~224 lines): unreferenced functions, write-only Palette roles, unused metrics.h tokens.
- [x] One shared provider/model picker implementation
      — Single impl `drawProviderModelPicker` (legacy_chat.cpp:1734); 2 call sites:
        settings AI tab (settings_modal.cpp:568) + composer settings popover
        (legacy_chat.cpp:2164). No divergent BeginCombo copies (grep). The old third
        entry point (AI dock tab) no longer exists as a separate copy.
- [x] Permissions popup persists its checkboxes
      — legacy_chat.cpp:2877-2890: offlineMode and planMode both call saveSettings()
        immediately on change. "Show assistant thoughts" (gShowThinking) is session-only
        BY DESIGN (DESIGN §5 comment at legacy_chat.cpp:2192) — not a bug.
- [x] `panelsOverlay` grep still zero; build green; smoke shots taken
      — grep -rc panelsOverlay host/src/ = all zero. Build green after each of the 3
        deletion commits (flock'd). Scenario shots m1 (6 steps), sketch (3), appearance (1)
        + fea final frame, all exit 0; eyeballed m1_05_edit.png + appearance_01_settings.png:
        FM tree rows, PM panel, Copilot bubbles, Settings tabs all render correctly.
        (Scenario harness in a fresh worktree needs services/dist/AI-CAD + bin libaicad.so
        staged from the main repo and AI_CAD_REPO=$PWD — gitignored, done locally.)
        NOTE: m1 reopen step logs "TREE MISMATCH" + STEP parse errors — pre-existing stale
        staged .so (FOREMAN: "staged .so predates sim_run_static"), not this branch.
- [x] `graphify update .` run

## T-G4 backlog notes (P2 leftovers filed, not fixed — host/ is EOL at T-G11)

1. sim_manager.cpp ~2779-2784: stress-plot trace arrays tcol[5]/tdim[5] still literal RGB
   (yieldCol already migrated to Palette::toU32). Data-series colors with no palette roles;
   left as-is — ui/imgui migration will subsume.
2. E3 hooks (import-summary toast, "Reverse-engineer import" chip, ai_reverse_engineer /
   ai_explain_part palette commands) NEVER landed — the workflow's E3 agent died on session
   limit. Zero traces in host/src (grep). Decide: re-target to ui/imgui (T-G5+) or drop.
3. app.cpp ~1946/2012/2057: JOB_BUILD/JOB_CHAT job-completion branches are unreachable
   (JOB_CHAT never assigned; JOB_BUILD only as JobState default init) — left in place per
   the audit's conservative alternative; simplification is behavior-adjacent.
4. Triad dot labels / legend captions on pre-v2 glassBackground / toast level-tag contrast:
   not swept this pass (capped-P2 visual polish; migration subsumes). Audit finding 18
   confirms glass-v1 helpers still have live callers — they are NOT dead code.
5. Variance-scrim (T-BL13 report-only check): SHIPPED — ui_theme.cpp:868 dynVar tracks
   stats.variance, kDynVarBoostGain/Max (ui_theme.cpp:~635) raises scrim on busy backdrops.
6. Audit artifacts preserved: work/questions/T-G4-deadcode-audit.json (the recovered
   19-finding audit), work/questions/T-G4-survey.md (fresh state survey).

## AI hints

- graphify does NOT index `host/src`; grep is expected.
- Full P2 lists live in the overhaul run transcripts (`HANDOFF_GUI_OVERHAUL.md` §2 paths).
- Don't re-litigate locked design decisions (transparent columns override "pinned glass
  columns" — see agent memory gui-overlay-design-decisions).
