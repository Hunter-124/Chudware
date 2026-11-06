#include "aicm_loader.h"

#include "util/aicm_handoff.h"

#ifdef __EMSCRIPTEN__
#include "core/web_fetch.h"
#include <nlohmann/json.hpp>
#include <stdexcept>
#endif

#include <algorithm>
#include <cmath>
#include <limits>
#include <cstdio>
#include <cstring>
#include <list>
#include <mutex>
#include <utility>
#include <string>
#include <unordered_map>
#include <vector>

#ifndef __EMSCRIPTEN__
#include <filesystem>
#include <fstream>
#endif

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

    // Keep products wide until BOTH the payload and the destination representation
    // have been checked. In wasm32, narrowing 3 * 0x55555556 first yields 2.
    template<class T>
    bool arrayFits(uint64_t count) const {
        return ok && count <= remaining() / sizeof(T) &&
               count <= std::vector<T>{}.max_size() &&
               count <= uint64_t(std::numeric_limits<std::ptrdiff_t>::max()) / sizeof(T);
    }
    void floats(std::vector<float>& dst, uint64_t count) {
        if (!arrayFits<float>(count)) { ok = false; return; }
        dst.resize(static_cast<size_t>(count));
        if (count) read(dst.data(), static_cast<size_t>(count) * sizeof(float));
    }
    void uints(std::vector<uint32_t>& dst, uint64_t count) {
        if (!arrayFits<uint32_t>(count)) { ok = false; return; }
        dst.resize(static_cast<size_t>(count));
        if (count) read(dst.data(), static_cast<size_t>(count) * sizeof(uint32_t));
    }
};

