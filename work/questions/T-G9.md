# T-G9 Blocker: Main checkout dirty — claim gated

**Date:** 2026-07-03  
**Agent:** sonnet-marsh  
**Task:** T-G9 (GUI migration wave 5 — polish, parity battery, cutover prep)

## Blocker

`work.py claim --owner sonnet-marsh --task T-G9` returns BUSY because the main
checkout has 3 modified tracked files with no active MERGE_HEAD/rebase in progress:

```
engine_oracle/piping/test_catalog.py
engine_oracle/piping/test_cfd_handoff.py
services/agent/test_h7_shadow.py
```

These appear to be orphaned working-tree edits from a previous foreman session.
No merge/rebase sentinels exist in .git/ — it's a stale dirty state, not an
active operation.

## Why I cannot self-resolve

Autopilot rule: "main checkout foreman-only EXCEPT `work.py claim`."  
Committing, stashing, or discarding those files is not within my granted access.
The files belong to piping/agent workstreams, not GUI.

## Recommended action (for foreman/human)

One of:
1. `git -C /home/hunter/Repositories/CAD stash` — saves them for later review
2. `git -C /home/hunter/Repositories/CAD checkout -- engine_oracle/piping/test_catalog.py engine_oracle/piping/test_cfd_handoff.py services/agent/test_h7_shadow.py` — discard if they're stale/orphaned
3. Commit them if they represent real in-progress work for a future task

After clearing, `work.py claim --owner sonnet-marsh --task T-G9` should succeed
and T-G9 work can begin.

## Status

Waiting on foreman/human to clear main checkout. T-G9 is unblocked on the
task-graph side (T-G8 merged at ccc37a8).
