#include "aicm.h"

#include <cstdio>
#include <cstring>

namespace aicm {

namespace {

// Little-endian append helpers. The host (and the build target) are x86-64
// little-endian, and the format mandates LE, so a raw memcpy of the in-memory
// integer/float bytes is correct here. (If this is ever ported to a big-endian
// target these must byte-swap; we keep them centralized for that reason.)
inline void put_u8(std::vector<uint8_t>& b, uint8_t v) { b.push_back(v); }

inline void put_u16(std::vector<uint8_t>& b, uint16_t v) {
    b.push_back(static_cast<uint8_t>(v & 0xFF));
    b.push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
}

inline void put_u32(std::vector<uint8_t>& b, uint32_t v) {
    b.push_back(static_cast<uint8_t>(v & 0xFF));
    b.push_back(static_cast<uint8_t>((v >> 8) & 0xFF));
    b.push_back(static_cast<uint8_t>((v >> 16) & 0xFF));
    b.push_back(static_cast<uint8_t>((v >> 24) & 0xFF));
}

inline void put_u64(std::vector<uint8_t>& b, uint64_t v) {
    for (int i = 0; i < 8; ++i) b.push_back(static_cast<uint8_t>((v >> (8 * i)) & 0xFF));
}

inline void put_f32(std::vector<uint8_t>& b, float f) {
    uint32_t u;
    std::memcpy(&u, &f, 4);  // bit-cast; IEEE-754 single
    put_u32(b, u);
}

inline void put_str(std::vector<uint8_t>& b, const std::string& s) {
    // u16 byte_len + UTF-8 bytes, no terminator. Strings are short display names;
    // clamp defensively so an absurd name can't overflow the u16 length field.
    size_t n = s.size() > 0xFFFF ? 0xFFFF : s.size();
    put_u16(b, static_cast<uint16_t>(n));
    b.insert(b.end(), s.begin(), s.begin() + static_cast<std::ptrdiff_t>(n));
}

inline void put_f32_array(std::vector<uint8_t>& b, const std::vector<float>& a) {
    for (float f : a) put_f32(b, f);
}

inline void put_u32_array(std::vector<uint8_t>& b, const std::vector<uint32_t>& a) {
    for (uint32_t v : a) put_u32(b, v);
}

// --- per-chunk payload encoders (mirror tessellate.py _encode_*) -------------

std::vector<uint8_t> encode_body(const AicmBody& bt) {
    std::vector<uint8_t> p;
    put_u32(p, bt.body_id);
    put_str(p, bt.name);
    put_f32(p, bt.color[0]);
    put_f32(p, bt.color[1]);
    put_f32(p, bt.color[2]);
    put_u32(p, bt.n_verts());
    put_u32(p, bt.n_tris());
    put_f32_array(p, bt.pos.vec());
    put_f32_array(p, bt.nrm.vec());
    put_u32_array(p, bt.idx.vec());
    // C32-A material trailer (metallic, roughness) appended after idx.
    put_f32(p, bt.metallic);
    put_f32(p, bt.roughness);
    return p;
}

std::vector<uint8_t> encode_face(const AicmBody& bt) {
    std::vector<uint8_t> p;
    put_u32(p, static_cast<uint32_t>(bt.faces.size()));
    for (const FaceRec& fr : bt.faces) {
        put_u32(p, fr.face_id);
        put_u32(p, fr.first_tri);
        put_u32(p, fr.tri_count);
        put_u8(p, fr.surf_type);
    }
    return p;
}

std::vector<uint8_t> encode_edge(const AicmBody& bt) {
    std::vector<uint8_t> p;
    put_u32(p, static_cast<uint32_t>(bt.edges.size()));
    for (const EdgeRec& er : bt.edges) {
        put_u32(p, er.edge_id);
        put_u32(p, er.first_pt);
        put_u32(p, er.pt_count);
        put_u8(p, er.curve_type);
    }
    put_u32(p, bt.total_edge_pts());
    put_f32_array(p, bt.edge_pts.vec());
    return p;
}

std::vector<uint8_t> encode_vert(const AicmBody& bt) {
    std::vector<uint8_t> p;
    put_u32(p, static_cast<uint32_t>(bt.verts.size()));
    for (const VertRec& vr : bt.verts) {
        put_u32(p, vr.vertex_id);
        put_f32(p, vr.xyz[0]);
        put_f32(p, vr.xyz[1]);
        put_f32(p, vr.xyz[2]);
    }
    return p;
}

// Build one length-prefixed chunk: fourcc[4] · u64 payload_len · payload.
std::vector<uint8_t> make_chunk(const char fourcc[4], const std::vector<uint8_t>& payload) {
    std::vector<uint8_t> c;
    c.insert(c.end(), fourcc, fourcc + 4);
    put_u64(c, static_cast<uint64_t>(payload.size()));
    c.insert(c.end(), payload.begin(), payload.end());
    return c;
}

// Append one length-prefixed chunk in place (header chunk emit).
void emit_chunk(std::vector<uint8_t>& out, const char fourcc[4],
                const std::vector<uint8_t>& payload) {
    out.insert(out.end(), fourcc, fourcc + 4);
    put_u64(out, static_cast<uint64_t>(payload.size()));
    out.insert(out.end(), payload.begin(), payload.end());
}

}  // namespace

// --- Overlay chunk encoders (mirror tessellate.py scalar_chunk/disp_chunk/lines_chunk) ---

std::vector<uint8_t> scalar_chunk(uint32_t body_id, const std::string& name,
                                  const std::vector<float>& values, float vmin, float vmax) {
    std::vector<uint8_t> p;
    put_u32(p, body_id);
    put_str(p, name);
    put_f32(p, vmin);
    put_f32(p, vmax);
    put_u32(p, static_cast<uint32_t>(values.size()));
    put_f32_array(p, values);
    return make_chunk("SCLR", p);
}

std::vector<uint8_t> disp_chunk(uint32_t body_id, float scale_hint,
                                const std::vector<float>& vectors) {
    std::vector<uint8_t> p;
    put_u32(p, body_id);
    put_f32(p, scale_hint);
    // n = floor(len/3), mirroring tessellate.py disp_chunk (vec flattened, n = len//3).
    put_u32(p, static_cast<uint32_t>(vectors.size() / 3));
    // Only the first 3*n floats are written (tessellate.py packs vec.tobytes() of the
    // flattened array, which is 3*n long once reshaped through (n,3); a non-multiple of
    // 3 trailing remainder is dropped). Match that exactly.
    const size_t n3 = (vectors.size() / 3) * 3;
    for (size_t i = 0; i < n3; ++i) put_f32(p, vectors[i]);
    return make_chunk("DISP", p);
}

std::vector<uint8_t> lines_chunk(const std::string& name,
                                 const std::vector<std::vector<float>>& polylines,
                                 const std::vector<std::vector<float>>* scalars) {
    std::vector<uint8_t> p;
    put_str(p, name);
    put_u32(p, static_cast<uint32_t>(polylines.size()));
    uint32_t total_pts = 0;
    for (const auto& pl : polylines) {
        uint32_t n = static_cast<uint32_t>(pl.size() / 3);
        put_u32(p, n);
        total_pts += n;
    }
    put_u32(p, total_pts);
    for (const auto& pl : polylines) {
        const size_t n3 = (pl.size() / 3) * 3;
        for (size_t i = 0; i < n3; ++i) put_f32(p, pl[i]);
    }
    const uint8_t has_scalar = scalars ? 1 : 0;
    put_u8(p, has_scalar);
    if (scalars) {
        for (const auto& s : *scalars) put_f32_array(p, s);
    }
    return make_chunk("LINS", p);
}

std::vector<uint8_t> write_aicm(const std::vector<AicmBody>& bodies,
                                const std::vector<std::vector<uint8_t>>& extra_chunks) {
    std::vector<uint8_t> out;
    // Header: "AICM" · version=1 · chunk_count (4 per body + the extra chunks).
    const char magic[4] = {'A', 'I', 'C', 'M'};
    out.insert(out.end(), magic, magic + 4);
    put_u32(out, 1);
    put_u32(out, static_cast<uint32_t>(bodies.size() * 4 + extra_chunks.size()));

    for (const AicmBody& bt : bodies) {
        emit_chunk(out, "BODY", encode_body(bt));
        emit_chunk(out, "FACE", encode_face(bt));
        emit_chunk(out, "EDGE", encode_edge(bt));
        emit_chunk(out, "VERT", encode_vert(bt));
    }
    // Append pre-encoded extra chunks (SCLR/DISP/LINS) after the body chunks — exactly
    // as tessellate.py: all_chunks = body_chunks + list(extra_chunks).
    for (const std::vector<uint8_t>& ec : extra_chunks)
        out.insert(out.end(), ec.begin(), ec.end());
    return out;
}

bool write_aicm_file(const std::string& path, const std::vector<AicmBody>& bodies,
                     const std::vector<std::vector<uint8_t>>& extra_chunks) {
    std::vector<uint8_t> blob = write_aicm(bodies, extra_chunks);
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    size_t want = blob.size();
    size_t got = want ? std::fwrite(blob.data(), 1, want, f) : 0;
    std::fclose(f);
    return got == want;
}

}  // namespace aicm
