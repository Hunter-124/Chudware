#include "aicm_loader.h"

#include "util/aicm_handoff.h"

#ifdef __EMSCRIPTEN__
#include "core/web_fetch.h"
#endif

#include <algorithm>
#include <cmath>
#include <limits>
#include <cstdio>
#include <cstring>
#include <list>
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

    std::unordered_map<uint32_t, BodyMesh*> appearance_bodies;
    if (!appearances.empty()) {
        for (BodyMesh& body : out.bodies) {
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

    bool lookup(const std::string& key, std::vector<uint8_t>& out) {
        auto it = index.find(key);
        if (it == index.end()) return false;
        order.splice(order.begin(), order, it->second);  // move to MRU
        out = it->second->bytes;
        return true;
    }

    void insert(const std::string& key, const std::vector<uint8_t>& bytes) {
        if (bytes.empty()) return;
        auto it = index.find(key);
        if (it != index.end()) {
            totalBytes -= it->second->bytes.size();
            order.erase(it->second);
            index.erase(it);
        }
        totalBytes += bytes.size();
        order.push_front(AicmLruEntry{key, bytes});  // copy into cache
        index[key] = order.begin();
        while (!order.empty() &&
               (order.size() > kAicmLruMaxEntries || totalBytes > kAicmLruMaxBytes)) {
            // Keep at least the MRU entry even if it alone exceeds the byte cap.
            if (order.size() == 1) break;
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
    // Browser has no /dev/shm or daemon FS — pull bytes from the gateway.
    // Hit the in-memory LRU first so a warm (doc,rev) switch never re-GETs.
    std::vector<uint8_t> bytes;
    const bool cacheHit = aicmLru().lookup(path, bytes);

    const bool is_shm = chudware::aicm_handoff::is_shm_uri(path);
    if (!cacheHit) {
        std::string url;
        if (is_shm) {
            // aicm-shm://[/]chudware_<key> → ?shm=chudware_<key> (gateway rejects a leading '/').
            std::string name = path.substr(std::strlen("aicm-shm://"));
            while (!name.empty() && name.front() == '/') name.erase(name.begin());
            if (name.empty()) {
                err = "malformed aicm-shm uri";
                return false;
            }
            url = "/files/aicm?shm=" + urlEncode(name);
        } else {
            if (path.empty()) {
                err = "empty AICM path";
                return false;
            }
            url = "/files/aicm?path=" + urlEncode(path);
        }

        std::string ferr;
        bytes = chudware::webFetch(url, ferr);
        if (bytes.empty()) {
            // 404 / network: fail the load as before. Do NOT cache empties.
            err = ferr.empty() ? ("empty AICM response for " + path) : ferr;
            return false;
        }
        aicmLru().insert(path, bytes);  // cache a copy; keep local bytes for parse
    }

    if (is_shm) {
        // Gateway serves the raw shm object: uint64 LE size header + AICM payload
        // (same layout map_shm strips on native).
        if (bytes.size() < 8) {
            err = "shm payload too short";
            return false;
        }
        uint64_t sz = 0;
        std::memcpy(&sz, bytes.data(), 8);
        if (8ull + sz > static_cast<uint64_t>(bytes.size())) {
            err = "shm payload size exceeds response";
            return false;
        }
        return loadAicmBytes(bytes.data() + 8, static_cast<size_t>(sz), out, err);
    }
    return loadAicmBytes(bytes.data(), bytes.size(), out, err);
}
#else  // !__EMSCRIPTEN__

bool loadAicmFile(const std::string& path, DocMesh& out, std::string& err) {
    // T-PERF10: aicm-shm:// URIs (and same-process cache) via aicm_handoff. File paths
    // still use the handoff map_file path so one code path covers both.
    chudware::aicm_handoff::Mapping m;
    if (!chudware::aicm_handoff::map(path, m, err)) return false;
    const bool ok = loadAicmBytes(m.data, m.size, out, err);
    chudware::aicm_handoff::unmap(m);
    return ok;
}

#endif  // __EMSCRIPTEN__

}  // namespace render
