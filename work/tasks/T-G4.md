---
id: T-G4
title: GUI dead-code and cleanliness pass on host/
workstream: G
owner: sonnet-oda
status: doing
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

- [ ] Reorg/dead-code phase specs verified-or-executed
- [ ] One shared provider/model picker implementation
- [ ] Permissions popup persists its checkboxes
- [ ] `panelsOverlay` grep still zero; build green; smoke shots taken
- [ ] `graphify update .` run

## AI hints

- graphify does NOT index `host/src`; grep is expected.
- Full P2 lists live in the overhaul run transcripts (`HANDOFF_GUI_OVERHAUL.md` §2 paths).
- Don't re-litigate locked design decisions (transparent columns override "pinned glass
  columns" — see agent memory gui-overlay-design-decisions).
