# Multi-Format Import/Export Plan (waves F0–F6)

> Goal: import **and** export the popular CAD/mesh interchange formats — even when we can
> only carry geometry as a "dumb" (non-parametric) body. Native `.step+` stays the only
> format that round-trips the full feature tree; everything else is interchange.
>
> Status doc: `docs/FORMATS_STATUS.md` (created with wave F0). Cadence per project norm:
> build + commit + a SolidWorks-vocabulary manual test checklist per wave. Single master
> lane, agents run **non-isolated** in the main tree, engine ⟂ host parallelized.

---

## 1. Current state (verified 2026-06-15)

The premise "we can only work with `.step+`" is **out of date** — the engine already has a
centralized exchange layer. What actually exists:

| Layer | Reality |
|---|---|
| `engine/doc/formats.py` | `export_document()` + `import_path()` for **STEP, IGES, BREP, STL, 3MF**; worker RPCs `import_file` / `export_file`. |
| `engine/worker.py:_doc_open` | Routes `_EXT_TO_FORMAT` extensions → `import_file`; `.step+` → `stepplus.open`. |
| `engine/doc/features/import_feature.py` | `import` feature = frozen body from a source file (mtime-cached). |
| Host `title_bar.cpp:140` | File ▸ Export submenu: STEP/IGES/STL/3MF/BREP. |
| Host `app.cpp:exportFile3d` | Native Save-As + `export_file` RPC for those 5 formats. |

### The real gaps (why it *feels* like step+-only)

1. **Open dialog filter shows only `*.step+`** (`kStepPlusFilter`) — you can't browse to a
   `.step`/`.stl` even though `_doc_open` would accept it. No File ▸ **Import** item.
2. **Mesh import is a dead end.** STL imports as a bare build123d `Face`; 3MF import returns a
   `_MeshShapeProxy` whose `.wrapped is None`, and `import_feature.py:59` **silently drops any
   body with `wrapped is None`**. So 3MF "import" produces nothing, and STL faces don't flow
   cleanly through tessellation/mass-props. Root cause: `tessellate.tessellate_body()` needs an
   **OCCT shape** (`BRepMesh_IncrementalMesh` + face/edge maps); a raw triangle soup has no
   topology to mesh.
3. **Missing popular formats entirely**: OBJ, PLY, glTF/GLB, SolidWorks (`.sldprt`/`.sldasm`),
   Parasolid (`.x_t`/`.x_b`), Rhino (`.3dm`).
4. **`canOpenPath` (`app.cpp:608`)** omits `.brep`, `.obj`, `.ply`, `.gltf`/`.glb`, `.3dm`, etc.

---

## 2. Architecture decisions

### D1 — First-class **mesh body** (the linchpin)

Imported meshes must become real, viewable, re-exportable bodies. Converting every triangle to
an OCCT face (sew → shell) explodes topology and is slow for big meshes, so instead:

- Add a **`MeshBody`** carrier in the engine: `verts: float32[N,3]`, `tris: uint32[M,3]`,
  optional `normals`, `name`, `color`. It is *not* a build123d shape.
- Add `tessellate.tessellate_mesh_body(mesh, body_id, …) -> BodyTess` that builds AICM geometry
  **directly** from raw triangles: per-vertex normals (area-weighted), one `FaceRec` spanning all
  tris, **boundary edges only** as the drawn wireframe (manifold edges shared by 2 tris are
  hidden, naked/feature edges drawn), empty B-rep `verts`. Scales to millions of triangles.
- Thread `MeshBody` through: `CompileOutput.bodies` may now contain `MeshBody`; the compiler core
  stamps `kind="mesh"` on the `BodyOut`; `session._write_aicm` branches mesh→`tessellate_mesh_body`,
  shape→`tessellate_body`; mass-props (`evaluate.py`) uses the divergence-theorem volume already
  computed for 3MF; `export_document` re-serializes `MeshBody` via the mesh writers.
- **Optional "Convert to Solid"** op (stretch, F6): sew the mesh into an OCCT `Shell`/`Solid`
  (`BRepBuilderAPI_Sewing` + `ShapeFix`) so the user can opt into B-rep (booleans/measure) when
  the triangle count is sane. Guarded by a triangle cap with a clear warning.

### D2 — Two clean import/export lanes by representation

- **B-rep lane (OCCT)** — STEP, IGES, BREP. Yields build123d `Shape`s (editable-ish: faces/edges
  but no parametric history). Already mostly built; XDE used for STEP product names.
- **Mesh lane (trimesh + lib3mf)** — STL, OBJ, PLY, 3MF, glTF/GLB. One unified `trimesh`-backed
  reader → `MeshBody`(s); one unified writer per extension. `lib3mf` (BSD) used for 3MF
  color/metadata fidelity; `build123d.export_gltf` / OCCT `RWGltf` acceptable for glTF export.
  `trimesh` (MIT) is already a dependency and reads/writes all of these robustly.