void prepareDerivedMesh(DocMesh& doc) {
    size_t faceCount = 0, edgeCount = 0;
    for (const auto& b : doc.bodies) {
        faceCount += b.faces.size();
        edgeCount += b.edges.size();
    }
    doc.faceNormalCache.reserve(faceCount);
    doc.faceBBoxCache.reserve(faceCount);
    doc.edgeCentroidCache.reserve(edgeCount);
    doc.bodyBBoxCache.reserve(doc.bodies.size());
    for (auto& b : doc.bodies) {
        b.faceIds.assign(b.pos.size() / 3, 0u);
        Vec3 mn(1e30f, 1e30f, 1e30f), mx(-1e30f, -1e30f, -1e30f);
        for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
            mn.x = std::min(mn.x, b.pos[i]); mx.x = std::max(mx.x, b.pos[i]);
            mn.y = std::min(mn.y, b.pos[i + 1]); mx.y = std::max(mx.y, b.pos[i + 1]);
            mn.z = std::min(mn.z, b.pos[i + 2]); mx.z = std::max(mx.z, b.pos[i + 2]);
        }
        if (!b.pos.empty())
            doc.bodyBBoxCache[b.body_id] = {mn.x, mn.y, mn.z, mx.x, mx.y, mx.z};
        for (const auto& fr : b.faces) {
            float nx = 0, ny = 0, nz = 0, cx = 0, cy = 0, cz = 0, totalArea = 0;
            float fmn[3] = {1e30f, 1e30f, 1e30f}, fmx[3] = {-1e30f, -1e30f, -1e30f};
            bool any = false;
            // Same encoding as IdPass::encodeId: body[31:20], kind[19:18], topo[17:0].
            const uint32_t id = (b.body_id << 20) | (1u << 18) | fr.face_id;
            for (uint32_t t = 0; t < fr.tri_count; ++t) {
                const size_t base = (size_t(fr.first_tri) + t) * 3;
                if (base + 2 >= b.idx.size()) break;
                const uint32_t i0 = b.idx[base], i1 = b.idx[base + 1], i2 = b.idx[base + 2];
                const float* p0 = &b.pos[size_t(i0) * 3];
                const float* p1 = &b.pos[size_t(i1) * 3];
                const float* p2 = &b.pos[size_t(i2) * 3];
                b.faceIds[i0] = id; b.faceIds[i1] = id; b.faceIds[i2] = id;
                for (const float* p : {p0, p1, p2}) {
                    for (int c = 0; c < 3; ++c) {
                        fmn[c] = std::min(fmn[c], p[c]);
                        fmx[c] = std::max(fmx[c], p[c]);
                    }
                }
                any = true;
                const float e1x = p1[0]-p0[0], e1y = p1[1]-p0[1], e1z = p1[2]-p0[2];
                const float e2x = p2[0]-p0[0], e2y = p2[1]-p0[1], e2z = p2[2]-p0[2];
                const float crx = e1y*e2z - e1z*e2y;
                const float cry = e1z*e2x - e1x*e2z;
                const float crz = e1x*e2y - e1y*e2x;
                const float area = std::sqrt(crx*crx + cry*cry + crz*crz);
                nx += crx; ny += cry; nz += crz;
                cx += (p0[0]+p1[0]+p2[0]) * (1.0f/3.0f) * area;
                cy += (p0[1]+p1[1]+p2[1]) * (1.0f/3.0f) * area;
                cz += (p0[2]+p1[2]+p2[2]) * (1.0f/3.0f) * area;
                totalArea += area;
            }
            const uint64_t key = (uint64_t(b.body_id) << 32) | fr.face_id;
            if (any) doc.faceBBoxCache[key] = {fmn[0], fmn[1], fmn[2], fmx[0], fmx[1], fmx[2]};
            const float len = std::sqrt(nx*nx + ny*ny + nz*nz);
            if (len < 1e-12f) continue;
            if (totalArea > 1e-12f) { cx /= totalArea; cy /= totalArea; cz /= totalArea; }
            doc.faceNormalCache[key] = {nx/len, ny/len, nz/len, cx, cy, cz};
        }
        size_t segments = 0;
        for (const auto& es : b.edges)
            if (es.pt_count > 1) segments += size_t(es.pt_count) - 1;
        b.edgeLines.reserve(segments * 6);
        b.edgeIds.reserve(segments * 2);
        for (const auto& es : b.edges) {
            double cx = 0, cy = 0, cz = 0;
            const uint32_t id = (b.body_id << 20) | (2u << 18) | es.edge_id;
            for (uint32_t p = 0; p < es.pt_count; ++p) {
                const float* a = &b.edge_pts[(size_t(es.first_pt) + p) * 3];
                cx += a[0]; cy += a[1]; cz += a[2];
                if (p + 1 < es.pt_count) {
                    const float* next = a + 3;
                    b.edgeLines.insert(b.edgeLines.end(),
                        {a[0], a[1], a[2], next[0], next[1], next[2]});
                    b.edgeIds.push_back(id);
                    b.edgeIds.push_back(id);
                }
            }
            if (es.pt_count)
                doc.edgeCentroidCache[(uint64_t(b.body_id) << 32) | es.edge_id] =
                    {float(cx/es.pt_count), float(cy/es.pt_count), float(cz/es.pt_count)};
        }
        b.vertexPoints.reserve(b.brep_verts.size() * 3);
        b.vertexIds.reserve(b.brep_verts.size());
        for (const auto& v : b.brep_verts) {
            b.vertexPoints.insert(b.vertexPoints.end(), {v.xyz[0], v.xyz[1], v.xyz[2]});
            b.vertexIds.push_back((b.body_id << 20) | (3u << 18) | v.vertex_id);
        }
        if (!b.pos.empty()) {
            b.shadedMesh.positions = b.pos;
            b.shadedMesh.normals = b.nrm;
            b.shadedMesh.indices = b.idx;
            b.shadedMesh.edgeLines = b.edgeLines;
            b.shadedMesh.bbMin = mn;
            b.shadedMesh.bbMax = mx;
            b.shadedMesh.center = (mn + mx) * 0.5f;
            b.shadedMesh.radius = std::max(0.001f, length(mx - mn) * 0.5f);
        }
    }
    for (auto& field : doc.scalars) field.valueCount = field.values.size();
    for (auto& field : doc.displacements) field.vectorSize = field.vec.size();
    doc.derivedReady = true;
}

}  // namespace

