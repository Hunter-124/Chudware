---
id: T-M4
title: CAM overhaul: feeds/speeds DB unification with engine materials registry
workstream: M
owner: unassigned
status: todo
branch: task/T-M4-cam-overhaul
day: 4
depends_on: []
---

## Context

CAM overhaul wave 1. Resolves GitHub issue #2: CuttingDB's hardcoded 25-material table is
disconnected from the shared engine/materials registry (T-F2) — feeds/speeds and FEA use
different material truths. Audit: work/audits/CAM_AUDIT.md §3.2, §T-M4.

## Files / areas touched

- core/cam/feeds.{h,cpp}, core/cam/session.cpp, the engine/materials registry schema
  (additive `cam` sub-section), services/mcp/cam_tools.py (cam_materials, cam_add_setup)

## Definition of done

- [ ] Materials registry schema gains an additive `cam` sub-section (sfm_mill/sfm_drill/
      sfm_turn/hss_factor/fz_ref/fz_ref_dia/stepover_frac/stepdown_frac/plunge_frac/cut_feed_ref)
- [ ] CuttingDB::builtin() loads from the registry with in-code fallback (headless build intact)
- [ ] cam_materials() reflects the registry's live material list; cam_add_setup accepts any
      registry material ID
- [ ] Values for the existing 25 materials preserved byte-identically in the migration
      (no silent feeds changes — diff-tested)
- [ ] core build + ctest + mcp-venv cam tool tests green; `graphify update .`; close issue #2 ref

## Driver notes (Fable)

- The registry (T-F2, merged) is REGISTRY-AUTHORITATIVE with provenance + condition fields —
  your cam sub-section must carry provenance too (cite the CuttingDB's original source).
  NEVER fabricate feeds data (T-F2's machinability_pct precedent: unseeded > invented).
- Registry schema is a CONTRACT-ADJACENT change: additive only; if anything non-additive
  seems needed, work/questions/T-M4.md first.
- cam_tools.py shared with M3/M5 — append-only, small commits.