- **Rhino lane (rhino3dm)** — `.3dm` splits into B-reps (→ OCCT via NURBS/region, fall back to its
  render mesh) and meshes (→ `MeshBody`).

### D3 — Proprietary formats: opportunistic, never blocking

Per owner decision (has SolidWorks installed, willing to make test parts):

- **Primary**: COM-automate the installed SolidWorks (`pywin32`) to silently export a temp STEP,
  then import via the B-rep lane. Free when SW is present; Windows-only; degrades gracefully.
- **Experimental**: probe the `.sldprt`/`.sldasm` OLE2/Compound-File container (`olefile`, BSD) for
  an embedded tessellated preview/graphics body → `MeshBody`. R&D using owner-supplied test parts;
  documented as best-effort, version-dependent. **Never** on the critical path.
- **Fallback always available**: UI guidance "Export STEP from SolidWorks, then open here."
- **Parasolid `.x_t`/`.x_b`**: no free reader. Route through SW-COM if available, else punt to the
  STEP-export guidance. Export: emit STEP (SolidWorks/NX import STEP losslessly).

### D4 — Format registry, not if/else sprawl

Replace the ad-hoc `if fmt ==` chains with a single table in `formats.py`:
`FORMAT_REGISTRY[ext] = FormatSpec(name, lane, can_import, can_export, reader, writer, label)`.
`_EXT_TO_FORMAT`, the host filter strings, and `canOpenPath` all derive from one source of truth
(host gets a generated `format_filters.h` or a small `doc_formats` RPC that returns the matrix).

---

## 3. Target format support matrix

| Ext | Format | Lane | Import | Export | Fidelity | Library |
|---|---|---|---|---|---|---|
| `.step+` | AI-CAD native | — | ✅ | ✅ | **full parametric** | (own) |
| `.step` `.stp` | STEP AP214/242 | B-rep | ✅ | ✅ | solids + names (no history) | OCCT |
| `.iges` `.igs` | IGES | B-rep | ✅ | ✅ | surfaces→sewn shell | OCCT |
| `.brep` | OCCT BREP | B-rep | ✅ | ✅ | exact B-rep | OCCT |
| `.stl` | STL (bin/ascii) | mesh | ✅* | ✅ | mesh | trimesh |
| `.3mf` | 3MF | mesh | ✅* | ✅ | mesh + color/meta | lib3mf |
| `.obj` | Wavefront OBJ | mesh | ➕ | ➕ | mesh + groups | trimesh |
| `.ply` | Stanford PLY | mesh | ➕ | ➕ | mesh + vtx color | trimesh |
| `.gltf` `.glb` | glTF 2.0 | mesh | ➕ | ➕ | mesh + material | trimesh / OCCT |
| `.3dm` | Rhino | rhino | ➕ | ➕ | B-rep + mesh | rhino3dm |
| `.sldprt` | SolidWorks part | proprietary | 🧪 | STEP-out | mesh/preview or via SW-COM | olefile / pywin32 |
| `.sldasm` | SolidWorks asm | proprietary | 🧪 | STEP-out | per-component via SW-COM | olefile / pywin32 |
| `.x_t` `.x_b` | Parasolid | proprietary | 🧪 | STEP-out | via SW-COM only | pywin32 |

✅ exists · ✅* exists but fixed by F0 mesh-body work · ➕ new in this plan · 🧪 experimental/opportunistic

---

## 4. Wave breakdown

Each wave is sized to land independently with green engine tests + a manual checklist. Engine
waves (F0–F3, F5) and the host wave (F4) parallelize once the F0 RPC/registry contract is fixed.

### F0 — Mesh-body foundation (engine) — *unblocks all mesh formats*
- `engine/doc/mesh_body.py`: `MeshBody` dataclass + helpers (bbox, volume via divergence theorem,
  area, boundary-edge extraction, area-weighted normals).
- `engine/doc/tessellate.py`: add `tessellate_mesh_body()`; no change to `tessellate_body()`.
- `engine/doc/features/base.py` `CompileOutput.kind`: allow `"mesh"`.
- Compiler core + `session.py`: carry `MeshBody` bodies; `_write_aicm` branches by kind;
  `_solid_bodies`/mass-props handle mesh bodies (volume/area/bbox; no faces/edges B-rep queries).
- `import_feature.py`: stop dropping `wrapped is None`; route mesh sources → `MeshBody`.
- `formats.py`: `_import_stl`/`_import_3mf` return `MeshBody`; `export_document` re-serializes
  `MeshBody` to STL/3MF.
