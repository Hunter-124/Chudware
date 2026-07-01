#include "mesh.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <limits>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {
// Quantize a position to weld coincident vertices (STL stores no shared topology).
struct Key {
    int64_t x, y, z;
    bool operator==(const Key& o) const { return x == o.x && y == o.y && z == o.z; }
};
struct KeyHash {
    size_t operator()(const Key& k) const {
        size_t h = 1469598103934665603ull;
        for (int64_t v : {k.x, k.y, k.z}) {
            h ^= static_cast<size_t>(v);
            h *= 1099511628211ull;
        }
        return h;
    }
};
Key quant(float x, float y, float z) {
    const double s = 1000.0;  // 1e-3 mm welding tolerance
    return {static_cast<int64_t>(std::llround(x * s)),
            static_cast<int64_t>(std::llround(y * s)),
            static_cast<int64_t>(std::llround(z * s))};
}

// Crease threshold shared by smoothing-group normal computation AND feature-edge extraction:
// adjacent triangles whose face normals differ by more than this angle are considered a crease
// (their shared vertices are NOT smoothed together, and the shared edge is drawn as feature line).
// 30 deg cleanly separates fillet curvature from the planar faces it blends into, while still
// smoothing the many small facets within a single cylinder/fillet.
constexpr float kCreaseDeg = 30.0f;
const float kCreaseCos = std::cos(kCreaseDeg * 3.14159265f / 180.0f);
}  // namespace

