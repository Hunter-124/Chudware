---
id: T-D1
title: structural_schema.md contract and engine/structural/ scaffold
workstream: D
owner: opus-vex
status: review
branch: task/T-D1-structural-schema-scaffold
day: 1
depends_on: []
---

## Context

Day 1 establishes the structural pillar following the ECAD precedent: a frozen contract and a Python module with doc kind `aicad.structure/1`. Element types (`beam`, `column`, `post`, `joist`, `stud_wall`, `footing`) compile to B-rep via OCCT prisms so they render in the normal viewport. The schema must be frozen on day 1 because T-D5 (cost takeoff) depends on T-B2 (BOM rollup) which reads structural element material specs.

## Files / areas touched

- `contracts/structural_schema.md` — new frozen contract
- `engine_oracle/structural/__init__.py`, `engine_oracle/structural/elements.py` — new module
- `engine_oracle/structural/brep.py` — OCCT prism generation for structural elements
- `engine_oracle/doc/` — doc-meta: register `aicad.structure/1` doc kind
- `services/mcp/server.py` — eventual registration (day 3)

## Steps

1. Run `graphify query "ECAD engine module doc kind schema"` to understand the ECAD module pattern and how doc kinds are registered.
2. Draft `contracts/structural_schema.md`: element types, element properties (section, material, length, orientation), load case schema (dead/live/wind presets), analysis result schema (member forces, deflections, D/C ratio), MCP tool signatures.
3. Implement `elements.py`: dataclasses `Beam`, `Column`, `Post`, `Joist`, `StudWall`, `Footing`; each has `section_name: str`, `material: str`, `length_mm: float`, `transform: list[float]` (4×4 matrix); `StructureDoc` holds a list of elements + load cases.
4. Implement `brep.py`: `element_to_brep(element) -> TopoDS_Shape` using OCCT `BRepPrimAPI_MakePrism` on the section profile extruded along length.
5. Register `aicad.structure/1` doc kind in the doc-meta system.
6. Smoke-test: create a `Post` (100×100mm section, 2400mm tall, cedar); call `element_to_brep(post)`; assert result is a valid OCCT shape.

## Definition of done

- [ ] `contracts/structural_schema.md` frozen, all element types and result schemas documented
- [ ] `engine_oracle/structural/` package importable
- [ ] `element_to_brep()` returns a valid OCCT shape for each element type
- [ ] `aicad.structure/1` doc kind registered in doc-meta
- [ ] `python -m pytest engine_oracle/structural/test_elements.py -q` passes

## AI hints

- `graphify query "OCCT BRepPrimAPI_MakePrism extrude"` — find existing OCCT prism usage in the repo
- `graphify explain "doc-meta doc kind registration"` — find how to register a new doc kind
- Relevant files: `engine_oracle/doc/`, `engine_oracle/ecad/` (as structural precedent), `engine_oracle/kernel.py`
- Section profiles: rectangular hollow section → rectangular wire profile extruded; I-beam → I-wire profile from AISC dims; lumber → solid rectangular; implement a `section_profile(name) -> TopoDS_Wire` function in `brep.py`
