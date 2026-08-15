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
today. Nothing in that set describes Chudware internals, and the rule in
**Keeping this clean** governs it.

This directory has since also become the home for Chudware's own build,
packaging and operations references — `HOSTD.md`, `BUILD_WINDOWS.md`,
`PACKAGING_WINDOWS.md`, `CI_RUNNERS.md`, `serving_runbook.md`, and the demo and
degradation notes — which is what `docs/README.md` says this folder is for
("build/packaging guides, CAD data-model and file-format references, runbooks").
Those files do describe Chudware internals; the clean-room rule does not apply
to them, and they are not part of the set listed below.

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

- **`services/formats/formats.py`** — the single source of truth for the format matrix
  (`FORMAT_REGISTRY`) and the import entry point (`import_path`). Today: STEP
  (AP214), IGES, BREP, STL, OBJ, PLY, glTF/GLB, 3MF, and Rhino `.3dm` at the mesh
  level are fully wired; **DXF import** (via `services/formats/formats_dxf.py`, ezdxf
  MIT) is the newest addition (2D vector drawings → planar B-rep, import-only);
  SolidWorks `.sldprt`/`.sldasm` and Parasolid `.x_t`/`.x_b` are experimental,
  import-only, non-export entries. **DWG import is not yet implemented** — see
  below.
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

Do not add anything to this directory that was learned from a proprietary binary,
decompiled code, or a vendor SDK's internal (non-public) documentation. If a new
format needs to be added to this reference set, source it the same way the
original three files were sourced: the format's own public/ISO/ANSI/ASTM
specification, or openly published vendor developer docs. This keeps
`docs/reference/` safe to reference from anywhere in the repo, including from
work that will eventually ship commercially (see the clean-room posture already
recorded for KiCad and BOLTS in `contracts/licenses.md`).
