---
id: T-BL02
title: Format interop track (reverse-engineering intake)
workstream: backlog
owner: mooseinatree
status: backlog
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
then be dispatched per-format to add the importer in `core/doc/formats.cpp` plus
fixture round-trip tests once a spec/sample is available. Don't claim this whole task
via the normal agent auto-claim path — coordinate with mooseinatree on which format is ready.

## Definition of done

- [ ] Each remaining format lands in `core/doc/formats.cpp` with fixture round-trip tests
- [ ] Clean-room protocol memo followed for every reversed format
- [ ] ODA membership decision recorded for DWG/Revit

## Progress log

### 2026-07-03 — mooseinatree (Opus driver, isolated Windows checkout)

Picking up the greenlit no-reversing slice: **DXF import via ezdxf (MIT)**. Status:
- `.3dm` mesh import/export and DXF/DWG import now live in `core/doc/formats.cpp`.
- `.dxf` was absent when this slice began; its live C++ importer now handles the
  clean-room entity subset and assembles OCCT edges, wires, and faces. It remains
  import-only (2D-to-DXF export is still deferred).

**Historical environment note:** the 2026-07-03 Windows checkout materialized the old
Python-oracle symlinks as directories because `core.symlinks=false`. That tree and its
symlinks were retired on 2026-07-23; canonical format work now lives in
`core/doc/formats.cpp`.
- 2026-07-04 00:53 UTC [mooseinatree] DXF import slice completed in the former Python
  oracle, then ported to `core/doc/formats.cpp` by T-BL47. The live C++ importer covers
  LINE/CIRCLE/ARC/LWPOLYLINE/POLYLINE/ELLIPSE/SPLINE as OCCT edges, wires, and faces.
  T-BL02 remains the ongoing interop umbrella for Parasolid/CATIA V5/Inventor/Altium.
- 2026-07-04 02:25 UTC [mooseinatree] DWG import and clean-room reference docs landed.
  The live implementation is in `core/doc/formats.cpp`: an external user-provided
  LibreDWG converter produces DXF, then the DXF importer handles it in-process.
- 2026-08-04 [audit] DXF/DWG/.3dm shipped and were ported to C++ (core/doc/formats.cpp);
  Parasolid/CATIA V5/Inventor/Altium and the ODA membership decision remain untouched;
  last prior activity 2026-07-04.
