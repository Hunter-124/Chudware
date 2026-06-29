# C02 — AICM tessellation writer (+ reference parser)

Model: **sonnet** · Wave 0 · Depends: none · Follow `docs/sessions/README.md` conventions.

## Mission
Write `engine/doc/tessellate.py`: OCCT shapes → AICM v1 binary (the viewport/picking format),
plus a pure-Python parser used by tests and later by tooling.

## Read first
1. `contracts/mesh_format.md` — the byte-level spec, normative
2. `contracts/fixtures/make_box_aicm.py` + `fixtures/box.aicm` — golden encoding example
3. `docs/PLAN.md` §3 "Architecture (validated)" — the EXACT verified OCP call paths (edge
   polylines via `PolygonOnTriangulation_s`, NOT `Polygon3D_s`; normals via
   `BRepLib_ToolTriangulatedShape.ComputeNormals_s`; orientation flip for REVERSED faces)

## Owned paths
`engine/doc/tessellate.py` · `engine/doc/test_aicm.py`

## Deliverables
1. `tessellate.py`:
   - `tessellate_body(shape, body_id, name, color, lin_deflection=0.1, ang_deflection=0.5)
     -> BodyTess` (numpy arrays: pos, nrm, idx grouped per face; face records id/first/count/
     surf_type; edge polylines with ids/curve types; brep vertex points)
   - `write_aicm(bodies: list[BodyTess], extra_chunks=()) -> bytes` and
     `write_aicm_file(path, ...)`; `scalar_chunk(body_id, name, values, vmin=None, vmax=None)`,
     `disp_chunk(...)`, `lines_chunk(...)` helpers so Evaluate/FEA/CFD reuse this writer.
   - `read_aicm(data: bytes) -> dict` — full parser (all chunk types), tolerant of unknown
     fourccs (skip).
   - Implementation path (verified on this machine's OCP 7.8):
     `BRepMesh_IncrementalMesh(shape.wrapped, 0.1, False, 0.5, True)` →
     `TopExp.MapShapes_s` for FACE/EDGE/VERTEX index maps →
     per face: `BRep_Tool.Triangulation_s(face, TopLoc_Location())`, apply the location
     transform to nodes, `BRepLib_ToolTriangulatedShape.ComputeNormals_s` for normals, flip
     normal AND winding when `face.Orientation() == TopAbs_REVERSED` →
     per edge: `BRep_Tool.PolygonOnTriangulation_s(edge, tri_of_adjacent_face, loc)` (adjacent
     face via `TopExp.MapShapesAndAncestors_s`), fallback `GCPnts_TangentialDeflection` →
     surf/curve types via `BRepAdaptor_Surface/Curve(...).GetType()`.
   - Triangles MUST be written grouped per face (contiguous FACE ranges).
2. `test_aicm.py`:
   - parser vs golden: `read_aicm(fixtures/box.aicm)` → 1 body, 24 verts, 12 tris, 6 faces with
     contiguous ranges covering all tris, 12 edges, 8 verts
   - real shape: tessellate `build123d.Box(20,10,6)` then a filleted+bored box; parse own
     output; assert: header/version ok, FACE ranges contiguous + cover n_tris exactly, every
     normal unit-length ±1e-3, normals point OUTWARD (dot(normal, vert−body_center) > 0 for a
     convex box), edge count == TopExp edge-map extent, cylinder face has surf_type 1
   - scalar/disp/lines chunks roundtrip through write→read

## Constraints
- numpy + OCP + build123d only. No file-format deviations — if the contract is ambiguous,
  match `make_box_aicm.py`'s encoding exactly and note the ambiguity in your report.
- Do not edit `engine/doc/__init__.py` — report desired exports.

## Verification
`& $py -m engine.doc.test_aicm` green; `& $py -m engine.doc.test_schema_fixtures` still green.
