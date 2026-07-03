---
id: T-H2
title: Canonical doc serialization policy + semantic doc_diff tool
workstream: H
owner: sonnet-veda
status: done
branch: task/T-H2-canonical-serialization
day: 11
depends_on: []
lane: platform-infra
kind: new
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

- [x] Saving the same document twice yields byte-identical document.json; rebuild does
      not dirty the file when geometry is unchanged
      → `core/doc/stepplus.cpp::canonical_doc_json()` rounds floats to 6dp + sorted keys
      → byte-stability verified by test_doc_canonical.py::TestCanonicalDumpByteStability (3 tests)
- [x] doc_diff returns correct feature-level deltas for an edit-script test corpus,
      exposed as an MCP tool
      → `services/mcp/doc_diff_tools.py::doc_diff()` — 44 pytest tests all green
      → registered as MCP tool `doc_diff` via `register_doc_diff_tools(mcp)` in server.py
- [x] No behavioral change to existing docs (version-tolerant load)
      → float rounding is serialize-time only (in-memory doubles untouched)
      → load path (stepplus_open) unchanged; existing documents load as before
- [x] `graphify update .`; gate green (with known pre-existing exceptions)
      → graphify updated: 19176 nodes, 42714 edges, 915 communities
      → gate results (bash scripts/gate.sh, 2026-07-02):
        - check.py: FAIL-RECORDED (pre-existing feature_surface_audit + parity_audit
          red, non-fatal per gate.sh CHECK_PY_FATAL=0 — not this task)
        - core build (libaicad, OCCT ON): PASS — canonical_doc_json compiles clean
        - ui/abstract build + ctest: PASS
        - ctest (core): PASS (100%)
        - pytest stage: SKIP (gate box has no provisioned venv); run manually with
          the MCP venv instead: services/mcp/test_doc_canonical.py = 44/44 green
        - ui/imgui: FAIL — the PRE-EXISTING PanelField::selMulti break
          (property_manager.cpp:2265, documented in work/FOREMAN.md); not a T-H2
          regression. T-H2 introduces ZERO new gate failures.

## Driver notes (Fable, 2026-07-02)

- **Canonical-dump precedent already in-tree:** ECAD undo snapshots use sorted-keys
  compact dump (`core/ecad/session.cpp:93-99`) — generalize that policy. Rounding:
  µm for mm lengths (6 decimals), 1e-3 for degrees, applied at SERIALIZE time only
  (in-memory doubles untouched — solver precision must not change).
- **Save path:** `.step+` writer per `contracts/stepplus.md` (atomic .tmp-then-
  replace, `document.json` entry); `meta.json` timestamps are exempt from the
  byte-stability DoD (only `document.json` must be stable). Known noise sources
  documented in `docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §3.2 — that table is your
  checklist.
- **doc_diff:** both inputs are canonical JSON so the diff is pure Python/C++ tree
  walk keyed on stable ids (`f1/e1/c1/k1/m1`): features added/edited/removed,
  `params_changed [{feature, param, old, new}]`, sketch entity/constraint deltas,
  mate deltas, suppression flips. Ignore `refs` churn when the resolved target is
  unchanged. MCP registration mirrors any `register_*_tools()` module.
- **Consumers to keep in mind (don't build for them, just don't block them):**
  T-H1 correction traces, T-F17 change cards, T-BL10 PDM-lite, GSL `diff()`.
- **Test fixture idea:** take 3 samples from `corpora/gsl_dataset` (document.json
  is already emitted per sample by `tools/gsl_dataset_gen.py:324`), apply known op
  scripts via the engine, assert exact diff output.
