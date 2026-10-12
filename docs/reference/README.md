# docs/reference/ — build & packaging guides, runbooks, and the clean-room CAD reference

This directory holds two distinct kinds of material, and the distinction is
load-bearing.

The **clean-room CAD format & data-model reference** was folded in from a
sibling clean-room project, **Clean Engine**
(`D:\Clean Engine\ODA\`), whose README states explicitly that its contents are
sourced *only* from public specifications, ISO/ASTM/ASPRS standards, and
vendor-published developer documentation — never from any proprietary binary or
decompiled code. That clean-room framing is preserved verbatim in each file below;
treat it as load-bearing (it's what keeps this material safe to have in the tree
alongside a product with license obligations — see `contracts/licenses.md`).

The **clean-room CAD-format set** — the three files under **Contents** below —
describes the public CAD format landscape and the classic AutoCAD-lineage data
model, then cross-links to where this repo's *actual* interop surface sits
today. The public-spec material is background, not a description of Chudware's
document model; explicitly labelled repo annotations describe the current implementation.
**Keeping this clean** governs the clean-room format set.

This directory has since also become the home for Chudware's own build,
packaging, operations and domain references, which `docs/README.md` indexes as
"build/packaging guides, CAD data-model and file-format references, runbooks".
Those files do describe Chudware internals; the clean-room rule does not apply
to them, and they are not part of the clean-room set:

- **[HOSTD.md](HOSTD.md)** — `chudware-hostd` shared engine daemon operator reference.
- **[BUILD_WINDOWS.md](BUILD_WINDOWS.md)** — building core, host and the Python runtime
  on Windows.
- **[PACKAGING_WINDOWS.md](PACKAGING_WINDOWS.md)** — the Windows release/installer
  pipeline and the manual runtime checklist.
- **[CI_RUNNERS.md](CI_RUNNERS.md)** — the self-hosted CI runner fleet.
- **[serving_runbook.md](serving_runbook.md)** — LoRA adapter serving, routing, canary
  and rollback (T-H9).
- **[REVENG_DEGRADATION.md](REVENG_DEGRADATION.md)** — Reverse Engineering capability
  matrix and banners from `services/vision/reveng_provision.py`.
- **[EM_SIMULATION.md](EM_SIMULATION.md)** — full-wave EM simulation family.
- **[FIN25_SENSOR_NODE_GOLDEN_DEMO.md](FIN25_SENSOR_NODE_GOLDEN_DEMO.md)** — dated
  (2026-07-10) STM32 sensor-node golden demo record.
- **[parts_library_data_coverage.md](parts_library_data_coverage.md)** — dated
  (2026-07-08) Parts Library data-coverage findings.
- **[rover_competition_ruleset.md](rover_competition_ruleset.md)** — cached snapshot of
  the external NASA HERC ruleset.

## Contents

- **[cad-file-formats.md](cad-file-formats.md)** — survey of ~12 CAD/BIM/mesh
  format families (DWG, DXF, DGN, DWF/DWFx, IFC, STEP, IGES, PRC, LandXML,
  STL/OBJ/glTF/etc., PDF, point clouds), each with its authoritative public spec
  and a structural summary. Annotated per-format with what this repo already
  implements vs. what's reference-only.
- **[cad-data-model.md](cad-data-model.md)** — the AutoCAD-lineage `Db/Ge/Gi/Gs`
  data model (handle graph, symbol tables, entity catalog, geometry/graphics
  layering), as publicly documented by the DXF Reference. Cross-linked to this
  repo's own document model and tessellation format where the concepts rhyme.
- **[building-format-support.md](building-format-support.md)** — license-aware,
  per-format implementation guidance (which open-source library, which license
  regime) and a recommended layered architecture, annotated with what this repo
  has already built vs. what a future format would need.

## How this fits into Chudware's interop work

This repo's actual format-interop surface — what's implemented, not just
referenced — lives here:

- **`core/doc/formats.cpp`** — production format matrix (`format_registry()`) and
  import/export surface. STEP, IGES, BREP, STL, OBJ, PLY, glTF/GLB, 3MF and Rhino `.3dm`
  are wired according to build capabilities (lib3mf/openNURBS are optional at build
  time); DXF is imported and exported natively (hand-written ASCII R12, R2000 when solids are
  written; no ezdxf; INSERT blocks expanded, 3DFACE/POLYFACE meshes become mesh bodies, ACIS
  3DSOLID/BODY/REGION/SURFACE become exact B-rep via `core/doc/acis_read.cpp`); DWG is written
  natively as AutoCAD R2000 with ACIS solids (`core/doc/dwg_write.cpp`, `core/doc/acis_write.cpp`),
  its R2013+ ACIS data storage is read natively (`core/doc/dwg_native.cpp`) and its 2D
  entities are imported through LibreDWG `dwg2dxf` run as a subprocess (`core/doc/dwg.cpp`;
  PATH or the per-user install from `scripts/provision_libredwg.py`); ACIS `.sat`/`.sab`
  import and `.sat` (7.0) export use the same ACIS reader/writer.
- **`services/formats/`** — Python helpers, not the production path:
  `formats.py` + `formats_dxf.py`/`formats_dwg.py` are the import-only parity oracle
  (ezdxf, build123d/OCP, trimesh, lib3mf, rhino3dm; the oracle's DWG lane also tries the
  ODA File Converter), `libredwg.py` resolves the LibreDWG tools, and
  `services/ecad/threed.py` uses `import_path` to load board 3D models.
- **SolidWorks `.sldprt`/`.sldasm`** are import-only. On Windows, installed SolidWorks
  COM is preferred for an exact STEP/B-rep conversion. On every platform, modern
  SolidWorks 2015+ saved-display triangle strips import as explicitly labelled
  tessellation-fidelity bodies; assemblies preserve component instance paths and composed
  transforms. Imported bodies are baked into `.step+`, remain editable as native import
  features, export normally, and feed Polyhedral-FEA. Parasolid `.x_t`/`.x_b`
  still needs SolidWorks/NX conversion because it has no saved-display lane.
- **`work/backlog/T-BL02.md`** — the format-interop backlog task tracking what
  lands next (Parasolid payload extraction, CATIA V5, Inventor, Altium
  clean-room parsers, and the DWG/Revit ODA-membership decision). This directory
  is meant to give whoever picks up that task (or the next format) a running
  start on the public-spec landscape without having to re-derive it.
- **`contracts/licenses.md`** — the binding, append-only license ledger for every
  dependency this repo actually ships or links against. When a format-support
  library is added for real (not just referenced here), it needs a row there,
  per that file's own rule. This reference directory is descriptive background;
  `contracts/licenses.md` is the enforceable contract.

## Reading order

1. `cad-file-formats.md` — what formats exist and where their public specs live.
2. `cad-data-model.md` — the shared DWG/DXF-family object model, for anyone
   implementing (or reviewing) a DXF/DWG reader.
3. `building-format-support.md` — license-aware implementation guidance and
   architecture, mapped onto what this repo has already built.

## Keeping this clean

Do not add anything to the three-file clean-room format set that was learned from
a proprietary binary, decompiled code, or a vendor SDK's internal (non-public)
documentation. If a new format needs to be added to this reference set, source it
the same way the original three files were sourced: the format's own public/ISO/ANSI/ASTM
specification, or openly published vendor developer docs. This keeps
`docs/reference/` safe to reference from anywhere in the repo, including from
work that will eventually ship commercially (see the clean-room posture already
recorded for KiCad and BOLTS in `contracts/licenses.md`).