- Tests: STL round-trip → real body with triangles + bbox; 3MF import yields a viewable body;
  mass-props on a mesh body.

### F1 — Mesh format breadth (engine)
- `FORMAT_REGISTRY` (D4); unified `trimesh` reader/writer; add **OBJ, PLY, glTF/GLB**.
- `lib3mf` path keeps 3MF color/metadata on both import and export.
- Extend `_EXT_TO_FORMAT`; per-format round-trip tests; license-ledger rows.

### F2 — B-rep polish (engine)
- Confirm STEP/IGES/BREP import produce solids (not loose faces); multi-solid STEP → multiple
  named bodies (XDE already partial); STEP **AP242** option; IGES/STEP export unit handling.
- Tests: multi-body STEP names; IGES sew→closed shell volume; BREP exact round-trip.

### F3 — Rhino `.3dm` (engine)
- Add `rhino3dm` (MIT) to requirements + ledger. Reader: B-reps → OCCT (or render mesh →
  `MeshBody` fallback), meshes → `MeshBody`. Writer: bodies → `.3dm` (mesh; B-rep if feasible).
- Tests: round-trip a `.3dm` with a polysurface + a mesh.

### F4 — Host integration (C++) — *parallel with F1–F3*
- Unified **Open** filter built from the format matrix (all importable extensions + "All CAD
  files"); add File ▸ **Import…** (into current doc vs new doc).
- Expand Export submenu + `exportFile3d` filters: OBJ, PLY, glTF/GLB, 3DM (+ existing 5).
- Fix `canOpenPath` to include every importable extension; drag-drop honors the same set.
- Large-import UX: busy toast/spinner (mesh imports can be slow); error toasts on failure.
- Source of truth: a `doc_formats` RPC (or generated header) so the C++ filter never drifts
  from the engine registry.

### F5 — SolidWorks / Parasolid (engine, experimental) — *last, non-blocking*
- `engine/doc/formats_solidworks.py`:
  - `sw_com_export_step(path)` — best-effort `pywin32` automation of installed SolidWorks →
    temp STEP → B-rep import. Feature-detected; clean error if SW/pywin32 absent.
  - `sldprt_extract_preview(path)` — `olefile` probe of the OLE2 container for an embedded
    tessellated preview → `MeshBody`. R&D with owner test parts; documented best-effort.
- Wire `.sldprt`/`.sldasm`/`.x_t`/`.x_b` into the registry as import-with-fallback; export = STEP.
- Owner deliverable: a short "make me these test parts" list to drive reverse-engineering.

### F6 — Polish / QA / docs
- `docs/FORMATS_STATUS.md` final matrix; `docs/contracts/formats.md` contract for the registry +
  `doc_formats` RPC. Optional **"Convert mesh to solid"** op (D1 stretch).
- Full manual SW-vocabulary checklist; engine test sweep stays green.

---

## 5. Dependencies & license posture (all GPL-clean)

| dependency | license | added in | role |
|---|---|---|---|
| trimesh | MIT | (existing) | mesh read/write lane (STL/OBJ/PLY/3MF/glTF) |
| lib3mf | BSD-2 | (existing) | 3MF color/metadata fidelity |
| rhino3dm | MIT | F3 | `.3dm` read/write |
| olefile | BSD-2 | F5 | `.sldprt`/`.sldasm` OLE2 container probe (experimental) |
| pywin32 | PSF/BSD | F5 | optional SolidWorks COM automation (Windows-only, feature-detected) |

No commercial/GPL deps. ODA MCAD SDK and the Parasolid kernel are explicitly **out of scope**
(paid, closed). Each new dep gets a row in `docs/contracts/licenses.md` in the same wave.

---

## 6. Risks & mitigations

- **OCP missing `RWObj`/`RWPly`/`RWGltf`** in cadquery-ocp 7.8.1 → we lean on `trimesh` for the
  mesh lane, so this is moot; OCCT only does STEP/IGES/BREP where it's already proven.
- **Huge meshes** (multi-M triangles) slow tessellation/UI → mesh-body path is direct (no
  BRepMesh); add a triangle-count guard + optional decimation on import.
- **SolidWorks RE is open-ended** → boxed into F5, never blocks F0–F4; STEP-export fallback always
  works.
- **Host/engine filter drift** → single registry + `doc_formats` RPC (D4).
- **Worktree staleness gotcha** (memory) → agents run non-isolated in the main tree.

---

## 7. Execution order

1. **F0** (engine, serial — everything depends on it).
2. **F1 + F4** in parallel (engine mesh breadth ⟂ host UI against the F0 contract).
3. **F2 + F3** (engine B-rep polish ⟂ Rhino).
4. **F5** experimental SolidWorks (after owner supplies test parts).
5. **F6** polish + docs + convert-to-solid stretch.
</content>
</invoke>
