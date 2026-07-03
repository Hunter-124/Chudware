---
id: T-BL21
title: Hollow structural sections - model true wall voids (RHS/HSS) for mass/interference correctness
workstream: backlog
owner: sonnet-quorix
status: review
branch: task/T-BL21-hollow-sections
depends_on: []
lane: modeling-core
kind: fix
priority: 3
---

## Context

**Filed 2026-07-03 (Fable advisor, closing T-D1 Q3).** `section_profile` extrudes
hollow structural sections (RHS/HSS/pipe) as their solid outer envelope; wall
thickness lives only in `section_name` for takeoff. Accepted for v1, but every hollow
member currently has wrong volume, mass, and interference geometry — this bites any
future mass-properties readout, clash check, or rendering-fidelity work on structural
docs.

## Definition of done

- [x] Hollow section profiles (RHS, HSS, CHS/pipe at minimum, **mm-format designations** —
      see `work/questions/T-BL21.md` Q1 for the still-open imperial-catalog `HSS4X4X1/4`-style
      naming reconciliation) generate true outer-minus-inner prisms (sketch with inner void or
      equivalent boolean)
- [x] Mass/volume of a reference member matches handbook value within tolerance
      (cited reference, byte-exact expected literal — T-D3 discipline)
- [x] check_interference on two touching hollow members reflects real wall geometry
- [x] Takeoff/cost path (T-D5) still produces identical weights (it already uses
      per-length handbook mass, not solid volume — verify, don't assume)

## AI hints

- `graphify query "section_profile structural member extrusion"` — where the solid
  envelope is built today
- `work/questions/T-D1.md` Q3 — the original deferral and its rationale

## Resolution (sonnet-quorix, 2026-07-03)

`engine_oracle/structural/brep.py`: refactored section parsing into `_parse_section` (shared
by `section_profile` and `element_to_brep`) so `element_to_brep` now builds a true
outer-minus-inner FACE (`_section_face`/`_hollow_face`, `BRepBuilderAPI_MakeFace` + a
reversed inner wire) for `RHS`/`HSS WxHxT` (rectangular hollow) and new `CHS`/`PIPE ODxT`
(circular hollow/pipe) forms, then prisms that face. `section_profile()`'s public contract is
unchanged (still returns only the outer wire — verified by keeping
`test_section_profile_returns_wire` intact and adding a hollow-forms variant). A wall
thickness that doesn't physically fit the envelope (`t<=0` or `t>=` half the smaller outer
dim) degrades to the solid outer shape rather than a self-intersecting face.

DoD verification:
- Outer-minus-inner prism: `test_rhs_produces_true_hollow_prism_not_solid_envelope` — volume
  matches the analytic `(100*50 - 90*40)*length` hollow prism, not the old solid envelope.
- Handbook match (byte-exact literal, T-D3 discipline): `test_chs_pipe_matches_asme_b36_10m_handbook_area`
  — NPS 4 Schedule 40 pipe, OD 4.500in / wall 0.237in / area 3.174in², ASME B36.10M (bundled
  official table via `mcp__ai-cad__research_standard_section("pipe 4 sch 40")`). A pipe
  cross-section is a true annulus, so our outer-minus-inner CHS model matches the handbook
  area to 0.0015% in a standalone check — asserted here at `rel_tol=5e-3`.
- `check_interference` reflects real wall geometry: `test_hollow_wall_geometry_is_visible_to_interference_checks`
  — a 60×60 rod centred in an RHS100x100x10 tube's 80×80 void (10mm clearance to the wall)
  produces **zero** `BRepAlgoAPI_Common` overlap volume with the tube solid (the same boolean
  primitive `eval_interference` uses); under the old solid-envelope bug this would have been a
  full-cross-section false interference. (The core-doc `check_interference` MCP tool itself is
  C++-side and out of this Python engine slice's reach to drive end-to-end — this test proves
  the underlying solid geometry it depends on is now correct.)
- Takeoff unchanged: confirmed by inspection — `estimate.py`/`test_estimate.py` import neither
  `engine.structural.brep` nor `section_profile`; `steel_weight_lbs`/`board_feet` use only
  `section_name` string lookups against `_AISC_LBS_PER_FT` / nominal-lumber tables, never
  `brep.volume_mm3`. `test_estimate.py` (all cases) passes unchanged.

Also updated (documentation-only, no behavior change): `contracts/structural_schema.md` §4
(new `HSS`/`CHS`/`PIPE` rows + a note that `analysis.section_properties` still uses the solid
outer rectangle for RHS/HSS stiffness properties — that path is a separate, unscoped
follow-up), and the stale "(matches brep)" comment in `engine_oracle/structural/analysis.py`.

`engine_oracle/structural/` suite: 133 passed, 1 skipped (pre-existing OCP-absent skip guard,
unaffected) — see `python3 -m pytest engine_oracle/structural/ -q`.

Escalated (non-blocking, `work/questions/T-BL21.md` Q1): this task's mm-based `section_name`
grammar (`HSS100x100x6`) and the existing imperial/fractional `HSS4X4X1/4`-style keys in
`estimate._AISC_LBS_PER_FT` disagree on what an `HSS` designation means — an element named with
the codebase's real catalog format gets correct takeoff mass (string lookup) but a generic
default-rectangle solid, not its true hollow geometry. Closed the dangerous half of that gap (a
literal `/` in the key is now treated as unparseable → safe per-type default, instead of the
digit-regex silently mis-tokenizing `"1/4"` into `[1, 4]` and fabricating a 4mm tube) — see
`test_fractional_imperial_hss_designation_does_not_fabricate_geometry`. Full reconciliation
(single `section_name` driving both paths) needs an advisor call on whether to convert the
imperial catalog to mm-equivalent strings or add a unit-aware parser branch; recommendation and
alternatives are in the question file.

MCP freshness check: `cad_guide` (services/mcp/cad_tools.py `cad_guide_text`) has no
structural/section-format topic to update — structural tools are documented via the contract +
their own MCP docstrings, not cad_guide's topic list. `work/context/` has no section pack
referencing `section_profile`/RHS/structural either. No agent-facing doc beyond
`contracts/structural_schema.md` (already updated) needed a change.
