# Multi-Format Import/Export — Status

Tracks `docs/FORMATS_PLAN.md`. Engine venv: `%APPDATA%\AI-CAD\runtime\venv`.

## Wave status

| Wave | Scope | State |
|---|---|---|
| F0 | Mesh-body foundation (MeshBody → AICM, mass-props, import/export plumbing) | ✅ done |
| F1 | Mesh breadth: OBJ/PLY/glTF/GLB + FORMAT_REGISTRY + `doc_formats` RPC | ✅ done |
| F2 | B-rep polish: multi-solid STEP → separate named bodies | ✅ done |
| F3 | Rhino `.3dm` via rhino3dm (mesh-level) | ✅ done |
| F4 | Host UI: unified Open/Import filter, Import menu, expanded Export | ✅ done |
| F5 | SolidWorks/Parasolid: SW-COM→STEP (✅ validated) + OLE2 probe (RE stub) | ✅ done |
| F6 | Polish, docs, convert-mesh-to-solid stretch | ⏳ optional |

## Live capability matrix

| Ext | Import | Export | Representation |
|---|---|---|---|
| `.step+` | ✅ | ✅ (save) | full parametric (native) |
| `.step` `.stp` | ✅ | ✅ | B-rep solid |
| `.iges` `.igs` | ✅ | ✅ | B-rep (sewn shell) |
| `.brep` | ✅ | ✅ | exact B-rep |
| `.stl` | ✅ | ✅ | mesh body |
| `.3mf` | ✅ (lib3mf) | ✅ (lib3mf) | mesh body |
| `.obj` | ✅ | ✅ | mesh body |
| `.ply` | ✅ | ✅ | mesh body |
| `.gltf` `.glb` | ✅ | ✅ | mesh body |
| `.3dm` | ✅ (mesh) | ✅ (mesh) | Rhino render meshes (NURBS not tessellated) |
| `.sldprt` `.sldasm` | ✅ via SW-COM* | ✗ (export STEP) | needs SolidWorks installed |
| `.x_t` `.x_b` | ✅ via SW-COM* | ✗ (export STEP) | needs SolidWorks installed |

\* SolidWorks COM→STEP→import validated on SW 2025 (adapter-plate sample → 1 solid, vol 10888 mm³).
Requires SolidWorks installed + licensed + `pywin32` in the engine runtime; otherwise a clear
"export STEP" error. Modern `.sldprt` is a proprietary non-OLE2 container, so there is no
standalone reader — the OLE2 preview-mesh probe is a research stub for legacy files only.

## SolidWorks / Parasolid (F5, experimental)

- **Import chain** (`engine/doc/formats_solidworks.py`): SolidWorks COM → STEP → B-rep lane;
  else legacy-OLE2 preview mesh (stub); else a clear "export STEP" error. Parasolid (.x_t/.x_b)
  goes through SW COM only.
- **Finding (SW 2025):** modern `.sldprt` is **not** an OLE2 container (magic `7b ab b3 01`, not
  `d0 cf 11 e0`) and not ZIP — it's an undocumented proprietary format. The `olefile` probe only
  helps legacy (pre-~2015) files. So modern SW import relies on the **COM → STEP** path, which
  requires SolidWorks installed + licensed + `pywin32` in the engine runtime.
- **Test parts to create (for reverse-engineering the modern container / validating COM):**
  1. A simple single-body part (one extruded boss) — `.sldprt`.
  2. A multi-body part (2–3 separate bodies) — `.sldprt`.
  3. A small assembly (2–3 components) — `.sldasm`.
  4. The same simple part exported as Parasolid `.x_t` and `.x_b`.
  5. Optionally the same part saved from an *older* SolidWorks (if available) to compare containers.
  Drop them anywhere and point me at the folder; I'll use `ole2_streams()` + a hex probe to map the
  layout and extend the importer.

## Notes / decisions

- **Mesh body** (`engine/doc/mesh_body.py`): imported triangle soups are a first-class `MeshBody`
  (no OCCT topology, `.wrapped is None`). Downstream dispatches by `isinstance(shape, MeshBody)`.
  Renders via `tessellate.tessellate_mesh_body` (no BRepMesh); naked + sharp-dihedral wireframe.
- **3MF** uses **lib3mf** (bundled, BSD) for import + export — avoids trimesh's networkx+lxml deps.
- **Other meshes** (STL/OBJ/PLY/glTF) use **trimesh** (MIT); `networkx` (BSD) added for scene graphs.
- Mesh bodies **cannot** export to STEP/IGES/BREP (no topology) — raises a clear error.
- B-rep export of a mesh body and mesh export of a solid (tessellate-on-the-fly) are both handled.
- Tests: `engine/doc/test_formats.py` (12 cases); full `engine/doc` suite green (706).
</content>
