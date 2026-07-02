---
id: T-D1
title: structural_schema.md contract and engine/structural/ scaffold
workstream: D
owner: opus-vex
status: done
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

- [x] `contracts/structural_schema.md` frozen, all element types and result schemas documented
- [x] `engine_oracle/structural/` package importable
- [x] `element_to_brep()` returns a valid OCCT shape for each element type (verified: all 6 types non-null, positive volume; I-section < bounding box)
- [x] `aicad.structure/1` doc kind registered in doc-meta (self-declared schema + `is_structure`/`doc_kind` helpers — see note below / questions Q2)
- [x] `python -m pytest engine_oracle/structural/test_elements.py -q` passes (10 passed, with OCP present)

## Implementation notes (opus-vex)

- **`engine` is a symlink → `engine_oracle`** (git-tracked). Code imports via `from engine.structural…`
  to match the ECAD precedent; files live under `engine_oracle/`.
- **doc-kind "registration":** no central Python doc-kind registry exists (the parametric doc
  engine moved to the C++ core; Python `engine.doc` is closure-free helpers). A doc self-declares
  its kind via its `schema` field — same as `aicad.sch/1` / `aicad.pcb/1`. Implemented as
  `SCHEMA="aicad.structure/1"` + `is_structure`/`doc_kind`/`new_structure`. See questions Q2.
- **Geometry:** raw OCP (cadquery-ocp) per the hints — `section_profile(name)->TopoDS_Wire`
  (rect / RHS-outer / I-shape + a small AISC `W` table) faced + extruded +Z with
  `BRepPrimAPI_MakePrism`, then a 4×4 rigid `gp_Trsf` placement. OCP is lazily imported so
  `elements.py` stays OCCT-free. RHS is modelled as its outer rectangle for v1 (questions Q3).
- **`element_type` is a `ClassVar`** (not an init field) so the subclass dataclasses stay valid
  (a defaulted `element_type` field would otherwise precede the base's non-default fields).
- **Verification:** `pytest engine_oracle/structural/test_elements.py -q` → 10 passed in a venv
  with OCP installed. Standalone smoke: 100×100×2400 cedar post → exact 24 000 000 mm³ solid;
  every element type yields a valid non-null solid.
- **Did NOT edit** `services/mcp/server.py` (day-3 registration) — out of scope.

## ⚠️ Gate status — pre-existing blocker (see work/questions/T-D1.md Q1)

`bash scripts/gate.sh` PASSES every stage EXCEPT `ui/imgui`, which fails to **compile** on the same
**pre-existing master break** flagged from T-C1: `property_manager.cpp` references
`PanelField::selMulti` (10×) but the struct in `ui/abstract/panel_spec.h` has no such member
(present at `f6e156c`, before this claim). T-D1 adds zero C++, so it neither causes nor can fix it.

```
check.py     FAIL-RECORDED  (pre-existing, non-fatal)
core build   PASS  (libaicad, OCCT ON)
ui/abstract  PASS
ctest        PASS  (100%)
pytest       SKIP  (no provisioned venv; structural suite verified separately — 10 passed)
ui/imgui     FAIL  (pre-existing selMulti compile break — NOT from T-D1)
```

The structural slice is merge-ready on its own; the GUI break needs a separate GUI-workstream fix.

## AI hints

- `graphify query "OCCT BRepPrimAPI_MakePrism extrude"` — find existing OCCT prism usage in the repo
- `graphify explain "doc-meta doc kind registration"` — find how to register a new doc kind
- Relevant files: `engine_oracle/doc/`, `engine_oracle/ecad/` (as structural precedent), `engine_oracle/kernel.py`
- Section profiles: rectangular hollow section → rectangular wire profile extruded; I-beam → I-wire profile from AISC dims; lumber → solid rectangular; implement a `section_profile(name) -> TopoDS_Wire` function in `brep.py`
