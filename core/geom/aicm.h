// AICM v1 binary WRITER (pure C++, no OCCT). Format contract: contracts/mesh_format.md.
// Ported from the Python writer in services/sim/tessellate.py (write_aicm / _encode_*),
// whose BODY/FACE/EDGE/VERT encoding it shares; only this writer emits APRG.
// The GUI's host/src/render/aicm_loader is the consumer.
//
// Kept OCCT-free so it is unit-testable standalone: the OCCT tessellator
// (geom/tessellate.{h,cpp}) fills these plain structs, this file serializes them.
//
// Byte format (little-endian, packed, no padding; f32 = IEEE-754 single):
//   Header : "AICM" · u32 version=1 · u32 chunk_count
//   Chunk  : fourcc[4] · u64 payload_len · payload
//   String : u16 byte_len + UTF-8 bytes (no terminator)
// Per body, in order: BODY, FACE, EDGE, VERT, then optional APRG appearance regions.
#pragma once

#include <cstdint>
#include <initializer_list>
#include <memory>
#include <string>
#include <utility>
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

// Material over a nonempty span of stable 1-based face IDs, never triangle offsets.
struct AppearanceRegion {
    uint32_t first_face = 1;
    uint32_t face_count = 0;
    float color[3] = {0.62f, 0.70f, 0.82f};
    float metallic = 0.0f;
    float roughness = 0.5f;
};

// Copy-on-write vector: AicmBody assignment / cache-hit reuse is O(1) pointer
// traffic until a mutator (push_back/resize/…) detaches.
template <typename T>
class CowVec {
public:
    CowVec() : p_(std::make_shared<std::vector<T>>()) {}
    CowVec(std::initializer_list<T> il)
        : p_(std::make_shared<std::vector<T>>(il)) {}
    CowVec(const CowVec&) = default;
    CowVec& operator=(const CowVec&) = default;
    CowVec(CowVec&&) noexcept = default;
    CowVec& operator=(CowVec&&) noexcept = default;
    CowVec& operator=(std::initializer_list<T> il) {
        p_ = std::make_shared<std::vector<T>>(il);
        return *this;
    }

    size_t size() const { return p_->size(); }
    bool empty() const { return p_->empty(); }
    void reserve(size_t n) { detach(); p_->reserve(n); }
    void resize(size_t n) { detach(); p_->resize(n); }
    void resize(size_t n, const T& v) { detach(); p_->resize(n, v); }
    void clear() { detach(); p_->clear(); }
    void assign(size_t n, const T& v) { detach(); p_->assign(n, v); }
    void push_back(const T& v) { detach(); p_->push_back(v); }
    void push_back(T&& v) { detach(); p_->push_back(std::move(v)); }
    template <typename... Args>
    void emplace_back(Args&&... args) {
        detach();
        p_->emplace_back(std::forward<Args>(args)...);
    }
    // Append range (tessellate: was insert(end(), first, last) on std::vector).
    template <typename InputIt>
    void insert_end(InputIt first, InputIt last) {
        detach();
        p_->insert(p_->end(), first, last);
    }

    T& operator[](size_t i) { detach(); return (*p_)[i]; }
    const T& operator[](size_t i) const { return (*p_)[i]; }
    T& back() { detach(); return p_->back(); }
    const T& back() const { return p_->back(); }

    typename std::vector<T>::iterator begin() { detach(); return p_->begin(); }
    typename std::vector<T>::iterator end() { detach(); return p_->end(); }
    typename std::vector<T>::const_iterator begin() const { return p_->begin(); }
    typename std::vector<T>::const_iterator end() const { return p_->end(); }
    typename std::vector<T>::const_iterator cbegin() const { return p_->cbegin(); }
    typename std::vector<T>::const_iterator cend() const { return p_->cend(); }

    // Const view — never detaches (encode/serialize path).
    const std::vector<T>& vec() const { return *p_; }
    operator const std::vector<T>&() const { return *p_; }

private:
    void detach() {
        if (p_.use_count() != 1) p_ = std::make_shared<std::vector<T>>(*p_);
    }
    std::shared_ptr<std::vector<T>> p_;
};

// A fully tessellated body ready to serialize. Geometry is stored flat
// (3*n_verts for pos/nrm, 3*n_tris for idx, 3*total_pts for edge_pts) — mirrors
// the BODY/FACE/EDGE/VERT chunk payloads exactly. Heavy buffers are COW so a
// body-cache hit re-stamps body_id/name without deep-copying mesh arrays.
struct AicmBody {
    uint32_t body_id = 0;
    std::string name;
    float color[3] = {0.71f, 0.76f, 0.82f};

    CowVec<float> pos;     // 3 * n_verts
    CowVec<float> nrm;     // 3 * n_verts
    CowVec<uint32_t> idx;  // 3 * n_tris

    CowVec<FaceRec> faces;
    CowVec<EdgeRec> edges;
    CowVec<float> edge_pts;  // 3 * total_pts (polyline vertices)
    CowVec<VertRec> verts;

    // C32-A RealView material trailer (dielectric default == pre-C32 look).
    float metallic = 0.0f;
    float roughness = 0.5f;
    std::vector<AppearanceRegion> appearance_regions;

    uint32_t n_verts() const { return static_cast<uint32_t>(pos.size() / 3); }
    uint32_t n_tris() const { return static_cast<uint32_t>(idx.size() / 3); }
    uint32_t total_edge_pts() const { return static_cast<uint32_t>(edge_pts.size() / 3); }
};

// --- Overlay chunk encoders (same payloads as services/sim/tessellate.py scalar/disp/lines) ---
// Each returns a complete length-prefixed chunk (fourcc[4] · u64 len · payload),
// ready to pass in write_aicm's extra_chunks. host/src/render/aicm_loader is the
// consumer (SCLR/DISP/LINS branches).
//
// SCLR payload: u32 body_id · str name · f32 vmin · f32 vmax · u32 count · f32[count]
std::vector<uint8_t> scalar_chunk(uint32_t body_id, const std::string& name,
                                  const std::vector<float>& values, float vmin, float vmax);

// DISP payload: u32 body_id · f32 scale_hint · u32 n · f32[3*n]  (vectors is flat 3*n)
std::vector<uint8_t> disp_chunk(uint32_t body_id, float scale_hint,
                                const std::vector<float>& vectors);

// LINS payload: str name · u32 n_lines · u32[n_lines] pts_per_line · u32 total_pts ·
//   f32[3*total_pts] · u8 has_scalar · (f32[total_pts] scalars iff has_scalar).
// `polylines` is a list of flat 3*Ni point arrays; `scalars` (when non-null, one Ni
// per polyline) sets has_scalar=1 and appends the concatenated per-point scalars.
std::vector<uint8_t> lines_chunk(const std::string& name,
                                 const std::vector<std::vector<float>>& polylines,
                                 const std::vector<std::vector<float>>* scalars = nullptr);

// Serialize a list of bodies to an AICM v1 byte blob (deterministic). Any
// pre-encoded `extra_chunks` (SCLR/DISP/LINS …) are appended after the body chunks
// and counted in the header chunk_count — the same order as the Python write_aicm.
std::vector<uint8_t> write_aicm(const std::vector<AicmBody>& bodies,
                                const std::vector<std::vector<uint8_t>>& extra_chunks = {});

// Convenience: serialize and write to a file. Returns false on open/write failure.
bool write_aicm_file(const std::string& path, const std::vector<AicmBody>& bodies,
                     const std::vector<std::vector<uint8_t>>& extra_chunks = {});

}  // namespace aicm
