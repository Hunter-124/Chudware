---
id: T-G1
title: Land the uncommitted GUI working tree in scoped commits
workstream: G
owner: main-checkout
status: done
branch: task/T-G1-land-gui-tree
day: 1
depends_on: []
lane: app-gui
kind: overhaul
---

> **Landed 2026-07-02 by direct user approval (session): commits ac45df9 + 7a61230; m1 reopen TREE MISMATCH filed separately.**

## Context

The main checkout carries two sessions of verified-but-uncommitted GUI work on `master`:
the GUI-overhaul Commit A set (`docs/HANDOFF_GUI_OVERHAUL.md` §6) and the glass-polish
session's 7-file set with its 6-piece commit plan (`docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md`
§COMMIT PLAN). Everything downstream in workstream G builds on top of this tree, so it
must land first, in reviewable pieces — not one blob.

**SPECIAL TASK — not for autopilot worktree agents.** The dirty files only exist in the
main checkout's working tree; a fresh worktree cannot see them. Run this in the main
checkout, human-supervised (owner stays `main-checkout`). Commits land directly on
`master` per the already-approved commit protocol in `HANDOFF_GUI_GLASS_POLISH.md` §3.

## Files / areas touched

- `host/src/**` (modified files only — see `git status`)
- Explicitly EXCLUDED (other in-flight lanes, never scoop): `ui/abstract/panel_spec.*`,
  `services/mcp/**`, `docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md`, `docs/REFACTOR_LOG.md`

## Steps

1. Verify green build first: `flock /tmp/host-build-linux.lock -c "cmake --build host/build-linux -j$(nproc)"`.
2. Re-shoot smoke scenarios (`AI_CAD_SHOT=m1|sketch|fea|appearance`) and eyeball them.
3. Commit the glass-polish set in the 6 pieces listed in
   `docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md` §COMMIT PLAN (`git add <exact files>`, by hunks
   where a file spans pieces).
4. Commit the remaining pre-existing GUI-overhaul host/src edits as Commit A per
   `docs/HANDOFF_GUI_OVERHAUL.md` §6 (append the GUI-lane paragraph to
   `docs/REFACTOR_LOG.md` first; commit the log separately at the very end).
5. `git status` after: only the excluded lanes' files may remain dirty.

## Definition of done

- [ ] Build green + smoke screenshots eyeballed before any commit
- [ ] Glass-polish set committed in logical pieces per the commit plan
- [ ] GUI-overhaul Commit A set committed
- [ ] No excluded-lane files scooped into any commit (`git show --stat` spot-check)
- [ ] `graphify update .` run

## AI hints

- The full reasoning trail for every piece is in `docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md`
  and `docs/HANDOFF_GUI_OVERHAUL.md` §3/§6.
- Commit-message style: short imperative summary, no emojis, end with
  `Co-Authored-By: Claude <noreply@anthropic.com>`.
