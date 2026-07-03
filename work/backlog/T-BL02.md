---
id: T-BL02
title: Format interop track (reverse-engineering intake)
workstream: backlog
owner: mooseinatree
status: todo
branch: task/T-BL02-format-interop
depends_on: []
lane: modeling-core
kind: new
priority: 3
---

## Context

Land parsers as specs arrive from the reversing effort. Order: openNURBS/.3dm (MIT lib, no reversing), Parasolid payload extraction from NX/Solid Edge, DXF via ezdxf, then CATIA V5, Inventor, Altium (clean-room; KiCad importer as spec reference only). Defer Revit — IFC covers BIM.

## Ownership (2026-07-03)

Assigned to **mooseinatree** (human) — has the competitor CAD software (NX/Solid Edge/
CATIA/Inventor/Altium) and a reverse-engineering setup on Windows to actually produce
the format specs/samples this task consumes. Division of labor: mooseinatree reverses
each format and lands the clean-room protocol memo + sample files; an agent driver can
then be dispatched per-format to write the `engine/doc/formats.py` importer + fixture
round-trip tests once a spec/sample is available. Don't claim this whole task via the
normal agent auto-claim path — coordinate with mooseinatree on which format is ready.

## Definition of done

- [ ] Each format lands as an engine/doc/formats.py importer with fixture round-trip tests
- [ ] Clean-room protocol memo followed for every reversed format
- [ ] ODA membership decision recorded for DWG/Revit
