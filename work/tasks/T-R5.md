---
id: T-R5
title: PLM in-editor GUI - history timeline panel, rev badges, checkpoint/release toolbar, compare view (all doc kinds)
workstream: R
owner: fable-quill
status: doing
branch: task/T-R5-plm-editor-gui
depends_on: [T-R2, T-R3, T-U1]
lane: plm-projects
kind: new
priority: 3
---

## Context

PLM inside every editor ([docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md)
§4), built on the T-U1 style-conformance kit so it lands as ONE coherent app.
Ease-of-use rules are normative: no git vocabulary anywhere; the timeline reads
like a story (thumbnail + when + who + plain-language summary — T-R3's
summaries); restore/compare are right-click affordances, not modes; advanced
anything goes behind a disclosure. Covers part studio, assembly, drawing, ECAD
schematic/PCB, CAM workspace (T-U3), and the U7 viewers.

## Files / areas touched

- ui/ (imgui, post-G9 tree — NOT host/, which T-G11 deletes): history panel
  (timeline widget: thumbnail rows, hover preview, user/AI attribution mark,
  right-click restore/compare/checkpoint-from-here)
- Doc-tab revision badge: rev letter + lifecycle chip on tabs
  (ui doc_tabs successor); Released docs open read-only with prominent
  "Start rev C" action
- Main toolbar: Checkpoint + Release buttons with inline Copilot-drafted
  message (editable before commit; esc = plain manual box)
- Compare view: side-by-side 3D via eval_compare render path; drawings/ECAD as
  2D overlay; change list docked from plm_compare semantic output
- Session-vs-history clarity: panel splits "Session changes" (undo/redo) from
  "Project history" (persistent) — the two-timelines confusion killer
- **FM Save/Open strip replacement (Hunter, 2026-07-03):** the bottom-of-FM
  filename + Save/Open strip becomes the vault entry point — Save stays (cuts an
  auto-history entry per T-R2), Open relocates to the Project Hub/recents, and the
  strip gains Checkpoint + a compact "history" affordance (opens the panel).
  Restyle to the T-U1 kit with proper dark-mode contrast (T-G14 finding 7 fixes
  the current tokens first; don't ship a second bespoke style)

## Definition of done

- [ ] History panel functional in ALL doc kinds (part/asm/drawing/schematic/PCB/
      CAM) with real thumbnails + T-R3 summaries; AI entries visibly attributed
- [ ] One-click checkpoint with pre-drafted message; release flow runs T-R4
      pre-flight and shows results in plain language
- [ ] Restore from panel round-trips (edit → restore older → doc appears, new
      history entry appended); compare opens side-by-side + change list
- [ ] Rev badge on tabs; Released read-only + start-next-rev path works
- [ ] Zero git vocabulary audit: string scan of new UI text for
      commit/branch/merge/push/pull/repo = no hits
- [ ] T-U1 style-conformance checks pass; CHUDWARE_SHOT headless scenarios added
      for panel/compare/release flows; gate + graphify update .

## Locked decisions (Hunter, 2026-07-05 planning session)

- **Timeline density:** collapse auto-saves between checkpoints into an expandable
  group ("12 saves" row, thumbnails on expand); checkpoints + releases are the story rows.
- **Released-doc editing:** AUTO-BRANCH SILENTLY — first edit on a Released doc creates
  the next-rev draft behind the scenes; a toast announces it and MUST carry a
  "back to rev B (discard draft)" affordance so nobody is confused about where they are.
  No read-only wall, no prompt.
- **Part numbers:** auto-suggest from a configurable scheme (e.g. PRJ-00042) pre-filled
  at first release, freely editable in the field.
- **Checkpoint/release message drafts:** LOCAL model tier drafts from the T-R3 change
  summary (offline-safe, free); plain manual box fallback when no local model is
  provisioned. Frontier is never required for this.

## Progress log
- 2026-07-05 19:54 UTC [fable-quill] Python seam landed (7cc85e5d): services/plm/worker_rpc.py exposes plm_* over the engine-worker transport (same Vault impl as MCP tools) + T-R5 seams plm_scheme_set / plm_discard_draft / plm_draft (local-model tier w/ manual-box fallback, validated git-vocab-free + numeral-faithful). vault.set_part_number_prefix + vault.discard_working_rev added. 101 plm tests green (21 new). Next: ui/imgui panel + toolbar + tabs + strip.
- 2026-07-05 20:22 UTC [fable-quill] DRIVER DIED (session limit, resets Jul 10). Integrator preserved all work to origin/task/T-R5-plm-editor-gui: committed plm worker-RPC seam (7cc85e5d) + WIP commit rescuing uncommitted panel files (plm_panel.cpp/h, ui/abstract/plm_view.h, toast wiring). Does NOT build clean yet — mid-build. Needs a finisher to complete the panel + wire it in. Hunter's locked design decisions are in the task file.
- 2026-07-06 [opus-dp19 finisher] Merged T-R5 onto current master (post-host-deletion, post-T-DP16). Clean merge — no source conflicts. Wiring the panel into application.cpp + mounting drawPlmPanel into the PlmManagement workspace shell (drawPlmShell) + registering ws_plm shot seams. Landing on master per Hunter's directive.
