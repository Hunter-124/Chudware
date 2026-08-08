// Unit tests for the FEA boundary-surface extractor (chudware::sim::fea_surface_body).
//
// Result plots (stress colormap + deformed shape) must render on the FEA mesh
// BOUNDARY surface — which has nodes all along the part — not the CAD body's OCCT
// tessellation (a flat box face is only its 8 corners, so a bent beam renders as a
// straight tilt). This pins the boundary-face extraction, winding (outward normals),
// tet10 mid-edge subdivision, and the surf_to_vol compaction the overlays index
// fields through.
//
// OCCT-free / gmsh-free: we build a structured box-beam tet mesh in code (the same
// Freudenthal split helper used by test_tet10.cpp), so no STEP/mesher is needed.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "sim/overlay.h"
#include "util/aicm_handoff.h"

using chudware::sim::fea_surface_body;
using chudware::sim::FeaSurface;

namespace {

// ── Structured box tet mesh (mirrors test_tet10.cpp's BoxMesh helpers) ─────────
struct BoxMesh {
    std::vector<double> points;  // n*3 flat
    std::vector<int> tets;       // m*K flat
    int nodes_per_elem = 4;
    int nx = 0, ny = 0, nz = 0;
    double Lx = 0, Ly = 0, Lz = 0;
};

BoxMesh make_box_tet4(int nx, int ny, int nz, double Lx, double Ly, double Lz) {
    BoxMesh m;
    m.nx = nx; m.ny = ny; m.nz = nz; m.Lx = Lx; m.Ly = Ly; m.Lz = Lz;
    m.nodes_per_elem = 4;
    const int NX = nx + 1, NY = ny + 1, NZ = nz + 1;
    auto gid = [&](int i, int j, int k) { return (k * NY + j) * NX + i; };
    m.points.resize(static_cast<size_t>(NX) * NY * NZ * 3);
    for (int k = 0; k < NZ; ++k)
        for (int j = 0; j < NY; ++j)
            for (int i = 0; i < NX; ++i) {
                const int id = gid(i, j, k);
                m.points[id * 3 + 0] = Lx * i / nx;
                m.points[id * 3 + 1] = Ly * j / ny;
                m.points[id * 3 + 2] = Lz * k / nz;
            }
    static const int TET[6][4] = {{0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
                                  {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
    for (int k = 0; k < nz; ++k)
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                const int c[8] = {gid(i, j, k),         gid(i + 1, j, k),
                                  gid(i + 1, j + 1, k), gid(i, j + 1, k),
                                  gid(i, j, k + 1),     gid(i + 1, j, k + 1),
                                  gid(i + 1, j + 1, k + 1), gid(i, j + 1, k + 1)};
                for (auto& tt : TET)
                    for (int v = 0; v < 4; ++v) m.tets.push_back(c[tt[v]]);
            }
    return m;
}

BoxMesh promote_to_tet10(const BoxMesh& m4) {
    BoxMesh m = m4;
    m.nodes_per_elem = 10;
    m.tets.clear();
    std::map<std::pair<int, int>, int> edge_mid;
    auto mid_node = [&](int a, int b) {
        auto key = std::make_pair(std::min(a, b), std::max(a, b));
        auto it = edge_mid.find(key);
        if (it != edge_mid.end()) return it->second;
        const int id = static_cast<int>(m.points.size() / 3);
        for (int c = 0; c < 3; ++c)
            m.points.push_back(0.5 * (m4.points[a * 3 + c] + m4.points[b * 3 + c]));
        edge_mid[key] = id;
        return id;
    };
    static const int EDGES[6][2] = {{0, 1}, {1, 2}, {2, 0}, {3, 0}, {3, 2}, {3, 1}};
    const int n4 = static_cast<int>(m4.tets.size() / 4);
    for (int e = 0; e < n4; ++e) {
        const int* t = &m4.tets[e * 4];
        for (int v = 0; v < 4; ++v) m.tets.push_back(t[v]);
        for (auto& ed : EDGES) m.tets.push_back(mid_node(t[ed[0]], t[ed[1]]));
    }
    return m;
}

// All boundary tet-faces (sorted global corner-id triples that occur in exactly one
// tet) of a tet4/tet10 corner connectivity — the reference set the extractor must hit.
std::set<std::array<int, 3>> boundary_face_keys(const BoxMesh& m) {
    static const int FACE[4][3] = {{0, 1, 2}, {0, 1, 3}, {0, 2, 3}, {1, 2, 3}};
    const int stride = m.nodes_per_elem;
    const int nt = static_cast<int>(m.tets.size() / stride);
    std::map<std::array<int, 3>, int> cnt;
    for (int e = 0; e < nt; ++e) {
        const int* t = &m.tets[e * stride];
        for (auto& f : FACE) {
            std::array<int, 3> key = {t[f[0]], t[f[1]], t[f[2]]};
            std::sort(key.begin(), key.end());
            ++cnt[key];
        }
    }
    std::set<std::array<int, 3>> b;
    for (auto& kv : cnt)
        if (kv.second == 1) b.insert(kv.first);
    return b;
}

// The set of corner-node ids that lie on at least one boundary face.
std::set<int> boundary_corner_nodes(const BoxMesh& m) {
    std::set<int> s;
    for (const auto& key : boundary_face_keys(m))
        for (int g : key) s.insert(g);
    return s;
}

double mesh_centroid(const std::vector<float>& pos, double c[3]) {
    const size_t nv = pos.size() / 3;
    c[0] = c[1] = c[2] = 0.0;
    for (size_t i = 0; i < nv; ++i)
        for (int k = 0; k < 3; ++k) c[k] += pos[i * 3 + k];
    if (nv) { c[0] /= nv; c[1] /= nv; c[2] /= nv; }
    return static_cast<double>(nv);
}

uint16_t u16le(const uint8_t* p) {
    return static_cast<uint16_t>(p[0]) |
           (static_cast<uint16_t>(p[1]) << 8);
}

uint32_t u32le(const uint8_t* p) {
    return static_cast<uint32_t>(p[0]) |
           (static_cast<uint32_t>(p[1]) << 8) |
           (static_cast<uint32_t>(p[2]) << 16) |
           (static_cast<uint32_t>(p[3]) << 24);
}

uint64_t u64le(const uint8_t* p) {
    uint64_t value = 0;
    for (int i = 0; i < 8; ++i) value |= static_cast<uint64_t>(p[i]) << (8 * i);
    return value;
}

float f32le(const uint8_t* p) {
    const uint32_t bits = u32le(p);
    float value;
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

struct ScalarWire {
    float min = 0.0f;
    float max = 0.0f;
    std::vector<float> values;
};

struct HandoffCleanup {
    ~HandoffCleanup() { chudware::aicm_handoff::release_all(); }
};

bool read_scalar_wire(const std::string& uri, const std::string& wanted,
                      ScalarWire& scalar) {
    chudware::aicm_handoff::Mapping mapping;
    std::string err;
    if (!chudware::aicm_handoff::map(uri, mapping, err)) return false;
    const uint8_t* data = mapping.data;
    const size_t size = mapping.size;
    bool found = false;
    if (size >= 12 && std::memcmp(data, "AICM", 4) == 0) {
        const uint32_t chunks = u32le(data + 8);
        size_t off = 12;
        for (uint32_t i = 0; i < chunks && off <= size; ++i) {
            if (size - off < 12) break;
            const uint8_t* tag = data + off;
            const uint64_t payload_size = u64le(data + off + 4);
            off += 12;
            if (payload_size > size - off) break;
            const uint8_t* payload = data + off;
            if (std::memcmp(tag, "SCLR", 4) == 0 && payload_size >= 18) {
                const uint16_t name_size = u16le(payload + 4);
                const size_t metadata = 4 + 2 + static_cast<size_t>(name_size) + 4 + 4 + 4;
                if (metadata <= payload_size) {
                    const std::string name(reinterpret_cast<const char*>(payload + 6),
                                           name_size);
                    const uint8_t* range = payload + 6 + name_size;
                    const uint32_t count = u32le(range + 8);
                    if (count <= (payload_size - metadata) / 4 && name == wanted) {
                        scalar.min = f32le(range);
                        scalar.max = f32le(range + 4);
                        scalar.values.resize(count);
                        const uint8_t* values = range + 12;
                        for (uint32_t j = 0; j < count; ++j)
                            scalar.values[j] = f32le(values + static_cast<size_t>(j) * 4);
                        found = true;
                        break;
                    }
                }
            }
            off += static_cast<size_t>(payload_size);
        }
    }
    chudware::aicm_handoff::unmap(mapping);
    return found;
}

}  // namespace

// ── tet4: vertices = unique boundary nodes; no interior node leaks ─────────────
TEST_CASE("fea_surface_body tet4: boundary nodes only, valid indices") {
    BoxMesh m = make_box_tet4(4, 2, 2, 4.0, 1.0, 1.0);
    FeaSurface fs = fea_surface_body(m.points, m.tets, 4);

    const size_t nv = fs.body.pos.size() / 3;
    CHECK(nv == fs.surf_to_vol.size());
    CHECK(nv > 8);  // far more than the 8 STEP corners — that's the whole point

    // For a solid box beam EVERY corner node is on the surface (no interior corner).
    std::set<int> ref = boundary_corner_nodes(m);
    CHECK(nv == ref.size());

    // surf_to_vol carries exactly the boundary-node set (no interior node leaked in).
    std::set<int> got(fs.surf_to_vol.begin(), fs.surf_to_vol.end());
    CHECK(got == ref);
    CHECK(got.size() == fs.surf_to_vol.size());  // no duplicate compaction

    // Every idx entry is a valid vertex.
    REQUIRE(fs.body.idx.size() % 3 == 0);
    for (uint32_t i : fs.body.idx) CHECK(static_cast<size_t>(i) < nv);

    MESSAGE("tet4 boundary verts = ", nv, "  tris = ", fs.body.idx.size() / 3);
}

// ── Boundary correctness: every emitted triangle came from a real boundary face ─
TEST_CASE("fea_surface_body tet4: no interior faces leaked") {
    BoxMesh m = make_box_tet4(3, 2, 2, 3.0, 2.0, 2.0);
    FeaSurface fs = fea_surface_body(m.points, m.tets, 4);
    std::set<std::array<int, 3>> ref = boundary_face_keys(m);

    // Each emitted triangle's GLOBAL corner triple (via surf_to_vol) must be a boundary
    // face key. (tet4 -> one triangle per boundary face, so counts match exactly too.)
    const size_t ntri = fs.body.idx.size() / 3;
    std::set<std::array<int, 3>> seen;
    for (size_t t = 0; t < ntri; ++t) {
        std::array<int, 3> key = {fs.surf_to_vol[fs.body.idx[t * 3 + 0]],
                                  fs.surf_to_vol[fs.body.idx[t * 3 + 1]],
                                  fs.surf_to_vol[fs.body.idx[t * 3 + 2]]};
        std::sort(key.begin(), key.end());
        CHECK(ref.count(key) == 1);  // came from exactly one tet (boundary)
        seen.insert(key);
    }
    CHECK(seen.size() == ref.size());        // every boundary face emitted
    CHECK(ntri == ref.size());               // exactly one tri per boundary face
}

// ── tet10: mid-edge boundary nodes included; each face -> 4 sub-triangles ───────
TEST_CASE("fea_surface_body tet10: mid-edge nodes + 4 sub-tris per face") {
    BoxMesh m4 = make_box_tet4(3, 2, 2, 3.0, 2.0, 2.0);
    BoxMesh m10 = promote_to_tet10(m4);

    FeaSurface fs4 = fea_surface_body(m4.points, m4.tets, 4);
    FeaSurface fs10 = fea_surface_body(m10.points, m10.tets, 10);

    const size_t nv4 = fs4.body.pos.size() / 3;
    const size_t nv10 = fs10.body.pos.size() / 3;
    // tet10 surface adds the boundary mid-edge nodes -> strictly more verts.
    CHECK(nv10 > nv4);

    // Each boundary face is subdivided into 4 sub-triangles.
    const size_t nbf = boundary_face_keys(m4).size();
    CHECK(fs10.body.idx.size() / 3 == 4 * nbf);

    // Indices valid; surf_to_vol deduped.
    for (uint32_t i : fs10.body.idx) CHECK(static_cast<size_t>(i) < nv10);
    std::set<int> got(fs10.surf_to_vol.begin(), fs10.surf_to_vol.end());
    CHECK(got.size() == fs10.surf_to_vol.size());

    MESSAGE("tet10 boundary verts = ", nv10, " (tet4 = ", nv4,
            ")  tris = ", fs10.body.idx.size() / 3);
}

// ── Normals point outward (convex box): n . (vertex - centroid) >= ~0 ──────────
TEST_CASE("fea_surface_body normals point outward (convex box)") {
    BoxMesh m = make_box_tet4(3, 3, 3, 3.0, 3.0, 3.0);
    FeaSurface fs = fea_surface_body(m.points, m.tets, 4);

    double c[3];
    const size_t nv = static_cast<size_t>(mesh_centroid(fs.body.pos, c));
    REQUIRE(fs.body.nrm.size() == nv * 3);
    for (size_t i = 0; i < nv; ++i) {
        const double rx = fs.body.pos[i * 3 + 0] - c[0];
        const double ry = fs.body.pos[i * 3 + 1] - c[1];
        const double rz = fs.body.pos[i * 3 + 2] - c[2];
        const double nx = fs.body.nrm[i * 3 + 0];
        const double ny = fs.body.nrm[i * 3 + 1];
        const double nz = fs.body.nrm[i * 3 + 2];
        // Normalized normal (guard already applied) — must point away from centroid.
        const double dot = nx * rx + ny * ry + nz * rz;
        // Allow a small negative tolerance for edge/corner averaging.
        CHECK(dot > -0.2);
        // Normals must be unit length.
        CHECK(std::sqrt(nx * nx + ny * ny + nz * nz) == doctest::Approx(1.0).epsilon(1e-4));
    }
}

TEST_CASE("fea_surface_body rejects malformed and non-finite meshes") {
    const std::vector<double> points = {
        0, 0, 0,
        1, 0, 0,
        0, 1, 0,
        0, 0, 1,
    };
    const std::vector<int> tet = {0, 1, 2, 3};
    CHECK_FALSE(fea_surface_body(points, tet, 4).body.pos.empty());

    CHECK(fea_surface_body(points, {0, 1, 2, 4}, 4).body.pos.empty());
    CHECK(fea_surface_body(points, {0, 1, 2}, 4).body.pos.empty());
    CHECK(fea_surface_body(points, tet, 6).body.pos.empty());
    CHECK(fea_surface_body(points, {0, 1, 1, 3}, 4).body.pos.empty());

    std::vector<double> coplanar = points;
    coplanar[3 * 3 + 2] = 0.0;
    coplanar[3 * 3 + 0] = 0.25;
    coplanar[3 * 3 + 1] = 0.25;
    CHECK(fea_surface_body(coplanar, tet, 4).body.pos.empty());

    std::vector<double> nonfinite = points;
    nonfinite[0] = std::numeric_limits<double>::infinity();
    CHECK(fea_surface_body(nonfinite, tet, 4).body.pos.empty());
}

TEST_CASE("fea_surface_body rejects non-manifold faces") {
    const std::vector<double> points = {
        0, 0, 0,
        1, 0, 0,
        0, 1, 0,
        0, 0, 1,
        0, 0, -1,
        0.25, 0.25, 2,
    };
    const std::vector<int> three_on_one_face = {
        0, 1, 2, 3,
        0, 1, 2, 4,
        0, 1, 2, 5,
    };
    FeaSurface fs = fea_surface_body(points, three_on_one_face, 4);
    CHECK(fs.body.pos.empty());
    CHECK(fs.surf_to_vol.empty());
}

TEST_CASE("map_to_surface preserves exact node correspondence and fails closed") {
    const std::vector<double> volume = {
        0, 0, 0,
        2, 0, 0,
        0, 2, 0,
    };
    const std::vector<double> surface = {
        2, 0, 0,
        0, 0, 0,
        0, 2, 0,
    };
    const std::vector<double> vectors = {
        10, 11, 12,
        20, 21, 22,
        30, 31, 32,
    };
    const std::vector<double> mapped =
        chudware::sim::map_to_surface(surface, volume, vectors, 3);
    CHECK(mapped == std::vector<double>{20, 21, 22, 10, 11, 12, 30, 31, 32});

    CHECK(chudware::sim::map_to_surface(surface, volume, {1, 2}, 1).empty());
    CHECK(chudware::sim::map_to_surface(surface, volume, vectors, 0).empty());
    std::vector<double> bad_values = vectors;
    bad_values[4] = std::numeric_limits<double>::quiet_NaN();
    CHECK(chudware::sim::map_to_surface(surface, volume, bad_values, 3).empty());
}

TEST_CASE("overlay writers reject mismatched nodal fields") {
    const std::vector<double> points = {
        0, 0, 0,
        1, 0, 0,
        0, 1, 0,
        0, 0, 1,
    };
    const std::vector<int> tet = {0, 1, 2, 3};
    const std::vector<double> scalar(4, 1.0);

    const std::vector<double> displacement(12, 0.0);
    CHECK(chudware::sim::static_overlay(
              "", points, tet, 4, {1.0, 2.0}, scalar, displacement,
              {}, {}, {}, 200.0, nlohmann::json::object())
              .empty());
    CHECK(chudware::sim::fatigue_overlay(
              "", points, tet, 4, scalar, {1.0, 2.0}, scalar)
              .empty());
    CHECK(chudware::sim::modal_overlay(
              "", points, tet, 4, {0.0, 0.0, 0.0}, 10.0)
              .empty());
}

TEST_CASE("static_overlay maps exact nodal stress and emits a finite honest range") {
    const std::vector<double> points = {
        0, 0, 0,
        1, 0, 0,
        0, 1, 0,
        0, 0, 1,
    };
    const std::vector<int> tet = {0, 1, 2, 3};
    const std::vector<double> stress = {10, 20, 30, 40};
    const std::vector<double> displacement(12, 0.0);
    const std::vector<double> disp_mag(4, 0.0);
    const FeaSurface fs = fea_surface_body(points, tet, 4);
    REQUIRE(fs.surf_to_vol.size() == 4);

    HandoffCleanup cleanup;
    const std::string uri = chudware::sim::static_overlay(
        "", points, tet, 4, stress, disp_mag, displacement,
        {}, {}, {}, 200.0, nlohmann::json::object());
    REQUIRE_FALSE(uri.empty());
    ScalarWire wire;
    REQUIRE(read_scalar_wire(uri, "von_mises", wire));
    REQUIRE(wire.values.size() == fs.surf_to_vol.size());
    for (size_t i = 0; i < wire.values.size(); ++i)
        CHECK(wire.values[i] ==
              doctest::Approx(stress[static_cast<size_t>(fs.surf_to_vol[i])]));
    CHECK(std::isfinite(wire.min));
    CHECK(std::isfinite(wire.max));
    CHECK(wire.min == doctest::Approx(0.0f));
    CHECK(wire.max == doctest::Approx(40.0f));
    CHECK(wire.max > wire.min);
    ScalarWire zero_range;
    REQUIRE(read_scalar_wire(uri, "displacement_mag", zero_range));
    CHECK(std::isfinite(zero_range.min));
    CHECK(std::isfinite(zero_range.max));
    CHECK(zero_range.max > zero_range.min);
}
