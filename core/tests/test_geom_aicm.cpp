// Wave 2 verification: OCCT shape -> geom::tessellate_body -> aicm::write_aicm ->
// HOST render::parseAicm round-trip. The host loader (host/src/render/aicm_loader)
// is the contract oracle: if it accepts our bytes and the geometry invariants hold,
// the C++ emitter is byte-compatible with the format the host consumes.
//
// Invariants are OCCT-version-INDEPENDENT (counts/ranges/coverage/bbox), NOT a byte
// compare against the hand-authored docs/contracts/fixtures/box.aicm.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#include "geom/aicm.h"
#include "geom/features.h"
#include "geom/tessellate.h"
#include "aicm_loader.h"  // host loader (host/src/render on the include path)

namespace {

// Tessellate `shape` -> AICM bytes -> parse with the HOST loader. Asserts the host
// accepts it and yields exactly one body; returns the parsed DocMesh + raw bytes.
struct Loaded {
    render::DocMesh doc;
    std::vector<uint8_t> bytes;
};

Loaded roundtrip(const TopoDS_Shape& shape, const std::string& name,
                 const float color[3]) {
    aicm::AicmBody body = geom::tessellate_body(shape, 1, name, color);
    std::vector<aicm::AicmBody> bodies{body};
    std::vector<uint8_t> bytes = aicm::write_aicm(bodies);

    render::DocMesh doc;
    std::string err;
    bool ok = render::parseAicm(bytes.data(), bytes.size(), doc, err);
    REQUIRE_MESSAGE(ok, "host loader rejected AICM: " << err);
    return {std::move(doc), std::move(bytes)};
}

// Structural invariants shared by every body (independent of the source shape).
void check_common(const render::BodyMesh& b) {
    // Geometry array sizes are self-consistent.
    CHECK(b.pos.size() == 3ull * b.n_verts);
    CHECK(b.nrm.size() == 3ull * b.n_verts);
    CHECK(b.idx.size() == 3ull * b.n_tris);

    // Every triangle index is in range.
    for (uint32_t i : b.idx) CHECK(i < b.n_verts);

    // Face ranges are contiguous, in order, and cover exactly all triangles.
    uint32_t running = 0;
    for (const render::FaceRange& f : b.faces) {
        CHECK(f.first_tri == running);
        running += f.tri_count;
    }
    CHECK(running == b.n_tris);

    // Every edge polyline stays within the edge_pts array.
    const uint32_t total_pts = static_cast<uint32_t>(b.edge_pts.size() / 3);
    for (const render::EdgeSegment& e : b.edges) {
        CHECK(e.first_pt + e.pt_count <= total_pts);
    }
}

// Axis-aligned bbox of the triangle vertices, expected to equal the requested dims.
void bbox(const render::BodyMesh& b, double lo[3], double hi[3]) {
    for (int k = 0; k < 3; ++k) {
        lo[k] = std::numeric_limits<double>::max();
        hi[k] = -std::numeric_limits<double>::max();
    }
    for (uint32_t v = 0; v < b.n_verts; ++v) {
        for (int k = 0; k < 3; ++k) {
            double c = b.pos[3ull * v + k];
            if (c < lo[k]) lo[k] = c;
            if (c > hi[k]) hi[k] = c;
        }
    }
}

}  // namespace

TEST_CASE("box tessellation round-trips through the host AICM loader") {
    const double DX = 20.0, DY = 10.0, DZ = 6.0;
    const float color[3] = {0.71f, 0.76f, 0.82f};
    Loaded ld = roundtrip(geom::make_box(DX, DY, DZ), "Body1", color);

    CHECK(ld.doc.version == 1);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& b = ld.doc.bodies[0];

    // Header / identity round-trip.
    CHECK(b.body_id == 1);
    CHECK(b.name == "Body1");
    CHECK(b.color[0] == doctest::Approx(0.71f));
    CHECK(b.color[1] == doctest::Approx(0.76f));
    CHECK(b.color[2] == doctest::Approx(0.82f));

    check_common(b);

    // Report the exact OCCT 7.9.3 tessellation stats for this box.
    uint32_t total_edge_pts = static_cast<uint32_t>(b.edge_pts.size() / 3);
    MESSAGE("box stats: n_verts=" << b.n_verts << " n_tris=" << b.n_tris
            << " faces=" << b.faces.size() << " edges=" << b.edges.size()
            << " brep_verts=" << b.brep_verts.size()
            << " total_edge_pts=" << total_edge_pts);

    // A box: 6 planar faces, 12 edges, 8 B-rep vertices, >= 12 tris.
    CHECK(b.faces.size() == 6);
    for (const render::FaceRange& f : b.faces) CHECK(f.surf_type == 0);  // plane
    CHECK(b.edges.size() == 12);
    for (const render::EdgeSegment& e : b.edges) CHECK(e.pt_count >= 2);
    CHECK(b.brep_verts.size() == 8);
    CHECK(b.n_tris >= 12);

    // Watertight bbox equals the requested dims (min corner at origin).
    double lo[3], hi[3];
    bbox(b, lo, hi);
    CHECK(lo[0] == doctest::Approx(0.0).epsilon(1e-4));
    CHECK(lo[1] == doctest::Approx(0.0).epsilon(1e-4));
    CHECK(lo[2] == doctest::Approx(0.0).epsilon(1e-4));
    CHECK((hi[0] - lo[0]) == doctest::Approx(DX).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(DY).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(DZ).epsilon(1e-4));

    // Every per-vertex normal is unit length.
    for (uint32_t v = 0; v < b.n_verts; ++v) {
        double nx = b.nrm[3ull * v + 0];
        double ny = b.nrm[3ull * v + 1];
        double nz = b.nrm[3ull * v + 2];
        CHECK(std::sqrt(nx * nx + ny * ny + nz * nz) == doctest::Approx(1.0).epsilon(1e-3));
    }
}

TEST_CASE("extruded rectangle round-trips through the host AICM loader") {
    const double W = 12.0, H = 8.0, D = 5.0;
    const float color[3] = {0.4f, 0.5f, 0.6f};
    Loaded ld = roundtrip(geom::extrude_rect(W, H, D), "Extrude1", color);

    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& b = ld.doc.bodies[0];

    CHECK(b.name == "Extrude1");
    check_common(b);

    // A prism of a rectangle is a box: 6 faces, 12 edges, 8 vertices.
    CHECK(b.faces.size() == 6);
    for (const render::FaceRange& f : b.faces) CHECK(f.surf_type == 0);  // plane
    CHECK(b.edges.size() == 12);
    CHECK(b.brep_verts.size() == 8);
    CHECK(b.n_tris >= 12);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

TEST_CASE("write_aicm is deterministic (byte-identical on repeat)") {
    const float color[3] = {0.71f, 0.76f, 0.82f};
    aicm::AicmBody body =
        geom::tessellate_body(geom::make_box(20.0, 10.0, 6.0), 1, "Body1", color);
    std::vector<aicm::AicmBody> bodies{body};

    std::vector<uint8_t> a = aicm::write_aicm(bodies);
    std::vector<uint8_t> c = aicm::write_aicm(bodies);
    CHECK(a == c);
    CHECK(!a.empty());
}
