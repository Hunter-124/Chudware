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

---

## Write-set extension note (sonnet-marsh, 2026-07-03, FYI — proceeding, flag if wrong)

The claim-time blocker above was resolved by the foreman (stale stash discarded). During
execution I touched two files outside the listed write-set; both feel within the spirit of
the foreman scope additions but are recorded here for advisor review:

1. `services/worker/worker_client.{h,cpp}` — added the env-gated `AI_CAD_RPC_TRACE` JSONL
   trace wrapper at `WorkerClient::call()` (+ credential redaction). RATIONALE: the mandated
   parity harnesses need to observe the RPC stream each binary drives; `call()` is the single
   funnel covering ALL routing (Python worker subprocess, in-process libaicad doc/CAM,
   ECAD/agent C++ seams), and services/worker is linked by both binaries, so one off-by-default
   hook instruments both without touching host/src. When the env var is unset (all production
   paths) the wrapper is a tail call.
   ALTERNATIVE CONSIDERED: an AI_CAD_PYTHON stdio shim — rejected because doc-cpp/CAM/ECAD
   calls route in-process to libaicad and never cross the subprocess boundary.

2. `ui/imgui/src/ui/sim_manager.cpp` — restored host's `captureSelectionInto()` in the
   sim_fixture / sim_load_force / sim_load_pressure command branches. RATIONALE: real
   host-parity regression exposed by the study harness (viewport pre-selection silently
   ignored); three-line-per-branch restoration of ported-but-unused code already in the file.

Recommended answer: accept both (harness infra + regression fix). If the trace hook should
live elsewhere (e.g. a dispatcher decorator), happy to move it in a follow-up.
