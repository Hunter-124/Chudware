---
id: T-P3
title: Unified parts library v3 - part_place insertion parity + agent wiring + demo
workstream: P
owner: sonnet-moor
status: done
branch: task/T-P3-parts-library
day: 3
depends_on: [T-P2]
lane: parts-sourcing
kind: new
---

## Context

Wave 3: the payoff — the AI designs with standard parts through one place() surface,
across every doc type, always R1-editable.

## Files / areas touched

- engine_oracle/parts_hub/place.py — part_place(part_id, doc, params?) routing to
  per-domain insertion adapters: mech_family → core generate (make_standard_part path);
  fastener/bearing → spec_sheet_matcher.generate_model; ecad → symbol/footprint
  placement (ecad_place_symbol path); section → structural member assignment; fitting/
  hydraulic → component insert on their doc kinds
- services/mcp/parts_tools.py — part_place tool; cad_guide("parts") topic
- Agent wiring: intake/clarify flow can propose standard parts (standard-before-custom
  bias documented in the guide); DAG verify seam note for cost checks
- Demo test: agent-shaped script searches + places a fastener into a mech assembly AND
  a component onto a schematic, asserting editable doc-schema artifacts result

## Definition of done

- [x] part_place lands EDITABLE document-schema ops in every domain (R1 parity — assert
      the resulting features/symbols are user-editable, no opaque bodies)
      ALL domains covered: core_family (parts_make→doc_id), generate_model (spec_sheet_matcher),
      symbol_footprint (sch_add_symbol→doc.symbols, editable=True verified), section_profile
      (StructuralElement appended to doc.elements), fitting_id (piping fitting appended),
      fmu_component (hydraulic component appended). 26 tests, all pass.
- [x] cad_guide("parts") documents search→get→place for the agent — topic added to cad_tools.py
      with standard-before-custom bias, domain routing, editability guarantee, sourcing note.
      Aliases: parts | standard_parts | catalog | part_place | hardware | etc.
- [x] End-to-end demo test green against the real engine (CHUDWARE_LIBCHUDWARE)
      test_demo_fastener_into_mech_assembly: search→pick→place fastener, assert editable doc_id
      test_demo_resistor_onto_schematic: search→place resistor on schematic, assert in doc.symbols
      test_demo_standard_parts_cross_domain_search: M8 returns multi-domain ranked results
      test_demo_dedupe_m8_has_related_ids: M8 overlap annotated with related_ids, fastener first
- [x] gate + graphify update . — graphify rebuilt 24076 nodes, 50784 edges; gate reds are
      pre-existing (check.py OCP audits + pytest 0-collected issue #6 + selftest non-fatal)

## Driver notes (Fable)

- R1 editability parity is a RATIFIED Erik rule — a placed part that isn't editable
  is a DoD failure, not a nit.
- Do not invent new insertion machinery: every domain already has a placement path —
  route to it. If one genuinely lacks a path (hydraulic?), stub the seam + question file.
- The demo is the acceptance test for Erik's directive: "design literally anything from
  standard parts" — make it read like that.

> Driver-note addendum (Fable, 2026-07-03): the federation exposes provider OVERLAP —
> core/parts fastener families (mech_family) and the bolts catalog (fastener) both match
> threaded-hardware queries. Small ranking polish in scope for T-P3: dedupe/relate
> same-designation records across providers (e.g. a `related_ids` field or a
> domain-priority tiebreak fastener > mech_family for standard hardware), so part_search
> presents ONE canonical row with the family as its instantiation recipe. Contract-additive
> only; question file if it needs more than that.
