// AICM v1 binary WRITER (pure C++, no OCCT) — C++ port of the encoder half of
// engine/doc/tessellate.py (write_aicm / _encode_*). Format contract:
// docs/contracts/mesh_format.md. The host's render/aicm_loader is the consumer.
//
// Kept OCCT-free so it is unit-testable standalone: the OCCT tessellator
// (geom/tessellate.{h,cpp}) fills these plain structs, this file serializes them.
//
// Byte format (little-endian, packed, no padding; f32 = IEEE-754 single):
//   Header : "AICM" · u32 version=1 · u32 chunk_count
//   Chunk  : fourcc[4] · u64 payload_len · payload
//   String : u16 byte_len + UTF-8 bytes (no terminator)
// Per body, in order: BODY, FACE, EDGE, VERT. chunk_count = 4 * n_bodies.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace aicm {

// One picking face: a contiguous run of triangles in the body's index buffer.
struct FaceRec {
    uint32_t face_id = 0;
    uint32_t first_tri = 0;
    uint32_t tri_count = 0;
    uint8_t surf_type = 0;  // 0 plane,1 cyl,2 cone,3 sphere,4 torus,5 bspline,6 other
};

// One picking edge record (the polyline points live in AicmBody::edge_pts).
struct EdgeRec {
    uint32_t edge_id = 0;
    uint32_t first_pt = 0;
    uint32_t pt_count = 0;
    uint8_t curve_type = 0;  // 0 line,1 circle,2 ellipse,3 bspline,4 other
};

// One B-rep vertex.
struct VertRec {
    uint32_t vertex_id = 0;
    float xyz[3] = {0, 0, 0};
};

// A fully tessellated body ready to serialize. Geometry is stored flat
// (3*n_verts for pos/nrm, 3*n_tris for idx, 3*total_pts for edge_pts) — mirrors
// the BODY/FACE/EDGE/VERT chunk payloads exactly.
struct AicmBody {
    uint32_t body_id = 0;
    std::string name;
    float color[3] = {0.71f, 0.76f, 0.82f};

    std::vector<float> pos;     // 3 * n_verts
    std::vector<float> nrm;     // 3 * n_verts
    std::vector<uint32_t> idx;  // 3 * n_tris

    std::vector<FaceRec> faces;
    std::vector<EdgeRec> edges;
    std::vector<float> edge_pts;  // 3 * total_pts (polyline vertices)
    std::vector<VertRec> verts;

    // C32-A RealView material trailer (dielectric default == pre-C32 look).
    float metallic = 0.0f;
    float roughness = 0.5f;

    uint32_t n_verts() const { return static_cast<uint32_t>(pos.size() / 3); }
    uint32_t n_tris() const { return static_cast<uint32_t>(idx.size() / 3); }
    uint32_t total_edge_pts() const { return static_cast<uint32_t>(edge_pts.size() / 3); }
};

// Serialize a list of bodies to an AICM v1 byte blob (deterministic).
std::vector<uint8_t> write_aicm(const std::vector<AicmBody>& bodies);

// Convenience: serialize and write to a file. Returns false on open/write failure.
bool write_aicm_file(const std::string& path, const std::vector<AicmBody>& bodies);

}  // namespace aicm
