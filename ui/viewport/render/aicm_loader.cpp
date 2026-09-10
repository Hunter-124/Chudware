#include "aicm_loader.h"

#include "util/aicm_handoff.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>

namespace render {

namespace {

// A bounds-checked little-endian cursor over the blob. Any read past `end` flips `ok` and
// thereafter returns zeroes, so a truncated file fails cleanly instead of reading garbage.
struct Cursor {
    const uint8_t* p;
    const uint8_t* end;
    bool ok = true;

    Cursor(const uint8_t* d, size_t n) : p(d), end(d + n) {}

    size_t remaining() const { return ok ? (size_t)(end - p) : 0; }

    void read(void* dst, size_t n) {
        if (!ok || (size_t)(end - p) < n) {
            ok = false;
            std::memset(dst, 0, n);
            return;
        }
        std::memcpy(dst, p, n);
        p += n;
    }
    void skip(size_t n) {
        if (!ok || (size_t)(end - p) < n) { ok = false; return; }
        p += n;
    }

    uint8_t u8() { uint8_t v = 0; read(&v, 1); return v; }
    uint16_t u16() { uint16_t v = 0; read(&v, 2); return v; }
    uint32_t u32() { uint32_t v = 0; read(&v, 4); return v; }
    uint64_t u64() { uint64_t v = 0; read(&v, 8); return v; }
    float f32() { float v = 0; read(&v, 4); return v; }

    std::string str() {
        uint16_t blen = u16();
        std::string s;
        if (ok && (size_t)(end - p) >= blen) {
            s.assign(reinterpret_cast<const char*>(p), blen);
            p += blen;
        } else if (blen) {
            ok = false;
        }
        return s;
    }

