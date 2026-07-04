# T-BL48 — duplicate task-id collision (needs foreman/human bookkeeping)

**Driver:** opus-tarn · **Date:** 2026-07-04 · **Recommendation below is safe to adopt as-is.**

## The collision

There are TWO task files both claiming id **T-BL48**, and TWO branches, for the same
underlying work (wire ui/imgui interactive sketcher entry):

| Task file | Branch | Owner | Status | Scope |
|---|---|---|---|---|
| `work/backlog/T-BL48-wire-imgui-sketch-entry.md` (cut by fable-helm) | `task/T-BL48-wire-imgui-sketch-entry` | opus-tarn | doing | **superset**: ribbon entry + **feature-tree edit-existing-sketch** + exit + extrude |
| `work/backlog/T-BL48-uiimgui-sketch-entry-wiring.md` (renamed from T-BL47 by opus-hunter) | `task/T-BL48-sketch-entry-wiring` | opus-hunter | review | ribbon dispatch entry + plane-pick + exit only (no feature-tree edit) |

The board (`work.py board`) shows the fable-helm task file as the canonical T-BL48
(owner opus-tarn). opus-hunter's branch was cut earlier the same session (renumbered
from T-BL47) and is already pushed at `status: review`, sitting in worktree `../CAD-T-BL47`.

## What I did (does not need a decision — it's the low-risk path)

Per advisor ruling: I did NOT reimplement hunter's entry wiring. I **based my branch
`task/T-BL48-wire-imgui-sketch-entry` on hunter's tip** (`origin/task/T-BL48-sketch-entry-wiring`)
and added only the missing superset delta:
- feature_tree.cpp: filled the 3 deferred stubs (`Edit Sketch` / `Edit Sketch Plane/Face`
  ctx-menu + double-click on a sketch row) → they now push a target fid to AppState.
- AppState: `pendingSketchEditFid` / `pendingSketchReparentFid` (fid-carrying pending
  fields; hunter's `pendingSketchCmds` is command-ids only and can't carry a target fid).
- viewport.cpp drain: calls `s_sketch.enterForFeature(fid)` / `beginPlaneEdit(fid)`.
- Removed the dead `feature_tree.cpp:315 void* sketchMode=nullptr` placeholder — the
  queue-push pattern IS the shared-state handoff, so the placeholder is superseded.

So my branch **contains all of hunter's commits plus my delta**. It survives either
merge outcome.

## Recommended resolution (for foreman / human at merge time)

1. **Adopt ONE canonical T-BL48 = the fable-helm superset task file**
   (`T-BL48-wire-imgui-sketch-entry.md`). Delete/supersede hunter's
   `T-BL48-uiimgui-sketch-entry-wiring.md` (I kept both files in-tree on my branch so the
   history is visible; the integrator should collapse to one).
2. **Merge my branch, not hunter's** — mine is a strict superset (his commits are in my
   history, credited: `7c542881`, `f10075cf`, `b1fa93b9`). Merging both would double-apply
   the entry wiring and conflict. Close hunter's `task/T-BL48-sketch-entry-wiring` review
   as superseded-by / folded-into my branch.
3. If foreman would rather land hunter's branch FIRST and rebase mine: my delta is
   isolated to feature_tree.cpp + the new AppState fields + the viewport drain extension —
   a clean rebase onto master-with-hunter's-branch leaves only my delta.

**Open item that gates my review flip:** I need to know hunter's branch's merge fate
(merge-mine-superset vs land-his-then-rebase) so I set the right final rebase base.
Credit for the entry wiring is opus-hunter's; my contribution is the feature-tree edit
half + the end-to-end extrude verification.

opus-hunter notified via team_send.