bool loadBinarySTL(const std::string& path, Mesh& out, std::string& err) {
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) {
        err = "cannot open " + path;
        return false;
    }
    char header[80];
    if (std::fread(header, 1, 80, f) != 80) {
        err = "short header";
        std::fclose(f);
        return false;
    }
    uint32_t nTris = 0;
    if (std::fread(&nTris, 4, 1, f) != 1) {
        err = "no triangle count";
        std::fclose(f);
        return false;
    }

    out = Mesh{};

    // ------------------------------------------------------------------
    // Pass 1: read triangles, weld vertices by position (topology only), and accumulate the
    // per-triangle geometric face normal (area-weighted) and a normalized face normal per tri.
    // The welded set drives adjacency (for crease detection + feature edges); the *render* mesh
    // built in pass 2 splits welded vertices back apart at creases so a fillet's curvature never
    // bleeds across a crease onto an adjacent planar face.
    // ------------------------------------------------------------------
    std::unordered_map<Key, uint32_t, KeyHash> lut;
    std::vector<Vec3> weldPos;                 // unique welded positions
    std::vector<uint32_t> triWeld;             // 3 welded indices per triangle
    std::vector<Vec3> triFaceN;                // normalized face normal per triangle
    triWeld.reserve(size_t(nTris) * 3);
    triFaceN.reserve(nTris);

    Vec3 mn(std::numeric_limits<float>::max(), std::numeric_limits<float>::max(),
            std::numeric_limits<float>::max());
    Vec3 mx(-std::numeric_limits<float>::max(), -std::numeric_limits<float>::max(),
            -std::numeric_limits<float>::max());

    auto weld = [&](float x, float y, float z) -> uint32_t {
        Key k = quant(x, y, z);
        auto it = lut.find(k);
        if (it != lut.end()) return it->second;
        uint32_t idx = static_cast<uint32_t>(weldPos.size());
        lut.emplace(k, idx);
        weldPos.emplace_back(x, y, z);
        if (x < mn.x) mn.x = x; if (y < mn.y) mn.y = y; if (z < mn.z) mn.z = z;
        if (x > mx.x) mx.x = x; if (y > mx.y) mx.y = y; if (z > mx.z) mx.z = z;
        return idx;
    };

    for (uint32_t t = 0; t < nTris; ++t) {
        float buf[12];
        uint16_t attr;
        if (std::fread(buf, 4, 12, f) != 12 || std::fread(&attr, 2, 1, f) != 1) {
            err = "truncated triangle data";
            std::fclose(f);
            return false;
        }
        Vec3 a(buf[3], buf[4], buf[5]), b(buf[6], buf[7], buf[8]), c(buf[9], buf[10], buf[11]);
        uint32_t ia = weld(a.x, a.y, a.z);
        uint32_t ib = weld(b.x, b.y, b.z);
        uint32_t ic = weld(c.x, c.y, c.z);
        triWeld.insert(triWeld.end(), {ia, ib, ic});
        triFaceN.push_back(normalize(cross(b - a, c - a)));
    }
    std::fclose(f);

    const size_t nFaceTris = triFaceN.size();

    // ------------------------------------------------------------------
    // Group triangles into smoothing groups via a union-find over shared welded edges: two
    // triangles sharing an edge are unioned iff the dihedral angle between their face normals is
    // below the crease threshold. The union therefore floods across a smoothly-varying surface (a
    // cylinder, a fillet) — even all the way around a 360 deg cylinder, because each step is small —
    // but STOPS at every crease (fillet->plane, cylinder->cap). A welded vertex shared by N groups
    // is later split into N render vertices, so curvature on one side of a crease never bleeds onto
    // the planar face on the other side. Build is O(n) amortized (near-linear union-find).
    // ------------------------------------------------------------------
    std::vector<uint32_t> uf(nFaceTris);
    for (uint32_t i = 0; i < nFaceTris; ++i) uf[i] = i;
    std::function<uint32_t(uint32_t)> findRoot = [&](uint32_t a) {
        while (uf[a] != a) { uf[a] = uf[uf[a]]; a = uf[a]; }
        return a;
    };
    auto unite = [&](uint32_t a, uint32_t b) {
        uint32_t ra = findRoot(a), rb = findRoot(b);
        if (ra != rb) uf[ra] = rb;
    };

    // Find triangles adjacent across each welded edge and union the non-crease pairs.
    {
        std::unordered_map<uint64_t, uint32_t> firstTriOnEdge;  // edge -> tri index (+1, 0=empty)
        firstTriOnEdge.reserve(nFaceTris * 3);
        auto ekey = [](uint32_t a, uint32_t b) -> uint64_t {
            if (a > b) std::swap(a, b);
            return (uint64_t(a) << 32) | uint64_t(b);
        };
        for (uint32_t t = 0; t < nFaceTris; ++t) {
            uint32_t v[3] = {triWeld[t * 3], triWeld[t * 3 + 1], triWeld[t * 3 + 2]};
            uint32_t pairs[3][2] = {{v[0], v[1]}, {v[1], v[2]}, {v[2], v[0]}};
            for (auto& e : pairs) {
                uint64_t k = ekey(e[0], e[1]);
                auto it = firstTriOnEdge.find(k);
                if (it == firstTriOnEdge.end()) {
                    firstTriOnEdge.emplace(k, t + 1);
                } else {
                    uint32_t other = it->second - 1;
                    // Manifold edge shared by exactly two tris: smooth across it if not a crease.
                    if (dot(triFaceN[t], triFaceN[other]) >= kCreaseCos) unite(t, other);
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // Pass 2: build the render mesh. Split each welded vertex per distinct smoothing-group root that
    // touches it; accumulate area-weighted normals from only that group's triangles. Flat faces
    // collapse to a single constant normal -> zero streaks; smooth surfaces keep one shared normal.
    // ------------------------------------------------------------------
    out.positions.reserve(weldPos.size() * 3);
    out.normals.reserve(weldPos.size() * 3);
    out.indices.resize(nFaceTris * 3);

    // Per welded vertex: map smoothing-group root -> emitted split-vertex index.
    std::vector<std::unordered_map<uint32_t, uint32_t>> vgroups(weldPos.size());
    std::vector<Vec3> accN;  // accumulated area-weighted normal per emitted vertex
    accN.reserve(weldPos.size());

    auto emitCorner = [&](uint32_t wv, uint32_t root) -> uint32_t {
        auto& groups = vgroups[wv];
        auto it = groups.find(root);
        if (it != groups.end()) return it->second;
        uint32_t idx = static_cast<uint32_t>(out.positions.size() / 3);
        const Vec3& p = weldPos[wv];
        out.positions.insert(out.positions.end(), {p.x, p.y, p.z});
        out.normals.insert(out.normals.end(), {0.0f, 0.0f, 0.0f});  // filled below
        accN.emplace_back(0.0f, 0.0f, 0.0f);
        groups.emplace(root, idx);
        return idx;
    };

    for (uint32_t t = 0; t < nFaceTris; ++t) {
        uint32_t root = findRoot(t);
        uint32_t i0 = triWeld[t * 3], i1 = triWeld[t * 3 + 1], i2 = triWeld[t * 3 + 2];
        Vec3 areaN = cross(weldPos[i1] - weldPos[i0], weldPos[i2] - weldPos[i0]);  // ~2*area
        for (int k = 0; k < 3; ++k) {
            uint32_t wv = triWeld[t * 3 + k];
            uint32_t e = emitCorner(wv, root);
            out.indices[t * 3 + k] = e;
            accN[e] = accN[e] + areaN;
        }
    }
    for (size_t i = 0; i < accN.size(); ++i) {
        Vec3 n = normalize(accN[i]);
        out.normals[i * 3 + 0] = n.x;
        out.normals[i * 3 + 1] = n.y;
        out.normals[i * 3 + 2] = n.z;
    }

    out.bbMin = mn;
    out.bbMax = mx;
    out.center = (mn + mx) * 0.5f;
    out.radius = std::max(0.001f, length(mx - mn) * 0.5f);

    // Feature edges: boundary edges, plus edges where the two adjacent faces meet at an angle
    // sharper than the (shared) crease threshold. Gives clean CAD line work without the
    // triangulation noise of a full wireframe. Computed on the welded set so an edge appears once.
    struct EdgeRec {
        Vec3 n0, n1;
        int count = 0;
    };
    std::unordered_map<uint64_t, EdgeRec> edges;
    auto ekey = [](uint32_t a, uint32_t b) -> uint64_t {
        if (a > b) std::swap(a, b);
        return (uint64_t(a) << 32) | uint64_t(b);
    };
    for (size_t t = 0; t < nFaceTris; ++t) {
        uint32_t i0 = triWeld[t * 3], i1 = triWeld[t * 3 + 1], i2 = triWeld[t * 3 + 2];
        const Vec3& fn = triFaceN[t];
        uint32_t tri[3][2] = {{i0, i1}, {i1, i2}, {i2, i0}};
        for (auto& e : tri) {
            EdgeRec& r = edges[ekey(e[0], e[1])];
            if (r.count == 0) r.n0 = fn; else r.n1 = fn;
            r.count++;
        }
    }
    for (auto& kv : edges) {
        const EdgeRec& r = kv.second;
        bool feature = (r.count == 1) || (r.count == 2 && dot(r.n0, r.n1) < kCreaseCos);
        if (!feature) continue;
        uint32_t a = uint32_t(kv.first >> 32), b = uint32_t(kv.first & 0xffffffff);
        const Vec3& pa = weldPos[a];
        const Vec3& pb = weldPos[b];
        out.edgeLines.insert(out.edgeLines.end(), {pa.x, pa.y, pa.z, pb.x, pb.y, pb.z});
    }
    return !out.indices.empty();
}
