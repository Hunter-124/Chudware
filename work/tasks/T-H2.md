---
id: T-H2
title: Canonical doc serialization policy + semantic doc_diff tool
workstream: H
owner: unassigned
status: todo
branch: task/T-H2-canonical-serialization
day: 11
depends_on: []
---

## Context

Pillar 16 / IR review (`docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §3.2–3.3). Document
JSON is already a good model target; kill the remaining diff noise: float dust
(599.9999…), non-canonical key order, timestamps inside diffable payloads. Then build
`doc_diff(rev_a, rev_b)` — feature-level semantic diff (features added/edited/removed,
params_changed old→new, sketch/mate deltas). Consumers: correction traces (T-H1), GSL
`diff()` surface, mission-control change cards (T-F17), PDM-lite (T-BL10). Complements
T-F10's gsl-ser freeze (prompt serialization) — this task owns the DOCUMENT side.
ECAD's sorted-keys canonical dump is the precedent to generalize.

## Files / areas touched

- `contracts/doc_schema.md` — canonical dump appendix: fixed rounding (µm-grade lengths,
  1e-3 deg), sorted keys, no timestamps in document.json
- doc save/serialize paths (app engine + core sessions) — apply policy on write
- `core/doc/` or `services/` — `doc_diff` implementation + MCP tool registration
- tests: round-trip byte-stability, diff correctness on known edit scripts

## Definition of done

- [ ] Saving the same document twice yields byte-identical document.json; rebuild does
      not dirty the file when geometry is unchanged
- [ ] doc_diff returns correct feature-level deltas for an edit-script test corpus,
      exposed as an MCP tool
- [ ] No behavioral change to existing docs (version-tolerant load)
- [ ] `graphify update .`; gate green
