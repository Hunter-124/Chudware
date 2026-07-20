---
name: plm-workflow
description: PLM vault discipline — checkpoint before risk, release with where_used, restore/rollback with history.
inject: auto
triggers:
  keywords: [checkpoint, release, revision, where used, where-used, rollback, change order, vault]
  tools: [plm_checkpoint, plm_release, plm_where_used, plm_status, plm_history, plm_restore]
  phases: [deliver]
---

# PLM Workflow

## Sequence

1. **Open/init** — `plm_open` / `plm_init` on the project folder.
2. **Checkpoint** — `plm_checkpoint` with a clear message before bulk or risky edits.
3. **Status** — `plm_status` before release decisions.
4. **Release** — `plm_where_used` then `plm_release` (item_id + message).
5. **Recover** — `plm_history` → `plm_compare` → `plm_restore` / `plm_rollback`.

## Rules

- Never skip where_used on shared items at release.
- Speak plain language to the user (no git jargon).
- Ledger checkpoint/release commit ids as artifacts.
