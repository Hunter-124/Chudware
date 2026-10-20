// AICM v1 reader (contract: docs/contracts/mesh_format.md). C++ counterpart of the Python
// reference writer/parser in engine/doc/tessellate.py. Parses a chunked AICM blob into a DocMesh:
// per-body tessellation with the picking metadata (per-FACE triangle ranges, per-EDGE polyline
// segments, B-rep VERTices) plus the SCLR/DISP/LINS overlay arrays. Unknown fourccs are skipped
// by payload_len, so the loader is forward-compatible. Pure CPU — no GL here; render/id_pass and
// render/overlays build the GPU buffers from these arrays.
#pragma once

#include <cstdint>
#include <cstddef>
#include <string>
#include <vector>

namespace render {

// One picking face: a contiguous run of triangles in the body's index buffer.
struct FaceRange {
    uint32_t face_id = 0;   // OCP TopExp face-map index (1-based, stable within a rebuild)
    uint32_t first_tri = 0; // first triangle (3 indices) belonging to this face
    uint32_t tri_count = 0;
    uint8_t surf_type = 0;  // 0 plane,1 cyl,2 cone,3 sphere,4 torus,5 bspline,6 other
};

// One picking edge: a polyline (run of points in the body's edge_pts array).
struct EdgeSegment {
    uint32_t edge_id = 0;   // OCP TopExp edge-map index
    uint32_t first_pt = 0;  // first point index into BodyMesh::edge_pts
    uint32_t pt_count = 0;
    uint8_t curve_type = 0; // 0 line,1 circle,2 ellipse,3 bspline,4 other
};

// One B-rep vertex (endpoint picking target).
struct BrepVertex {
    uint32_t vertex_id = 0; // OCP TopExp vertex-map index
    float xyz[3] = {0, 0, 0};
};

// Authored material over stable 1-based face IDs, independent of tessellation density.
struct AppearanceRegion {
    uint32_t first_face = 1;
    uint32_t face_count = 0;
    float color[3] = {0.62f, 0.70f, 0.82f};
    float metallic = 0.0f;
    float roughness = 0.5f;
};

// One renderable body: geometry + picking topology. GPU buffers are NOT here (CPU arrays only);
// the id pass / overlays upload from these.
struct BodyMesh {
    uint32_t body_id = 0;
    std::string name;
    float color[3] = {0.71f, 0.76f, 0.82f};
    // C32-A RealView material (dielectric default == pre-C32 look; absent in old AICM files).
    float metallic = 0.0f;
    float roughness = 0.5f;
    std::vector<AppearanceRegion> appearance_regions;

    uint32_t n_verts = 0;
    uint32_t n_tris = 0;
    std::vector<float> pos;     // 3*n_verts
    std::vector<float> nrm;     // 3*n_verts
    std::vector<uint32_t> idx;  // 3*n_tris (CCW outward)

    std::vector<FaceRange> faces;
    std::vector<EdgeSegment> edges;
    std::vector<float> edge_pts;  // 3*total_pts (polyline vertices referenced by EdgeSegment)
    std::vector<BrepVertex> brep_verts;
};

// Per-vertex scalar overlay (Evaluate / FEA results). Stored, exposed for C16/C19; minimal use now.
struct ScalarField {
    uint32_t body_id = 0;
    std::string name;
    float vmin = 0, vmax = 0;
    std::vector<float> values;  // n == that body's n_verts (NaN = no data)
};

// Per-vertex displacement overlay (deformed FEA shape).
struct DispField {
    uint32_t body_id = 0;
    float scale_hint = 1.0f;
    std::vector<float> vec;  // 3*n
};

// A polyline set (streamlines / section curves), optionally with a per-point scalar.
struct LineSet {
    std::string name;
    std::vector<uint32_t> pts_per_line;
    std::vector<float> pts;     // 3*total_pts
    bool has_scalar = false;
    std::vector<float> scalar;  // total_pts (present iff has_scalar)
};

// The whole document tessellation.
struct DocMesh {
    uint32_t version = 0;
    std::vector<BodyMesh> bodies;
    std::vector<ScalarField> scalars;
    std::vector<DispField> displacements;
    std::vector<LineSet> lines;

    bool empty() const { return bodies.empty(); }
};

// Parse an AICM blob already in memory. Returns false (and sets err, clears out) for
// unsupported versions/known record sizes, truncated chunks, wide count/product
// overflows or invalid topology ranges. Counts are checked before narrowing or
// allocating; known sections must match their declared payload exactly. Trailing
// file bytes and unknown chunks remain forward-compatible.
bool parseAicm(const uint8_t* data, size_t size, DocMesh& out, std::string& err);

// Parse AICM bytes already in memory (alias of parseAicm; shared by file/mmap and web fetch).
bool loadAicmBytes(const uint8_t* data, size_t size, DocMesh& out, std::string& err);

// Convenience: map/read (native) or HTTP-fetch (web) the path/URI then parse.
// Web: aicm-cell: workspace/document grant → authenticated /files/aicm fetch.
bool loadAicmFile(const std::string& path, DocMesh& out, std::string& err);

}  // namespace render