bool parseAicm(const uint8_t* data, size_t size, DocMesh& out, std::string& err) {
    out = DocMesh{};
    err.clear();
    if (!data || size > size_t(std::numeric_limits<std::ptrdiff_t>::max())) {
        err = "invalid AICM buffer size";
        return false;
    }
    Cursor c(data, size);
    DocMesh mesh;
    char magic[4];
    c.read(magic, 4);
    if (!c.ok || std::memcmp(magic, "AICM", 4) != 0) {
        err = "bad magic (not an AICM file)";
        return false;
    }
    mesh.version = c.u32();
    uint32_t chunk_count = c.u32();
    if (!c.ok) { err = "truncated header"; return false; }
    if (mesh.version != 1) { err = "unsupported AICM version"; return false; }
    if (chunk_count > c.remaining() / 12) { err = "truncated chunk headers"; return false; }

    // FACE/EDGE/VERT bind to the most recent preceding BODY.
    BodyMesh* current = nullptr;
    std::unordered_map<uint32_t, std::vector<AppearanceRegion>> appearances;

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
        c.end = payload_start + static_cast<size_t>(payload_len);

        auto fcc = [&](const char* s) { return std::memcmp(fourcc, s, 4) == 0; };

        if (fcc("BODY")) {
            BodyMesh body;
            body.body_id = c.u32();
            body.name = c.str();
            body.color[0] = c.f32();
            body.color[1] = c.f32();
            body.color[2] = c.f32();
            body.n_verts = c.u32();
            body.n_tris = c.u32();
            const uint64_t vertices = uint64_t(body.n_verts) * 3;
            const uint64_t indices = uint64_t(body.n_tris) * 3;
            const uint64_t geometry_bytes = vertices * 2 * sizeof(float) +
                                            indices * sizeof(uint32_t);
            if (!c.ok || geometry_bytes > c.remaining() ||
                (c.remaining() - geometry_bytes != 0 && c.remaining() - geometry_bytes != 8) ||
                indices > uint64_t(std::numeric_limits<int>::max()) ||
                !c.arrayFits<float>(vertices) || !c.arrayFits<uint32_t>(indices)) {
                err = "invalid BODY payload length or count";
                return false;
            }
            mesh.bodies.push_back(std::move(body));
            current = &mesh.bodies.back();
            c.floats(current->pos, vertices);
            c.floats(current->nrm, vertices);
            c.uints(current->idx, indices);
            if (!c.ok) { err = "truncated BODY payload"; return false; }
            if (std::any_of(current->idx.begin(), current->idx.end(),
                            [&](uint32_t index) { return index >= current->n_verts; })) {
                err = "invalid BODY vertex index";
                return false;
            }
            // The only supported BODY trailer is the two-float material record.
            // Legacy files without it retain their dielectric appearance.
            if (c.remaining() == 8) {
                current->metallic = c.f32();
                current->roughness = c.f32();
            }
        } else if (fcc("APRG")) {
            const uint32_t body_id = c.u32();
            const uint32_t n = c.u32();
            if (!c.ok || c.remaining() % 28 != 0 || n != c.remaining() / 28) {
                err = "invalid APRG payload length";
                return false;
            }
            auto inserted = appearances.emplace(body_id, std::vector<AppearanceRegion>{});
            if (!inserted.second) { err = "duplicate APRG body"; return false; }
            auto& regions = inserted.first->second;
            regions.reserve(n);
            for (uint32_t i = 0; i < n; ++i) {
                AppearanceRegion region;
                region.first_face = c.u32();
                region.face_count = c.u32();
                for (float& channel : region.color) channel = c.f32();
                region.metallic = c.f32();
                region.roughness = c.f32();
                auto unit = [](float v) { return std::isfinite(v) && v >= 0.0f && v <= 1.0f; };
                if (!region.first_face || !region.face_count ||
                    uint64_t(region.first_face) + region.face_count >
                        uint64_t(std::numeric_limits<uint32_t>::max()) + 1 ||
                    !unit(region.color[0]) || !unit(region.color[1]) || !unit(region.color[2]) ||
                    !unit(region.metallic) || !unit(region.roughness)) {
                    err = "invalid APRG appearance region";
                    return false;
                }
                regions.push_back(region);
            }
        } else if (fcc("FACE")) {
            uint32_t n = c.u32();
            if (!current || !c.ok || uint64_t(n) * 13 != c.remaining() ||
                n > std::vector<FaceRange>{}.max_size()) {
                err = "invalid FACE payload length";
                return false;
            }
            std::vector<FaceRange> faces;
            faces.reserve(n);
            for (uint32_t i = 0; i < n; ++i) {
                FaceRange f;
                f.face_id = c.u32();
                f.first_tri = c.u32();
                f.tri_count = c.u32();
                f.surf_type = c.u8();
                if (uint64_t(f.first_tri) + f.tri_count > current->n_tris) {
                    err = "invalid FACE triangle range";
                    return false;
                }
                faces.push_back(f);
            }
            current->faces = std::move(faces);
        } else if (fcc("EDGE")) {
            uint32_t n = c.u32();
            if (!current || !c.ok || c.remaining() < 4 || n > (c.remaining() - 4) / 13 ||
                n > std::vector<EdgeSegment>{}.max_size()) {
                err = "invalid EDGE record count";
                return false;
            }
            // Validate the point section before allocating the edge records.
            Cursor points = c;
            points.skip(static_cast<size_t>(uint64_t(n) * 13));
            const uint32_t declared_pts = points.u32();
            const uint64_t point_elements = uint64_t(declared_pts) * 3;
            if (!points.ok || point_elements * sizeof(float) != points.remaining() ||
                !points.arrayFits<float>(point_elements)) {
                err = "invalid EDGE point payload length";
                return false;
            }
            std::vector<EdgeSegment> edges;
            edges.reserve(n);
            uint64_t line_vertices = 0;
            for (uint32_t i = 0; i < n; ++i) {
                EdgeSegment e;
                e.edge_id = c.u32();
                e.first_pt = c.u32();
                e.pt_count = c.u32();
                e.curve_type = c.u8();
                if (uint64_t(e.first_pt) + e.pt_count > declared_pts) {
                    err = "invalid EDGE point range";
                    return false;
                }
                if (e.pt_count > 1) line_vertices += uint64_t(e.pt_count - 1) * 2;
                if (line_vertices > uint64_t(std::numeric_limits<int>::max()) / 3 ||
                    line_vertices > uint64_t(std::numeric_limits<std::ptrdiff_t>::max()) /
                                        (3 * sizeof(float))) {
                    err = "unsupported EDGE draw size";
                    return false;
                }
                edges.push_back(e);
            }
            c.u32(); // declared_pts was validated before allocating records.
            std::vector<float> pts;
            c.floats(pts, point_elements);
            if (!c.ok) { err = "truncated EDGE payload"; return false; }
            current->edges = std::move(edges);
            current->edge_pts = std::move(pts);
        } else if (fcc("VERT")) {
            uint32_t n = c.u32();
            if (!current || !c.ok || uint64_t(n) * 16 != c.remaining() ||
                n > std::vector<BrepVertex>{}.max_size()) {
                err = "invalid VERT payload length";
                return false;
            }
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
            current->brep_verts = std::move(verts);
        } else if (fcc("SCLR")) {
            ScalarField sf;
            sf.body_id = c.u32();
            sf.name = c.str();
            sf.vmin = c.f32();
            sf.vmax = c.f32();
            uint32_t n = c.u32();
            if (!c.ok || uint64_t(n) * sizeof(float) != c.remaining() ||
                !c.arrayFits<float>(n)) {
                err = "invalid SCLR payload length";
                return false;
            }
            c.floats(sf.values, n);
            mesh.scalars.push_back(std::move(sf));
        } else if (fcc("DISP")) {
            DispField df;
            df.body_id = c.u32();
            df.scale_hint = c.f32();
            uint32_t n = c.u32();
            const uint64_t elements = uint64_t(n) * 3;
            if (!c.ok || elements * sizeof(float) != c.remaining() ||
                !c.arrayFits<float>(elements)) {
                err = "invalid DISP payload length";
                return false;
            }
            c.floats(df.vec, elements);
            mesh.displacements.push_back(std::move(df));
        } else if (fcc("LINS")) {
            LineSet ls;
            ls.name = c.str();
            uint32_t n_lines = c.u32();
            if (!c.ok || c.remaining() < 5 || n_lines > (c.remaining() - 5) / 4 ||
                !c.arrayFits<uint32_t>(n_lines)) {
                err = "invalid LINS line count";
                return false;
            }
            Cursor points = c;
            uint64_t sum = 0;
            for (uint32_t i = 0; i < n_lines; ++i) sum += points.u32();
            const uint32_t total_pts = points.u32();
            const uint64_t elements = uint64_t(total_pts) * 3;
            if (!points.ok || elements * sizeof(float) >= points.remaining() ||
                !points.arrayFits<float>(elements) || sum != total_pts) {
                err = "invalid LINS point payload length";
                return false;
            }
            points.skip(static_cast<size_t>(elements * sizeof(float)));
            const uint8_t has_scalar = points.u8();
            if (!points.ok || has_scalar > 1 ||
                points.remaining() != (has_scalar ? uint64_t(total_pts) * sizeof(float) : 0) ||
                (has_scalar && !points.arrayFits<float>(total_pts))) {
                err = "invalid LINS scalar payload length";
                return false;
            }
            c.uints(ls.pts_per_line, n_lines);
            c.u32(); // total_pts was validated above.
            c.floats(ls.pts, elements);
            ls.has_scalar = c.u8() != 0;
            if (ls.has_scalar) c.floats(ls.scalar, total_pts);
            mesh.lines.push_back(std::move(ls));
        }
        // else: unknown fourcc — fall through and skip by payload_len below.

        if (!c.ok) { err = "truncated chunk payload"; return false; }

        // Re-sync to the declared chunk boundary regardless of how much we consumed; this both
        // skips unknown chunks and guards against any per-chunk parse drift (spec requirement).
        c.p = payload_start + static_cast<size_t>(payload_len);
        c.end = file_end;
    }

    std::unordered_map<uint32_t, BodyMesh*> appearance_bodies;
    if (!appearances.empty()) {
        for (BodyMesh& body : mesh.bodies) {
            if (appearances.find(body.body_id) == appearances.end()) continue;
            auto inserted = appearance_bodies.emplace(body.body_id, &body);
            if (!inserted.second) inserted.first->second = nullptr;
        }
    }
    for (auto& entry : appearances) {
        const auto target = appearance_bodies.find(entry.first);
        if (target == appearance_bodies.end()) { err = "APRG references unknown body"; return false; }
        if (!target->second) { err = "ambiguous APRG body"; return false; }
        BodyMesh* body = target->second;
        auto& regions = entry.second;
        std::sort(regions.begin(), regions.end(),
            [](const AppearanceRegion& a, const AppearanceRegion& b) {
                return a.first_face < b.first_face;
            });
        std::vector<uint32_t> face_ids;
        face_ids.reserve(body->faces.size());
        for (const auto& face : body->faces) face_ids.push_back(face.face_id);
        std::sort(face_ids.begin(), face_ids.end());
        if (std::adjacent_find(face_ids.begin(), face_ids.end()) != face_ids.end()) {
            err = "ambiguous APRG face IDs";
            return false;
        }
        uint64_t previous_end = 0;
        for (const auto& region : regions) {
            const uint64_t end = uint64_t(region.first_face) + region.face_count;
            if (region.first_face < previous_end) {
                err = "overlapping APRG face spans";
                return false;
            }
            const auto face = std::lower_bound(face_ids.begin(), face_ids.end(), region.first_face);
            if (face == face_ids.end() || *face != region.first_face ||
                region.face_count > static_cast<size_t>(face_ids.end() - face) ||
                face[region.face_count - 1] != end - 1) {
                err = "APRG references missing faces";
                return false;
            }
            previous_end = end;
        }
        body->appearance_regions = std::move(regions);
    }

    prepareDerivedMesh(mesh);
    out = std::move(mesh);
    return true;
}

