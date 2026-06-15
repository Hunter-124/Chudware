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
| F5 | SolidWorks/Parasolid (experimental: SW-COM + OLE2 probe) | ⏳ next |
| F6 | Polish, docs, convert-mesh-to-solid stretch | ⏳ pending |

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
| `.sldprt` `.sldasm` | ⏳ F5 | STEP-out | experimental |
| `.x_t` `.x_b` | ⏳ F5 | STEP-out | via SW-COM only |

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
