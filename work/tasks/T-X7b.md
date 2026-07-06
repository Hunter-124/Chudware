---
id: T-X7b
title: Vendor wave-2 follow-ups — McMaster STEP import + structured price to bom_price seam
workstream: X
owner: unassigned
status: todo
branch: task/T-X7b-vendor-wave2-followups
depends_on: [T-X7]
lane: parts-sourcing
kind: new
priority: 4
---

## Context

T-X7 (merged) shipped the 4 wave-2 vendor adapters (Amazon/McMaster/JLCPCB/PCBWay) as
part_search federation providers with 45 tests + the vendor survey. Two DoD items were
honestly left partial and split here:

- McMaster STEP -> import_part with source metadata: adapter acquire() returns the
  reference but the STEP fetch -> import_part wiring is incomplete.
- Structured price/availability -> bom_price approved-provider seam: NOT DONE.

## Definition of done
- [ ] McMaster (and where available) STEP acquire -> import_part with source metadata.
- [ ] Structured price/availability feeds the bom_price approved-provider seam.
- [ ] Tests; scripts/gate.sh PASS; graphify update.
