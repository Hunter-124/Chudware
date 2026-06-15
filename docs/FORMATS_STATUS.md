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
| F6 | Convert mesh→solid: engine `solidify` + import `as_solid` + host "Import as Solid" menu | ✅ done |

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
- **Reverse-engineering verdict (measured on the `sldprt reversing` test parts, SW 2025):**
  modern `.sldprt`/`.sldasm` is a **fully compressed/encrypted opaque container** — no standalone
  geometry extraction is possible. Evidence:
  - Per-file-varying magic (`5e4bf6ca`, `1bd70589`, `28411a57`, `adf14fe2`, …) — not OLE2
    (`d0cf11e0`), not ZIP (`504b`).
  - Shannon entropy ≈ **7.9/8.0 bits/byte** across the whole file (maximum — compressed/encrypted).
  - No plaintext stream names, no PNG/BMP preview, no Parasolid/geometry markers, no zero-runs.
  - `olefile` probe returns [] (only legacy pre-~2015 OLE2 files would yield streams).
  Conclusion: there is no free standalone reader; geometry must come via **SolidWorks COM → STEP**.
- **COM → STEP import VALIDATED on every test part** (SolidWorks installed + `pywin32`):
  | part | result |
  |---|---|
  | `25mm-diameter-circle-extruded-100mm.SLDPRT` | 1 body, vol **49087.4 mm³** = exactly π·12.5²·100 ✓ |
  | `25mm…+FEA.SLDPRT` | 1 body, vol 49087.4 ✓ |
  | `4inx8in-2inx4in-loft-4in-tall.SLDPRT` | 1 body, vol 1223567 ✓ |
  | `50mm…extrude-cut…through-all.SLDPRT` | 1 body, vol 73631 ✓ |
  | `assembly-(cylinder inside cylinder…).SLDASM` | **2 bodies** (49087 + 73631) ✓ |
  First import is slow (SolidWorks cold start ~5–12 s); subsequent ones ~1 s.

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
