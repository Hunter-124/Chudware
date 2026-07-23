---
id: T-RF10
title: RF wave-2 GUI feedback batch — RESCOPED 2026-07-22 (original 15 items already landed)
workstream: RF
owner: unassigned
status: done
branch: task/T-RF10-gui-feedback-batch
depends_on: [T-RF5, T-RF6]
lane: app-gui
kind: fix
priority: 3
---

## Context

RESCOPED at dispatch prep: the 15-item batch in docs/plans/2026-07-16-gui-session.md is
ALREADY DONE — its status log records every wave committed (0d717a1a, ba9b1f77, f18d16b1,
16d3a108, d4f4033f, a94fa0eb, 1f4875ef + batch 8). What actually remains:

1. docs/GUI_OVERHAUL_SPEC.md pending `[~]` items (menu removal, chip styling — verify
   which are still open).
2. The gui-session doc's own Deferred section: item 9 full nested history for imported
   parts (needs an engine RPC `doc_import_feature_tree` — engine work, belongs to the
   Z-stream/T-RF19 evaluation, not GUI) and the optional item 5b/6b contract note in
   feature_params.md (append-only contract edit, cheap).

## Scope

Audit GUI_OVERHAUL_SPEC.md `[~]` items: fix what is still open (small GUI fixes,
screenshot-verified with AI_CAD_SHOT), close what is already done with a note. Append the
feature_params.md defaults note (integrator-owned contract). Ensure the
doc_import_feature_tree gap is captured for T-RF19's evaluation (do not build it here).

## Definition of done

- [x] Every GUI_OVERHAUL_SPEC `[~]` item is either fixed + shot-verified or marked done
      with evidence in the spec.
- [x] feature_params.md defaults note appended.
- [x] doc_import_feature_tree recorded in T-RF19's scope notes.
- [x] gate.sh green; REFACTOR_LOG handoff.
