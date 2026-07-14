---
name: assess-model-state
description: Read-only health check of the open model — describe, measure, overlap, interference, interface_check, verify_battery — before editing or declaring done.
inject: auto
triggers:
  keywords: [assess, health check, what is wrong, model state, status of, inspect the, review the part]
  tools: [describe_part, describe_assembly, measure_part, verify_battery, check_feature_overlap, check_interference, interface_check]
  phases: [part_verify, deliver]
---

# Assess Model State

Read-only pass. Prefer this before large edits and always before "done".

## Sequence

1. Scope: `list_parts()` — which docs are open? Pick the target `doc_id`.
2. Narrative geometry:
   - Part: `describe_part(doc_id)` (and `describe_sketch` if sketch-heavy)
   - Assembly: `describe_assembly(doc_id)` after `solve_mates` if mates exist
3. Metrics: `measure_part` / `query_geometry` for critical dims.
4. Structure checks:
   - `check_feature_overlap(doc_id)` on parts
   - `check_interference(doc_id)` on multi-body/assembly
   - `interface_check(doc_id)` for alignment (patterns, coaxiality, flush faces)
5. Full gate: `verify_battery(doc_id)` — narrate PASS/WARN/FAIL per check.
6. Summarize: what is healthy, what blocks shipping, what to fix next (ordered).

## Rules

- Do not mutate geometry in an assess pass unless the user asked for fixes.
- Verify-before-done still applies when assessment is the last step of a build.
- Cite tool evidence; never invent masses, clearances, or pass/fail.

## Deeper docs

- `cad_guide('review')`, `cad_guide('verify')`, `cad_guide('gsl')`
