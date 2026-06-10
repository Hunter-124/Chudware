#include "mesh.h"

#include <cstdio>
#include <cstring>
#include <limits>
#include <unordered_map>
#include <utility>

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
    std::unordered_map<Key, uint32_t, KeyHash> lut;
    std::vector<Vec3> accNormals;  // accumulated (area-weighted) normals per unique vertex

    Vec3 mn(std::numeric_limits<float>::max(), std::numeric_limits<float>::max(),
            std::numeric_limits<float>::max());
    Vec3 mx(-std::numeric_limits<float>::max(), -std::numeric_limits<float>::max(),
            -std::numeric_limits<float>::max());

    auto addVertex = [&](float x, float y, float z, const Vec3& fn) -> uint32_t {
        Key k = quant(x, y, z);
        auto it = lut.find(k);
        uint32_t idx;
        if (it == lut.end()) {
            idx = static_cast<uint32_t>(out.positions.size() / 3);
            lut.emplace(k, idx);
            out.positions.insert(out.positions.end(), {x, y, z});
            accNormals.push_back(fn);
        } else {
            idx = it->second;
            accNormals[idx] = accNormals[idx] + fn;  // area-weighted (fn not normalized yet)
        }
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
        Vec3 fn = cross(b - a, c - a);  // length proportional to triangle area => area weighting
        uint32_t ia = addVertex(a.x, a.y, a.z, fn);
        uint32_t ib = addVertex(b.x, b.y, b.z, fn);
        uint32_t ic = addVertex(c.x, c.y, c.z, fn);
        out.indices.insert(out.indices.end(), {ia, ib, ic});
    }
    std::fclose(f);

    out.normals.resize(out.positions.size());
    for (size_t i = 0; i < accNormals.size(); ++i) {
        Vec3 n = normalize(accNormals[i]);
        out.normals[i * 3 + 0] = n.x;
        out.normals[i * 3 + 1] = n.y;
        out.normals[i * 3 + 2] = n.z;
    }

    out.bbMin = mn;
    out.bbMax = mx;
    out.center = (mn + mx) * 0.5f;
    out.radius = std::max(0.001f, length(mx - mn) * 0.5f);

    // Feature edges: boundary edges, plus edges where the two adjacent faces meet at an
    // angle sharper than the crease threshold. Gives clean CAD line work without the
    // triangulation noise of a full wireframe.
    struct EdgeRec {
        Vec3 n0, n1;
        int count = 0;
    };
    std::unordered_map<uint64_t, EdgeRec> edges;
    auto ekey = [](uint32_t a, uint32_t b) -> uint64_t {
        if (a > b) std::swap(a, b);
        return (uint64_t(a) << 32) | uint64_t(b);
    };
    auto pos = [&](uint32_t i) { return Vec3(out.positions[i * 3], out.positions[i * 3 + 1],
                                             out.positions[i * 3 + 2]); };
    for (size_t t = 0; t + 2 < out.indices.size(); t += 3) {
        uint32_t i0 = out.indices[t], i1 = out.indices[t + 1], i2 = out.indices[t + 2];
        Vec3 fn = normalize(cross(pos(i1) - pos(i0), pos(i2) - pos(i0)));
        uint32_t tri[3][2] = {{i0, i1}, {i1, i2}, {i2, i0}};
        for (auto& e : tri) {
            EdgeRec& r = edges[ekey(e[0], e[1])];
            if (r.count == 0) r.n0 = fn; else r.n1 = fn;
            r.count++;
        }
    }
    const float creaseCos = std::cos(25.0f * 3.14159265f / 180.0f);
    for (auto& kv : edges) {
        const EdgeRec& r = kv.second;
        bool feature = (r.count == 1) || (r.count == 2 && dot(r.n0, r.n1) < creaseCos);
        if (!feature) continue;
        uint32_t a = uint32_t(kv.first >> 32), b = uint32_t(kv.first & 0xffffffff);
        Vec3 pa = pos(a), pb = pos(b);
        out.edgeLines.insert(out.edgeLines.end(), {pa.x, pa.y, pa.z, pb.x, pb.y, pb.z});
    }
    return !out.indices.empty();
}
