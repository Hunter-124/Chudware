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
#include <map>
#include <set>
#include <vector>

#include "sim/overlay.h"

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