    void floats(std::vector<float>& dst, size_t count) {
        if (count > remaining() / sizeof(float)) { ok = false; return; }
        dst.resize(count);
        if (count) read(dst.data(), count * 4);  // IEEE-754 LE matches in-memory float on MSVC/x64
    }
    void uints(std::vector<uint32_t>& dst, size_t count) {
        if (count > remaining() / sizeof(uint32_t)) { ok = false; return; }
        dst.resize(count);
        if (count) read(dst.data(), count * 4);
    }
};

}  // namespace

bool parseAicm(const uint8_t* data, size_t size, DocMesh& out, std::string& err) {
    out = DocMesh{};
    Cursor c(data, size);

    char magic[4];
    c.read(magic, 4);
    if (!c.ok || std::memcmp(magic, "AICM", 4) != 0) {
        err = "bad magic (not an AICM file)";
        return false;
    }
    out.version = c.u32();
    uint32_t chunk_count = c.u32();
    if (!c.ok) { err = "truncated header"; return false; }

    // FACE/EDGE/VERT bind to the most recent preceding BODY.
    BodyMesh* current = nullptr;
    std::map<uint32_t, std::vector<AppearanceRegion>> appearances;

    for (uint32_t ci = 0; ci < chunk_count; ++ci) {
        char fourcc[4];
        c.read(fourcc, 4);
        uint64_t payload_len = c.u64();
        if (!c.ok) { err = "truncated chunk header"; return false; }
        const uint8_t* payload_start = c.p;
        if (payload_len > c.remaining()) {
            err = "chunk payload overruns file";
            return false;
        }
        const uint8_t* file_end = c.end;
        c.end = payload_start + payload_len;

        auto fcc = [&](const char* s) { return std::memcmp(fourcc, s, 4) == 0; };

        if (fcc("BODY")) {
            out.bodies.emplace_back();
            current = &out.bodies.back();
            current->body_id = c.u32();
            current->name = c.str();
            current->color[0] = c.f32();
            current->color[1] = c.f32();
            current->color[2] = c.f32();
            current->n_verts = c.u32();
            current->n_tris = c.u32();
            c.floats(current->pos, 3ull * current->n_verts);
            c.floats(current->nrm, 3ull * current->n_verts);
            c.uints(current->idx, 3ull * current->n_tris);
            // C32-A material trailer: present iff the chunk has >=8 bytes left after idx. Old AICM
            // files (no trailer) keep the dielectric defaults; the boundary re-sync below skips any
            // bytes either way, so this stays forward-compatible.
            if (c.ok && (size_t)(c.p - payload_start) + 8 <= payload_len) {
                current->metallic = c.f32();
                current->roughness = c.f32();
            }
        } else if (fcc("FACE")) {
            uint32_t n = c.u32();
            if (!c.ok || n > c.remaining() / 13) { err = "truncated FACE payload"; return false; }
            std::vector<FaceRange> faces;
            faces.reserve(n);
            for (uint32_t i = 0; i < n; ++i) {
                FaceRange f;
                f.face_id = c.u32();
                f.first_tri = c.u32();
                f.tri_count = c.u32();
                f.surf_type = c.u8();
                faces.push_back(f);
            }
            if (current) current->faces = std::move(faces);
        } else if (fcc("EDGE")) {
            uint32_t n = c.u32();
            if (!c.ok || n > c.remaining() / 13) { err = "truncated EDGE payload"; return false; }
            std::vector<EdgeSegment> edges;
            edges.reserve(n);
            for (uint32_t i = 0; i < n; ++i) {
                EdgeSegment e;
                e.edge_id = c.u32();
                e.first_pt = c.u32();
                e.pt_count = c.u32();
                e.curve_type = c.u8();
                edges.push_back(e);
            }
            uint32_t total_pts = c.u32();
            std::vector<float> pts;
            c.floats(pts, 3ull * total_pts);
            if (current) {
                current->edges = std::move(edges);
                current->edge_pts = std::move(pts);
            }
        } else if (fcc("VERT")) {
            uint32_t n = c.u32();
            if (!c.ok || n > c.remaining() / 16) { err = "truncated VERT payload"; return false; }
            std::vector<BrepVertex> verts;
            verts.reserve(n);
            for (uint32_t i = 0; i < n; ++i) {
                BrepVertex v;
                v.vertex_id = c.u32();
                v.xyz[0] = c.f32();
                v.xyz[1] = c.f32();
                v.xyz[2] = c.f32();
                verts.push_back(v);
            }
            if (current) current->brep_verts = std::move(verts);
        } else if (fcc("APRG")) {
            const uint32_t body_id = c.u32();
            const uint32_t count = c.u32();
            if (!c.ok || c.remaining() % 28 != 0 || count != c.remaining() / 28) {
                err = "invalid APRG payload length";
                return false;
            }
            auto inserted = appearances.emplace(body_id, std::vector<AppearanceRegion>{});
            if (!inserted.second) { err = "duplicate APRG body"; return false; }
            auto& regions = inserted.first->second;
            regions.reserve(count);
            const auto unit = [](float value) {
                return std::isfinite(value) && value >= 0.0f && value <= 1.0f;
            };
            for (uint32_t i = 0; i < count; ++i) {
                AppearanceRegion region;
                region.first_face = c.u32();
                region.face_count = c.u32();
                for (float& color : region.color) color = c.f32();
                region.metallic = c.f32();
                region.roughness = c.f32();
                if (!region.first_face || !region.face_count ||
                    uint64_t(region.first_face) + region.face_count - 1 >
                        std::numeric_limits<uint32_t>::max()) {
                    err = "invalid APRG face span";
                    return false;
                }
                if (!unit(region.color[0]) || !unit(region.color[1]) || !unit(region.color[2]) ||
                    !unit(region.metallic) || !unit(region.roughness)) {
                    err = "invalid APRG material";
                    return false;
                }
                regions.push_back(region);
            }
        } else if (fcc("SCLR")) {
            ScalarField sf;
            sf.body_id = c.u32();
            sf.name = c.str();
            sf.vmin = c.f32();
            sf.vmax = c.f32();
            uint32_t n = c.u32();
            c.floats(sf.values, n);
            out.scalars.push_back(std::move(sf));
        } else if (fcc("DISP")) {
            DispField df;
            df.body_id = c.u32();
            df.scale_hint = c.f32();
            uint32_t n = c.u32();
            c.floats(df.vec, 3ull * n);
            out.displacements.push_back(std::move(df));
        } else if (fcc("LINS")) {
            LineSet ls;
            ls.name = c.str();
            uint32_t n_lines = c.u32();
            c.uints(ls.pts_per_line, n_lines);
            uint32_t total_pts = c.u32();
            c.floats(ls.pts, 3ull * total_pts);
            ls.has_scalar = c.u8() != 0;
            if (ls.has_scalar) c.floats(ls.scalar, total_pts);
            out.lines.push_back(std::move(ls));
        }
        // else: unknown fourcc — fall through and skip by payload_len below.

        if (!c.ok) { err = "truncated chunk payload"; return false; }

        // Re-sync to the declared chunk boundary regardless of how much we consumed; this both
        // skips unknown chunks and guards against any per-chunk parse drift (spec requirement).
        c.p = payload_start + payload_len;
        c.end = file_end;
    }

    std::map<uint32_t, BodyMesh*> appearance_bodies;
    if (!appearances.empty()) {
        for (BodyMesh& body : out.bodies) {
            if (appearances.find(body.body_id) == appearances.end()) continue;
            auto inserted = appearance_bodies.emplace(body.body_id, &body);
            if (!inserted.second) inserted.first->second = nullptr;
        }
    }
    for (auto& appearance : appearances) {
        const auto target = appearance_bodies.find(appearance.first);
        if (target == appearance_bodies.end()) { err = "APRG references unknown body"; return false; }
        if (!target->second) {
            err = "ambiguous APRG body";
            return false;
        }
        BodyMesh& body = *target->second;
        std::vector<uint32_t> face_ids;
        face_ids.reserve(body.faces.size());
        for (const FaceRange& face : body.faces) face_ids.push_back(face.face_id);
        std::sort(face_ids.begin(), face_ids.end());
        if (std::adjacent_find(face_ids.begin(), face_ids.end()) != face_ids.end()) {
            err = "ambiguous APRG face IDs";
            return false;
        }
        std::vector<std::pair<uint32_t, uint64_t>> spans;
        spans.reserve(appearance.second.size());
        for (const AppearanceRegion& region : appearance.second) {
            const uint64_t end_face = uint64_t(region.first_face) + region.face_count;
            const auto first = std::lower_bound(face_ids.begin(), face_ids.end(), region.first_face);
            if (first == face_ids.end() || *first != region.first_face ||
                region.face_count > static_cast<size_t>(face_ids.end() - first) ||
                first[region.face_count - 1] != end_face - 1) {
                err = "APRG references missing faces";
                return false;
            }
            spans.emplace_back(region.first_face, end_face);
        }
        std::sort(spans.begin(), spans.end());
        for (size_t i = 1; i < spans.size(); ++i) {
            if (spans[i].first < spans[i - 1].second) {
                err = "overlapping APRG face spans";
                return false;
            }
        }
        body.appearance_regions = std::move(appearance.second);
    }

    return true;
}

bool loadAicmFile(const std::string& path, DocMesh& out, std::string& err) {
    // T-PERF10: aicm-shm:// URIs (and same-process cache) via aicm_handoff.
    chudware::aicm_handoff::Mapping m;
    if (!chudware::aicm_handoff::map(path, m, err)) return false;
    const bool ok = parseAicm(m.data, m.size, out, err);
    chudware::aicm_handoff::unmap(m);
    return ok;
}

}  // namespace render
