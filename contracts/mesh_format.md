# Contract: AICM v1 — tessellation + overlay binary format

> One format for everything the viewport draws: B-rep tessellation with picking metadata,
> Evaluate scalar overlays, FEA results, CFD streamlines. Unknown chunks are skipped (forward
> compatible). Golden fixture: `fixtures/box.aicm` (+ generator `fixtures/make_box_aicm.py`,
> byte-stable). Python writer: `services/sim/tessellate.py`. C++ reader: `host/src/render/aicm_loader`.

## 1. Encoding rules

- **Little-endian, packed** (no alignment padding). `f32` = IEEE 754 single. `u8/u16/u32/u64`
  unsigned. Strings: `u16 byte_len` + UTF-8 bytes (no terminator).
- File header: `4 bytes magic "AICM"` · `u32 version = 1` · `u32 chunk_count`.
- Chunk header: `4 bytes fourcc` · `u64 payload_len` · payload.
- `FACE`/`EDGE`/`VERT` chunks bind to the **most recent preceding `BODY`** chunk.
- Readers MUST skip unknown fourccs using `payload_len`.

## 2. Chunks

### `BODY` — one renderable body
```
u32 body_id          // stable within one rebuild only
str name             // "Body1", "sketch:f3" for sketch-region pseudo-bodies
f32 color[3]         // 0..1 sRGB
u32 n_verts
u32 n_tris
f32 pos[3*n_verts]
f32 nrm[3*n_verts]   // per-vertex, face-orientation corrected
u32 idx[3*n_tris]    // CCW outward winding
```

### `FACE` — picking ranges over the body's triangles
```
u32 n_faces
repeat n_faces:
  u32 face_id        // OCP TopExp face-map index (stable within rebuild)
  u32 first_tri      // triangles are written grouped per face → contiguous ranges
  u32 tri_count
  u8  surf_type      // 0 plane, 1 cylinder, 2 cone, 3 sphere, 4 torus, 5 bspline, 6 other
```

### `EDGE` — feature/boundary edge polylines
```
u32 n_edges
repeat n_edges:
  u32 edge_id        // OCP TopExp edge-map index
  u32 first_pt       // index into the pts array below
  u32 pt_count
  u8  curve_type     // 0 line, 1 circle, 2 ellipse, 3 bspline, 4 other
u32 total_pts
f32 pts[3*total_pts]
```

### `VERT` — B-rep vertices (endpoint picking)
```
u32 n
repeat n: { u32 vertex_id; f32 xyz[3] }
```

### `SCLR` — per-vertex scalar field (overlays: draft, thickness, curvature, von Mises, FoS, |v|)
```
u32 body_id          // must match a BODY in this file
str name             // "von_mises", "thickness", …
f32 vmin, f32 vmax   // legend range (may differ from data extremes for clamped maps)
u32 n                // == that BODY's n_verts
f32 v[n]             // NaN = no data (render neutral)
```

### `DISP` — per-vertex displacement (FEA deformed shape)
```
u32 body_id
f32 scale_hint       // suggested display multiplier for unit visual deformation
u32 n
f32 vec[3*n]
```

### `LINS` — polyline set (streamlines, section curves)
```
str name
u32 n_lines
u32 pts_per_line[n_lines]
u32 total_pts        // == sum(pts_per_line)
f32 pts[3*total_pts]
u8  has_scalar       // 0|1
f32 s[total_pts]     // present iff has_scalar (e.g. speed for streamline coloring)
```

## 3. Producer requirements

- Triangles MUST be grouped per face so `FACE` ranges are contiguous (the per-face OCP
  triangulations make this natural).
- Normals via `BRepLib_ToolTriangulatedShape.ComputeNormals_s`, flipped (with winding) for
  `TopAbs_REVERSED` faces.
- Edge polylines via `BRep_Tool.PolygonOnTriangulation_s` against an adjacent face's
  triangulation (`TopExp.MapShapesAndAncestors_s`); fallback `GCPnts_TangentialDeflection`.
- Sketch profile regions are exported as a pseudo-body named `"sketch:<feature-id>"` whose FACE
  entries use `face_id = region hint_index` (pickable as extrude/revolve profiles).

## 4. Golden fixture

`fixtures/box.aicm`: 20×10×6 box centered at origin. 24 verts (4/face × 6 faces, axis-aligned
normals), 12 tris, 12 edges (2-pt polylines), 8 B-rep vertices, face order −X,+X,−Y,+Y,−Z,+Z,
ids 1…; body_id 1, name "Body1", color (0.71, 0.76, 0.82). The generator is deterministic;
tests regenerate and byte-compare against the committed file. The C++ loader test parses the same
file and asserts counts/ranges.