bool loadAicmBytes(const uint8_t* data, size_t size, DocMesh& out, std::string& err) {
    return parseAicm(data, size, out, err);
}

#ifdef __EMSCRIPTEN__
namespace {

// Percent-encode for query values (RFC 3986 unreserved left alone).
std::string urlEncode(const std::string& s) {
    static const char* kHex = "0123456789ABCDEF";
    std::string out;
    out.reserve(s.size() * 3);
    for (unsigned char c : s) {
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') {
            out.push_back(static_cast<char>(c));
        } else {
            out.push_back('%');
            out.push_back(kHex[c >> 4]);
            out.push_back(kHex[c & 0xF]);
        }
    }
    return out;
}

// In-memory AICM byte cache keyed by the aicm URI (revision-stable). Avoids re-fetching the
// same (doc,rev) tess over HTTP when the user switches tabs. Engine keeps only the last few
// published revs — 404s are not cached and fail the load as before.
constexpr size_t kAicmLruMaxEntries = 8;
constexpr size_t kAicmLruMaxBytes   = 64ull * 1024ull * 1024ull;  // 64 MB

struct AicmLruEntry {
    std::string key;               // original path/URI
    std::vector<uint8_t> bytes;
};

struct AicmLru {
    std::list<AicmLruEntry> order;  // front = most recent
    std::unordered_map<std::string, std::list<AicmLruEntry>::iterator> index;
    size_t totalBytes = 0;
    std::mutex mutex;

