---
id: T-R5
title: PLM in-editor GUI - history timeline panel, rev badges, checkpoint/release toolbar, compare view (all doc kinds)
workstream: R
owner: unassigned
status: todo
branch: task/T-R5-plm-editor-gui
depends_on: [T-R2, T-R3, T-U1]
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
- [ ] T-U1 style-conformance checks pass; AI_CAD_SHOT headless scenarios added
      for panel/compare/release flows; gate + graphify update .
