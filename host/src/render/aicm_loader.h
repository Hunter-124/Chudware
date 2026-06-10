// AICM v1 reader (contract: docs/contracts/mesh_format.md). C++ counterpart of the Python
// reference writer/parser in engine/doc/tessellate.py. Parses a chunked AICM blob into a DocMesh:
// per-body tessellation with the picking metadata (per-FACE triangle ranges, per-EDGE polyline
// segments, B-rep VERTices) plus the SCLR/DISP/LINS overlay arrays. Unknown fourccs are skipped
// by payload_len, so the loader is forward-compatible. Pure CPU — no GL here; render/id_pass and
// render/overlays build the GPU buffers from these arrays.
#pragma once

#include <cstdint>
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

// One renderable body: geometry + picking topology. GPU buffers are NOT here (CPU arrays only);
// the id pass / overlays upload from these.
struct BodyMesh {
    uint32_t body_id = 0;
    std::string name;
    float color[3] = {0.71f, 0.76f, 0.82f};

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

// Parse an AICM blob already in memory. Returns false (and sets err) on a malformed header or a
// truncated/overrunning chunk; tolerant of trailing junk and unknown chunks otherwise.
bool parseAicm(const uint8_t* data, size_t size, DocMesh& out, std::string& err);

// Convenience: read the whole file then parse. Returns false on open failure or parse error.
bool loadAicmFile(const std::string& path, DocMesh& out, std::string& err);

}  // namespace render