    const std::vector<uint8_t>* lookup(const std::string& key) {
        auto it = index.find(key);
        if (it == index.end()) return nullptr;
        order.splice(order.begin(), order, it->second);  // move to MRU
        return &it->second->bytes;
    }

    void insert(const std::string& key, std::vector<uint8_t>&& bytes) {
        if (bytes.empty() || bytes.size() > kAicmLruMaxBytes) return;
        auto it = index.find(key);
        if (it != index.end()) {
            totalBytes -= it->second->bytes.size();
            order.erase(it->second);
            index.erase(it);
        }
        totalBytes += bytes.size(); // Both operands are bounded by the 64 MiB cache cap.
        order.push_front(AicmLruEntry{key, std::move(bytes)});
        index[key] = order.begin();
        while (!order.empty() &&
               (order.size() > kAicmLruMaxEntries || totalBytes > kAicmLruMaxBytes)) {
            totalBytes -= order.back().bytes.size();
            index.erase(order.back().key);
            order.pop_back();
        }
    }
};

AicmLru& aicmLru() {
    static AicmLru cache;
    return cache;
}

}  // namespace

bool loadAicmFile(const std::string& path, DocMesh& out, std::string& err) {
    // The gateway binds this document grant to the workspace and epoch. Never turn a
    // daemon file path or shared-memory name into a fetch.
    constexpr const char* prefix = "aicm-cell:";
    if (path.compare(0, std::strlen(prefix), prefix) != 0) {
        err = "AICM requires a workspace document grant";
        return false;
    }
    std::string workspace, doc;
    try {
        const auto ref = nlohmann::json::parse(path.substr(std::strlen(prefix)));
        workspace = ref.at("workspace_key").get<std::string>();
        doc = ref.at("doc_id").get<std::string>();
        if (workspace.empty() || doc.empty() ||
            workspace.find('\0') != std::string::npos || doc.find('\0') != std::string::npos)
            throw std::runtime_error("Invalid document grant");
    } catch (const std::exception&) {
        err = "Invalid AICM workspace document grant";
        return false;
    }
    auto& cache = aicmLru();
    {
        std::lock_guard<std::mutex> guard(cache.mutex);
        if (const auto* bytes = cache.lookup(path))
            return loadAicmBytes(bytes->data(), bytes->size(), out, err);
    }
    const std::string url = "/files/aicm?workspace_key=" + urlEncode(workspace) +
                            "&doc_id=" + urlEncode(doc);
    auto bytes = chudware::webFetch(url, err);
    if (bytes.empty()) {
        if (err.empty()) err = "Empty AICM response";
        return false;
    }
    if (!loadAicmBytes(bytes.data(), bytes.size(), out, err)) return false;
    {
        std::lock_guard<std::mutex> guard(cache.mutex);
        cache.insert(path, std::move(bytes));
    }
    return true;
}
#else  // !__EMSCRIPTEN__

bool loadAicmFile(const std::string& path, DocMesh& out, std::string& err) {
    // aicm-shm:// URIs (and same-process cache) via aicm_handoff. File paths
    // still use the handoff map_file path so one code path covers both.
    chudware::aicm_handoff::Mapping m;
    if (!chudware::aicm_handoff::map(path, m, err)) return false;
    const bool ok = loadAicmBytes(m.data, m.size, out, err);
    chudware::aicm_handoff::unmap(m);
    return ok;
}

#endif  // __EMSCRIPTEN__

}  // namespace render
