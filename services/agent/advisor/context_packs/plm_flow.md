# PLM vault workflow context pack
# Tools: plm_init, plm_open, plm_save, plm_checkpoint, plm_release, plm_history,
#        plm_restore, plm_compare, plm_where_used, plm_rollback, plm_status, plm_set_ai_mode

## Discipline
1. plm_checkpoint (named message) BEFORE risky multi-step edits or bulk imports.
2. Release path: plm_status → plm_where_used → plm_release (item_id + message). Never skip where_used on shared items.
3. Recovery: plm_history → plm_compare → plm_restore / plm_rollback with explicit target commit.
4. plm_set_ai_mode only when the vault policy requires it; leave user mode alone otherwise.
5. Hints use plain language — no git jargon to the user.
